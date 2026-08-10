"""Control plane API tests.

The deploy step is stubbed out — these cover the API contract (validation,
status codes, lifecycle transitions), not the Docker path. The real build-and-
run path is covered in test_deploy.py.
"""

import pytest

from hangar import api as api_mod
from hangar import backends
from hangar import deploy as deploy_mod
from hangar import store


@pytest.fixture
def source(tmp_path):
    app_dir = tmp_path / "src"
    app_dir.mkdir()
    (app_dir / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (app_dir / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    return app_dir


@pytest.fixture(autouse=True)
def backend(fake_backend):
    """Every test in this module runs against the no-op backend."""
    return fake_backend


@pytest.fixture(autouse=True)
def no_real_deploys(monkeypatch):
    """Keep the API tests off the deploy pipeline entirely."""
    calls = []
    monkeypatch.setattr(deploy_mod, "deploy", lambda app_id: calls.append(app_id))
    monkeypatch.setattr(api_mod.deploy_mod, "deploy", lambda app_id: calls.append(app_id))
    return calls


def create(client, source, name="test-app"):
    return client.post("/apps", json={"name": name, "source_path": str(source)})


# --------------------------------------------------------------------------
# Creation and validation
# --------------------------------------------------------------------------


def test_health_reports_configuration(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["backend"] == "fake"
    assert body["auth"] == "disabled"


def test_create_returns_202_and_queued_app(client, source):
    response = create(client, source)
    assert response.status_code == 202
    body = response.json()
    assert body["name"] == "test-app"
    assert body["status"] == "queued"
    assert body["url"] is None
    assert body["id"]


def test_create_schedules_a_deploy(client, source, no_real_deploys):
    app_id = create(client, source).json()["id"]
    assert no_real_deploys == [app_id]


def test_name_is_lowercased(client, source):
    assert create(client, source, name="Test-App").json()["name"] == "test-app"


@pytest.mark.parametrize("name", ["ab", "-leading", "trailing-", "has space", "UPPER!"])
def test_rejects_invalid_names(client, source, name):
    assert create(client, source, name=name).status_code == 422


def test_rejects_relative_source_path(client):
    r = client.post("/apps", json={"name": "rel-app", "source_path": "examples/x"})
    assert r.status_code == 422
    assert "absolute" in r.json()["detail"]


def test_rejects_missing_source_directory(client, tmp_path):
    r = client.post(
        "/apps", json={"name": "gone-app", "source_path": str(tmp_path / "nope")}
    )
    assert r.status_code == 422


def test_rejects_duplicate_name(client, source):
    create(client, source)
    assert create(client, source).status_code == 409


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def test_list_and_get(client, source):
    app_id = create(client, source).json()["id"]
    assert [a["id"] for a in client.get("/apps").json()] == [app_id]
    assert client.get(f"/apps/{app_id}").json()["id"] == app_id


def test_get_unknown_app_is_404(client):
    assert client.get("/apps/doesnotexist").status_code == 404


def test_logs_include_build_log_and_survive_missing_container(client, source):
    app_id = create(client, source).json()["id"]
    with store.session() as sess:
        sess.add(store.Deployment(app_id=app_id, build_log="line one\nline two"))
        sess.commit()

    body = client.get(f"/apps/{app_id}/logs").json()
    assert "line one" in body["build_log"]
    # No container exists, so runtime logs are empty rather than an error.
    assert body["runtime_log"] == ""


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_stop_marks_stopped_and_clears_url(client, source, backend):
    app_id = create(client, source).json()["id"]
    with store.session() as sess:
        app = store.get_app(sess, app_id)
        app.status = "running"
        app.url = "http://localhost:9999"
        store.save(sess, app)

    body = client.post(f"/apps/{app_id}/stop").json()
    assert body["status"] == "stopped"
    assert body["url"] is None


def test_restart_rereads_the_published_port(client, source, backend):
    """Docker can republish on a different port, so the stored URL must refresh."""
    app_id = create(client, source).json()["id"]
    backend.port = 54321

    body = client.post(f"/apps/{app_id}/restart").json()
    assert body["status"] == "running"
    assert body["url"] == "http://localhost:54321"


def test_lifecycle_action_on_missing_container_is_409(client, source, backend):
    app_id = create(client, source).json()["id"]
    backend.errors["stop"] = "no container for app"
    assert client.post(f"/apps/{app_id}/stop").status_code == 409


def test_redeploy_requeues(client, source, no_real_deploys):
    app_id = create(client, source).json()["id"]
    no_real_deploys.clear()

    response = client.post(f"/apps/{app_id}/redeploy")
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert no_real_deploys == [app_id]


def test_delete_removes_app_and_its_deployments(client, source, backend):
    app_id = create(client, source).json()["id"]
    with store.session() as sess:
        sess.add(store.Deployment(app_id=app_id, build_log="x"))
        sess.commit()

    assert client.delete(f"/apps/{app_id}").status_code == 204
    assert client.get(f"/apps/{app_id}").status_code == 404

    with store.session() as sess:
        assert store.deployments_for(sess, app_id) == []


def test_delete_unknown_app_is_404(client):
    assert client.delete("/apps/nope").status_code == 404
