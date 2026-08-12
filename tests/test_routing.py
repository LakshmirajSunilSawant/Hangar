"""Routing tests that need no Caddy.

Route construction and hostname derivation are pure; the admin-API calls are
checked against a stub session. tests/test_routing_caddy.py drives a real
Caddy, because a stub can only prove we send what we think we send.
"""

import json

import pytest
import requests

from hangar import config, routing
from hangar.routing import CaddyRouter, NullRouter, RoutingError, route, route_id


class StubResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class StubSession:
    """Records requests and replays queued responses."""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, str, object]] = []
        self.responses = responses or {}
        self.default = StubResponse(200, {})

    def request(self, method, url, json=None, timeout=None):
        path = url.split("2019", 1)[-1] if "2019" in url else url
        self.calls.append((method, path, json))
        for (m, p), response in self.responses.items():
            if m == method and p == path:
                return response
        return self.default

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method is None or m == method]

    def payload_for(self, method, path):
        for m, p, body in self.calls:
            if m == method and p == path:
                return body
        return None


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_APP_DOMAIN", "apps.example.com")
    monkeypatch.setenv("HANGAR_CADDY_ADMIN_URL", "http://localhost:2019")


# --------------------------------------------------------------------------
# Hostnames and URLs
# --------------------------------------------------------------------------


def test_hostname_is_app_name_under_the_domain(routed):
    assert config.settings().hostname_for_app("sales-tool") == "sales-tool.apps.example.com"


def test_url_uses_the_configured_scheme(routed, monkeypatch):
    assert config.settings().url_for_app("sales-tool") == "https://sales-tool.apps.example.com"
    monkeypatch.setenv("HANGAR_APP_SCHEME", "http")
    assert config.settings().url_for_app("sales-tool") == "http://sales-tool.apps.example.com"


def test_trailing_dots_in_the_domain_are_tolerated(monkeypatch):
    monkeypatch.setenv("HANGAR_APP_DOMAIN", ".apps.example.com.")
    assert config.settings().hostname_for_app("x") == "x.apps.example.com"


def test_url_requires_a_domain(monkeypatch):
    monkeypatch.delenv("HANGAR_APP_DOMAIN", raising=False)
    with pytest.raises(ValueError, match="HANGAR_APP_DOMAIN"):
        config.settings().url_for_app("x")


# --------------------------------------------------------------------------
# Route JSON
# --------------------------------------------------------------------------


def test_route_matches_host_and_proxies_to_the_port():
    r = route("abc123", "sales.example.com", "127.0.0.1:64800")

    assert r["@id"] == "hangar-abc123"
    assert r["match"] == [{"host": ["sales.example.com"]}]
    assert r["handle"][0]["handler"] == "reverse_proxy"
    assert r["handle"][0]["upstreams"] == [{"dial": "127.0.0.1:64800"}]


def test_route_is_terminal():
    """Without this, a later app's route could also handle the same request."""
    assert route("abc", "h", "127.0.0.1:1")["terminal"] is True


def test_route_ids_are_namespaced_so_other_caddy_routes_are_untouched():
    assert route_id("abc").startswith("hangar-")


def test_upstream_host_is_configurable(monkeypatch):
    """Caddy in a container can't reach app containers over loopback."""
    r = route("abc", "h", "host.docker.internal:3000")
    assert r["handle"][0]["upstreams"] == [{"dial": "host.docker.internal:3000"}]


# -- waking a sleeping app -------------------------------------------------


def test_no_retry_window_when_scale_to_zero_is_off():
    """An always-on app that refuses a connection is broken, not waking."""
    assert "load_balancing" not in route("abc", "h", "127.0.0.1:1")["handle"][0]


def test_wake_timeout_makes_caddy_retry_the_upstream():
    """A just-started container hasn't bound its port yet; a 502 here is a bug."""
    proxy = route("abc", "h", "127.0.0.1:1", wake_timeout=30)["handle"][0]
    assert proxy["load_balancing"]["try_duration"] == "30s"


def test_the_retry_window_is_on_the_app_not_the_auth_hook():
    """Retrying the control plane would just make a real 401 arrive slowly."""
    handlers = route("abc", "h", "127.0.0.1:1", control_plane="cp:8080", wake_timeout=30)[
        "handle"
    ]
    forward_auth, app_proxy = handlers
    assert "load_balancing" not in forward_auth
    assert app_proxy["load_balancing"]["try_duration"] == "30s"


def test_identity_headers_are_not_injected_when_app_auth_is_off():
    """The hook still runs — for waking — but there is no identity to copy.

    Copying them anyway sets them empty, and an app reading X-Hangar-User would
    see a blank string rather than nothing at all.
    """
    forward_auth = route(
        "abc", "h", "127.0.0.1:1", control_plane="cp:8080", inject_identity=False
    )["handle"][0]

    assert forward_auth["handle_response"][0]["routes"] == []
    assert "X-Hangar-User" not in json.dumps(forward_auth)


# --------------------------------------------------------------------------
# NullRouter — the default
# --------------------------------------------------------------------------


def test_null_router_returns_the_direct_port_url():
    url = NullRouter().upsert(app_id="a", app_name="app", upstream="127.0.0.1:8000", host_port=8000)
    assert url == "http://localhost:8000"


def test_null_router_remove_is_a_no_op():
    assert NullRouter().remove("anything") is None


def test_default_router_is_none():
    assert routing.get_router().name == "none"


def test_unknown_router_names_the_known_ones(monkeypatch):
    monkeypatch.setenv("HANGAR_ROUTER", "nginx")
    with pytest.raises(RoutingError, match="unknown router"):
        routing.get_router()


# --------------------------------------------------------------------------
# CaddyRouter against a stub
# --------------------------------------------------------------------------


def test_upsert_bootstraps_an_empty_caddy(routed):
    session = StubSession({("GET", "/config/"): StubResponse(200, {})})
    router = CaddyRouter(session=session)

    url = router.upsert(app_id="abc", app_name="sales", upstream="127.0.0.1:64800", host_port=64800)

    assert url == "https://sales.apps.example.com"
    assert "/load" in session.paths("POST")
    listen = session.payload_for("POST", "/load")["apps"]["http"]["servers"]["srv0"]
    assert listen["listen"] == [":80"]


def test_upsert_refuses_to_overwrite_an_existing_caddy_config(routed):
    """Silently replacing a working Caddy would be worse than failing."""
    session = StubSession({
        ("GET", "/config/"): StubResponse(
            200, {"apps": {"http": {"servers": {"other": {}}}}}
        )
    })
    with pytest.raises(RoutingError, match="HANGAR_CADDY_SERVER"):
        CaddyRouter(session=session).upsert(
            app_id="abc", app_name="sales", upstream="127.0.0.1:1", host_port=1
        )

    assert "/load" not in session.paths("POST")


def test_upsert_reuses_an_existing_hangar_server(routed):
    session = StubSession({
        ("GET", "/config/"): StubResponse(
            200, {"apps": {"http": {"servers": {"srv0": {"routes": []}}}}}
        )
    })
    CaddyRouter(session=session).upsert(app_id="abc", app_name="sales", upstream="127.0.0.1:8000", host_port=8000)

    assert "/load" not in session.paths("POST")
    assert "/config/apps/http/servers/srv0/routes/0" in session.paths("PUT")


def test_route_is_inserted_at_the_front_not_appended(routed):
    """Appending puts us behind any catch-all, which matches everything first.

    Caddy's own default Caddyfile installs exactly such a catch-all, so an
    appended route is silently never reached.
    """
    session = StubSession({
        ("GET", "/config/"): StubResponse(
            200, {"apps": {"http": {"servers": {"srv0": {"routes": []}}}}}
        )
    })
    CaddyRouter(session=session).upsert(app_id="abc", app_name="sales", upstream="127.0.0.1:8000", host_port=8000)

    # PUT at index 0 inserts; POST to the array would append.
    assert session.paths("PUT") == ["/config/apps/http/servers/srv0/routes/0"]
    assert "/config/apps/http/servers/srv0/routes" not in session.paths("POST")


def test_upsert_deletes_before_adding_so_redeploys_dont_duplicate(routed):
    session = StubSession({
        ("GET", "/config/"): StubResponse(
            200, {"apps": {"http": {"servers": {"srv0": {"routes": []}}}}}
        ),
        ("DELETE", "/id/hangar-abc"): StubResponse(500, text="unknown object ID"),
    })
    CaddyRouter(session=session).upsert(app_id="abc", app_name="sales", upstream="127.0.0.1:8000", host_port=8000)

    methods = [m for m, _, _ in session.calls]
    assert methods.index("DELETE") < methods.index("PUT")


def test_remove_tolerates_a_route_that_was_never_added(routed):
    """Caddy answers 500 'unknown object ID', not 404, for a missing route."""
    session = StubSession({
        ("DELETE", "/id/hangar-abc"): StubResponse(500, text="unknown object ID")
    })
    CaddyRouter(session=session).remove("abc", missing_ok=True)


def test_remove_raises_on_a_real_failure(routed):
    session = StubSession({
        ("DELETE", "/id/hangar-abc"): StubResponse(500, text="disk on fire")
    })
    with pytest.raises(RoutingError, match="disk on fire"):
        CaddyRouter(session=session).remove("abc")


def test_unreachable_caddy_is_reported_clearly(routed):
    class DeadSession:
        def request(self, *a, **kw):
            raise requests.ConnectionError("connection refused")

    router = CaddyRouter(session=DeadSession())
    assert router.available() is False
    with pytest.raises(RoutingError, match="could not reach Caddy"):
        router.upsert(app_id="a", app_name="b", upstream="127.0.0.1:1", host_port=1)


def test_routing_without_a_domain_fails_as_a_routing_error(monkeypatch):
    """Misconfiguration should fail the deploy, not crash the worker thread."""
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.delenv("HANGAR_APP_DOMAIN", raising=False)

    with pytest.raises(RoutingError, match="HANGAR_APP_DOMAIN"):
        CaddyRouter(session=StubSession()).upsert(
            app_id="a", app_name="b", upstream="127.0.0.1:1", host_port=1
        )
