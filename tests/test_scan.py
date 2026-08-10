"""Static security scanner tests.

The scanner runs on untrusted code, so the load-bearing property is that
analysing hostile source never executes it. That is asserted explicitly below,
not just assumed from using `ast`.
"""

import pytest

from hangar import scan
from hangar.scan import Finding, ScanResult


def write(root, files: dict[str, str]):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def rules(result: ScanResult) -> set[str]:
    return {f.rule for f in result.findings}


# --------------------------------------------------------------------------
# The scanner must not run what it scans
# --------------------------------------------------------------------------


def test_scanning_does_not_execute_the_code(tmp_path):
    """The whole point of static analysis. A regression here is a sandbox escape."""
    marker = tmp_path / "PWNED"
    write(tmp_path, {
        "evil.py": f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n",
    })

    scan.scan(tmp_path)
    assert not marker.exists(), "scanning executed the module"


def test_import_time_side_effects_are_not_triggered(tmp_path):
    marker = tmp_path / "IMPORTED"
    write(tmp_path, {
        "sitecustomize.py": f"open({str(marker)!r}, 'w').close()\n",
        "main.py": "import sitecustomize\n",
    })

    scan.scan(tmp_path)
    assert not marker.exists()


# --------------------------------------------------------------------------
# The patterns PRD §8 names by hand
# --------------------------------------------------------------------------


def test_flags_eval_and_exec(tmp_path):
    write(tmp_path, {"a.py": "eval(input())\nexec('x=1')\n"})
    result = scan.scan(tmp_path)

    assert "python.eval" in rules(result)
    assert "python.exec" in rules(result)
    assert result.highest_severity == "high"


def test_flags_shell_execution(tmp_path):
    write(tmp_path, {"a.py": "import os\nos.system('rm -rf /')\n"})
    assert "python.os-system" in rules(scan.scan(tmp_path))


def test_subprocess_with_shell_true_is_worse_than_without(tmp_path):
    write(tmp_path, {"safe.py": "import subprocess\nsubprocess.run(['ls'])\n"})
    without = scan.scan(tmp_path).findings[0]

    write(tmp_path, {"safe.py": "import subprocess\nsubprocess.run('ls', shell=True)\n"})
    with_shell = scan.scan(tmp_path).findings[0]

    assert without.severity == "medium"
    assert with_shell.severity == "high"
    assert "shell" in with_shell.message


def test_flags_filesystem_escapes(tmp_path):
    write(tmp_path, {"a.py": "open('../../etc/passwd')\n"})
    assert "python.path-traversal" in rules(scan.scan(tmp_path))


def test_flags_sensitive_paths(tmp_path):
    write(tmp_path, {"a.py": "PATH = '/etc/shadow'\n"})
    assert "python.sensitive-path" in rules(scan.scan(tmp_path))


def test_docker_socket_access_is_high_severity(tmp_path):
    """Reaching the Docker socket from inside a sandbox is a host takeover."""
    write(tmp_path, {"a.py": "SOCK = '/var/run/docker.sock'\n"})
    finding = next(
        f for f in scan.scan(tmp_path).findings if f.rule == "python.sensitive-path"
    )
    assert finding.severity == "high"


def test_flags_raw_network_calls(tmp_path):
    write(tmp_path, {"a.py": "import socket\ns = socket.socket()\n"})
    found = rules(scan.scan(tmp_path))
    assert "python.raw-socket" in found
    assert "python.network-import" in found


def test_flags_unsafe_deserialisation(tmp_path):
    write(tmp_path, {"a.py": "import pickle\npickle.loads(data)\n"})
    assert "python.pickle-loads" in rules(scan.scan(tmp_path))


def test_yaml_safe_loader_is_not_flagged(tmp_path):
    """Flagging the safe form too would train people to ignore the scanner."""
    write(tmp_path, {
        "unsafe.py": "import yaml\nyaml.load(text)\n",
        "safe.py": "import yaml\nyaml.load(text, Loader=yaml.SafeLoader)\n",
    })
    findings = [f for f in scan.scan(tmp_path).findings if f.rule == "python.yaml-load"]

    assert len(findings) == 1
    assert findings[0].file == "unsafe.py"


def test_reports_file_and_line(tmp_path):
    write(tmp_path, {"pkg/mod.py": "x = 1\ny = 2\neval('z')\n"})
    finding = next(f for f in scan.scan(tmp_path).findings if f.rule == "python.eval")

    assert finding.file == "pkg/mod.py"
    assert finding.line == 3


# --------------------------------------------------------------------------
# JavaScript
# --------------------------------------------------------------------------


def test_flags_javascript_eval_and_child_process(tmp_path):
    write(tmp_path, {
        "index.js": "const cp = require('child_process');\neval(userInput);\n",
    })
    found = rules(scan.scan(tmp_path))
    assert "js.eval" in found
    assert "js.child-process" in found


def test_ignores_commented_out_javascript(tmp_path):
    write(tmp_path, {"index.js": "// eval(x) is dangerous, don't\nconst a = 1;\n"})
    assert "js.eval" not in rules(scan.scan(tmp_path))


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


def test_unparseable_python_is_reported_rather_than_silently_skipped(tmp_path):
    """Silence would imply "scanned and clean" for a file that wasn't scanned."""
    write(tmp_path, {"broken.py": "def oops(:\n"})
    result = scan.scan(tmp_path)

    assert "python.syntax-error" in rules(result)
    assert "not scanned" in result.findings[0].message


def test_undecodable_files_do_not_crash_the_scan(tmp_path):
    write(tmp_path, {"ok.py": "eval('x')\n"})
    (tmp_path / "binary.py").write_bytes(b"\xff\xfe\x00garbage")

    assert "python.eval" in rules(scan.scan(tmp_path))


def test_vendored_directories_are_not_scanned(tmp_path):
    """node_modules would produce thousands of findings about other people's code."""
    write(tmp_path, {
        "app.py": "x = 1\n",
        "node_modules/pkg/index.js": "eval(x)\n",
        ".venv/lib/thing.py": "eval('x')\n",
    })
    assert scan.scan(tmp_path).findings == []


def test_clean_app_produces_no_findings(tmp_path):
    write(tmp_path, {
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n\n"
                   "@app.get('/')\ndef index():\n    return {'ok': True}\n",
    })
    result = scan.scan(tmp_path)

    assert result.findings == []
    assert result.highest_severity is None


def test_builtin_scanner_always_runs(tmp_path):
    """A gate made only of optional tools does nothing when none are installed."""
    write(tmp_path, {"a.py": "x = 1\n"})
    assert "hangar-builtin" in scan.scan(tmp_path).tools_run


def test_missing_external_tools_are_recorded_not_hidden(tmp_path):
    write(tmp_path, {"a.py": "x = 1\n"})
    result = scan.scan(tmp_path)

    known = {"bandit", "semgrep", "osv-scanner"}
    accounted = set(result.tools_run) | set(result.tools_skipped)
    assert known <= accounted


# --------------------------------------------------------------------------
# Result aggregation
# --------------------------------------------------------------------------


def make(severity: str) -> Finding:
    return Finding("t", "r", severity, "m", "f.py", 1)


def test_highest_severity_and_counts():
    result = ScanResult(findings=[make("low"), make("high"), make("medium")])

    assert result.highest_severity == "high"
    assert result.counts() == {"low": 1, "medium": 1, "high": 1}


def test_at_or_above_threshold():
    result = ScanResult(findings=[make("low"), make("medium"), make("high")])

    assert len(result.at_or_above("low")) == 3
    assert len(result.at_or_above("medium")) == 2
    assert len(result.at_or_above("high")) == 1


def test_findings_are_sorted_worst_first(tmp_path):
    write(tmp_path, {"a.py": "import socket\neval('x')\n"})
    severities = [f.severity for f in scan.scan(tmp_path).findings]

    assert severities == sorted(severities, key=lambda s: -scan.SEVERITY_ORDER[s])


def test_result_serialises_for_storage():
    result = ScanResult(findings=[make("high")], tools_run=["hangar-builtin"])
    data = result.as_dict()

    assert data["counts"]["high"] == 1
    assert data["findings"][0]["severity"] == "high"
    assert data["highest_severity"] == "high"
