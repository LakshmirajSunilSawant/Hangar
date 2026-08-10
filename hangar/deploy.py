"""Deploy orchestration: detect -> build -> run, with status transitions.

Runs on a background thread so the API can return an app id immediately rather
than holding a request open for the length of an image build. Every stage
writes its outcome to the store, so a caller polling GET /apps/{id} sees
queued -> building -> running (or failed, with the error attached).
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import backends, routing, store
from .backends import BackendError
from .detect import DetectionError, detect
from .routing import RoutingError
from .store import AppStatus, Deployment, DeploymentStatus

log = logging.getLogger("hangar.deploy")


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

        source = Path(app.source_ref)
        lines: list[str] = []

        def record(line: str) -> None:
            lines.append(line)

        try:
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

            result = backend.build(source, detection, image_tag(app), on_log=record)

            running = backend.run(
                result.image_tag,
                app_id=app.id,
                app_name=app.name,
                container_port=detection.port,
            )

            # Publishing the route is what makes the app shareable; without a
            # router this returns the direct host:port URL unchanged.
            url = routing.get_router().upsert(
                app_id=app.id, app_name=app.name, host_port=running.host_port
            )
            record(f"running at {url}")

            deployment.status = DeploymentStatus.SUCCEEDED
            deployment.image_ref = result.image_tag
            deployment.build_log = "\n".join(lines)
            deployment.finished_at = store.utcnow()

            app.status = AppStatus.RUNNING
            app.url = url
            app.host_port = running.host_port
            store.save(sess, app, deployment)
            log.info("app %s (%s) deployed to %s", app.name, app.id, running.url)

        except RoutingError as exc:
            # The container started but isn't reachable. Leaving it running
            # behind a failed app record wastes memory and confuses the next
            # deploy, so undo it.
            _discard_container(app.id, lines)
            _fail(sess, app, deployment, lines, str(exc))
        except (DetectionError, BackendError) as exc:
            _fail(sess, app, deployment, lines, str(exc))
        except Exception as exc:  # noqa: BLE001 - a crash here must not kill the thread
            log.exception("unexpected failure deploying %s", app_id)
            _fail(sess, app, deployment, lines, f"unexpected error: {exc}")


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
