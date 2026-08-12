"""Persistence for the control plane.

Driven by DATABASE_URL. SQLite by default, so a laptop needs no setup;
Postgres when a URL is supplied, since a hosted control plane needs state that
survives a restart and free-tier hosts rarely offer a persistent disk.

This covers the App and Deployment halves of the PRD's data model. User,
Permission, AppDatabase, and ResourceUsage arrive with the auth, database, and
observability milestones respectively; modelling them now would mean guessing
at an auth design that hasn't been built.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select

from . import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    # Short enough to read in a URL and a container name, wide enough not to collide.
    return uuid.uuid4().hex[:12]


class AppStatus(str, Enum):
    QUEUED = "queued"
    BUILDING = "building"
    RUNNING = "running"
    # Stopped by the idle reaper rather than by a person, and woken by the next
    # request. Distinct from STOPPED because a deliberate stop must stay
    # stopped — see idle.py.
    SLEEPING = "sleeping"
    STOPPED = "stopped"
    FAILED = "failed"


class DeploymentStatus(str, Enum):
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Role(str, Enum):
    """PRD §7. Ordered by power; see `permissions.py` for what each may do."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class ScanStatus(str, Enum):
    SKIPPED = "skipped"
    CLEAN = "clean"
    FLAGGED = "flagged"   # findings recorded, deploy continued
    BLOCKED = "blocked"   # findings above the threshold, deploy refused


class App(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(index=True)
    source_type: str = "path"  # "path" | "zip" | "repo"
    # Where the source came from: an absolute path, an uploaded filename, or a
    # repository URL. Kept for display and for re-fetching repo-backed apps.
    source_ref: str
    # Local directory the build reads. Same as source_ref for "path"; an
    # extracted directory under HANGAR_SOURCE_ROOT for "zip" and "repo".
    source_dir: str = ""
    # Branch, tag, or commit for repo-backed apps.
    source_revision: str | None = None
    runtime: str | None = None
    framework: str | None = None
    status: str = AppStatus.QUEUED
    url: str | None = None
    host_port: int | None = None
    # Dial address the router uses. Stored because with egress denied there is
    # no host port to recompute it from on restart.
    upstream: str | None = None
    # "none" | "sqlite" | "postgres". Null means "use the server default".
    db_type: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class User(SQLModel, table=True):
    """PRD §7. Identity lives with the platform, not with any deployed app."""

    id: str = Field(default_factory=new_id, primary_key=True)
    email: str = Field(index=True, unique=True)
    # Which identity provider vouches for this user, and its id for them.
    auth_provider: str = "local"
    auth_provider_id: str | None = None
    # argon2id, from libsodium. Empty until an invite is accepted, and always
    # empty for externally-authenticated users.
    password_hash: str = ""
    # Hash of the one-time invite token; cleared once used.
    invite_hash: str = ""
    invited_at: datetime | None = None
    is_admin: bool = False
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_active(self) -> bool:
        return bool(self.password_hash) or self.auth_provider != "local"


class Permission(SQLModel, table=True):
    """Who may do what with one app (PRD §7)."""

    id: str = Field(default_factory=new_id, primary_key=True)
    app_id: str = Field(index=True, foreign_key="app.id")
    user_id: str = Field(index=True, foreign_key="user.id")
    role: str = Role.VIEWER
    granted_at: datetime = Field(default_factory=utcnow)


class UserSession(SQLModel, table=True):
    """A logged-in browser.

    Only the hash of the session token is stored, so a leaked database does not
    hand over live sessions.
    """

    id: str = Field(default_factory=new_id, primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    user_id: str = Field(index=True, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime

    def is_expired(self, now: datetime | None = None) -> bool:
        moment = now or utcnow()
        expires = self.expires_at
        # SQLite hands back naive datetimes; compare like with like.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= moment


class AppDatabase(SQLModel, table=True):
    """Storage scoped to one app (PRD §7)."""

    id: str = Field(default_factory=new_id, primary_key=True)
    app_id: str = Field(index=True, foreign_key="app.id")
    db_type: str  # "sqlite" | "postgres"
    # Volume name or database name. Never a password.
    connection_ref: str
    # Sealed with libsodium; empty for sqlite, which has no credentials.
    secret: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class AppSecret(SQLModel, table=True):
    """One environment variable an app needs but must not ship in its source.

    The value is sealed with libsodium (see secrets.py) and is never returned
    by the API — not to an owner, not to an admin. Names are listable; values
    only ever leave here on their way into a container's environment.
    """

    id: str = Field(default_factory=new_id, primary_key=True)
    app_id: str = Field(index=True, foreign_key="app.id")
    name: str = Field(index=True)
    sealed_value: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Deployment(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    app_id: str = Field(index=True, foreign_key="app.id")
    status: str = DeploymentStatus.BUILDING
    image_ref: str | None = None
    build_log: str = ""
    error: str | None = None
    scan_status: str = ScanStatus.SKIPPED
    # Serialised ScanResult. JSON in a text column rather than its own table:
    # findings are only ever read as a whole, per deployment.
    scan_report: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    def scan(self) -> dict:
        if not self.scan_report:
            return {}
        try:
            return json.loads(self.scan_report)
        except json.JSONDecodeError:
            return {}


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


_engine = None


def engine(db_path: Path | str | None = None):
    """Process-wide engine, created on first use.

    ``db_path`` is a convenience for tests; normal callers get whatever
    DATABASE_URL (or the SQLite default) resolves to.
    """
    global _engine
    if _engine is None or db_path is not None:
        url = f"sqlite:///{Path(db_path)}" if db_path else config.database_url()
        _engine = create_engine(url, **_engine_options(url))
        if url.startswith("sqlite"):
            _enforce_sqlite_foreign_keys(_engine)
        SQLModel.metadata.create_all(_engine)
    return _engine


def _enforce_sqlite_foreign_keys(engine) -> None:
    """Make SQLite check foreign keys, which it does not do by default.

    Without this, SQLite silently accepts deletes that Postgres refuses, so a
    deployment on Postgres hits integrity errors that no test on SQLite could
    ever have caught. That is not hypothetical: deleting a shared app failed in
    production while the whole suite was green, because the Permission rows
    pointing at it were never removed and SQLite did not care.

    Per-connection, because the pragma is not persisted in the database file.
    """
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _engine_options(url: str) -> dict:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Deploys run on background threads, so connections cross threads.
        return {"connect_args": {"check_same_thread": False}}

    # Managed Postgres (Render, Neon, Supabase) drops idle connections; without
    # pre-ping the first query after an idle period fails on a dead socket.
    return {"pool_pre_ping": True, "pool_recycle": 300}


def reset_engine() -> None:
    """Drop the cached engine — used by tests to swap databases."""
    global _engine
    _engine = None


def session() -> Session:
    return Session(engine())


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def get_app(sess: Session, app_id: str) -> App | None:
    return sess.get(App, app_id)


def list_apps(sess: Session) -> list[App]:
    return list(sess.exec(select(App).order_by(App.created_at.desc())).all())


def app_by_name(sess: Session, name: str) -> App | None:
    return sess.exec(select(App).where(App.name == name)).first()


def user_by_email(sess, email: str) -> User | None:
    return sess.exec(select(User).where(User.email == email.strip().lower())).first()


def get_user(sess, user_id: str) -> User | None:
    return sess.get(User, user_id)


def list_users(sess) -> list[User]:
    return list(sess.exec(select(User).order_by(User.created_at)).all())


def session_by_hash(sess, token_hash: str) -> UserSession | None:
    return sess.exec(
        select(UserSession).where(UserSession.token_hash == token_hash)
    ).first()


def permission_for(sess, app_id: str, user_id: str) -> Permission | None:
    return sess.exec(
        select(Permission)
        .where(Permission.app_id == app_id)
        .where(Permission.user_id == user_id)
    ).first()


def permissions_for_app(sess, app_id: str) -> list[Permission]:
    return list(
        sess.exec(select(Permission).where(Permission.app_id == app_id)).all()
    )


def apps_visible_to(sess, user_id: str) -> list[App]:
    """Only apps this user has been granted something on."""
    app_ids = [
        p.app_id
        for p in sess.exec(select(Permission).where(Permission.user_id == user_id)).all()
    ]
    if not app_ids:
        return []
    return list(
        sess.exec(
            select(App).where(App.id.in_(app_ids)).order_by(App.created_at.desc())
        ).all()
    )


def databases_for(sess, app_id: str) -> list[AppDatabase]:
    return list(
        sess.exec(select(AppDatabase).where(AppDatabase.app_id == app_id)).all()
    )


def deployments_for(sess: Session, app_id: str) -> list[Deployment]:
    return list(
        sess.exec(
            select(Deployment)
            .where(Deployment.app_id == app_id)
            .order_by(Deployment.created_at.desc())
        ).all()
    )


def latest_deployment(sess: Session, app_id: str) -> Deployment | None:
    deployments = deployments_for(sess, app_id)
    return deployments[0] if deployments else None


def save(sess: Session, *records) -> None:
    for record in records:
        if isinstance(record, App):
            record.updated_at = utcnow()
        sess.add(record)
    sess.commit()
    for record in records:
        sess.refresh(record)
