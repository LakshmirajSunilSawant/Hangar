"""Hangar control plane API.

Callers are either the shared admin token (scripts, CI) or a signed-in user
scoped to their owner/editor/viewer grants — see auth.py for how that is
resolved and permissions.py for what each role may do.

/internal/authorize is the forward-auth endpoint the proxy in front of
deployed apps calls, which is what makes authentication platform-level rather
than something every app has to implement (PRD §8).
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import backends, config, database, identity, idle, ingest, metrics, routing
from . import deploy as deploy_mod
from . import store
from .auth import authorize, current_principal, require_admin, require_token
from .backends import BackendError
from .database import DatabaseError
from .ingest import IngestError
from .routing import RoutingError
from .identity import Principal
from .permissions import Action
from .routes_auth import (
    GrantRequest,
    GrantView,
    auth_router,
    grant_view,
    users_router,
    validate_role,
)
from .store import AppStatus, Permission, Role

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own the background threads that outlive a single request.

    The idle reaper and the metrics collector. Each declines to start when its
    feature is switched off, which is why a default configuration runs no
    background threads at all.
    """
    idle.REAPER.start()
    metrics.COLLECTOR.start()
    try:
        yield
    finally:
        idle.REAPER.stop()
        metrics.COLLECTOR.stop()


api = FastAPI(
    title="Hangar",
    description="Cloud for small software — deploy a generated app to a live URL.",
    version="0.1.0",
    lifespan=lifespan,
)

# Everything under /apps is authenticated. /healthz stays open so a platform
# health check or uptime pinger doesn't need the token.
apps = APIRouter(prefix="/apps", tags=["apps"], dependencies=[Depends(require_token)])

log = logging.getLogger("hangar.api")

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
    database: str | None = Field(
        default=None,
        description="Per-app database: none, sqlite, or postgres. Defaults to HANGAR_APP_DB.",
        examples=["sqlite"],
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
    database: str | None = None
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
            database=app.db_type,
            error=app.error,
            created_at=app.created_at.isoformat(),
            updated_at=app.updated_at.isoformat(),
        )


class LogsView(BaseModel):
    app_id: str
    build_log: str
    runtime_log: str


class MetricsView(BaseModel):
    app_id: str
    # Seconds between samples, so the UI can label the axis without guessing.
    interval: int
    window_minutes: float
    memory_limit_mb: float
    cpu_limit: float
    current: dict | None = None
    samples: list[dict] = Field(default_factory=list)


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
        "idle_timeout": settings.idle_timeout,
        "idle_reaper": idle.REAPER.running,
        "metrics": metrics.COLLECTOR.running,
    }


@api.get("/internal/authorize", tags=["meta"], include_in_schema=False)
def authorize_app_request(request: Request, response: Response):
    """Forward-auth endpoint for the proxy in front of deployed apps.

    PRD §8: "recipients authenticate to [the platform] before ever reaching the
    app's own routes, via a proxy layer that injects identity headers." Caddy
    asks this endpoint about every request; a 2xx lets it through and the
    response headers are copied onto the upstream request, so the app learns
    who the user is without implementing any auth itself.

    Deliberately unauthenticated as a route: it *is* the authentication check,
    and answering 401 is a normal outcome rather than an error.

    It is also where scale-to-zero happens. Waking is done *after* the access
    check, so an anonymous stranger cannot start every app on the box by
    walking the hostnames.
    """
    settings = config.settings()
    hostname = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    ).split(":")[0]

    with store.session() as sess:
        app = _app_for_hostname(sess, hostname)
        if app is None:
            raise HTTPException(404, f"no app is served at {hostname!r}")
        app_id, sleeping = app.id, app.status == AppStatus.SLEEPING.value

        if settings.require_app_auth:
            user = identity.resolve_session(
                sess, request.cookies.get(identity.SESSION_COOKIE, "")
            )
            if user is None:
                # 401 rather than a redirect: the proxy decides how to present
                # a login, and an API client behind the same hostname needs a
                # status it can act on.
                raise HTTPException(
                    401,
                    "sign in to reach this app",
                    headers={"WWW-Authenticate": "Cookie"},
                )

            role = None if user.is_admin else _role_for_user(sess, app.id, user.id)
            if not user.is_admin and role is None:
                raise HTTPException(403, "you do not have access to this app")

            # Headers the app can trust, because it can only be reached through
            # this proxy — see the note in routing.py about binding apps to the
            # proxy's network.
            response.headers["X-Hangar-User"] = user.email
            response.headers["X-Hangar-User-Id"] = user.id
            response.headers["X-Hangar-Role"] = (
                "owner" if user.is_admin else role.value
            )

    # Outside the session: waking opens its own, and holding this one across a
    # container start would pin a connection for the length of the wake.
    if settings.idle_enabled:
        idle.TRACKER.touch(app_id)
        if sleeping:
            # Returns as soon as the container is started, not when the app is
            # listening — Caddy retries the upstream for HANGAR_WAKE_TIMEOUT,
            # and it is the only component on the app's network.
            idle.wake(app_id)
    return {"ok": True}


def _app_for_hostname(sess, hostname: str):
    """Find the app a request hostname belongs to."""
    if not hostname:
        return None
    settings = config.settings()
    if not settings.app_domain:
        return None

    suffix = "." + settings.app_domain.strip(".")
    if not hostname.endswith(suffix):
        return None
    return store.app_by_name(sess, hostname[: -len(suffix)])


def _role_for_user(sess, app_id: str, user_id: str):
    permission = store.permission_for(sess, app_id, user_id)
    return Role(permission.role) if permission else None


@apps.post("", response_model=AppView, status_code=202)
def create_app(
    request: CreateAppRequest,
    background: BackgroundTasks,
    principal: Principal = Depends(require_admin),
) -> AppView:
    """Register an app and kick off a deploy.

    Returns 202 immediately — the build takes far longer than a request should.
    Poll GET /apps/{id} for the outcome.
    """
    name = _validate_name(request.name)

    if bool(request.source_path) == bool(request.repo_url):
        raise HTTPException(
            422, "provide exactly one of source_path or repo_url"
        )

    db_type = _validate_database(request.database)

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
        app = store.App(name=name, db_type=db_type, **fields)
        store.save(sess, app)
        _make_owner(sess, principal, app.id)
        view = AppView.of(app)

    background.add_task(deploy_mod.deploy, view.id)
    return view


@apps.post("/upload", response_model=AppView, status_code=202)
async def upload_app(
    background: BackgroundTasks,
    name: str = Form(description="Lowercase app name."),
    file: UploadFile = File(description="Zip archive of the app's source."),
    principal: Principal = Depends(require_admin),
    # Aliased so the wire name stays "database" without shadowing the module.
    db_choice: str | None = Form(
        default=None, alias="database", description="none, sqlite, or postgres."
    ),
) -> AppView:
    """Register an app from an uploaded zip and kick off a deploy."""
    app_name = _validate_name(name)
    db_type = _validate_database(db_choice)

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
            db_type=db_type,
        )
        store.save(sess, app)
        _make_owner(sess, principal, app.id)
        view = AppView.of(app)

    background.add_task(deploy_mod.deploy, view.id)
    return view


@apps.get("", response_model=list[AppView])
def list_apps(principal: Principal = Depends(current_principal)) -> list[AppView]:
    """Only apps the caller has been granted access to."""
    with store.session() as sess:
        if principal.kind in ("admin", "anonymous") or principal.is_admin:
            rows = store.list_apps(sess)
        else:
            rows = store.apps_visible_to(sess, principal.user_id)
        return [AppView.of(app) for app in rows]


@apps.get("/{app_id}", response_model=AppView)
def get_app(
    app_id: str, principal: Principal = Depends(current_principal)
) -> AppView:
    with store.session() as sess:
        app = _require(sess, app_id)
        authorize(sess, principal, app_id, Action.VIEW)
        return AppView.of(app)


@apps.get("/{app_id}/logs", response_model=LogsView)
def get_logs(
    app_id: str, tail: int = 200, principal: Principal = Depends(current_principal)
) -> LogsView:
    with store.session() as sess:
        app = _require(sess, app_id)
        # Logs can contain anything the app printed, so viewers are excluded.
        authorize(sess, principal, app_id, Action.VIEW_LOGS)
        deployment = store.latest_deployment(sess, app_id)
        build_log = deployment.build_log if deployment else ""

    try:
        runtime_log = backends.get_backend().logs(app_id, tail=tail)
    except BackendError:
        # No container yet (or already removed) — the build log still matters.
        runtime_log = ""

    return LogsView(app_id=app.id, build_log=build_log, runtime_log=runtime_log)


@apps.get("/{app_id}/metrics", response_model=MetricsView)
def get_metrics(
    app_id: str, principal: Principal = Depends(current_principal)
) -> MetricsView:
    """CPU and memory over the recent past, against the app's own caps.

    VIEW rather than VIEW_LOGS: how much memory an app is using says nothing
    about what it printed, and knowing your own tool is about to be OOM-killed
    is exactly the thing a viewer needs to be able to see.
    """
    settings = config.settings()
    with store.session() as sess:
        _require(sess, app_id)
        authorize(sess, principal, app_id, Action.VIEW)

    samples = metrics.HISTORY.samples(app_id)
    latest = samples[-1] if samples else None
    return MetricsView(
        app_id=app_id,
        interval=settings.metrics_interval,
        window_minutes=settings.metrics_window_minutes,
        # From configuration, not from the samples: an app with no readings yet
        # still has a cap, and showing it is how "0 of 512MB" reads correctly.
        memory_limit_mb=float(settings.memory_mb),
        cpu_limit=settings.cpus,
        current=latest.as_dict() if latest else None,
        samples=[sample.as_dict() for sample in samples],
    )


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


@apps.get("/{app_id}/access", response_model=list[GrantView])
def list_access(
    app_id: str, principal: Principal = Depends(current_principal)
) -> list[GrantView]:
    """Who can reach this app, and what they may do."""
    with store.session() as sess:
        _require(sess, app_id)
        authorize(sess, principal, app_id, Action.SHARE)

        grants = []
        for permission in store.permissions_for_app(sess, app_id):
            user = store.get_user(sess, permission.user_id)
            if user is not None:
                grants.append(grant_view(user, permission))
        return grants


@apps.put("/{app_id}/access", response_model=GrantView)
def grant_access(
    app_id: str,
    request: GrantRequest,
    principal: Principal = Depends(current_principal),
) -> GrantView:
    """Share an app with someone, or change what they may do.

    The person must already have been invited — this grants access to an app,
    it does not create accounts, so a typo in an email cannot silently hand
    access to a stranger who later registers that address.
    """
    role = validate_role(request.role)

    with store.session() as sess:
        _require(sess, app_id)
        authorize(sess, principal, app_id, Action.SHARE)

        user = store.user_by_email(sess, request.email)
        if user is None:
            raise HTTPException(
                404,
                f"no user with email {request.email!r} — invite them first "
                "(POST /users)",
            )

        permission = store.permission_for(sess, app_id, user.id)
        if permission is None:
            permission = Permission(app_id=app_id, user_id=user.id, role=role)
        else:
            permission.role = role

        sess.add(permission)
        sess.commit()
        sess.refresh(permission)
        return grant_view(user, permission)


@apps.delete("/{app_id}/access/{user_id}", status_code=204)
def revoke_access(
    app_id: str, user_id: str, principal: Principal = Depends(current_principal)
) -> None:
    with store.session() as sess:
        _require(sess, app_id)
        authorize(sess, principal, app_id, Action.SHARE)

        permission = store.permission_for(sess, app_id, user_id)
        if permission is None:
            raise HTTPException(404, "that user has no access to this app")

        # Removing the last owner would leave an app nobody can share or
        # delete, recoverable only with the admin token.
        if permission.role == Role.OWNER:
            owners = [
                p
                for p in store.permissions_for_app(sess, app_id)
                if p.role == Role.OWNER
            ]
            if len(owners) <= 1:
                raise HTTPException(
                    409, "an app must keep at least one owner — grant another first"
                )

        sess.delete(permission)
        sess.commit()


@apps.post("/{app_id}/redeploy", response_model=AppView, status_code=202)
def redeploy(
    app_id: str,
    background: BackgroundTasks,
    principal: Principal = Depends(current_principal),
) -> AppView:
    with store.session() as sess:
        app = _require(sess, app_id)
        authorize(sess, principal, app_id, Action.DEPLOY)
        app.status = AppStatus.QUEUED
        app.error = None
        store.save(sess, app)
        view = AppView.of(app)

    background.add_task(deploy_mod.deploy, app_id)
    return view


@apps.post("/{app_id}/stop", response_model=AppView)
def stop_app(
    app_id: str, principal: Principal = Depends(current_principal)
) -> AppView:
    with store.session() as sess:
        app = _require(sess, app_id)
        authorize(sess, principal, app_id, Action.DEPLOY)
        _act(backends.get_backend().stop, app_id)
        # Withdraw the route too, so the hostname fails cleanly instead of
        # proxying to a dead port.
        _act(routing.get_router().remove, app_id)
        app.status = AppStatus.STOPPED
        app.url = None
        store.save(sess, app)
        # A deliberate stop is not idleness. Dropping the last-seen time keeps
        # the reaper from reasoning about an app it must not touch.
        idle.TRACKER.forget(app_id)
        return AppView.of(app)


@apps.post("/{app_id}/sleep", response_model=AppView)
def sleep_app(
    app_id: str, principal: Principal = Depends(current_principal)
) -> AppView:
    """Stop a running app now, without waiting out its idle timeout.

    The route stays published and the URL keeps working — that is the whole
    difference from `/stop`, and the reason this is worth its own endpoint
    rather than being an operator-only detail of the reaper.
    """
    settings = config.settings()
    if not settings.idle_enabled:
        raise HTTPException(
            409,
            "scale-to-zero is off, so a slept app would have nothing to wake "
            "it — set HANGAR_IDLE_TIMEOUT, or use /stop.",
        )

    with store.session() as sess:
        app = _require(sess, app_id)
        authorize(sess, principal, app_id, Action.DEPLOY)
        if app.status != AppStatus.RUNNING.value:
            raise HTTPException(409, f"{app.name} is {app.status}, not running")

    if not idle.put_to_sleep(app_id):
        raise HTTPException(502, "could not stop the app's container")

    with store.session() as sess:
        return AppView.of(_require(sess, app_id))


@apps.post("/{app_id}/wake", response_model=AppView)
def wake_app(
    app_id: str, principal: Principal = Depends(current_principal)
) -> AppView:
    """Start a sleeping app without waiting for someone to visit it.

    The proxy does this by itself on the first request; this is for the
    dashboard, and for measuring wake time without a browser in the way.
    """
    with store.session() as sess:
        app = _require(sess, app_id)
        authorize(sess, principal, app_id, Action.DEPLOY)
        if app.status != AppStatus.SLEEPING.value:
            raise HTTPException(409, f"{app.name} is {app.status}, not sleeping")

    if not idle.wake(app_id):
        raise HTTPException(502, "could not start the app's container")

    with store.session() as sess:
        return AppView.of(_require(sess, app_id))


@apps.post("/{app_id}/restart", response_model=AppView)
def restart_app(
    app_id: str, principal: Principal = Depends(current_principal)
) -> AppView:
    backend = backends.get_backend()
    with store.session() as sess:
        app = _require(sess, app_id)
        authorize(sess, principal, app_id, Action.DEPLOY)
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
        # Restarting counts as use; without this the reaper could sleep an app
        # seconds after someone deliberately brought it back.
        idle.TRACKER.touch(app_id)
        return AppView.of(app)


@apps.delete("/{app_id}", status_code=204)
def delete_app(
    app_id: str,
    keep_data: bool = False,
    principal: Principal = Depends(current_principal),
) -> None:
    """Remove the app.

    Its database goes with it unless `keep_data=true`, since storage scoped to
    a deleted app helps nobody and would accumulate silently. That is
    irreversible, so it is worth saying plainly rather than burying.
    """
    with store.session() as sess:
        app = _require(sess, app_id)
        authorize(sess, principal, app_id, Action.DELETE)
        backends.get_backend().remove(app_id, missing_ok=True)
        routing.get_router().remove(app_id, missing_ok=True)

        if not keep_data:
            try:
                database.deprovision(sess, app_id)
                backends.get_backend().remove_data(app_id)
            except (DatabaseError, BackendError) as exc:
                # The app is going away regardless; a stuck volume shouldn't
                # leave an undeletable record behind.
                log.warning("could not remove data for %s: %s", app_id, exc)

        # Extracted zip and repo sources are Hangar's to clean up; a
        # source_path app's directory belongs to the user and is left alone.
        if app.source_type in ("zip", "repo"):
            ingest.discard_source(app_id)
        for deployment in store.deployments_for(sess, app_id):
            sess.delete(deployment)
        sess.delete(app)
        sess.commit()
        idle.TRACKER.forget(app_id)
        metrics.HISTORY.forget(app_id)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _require(sess, app_id: str) -> store.App:
    app = store.get_app(sess, app_id)
    if app is None:
        raise HTTPException(404, f"no app with id {app_id}")
    return app


def _make_owner(sess, principal: Principal, app_id: str) -> None:
    """Whoever created an app owns it.

    The shared admin token belongs to no user, so there is nobody to record;
    it is treated as owner everywhere anyway (see auth.role_for).
    """
    if principal.user_id is None:
        return
    sess.add(Permission(app_id=app_id, user_id=principal.user_id, role=Role.OWNER))
    sess.commit()


def _validate_name(name: str) -> str:
    cleaned = name.strip().lower()
    if not NAME_PATTERN.match(cleaned):
        raise HTTPException(
            422,
            "name must be 3-40 characters of lowercase letters, digits, or hyphens, "
            "and start and end with a letter or digit",
        )
    return cleaned


def _validate_database(choice: str | None) -> str | None:
    """None means "whatever the server is configured to default to"."""
    if choice is None:
        return None
    if choice not in ("none", "sqlite", "postgres"):
        raise HTTPException(
            422, "database must be one of: none, sqlite, postgres"
        )
    return choice


def _reject_duplicate(sess, name: str) -> None:
    if store.app_by_name(sess, name) is not None:
        raise HTTPException(409, f"an app named '{name}' already exists")


def _act(action, app_id: str) -> None:
    try:
        action(app_id)
    except (BackendError, RoutingError) as exc:
        raise HTTPException(409, str(exc)) from exc


api.include_router(auth_router)
api.include_router(users_router)
api.include_router(apps)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


def dashboard_dir() -> Path:
    override = os.environ.get("HANGAR_DASHBOARD_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "dashboard" / "dist"


def mount_dashboard(app: FastAPI = api) -> bool:
    """Serve the built dashboard, if it has been built.

    Mounted last so it never shadows the API: Starlette matches routes in
    registration order, and this one matches everything.

    The dashboard is unauthenticated on purpose — it is a static bundle with no
    secrets in it, and it asks the user for the API token, which every request
    it makes then carries.
    """
    directory = dashboard_dir()
    if not (directory / "index.html").is_file():
        return False

    app.mount("/", StaticFiles(directory=str(directory), html=True), name="dashboard")
    return True


mount_dashboard()
