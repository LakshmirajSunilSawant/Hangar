"""Egress default-deny, verified against real Docker networking.

PRD §8: "Network egress from sandboxes is default-deny except to an explicit
allowlist." A test that only asserts Hangar *asked* for an internal network
would pass even if the container still had full internet access, so these
tests execute a real outbound request from inside the sandbox and require it
to fail.

The trade-off this design has to live with: a container on an internal network
cannot publish a port to the host, so egress-deny only works with a proxy
attached to the same network. That is asserted here too.
"""

import time
from pathlib import Path

import pytest

from hangar import config, runtime
from hangar.backends.base import DeployError

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
PROBE_IMAGE = "alpine:latest"

pytestmark = pytest.mark.slow


@pytest.fixture
def docker(docker_available):
    if not docker_available:
        pytest.skip("Docker daemon not reachable")
    import docker as docker_sdk

    return docker_sdk.from_env()


@pytest.fixture
def deny_egress(monkeypatch):
    monkeypatch.setenv("HANGAR_EGRESS", "deny")
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_APP_DOMAIN", "apps.test")
    monkeypatch.setenv("HANGAR_APP_NETWORK", "hangar-test-egress")
    yield "hangar-test-egress"


@pytest.fixture
def network(docker, deny_egress):
    name = deny_egress
    yield name
    try:
        docker.networks.get(name).remove()
    except Exception:
        pass


# --------------------------------------------------------------------------
# The property that matters
# --------------------------------------------------------------------------


def test_app_on_the_internal_network_cannot_reach_the_internet(docker, network):
    """The actual §8 requirement, proved by trying."""
    runtime._ensure_internal_network(docker, network)
    docker.images.pull(PROBE_IMAGE)

    result = docker.containers.run(
        PROBE_IMAGE,
        command=[
            "sh", "-c",
            "wget -q -T 3 -O- http://example.com >/dev/null 2>&1 "
            "&& echo REACHED || echo BLOCKED",
        ],
        network=network,
        remove=True,
    )
    assert b"BLOCKED" in result, "sandbox still had outbound internet access"


def test_dns_resolution_also_fails_on_the_internal_network(docker, network):
    """Exfiltration over DNS is a real technique; the name lookup must fail too."""
    runtime._ensure_internal_network(docker, network)

    result = docker.containers.run(
        PROBE_IMAGE,
        command=[
            "sh", "-c",
            "nslookup example.com >/dev/null 2>&1 && echo RESOLVED || echo BLOCKED",
        ],
        network=network,
        remove=True,
    )
    assert b"BLOCKED" in result


def test_default_is_allow_so_builds_and_normal_apps_still_work(monkeypatch):
    """Egress-deny needs a proxy, so it can't be the out-of-the-box default."""
    monkeypatch.delenv("HANGAR_EGRESS", raising=False)
    assert config.settings().egress_denied is False


# --------------------------------------------------------------------------
# The constraint the design has to live with
# --------------------------------------------------------------------------


def test_denying_egress_without_a_router_is_rejected_up_front(monkeypatch):
    """Silently unreachable apps would be far worse than a clear error."""
    monkeypatch.setenv("HANGAR_EGRESS", "deny")
    monkeypatch.setenv("HANGAR_ROUTER", "none")

    with pytest.raises(ValueError, match="only reachable through a proxy"):
        config.settings().validate()


def test_a_non_internal_network_is_refused(docker, deny_egress, monkeypatch):
    """An existing bridge network of the same name would leave egress wide open
    while Hangar reported it as denied — worse than failing."""
    name = "hangar-test-not-internal"
    monkeypatch.setenv("HANGAR_APP_NETWORK", name)
    docker.networks.create(name, driver="bridge", internal=False)
    try:
        with pytest.raises(DeployError, match="not internal"):
            runtime._ensure_internal_network(docker, name)
    finally:
        docker.networks.get(name).remove()


def test_network_is_created_as_internal(docker, network):
    created = runtime._ensure_internal_network(docker, network)
    assert created.attrs["Internal"] is True


def test_existing_internal_network_is_reused(docker, network):
    first = runtime._ensure_internal_network(docker, network)
    second = runtime._ensure_internal_network(docker, network)
    assert first.id == second.id


# --------------------------------------------------------------------------
# End to end through the deploy pipeline
# --------------------------------------------------------------------------


def test_run_attaches_to_the_internal_network_and_publishes_nothing(
    db, docker, network
):
    """With egress denied the app is off the host network entirely.

    Driven through runtime.run rather than the API, because a deploy without a
    reachable Caddy tears its container down again — correctly — leaving
    nothing to inspect.
    """
    docker.images.pull("nginx:alpine")
    app_id = "egresstest01"

    app = runtime.run("nginx:alpine", app_id=app_id, app_name="sealed", container_port=80)
    try:
        assert app.host_port is None, "published a host port despite egress deny"
        assert app.upstream == f"hangar-{app_id}:80"

        networks = docker.containers.get(f"hangar-{app_id}").attrs[
            "NetworkSettings"
        ]["Networks"]
        assert network in networks, f"app not attached to {network}"
        assert "bridge" not in networks, "also on the default bridge — egress leaks"
    finally:
        runtime.remove(app_id, missing_ok=True)


def test_run_publishes_a_port_when_egress_is_allowed(db, docker, monkeypatch):
    """The contrast case: normal mode still reaches the host."""
    monkeypatch.setenv("HANGAR_EGRESS", "allow")
    monkeypatch.delenv("HANGAR_ROUTER", raising=False)
    docker.images.pull("nginx:alpine")
    app_id = "egresstest02"

    app = runtime.run("nginx:alpine", app_id=app_id, app_name="open", container_port=80)
    try:
        assert app.host_port is not None
        assert app.upstream == f"127.0.0.1:{app.host_port}"
    finally:
        runtime.remove(app_id, missing_ok=True)


def test_deploy_reports_failure_rather_than_leaving_an_unreachable_app(
    client, docker, network
):
    """No Caddy is running, so routing fails; the app must not look healthy."""
    app_id = client.post(
        "/apps",
        json={"name": "unroutable-app", "source_path": str(EXAMPLES / "fastapi-hello")},
    ).json()["id"]
    try:
        app = client.get(f"/apps/{app_id}").json()
        assert app["status"] == "failed"
        # And the container it started was cleaned up rather than orphaned.
        time.sleep(0.5)
        assert runtime.status(app_id) == "absent"
    finally:
        runtime.remove(app_id, missing_ok=True)
