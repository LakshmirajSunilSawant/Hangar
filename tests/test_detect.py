"""Detection tests.

These build throwaway source trees rather than using the examples/ apps, so a
change to a sample app can't silently invalidate the detection contract.
"""

from pathlib import Path

import pytest

from hangar.detect import Detection, DetectionError, detect

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def write(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


def test_detects_fastapi(tmp_path):
    write(tmp_path, {
        "requirements.txt": "fastapi\nuvicorn[standard]>=0.32\n",
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    })
    d = detect(tmp_path)
    assert d.runtime == "python"
    assert d.framework == "fastapi"
    assert d.entrypoint == "main.py"
    assert d.port == 8000
    assert d.start_command[:2] == ["uvicorn", "main:app"]


def test_detects_fastapi_with_nonstandard_variable_and_nested_entry(tmp_path):
    write(tmp_path, {
        "requirements.txt": "fastapi\n",
        "src/api.py": "from fastapi import FastAPI\nservice = FastAPI()\n",
    })
    d = detect(tmp_path)
    assert d.entrypoint == "src/api.py"
    # uvicorn needs a dotted module path, not a filesystem path.
    assert d.start_command[1] == "src.api:service"


def test_detects_flask(tmp_path):
    write(tmp_path, {
        "requirements.txt": "flask\n",
        "app.py": "from flask import Flask\napp = Flask(__name__)\n",
    })
    d = detect(tmp_path)
    assert d.framework == "flask"
    assert d.port == 5000
    assert d.start_command[0] == "gunicorn"


def test_detects_streamlit(tmp_path):
    write(tmp_path, {
        "requirements.txt": "streamlit\npandas\n",
        "streamlit_app.py": "import streamlit as st\nst.write('hi')\n",
    })
    d = detect(tmp_path)
    assert d.framework == "streamlit"
    assert d.port == 8501
    assert d.start_command[:3] == ["streamlit", "run", "streamlit_app.py"]


def test_streamlit_wins_over_fastapi_when_both_present(tmp_path):
    """A Streamlit app has no ASGI object; misreading it as FastAPI breaks the run."""
    write(tmp_path, {
        "requirements.txt": "streamlit\nfastapi\n",
        "app.py": "import streamlit as st\n",
    })
    assert detect(tmp_path).framework == "streamlit"


def test_detects_framework_from_source_when_dependency_file_absent(tmp_path):
    write(tmp_path, {"main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    d = detect(tmp_path)
    assert d.framework == "fastapi"
    assert d.dependency_file is None


def test_reads_dependencies_from_pyproject(tmp_path):
    write(tmp_path, {
        "pyproject.toml": (
            '[project]\nname = "x"\nversion = "1"\n'
            'dependencies = ["fastapi>=0.115", "uvicorn[standard]"]\n'
        ),
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    })
    d = detect(tmp_path)
    assert d.dependency_file == "pyproject.toml"
    assert d.framework == "fastapi"


def test_prefers_conventional_entrypoint_name(tmp_path):
    write(tmp_path, {
        "requirements.txt": "fastapi\n",
        "zzz_helper.py": "from fastapi import FastAPI\nother = FastAPI()\n",
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    })
    assert detect(tmp_path).entrypoint == "main.py"


def test_ignores_vendored_directories(tmp_path):
    """A dependency vendored into .venv must not be mistaken for the app."""
    write(tmp_path, {
        "requirements.txt": "fastapi\n",
        ".venv/lib/site-packages/thing/main.py": "app = FastAPI()\n",
        "server.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    })
    assert detect(tmp_path).entrypoint == "server.py"


# --------------------------------------------------------------------------
# Node
# --------------------------------------------------------------------------


def test_detects_express(tmp_path):
    write(tmp_path, {
        "package.json": (
            '{"name":"x","main":"index.js",'
            '"scripts":{"start":"node index.js"},'
            '"dependencies":{"express":"^4.21.2"}}'
        ),
        "index.js": "const express = require('express');\n",
    })
    d = detect(tmp_path)
    assert d.runtime == "node"
    assert d.framework == "express"
    assert d.entrypoint == "index.js"
    assert d.port == 3000
    assert d.start_command == ["npm", "run", "start"]


def test_detects_next(tmp_path):
    write(tmp_path, {
        "package.json": (
            '{"name":"x","scripts":{"build":"next build","start":"next start"},'
            '"dependencies":{"next":"^15.0.0","react":"^19.0.0"}}'
        ),
    })
    d = detect(tmp_path)
    assert d.framework == "next"
    assert d.port == 3000


def test_node_without_start_script_runs_entrypoint_directly(tmp_path):
    write(tmp_path, {
        "package.json": '{"name":"x","dependencies":{"express":"^4.21.2"}}',
        "server.js": "require('express')();\n",
    })
    assert detect(tmp_path).start_command == ["node", "server.js"]


def test_node_wins_when_a_python_helper_script_is_present(tmp_path):
    """A Node app shipping a build script shouldn't be deployed as Python."""
    write(tmp_path, {
        "package.json": (
            '{"name":"x","main":"index.js","dependencies":{"express":"^4.21.2"}}'
        ),
        "index.js": "require('express')();\n",
        "scripts/generate.py": "print('codegen')\n",
    })
    assert detect(tmp_path).runtime == "node"


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------


def test_rejects_empty_directory(tmp_path):
    with pytest.raises(DetectionError, match="empty"):
        detect(tmp_path)


def test_rejects_unknown_stack(tmp_path):
    write(tmp_path, {"main.go": "package main\n", "go.mod": "module x\n"})
    with pytest.raises(DetectionError, match="Python or Node"):
        detect(tmp_path)


def test_rejects_unsupported_python_framework(tmp_path):
    write(tmp_path, {
        "requirements.txt": "django\n",
        "manage.py": "import django\n",
    })
    with pytest.raises(DetectionError, match="no supported framework"):
        detect(tmp_path)


def test_rejects_missing_directory(tmp_path):
    with pytest.raises(DetectionError, match="not a directory"):
        detect(tmp_path / "nope")


def test_survives_undecodable_source_file(tmp_path):
    """Untrusted source can be any encoding — detection must not crash on it."""
    write(tmp_path, {"requirements.txt": "fastapi\n"})
    (tmp_path / "main.py").write_bytes(b"\xff\xfe from fastapi import FastAPI\n")
    (tmp_path / "server.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    assert detect(tmp_path).framework == "fastapi"


# --------------------------------------------------------------------------
# The shipped sample apps must stay deployable
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,runtime,framework",
    [("fastapi-hello", "python", "fastapi"), ("express-hello", "node", "express")],
)
def test_example_apps_are_detected(name, runtime, framework):
    d = detect(EXAMPLES / name)
    assert isinstance(d, Detection)
    assert (d.runtime, d.framework) == (runtime, framework)
