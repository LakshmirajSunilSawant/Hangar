"""Deploy orchestration: detect -> build -> run, with status transitions.

Runs on a background thread so the API can return an app id immediately rather
than holding a request open for the length of an image build. Every stage
writes its outcome to the store, so a caller polling GET /apps/{id} sees
queued -> building -> running (or failed, with the error attached).
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import backends, store
from .backends import BackendError
from .detect import DetectionError, detect
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
            record(f"running at {running.url}")

            deployment.status = DeploymentStatus.SUCCEEDED
            deployment.image_ref = result.image_tag
            deployment.build_log = "\n".join(lines)
            deployment.finished_at = store.utcnow()

            app.status = AppStatus.RUNNING
            app.url = running.url
            app.host_port = running.host_port
            store.save(sess, app, deployment)
            log.info("app %s (%s) deployed to %s", app.name, app.id, running.url)

        except (DetectionError, BackendError) as exc:
            _fail(sess, app, deployment, lines, str(exc))
        except Exception as exc:  # noqa: BLE001 - a crash here must not kill the thread
            log.exception("unexpected failure deploying %s", app_id)
            _fail(sess, app, deployment, lines, f"unexpected error: {exc}")


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
