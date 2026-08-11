"""Routing verified against a real Caddy.

A stub can only prove Hangar sends the JSON it thinks it sends. These tests
run an actual Caddy container, deploy an actual app, and make an actual HTTP
request through the proxy — which is the only way to know the route works.

Requests carry an explicit Host header instead of relying on DNS, so no
hostname needs to resolve for the test to be meaningful.
"""

import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hangar import routing, runtime

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CADDY_IMAGE = "caddy:2-alpine"
APP_DOMAIN = "apps.hangar-test"

pytestmark = pytest.mark.slow


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(url: str, host: str | None = None, timeout: float = 3.0):
    """Returns (status, body). A refused connection surfaces as status 0."""
    request = urllib.request.Request(url)
    if host:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, ""


@pytest.fixture(scope="module")
def caddy(request):
    """A real Caddy container with its admin API reachable from the host."""
    try:
        import docker as docker_sdk

        client = docker_sdk.from_env()
        client.ping()
    except Exception:
        pytest.skip("Docker daemon not reachable")

    client.images.pull(CADDY_IMAGE)
    admin_port, http_port = free_port(), free_port()

    container = client.containers.run(
        CADDY_IMAGE,
        name=f"hangar-test-caddy-{admin_port}",
        detach=True,
        # Caddy's admin API binds to loopback inside the container by default,
        # which would make it unreachable from the test process.
        environment={"CADDY_ADMIN": "0.0.0.0:2019"},
        ports={"2019/tcp": admin_port, "80/tcp": http_port},
        # Docker Desktop provides host.docker.internal automatically; plain
        # Linux Docker (CI) does not, and without this the proxy cannot reach
        # the app ports published on the host.
        extra_hosts={"host.docker.internal": "host-gateway"},
    )

    admin_url = f"http://localhost:{admin_port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if get(f"{admin_url}/config/")[0] == 200:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"Caddy admin API never came up: {container.logs()[-2000:]}")

        # Clear the image's default Caddyfile config. It installs a catch-all
        # welcome-page route, which would answer every request regardless of
        # our routing and make these assertions meaningless. Starting empty
        # also exercises Hangar's bootstrap path against a real Caddy.
        _post_json(f"{admin_url}/load", {"admin": {"listen": "0.0.0.0:2019"}})

        yield admin_url, http_port
    finally:
        container.remove(force=True)


def _post_json(url: str, payload: dict) -> int:
    import json

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


@pytest.fixture
def routed(monkeypatch, caddy):
    """Point Hangar at the test Caddy."""
    admin_url, http_port = caddy
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_CADDY_ADMIN_URL", admin_url)
    monkeypatch.setenv("HANGAR_APP_DOMAIN", APP_DOMAIN)
    monkeypatch.setenv("HANGAR_APP_SCHEME", "http")
    # Caddy is containerised here, so loopback would resolve to itself rather
    # than to the host publishing the app's port.
    monkeypatch.setenv("HANGAR_UPSTREAM_HOST", "host.docker.internal")
    return http_port


@pytest.fixture
def deployed(client, routed):
    """Deploy the FastAPI sample behind Caddy; clean up afterwards."""
    name = "routed-app"
    response = client.post(
        "/apps", json={"name": name, "source_path": str(EXAMPLES / "fastapi-hello")}
    )
    assert response.status_code == 202, response.text
    app_id = response.json()["id"]

    yield app_id, client.get(f"/apps/{app_id}").json(), routed

    runtime.remove(app_id, missing_ok=True)
    routing.get_router().remove(app_id, missing_ok=True)


def wait_for_route(port: int, host: str, timeout: float = 20.0):
    """Poll through Caddy until the app answers."""
    deadline = time.monotonic() + timeout
    last = (0, "")
    while time.monotonic() < deadline:
        last = get(f"http://localhost:{port}/", host=host)
        if last[0] == 200:
            return last
        time.sleep(0.3)
    return last


# --------------------------------------------------------------------------


def test_app_gets_a_stable_hostname_not_a_random_port(deployed):
    _, app, _ = deployed

    assert app["status"] == "running", app.get("error")
    # The whole point: a name someone can bookmark, not localhost:64800.
    assert app["url"] == f"http://routed-app.{APP_DOMAIN}"
    assert "localhost" not in app["url"]


def test_traffic_actually_reaches_the_container_through_caddy(deployed):
    _, app, http_port = deployed

    status, body = wait_for_route(http_port, f"routed-app.{APP_DOMAIN}")
    assert status == 200, f"Caddy did not proxy to the app (got {status})"
    assert "Deployed by Hangar." in body
    assert "fastapi-hello" in body


def test_unknown_hostnames_are_not_served(deployed):
    """A route must match only its own app, or apps leak into each other."""
    _, _, http_port = deployed
    wait_for_route(http_port, f"routed-app.{APP_DOMAIN}")

    _, body = get(f"http://localhost:{http_port}/", host=f"nope.{APP_DOMAIN}")
    assert "Deployed by Hangar." not in body


def test_deleting_an_app_withdraws_its_route(deployed, client):
    app_id, _, http_port = deployed
    wait_for_route(http_port, f"routed-app.{APP_DOMAIN}")

    assert client.delete(f"/apps/{app_id}").status_code == 204

    # Asserting on the body, not the status: with no route matching, Caddy
    # answers with an empty 200 rather than refusing the connection.
    _, body = get(f"http://localhost:{http_port}/", host=f"routed-app.{APP_DOMAIN}")
    assert "Deployed by Hangar." not in body, "still served after delete"


def test_stopping_an_app_withdraws_its_route(deployed, client):
    app_id, _, http_port = deployed
    wait_for_route(http_port, f"routed-app.{APP_DOMAIN}")

    assert client.post(f"/apps/{app_id}/stop").json()["status"] == "stopped"
    _, body = get(f"http://localhost:{http_port}/", host=f"routed-app.{APP_DOMAIN}")
    assert "Deployed by Hangar." not in body, "still served after stop"


def test_restart_keeps_the_hostname_across_a_port_change(deployed, client):
    """The point of routing by name: the URL survives the port moving."""
    app_id, app, http_port = deployed
    wait_for_route(http_port, f"routed-app.{APP_DOMAIN}")

    client.post(f"/apps/{app_id}/stop")
    restarted = client.post(f"/apps/{app_id}/restart").json()

    assert restarted["url"] == app["url"]
    status, body = wait_for_route(http_port, f"routed-app.{APP_DOMAIN}")
    assert status == 200, "app unreachable after restart"
    assert "Deployed by Hangar." in body


def test_redeploy_does_not_duplicate_routes(deployed, client, routed):
    """Upsert must replace, not append — duplicates would shadow each other."""
    app_id, _, http_port = deployed
    wait_for_route(http_port, f"routed-app.{APP_DOMAIN}")

    client.post(f"/apps/{app_id}/redeploy")
    status, _ = wait_for_route(http_port, f"routed-app.{APP_DOMAIN}")
    assert status == 200

    admin_url = routing.get_router().admin
    _, config_body = get(f"{admin_url}/config/")
    assert config_body.count(f'"hangar-{app_id}"') == 1


def test_health_reports_the_router_is_reachable(client, routed):
    body = client.get("/healthz").json()
    assert body["router"] == "caddy"
    assert body["router_available"] is True
    assert body["app_domain"] == APP_DOMAIN
