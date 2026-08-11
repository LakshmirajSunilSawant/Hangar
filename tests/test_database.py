"""Per-app database provisioning.

The Postgres path talks to a real Postgres when HANGAR_TEST_POSTGRES_URL is
set, and is skipped otherwise — role and database creation is exactly the kind
of thing a mock would happily let you get wrong.
"""

import base64
import os

import pytest

from hangar import database, store
from hangar import deploy as deploy_mod
from hangar.database import DatabaseError

POSTGRES_URL = os.environ.get("HANGAR_TEST_POSTGRES_URL")
KEY = base64.b64encode(b"k" * 32).decode()

SAFE_APP = {
    "requirements.txt": "fastapi\n",
    "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
}


def make_app(tmp_path, db_type=None, name="db-app") -> str:
    source = tmp_path / name
    source.mkdir(parents=True, exist_ok=True)
    for filename, content in SAFE_APP.items():
        (source / filename).write_text(content, encoding="utf-8")

    with store.session() as sess:
        app = store.App(
            name=name,
            source_type="path",
            source_ref=str(source),
            source_dir=str(source),
            db_type=db_type,
        )
        store.save(sess, app)
        return app.id


def db_record(app_id):
    with store.session() as sess:
        return database._record(sess, app_id)


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


def test_no_database_by_default(db, fake_backend, tmp_path):
    app_id = make_app(tmp_path)
    deploy_mod.deploy(app_id)

    assert db_record(app_id) is None
    assert "DATABASE_URL" not in fake_backend.last_env


def test_server_default_applies_when_the_request_says_nothing(
    db, fake_backend, tmp_path, monkeypatch
):
    monkeypatch.setenv("HANGAR_APP_DB", "sqlite")
    app_id = make_app(tmp_path)
    deploy_mod.deploy(app_id)

    assert db_record(app_id).db_type == "sqlite"


def test_per_app_choice_overrides_the_server_default(
    db, fake_backend, tmp_path, monkeypatch
):
    monkeypatch.setenv("HANGAR_APP_DB", "sqlite")
    app_id = make_app(tmp_path, db_type="none")
    deploy_mod.deploy(app_id)

    assert db_record(app_id) is None


def test_unknown_database_type_fails_the_deploy(db, fake_backend, tmp_path):
    app_id = make_app(tmp_path, db_type="mysql")
    deploy_mod.deploy(app_id)

    with store.session() as sess:
        app = store.get_app(sess, app_id)
    assert app.status == "failed"
    assert "unknown database type" in app.error


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------


def test_sqlite_injects_a_database_url_and_mounts_a_volume(db, fake_backend, tmp_path):
    app_id = make_app(tmp_path, db_type="sqlite")
    deploy_mod.deploy(app_id)

    assert fake_backend.last_env["DATABASE_URL"] == "sqlite:////data/app.db"
    assert fake_backend.last_env["HANGAR_DATA_DIR"] == "/data"

    volume = database.volume_name(app_id)
    assert fake_backend.last_volumes[volume]["bind"] == "/data"
    # Read-write, or the whole exercise is pointless.
    assert fake_backend.last_volumes[volume]["mode"] == "rw"


def test_sqlite_needs_no_secret_key(db, fake_backend, tmp_path, monkeypatch):
    """There are no credentials, so requiring a key would be gratuitous."""
    monkeypatch.delenv("HANGAR_SECRET_KEY", raising=False)
    app_id = make_app(tmp_path, db_type="sqlite")
    deploy_mod.deploy(app_id)

    with store.session() as sess:
        assert store.get_app(sess, app_id).status == "running"
    assert db_record(app_id).secret == ""


def test_redeploy_reuses_the_same_volume(db, fake_backend, tmp_path):
    """A new volume per deploy would silently lose the app's data."""
    app_id = make_app(tmp_path, db_type="sqlite")
    deploy_mod.deploy(app_id)
    first = dict(fake_backend.last_volumes)

    deploy_mod.deploy(app_id)
    assert fake_backend.last_volumes == first

    with store.session() as sess:
        from sqlmodel import select

        rows = sess.exec(
            select(store.AppDatabase).where(store.AppDatabase.app_id == app_id)
        ).all()
    assert len(rows) == 1


def test_volume_name_is_scoped_to_the_app(db, tmp_path):
    a = make_app(tmp_path, db_type="sqlite", name="app-one")
    b = make_app(tmp_path, db_type="sqlite", name="app-two")
    assert database.volume_name(a) != database.volume_name(b)


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------


def test_deleting_an_app_destroys_its_data(client, fake_backend, tmp_path):
    app_id = client.post(
        "/apps",
        json={
            "name": "doomed-app",
            "source_path": str(_source(tmp_path)),
            "database": "sqlite",
        },
    ).json()["id"]

    assert db_record(app_id) is not None
    assert client.delete(f"/apps/{app_id}").status_code == 204

    assert db_record(app_id) is None
    assert app_id in fake_backend.removed_data


def test_keep_data_preserves_the_database(client, fake_backend, tmp_path):
    """Deleting an app is not always meant to delete a year of its data."""
    app_id = client.post(
        "/apps",
        json={
            "name": "spared-app",
            "source_path": str(_source(tmp_path)),
            "database": "sqlite",
        },
    ).json()["id"]

    assert client.delete(f"/apps/{app_id}?keep_data=true").status_code == 204
    assert app_id not in fake_backend.removed_data


def _source(tmp_path):
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    for name, content in SAFE_APP.items():
        (source / name).write_text(content, encoding="utf-8")
    return source


# --------------------------------------------------------------------------
# API surface
# --------------------------------------------------------------------------


def test_api_rejects_an_unknown_database_choice(client, fake_backend, tmp_path):
    response = client.post(
        "/apps",
        json={
            "name": "bad-db-app",
            "source_path": str(_source(tmp_path)),
            "database": "mongo",
        },
    )
    assert response.status_code == 422
    assert "none, sqlite, postgres" in response.json()["detail"]


def test_api_reports_the_choice(client, fake_backend, tmp_path):
    body = client.post(
        "/apps",
        json={
            "name": "reported-app",
            "source_path": str(_source(tmp_path)),
            "database": "sqlite",
        },
    ).json()
    assert body["database"] == "sqlite"


# --------------------------------------------------------------------------
# Postgres — against a real server
# --------------------------------------------------------------------------


postgres = pytest.mark.skipif(
    not POSTGRES_URL, reason="HANGAR_TEST_POSTGRES_URL not set"
)


@pytest.fixture
def pg(monkeypatch):
    monkeypatch.setenv("HANGAR_APP_DB_ADMIN_URL", POSTGRES_URL)
    monkeypatch.setenv("HANGAR_SECRET_KEY", KEY)


@postgres
def test_postgres_requires_a_secret_key(db, fake_backend, tmp_path, monkeypatch):
    """Otherwise the generated password would be stored in plaintext."""
    monkeypatch.setenv("HANGAR_APP_DB_ADMIN_URL", POSTGRES_URL)
    monkeypatch.delenv("HANGAR_SECRET_KEY", raising=False)

    app_id = make_app(tmp_path, db_type="postgres", name="nokey-app")
    deploy_mod.deploy(app_id)

    with store.session() as sess:
        app = store.get_app(sess, app_id)
    assert app.status == "failed"
    assert "HANGAR_SECRET_KEY" in app.error


@postgres
def test_postgres_provisions_a_usable_database(db, fake_backend, tmp_path, pg):
    from sqlalchemy import create_engine, text

    app_id = make_app(tmp_path, db_type="postgres", name="pg-app")
    try:
        deploy_mod.deploy(app_id)

        with store.session() as sess:
            assert store.get_app(sess, app_id).status == "running", (
                store.get_app(sess, app_id).error
            )

        url = fake_backend.last_env["DATABASE_URL"]
        assert url.startswith("postgresql://")

        # The credentials handed to the app must actually work.
        engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (id int)"))
            conn.execute(text("INSERT INTO t VALUES (1)"))
            conn.commit()
            assert conn.execute(text("SELECT count(*) FROM t")).scalar() == 1
        engine.dispose()
    finally:
        with store.session() as sess:
            database.deprovision(sess, app_id)


@postgres
def test_postgres_password_is_never_stored_in_plaintext(db, fake_backend, tmp_path, pg):
    app_id = make_app(tmp_path, db_type="postgres", name="sealed-app")
    try:
        deploy_mod.deploy(app_id)

        password = fake_backend.last_env["DATABASE_URL"].split(":")[2].split("@")[0]
        record = db_record(app_id)

        assert record.secret
        assert password not in record.secret
        assert password not in record.connection_ref
    finally:
        with store.session() as sess:
            database.deprovision(sess, app_id)


@postgres
def test_redeploy_keeps_the_same_credentials(db, fake_backend, tmp_path, pg):
    """Rotating the password on every deploy would break a running app."""
    app_id = make_app(tmp_path, db_type="postgres", name="stable-app")
    try:
        deploy_mod.deploy(app_id)
        first = fake_backend.last_env["DATABASE_URL"]

        deploy_mod.deploy(app_id)
        assert fake_backend.last_env["DATABASE_URL"] == first
    finally:
        with store.session() as sess:
            database.deprovision(sess, app_id)


@postgres
def test_deprovision_drops_the_database_and_role(db, fake_backend, tmp_path, pg):
    from sqlalchemy import create_engine, text

    app_id = make_app(tmp_path, db_type="postgres", name="dropped-app")
    deploy_mod.deploy(app_id)
    name = database.identifier(app_id)

    with store.session() as sess:
        database.deprovision(sess, app_id)

    engine = create_engine(POSTGRES_URL.replace("postgresql://", "postgresql+psycopg://"))
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).first() is None
        assert conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}
        ).first() is None
    engine.dispose()


@postgres
def test_apps_cannot_reach_each_others_databases(db, fake_backend, tmp_path, pg):
    """Separate databases, not shared schemas — PRD §9's isolation goal."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError, ProgrammingError

    first = make_app(tmp_path, db_type="postgres", name="tenant-one")
    second = make_app(tmp_path, db_type="postgres", name="tenant-two")
    try:
        deploy_mod.deploy(first)
        first_url = fake_backend.last_env["DATABASE_URL"]
        deploy_mod.deploy(second)

        # Try to reach app two's database using app one's credentials.
        other = database.identifier(second)
        user, rest = first_url.split("://", 1)[1].split("@", 1)
        host = rest.split("/", 1)[0]
        crossover = f"postgresql+psycopg://{user}@{host}/{other}"

        engine = create_engine(crossover, connect_args={"connect_timeout": 5})
        with pytest.raises((OperationalError, ProgrammingError)):
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        engine.dispose()
    finally:
        with store.session() as sess:
            database.deprovision(sess, first)
            database.deprovision(sess, second)


def test_identifier_rejects_unsafe_app_ids():
    """Identifiers can't be parameterised, so they must be provably safe."""
    with pytest.raises(DatabaseError, match="safe database name"):
        database.identifier('x"; DROP DATABASE hangar; --')


def test_identifier_is_derived_from_the_app_id():
    assert database.identifier("abc123def456") == "app_abc123def456"
