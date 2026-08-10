"""Hangar control plane API.

Routes under /apps require the shared bearer token from HANGAR_API_TOKEN
(see auth.py). That guards the control plane itself; per-user identity and
owner/editor/viewer permissions are still Milestone 3.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import backends, config, routing
from . import deploy as deploy_mod
from . import store
from .auth import require_token
from .backends import BackendError
from .routing import RoutingError
from .store import AppStatus

api = FastAPI(
    title="Hangar",
    description="Cloud for small software — deploy a generated app to a live URL.",
    version="0.1.0",
)

# Everything under /apps is authenticated. /healthz stays open so a platform
# health check or uptime pinger doesn't need the token.
apps = APIRouter(prefix="/apps", tags=["apps"], dependencies=[Depends(require_token)])

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


@api.get("/healthz", tags=["meta"])
def healthz() -> dict:
    """Unauthenticated health check, for platform probes and uptime pingers."""
    settings = config.settings()
    backend = backends.get_backend()
    router = routing.get_router()
    return {
        "status": "ok",
        "backend": backend.name,
        "backend_available": backend.available(),
        "router": router.name,
        "router_available": router.available(),
        "app_domain": settings.app_domain,
        "auth": "enabled" if settings.auth_enabled else "disabled",
        "sandbox_runtime": settings.sandbox_runtime or "docker-default",
    }


@apps.post("", response_model=AppView, status_code=202)
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


@apps.get("", response_model=list[AppView])
def list_apps() -> list[AppView]:
    with store.session() as sess:
        return [AppView.of(app) for app in store.list_apps(sess)]


@apps.get("/{app_id}", response_model=AppView)
def get_app(app_id: str) -> AppView:
    with store.session() as sess:
        return AppView.of(_require(sess, app_id))


@apps.get("/{app_id}/logs", response_model=LogsView)
def get_logs(app_id: str, tail: int = 200) -> LogsView:
    with store.session() as sess:
        app = _require(sess, app_id)
        deployment = store.latest_deployment(sess, app_id)
        build_log = deployment.build_log if deployment else ""

    try:
        runtime_log = backends.get_backend().logs(app_id, tail=tail)
    except BackendError:
        # No container yet (or already removed) — the build log still matters.
        runtime_log = ""

    return LogsView(app_id=app.id, build_log=build_log, runtime_log=runtime_log)


@apps.post("/{app_id}/redeploy", response_model=AppView, status_code=202)
def redeploy(app_id: str, background: BackgroundTasks) -> AppView:
    with store.session() as sess:
        app = _require(sess, app_id)
        app.status = AppStatus.QUEUED
        app.error = None
        store.save(sess, app)
        view = AppView.of(app)

    background.add_task(deploy_mod.deploy, app_id)
    return view


@apps.post("/{app_id}/stop", response_model=AppView)
def stop_app(app_id: str) -> AppView:
    with store.session() as sess:
        app = _require(sess, app_id)
        _act(backends.get_backend().stop, app_id)
        # Withdraw the route too, so the hostname fails cleanly instead of
        # proxying to a dead port.
        _act(routing.get_router().remove, app_id)
        app.status = AppStatus.STOPPED
        app.url = None
        store.save(sess, app)
        return AppView.of(app)


@apps.post("/{app_id}/restart", response_model=AppView)
def restart_app(app_id: str) -> AppView:
    backend = backends.get_backend()
    with store.session() as sess:
        app = _require(sess, app_id)
        _act(backend.restart, app_id)

        # The published port can change across a restart, so re-read it from
        # the backend rather than trusting what was stored at deploy time.
        port = backend.host_port(app_id)
        if port is not None:
            app.host_port = port
            # Re-point the route at the new port; the hostname is unchanged,
            # which is the whole benefit of routing by name.
            try:
                app.url = routing.get_router().upsert(
                    app_id=app.id, app_name=app.name, host_port=port
                )
            except RoutingError as exc:
                raise HTTPException(409, str(exc)) from exc

        app.status = AppStatus.RUNNING
        store.save(sess, app)
        return AppView.of(app)


@apps.delete("/{app_id}", status_code=204)
def delete_app(app_id: str) -> None:
    with store.session() as sess:
        app = _require(sess, app_id)
        backends.get_backend().remove(app_id, missing_ok=True)
        routing.get_router().remove(app_id, missing_ok=True)
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
    except (BackendError, RoutingError) as exc:
        raise HTTPException(409, str(exc)) from exc


api.include_router(apps)
