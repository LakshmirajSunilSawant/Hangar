"""Scale-to-zero: sleeping unused apps and waking them on the next request.

The reaper is tested against a fake clock rather than by waiting, so these are
fast and deterministic. What that cannot prove is that a woken container is
actually listening in time — that lives in the Caddy retry window and is
covered by test_routing.py asserting the try_duration is there at all, and by
scripts/measure-wake.sh against a real stack.
"""

import pytest

from hangar import config, idle, store
from hangar.backends.base import BackendError
from hangar.store import App, AppStatus


@pytest.fixture
def tracker():
    return idle.Tracker()


@pytest.fixture
def idle_on(monkeypatch):
    """Scale-to-zero enabled, with a router so validate() is satisfied."""
    monkeypatch.setenv("HANGAR_IDLE_TIMEOUT", "300")
    monkeypatch.setenv("HANGAR_IDLE_CHECK_INTERVAL", "30")
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_APP_DOMAIN", "apps.test")


def make_app(name: str = "notes", status: str = AppStatus.RUNNING) -> str:
    with store.session() as sess:
        app = App(name=name, source_ref="/src", source_dir="/src", status=status)
        store.save(sess, app)
        return app.id


def status_of(app_id: str) -> str:
    with store.session() as sess:
        return store.get_app(sess, app_id).status


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_off_by_default():
    assert config.settings().idle_timeout == 0
    assert not config.settings().idle_enabled


def test_idle_requires_a_router(monkeypatch):
    """A slept app with no proxy in front of it is just a down app."""
    monkeypatch.setenv("HANGAR_IDLE_TIMEOUT", "300")
    monkeypatch.setenv("HANGAR_ROUTER", "none")
    with pytest.raises(ValueError, match="wake them"):
        config.settings().validate()


def test_check_interval_must_be_finer_than_the_timeout(monkeypatch):
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_IDLE_TIMEOUT", "30")
    monkeypatch.setenv("HANGAR_IDLE_CHECK_INTERVAL", "60")
    with pytest.raises(ValueError, match="must be longer"):
        config.settings().validate()


def test_idle_makes_the_proxy_hook_requests(monkeypatch):
    """Even with app auth off — the hook is how a request wakes an app."""
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_IDLE_TIMEOUT", "300")
    monkeypatch.setenv("HANGAR_APP_AUTH", "0")
    assert config.settings().hooks_requests


# --------------------------------------------------------------------------
# The tracker
# --------------------------------------------------------------------------


def test_unseen_app_has_no_idle_time(tracker):
    assert tracker.idle_for("nope") is None


def test_idle_time_counts_from_the_last_touch(tracker):
    tracker.touch("a", now=1000.0)
    assert tracker.idle_for("a", now=1075.0) == 75.0
    tracker.touch("a", now=1060.0)
    assert tracker.idle_for("a", now=1075.0) == 15.0


# --------------------------------------------------------------------------
# Sleeping
# --------------------------------------------------------------------------


def test_sleeps_an_app_past_its_timeout(db, fake_backend, idle_on, tracker):
    app_id = make_app()
    tracker.touch(app_id, now=0.0)

    assert idle.sleep_idle_apps(tracker=tracker, now=301.0) == [app_id]
    assert status_of(app_id) == AppStatus.SLEEPING
    assert ("stop", app_id) in fake_backend.calls


def test_leaves_a_recently_used_app_alone(db, fake_backend, idle_on, tracker):
    app_id = make_app()
    tracker.touch(app_id, now=0.0)

    assert idle.sleep_idle_apps(tracker=tracker, now=299.0) == []
    assert status_of(app_id) == AppStatus.RUNNING
    assert "stop" not in fake_backend.methods()


def test_first_sighting_starts_the_clock_rather_than_sleeping(
    db, fake_backend, idle_on, tracker
):
    """A control-plane restart forgets last-seen times; it must not sleep the box."""
    app_id = make_app()

    assert idle.sleep_idle_apps(tracker=tracker, now=10_000.0) == []
    assert status_of(app_id) == AppStatus.RUNNING
    # ...but the clock is now running, so it sleeps a timeout later.
    assert idle.sleep_idle_apps(tracker=tracker, now=10_301.0) == [app_id]


def test_never_sleeps_a_deliberately_stopped_app(db, fake_backend, idle_on, tracker):
    app_id = make_app(status=AppStatus.STOPPED)
    tracker.touch(app_id, now=0.0)

    assert idle.sleep_idle_apps(tracker=tracker, now=99_999.0) == []
    assert status_of(app_id) == AppStatus.STOPPED


def test_never_sleeps_a_failed_app(db, fake_backend, idle_on, tracker):
    app_id = make_app(status=AppStatus.FAILED)
    tracker.touch(app_id, now=0.0)

    assert idle.sleep_idle_apps(tracker=tracker, now=99_999.0) == []
    assert status_of(app_id) == AppStatus.FAILED


def test_does_nothing_when_disabled(db, fake_backend, tracker):
    app_id = make_app()
    tracker.touch(app_id, now=0.0)

    assert idle.sleep_idle_apps(tracker=tracker, now=99_999.0) == []
    assert status_of(app_id) == AppStatus.RUNNING


def test_a_failed_stop_leaves_the_app_running(db, fake_backend, idle_on, tracker):
    """Reporting an app as sleeping when its container is still up would be a lie."""
    app_id = make_app()
    tracker.touch(app_id, now=0.0)
    fake_backend.errors["stop"] = "docker is unhappy"

    assert idle.sleep_idle_apps(tracker=tracker, now=301.0) == []
    assert status_of(app_id) == AppStatus.RUNNING


def test_sleeping_forgets_the_last_seen_time(db, fake_backend, idle_on, tracker):
    """A stale entry would make the next pass re-sleep an app it just slept."""
    app_id = make_app()
    tracker.touch(app_id, now=0.0)
    idle.sleep_idle_apps(tracker=tracker, now=301.0)

    assert tracker.idle_for(app_id) is None


# --------------------------------------------------------------------------
# Waking
# --------------------------------------------------------------------------


def test_wake_starts_the_container_without_rebuilding(
    db, fake_backend, idle_on, tracker
):
    app_id = make_app(status=AppStatus.SLEEPING)

    assert idle.wake(app_id, tracker=tracker) is True
    assert status_of(app_id) == AppStatus.RUNNING
    # `start`, not `run`: no rebuild, no new container, volumes intact.
    assert fake_backend.methods() == ["start"]


def test_wake_is_a_no_op_on_a_running_app(db, fake_backend, idle_on, tracker):
    app_id = make_app(status=AppStatus.RUNNING)

    assert idle.wake(app_id, tracker=tracker) is False
    assert fake_backend.methods() == []


def test_wake_refuses_a_deliberately_stopped_app(db, fake_backend, idle_on, tracker):
    """Someone pressed stop. A visitor must not undo that."""
    app_id = make_app(status=AppStatus.STOPPED)

    assert idle.wake(app_id, tracker=tracker) is False
    assert status_of(app_id) == AppStatus.STOPPED


def test_a_failed_wake_leaves_the_app_sleeping(db, fake_backend, idle_on, tracker):
    app_id = make_app(status=AppStatus.SLEEPING)
    fake_backend.errors["start"] = "no such container"

    assert idle.wake(app_id, tracker=tracker) is False
    assert status_of(app_id) == AppStatus.SLEEPING


def test_waking_marks_the_app_as_used(db, fake_backend, idle_on, tracker):
    app_id = make_app(status=AppStatus.SLEEPING)
    idle.wake(app_id, tracker=tracker)

    assert tracker.idle_for(app_id) is not None


# --------------------------------------------------------------------------
# The reaper thread
# --------------------------------------------------------------------------


def test_reaper_does_not_start_when_disabled(db):
    reaper = idle.Reaper()
    assert reaper.start() is False
    assert not reaper.running


def test_reaper_starts_and_stops(db, idle_on):
    reaper = idle.Reaper()
    assert reaper.start() is True
    assert reaper.running
    reaper.stop()
    assert not reaper.running


# --------------------------------------------------------------------------
# The proxy hook — where a real request meets a sleeping app
# --------------------------------------------------------------------------


@pytest.fixture
def hooked(client, fake_backend, idle_on):
    """A client whose /internal/authorize is the wake path, with auth off."""
    return client


def authorize(client, host: str):
    return client.get("/internal/authorize", headers={"X-Forwarded-Host": host})


def test_a_request_wakes_a_sleeping_app(db, hooked, fake_backend):
    app_id = make_app("notes", status=AppStatus.SLEEPING)

    assert authorize(hooked, "notes.apps.test").status_code == 200
    assert status_of(app_id) == AppStatus.RUNNING
    assert ("start", app_id) in fake_backend.calls


def test_a_request_to_a_running_app_starts_nothing(db, hooked, fake_backend):
    make_app("notes", status=AppStatus.RUNNING)

    assert authorize(hooked, "notes.apps.test").status_code == 200
    assert "start" not in fake_backend.methods()


def test_a_request_records_activity(db, hooked):
    app_id = make_app("notes")
    authorize(hooked, "notes.apps.test")

    assert idle.TRACKER.idle_for(app_id) is not None


def test_an_unknown_hostname_starts_nothing(db, hooked, fake_backend):
    make_app("notes", status=AppStatus.SLEEPING)

    assert authorize(hooked, "nope.apps.test").status_code == 404
    assert fake_backend.methods() == []


def test_an_unauthorised_visitor_cannot_wake_an_app(
    db, client, fake_backend, idle_on, monkeypatch
):
    """Otherwise a stranger walking hostnames can start every app on the box."""
    monkeypatch.setenv("HANGAR_APP_AUTH", "1")
    monkeypatch.setenv("HANGAR_COOKIE_DOMAIN", ".apps.test")
    app_id = make_app("notes", status=AppStatus.SLEEPING)

    assert authorize(client, "notes.apps.test").status_code == 401
    assert status_of(app_id) == AppStatus.SLEEPING
    assert fake_backend.methods() == []


# --------------------------------------------------------------------------
# POST /apps/{id}/wake
# --------------------------------------------------------------------------


def test_wake_endpoint_starts_a_sleeping_app(db, client, fake_backend, idle_on):
    app_id = make_app(status=AppStatus.SLEEPING)

    response = client.post(f"/apps/{app_id}/wake")
    assert response.status_code == 200
    assert response.json()["status"] == "running"


def test_wake_endpoint_refuses_an_app_that_is_not_asleep(
    db, client, fake_backend, idle_on
):
    app_id = make_app(status=AppStatus.RUNNING)

    response = client.post(f"/apps/{app_id}/wake")
    assert response.status_code == 409
    assert "not sleeping" in response.json()["detail"]


# --------------------------------------------------------------------------
# POST /apps/{id}/sleep
# --------------------------------------------------------------------------


def test_sleep_endpoint_stops_the_app(db, client, fake_backend, idle_on):
    app_id = make_app(status=AppStatus.RUNNING)

    response = client.post(f"/apps/{app_id}/sleep")
    assert response.status_code == 200
    assert response.json()["status"] == "sleeping"
    assert ("stop", app_id) in fake_backend.calls


def test_sleeping_keeps_the_url_and_the_route(db, client, fake_backend, idle_on):
    """The difference from /stop: the link a colleague has still works."""
    app_id = make_app(status=AppStatus.RUNNING)
    with store.session() as sess:
        app = store.get_app(sess, app_id)
        app.url = "http://notes.apps.test"
        store.save(sess, app)

    assert client.post(f"/apps/{app_id}/sleep").json()["url"] == "http://notes.apps.test"


def test_sleep_endpoint_refuses_when_scale_to_zero_is_off(db, client, fake_backend):
    """Nothing would wake it again, so this would be an outage with a nice name."""
    app_id = make_app(status=AppStatus.RUNNING)

    response = client.post(f"/apps/{app_id}/sleep")
    assert response.status_code == 409
    assert "scale-to-zero is off" in response.json()["detail"]
    assert fake_backend.methods() == []


def test_sleep_endpoint_refuses_an_app_that_is_not_running(
    db, client, fake_backend, idle_on
):
    app_id = make_app(status=AppStatus.STOPPED)

    assert client.post(f"/apps/{app_id}/sleep").status_code == 409
