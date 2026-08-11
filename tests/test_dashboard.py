"""The dashboard mount must never shadow the API.

It is mounted at "/" and matches everything, so registration order is the only
thing keeping /apps and /healthz working. That is exactly the kind of thing
that breaks silently, so it is asserted here.
"""

import pytest

from hangar import api as api_mod
from hangar import deploy as deploy_mod


@pytest.fixture(autouse=True)
def backend(fake_backend):
    return fake_backend


@pytest.fixture(autouse=True)
def no_real_deploys(monkeypatch):
    monkeypatch.setattr(deploy_mod, "deploy", lambda app_id: None)
    monkeypatch.setattr(api_mod.deploy_mod, "deploy", lambda app_id: None)


def built() -> bool:
    return (api_mod.dashboard_dir() / "index.html").is_file()


def test_api_routes_still_resolve_with_the_dashboard_mounted(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/apps").status_code == 200


def test_upload_route_is_not_swallowed_by_the_app_id_route(client):
    """/apps/upload must not be read as an app whose id is "upload"."""
    response = client.post("/apps/upload", data={"name": "x"})
    # 422 for the missing file, not 404 for a missing app.
    assert response.status_code == 422


def test_openapi_still_served(client):
    schema = client.get("/openapi.json").json()
    assert "/apps" in schema["paths"]
    assert "/apps/{app_id}/scan" in schema["paths"]


def test_dashboard_is_served_when_built(client):
    if not built():
        pytest.skip("dashboard not built")

    response = client.get("/")
    assert response.status_code == 200
    assert 'id="root"' in response.text


def test_dashboard_needs_no_token_even_when_auth_is_on(client, monkeypatch):
    """It's a static bundle with no secrets; it prompts for the token itself."""
    if not built():
        pytest.skip("dashboard not built")

    monkeypatch.setenv("HANGAR_API_TOKEN", "secret")
    assert client.get("/").status_code == 200
    assert client.get("/apps").status_code == 401


def test_missing_dashboard_does_not_break_the_api(monkeypatch, tmp_path):
    """A control plane deployed without building the frontend must still work."""
    monkeypatch.setenv("HANGAR_DASHBOARD_DIR", str(tmp_path / "nothing-here"))
    assert api_mod.mount_dashboard(api_mod.FastAPI()) is False
