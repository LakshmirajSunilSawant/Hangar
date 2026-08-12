"""Scale-to-zero against a real Caddy and a real container.

tests/test_idle.py proves the decisions are right with a fake clock and a fake
backend. It cannot prove the part that actually matters to a visitor: that a
request to a *stopped* container comes back with the app's response instead of
a 502, because that depends on Caddy retrying an upstream that refuses the
first connection — behaviour no stub of ours can demonstrate.

This is the same lesson as the routing bug, where the stub tests were green
while a real Caddy returned its welcome page to every request.

Marked slow. Needs a Docker daemon; skips without one.
"""

import time

import pytest

from hangar import idle, routing, runtime, store
from hangar.store import AppStatus

# The proxy and control-plane harness, shared with the forward-auth tests.
from tests.test_forward_auth import (  # noqa: F401 - fixtures are used by name
    caddy,
    control_plane,
    http,
)

APP_DOMAIN = "apps.hangar-test"
APP_NAME = "sleepy"

# A static binary that serves HTTP on :80 and writes nothing, so it survives
# the read-only root filesystem and dropped capabilities that runtime.run
# applies. Standing in for a real app, whose build is not what's under test.
IMAGE = "traefik/whoami"
APP_PORT = 80

pytestmark = pytest.mark.slow


@pytest.fixture
def sleepy(caddy, control_plane, monkeypatch, docker_available):  # noqa: F811
    """A real, routed, running app with scale-to-zero switched on."""
    if not docker_available:
        pytest.skip("Docker daemon not reachable")

    import docker as docker_sdk

    admin_url, http_port = caddy
    _, cp_port = control_plane

    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_CADDY_ADMIN_URL", admin_url)
    monkeypatch.setenv("HANGAR_APP_DOMAIN", APP_DOMAIN)
    monkeypatch.setenv("HANGAR_APP_SCHEME", "http")
    # Auth off on purpose: this exercises the hook installed *only* for waking.
    monkeypatch.setenv("HANGAR_APP_AUTH", "0")
    monkeypatch.setenv("HANGAR_IDLE_TIMEOUT", "300")
    monkeypatch.setenv("HANGAR_IDLE_CHECK_INTERVAL", "30")
    monkeypatch.setenv("HANGAR_WAKE_TIMEOUT", "30")
    # A published port, so this test doesn't also need the internal network.
    monkeypatch.setenv("HANGAR_EGRESS", "allow")
    monkeypatch.setenv("HANGAR_UPSTREAM_HOST", "host.docker.internal")
    monkeypatch.setenv(
        "HANGAR_CONTROL_PLANE_ADDRESS", f"host.docker.internal:{cp_port}"
    )

    docker = docker_sdk.from_env()
    docker.images.pull(IMAGE)

    with store.session() as sess:
        app = store.App(
            name=APP_NAME,
            source_ref="/none",
            source_dir="/none",
            status=AppStatus.RUNNING,
        )
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
        app = store.get_app(sess, app_id)
        app.host_port, app.upstream = running.host_port, running.upstream
        store.save(sess, app)

    routing.get_router().upsert(
        app_id=app_id,
        app_name=APP_NAME,
        upstream=running.upstream,
        host_port=running.host_port,
    )

    try:
        _wait_for_app(http_port)
        yield app_id, http_port, docker
    finally:
        routing.get_router().remove(app_id, missing_ok=True)
        runtime.remove(app_id, docker_client=docker, missing_ok=True)


def _wait_for_app(http_port: int, timeout: float = 60.0) -> None:
    """Poll through Caddy until the app answers, so tests start from 'up'."""
    deadline = time.monotonic() + timeout
    last: tuple[int, str] = (0, "")
    while time.monotonic() < deadline:
        status, body, _ = http(
            f"http://localhost:{http_port}/", host=f"{APP_NAME}.{APP_DOMAIN}"
        )
        if status == 200:
            return
        last = (status, body[:200])
        time.sleep(0.25)
    pytest.fail(f"app never answered through Caddy: {last}")


def container_state(docker, app_id: str) -> str:
    container = docker.containers.get(f"hangar-{app_id}")
    container.reload()
    return container.status


def visit(http_port: int):
    return http(f"http://localhost:{http_port}/", host=f"{APP_NAME}.{APP_DOMAIN}")


# --------------------------------------------------------------------------


def test_sleeping_actually_stops_the_container(sleepy):
    """Not a status field being flipped — the process is gone and the RAM back."""
    app_id, _, docker = sleepy
    assert container_state(docker, app_id) == "running"

    assert idle.put_to_sleep(app_id) is True
    assert container_state(docker, app_id) == "exited"


def test_a_request_to_a_sleeping_app_returns_the_app(sleepy):
    """The one that matters: a visitor gets the app, not a 502."""
    app_id, http_port, docker = sleepy
    idle.put_to_sleep(app_id)
    assert container_state(docker, app_id) == "exited"

    started = time.monotonic()
    status, body, _ = visit(http_port)
    elapsed = time.monotonic() - started

    assert status == 200, f"waking returned {status}: {body[:300]}"
    assert "Hostname" in body, f"reached something that isn't the app: {body[:200]}"
    assert container_state(docker, app_id) == "running"
    # PRD §9 targets under 3 seconds. Asserted loosely because CI hardware is
    # not the Oracle box; scripts/measure-wake.sh is what reports the number.
    print(f"\nwake through the proxy: {elapsed:.2f}s")
    assert elapsed < 30


def test_the_app_is_marked_running_again_afterwards(sleepy):
    app_id, http_port, _ = sleepy
    idle.put_to_sleep(app_id)
    visit(http_port)

    with store.session() as sess:
        assert store.get_app(sess, app_id).status == AppStatus.RUNNING


def test_a_woken_container_keeps_its_published_port(sleepy):
    """A new port would leave the route pointing at nothing."""
    app_id, _, docker = sleepy
    before = runtime.host_port(app_id, docker_client=docker)

    idle.put_to_sleep(app_id)
    idle.wake(app_id)

    assert runtime.host_port(app_id, docker_client=docker) == before


def test_a_second_visit_does_not_restart_anything(sleepy):
    """Waking must be once per sleep, not once per request."""
    app_id, http_port, docker = sleepy
    idle.put_to_sleep(app_id)
    visit(http_port)

    started_at = docker.containers.get(f"hangar-{app_id}").attrs["State"]["StartedAt"]
    visit(http_port)
    assert (
        docker.containers.get(f"hangar-{app_id}").attrs["State"]["StartedAt"]
        == started_at
    )
