"""Forward-auth: recipients authenticate to the platform, not to the app.

PRD §8's distinctive requirement. A stub proves Hangar emits the JSON it thinks
it emits; only a real Caddy proves the request is actually stopped before it
reaches the app, and that the app is told who the visitor is.

The app here is `examples/whoami`, which does no authentication of its own and
simply reports the identity headers it received — so if the header injection
were broken, the assertion would show it rather than a green tick.
"""

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hangar import identity, routing, store
from hangar.routing import IDENTITY_HEADERS

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CADDY_IMAGE = "caddy:2-alpine"
APP_DOMAIN = "apps.hangar-test"
PASSWORD = "correct-horse-battery"
TOKEN = "admin-token"

pytestmark = pytest.mark.slow


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(url: str, host: str | None = None, cookie: str | None = None):
    """Returns (status, body, headers). Status 0 means the connection failed."""
    request = urllib.request.Request(url)
    if host:
        request.add_header("Host", host)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace"), dict(exc.headers)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, "", {}


def post_json(url: str, payload: dict):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response, json.loads(response.read().decode() or "{}")


@pytest.fixture(scope="module")
def caddy():
    """A real Caddy that can reach the host, with an empty starting config."""
    try:
        import docker as docker_sdk

        docker = docker_sdk.from_env()
        docker.ping()
    except Exception:
        pytest.skip("Docker daemon not reachable")

    docker.images.pull(CADDY_IMAGE)
    admin_port, http_port = free_port(), free_port()

    container = docker.containers.run(
        CADDY_IMAGE,
        name=f"hangar-fwdauth-caddy-{admin_port}",
        detach=True,
        environment={"CADDY_ADMIN": "0.0.0.0:2019"},
        ports={"2019/tcp": admin_port, "80/tcp": http_port},
        extra_hosts={"host.docker.internal": "host-gateway"},
    )
    admin = f"http://localhost:{admin_port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if http(f"{admin}/config/")[0] == 200:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"Caddy never started: {container.logs()[-2000:]}")

        # Drop the image's default catch-all so it can't answer for us.
        post_json(f"{admin}/load", {"admin": {"listen": "0.0.0.0:2019"}})
        yield admin, http_port
    finally:
        container.remove(force=True)


@pytest.fixture
def control_plane(db, tmp_path, monkeypatch):
    """Run the real control plane on a port Caddy can reach."""
    import threading

    import uvicorn

    monkeypatch.setenv("HANGAR_API_TOKEN", TOKEN)
    monkeypatch.setenv("HANGAR_APP_DOMAIN", APP_DOMAIN)
    monkeypatch.setenv("HANGAR_APP_SCHEME", "http")

    from hangar.api import api

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if http(f"{base}/healthz")[0] == 200:
            break
        time.sleep(0.2)
    else:
        pytest.fail("control plane never started")

    yield base, port

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def gated(caddy, control_plane, monkeypatch):
    """An app behind forward-auth, plus a user who can reach it."""
    admin_url, http_port = caddy
    base, cp_port = control_plane

    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_CADDY_ADMIN_URL", admin_url)
    monkeypatch.setenv("HANGAR_APP_AUTH", "1")
    # Caddy is containerised, so loopback would be its own container.
    monkeypatch.setenv("HANGAR_UPSTREAM_HOST", "host.docker.internal")
    monkeypatch.setenv(
        "HANGAR_CONTROL_PLANE_ADDRESS", f"host.docker.internal:{cp_port}"
    )

    # An app, owned by nobody in particular, and a user granted access.
    with store.session() as sess:
        app = store.App(
            name="gated-app",
            source_type="path",
            source_ref=str(EXAMPLES / "whoami"),
            source_dir=str(EXAMPLES / "whoami"),
        )
        store.save(sess, app)
        app_id = app.id

        _, invite_token = identity.invite(sess, "member@example.com")
        member = identity.accept_invite(sess, invite_token, PASSWORD)
        sess.add(
            store.Permission(app_id=app_id, user_id=member.id, role="viewer")
        )

        _, outsider_invite = identity.invite(sess, "outsider@example.com")
        identity.accept_invite(sess, outsider_invite, PASSWORD)
        sess.commit()

    yield app_id, base, http_port

    routing.get_router().remove(app_id, missing_ok=True)


def login(base: str, email: str) -> str:
    """Sign in against the real control plane and return the cookie header."""
    response, _ = post_json(
        f"{base}/auth/login", {"email": email, "password": PASSWORD}
    )
    raw = response.headers["set-cookie"]
    return raw.split(";", 1)[0]


def publish(app_id: str, upstream: str) -> str:
    return routing.get_router().upsert(
        app_id=app_id, app_name="gated-app", upstream=upstream, host_port=None
    )


# --------------------------------------------------------------------------


def test_route_carries_a_forward_auth_handler(gated):
    """Without HANGAR_APP_AUTH the route is a bare reverse_proxy."""
    app_id, base, _ = gated
    publish(app_id, "host.docker.internal:9")

    config_body = http(
        f"{routing.get_router().admin}/config/"
    )[1]
    assert "/internal/authorize" in config_body
    assert "X-Hangar-User" in config_body


def test_anonymous_requests_are_stopped_before_the_app(gated):
    """The app is not even dialled — the upstream here does not exist."""
    app_id, base, http_port = gated
    publish(app_id, "host.docker.internal:9")  # port 9 discards everything

    status, _, _ = http(f"http://localhost:{http_port}/", host=f"gated-app.{APP_DOMAIN}")
    assert status == 401, "an unauthenticated request was not rejected"


def test_a_user_without_a_grant_is_refused(gated):
    app_id, base, http_port = gated
    publish(app_id, "host.docker.internal:9")

    cookie = login(base, "outsider@example.com")
    status, _, _ = http(
        f"http://localhost:{http_port}/", host=f"gated-app.{APP_DOMAIN}", cookie=cookie
    )
    assert status == 403


def test_a_granted_user_reaches_the_app_with_identity_injected(
    gated, docker_available
):
    """The whole point: the app is told who the visitor is, and did nothing."""
    if not docker_available:
        pytest.skip("Docker daemon not reachable")

    import docker as docker_sdk
    import subprocess

    app_id, base, http_port = gated

    # A tiny upstream that echoes the headers it received.
    docker = docker_sdk.from_env()
    upstream_port = free_port()
    echo = docker.containers.run(
        "python:3.12-slim",
        name=f"hangar-echo-{upstream_port}",
        detach=True,
        ports={"8000/tcp": upstream_port},
        command=[
            "python", "-c",
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "import json\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        body = json.dumps({k: v for k, v in self.headers.items()}).encode()\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Type','application/json')\n"
            "        self.send_header('Content-Length', str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "    def log_message(self, *a): pass\n"
            "HTTPServer(('0.0.0.0',8000), H).serve_forever()\n",
        ],
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if http(f"http://localhost:{upstream_port}/")[0] == 200:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"echo upstream never started: {echo.logs()[-1000:]}")

        publish(app_id, f"host.docker.internal:{upstream_port}")
        cookie = login(base, "member@example.com")

        status, body, _ = http(
            f"http://localhost:{http_port}/",
            host=f"gated-app.{APP_DOMAIN}",
            cookie=cookie,
        )
        assert status == 200, f"granted user was blocked: {body[:300]}"

        received = {k.lower(): v for k, v in json.loads(body).items()}
        assert received["x-hangar-user"] == "member@example.com"
        assert received["x-hangar-role"] == "viewer"
        assert received["x-hangar-user-id"]
    finally:
        echo.remove(force=True)


def test_the_app_never_sees_the_session_cookie_holder_without_a_grant(gated):
    """Revoking access takes effect on the next request, not the next deploy."""
    app_id, base, http_port = gated
    publish(app_id, "host.docker.internal:9")
    cookie = login(base, "member@example.com")

    with store.session() as sess:
        user = store.user_by_email(sess, "member@example.com")
        permission = store.permission_for(sess, app_id, user.id)
        sess.delete(permission)
        sess.commit()

    status, _, _ = http(
        f"http://localhost:{http_port}/", host=f"gated-app.{APP_DOMAIN}", cookie=cookie
    )
    assert status == 403


def test_identity_headers_are_the_ones_routing_declares():
    """routing.py and api.py must agree, or headers arrive empty."""
    from hangar import api as api_mod
    import inspect

    source = inspect.getsource(api_mod.authorize_app_request)
    for header in IDENTITY_HEADERS:
        assert header in source, f"{header} is routed but never set"
