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
    STOPPED = "stopped"
    FAILED = "failed"


class DeploymentStatus(str, Enum):
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ScanStatus(str, Enum):
    SKIPPED = "skipped"
    CLEAN = "clean"
    FLAGGED = "flagged"   # findings recorded, deploy continued
    BLOCKED = "blocked"   # findings above the threshold, deploy refused


class App(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(index=True)
    source_type: str = "path"  # "path" today; "zip"/"repo" as ingestion grows
    source_ref: str  # absolute path to the source directory
    runtime: str | None = None
    framework: str | None = None
    status: str = AppStatus.QUEUED
    url: str | None = None
    host_port: int | None = None
    # Dial address the router uses. Stored because with egress denied there is
    # no host port to recompute it from on restart.
    upstream: str | None = None
    error: str | None = None
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
        SQLModel.metadata.create_all(_engine)
    return _engine


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
