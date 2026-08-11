"""Execution backend backed by a local Docker daemon.

WARNING: Docker's default runtime shares the host kernel. The PRD specifies
gVisor/Kata, and that remains the real isolation boundary for untrusted code.
Setting HANGAR_RUNTIME=runsc routes containers through gVisor on a host where
it's installed; until then the hardening applied here (non-root, dropped
capabilities, read-only rootfs, no privilege escalation, resource caps) is
defence in depth rather than a substitute.
"""

from __future__ import annotations

from pathlib import Path

from .. import builder, runtime
from ..detect import Detection
from .base import (
    BuildResult,
    DeployError,
    ExecutionBackend,
    LogSink,
    ResourceLimits,
    RunningApp,
)


class DockerBackend(ExecutionBackend):
    name = "docker"

    def available(self) -> bool:
        try:
            runtime.client().ping()
            return True
        except Exception:
            return False

    def build(
        self,
        source_dir: str | Path,
        detection: Detection,
        tag: str,
        *,
        on_log: LogSink | None = None,
    ) -> BuildResult:
        return builder.build(source_dir, detection, tag, on_log=on_log)

    def run(
        self,
        image_tag: str,
        app_id: str,
        app_name: str,
        container_port: int,
        *,
        env: dict[str, str] | None = None,
        limits: ResourceLimits | None = None,
        volumes: dict[str, dict] | None = None,
    ) -> RunningApp:
        return runtime.run(
            image_tag,
            app_id=app_id,
            app_name=app_name,
            container_port=container_port,
            env=env,
            limits=limits,
            volumes=volumes,
        )

    def status(self, app_id: str) -> str:
        return runtime.status(app_id)

    def logs(self, app_id: str, *, tail: int = 200) -> str:
        return runtime.logs(app_id, tail=tail)

    def stop(self, app_id: str) -> None:
        runtime.stop(app_id)

    def restart(self, app_id: str) -> None:
        runtime.restart(app_id)

    def remove(self, app_id: str, *, missing_ok: bool = False) -> None:
        runtime.remove(app_id, missing_ok=missing_ok)

    def host_port(self, app_id: str) -> int | None:
        return runtime.host_port(app_id)

    def remove_data(self, app_id: str) -> bool:
        return runtime.remove_volume(app_id)


__all__ = ["DockerBackend", "DeployError"]
