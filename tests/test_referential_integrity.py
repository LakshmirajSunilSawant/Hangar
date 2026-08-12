"""Deleting things that other rows point at.

Every one of these passed before the code was fixed, because SQLite does not
enforce foreign keys unless asked and the suite never asked. Postgres does, so
`DELETE /apps/{id}` failed in the live deployment on any app that had ever been
shared or deployed — while CI was green.

The systemic fix is in store.py, which now turns the pragma on for SQLite so
the test database behaves like the production one. These tests exist so the
individual delete paths stay covered, and so the reason is written down.

The trap they guard is specific and easy to walk back into: SQLAlchemy is not
told about the relationships between these tables (SQLModel declares plain
foreign-key columns, not `Relationship()`), so it does not order DELETEs by
dependency. Marking children for deletion before the parent is not enough —
without a flush in between, the parent's DELETE is emitted first and the whole
transaction is refused.
"""

import pytest

from hangar import appsecrets, secrets, store
from hangar.store import App, AppDatabase, Deployment, Permission, User, UserSession


TOKEN = "admin-token"


@pytest.fixture(autouse=True)
def sealing_key(monkeypatch):
    monkeypatch.setenv("HANGAR_SECRET_KEY", secrets.generate_key())


@pytest.fixture
def admin(monkeypatch):
    """Operator credentials — a shared app is not deletable anonymously."""
    monkeypatch.setenv("HANGAR_API_TOKEN", TOKEN)
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def app_id(db):
    with store.session() as sess:
        app = App(name="notes", source_ref="/s", source_dir="/s", db_type="sqlite")
        store.save(sess, app)
        return app.id


@pytest.fixture
def user_id(db):
    with store.session() as sess:
        user = User(email="member@example.com", password_hash="x")
        store.save(sess, user)
        return user.id


def test_sqlite_enforces_foreign_keys(db):
    """The pragma that makes every other test in this file meaningful."""
    from sqlalchemy import text

    with store.session() as sess:
        enabled = sess.exec(text("PRAGMA foreign_keys")).one()[0]
    assert enabled == 1, "SQLite is not enforcing foreign keys"


def test_an_orphan_row_is_rejected(db):
    """Proves the constraint is live, not just declared."""
    from sqlalchemy.exc import IntegrityError

    with store.session() as sess:
        sess.add(Permission(app_id="no-such-app", user_id="no-such-user"))
        with pytest.raises(IntegrityError):
            sess.commit()


# --------------------------------------------------------------------------
# Deleting an app
# --------------------------------------------------------------------------


def test_deleting_a_shared_app(client, admin, app_id, user_id, fake_backend):
    """The case that failed in production: whoami was shared, so it was stuck."""
    with store.session() as sess:
        sess.add(Permission(app_id=app_id, user_id=user_id, role="viewer"))
        sess.commit()

    assert client.delete(f"/apps/{app_id}", headers=admin).status_code == 204
    with store.session() as sess:
        assert store.get_app(sess, app_id) is None
        assert store.permissions_for_app(sess, app_id) == []


def test_deleting_an_app_that_has_been_deployed(client, app_id, fake_backend):
    with store.session() as sess:
        sess.add(Deployment(app_id=app_id))
        sess.commit()

    assert client.delete(f"/apps/{app_id}").status_code == 204
    with store.session() as sess:
        assert store.deployments_for(sess, app_id) == []


def test_deleting_an_app_with_secrets(client, app_id, fake_backend):
    with store.session() as sess:
        appsecrets.put(sess, app_id, "API_KEY", "hunter2")

    assert client.delete(f"/apps/{app_id}").status_code == 204
    with store.session() as sess:
        assert appsecrets.list_for(sess, app_id) == []


def test_deleting_an_app_with_a_database_record(client, app_id, fake_backend):
    with store.session() as sess:
        sess.add(
            AppDatabase(app_id=app_id, db_type="sqlite", connection_ref="vol")
        )
        sess.commit()

    assert client.delete(f"/apps/{app_id}").status_code == 204
    with store.session() as sess:
        assert store.databases_for(sess, app_id) == []


def test_keep_data_still_deletes_the_app(client, app_id, fake_backend):
    """keep_data skips deprovisioning, which used to strand the AppDatabase row."""
    with store.session() as sess:
        sess.add(
            AppDatabase(app_id=app_id, db_type="postgres", connection_ref="db")
        )
        sess.commit()

    assert client.delete(f"/apps/{app_id}?keep_data=true").status_code == 204
    with store.session() as sess:
        assert store.get_app(sess, app_id) is None


def test_deleting_an_app_with_everything_at_once(
    client, admin, app_id, user_id, fake_backend
):
    with store.session() as sess:
        sess.add(Permission(app_id=app_id, user_id=user_id, role="editor"))
        sess.add(Deployment(app_id=app_id))
        sess.add(AppDatabase(app_id=app_id, db_type="sqlite", connection_ref="v"))
        sess.commit()
        appsecrets.put(sess, app_id, "API_KEY", "x")

    assert client.delete(f"/apps/{app_id}", headers=admin).status_code == 204


# --------------------------------------------------------------------------
# Deleting a user
# --------------------------------------------------------------------------


def test_deleting_a_user_with_a_grant_and_a_session(client, admin, app_id, user_id):
    with store.session() as sess:
        sess.add(Permission(app_id=app_id, user_id=user_id, role="viewer"))
        sess.add(
            UserSession(
                token_hash="h", user_id=user_id, expires_at=store.utcnow()
            )
        )
        sess.commit()

    assert client.delete(f"/users/{user_id}", headers=admin).status_code == 204
    with store.session() as sess:
        assert store.get_user(sess, user_id) is None
        assert store.permissions_for_app(sess, app_id) == []
