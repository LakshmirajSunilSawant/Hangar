"""Hangar control plane API.

Routes under /apps require the shared bearer token from HANGAR_API_TOKEN
(see auth.py). That guards the control plane itself; per-user identity and
owner/editor/viewer permissions are still Milestone 3.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel, Field

from . import backends, config, ingest, routing
from . import deploy as deploy_mod
from . import store
from .auth import require_token
from .backends import BackendError
from .ingest import IngestError
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
    source_path: str | None = Field(
        default=None,
        description="Absolute path to the app's source directory on this host.",
    )
    repo_url: str | None = Field(
        default=None,
        description="GitHub repository — https://github.com/owner/repo or owner/repo.",
        examples=["https://github.com/owner/repo"],
    )
    ref: str | None = Field(
        default=None,
        description="Branch, tag, or commit. Defaults to the repo's default branch.",
    )


class AppView(BaseModel):
    id: str
    name: str
    status: str
    url: str | None = None
    runtime: str | None = None
    framework: str | None = None
    source_type: str
    source_ref: str
    source_revision: str | None = None
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
            source_type=app.source_type,
            source_ref=app.source_ref,
            source_revision=app.source_revision,
            error=app.error,
            created_at=app.created_at.isoformat(),
            updated_at=app.updated_at.isoformat(),
        )


class LogsView(BaseModel):
    app_id: str
    build_log: str
    runtime_log: str


class ScanView(BaseModel):
    app_id: str
    status: str
    policy: str
    counts: dict[str, int] = Field(default_factory=dict)
    highest_severity: str | None = None
    findings: list[dict] = Field(default_factory=list)
    tools_run: list[str] = Field(default_factory=list)
    tools_skipped: dict[str, str] = Field(default_factory=dict)


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
    name = _validate_name(request.name)

    if bool(request.source_path) == bool(request.repo_url):
        raise HTTPException(
            422, "provide exactly one of source_path or repo_url"
        )

    if request.source_path:
        source = Path(request.source_path).expanduser()
        if not source.is_absolute():
            raise HTTPException(422, "source_path must be an absolute path")
        if not source.is_dir():
            raise HTTPException(422, f"source_path is not a directory: {source}")
        fields = dict(
            source_type="path",
            source_ref=str(source.resolve()),
            source_dir=str(source.resolve()),
        )
    else:
        # Parsed up front so a malformed URL is a 422 now rather than a failed
        # deploy discovered by polling later.
        try:
            repo = ingest.parse_repo(request.repo_url, request.ref)
        except IngestError as exc:
            raise HTTPException(422, str(exc)) from exc
        fields = dict(
            source_type="repo",
            source_ref=repo.slug,
            source_revision=repo.ref,
        )

    with store.session() as sess:
        _reject_duplicate(sess, name)
        app = store.App(name=name, **fields)
        store.save(sess, app)
        view = AppView.of(app)

    background.add_task(deploy_mod.deploy, view.id)
    return view


@apps.post("/upload", response_model=AppView, status_code=202)
async def upload_app(
    background: BackgroundTasks,
    name: str = Form(description="Lowercase app name."),
    file: UploadFile = File(description="Zip archive of the app's source."),
) -> AppView:
    """Register an app from an uploaded zip and kick off a deploy."""
    app_name = _validate_name(name)

    with store.session() as sess:
        _reject_duplicate(sess, app_name)

    data = await file.read()

    # The app id names the extraction directory, so it has to exist before the
    # archive is unpacked.
    app_id = store.new_id()
    try:
        extracted = ingest.from_zip(data, app_id)
    except IngestError as exc:
        raise HTTPException(422, str(exc)) from exc

    with store.session() as sess:
        _reject_duplicate(sess, app_name)
        app = store.App(
            id=app_id,
            name=app_name,
            source_type="zip",
            source_ref=file.filename or "upload.zip",
            source_dir=str(extracted),
        )
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


@apps.get("/{app_id}/scan", response_model=ScanView)
def get_scan(app_id: str) -> ScanView:
    """Security findings from the most recent deployment's pre-execution scan."""
    with store.session() as sess:
        _require(sess, app_id)
        deployment = store.latest_deployment(sess, app_id)
        if deployment is None:
            raise HTTPException(404, f"app {app_id} has not been deployed yet")

        report = deployment.scan()
        return ScanView(
            app_id=app_id,
            status=deployment.scan_status,
            policy=config.settings().scan_policy,
            counts=report.get("counts", {}),
            highest_severity=report.get("highest_severity"),
            findings=report.get("findings", []),
            tools_run=report.get("tools_run", []),
            tools_skipped=report.get("tools_skipped", {}),
        )


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
        # With egress denied there is no published port and the container-name
        # upstream is stable, so the stored value stands.
        port = backend.host_port(app_id)
        if port is not None:
            app.host_port = port
            app.upstream = f"{config.settings().upstream_host}:{port}"

        # Re-point the route; the hostname is unchanged, which is the whole
        # benefit of routing by name.
        try:
            app.url = routing.get_router().upsert(
                app_id=app.id,
                app_name=app.name,
                upstream=app.upstream or "",
                host_port=app.host_port,
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
        # Extracted zip and repo sources are Hangar's to clean up; a
        # source_path app's directory belongs to the user and is left alone.
        if app.source_type in ("zip", "repo"):
            ingest.discard_source(app_id)
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


def _validate_name(name: str) -> str:
    cleaned = name.strip().lower()
    if not NAME_PATTERN.match(cleaned):
        raise HTTPException(
            422,
            "name must be 3-40 characters of lowercase letters, digits, or hyphens, "
            "and start and end with a letter or digit",
        )
    return cleaned


def _reject_duplicate(sess, name: str) -> None:
    if store.app_by_name(sess, name) is not None:
        raise HTTPException(409, f"an app named '{name}' already exists")


def _act(action, app_id: str) -> None:
    try:
        action(app_id)
    except (BackendError, RoutingError) as exc:
        raise HTTPException(409, str(exc)) from exc


api.include_router(apps)
