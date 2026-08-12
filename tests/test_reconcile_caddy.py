"""Reconciliation against a Caddy that really restarts.

The failure being reproduced here cannot be shown with a stub, because the
thing that goes wrong is Caddy's own startup: its official image runs
`caddy run --config /etc/caddy/Caddyfile`, so a restart loads the packaged
Caddyfile and silently discards every route Hangar pushed through the admin
API. Every app URL then serves the image's welcome page with a 200.

So this test restarts a real Caddy and asserts both halves: that the routes
are genuinely gone afterwards (if they weren't, the fix would be untested and
the test falsely green), and that reconcile() brings them back well enough to
serve the app again.

Marked slow. Needs a Docker daemon; skips without one.
"""

import time

import pytest

from hangar import reconcile, routing, runtime, store
from hangar.store import App, AppStatus
from tests.test_forward_auth import free_port, http, post_json

APP_DOMAIN = "apps.hangar-test"
APP_NAME = "survivor"
CADDY_IMAGE = "caddy:2-alpine"
IMAGE = "traefik/whoami"
APP_PORT = 80

pytestmark = pytest.mark.slow


@pytest.fixture
def stack(db, monkeypatch, docker_available):
    """A real Caddy and a real app container, routed together."""
    if not docker_available:
        pytest.skip("Docker daemon not reachable")

    import docker as docker_sdk

    docker = docker_sdk.from_env()
    docker.images.pull(CADDY_IMAGE)
    docker.images.pull(IMAGE)

    admin_port, http_port = free_port(), free_port()
    caddy = docker.containers.run(
        CADDY_IMAGE,
        name=f"hangar-reconcile-caddy-{admin_port}",
        detach=True,
        environment={"CADDY_ADMIN": "0.0.0.0:2019"},
        ports={"2019/tcp": admin_port, "80/tcp": http_port},
        extra_hosts={"host.docker.internal": "host-gateway"},
    )
    admin = f"http://localhost:{admin_port}"

    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_CADDY_ADMIN_URL", admin)
    monkeypatch.setenv("HANGAR_APP_DOMAIN", APP_DOMAIN)
    monkeypatch.setenv("HANGAR_APP_SCHEME", "http")
    monkeypatch.setenv("HANGAR_APP_AUTH", "0")
    monkeypatch.setenv("HANGAR_EGRESS", "allow")
    monkeypatch.setenv("HANGAR_UPSTREAM_HOST", "host.docker.internal")

    app_id = None
    try:
        _await(lambda: http(f"{admin}/config/")[0] == 200, "Caddy never started")
        # Clear the packaged catch-all, the way a fresh deployment would.
        post_json(f"{admin}/load", {"admin": {"listen": "0.0.0.0:2019"}})

        with store.session() as sess:
            app = App(name=APP_NAME, source_ref="/s", status=AppStatus.RUNNING)
            store.save(sess, app)
            app_id = app.id

        running = runtime.run(
            IMAGE,
            app_id=app_id,
            app_name=APP_NAME,
            container_port=APP_PORT,
            docker_client=docker,
        )
        with store.session() as sess:
            record = store.get_app(sess, app_id)
            record.host_port, record.upstream = running.host_port, running.upstream
            store.save(sess, record)

        routing.get_router().upsert(
            app_id=app_id,
            app_name=APP_NAME,
            upstream=running.upstream,
            host_port=running.host_port,
        )
        _await(lambda: serves_the_app(http_port), "app never answered via Caddy")

        yield caddy, http_port
    finally:
        if app_id:
            runtime.remove(app_id, docker_client=docker, missing_ok=True)
        caddy.remove(force=True)


def _await(condition, message: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    pytest.fail(message)


def fetch(http_port: int):
    return http(f"http://localhost:{http_port}/", host=f"{APP_NAME}.{APP_DOMAIN}")


def serves_the_app(http_port: int) -> bool:
    status, body, _ = fetch(http_port)
    return status == 200 and "Hostname" in body


# --------------------------------------------------------------------------


def test_a_caddy_restart_loses_every_route(stack):
    """The bug itself. If this ever fails, the fix below is no longer needed."""
    caddy, http_port = stack
    assert serves_the_app(http_port)

    caddy.restart(timeout=10)
    _await(lambda: fetch(http_port)[0] != 0, "Caddy never came back")

    status, body, _ = fetch(http_port)
    assert not serves_the_app(http_port), "expected the route to be lost"
    # And the shape of the failure: a cheerful 200, not an error anyone notices.
    assert status == 200 and "Caddy works" in body


def test_reconcile_brings_the_app_back(stack):
    caddy, http_port = stack
    caddy.restart(timeout=10)
    _await(lambda: fetch(http_port)[0] != 0, "Caddy never came back")
    assert not serves_the_app(http_port)

    assert reconcile.reconcile() == 1
    _await(lambda: serves_the_app(http_port), "reconcile did not restore the route")


def test_reconcile_is_safe_when_nothing_is_wrong(stack):
    """It runs on every start, so running it against a healthy proxy must be a no-op."""
    _, http_port = stack

    for _ in range(3):
        assert reconcile.reconcile() == 1
    assert serves_the_app(http_port)
