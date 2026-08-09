"""Runtime detection.

Given a source directory, work out what kind of app it is and how to start it.
Deliberately static — nothing in the source tree is executed or imported here,
since at this point the code is still untrusted.

Scope per the PRD: Python (FastAPI / Flask / Streamlit) and Node (Express /
Next.js). Anything else is an explicit, reported failure rather than a guess.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Ordered by how specific the signal is: a Streamlit app also depends on things
# that look generic, so the framework check has to run most-specific first.
PYTHON_DEP_FILES = ("requirements.txt", "pyproject.toml", "Pipfile")
NODE_DEP_FILES = ("package.json",)

# Directories never worth scanning for an entrypoint.
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    "node_modules", ".next", "dist", "build", ".pytest_cache", ".mypy_cache",
    ".idea", ".vscode", ".hangar",
}

DEFAULT_PORTS = {
    "fastapi": 8000,
    "flask": 5000,
    "streamlit": 8501,
    "express": 3000,
    "next": 3000,
}


class DetectionError(Exception):
    """Raised when the source tree isn't something Hangar can deploy."""


@dataclass
class Detection:
    """Everything the builder needs to produce a Dockerfile."""

    runtime: str  # "python" | "node"
    framework: str  # "fastapi" | "flask" | "streamlit" | "express" | "next" | "node"
    entrypoint: str  # path relative to source root
    port: int
    start_command: list[str]
    dependency_file: str | None = None
    # Normalised names the app declares. Lets the builder skip installing a
    # framework package the app already pins itself.
    declared_dependencies: set[str] = field(default_factory=set)
    # Human-readable trail of what led to this conclusion — surfaced in the
    # build log so a failed detection is debuggable without a re-run.
    evidence: list[str] = field(default_factory=list)

    @property
    def base_image(self) -> str:
        return "python:3.12-slim" if self.runtime == "python" else "node:22-slim"


def detect(source_dir: str | Path) -> Detection:
    """Detect the runtime and framework of the app in ``source_dir``."""
    root = Path(source_dir)
    if not root.is_dir():
        raise DetectionError(f"not a directory: {root}")

    files = _walk(root)
    if not files:
        raise DetectionError("source directory is empty")

    has_python = any(f.name in PYTHON_DEP_FILES for f in files) or any(
        f.suffix == ".py" for f in files
    )
    has_node = any(f.name in NODE_DEP_FILES for f in files)

    # A Node app that ships a helper script shouldn't be misread as Python, so
    # package.json wins whenever it declares a real dependency set.
    if has_node and _package_json(root) is not None:
        return _detect_node(root, files)
    if has_python:
        return _detect_python(root, files)
    if has_node:
        return _detect_node(root, files)

    raise DetectionError(
        "could not identify a Python or Node app — expected one of "
        f"{', '.join(PYTHON_DEP_FILES + NODE_DEP_FILES)} or a .py entrypoint"
    )


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


def _detect_python(root: Path, files: list[Path]) -> Detection:
    dep_file, declared = _python_dependencies(root)
    evidence = []
    if dep_file:
        evidence.append(f"found {dep_file} declaring {len(declared)} dependencies")

    py_files = [f for f in files if f.suffix == ".py"]
    if not py_files:
        raise DetectionError("Python dependencies found but no .py files to run")

    sources = {f: _read(f) for f in py_files}

    # Streamlit first: it's the most specific signal and its apps have no
    # importable ASGI/WSGI object, so misreading it as FastAPI breaks the run.
    if "streamlit" in declared or any("import streamlit" in s for s in sources.values()):
        entry = _pick_python_entry(root, sources, r"import\s+streamlit|from\s+streamlit")
        evidence.append(f"streamlit import in {entry}")
        return Detection(
            runtime="python",
            framework="streamlit",
            entrypoint=entry,
            port=DEFAULT_PORTS["streamlit"],
            start_command=[
                "streamlit", "run", entry,
                "--server.port", str(DEFAULT_PORTS["streamlit"]),
                "--server.address", "0.0.0.0",
                "--server.headless", "true",
            ],
            dependency_file=dep_file,
            declared_dependencies=declared,
            evidence=evidence,
        )

    if "fastapi" in declared or any("FastAPI(" in s for s in sources.values()):
        entry, var = _find_asgi_app(root, sources, r"(\w+)\s*=\s*FastAPI\s*\(")
        module = _module_path(entry)
        evidence.append(f"FastAPI instance '{var}' in {entry}")
        return Detection(
            runtime="python",
            framework="fastapi",
            entrypoint=entry,
            port=DEFAULT_PORTS["fastapi"],
            start_command=[
                "uvicorn", f"{module}:{var}",
                "--host", "0.0.0.0",
                "--port", str(DEFAULT_PORTS["fastapi"]),
            ],
            dependency_file=dep_file,
            declared_dependencies=declared,
            evidence=evidence,
        )

    if "flask" in declared or any("Flask(" in s for s in sources.values()):
        entry, var = _find_asgi_app(root, sources, r"(\w+)\s*=\s*Flask\s*\(")
        module = _module_path(entry)
        evidence.append(f"Flask instance '{var}' in {entry}")
        return Detection(
            runtime="python",
            framework="flask",
            entrypoint=entry,
            port=DEFAULT_PORTS["flask"],
            # Flask's dev server is single-threaded and warns loudly in prod;
            # gunicorn is added to the image so this is always available.
            start_command=[
                "gunicorn", f"{module}:{var}",
                "--bind", f"0.0.0.0:{DEFAULT_PORTS['flask']}",
                "--workers", "2",
            ],
            dependency_file=dep_file,
            declared_dependencies=declared,
            evidence=evidence,
        )

    raise DetectionError(
        "Python app found, but no supported framework (FastAPI, Flask, or "
        "Streamlit). Declared dependencies: "
        + (", ".join(sorted(declared)) or "none")
    )


def _python_dependencies(root: Path) -> tuple[str | None, set[str]]:
    """Return the dependency filename and the set of normalised package names."""
    req = root / "requirements.txt"
    if req.is_file():
        return "requirements.txt", _parse_requirements(_read(req))

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(_read(pyproject))
        except tomllib.TOMLDecodeError:
            return "pyproject.toml", set()
        deps = data.get("project", {}).get("dependencies", []) or []
        poetry = (
            data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
        )
        names = {requirement_name(d) for d in deps if isinstance(d, str)}
        names |= {k.lower() for k in poetry if k.lower() != "python"}
        return "pyproject.toml", {n for n in names if n}

    return None, set()


def _parse_requirements(text: str) -> set[str]:
    names = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        # Skip pip flags (-r other.txt, --index-url, ...) and blank lines.
        if not line or line.startswith("-"):
            continue
        name = requirement_name(line)
        if name:
            names.add(name)
    return names


def requirement_name(spec: str) -> str:
    """'uvicorn[standard]>=0.32' -> 'uvicorn'."""
    match = re.match(r"^\s*([A-Za-z0-9._-]+)", spec)
    return match.group(1).lower().replace("_", "-") if match else ""


def _find_asgi_app(
    root: Path, sources: dict[Path, str], pattern: str
) -> tuple[str, str]:
    """Find the file and variable name holding the app instance."""
    for path in _entry_order(root, sources):
        match = re.search(pattern, sources[path])
        if match:
            return _rel(root, path), match.group(1)
    raise DetectionError(
        "framework detected from dependencies but no app instance found in source"
    )


def _pick_python_entry(root: Path, sources: dict[Path, str], pattern: str) -> str:
    for path in _entry_order(root, sources):
        if re.search(pattern, sources[path]):
            return _rel(root, path)
    return _rel(root, _entry_order(root, sources)[0])


def _entry_order(root: Path, sources: dict[Path, str]) -> list[Path]:
    """Rank candidate entrypoints by how conventional the name and depth are."""
    preferred = ["main.py", "app.py", "server.py", "api.py", "streamlit_app.py"]

    def rank(path: Path) -> tuple[int, int, str]:
        rel = _rel(root, path)
        name_rank = preferred.index(path.name) if path.name in preferred else len(preferred)
        return (len(Path(rel).parts), name_rank, rel)

    return sorted(sources, key=rank)


def _module_path(entrypoint: str) -> str:
    """'src/main.py' -> 'src.main' — the import path uvicorn/gunicorn needs."""
    return Path(entrypoint).with_suffix("").as_posix().replace("/", ".")


# --------------------------------------------------------------------------
# Node
# --------------------------------------------------------------------------


def _detect_node(root: Path, files: list[Path]) -> Detection:
    pkg = _package_json(root)
    if pkg is None:
        raise DetectionError("Node app detected but package.json is missing or invalid")

    deps = {
        k.lower()
        for k in {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    }
    scripts = pkg.get("scripts", {}) or {}
    evidence = [f"package.json declaring {len(deps)} dependencies"]

    if "next" in deps:
        evidence.append("'next' in dependencies")
        # Next needs a build step; the builder handles that, we just declare start.
        return Detection(
            runtime="node",
            framework="next",
            entrypoint="package.json",
            port=DEFAULT_PORTS["next"],
            start_command=["npm", "run", "start"],
            dependency_file="package.json",
            declared_dependencies=deps,
            evidence=evidence,
        )

    entry = _node_entry(root, pkg, files)
    evidence.append(f"entrypoint {entry}")
    if "express" in deps:
        evidence.append("'express' in dependencies")
    # Plain Node servers start the same way as Express ones, so they share a path.
    framework = "express" if "express" in deps else "node"

    start = (
        ["npm", "run", "start"]
        if "start" in scripts
        else ["node", entry]
    )
    if "start" in scripts:
        evidence.append("using npm start script")

    return Detection(
        runtime="node",
        framework=framework,
        entrypoint=entry,
        port=DEFAULT_PORTS["express"],
        start_command=start,
        dependency_file="package.json",
        declared_dependencies=deps,
        evidence=evidence,
    )


def _package_json(root: Path) -> dict | None:
    path = root / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(_read(path))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _node_entry(root: Path, pkg: dict, files: list[Path]) -> str:
    main = pkg.get("main")
    if isinstance(main, str) and (root / main).is_file():
        return Path(main).as_posix()

    for candidate in ("index.js", "server.js", "app.js", "src/index.js", "src/server.js"):
        if (root / candidate).is_file():
            return candidate

    js = [f for f in files if f.suffix in (".js", ".mjs", ".cjs")]
    if not js:
        raise DetectionError("package.json found but no JavaScript entrypoint")
    return _rel(root, min(js, key=lambda f: (len(f.relative_to(root).parts), f.name)))


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _walk(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if IGNORED_DIRS & set(path.relative_to(root).parts):
            continue
        out.append(path)
    return out


def _read(path: Path) -> str:
    # Untrusted source may be any encoding; never let a decode error abort detection.
    return path.read_text(encoding="utf-8", errors="replace")


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
