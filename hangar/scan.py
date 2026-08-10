"""Static security analysis, run before code is executed for the first time.

PRD §8 requires this, and the PRD's own risk list calls skipping it "the
difference between a toy and something a real team will trust with actual
internal tools."

Two layers:

* A **built-in scanner** that always runs. Python is analysed with `ast`,
  JavaScript with patterns. It has no external dependencies, which matters more
  than it sounds: a security gate assembled purely from optional tools does
  nothing at all on a machine where none are installed, while still reporting
  success. This layer covers the patterns the PRD names by hand — subprocess
  and eval usage, filesystem escapes, raw network calls.

* **External tools** — Bandit, Semgrep, osv-scanner — used when present and
  skipped with a recorded reason when not.

Nothing here executes, imports, or evaluates the code being scanned. Parsing a
hostile file must not be a way to run it.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .detect import IGNORED_DIRS

log = logging.getLogger("hangar.scan")

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}
TOOL_TIMEOUT = 120

# Python callables that hand control to arbitrary code or the shell.
CODE_EXECUTION = {
    "eval": ("high", "evaluates arbitrary code at runtime"),
    "exec": ("high", "executes arbitrary code at runtime"),
    "compile": ("medium", "compiles code at runtime"),
    "__import__": ("medium", "imports a module chosen at runtime"),
}
SHELL_EXECUTION = {
    "os.system": ("high", "runs a shell command"),
    "os.popen": ("high", "runs a shell command"),
    "os.execv": ("high", "replaces the process with another program"),
    "subprocess.run": ("medium", "runs a subprocess"),
    "subprocess.call": ("medium", "runs a subprocess"),
    "subprocess.Popen": ("medium", "runs a subprocess"),
    "subprocess.check_output": ("medium", "runs a subprocess"),
    "subprocess.check_call": ("medium", "runs a subprocess"),
}
UNSAFE_DESERIALISATION = {
    "pickle.loads": ("high", "unpickling untrusted data can execute code"),
    "pickle.load": ("high", "unpickling untrusted data can execute code"),
    "marshal.loads": ("high", "unmarshalling untrusted data can execute code"),
    "yaml.load": ("high", "yaml.load without SafeLoader can construct objects"),
}
FILESYSTEM_WRITES = {
    "shutil.rmtree": ("medium", "recursively deletes a directory tree"),
    "os.remove": ("low", "deletes a file"),
    "os.unlink": ("low", "deletes a file"),
    "os.rmdir": ("low", "removes a directory"),
    "os.chmod": ("low", "changes file permissions"),
}
NETWORK_MODULES = {"socket", "requests", "urllib", "urllib3", "httpx", "http"}

# Paths outside an app's own directory. Reading /etc/passwd or writing to /proc
# is not something a small internal tool needs to do.
SENSITIVE_PATHS = ("/etc/", "/proc/", "/sys/", "/root/", "/var/run/docker.sock")

JS_PATTERNS = [
    (r"\beval\s*\(", "high", "js.eval", "evaluates arbitrary code at runtime"),
    (r"new\s+Function\s*\(", "high", "js.new-function", "builds a function from a string"),
    (
        r"""require\s*\(\s*['"]child_process['"]\s*\)|from\s+['"]child_process['"]""",
        "high",
        "js.child-process",
        "can run shell commands",
    ),
    (r"\bexecSync\s*\(|\bexec\s*\(", "medium", "js.exec", "runs a subprocess"),
    (r"\bprocess\.env\b", "low", "js.env-access", "reads environment variables"),
    (
        r"""require\s*\(\s*['"]net['"]\s*\)|require\s*\(\s*['"]dgram['"]\s*\)""",
        "medium",
        "js.raw-socket",
        "opens raw network sockets",
    ),
    (r"\.\./\.\./", "medium", "js.path-traversal", "path traversal outside the app"),
]


@dataclass(frozen=True)
class Finding:
    tool: str
    rule: str
    severity: str
    message: str
    file: str
    line: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    tools_run: list[str] = field(default_factory=list)
    tools_skipped: dict[str, str] = field(default_factory=dict)

    @property
    def highest_severity(self) -> str | None:
        if not self.findings:
            return None
        return max(self.findings, key=lambda f: SEVERITY_ORDER[f.severity]).severity

    def counts(self) -> dict[str, int]:
        counts = {"low": 0, "medium": 0, "high": 0}
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts

    def at_or_above(self, severity: str) -> list[Finding]:
        threshold = SEVERITY_ORDER[severity]
        return [f for f in self.findings if SEVERITY_ORDER[f.severity] >= threshold]

    def as_dict(self) -> dict:
        return {
            "findings": [f.as_dict() for f in self.findings],
            "tools_run": self.tools_run,
            "tools_skipped": self.tools_skipped,
            "counts": self.counts(),
            "highest_severity": self.highest_severity,
        }

    def summary(self) -> str:
        counts = self.counts()
        tools = ", ".join(self.tools_run) or "none"
        return (
            f"{len(self.findings)} findings "
            f"({counts['high']} high, {counts['medium']} medium, {counts['low']} low) "
            f"from: {tools}"
        )


def scan(source_dir: str | Path) -> ScanResult:
    """Analyse everything in ``source_dir`` without executing any of it."""
    root = Path(source_dir)
    result = ScanResult()

    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and not (IGNORED_DIRS & set(path.relative_to(root).parts))
    ]

    result.findings.extend(_builtin_scan(root, files))
    result.tools_run.append("hangar-builtin")

    for name, runner in (
        ("bandit", _run_bandit),
        ("semgrep", _run_semgrep),
        ("osv-scanner", _run_osv),
    ):
        if shutil.which(name) is None:
            result.tools_skipped[name] = "not installed"
            continue
        try:
            result.findings.extend(runner(root))
            result.tools_run.append(name)
        except Exception as exc:  # noqa: BLE001 - a broken tool must not block deploys
            log.warning("%s failed: %s", name, exc)
            result.tools_skipped[name] = f"failed: {exc}"

    result.findings.sort(
        key=lambda f: (-SEVERITY_ORDER[f.severity], f.file, f.line)
    )
    return result


# --------------------------------------------------------------------------
# Built-in scanner
# --------------------------------------------------------------------------


def _builtin_scan(root: Path, files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        if path.suffix == ".py":
            findings.extend(_scan_python(path, rel))
        elif path.suffix in (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx"):
            findings.extend(_scan_javascript(path, rel))
    return findings


def _scan_python(path: Path, rel: str) -> list[Finding]:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        # Unparseable source is itself worth surfacing — it will fail at build
        # time, and it means this file went unscanned.
        return [
            Finding(
                tool="hangar-builtin",
                rule="python.syntax-error",
                severity="low",
                message=f"could not parse, so it was not scanned: {exc.msg}",
                file=rel,
                line=exc.lineno or 0,
            )
        ]

    return _PythonVisitor(rel).run(tree)


class _PythonVisitor(ast.NodeVisitor):
    """Walks the AST looking for the patterns PRD §8 names."""

    def __init__(self, rel: str):
        self.rel = rel
        self.findings: list[Finding] = []

    def run(self, tree: ast.AST) -> list[Finding]:
        self.visit(tree)
        return self.findings

    # -- helpers --------------------------------------------------------

    def _add(self, node: ast.AST, rule: str, severity: str, message: str) -> None:
        self.findings.append(
            Finding(
                tool="hangar-builtin",
                rule=rule,
                severity=severity,
                message=message,
                file=self.rel,
                line=getattr(node, "lineno", 0),
            )
        )

    @staticmethod
    def _dotted(node: ast.AST) -> str:
        """'subprocess.run' from an attribute chain; '' when it isn't one."""
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return ""

    # -- visitors -------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else self._dotted(node.func)
        )

        if name in CODE_EXECUTION:
            severity, message = CODE_EXECUTION[name]
            self._add(node, f"python.{name}", severity, message)

        elif name in SHELL_EXECUTION:
            severity, message = SHELL_EXECUTION[name]
            # shell=True turns any interpolated string into a shell injection.
            if any(
                kw.arg == "shell" and getattr(kw.value, "value", False) is True
                for kw in node.keywords
            ):
                severity, message = "high", f"{message} through a shell (shell=True)"
            self._add(node, f"python.{name.replace('.', '-')}", severity, message)

        elif name in UNSAFE_DESERIALISATION:
            severity, message = UNSAFE_DESERIALISATION[name]
            if name == "yaml.load" and self._has_safe_loader(node):
                return
            self._add(node, f"python.{name.replace('.', '-')}", severity, message)

        elif name in FILESYSTEM_WRITES:
            severity, message = FILESYSTEM_WRITES[name]
            self._add(node, f"python.{name.replace('.', '-')}", severity, message)

        elif name in ("open", "io.open"):
            self._check_path_argument(node)

        elif name == "socket.socket":
            self._add(
                node, "python.raw-socket", "medium", "opens a raw network socket"
            )

        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import(node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self._check_import(node, node.module)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            for sensitive in SENSITIVE_PATHS:
                if node.value.startswith(sensitive):
                    self._add(
                        node,
                        "python.sensitive-path",
                        "high" if "docker.sock" in sensitive else "medium",
                        f"references a path outside the app: {node.value!r}",
                    )
                    break
        self.generic_visit(node)

    # -- checks ---------------------------------------------------------

    def _check_import(self, node: ast.AST, module: str) -> None:
        top = module.split(".")[0]
        if top in NETWORK_MODULES:
            self._add(
                node,
                "python.network-import",
                "low",
                f"imports {module}, which can make outbound network calls",
            )
        elif top in ("ctypes", "mmap"):
            self._add(
                node,
                "python.low-level-memory",
                "medium",
                f"imports {module}, which can manipulate process memory directly",
            )

    def _check_path_argument(self, node: ast.Call) -> None:
        if not node.args:
            return
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if ".." in first.value:
                self._add(
                    node,
                    "python.path-traversal",
                    "medium",
                    f"opens a path that escapes its directory: {first.value!r}",
                )

    @staticmethod
    def _has_safe_loader(node: ast.Call) -> bool:
        for kw in node.keywords:
            if kw.arg == "Loader":
                return "Safe" in ast.dump(kw.value)
        return any("Safe" in ast.dump(arg) for arg in node.args[1:])


def _scan_javascript(path: Path, rel: str) -> list[Finding]:
    """Pattern-based, since there's no JS parser in the standard library.

    Comment-only matches are ignored to keep the noise down; this is a
    heuristic layer, and Semgrep covers JavaScript properly when installed.
    """
    findings: list[Finding] = []
    text = path.read_text(encoding="utf-8", errors="replace")

    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        for pattern, severity, rule, message in JS_PATTERNS:
            if re.search(pattern, line):
                findings.append(
                    Finding(
                        tool="hangar-builtin",
                        rule=rule,
                        severity=severity,
                        message=message,
                        file=rel,
                        line=number,
                    )
                )
    return findings


# --------------------------------------------------------------------------
# External tools
# --------------------------------------------------------------------------


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=TOOL_TIMEOUT,
        check=False,
    )


def _run_bandit(root: Path) -> list[Finding]:
    proc = _run(["bandit", "-r", ".", "-f", "json", "-q"], root)
    if not proc.stdout.strip():
        return []
    data = json.loads(proc.stdout)
    severities = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}
    return [
        Finding(
            tool="bandit",
            rule=item.get("test_id", "bandit"),
            severity=severities.get(item.get("issue_severity", "LOW"), "low"),
            message=item.get("issue_text", ""),
            file=item.get("filename", "").lstrip("./"),
            line=item.get("line_number", 0),
        )
        for item in data.get("results", [])
    ]


def _run_semgrep(root: Path) -> list[Finding]:
    proc = _run(
        ["semgrep", "--config", "auto", "--json", "--quiet", "--no-git-ignore", "."],
        root,
    )
    if not proc.stdout.strip():
        return []
    data = json.loads(proc.stdout)
    severities = {"INFO": "low", "WARNING": "medium", "ERROR": "high"}
    return [
        Finding(
            tool="semgrep",
            rule=item.get("check_id", "semgrep"),
            severity=severities.get(
                item.get("extra", {}).get("severity", "INFO"), "low"
            ),
            message=item.get("extra", {}).get("message", "").strip(),
            file=item.get("path", ""),
            line=item.get("start", {}).get("line", 0),
        )
        for item in data.get("results", [])
    ]


def _run_osv(root: Path) -> list[Finding]:
    """Known vulnerabilities in declared dependencies, read from lockfiles."""
    proc = _run(["osv-scanner", "--format", "json", "-r", "."], root)
    if not proc.stdout.strip():
        return []
    data = json.loads(proc.stdout)

    findings = []
    for res in data.get("results", []):
        for package in res.get("packages", []):
            name = package.get("package", {}).get("name", "?")
            version = package.get("package", {}).get("version", "?")
            for vuln in package.get("vulnerabilities", []):
                findings.append(
                    Finding(
                        tool="osv-scanner",
                        rule=vuln.get("id", "OSV"),
                        severity="high",
                        message=(
                            f"{name} {version}: "
                            f"{vuln.get('summary', 'known vulnerability')}"
                        ),
                        file=res.get("source", {}).get("path", ""),
                        line=0,
                    )
                )
    return findings
