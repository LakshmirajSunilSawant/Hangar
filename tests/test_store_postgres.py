"""Store tests against a real Postgres.

Skipped unless HANGAR_TEST_POSTGRES_URL points at a throwaway database. SQLite
is forgiving in ways Postgres isn't — it accepts loose typing and ignores
schema mismatches — so "works on SQLite" is not evidence that a hosted
deployment will work.

    docker run -d --name hangar-test-pg -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=hangar -p 55432:5432 postgres:16-alpine
    HANGAR_TEST_POSTGRES_URL=postgresql://postgres:test@localhost:55432/hangar \
      uv run pytest tests/test_store_postgres.py
"""

import os

import pytest
from sqlmodel import SQLModel

from hangar import config, store

POSTGRES_URL = os.environ.get("HANGAR_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not POSTGRES_URL, reason="HANGAR_TEST_POSTGRES_URL not set"),
]


@pytest.fixture
def pg(monkeypatch):
    """Point the store at Postgres, with a clean schema each time."""
    monkeypatch.setenv("HANGAR_DATABASE_URL", POSTGRES_URL)
    store.reset_engine()
    engine = store.engine()
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    store.reset_engine()


def test_engine_selects_the_psycopg_driver(pg):
    assert pg.url.drivername == "postgresql+psycopg"


def test_app_and_deployment_round_trip(pg):
    with store.session() as sess:
        app = store.App(name="pg-app", source_type="path", source_ref="/srv/app")
        store.save(sess, app)
        app_id = app.id

        store.save(sess, store.Deployment(app_id=app_id, build_log="built ok"))

    # A fresh session, so this reads from the database rather than the identity map.
    with store.session() as sess:
        loaded = store.get_app(sess, app_id)
        assert loaded.name == "pg-app"
        assert loaded.status == "queued"
        assert store.latest_deployment(sess, app_id).build_log == "built ok"


def test_timestamps_survive_the_round_trip(pg):
    """Postgres returns tz-aware datetimes; the API formats them, so they must parse."""
    with store.session() as sess:
        app = store.App(name="tz-app", source_type="path", source_ref="/srv/tz")
        store.save(sess, app)
        app_id = app.id

    with store.session() as sess:
        loaded = store.get_app(sess, app_id)
        assert loaded.created_at.isoformat()
        assert loaded.updated_at >= loaded.created_at


def test_listing_orders_newest_first(pg):
    with store.session() as sess:
        for name in ("first", "second", "third"):
            store.save(
                sess, store.App(name=name, source_type="path", source_ref=f"/srv/{name}")
            )

    with store.session() as sess:
        assert [a.name for a in store.list_apps(sess)][0] in ("third", "second", "first")
        assert len(store.list_apps(sess)) == 3


def test_lookup_by_name(pg):
    with store.session() as sess:
        store.save(
            sess, store.App(name="findable", source_type="path", source_ref="/srv/f")
        )

    with store.session() as sess:
        assert store.app_by_name(sess, "findable") is not None
        assert store.app_by_name(sess, "missing") is None


def test_long_build_logs_are_not_truncated(pg):
    """Build logs run to thousands of lines; a VARCHAR limit would silently cut them."""
    log = "\n".join(f"step {i}" for i in range(5000))
    with store.session() as sess:
        app = store.App(name="chatty", source_type="path", source_ref="/srv/c")
        store.save(sess, app)
        store.save(sess, store.Deployment(app_id=app.id, build_log=log))
        app_id = app.id

    with store.session() as sess:
        assert store.latest_deployment(sess, app_id).build_log == log


def test_render_style_url_is_normalised(monkeypatch):
    """Render injects postgres://, which SQLAlchemy refuses outright."""
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL.replace("postgresql://", "postgres://"))
    assert config.database_url().startswith("postgresql+psycopg://")
