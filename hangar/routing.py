"""Public routing — turning a container on a random port into a shareable link.

Without this, a deployed app is reachable at `http://localhost:64800`, which
works on exactly one machine. With it, every app gets a stable hostname behind
a single entry point, and the port becomes an internal detail that can change
across restarts without breaking anyone's bookmark.

Caddy is the PRD's choice (§6) because it obtains and renews Let's Encrypt
certificates by itself. This talks to its admin API rather than writing
Caddyfiles, so there is no file to own, no reload to sequence, and no risk of
a half-written config being read.

Every route Hangar creates carries an `@id` of `hangar-<app_id>`. That makes
add/update/remove idempotent, and means Hangar only ever touches its own
routes — a Caddy already serving other sites keeps serving them.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import requests

from . import config

log = logging.getLogger("hangar.routing")

ROUTE_ID_PREFIX = "hangar-"
TIMEOUT = 10


class RoutingError(Exception):
    """The router could not be reached or refused a change."""


class Router(ABC):
    """Maps app names to running containers."""

    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def upsert(self, *, app_id: str, app_name: str, host_port: int) -> str:
        """Route ``app_name`` to ``host_port``. Returns the app's public URL."""

    @abstractmethod
    def remove(self, app_id: str, *, missing_ok: bool = True) -> None:
        ...


class NullRouter(Router):
    """No routing — apps are reached directly on their published port.

    The default, because a laptop has no DNS and no certificates, and adding a
    proxy there would buy nothing.
    """

    name = "none"

    def available(self) -> bool:
        return True

    def upsert(self, *, app_id: str, app_name: str, host_port: int) -> str:
        return config.settings().url_for_port(host_port)

    def remove(self, app_id: str, *, missing_ok: bool = True) -> None:
        return None


class CaddyRouter(Router):
    """Drives Caddy through its admin API."""

    name = "caddy"

    def __init__(self, settings=None, session: requests.Session | None = None):
        self.settings = settings or config.settings()
        self.admin = self.settings.caddy_admin_url.rstrip("/")
        self.server = self.settings.caddy_server
        self.session = session or requests.Session()

    # -- public API ------------------------------------------------------

    def available(self) -> bool:
        try:
            return self._get("/config/") is not None
        except RoutingError:
            return False

    def upsert(self, *, app_id: str, app_name: str, host_port: int) -> str:
        try:
            hostname = self.settings.hostname_for_app(app_name)
        except ValueError as exc:
            # Misconfiguration, not a bug — surface it as a routing failure so
            # the deploy reports it against the app instead of crashing.
            raise RoutingError(str(exc)) from exc

        self._ensure_server()

        # Delete-then-add rather than PATCH: a route may not exist yet, and
        # this keeps the two cases on one path.
        self.remove(app_id, missing_ok=True)

        # PUT at index 0 *inserts* at the front. Appending would put us behind
        # any catch-all already in the server — including the one Caddy's
        # default Caddyfile installs — and a catch-all matches everything, so
        # our host-matched route would never be reached.
        self._request(
            "PUT",
            f"/config/apps/http/servers/{self.server}/routes/0",
            json=route(app_id, hostname, self.settings.upstream_host, host_port),
            expect_ok=True,
        )
        log.info("routed %s -> %s:%s", hostname, self.settings.upstream_host, host_port)
        return self.settings.url_for_app(app_name)

    def remove(self, app_id: str, *, missing_ok: bool = True) -> None:
        response = self._request("DELETE", f"/id/{route_id(app_id)}")
        if response.status_code < 400:
            return
        # Caddy answers 500 with "unknown object ID" for a route that isn't
        # there, so absence can't be detected by status code alone.
        if missing_ok and _is_unknown_id(response):
            return
        raise RoutingError(
            f"could not remove route for {app_id}: "
            f"{response.status_code} {response.text.strip()[:200]}"
        )

    # -- Caddy plumbing --------------------------------------------------

    def _ensure_server(self) -> None:
        """Make sure an HTTP server exists for our routes to live in.

        Bootstraps only a Caddy with no configuration at all. If Caddy is
        already serving something, we refuse to overwrite it — silently
        replacing a working config would be far worse than an error.
        """
        current = self._get("/config/")
        if current and current.get("apps", {}).get("http", {}).get(
            "servers", {}
        ).get(self.server) is not None:
            return

        if current and current.get("apps"):
            raise RoutingError(
                f"Caddy has configuration but no server named {self.server!r}. "
                "Set HANGAR_CADDY_SERVER to the existing server's name rather "
                "than letting Hangar replace a running configuration."
            )

        self._request(
            "POST",
            "/load",
            json={
                "apps": {
                    "http": {
                        "servers": {
                            self.server: {
                                "listen": [self.settings.caddy_listen],
                                "routes": [],
                            }
                        }
                    }
                }
            },
            expect_ok=True,
        )
        log.info("bootstrapped Caddy server %s", self.server)

    def _get(self, path: str) -> Any:
        response = self._request("GET", path)
        if response.status_code >= 400:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _request(
        self, method: str, path: str, *, json: Any = None, expect_ok: bool = False
    ) -> requests.Response:
        url = f"{self.admin}{path}"
        try:
            response = self.session.request(method, url, json=json, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise RoutingError(f"could not reach Caddy admin API at {url}: {exc}") from exc

        if expect_ok and response.status_code >= 400:
            raise RoutingError(
                f"Caddy rejected {method} {path}: "
                f"{response.status_code} {response.text.strip()[:200]}"
            )
        return response


# --------------------------------------------------------------------------
# Route construction — pure, so it can be tested without a Caddy
# --------------------------------------------------------------------------


def route_id(app_id: str) -> str:
    return f"{ROUTE_ID_PREFIX}{app_id}"


def route(app_id: str, hostname: str, upstream_host: str, host_port: int) -> dict:
    """A Caddy route sending one hostname to one upstream."""
    return {
        "@id": route_id(app_id),
        "match": [{"host": [hostname]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": f"{upstream_host}:{host_port}"}],
            }
        ],
        # Without this, Caddy keeps evaluating later routes after this one
        # matches, and a second app's route could also handle the request.
        "terminal": True,
    }


def _is_unknown_id(response: requests.Response) -> bool:
    return "unknown object" in response.text.lower()


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

_ROUTERS: dict[str, type[Router]] = {"none": NullRouter, "caddy": CaddyRouter}


def get_router(name: str | None = None) -> Router:
    settings = config.settings()
    name = name or settings.router
    if name not in _ROUTERS:
        known = ", ".join(sorted(_ROUTERS))
        raise RoutingError(f"unknown router {name!r} (known: {known})")
    return _ROUTERS[name]()
