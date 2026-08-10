"""Backend selection and the deploy path's use of the interface."""

import pytest

from hangar import backends, store
from hangar import deploy as deploy_mod
from hangar.backends import BackendError
from hangar.backends.base import ExecutionBackend, ResourceLimits
from hangar.backends.docker_backend import DockerBackend


def test_docker_is_the_default_backend(db):
    assert isinstance(backends.get_backend(), DockerBackend)


def test_backend_is_selected_by_env(monkeypatch, fake_backend):
    assert backends.get_backend().name == "fake"


def test_unknown_backend_names_the_known_ones(monkeypatch):
    monkeypatch.setenv("HANGAR_BACKEND", "wishful")
    with pytest.raises(BackendError, match="unknown execution backend"):
        backends.get_backend()


def test_docker_backend_implements_the_full_interface():
    """A partial implementation should fail at construction, not mid-deploy."""
    missing = [
        name
        for name in ExecutionBackend.__abstractmethods__
        if getattr(DockerBackend, name) is getattr(ExecutionBackend, name)
    ]
    assert missing == []


def test_resource_limits_come_from_settings(monkeypatch):
    monkeypatch.setenv("HANGAR_APP_MEMORY_MB", "128")
    monkeypatch.setenv("HANGAR_APP_CPUS", "0.1")
    monkeypatch.setenv("HANGAR_APP_PIDS", "32")

    from hangar import config

    limits = ResourceLimits.from_settings(config.settings())
    assert (limits.memory_mb, limits.cpus, limits.pids) == (128, 0.1, 32)


# --------------------------------------------------------------------------
# deploy() goes through the backend
# --------------------------------------------------------------------------


@pytest.fixture
def app_row(db, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (source / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    with store.session() as sess:
        app = store.App(name="deployed-app", source_type="path", source_ref=str(source))
        store.save(sess, app)
        return app.id


def test_deploy_uses_the_configured_backend(app_row, fake_backend):
    deploy_mod.deploy(app_row)

    assert "build" in fake_backend.methods()
    assert "run" in fake_backend.methods()

    with store.session() as sess:
        app = store.get_app(sess, app_row)
        assert app.status == "running"
        assert app.url == "http://localhost:12345"
        assert app.runtime == "python"


def test_deploy_fails_clearly_when_the_backend_is_unavailable(app_row, fake_backend):
    """A stopped Docker daemon should read as a message, not a stack trace."""
    fake_backend.is_available = False
    deploy_mod.deploy(app_row)

    with store.session() as sess:
        app = store.get_app(sess, app_row)
        assert app.status == "failed"
        assert "unavailable" in app.error

    assert "build" not in fake_backend.methods()


def test_deploy_records_backend_failures_against_the_app(app_row, fake_backend):
    fake_backend.errors["run"] = "no capacity"
    deploy_mod.deploy(app_row)

    with store.session() as sess:
        app = store.get_app(sess, app_row)
        assert app.status == "failed"
        assert "no capacity" in app.error
        assert store.latest_deployment(sess, app_row).status == "failed"
