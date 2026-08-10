"""API token auth tests.

The control plane can create and delete deployments and read their logs, so
these cover the shape that matters: no anonymous access once a token is set,
and no accidental way to expose an unauthenticated API on a public interface.
"""

import pytest

from hangar import api as api_mod
from hangar import cli
from hangar import deploy as deploy_mod

TOKEN = "s3cret-token"


@pytest.fixture(autouse=True)
def backend(fake_backend):
    return fake_backend


@pytest.fixture(autouse=True)
def no_real_deploys(monkeypatch):
    monkeypatch.setattr(deploy_mod, "deploy", lambda app_id: None)
    monkeypatch.setattr(api_mod.deploy_mod, "deploy", lambda app_id: None)


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("HANGAR_API_TOKEN", TOKEN)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Route protection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/apps"),
        ("post", "/apps"),
        ("get", "/apps/abc"),
        ("get", "/apps/abc/logs"),
        ("post", "/apps/abc/stop"),
        ("post", "/apps/abc/restart"),
        ("post", "/apps/abc/redeploy"),
        ("delete", "/apps/abc"),
    ],
)
def test_every_apps_route_requires_a_token(client, secured, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_wrong_token_is_rejected(client, secured):
    assert client.get("/apps", headers=auth("not-the-token")).status_code == 403


def test_correct_token_is_accepted(client, secured):
    assert client.get("/apps", headers=auth(TOKEN)).status_code == 200


def test_health_stays_open_so_probes_work_without_credentials(client, secured):
    """Uptime pingers and platform health checks don't carry the token."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["auth"] == "enabled"


def test_health_never_leaks_the_token(client, secured):
    assert TOKEN not in client.get("/healthz").text


def test_unauthenticated_when_no_token_is_configured(client):
    """Local development shouldn't need setup."""
    assert client.get("/apps").status_code == 200


def test_missing_token_reports_how_to_authenticate(client, secured):
    response = client.get("/apps")
    assert "HANGAR_API_TOKEN" in response.json()["detail"]
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_deleting_requires_a_token(client, secured, source_app):
    """The destructive route matters most — check it can't be reached anonymously."""
    assert client.delete(f"/apps/{source_app}").status_code == 401
    assert client.delete(f"/apps/{source_app}", headers=auth(TOKEN)).status_code == 204


@pytest.fixture
def source_app(client, secured, tmp_path):
    app_dir = tmp_path / "src"
    app_dir.mkdir()
    (app_dir / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (app_dir / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    response = client.post(
        "/apps",
        json={"name": "guarded-app", "source_path": str(app_dir)},
        headers=auth(TOKEN),
    )
    return response.json()["id"]


# --------------------------------------------------------------------------
# The CLI must not expose an anonymous API
# --------------------------------------------------------------------------


def test_serve_refuses_public_bind_without_a_token(monkeypatch, capsys):
    monkeypatch.delenv("HANGAR_API_TOKEN", raising=False)
    # Fail loudly if the guard is missed and uvicorn actually starts.
    monkeypatch.setattr(
        "uvicorn.run", lambda *a, **kw: pytest.fail("server should not have started")
    )

    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert "refusing to bind" in capsys.readouterr().err


def test_serve_allows_public_bind_with_a_token(monkeypatch, secured):
    started = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: started.update(kw))

    assert cli.main(["serve", "--host", "0.0.0.0"]) == 0
    assert started["host"] == "0.0.0.0"


def test_serve_allows_loopback_without_a_token(monkeypatch):
    monkeypatch.delenv("HANGAR_API_TOKEN", raising=False)
    started = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: started.update(kw))

    assert cli.main(["serve"]) == 0
    assert started["host"] == "127.0.0.1"


def test_config_command_redacts_database_credentials(monkeypatch, capsys):
    monkeypatch.setenv("HANGAR_DATABASE_URL", "postgresql://user:hunter2@host/db")
    assert cli.main(["config"]) == 0

    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "***" in out
