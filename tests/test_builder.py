"""Dockerfile generation tests.

Rendering is pure, so these run without a Docker daemon. The actual build and
run path is covered by tests/test_deploy.py, which is skipped when Docker isn't
available.
"""

from hangar.builder import render_dockerfile
from hangar.detect import Detection


def make(**overrides) -> Detection:
    base = dict(
        runtime="python",
        framework="fastapi",
        entrypoint="main.py",
        port=8000,
        start_command=["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
        dependency_file="requirements.txt",
        declared_dependencies={"fastapi", "uvicorn"},
    )
    base.update(overrides)
    return Detection(**base)


def test_python_dockerfile_installs_requirements_before_copying_source():
    df = render_dockerfile(make())
    copy_reqs = df.index("COPY requirements.txt")
    install = df.index("pip install --no-cache-dir -r requirements.txt")
    copy_all = df.index("COPY . .")
    # Ordering is what makes the dependency layer cacheable across code edits.
    assert copy_reqs < install < copy_all


def test_skips_runtime_packages_the_app_already_declares():
    df = render_dockerfile(make())
    assert 'pip install --no-cache-dir "fastapi"' not in df


def test_installs_runtime_packages_the_app_omits():
    """A Flask app rarely declares gunicorn, but the start command needs it."""
    df = render_dockerfile(make(
        framework="flask",
        port=5000,
        start_command=["gunicorn", "app:app", "--bind", "0.0.0.0:5000"],
        declared_dependencies={"flask"},
    ))
    assert '"gunicorn"' in df
    assert '"flask"' not in df


def test_runs_as_non_root():
    df = render_dockerfile(make())
    assert "USER hangar" in df
    assert df.index("useradd") < df.index("USER hangar")


def test_start_command_is_exec_form():
    """Shell form would make the app a child of sh and break signal handling."""
    df = render_dockerfile(make())
    assert 'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]' in df


def test_port_is_exposed_and_published_as_env():
    df = render_dockerfile(make(port=8501))
    assert "ENV PORT=8501" in df
    assert "EXPOSE 8501" in df


def test_pyproject_project_is_installed():
    df = render_dockerfile(make(dependency_file="pyproject.toml"))
    assert "pip install --no-cache-dir ." in df
    # The source has to be present before the project can be installed.
    assert df.index("COPY . .") < df.index("pip install --no-cache-dir .")


def test_node_dockerfile_prefers_lockfile_install():
    df = render_dockerfile(make(
        runtime="node",
        framework="express",
        entrypoint="index.js",
        port=3000,
        start_command=["npm", "run", "start"],
        dependency_file="package.json",
        declared_dependencies={"express"},
    ))
    assert "FROM node:22-slim" in df
    assert "npm ci --omit=dev" in df
    assert "npm install --omit=dev" in df  # fallback when no lockfile
    assert df.index("COPY package.json") < df.index("COPY . .")


def test_next_app_is_built_before_start():
    df = render_dockerfile(make(
        runtime="node",
        framework="next",
        entrypoint="package.json",
        port=3000,
        start_command=["npm", "run", "start"],
        dependency_file="package.json",
        declared_dependencies={"next", "react"},
    ))
    assert "RUN npm run build" in df
    assert df.index("RUN npm run build") < df.index("CMD [")
