"""owner / editor / viewer, enforced end to end through the API.

These drive real sessions rather than stubbing the principal, because the
interesting failures are in the wiring: a route that forgets its check looks
identical to one that has it, right up until someone deletes an app they only
had read access to.
"""

import pytest

from hangar import deploy as deploy_mod
from hangar import identity, permissions, store
from hangar.permissions import Action
from hangar.store import Role

PASSWORD = "correct-horse-battery"
TOKEN = "admin-token"


@pytest.fixture(autouse=True)
def backend(fake_backend):
    return fake_backend


@pytest.fixture(autouse=True)
def no_real_deploys(monkeypatch):
    monkeypatch.setattr(deploy_mod, "deploy", lambda app_id: None)
    from hangar import api as api_mod

    monkeypatch.setattr(api_mod.deploy_mod, "deploy", lambda app_id: None)


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv("HANGAR_API_TOKEN", TOKEN)
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def source(tmp_path):
    app_dir = tmp_path / "src"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (app_dir / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    return app_dir


@pytest.fixture
def app_id(client, admin, source):
    return client.post(
        "/apps",
        json={"name": "shared-app", "source_path": str(source)},
        headers=admin,
    ).json()["id"]


def make_user(client, admin, email: str) -> str:
    """Invite, accept, and return the user id."""
    invite = client.post("/users", json={"email": email}, headers=admin).json()
    client.post(
        "/auth/accept-invite",
        json={"token": invite["invite_token"], "password": PASSWORD},
    )
    return invite["user"]["id"]


def sign_in(client, email: str) -> None:
    """Log the shared TestClient's cookie jar in as this user."""
    response = client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def grant(client, admin, app_id: str, email: str, role: str):
    return client.put(
        f"/apps/{app_id}/access", json={"email": email, "role": role}, headers=admin
    )


# --------------------------------------------------------------------------
# The capability table
# --------------------------------------------------------------------------


def test_owner_can_do_everything():
    for action in Action:
        assert permissions.allows(Role.OWNER, action)


def test_editor_can_deploy_but_not_share_or_delete():
    assert permissions.allows(Role.EDITOR, Action.DEPLOY)
    assert not permissions.allows(Role.EDITOR, Action.SHARE)
    assert not permissions.allows(Role.EDITOR, Action.DELETE)


def test_viewer_can_only_view():
    assert permissions.allows(Role.VIEWER, Action.VIEW)
    for action in (Action.VIEW_LOGS, Action.DEPLOY, Action.SHARE, Action.DELETE):
        assert not permissions.allows(Role.VIEWER, action)


def test_no_role_allows_nothing():
    for action in Action:
        assert not permissions.allows(None, action)
        assert not permissions.allows("nonsense", action)


# --------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------


def test_a_stranger_cannot_see_the_app_at_all(client, admin, app_id):
    """404, not 403 — whether an app exists is itself information."""
    make_user(client, admin, "stranger@example.com")
    sign_in(client, "stranger@example.com")

    assert client.get(f"/apps/{app_id}").status_code == 404
    assert client.get("/apps").json() == []


def test_viewer_sees_the_app_but_not_its_logs(client, admin, app_id):
    make_user(client, admin, "viewer@example.com")
    grant(client, admin, app_id, "viewer@example.com", "viewer")
    sign_in(client, "viewer@example.com")

    assert client.get(f"/apps/{app_id}").status_code == 200
    assert [a["id"] for a in client.get("/apps").json()] == [app_id]

    # Logs can contain anything the app printed.
    assert client.get(f"/apps/{app_id}/logs").status_code == 403


def test_viewer_cannot_change_anything(client, admin, app_id):
    make_user(client, admin, "viewer@example.com")
    grant(client, admin, app_id, "viewer@example.com", "viewer")
    sign_in(client, "viewer@example.com")

    assert client.post(f"/apps/{app_id}/stop").status_code == 403
    assert client.post(f"/apps/{app_id}/restart").status_code == 403
    assert client.post(f"/apps/{app_id}/redeploy").status_code == 403
    assert client.delete(f"/apps/{app_id}").status_code == 403


def test_editor_can_deploy(client, admin, app_id):
    make_user(client, admin, "editor@example.com")
    grant(client, admin, app_id, "editor@example.com", "editor")
    sign_in(client, "editor@example.com")

    assert client.post(f"/apps/{app_id}/stop").status_code == 200
    assert client.get(f"/apps/{app_id}/logs").status_code == 200


def test_editor_cannot_delete_or_share(client, admin, app_id):
    make_user(client, admin, "editor@example.com")
    grant(client, admin, app_id, "editor@example.com", "editor")
    sign_in(client, "editor@example.com")

    assert client.delete(f"/apps/{app_id}").status_code == 403
    assert client.get(f"/apps/{app_id}/access").status_code == 403
    assert (
        client.put(
            f"/apps/{app_id}/access",
            json={"email": "editor@example.com", "role": "owner"},
        ).status_code
        == 403
    )


def test_owner_can_share_and_delete(client, admin, app_id):
    make_user(client, admin, "owner2@example.com")
    grant(client, admin, app_id, "owner2@example.com", "owner")
    sign_in(client, "owner2@example.com")

    assert client.get(f"/apps/{app_id}/access").status_code == 200
    assert client.delete(f"/apps/{app_id}").status_code == 204


def test_only_admins_may_create_apps(client, admin, source):
    make_user(client, admin, "editor@example.com")
    sign_in(client, "editor@example.com")

    response = client.post(
        "/apps", json={"name": "sneaky-app", "source_path": str(source)}
    )
    assert response.status_code == 403


def test_creator_becomes_the_owner(client, admin, source):
    make_user(client, admin, "boss@example.com")
    # Promote them so they can create apps.
    with store.session() as sess:
        user = store.user_by_email(sess, "boss@example.com")
        user.is_admin = True
        sess.add(user)
        sess.commit()

    sign_in(client, "boss@example.com")
    app_id = client.post(
        "/apps", json={"name": "mine-app", "source_path": str(source)}
    ).json()["id"]

    grants = client.get(f"/apps/{app_id}/access").json()
    assert [(g["email"], g["role"]) for g in grants] == [
        ("boss@example.com", "owner")
    ]


# --------------------------------------------------------------------------
# Sharing
# --------------------------------------------------------------------------


def test_granting_requires_an_existing_user(client, admin, app_id):
    """A typo must not hand access to whoever registers that address later."""
    response = grant(client, admin, app_id, "typo@example.com", "viewer")
    assert response.status_code == 404
    assert "invite them first" in response.json()["detail"]


def test_grant_reports_what_the_role_allows(client, admin, app_id):
    make_user(client, admin, "viewer@example.com")
    body = grant(client, admin, app_id, "viewer@example.com", "viewer").json()

    assert body["role"] == "viewer"
    assert body["can"] == ["view"]


def test_regranting_changes_the_role(client, admin, app_id):
    make_user(client, admin, "person@example.com")
    grant(client, admin, app_id, "person@example.com", "viewer")
    grant(client, admin, app_id, "person@example.com", "editor")

    grants = client.get(f"/apps/{app_id}/access", headers=admin).json()
    assert len(grants) == 1
    assert grants[0]["role"] == "editor"


def test_invalid_roles_are_rejected(client, admin, app_id):
    make_user(client, admin, "person@example.com")
    assert grant(client, admin, app_id, "person@example.com", "superuser").status_code == 422


def test_revoking_removes_access(client, admin, app_id):
    user_id = make_user(client, admin, "person@example.com")
    grant(client, admin, app_id, "person@example.com", "viewer")

    assert client.delete(
        f"/apps/{app_id}/access/{user_id}", headers=admin
    ).status_code == 204

    sign_in(client, "person@example.com")
    assert client.get(f"/apps/{app_id}").status_code == 404


def test_the_last_owner_cannot_be_removed(client, admin, app_id):
    """Otherwise the app becomes unmanageable without the admin token."""
    user_id = make_user(client, admin, "solo@example.com")
    grant(client, admin, app_id, "solo@example.com", "owner")

    response = client.delete(f"/apps/{app_id}/access/{user_id}", headers=admin)
    assert response.status_code == 409
    assert "at least one owner" in response.json()["detail"]


def test_an_owner_can_be_removed_when_another_remains(client, admin, app_id):
    first = make_user(client, admin, "one@example.com")
    make_user(client, admin, "two@example.com")
    grant(client, admin, app_id, "one@example.com", "owner")
    grant(client, admin, app_id, "two@example.com", "owner")

    assert client.delete(
        f"/apps/{app_id}/access/{first}", headers=admin
    ).status_code == 204


def test_deleting_a_user_removes_their_access(client, admin, app_id):
    user_id = make_user(client, admin, "leaver@example.com")
    grant(client, admin, app_id, "leaver@example.com", "editor")

    assert client.delete(f"/users/{user_id}", headers=admin).status_code == 204

    grants = client.get(f"/apps/{app_id}/access", headers=admin).json()
    assert grants == []


# --------------------------------------------------------------------------
# Sessions through the API
# --------------------------------------------------------------------------


def test_login_sets_an_httponly_cookie(client, admin):
    make_user(client, admin, "person@example.com")
    response = client.post(
        "/auth/login", json={"email": "person@example.com", "password": PASSWORD}
    )

    cookie = response.headers["set-cookie"]
    assert identity.SESSION_COOKIE in cookie
    # HttpOnly means an XSS in a deployed app cannot read the session.
    assert "httponly" in cookie.lower()


def test_bad_credentials_are_401(client, admin):
    make_user(client, admin, "person@example.com")
    assert client.post(
        "/auth/login", json={"email": "person@example.com", "password": "wrong"}
    ).status_code == 401


def test_me_reports_the_signed_in_user(client, admin):
    make_user(client, admin, "person@example.com")
    sign_in(client, "person@example.com")

    body = client.get("/auth/me").json()
    assert body["email"] == "person@example.com"
    assert body["authenticated"] is True


def test_logout_ends_the_session(client, admin, app_id):
    make_user(client, admin, "person@example.com")
    grant(client, admin, app_id, "person@example.com", "viewer")
    sign_in(client, "person@example.com")
    assert client.get(f"/apps/{app_id}").status_code == 200

    client.post("/auth/logout")
    assert client.get(f"/apps/{app_id}").status_code == 401


def test_admin_token_still_works_for_scripts(client, admin, app_id):
    """CI and the deploy pipeline have no browser to log in with."""
    assert client.get("/apps", headers=admin).status_code == 200
    assert client.get(f"/apps/{app_id}", headers=admin).status_code == 200


def test_only_admins_may_manage_users(client, admin):
    make_user(client, admin, "person@example.com")
    sign_in(client, "person@example.com")

    assert client.get("/users").status_code == 403
    assert client.post("/users", json={"email": "x@example.com"}).status_code == 403


# --------------------------------------------------------------------------
# Cookie scope — the difference between working in curl and working in a browser
# --------------------------------------------------------------------------


def test_session_cookie_is_host_only_by_default(client, admin):
    make_user(client, admin, "person@example.com")
    response = client.post(
        "/auth/login", json={"email": "person@example.com", "password": PASSWORD}
    )
    assert "domain=" not in response.headers["set-cookie"].lower()


def test_session_cookie_can_span_subdomains(client, admin, monkeypatch):
    """Apps live at <name>.<app domain>, so the session has to reach them.

    Without this the browser holds a cookie scoped to the control plane's own
    hostname, never sends it to an app, and every visit is refused — while an
    API client passing the cookie by hand sees nothing wrong.
    """
    monkeypatch.setenv("HANGAR_COOKIE_DOMAIN", ".example.com")
    make_user(client, admin, "person@example.com")

    response = client.post(
        "/auth/login", json={"email": "person@example.com", "password": PASSWORD}
    )
    assert "domain=.example.com" in response.headers["set-cookie"].lower()


def test_logout_clears_the_cookie_on_the_same_domain(client, admin, monkeypatch):
    """A mismatched domain leaves the old cookie in place and logout does nothing."""
    monkeypatch.setenv("HANGAR_COOKIE_DOMAIN", ".example.com")
    make_user(client, admin, "person@example.com")
    sign_in(client, "person@example.com")

    response = client.post("/auth/logout")
    assert "domain=.example.com" in response.headers["set-cookie"].lower()


def test_app_auth_without_a_cookie_domain_is_refused(monkeypatch):
    """Fail at startup rather than silently refusing every visitor."""
    from hangar import config

    monkeypatch.setenv("HANGAR_APP_AUTH", "1")
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_APP_DOMAIN", "apps.example.com")
    monkeypatch.delenv("HANGAR_COOKIE_DOMAIN", raising=False)

    with pytest.raises(ValueError, match="HANGAR_COOKIE_DOMAIN"):
        config.settings().validate()


def test_app_auth_with_a_cookie_domain_is_accepted(monkeypatch):
    from hangar import config

    monkeypatch.setenv("HANGAR_APP_AUTH", "1")
    monkeypatch.setenv("HANGAR_ROUTER", "caddy")
    monkeypatch.setenv("HANGAR_APP_DOMAIN", "apps.example.com")
    monkeypatch.setenv("HANGAR_COOKIE_DOMAIN", ".apps.example.com")

    config.settings().validate()
