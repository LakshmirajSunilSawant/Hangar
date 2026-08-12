"""Scale-to-zero: stop apps nobody is using, start them again on demand.

A free VM has 12GB of RAM and the PRD expects a team to have more small tools
than they use at once. Most internal tools are opened a few times a day; paying
for their memory the rest of the time is the whole reason a platform like this
runs out of room.

The trick that makes this cheap is that the proxy already asks the control
plane about every request before the app sees it (routing.forward_auth_handler
-> /internal/authorize). That hook is a free activity signal *and* a place to
stand: a request for a sleeping app can start the container before it is
proxied on, and the visitor sees a slow page rather than an error.

Two halves:

* A reaper thread stops containers with no traffic for HANGAR_IDLE_TIMEOUT.
  It stops rather than removes, so the image, volumes, network attachment and
  gVisor runtime all survive and waking is a start, not a rebuild.
* The authorize endpoint wakes them, then returns. It does not wait for the app
  to bind its port — Caddy is configured to keep retrying the upstream for
  HANGAR_WAKE_TIMEOUT, which is the right place for that wait because Caddy is
  the only component on the app's network.

Last-seen times are deliberately in memory and not in the database: this is one
write per request otherwise, on a machine chosen for being free. The cost is
that a control-plane restart forgets them, which is handled by treating an
app's first sighting as activity — a restart wakes nothing and sleeps nothing
until the timeout has genuinely elapsed.
"""

from __future__ import annotations

import logging
import threading
import time

from . import backends, config, store
from .backends import BackendError
from .store import AppStatus

log = logging.getLogger("hangar.idle")

# Statuses the reaper is allowed to put to sleep. A STOPPED app was stopped by
# a person and must stay that way; FAILED has nothing running to stop.
SLEEPABLE = frozenset({AppStatus.RUNNING.value})


class Tracker:
    """Last-seen times for apps, by app id. Thread-safe and in-memory."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def touch(self, app_id: str, *, now: float | None = None) -> None:
        with self._lock:
            self._seen[app_id] = now if now is not None else time.monotonic()

    def seen_at(self, app_id: str) -> float | None:
        with self._lock:
            return self._seen.get(app_id)

    def idle_for(self, app_id: str, *, now: float | None = None) -> float | None:
        """Seconds since the last request, or None if we've never seen one.

        None is the "just restarted" case, and callers treat it as activity
        rather than as infinite idleness.
        """
        seen = self.seen_at(app_id)
        if seen is None:
            return None
        return (now if now is not None else time.monotonic()) - seen

    def forget(self, app_id: str) -> None:
        with self._lock:
            self._seen.pop(app_id, None)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


TRACKER = Tracker()


# --------------------------------------------------------------------------
# Sleeping and waking one app
# --------------------------------------------------------------------------


def wake(app_id: str, *, tracker: Tracker | None = None) -> bool:
    """Start a sleeping app. Returns whether it actually had to be started.

    Idempotent under concurrency in the sense that matters: two simultaneous
    requests may both call Docker's start, and the second is a no-op on an
    already-running container.
    """
    tracker = tracker or TRACKER
    with store.session() as sess:
        app = store.get_app(sess, app_id)
        if app is None or app.status != AppStatus.SLEEPING.value:
            return False

        started = time.monotonic()
        try:
            backends.get_backend().start(app_id)
        except BackendError as exc:
            # Leave the app marked sleeping: the next request tries again, and
            # meanwhile the dashboard shows something truthful.
            log.warning("could not wake %s (%s): %s", app.name, app_id, exc)
            return False

        app.status = AppStatus.RUNNING
        store.save(sess, app)
        tracker.touch(app_id)
        log.info(
            "woke %s (%s) in %.2fs", app.name, app_id, time.monotonic() - started
        )
        return True


def put_to_sleep(app_id: str, *, tracker: Tracker | None = None) -> bool:
    """Stop a running app's container, keeping everything else intact."""
    tracker = tracker or TRACKER
    with store.session() as sess:
        app = store.get_app(sess, app_id)
        if app is None or app.status not in SLEEPABLE:
            return False

        try:
            backends.get_backend().stop(app_id)
        except BackendError as exc:
            log.warning("could not sleep %s (%s): %s", app.name, app_id, exc)
            return False

        app.status = AppStatus.SLEEPING
        store.save(sess, app)
        # Forget rather than touch: an app with no last-seen time is treated as
        # active on the next pass, which would immediately re-sleep it in a
        # loop if we left a stale entry behind.
        tracker.forget(app_id)
        log.info("slept %s (%s)", app.name, app_id)
        return True


# --------------------------------------------------------------------------
# The reaper
# --------------------------------------------------------------------------


def sleep_idle_apps(
    *, tracker: Tracker | None = None, now: float | None = None
) -> list[str]:
    """One pass: sleep every running app past its idle timeout.

    Returns the ids it slept. Pure enough to test without threads or Docker.
    """
    tracker = tracker or TRACKER
    settings = config.settings()
    if not settings.idle_enabled:
        return []

    now = now if now is not None else time.monotonic()
    with store.session() as sess:
        candidates = [
            app.id for app in store.list_apps(sess) if app.status in SLEEPABLE
        ]

    slept: list[str] = []
    for app_id in candidates:
        idle = tracker.idle_for(app_id, now=now)
        if idle is None:
            # First sighting — most likely the control plane restarted under a
            # running app. Start its clock now instead of sleeping it blind.
            tracker.touch(app_id, now=now)
            continue
        if idle >= settings.idle_timeout and put_to_sleep(app_id, tracker=tracker):
            slept.append(app_id)
    return slept


class Reaper:
    """Background thread running `sleep_idle_apps` on an interval."""

    def __init__(self, tracker: Tracker | None = None) -> None:
        self.tracker = tracker or TRACKER
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        settings = config.settings()
        if not settings.idle_enabled or self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="hangar-idle-reaper", daemon=True
        )
        self._thread.start()
        log.info(
            "idle reaper started: sleeping apps after %ss, checking every %ss",
            settings.idle_timeout,
            settings.idle_check_interval,
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        interval = config.settings().idle_check_interval
        # wait() rather than sleep(): shutdown is immediate instead of waiting
        # out a whole interval.
        while not self._stop.wait(interval):
            try:
                sleep_idle_apps(tracker=self.tracker)
            except Exception:  # noqa: BLE001 - a bad pass must not kill the thread
                log.exception("idle reaper pass failed")


REAPER = Reaper()
