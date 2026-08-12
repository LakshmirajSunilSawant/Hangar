"""Rebuilding the proxy's routing table from the database.

The bug this exists for: Caddy's official image starts with
`caddy run --config /etc/caddy/Caddyfile`, so a Caddy restart throws away every
route Hangar pushed through the admin API and replaces them with the image's
welcome page. Nothing errors — the control plane is fine, the containers are
fine, the database still lists a URL for every app — and each of those URLs
serves "Caddy works!" to whoever opens it.

It was found on a live stack, where every app had been quietly broken for an
hour after WSL rebooted.
"""

import pytest

from hangar import reconcile, store
from hangar.routing import Router, RoutingError
from hangar.store import App, AppStatus


class RecordingRouter(Router):
    """Records what would be published, and can be told to fail."""

    name = "recording"

    def __init__(self, fail_for: set[str] | None = None):
        self.published: list[tuple[str, str, str]] = []
        self.hosts: list[tuple[str, str, str]] = []
        self.fail_for = fail_for or set()

    def available(self) -> bool:
        return True

    def upsert(self, *, app_id, app_name, upstream, host_port):
        if app_name in self.fail_for:
            raise RoutingError(f"nope: {app_name}")
        self.published.append((app_id, app_name, upstream))
        return f"http://{app_name}.test"

    def remove(self, app_id, *, missing_ok=True):
        return None

    def upsert_host(self, *, route_id, hostname, upstream):
        if hostname in self.fail_for:
            raise RoutingError(f"nope: {hostname}")
        self.hosts.append((route_id, hostname, upstream))


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_APP_DOMAIN", "apps.test")


def make_app(name: str, status: str, upstream: str | None = "host:8000") -> str:
    with store.session() as sess:
        app = App(
            name=name,
            source_ref="/s",
            status=status,
            upstream=upstream,
            url=f"http://{name}.apps.test",
        )
        store.save(sess, app)
        return app.id


def names(router: RecordingRouter) -> set[str]:
    return {name for _, name, _ in router.published}


# --------------------------------------------------------------------------


def test_running_apps_are_republished(db, routed):
    make_app("notes", AppStatus.RUNNING)
    router = RecordingRouter()

    assert reconcile.reconcile(router) == 1
    assert names(router) == {"notes"}


def test_sleeping_apps_are_republished_too(db, routed):
    """A slept app's URL must keep working — that is the whole point of it."""
    make_app("notes", AppStatus.SLEEPING)
    router = RecordingRouter()

    reconcile.reconcile(router)
    assert names(router) == {"notes"}


@pytest.mark.parametrize(
    "status", [AppStatus.STOPPED, AppStatus.FAILED, AppStatus.QUEUED, AppStatus.BUILDING]
)
def test_apps_that_should_not_be_reachable_are_left_alone(db, routed, status):
    """A stopped app's route was withdrawn deliberately; restoring it is wrong."""
    make_app("notes", status)
    router = RecordingRouter()

    assert reconcile.reconcile(router) == 0
    assert router.published == []


def test_an_app_with_no_upstream_is_skipped(db, routed):
    """Nothing to dial — publishing would point the hostname at an empty string."""
    make_app("notes", AppStatus.RUNNING, upstream=None)
    router = RecordingRouter()

    assert reconcile.reconcile(router) == 0


def test_one_bad_app_does_not_strand_the_others(db, routed):
    """Best-effort: a single unroutable app must not take the rest down with it."""
    make_app("good-one", AppStatus.RUNNING)
    make_app("bad-one", AppStatus.RUNNING)
    make_app("good-two", AppStatus.RUNNING)
    router = RecordingRouter(fail_for={"bad-one"})

    assert reconcile.reconcile(router) == 2
    assert names(router) == {"good-one", "good-two"}


def test_nothing_happens_without_a_router(db, monkeypatch):
    monkeypatch.setenv("HANGAR_ROUTER", "none")
    make_app("notes", AppStatus.RUNNING)
    router = RecordingRouter()

    assert reconcile.reconcile(router) == 0


# -- the dashboard's own route ---------------------------------------------


def test_the_dashboard_route_is_published_when_configured(db, routed, monkeypatch):
    """Otherwise the dashboard is the one thing a restart leaves unreachable."""
    monkeypatch.setenv("HANGAR_CONTROL_PLANE_HOST", "hangar.apps.test")
    monkeypatch.setenv("HANGAR_CONTROL_PLANE_ADDRESS", "hangar:8080")
    router = RecordingRouter()

    reconcile.reconcile(router)
    assert router.hosts == [
        (reconcile.CONTROL_PLANE_ROUTE, "hangar.apps.test", "hangar:8080")
    ]


def test_no_dashboard_route_unless_asked_for(db, routed):
    """Somebody may already be publishing it themselves."""
    router = RecordingRouter()
    reconcile.reconcile(router)

    assert router.hosts == []


def test_a_failed_dashboard_route_does_not_stop_the_apps(db, routed, monkeypatch):
    monkeypatch.setenv("HANGAR_CONTROL_PLANE_HOST", "hangar.apps.test")
    make_app("notes", AppStatus.RUNNING)
    router = RecordingRouter(fail_for={"hangar.apps.test"})

    assert reconcile.reconcile(router) == 1
    assert names(router) == {"notes"}
