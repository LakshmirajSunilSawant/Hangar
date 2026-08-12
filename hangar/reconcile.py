"""Put the proxy's routing table back the way the database says it should be.

Hangar drives Caddy through its admin API, and Caddy's official image starts
with `caddy run --config /etc/caddy/Caddyfile`. That means **every Caddy
restart discards every route Hangar ever pushed** and replaces them with the
image's default welcome page. Nothing errors. The control plane keeps working,
the containers keep running, the database still lists a URL for each app — and
every one of those URLs quietly serves "Caddy works!" to whoever opens it.

On a single free VM, restarts are not rare: a reboot, a `docker compose
restart`, an out-of-memory kill. So the routing table is treated as a cache of
the database rather than as durable state, and rebuilt whenever the control
plane starts.

This is idempotent by construction — the router's upsert already deletes before
adding — so running it when nothing is wrong costs a few HTTP calls and
changes nothing.
"""

from __future__ import annotations

import logging

from . import config, routing, store
from .routing import RoutingError
from .store import AppStatus

log = logging.getLogger("hangar.reconcile")

# Apps that are meant to be reachable. A stopped app's route was withdrawn
# deliberately, and a failed one never had a working upstream.
ROUTABLE = frozenset({AppStatus.RUNNING.value, AppStatus.SLEEPING.value})

# The route id for the control plane's own hostname. Namespaced like an app's,
# so it is equally safe to re-publish and equally easy to spot in Caddy.
CONTROL_PLANE_ROUTE = "control-plane"


def reconcile(router: routing.Router | None = None) -> int:
    """Re-publish every route that should exist. Returns how many were written.

    Best-effort: one unroutable app must not stop the others from coming back,
    and no failure here should stop the control plane from starting.
    """
    settings = config.settings()
    if settings.router == "none":
        return 0

    router = router or routing.get_router()
    written = 0

    if settings.control_plane_host:
        try:
            router.upsert_host(
                route_id=CONTROL_PLANE_ROUTE,
                hostname=settings.control_plane_host,
                upstream=settings.control_plane_address,
            )
            written += 1
            log.info(
                "published the dashboard at %s", settings.control_plane_host
            )
        except RoutingError as exc:
            log.warning("could not publish the dashboard route: %s", exc)

    with store.session() as sess:
        apps = [
            (app.id, app.name, app.upstream, app.host_port)
            for app in store.list_apps(sess)
            if app.status in ROUTABLE and app.upstream
        ]

    for app_id, name, upstream, host_port in apps:
        try:
            router.upsert(
                app_id=app_id,
                app_name=name,
                upstream=upstream,
                host_port=host_port,
            )
            written += 1
        except RoutingError as exc:
            log.warning("could not republish the route for %s: %s", name, exc)

    if written:
        log.info("reconciled %s route(s) with the proxy", written)
    return written
