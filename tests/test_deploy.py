"""End-to-end deploy test — the real thing, against a real Docker daemon.

Skipped automatically when Docker isn't reachable, so the suite still runs on a
machine without it. These are slow (a full image build); run just the fast
tests with `uv run pytest -m "not slow"`.
"""

import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hangar import config, runtime, store

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def require_docker(docker_available):
    if not docker_available:
        pytest.skip("Docker daemon not reachable")


def wait_for_http(url: str, timeout: float = 30.0) -> tuple[str, float]:
    """Poll ``url`` until it answers. Returns the body and seconds waited."""
    start = time.monotonic()
    deadline = start + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.read().decode(), time.monotonic() - start
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            time.sleep(0.2)
    raise AssertionError(f"{url} never became ready in {timeout}s (last error: {last})")


@pytest.fixture
def deployed(client, request):
    """Deploy an example app through the API and clean it up afterwards."""
    example = request.param
    response = client.post(
        "/apps", json={"name": example, "source_path": str(EXAMPLES / example)}
    )
    assert response.status_code == 202, response.text
    app_id = response.json()["id"]

    # BackgroundTasks run synchronously on TestClient teardown of the request,
    # so by now the deploy has already finished.
    yield app_id, client.get(f"/apps/{app_id}").json()

    runtime.remove(app_id, missing_ok=True)


@pytest.mark.parametrize(
    "deployed,expected_runtime,expected_framework",
    [("fastapi-hello", "python", "fastapi"), ("express-hello", "node", "express")],
    indirect=["deployed"],
)
def test_example_app_deploys_and_serves(deployed, expected_runtime, expected_framework):
    app_id, app = deployed

    assert app["status"] == "running", app.get("error")
    assert app["runtime"] == expected_runtime
    assert app["framework"] == expected_framework
    assert app["url"]

    body, waited = wait_for_http(app["url"])
    assert "Deployed by Hangar." in body
    # The PRD's cold-start target is under 3 seconds.
    assert waited < 10, f"took {waited:.1f}s to answer"


@pytest.mark.parametrize("deployed", ["fastapi-hello"], indirect=True)
def test_deployment_and_logs_are_recorded(deployed, client):
    app_id, app = deployed

    with store.session() as sess:
        deployment = store.latest_deployment(sess, app_id)
        assert deployment is not None
        assert deployment.status == "succeeded"
        assert deployment.image_ref
        assert "detected python/fastapi" in deployment.build_log

    # Serve a request first — the container is started but uvicorn may not have
    # flushed its startup banner at the moment the deploy call returns.
    wait_for_http(app["url"])

    logs = _poll_logs(client, app_id)
    assert "detected python/fastapi" in logs["build_log"]
    assert "Uvicorn running" in logs["runtime_log"]


def _poll_logs(client, app_id: str, timeout: float = 10.0) -> dict:
    """Fetch logs until the runtime log is non-empty."""
    deadline = time.monotonic() + timeout
    logs = {}
    while time.monotonic() < deadline:
        logs = client.get(f"/apps/{app_id}/logs").json()
        if logs["runtime_log"].strip():
            return logs
        time.sleep(0.2)
    return logs


@pytest.mark.parametrize("deployed", ["fastapi-hello"], indirect=True)
def test_stop_then_restart(deployed, client):
    app_id, app = deployed

    assert client.post(f"/apps/{app_id}/stop").json()["status"] == "stopped"
    assert runtime.status(app_id) in ("exited", "created")

    restarted = client.post(f"/apps/{app_id}/restart").json()
    assert restarted["status"] == "running"
    body, _ = wait_for_http(restarted["url"])
    assert "Deployed by Hangar." in body


@pytest.mark.parametrize("deployed", ["fastapi-hello"], indirect=True)
def test_delete_removes_the_container(deployed, client):
    app_id, _ = deployed

    assert client.delete(f"/apps/{app_id}").status_code == 204
    assert runtime.status(app_id) == "absent"
    assert client.get(f"/apps/{app_id}").status_code == 404


def test_failed_detection_marks_the_app_failed(client, tmp_path):
    """An unsupported stack must fail loudly, with the reason on the app."""
    source = tmp_path / "go-app"
    source.mkdir()
    (source / "main.go").write_text("package main\n", encoding="utf-8")
    (source / "go.mod").write_text("module x\n", encoding="utf-8")

    app_id = client.post(
        "/apps", json={"name": "go-app", "source_path": str(source)}
    ).json()["id"]

    app = client.get(f"/apps/{app_id}").json()
    assert app["status"] == "failed"
    assert "Python or Node" in app["error"]

    logs = client.get(f"/apps/{app_id}/logs").json()
    assert "ERROR" in logs["build_log"]


def test_resource_caps_are_applied(client, docker_available):
    """PRD §8: every app gets hard caps so one runaway app can't degrade the box."""
    import docker as docker_sdk

    name = "capped-app"
    app_id = client.post(
        "/apps", json={"name": name, "source_path": str(EXAMPLES / "fastapi-hello")}
    ).json()["id"]
    try:
        app = client.get(f"/apps/{app_id}").json()
        assert app["status"] == "running", app.get("error")

        container = docker_sdk.from_env().containers.get(f"hangar-{app_id}")
        host_config = container.attrs["HostConfig"]
        limits = config.settings()

        assert host_config["Memory"] == limits.memory_mb * 1024 * 1024
        assert host_config["NanoCpus"] == int(limits.cpus * 1_000_000_000)
        assert host_config["PidsLimit"] == limits.pids
        assert host_config["CapDrop"] == ["ALL"]
        assert host_config["ReadonlyRootfs"] is True
        assert "no-new-privileges:true" in host_config["SecurityOpt"]
    finally:
        runtime.remove(app_id, missing_ok=True)


def test_limits_honour_configuration(client, monkeypatch, docker_available):
    """Caps are configurable, so the Oracle box can run tighter than a laptop."""
    monkeypatch.setenv("HANGAR_APP_MEMORY_MB", "256")
    monkeypatch.setenv("HANGAR_APP_PIDS", "64")

    import docker as docker_sdk

    app_id = client.post(
        "/apps", json={"name": "tight-app", "source_path": str(EXAMPLES / "fastapi-hello")}
    ).json()["id"]
    try:
        assert client.get(f"/apps/{app_id}").json()["status"] == "running"
        host_config = docker_sdk.from_env().containers.get(
            f"hangar-{app_id}"
        ).attrs["HostConfig"]

        assert host_config["Memory"] == 256 * 1024 * 1024
        assert host_config["PidsLimit"] == 64
    finally:
        runtime.remove(app_id, missing_ok=True)


def test_sandbox_runtime_is_requested_when_configured(client, monkeypatch):
    """HANGAR_RUNTIME=runsc is the gVisor switch the PRD's §8 depends on.

    gVisor isn't installed here, so the deploy must fail — but it must fail
    because Docker rejected an unknown runtime, proving the setting reached it.
    """
    monkeypatch.setenv("HANGAR_RUNTIME", "definitely-not-installed")

    app_id = client.post(
        "/apps",
        json={"name": "sandboxed-app", "source_path": str(EXAMPLES / "fastapi-hello")},
    ).json()["id"]
    try:
        app = client.get(f"/apps/{app_id}").json()
        assert app["status"] == "failed"
        assert "definitely-not-installed" in app["error"]
    finally:
        runtime.remove(app_id, missing_ok=True)
