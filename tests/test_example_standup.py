"""The standup example — the one that shows the permission model in a UI.

Worth testing rather than eyeballing, because its whole point is that the role
Hangar attaches decides what a visitor can do. If that ever silently stopped
working, the app would still render, still look fine, and quietly let a viewer
post — which is exactly the failure the demo claims cannot happen.

The app is run in-process here; the platform is not involved, so the identity
headers are set by hand. That is legitimate precisely because it is what the
proxy does, and it is also why the app must never be reachable directly.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "standup"

OWNER = {"X-Hangar-User": "lead@example.com", "X-Hangar-Role": "owner"}
EDITOR = {"X-Hangar-User": "dev@example.com", "X-Hangar-Role": "editor"}
VIEWER = {"X-Hangar-User": "watcher@example.com", "X-Hangar-Role": "viewer"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'standup.db'}")
    monkeypatch.syspath_prepend(str(EXAMPLE))
    sys.modules.pop("main", None)
    import main

    # A context manager, so the startup handler that creates the schema runs.
    with TestClient(main.app) as c:
        yield c


def post(client, body: str, headers: dict):
    return client.post("/posts", data={"body": body}, headers=headers)


# --------------------------------------------------------------------------


def test_the_page_renders_for_anyone_who_reaches_it(client):
    assert client.get("/", headers=VIEWER).status_code == 200


def test_it_greets_the_visitor_by_the_header(client):
    """No user table — the name comes from the platform."""
    assert "watcher@example.com" in client.get("/", headers=VIEWER).text


@pytest.mark.parametrize("headers", [OWNER, EDITOR])
def test_owners_and_editors_get_a_compose_box(client, headers):
    assert "Post update" in client.get("/", headers=headers).text


def test_viewers_get_a_read_only_page_instead(client):
    body = client.get("/", headers=VIEWER).text
    assert "Read-only" in body
    assert "Post update" not in body


def test_a_viewer_cannot_post_even_by_asking_directly(client):
    """Hiding the form is not an access control; the check is server-side."""
    post(client, "should not appear", VIEWER)
    assert "should not appear" not in client.get("/", headers=OWNER).text


def test_an_editor_can_post_and_everyone_can_read_it(client):
    post(client, "Fixed the deploy pipeline.", EDITOR)

    for headers in (OWNER, EDITOR, VIEWER):
        page = client.get("/", headers=headers).text
        assert "Fixed the deploy pipeline." in page
    assert "dev@example.com" in client.get("/", headers=VIEWER).text


def test_without_an_identity_header_it_fails_closed(client):
    """Reached directly, it has no idea who anyone is, so nobody may write."""
    page = client.get("/").text
    assert "No identity header" in page
    assert "Post update" not in page

    post(client, "anonymous write", {})
    assert "anonymous write" not in client.get("/", headers=OWNER).text


def test_posts_are_escaped(client):
    """It is a platform for running other people's code; be the good example."""
    post(client, "<script>alert(1)</script>", OWNER)
    body = client.get("/", headers=OWNER).text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_an_empty_update_is_ignored(client):
    post(client, "   ", OWNER)
    assert client.get("/", headers=OWNER).text.count('class="update"') == 0


def test_nothing_is_fetched_from_the_internet(client):
    """Apps run with egress denied, so a CDN reference would render unstyled."""
    body = client.get("/", headers=OWNER).text
    assert "http://" not in body
    assert "https://" not in body


def test_detection_recognises_it(client):
    """It has to survive the same pipeline as anything else."""
    from hangar.detect import detect

    detected = detect(EXAMPLE)
    assert detected.runtime == "python"
    assert detected.framework == "fastapi"
