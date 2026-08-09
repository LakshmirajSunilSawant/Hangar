"""Hangar control plane API.

No auth yet — this is the thin vertical slice from the PRD's Milestone 2
("does the box work"), and the auth/permission layer is Milestone 3. Until
that lands, bind this to localhost only.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import deploy as deploy_mod
from . import runtime, store
from .store import AppStatus

api = FastAPI(
    title="Hangar",
    description="Cloud for small software — deploy a generated app to a live URL.",
    version="0.1.0",
)

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class CreateAppRequest(BaseModel):
    name: str = Field(
        description="Lowercase name, used in the image tag and container name.",
        examples=["team-dashboard"],
    )
    source_path: str = Field(
        description="Absolute path to the app's source directory on this host.",
    )


class AppView(BaseModel):
    id: str
    name: str
    status: str
    url: str | None = None
    runtime: str | None = None
    framework: str | None = None
    source_ref: str
    error: str | None = None
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, app: store.App) -> "AppView":
        return cls(
            id=app.id,
            name=app.name,
            status=app.status,
            url=app.url,
            runtime=app.runtime,
            framework=app.framework,
            source_ref=app.source_ref,
            error=app.error,
            created_at=app.created_at.isoformat(),
            updated_at=app.updated_at.isoformat(),
        )


class LogsView(BaseModel):
    app_id: str
    build_log: str
    runtime_log: str


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@api.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@api.post("/apps", response_model=AppView, status_code=202)
def create_app(request: CreateAppRequest, background: BackgroundTasks) -> AppView:
    """Register an app and kick off a deploy.

    Returns 202 immediately — the build takes far longer than a request should.
    Poll GET /apps/{id} for the outcome.
    """
    name = request.name.strip().lower()
    if not NAME_PATTERN.match(name):
        raise HTTPException(
            422,
            "name must be 3-40 characters of lowercase letters, digits, or hyphens, "
            "and start and end with a letter or digit",
        )

    source = Path(request.source_path).expanduser()
    if not source.is_absolute():
        raise HTTPException(422, "source_path must be an absolute path")
    if not source.is_dir():
        raise HTTPException(422, f"source_path is not a directory: {source}")

    with store.session() as sess:
        if store.app_by_name(sess, name) is not None:
            raise HTTPException(409, f"an app named '{name}' already exists")

        app = store.App(name=name, source_type="path", source_ref=str(source.resolve()))
        store.save(sess, app)
        view = AppView.of(app)

    background.add_task(deploy_mod.deploy, view.id)
    return view


@api.get("/apps", response_model=list[AppView])
def list_apps() -> list[AppView]:
    with store.session() as sess:
        return [AppView.of(app) for app in store.list_apps(sess)]


@api.get("/apps/{app_id}", response_model=AppView)
def get_app(app_id: str) -> AppView:
    with store.session() as sess:
        return AppView.of(_require(sess, app_id))


@api.get("/apps/{app_id}/logs", response_model=LogsView)
def get_logs(app_id: str, tail: int = 200) -> LogsView:
    with store.session() as sess:
        app = _require(sess, app_id)
        deployment = store.latest_deployment(sess, app_id)
        build_log = deployment.build_log if deployment else ""

    try:
        runtime_log = runtime.logs(app_id, tail=tail)
    except runtime.DeployError:
        # No container yet (or already removed) — the build log still matters.
        runtime_log = ""

    return LogsView(app_id=app.id, build_log=build_log, runtime_log=runtime_log)


@api.post("/apps/{app_id}/redeploy", response_model=AppView, status_code=202)
def redeploy(app_id: str, background: BackgroundTasks) -> AppView:
    with store.session() as sess:
        app = _require(sess, app_id)
        app.status = AppStatus.QUEUED
        app.error = None
        store.save(sess, app)
        view = AppView.of(app)

    background.add_task(deploy_mod.deploy, app_id)
    return view


@api.post("/apps/{app_id}/stop", response_model=AppView)
def stop_app(app_id: str) -> AppView:
    with store.session() as sess:
        app = _require(sess, app_id)
        _act(runtime.stop, app_id)
        app.status = AppStatus.STOPPED
        app.url = None
        store.save(sess, app)
        return AppView.of(app)


@api.post("/apps/{app_id}/restart", response_model=AppView)
def restart_app(app_id: str) -> AppView:
    with store.session() as sess:
        app = _require(sess, app_id)
        _act(runtime.restart, app_id)

        # The published port can change across a restart, so re-read it from
        # Docker rather than trusting what was stored at deploy time.
        port = runtime.host_port(app_id)
        if port is not None:
            app.host_port = port
            app.url = f"http://localhost:{port}"
        app.status = AppStatus.RUNNING
        store.save(sess, app)
        return AppView.of(app)


@api.delete("/apps/{app_id}", status_code=204)
def delete_app(app_id: str) -> None:
    with store.session() as sess:
        app = _require(sess, app_id)
        runtime.remove(app_id, missing_ok=True)
        for deployment in store.deployments_for(sess, app_id):
            sess.delete(deployment)
        sess.delete(app)
        sess.commit()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _require(sess, app_id: str) -> store.App:
    app = store.get_app(sess, app_id)
    if app is None:
        raise HTTPException(404, f"no app with id {app_id}")
    return app


def _act(action, app_id: str) -> None:
    try:
        action(app_id)
    except runtime.DeployError as exc:
        raise HTTPException(409, str(exc)) from exc
