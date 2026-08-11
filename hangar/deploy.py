"""Deploy orchestration: detect -> build -> run, with status transitions.

Runs on a background thread so the API can return an app id immediately rather
than holding a request open for the length of an image build. Every stage
writes its outcome to the store, so a caller polling GET /apps/{id} sees
queued -> building -> running (or failed, with the error attached).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import backends, config, database, ingest, routing, scan, store
from .backends import BackendError
from .database import DatabaseError
from .detect import DetectionError, detect
from .ingest import IngestError
from .routing import RoutingError
from .store import AppStatus, Deployment, DeploymentStatus, ScanStatus

log = logging.getLogger("hangar.deploy")


class ScanBlocked(Exception):
    """The security scan refused the deploy under a 'block' policy."""


def image_tag(app: store.App) -> str:
    # Lowercased because Docker rejects uppercase in repository names.
    return f"hangar/{app.name.lower()}:{app.id}"


def deploy(app_id: str) -> None:
    """Run a full deploy for ``app_id``. Safe to call on a background thread."""
    with store.session() as sess:
        app = store.get_app(sess, app_id)
        if app is None:
            log.warning("deploy requested for unknown app %s", app_id)
            return

        deployment = Deployment(app_id=app.id)
        app.status = AppStatus.BUILDING
        app.error = None
        store.save(sess, app, deployment)

        lines: list[str] = []

        def record(line: str) -> None:
            lines.append(line)

        try:
            source = _prepare_source(app, record)
            store.save(sess, app)
            backend = backends.get_backend()
            if not backend.available():
                raise BackendError(
                    f"the {backend.name} execution backend is unavailable — "
                    "is Docker running?"
                )

            detection = detect(source)
            record(f"detected {detection.runtime}/{detection.framework}")

            app.runtime = detection.runtime
            app.framework = detection.framework
            store.save(sess, app)

            # Scan before the build, not after: a build installs the app's
            # declared dependencies, which runs their setup code. By the time
            # an image exists, untrusted code has already had a turn.
            _scan(deployment, source, record)
            store.save(sess, deployment)
            if deployment.scan_status == ScanStatus.BLOCKED:
                raise ScanBlocked(deployment.error or "blocked by security scan")

            result = backend.build(source, detection, image_tag(app), on_log=record)

            # Provisioned after the build so a failing build doesn't create
            # storage, but before the run so DATABASE_URL exists at startup.
            provisioned = database.provision(sess, app)
            if provisioned.kind != "none":
                record(f"attached {provisioned.kind} database ({provisioned.connection_ref})")

            running = backend.run(
                result.image_tag,
                app_id=app.id,
                app_name=app.name,
                container_port=detection.port,
                env=provisioned.env,
                volumes=provisioned.volumes,
            )

            # Publishing the route is what makes the app shareable; without a
            # router this returns the direct host:port URL unchanged.
            url = routing.get_router().upsert(
                app_id=app.id,
                app_name=app.name,
                upstream=running.upstream,
                host_port=running.host_port,
            )
            record(f"running at {url}")

            deployment.status = DeploymentStatus.SUCCEEDED
            deployment.image_ref = result.image_tag
            deployment.build_log = "\n".join(lines)
            deployment.finished_at = store.utcnow()

            app.status = AppStatus.RUNNING
            app.url = url
            app.host_port = running.host_port
            app.upstream = running.upstream
            store.save(sess, app, deployment)
            log.info("app %s (%s) deployed to %s", app.name, app.id, url)

        except RoutingError as exc:
            # The container started but isn't reachable. Leaving it running
            # behind a failed app record wastes memory and confuses the next
            # deploy, so undo it.
            _discard_container(app.id, lines)
            _fail(sess, app, deployment, lines, str(exc))
        except (
            DetectionError,
            BackendError,
            ScanBlocked,
            IngestError,
            DatabaseError,
        ) as exc:
            _fail(sess, app, deployment, lines, str(exc))
        except Exception as exc:  # noqa: BLE001 - a crash here must not kill the thread
            log.exception("unexpected failure deploying %s", app_id)
            _fail(sess, app, deployment, lines, f"unexpected error: {exc}")


def _prepare_source(app, record) -> Path:
    """Make the app's source available locally, refreshing it where possible.

    Repo-backed apps re-fetch on every deploy, so "redeploy" means "deploy
    what's on the branch now" — which is what anyone pressing it expects.
    Uploaded zips can't be re-fetched, so the extracted copy is reused.
    """
    if app.source_type == "repo":
        record(f"fetching {app.source_ref}")
        repo = ingest.from_github(app.source_ref, app.id, app.source_revision)
        record(f"fetched {repo.slug}" + (f" at {repo.ref}" if repo.ref else ""))
        app.source_dir = str(ingest.source_dir_for(app.id))

    source = Path(app.source_dir or app.source_ref)
    if not source.is_dir():
        raise IngestError(f"source directory is missing: {source}")
    return source


def _scan(deployment, source: Path, record) -> None:
    """Run the security scan and record its verdict on the deployment."""
    settings = config.settings()
    if not settings.scan_enabled:
        deployment.scan_status = ScanStatus.SKIPPED
        record("security scan skipped (HANGAR_SCAN_POLICY=off)")
        return

    result = scan.scan(source)
    deployment.scan_report = json.dumps(result.as_dict())
    record(f"security scan: {result.summary()}")

    for note, reason in result.tools_skipped.items():
        record(f"  scanner unavailable: {note} ({reason})")
    for finding in result.findings[:20]:
        record(
            f"  [{finding.severity}] {finding.file}:{finding.line} "
            f"{finding.rule} — {finding.message}"
        )
    if len(result.findings) > 20:
        record(f"  ... and {len(result.findings) - 20} more")

    if not result.findings:
        deployment.scan_status = ScanStatus.CLEAN
        return

    blocking = result.at_or_above(settings.scan_block_severity)
    if settings.scan_policy == "block" and blocking:
        deployment.scan_status = ScanStatus.BLOCKED
        deployment.error = (
            f"blocked by security scan: {len(blocking)} finding(s) at or above "
            f"{settings.scan_block_severity} severity"
        )
        record(deployment.error)
        return

    deployment.scan_status = ScanStatus.FLAGGED


def _discard_container(app_id: str, lines: list[str]) -> None:
    """Best-effort teardown; a cleanup failure must not mask the real error."""
    try:
        backends.get_backend().remove(app_id, missing_ok=True)
        lines.append("removed the unreachable container")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"could not remove the unreachable container: {exc}")


def _fail(sess, app, deployment, lines: list[str], message: str) -> None:
    lines.append(f"ERROR: {message}")
    deployment.status = DeploymentStatus.FAILED
    deployment.error = message
    deployment.build_log = "\n".join(lines)
    deployment.finished_at = store.utcnow()

    app.status = AppStatus.FAILED
    app.error = message
    store.save(sess, app, deployment)
    log.warning("deploy failed for %s: %s", app.id, message)
