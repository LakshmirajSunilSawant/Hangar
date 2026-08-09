"""Persistence for the control plane.

SQLite via SQLModel — the PRD reserves Postgres for per-app databases, but the
control plane's own state is small enough that a single file keeps the MVP
dependency-free.

This covers the App and Deployment halves of the PRD's data model. User,
Permission, AppDatabase, and ResourceUsage arrive with the auth, database, and
observability milestones respectively; modelling them now would mean guessing
at an auth design that hasn't been built.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select


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
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


def default_db_path() -> Path:
    override = os.environ.get("HANGAR_DB")
    if override:
        return Path(override)
    return Path.cwd() / ".hangar" / "hangar.db"


_engine = None


def engine(db_path: Path | str | None = None):
    """Process-wide engine, created on first use."""
    global _engine
    if _engine is None or db_path is not None:
        path = Path(db_path) if db_path else default_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{path}",
            # Deploys run on background threads, so connections cross threads.
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(_engine)
    return _engine


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
