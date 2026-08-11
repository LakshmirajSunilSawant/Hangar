"""The execution backend interface.

Everything Hangar needs from "a thing that can build and run apps" lives here.
Nothing in this module imports Docker, so a backend that talks to a remote
runner over HTTP — or to a hosting provider's API — implements the same
surface without dragging the local Docker SDK along.

The PRD's target architecture puts execution on a separate box from the
control plane (gVisor sandboxes on an always-on VM, control plane wherever
convenient). This interface is the seam that makes that a configuration
change rather than a rewrite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..detect import Detection

LogSink = Callable[[str], None]


class BackendError(Exception):
    """Any failure from an execution backend."""


class BuildFailed(BackendError):
    """The image could not be built."""


class DeployError(BackendError):
    """The app could not be started or acted on."""


@dataclass
class ResourceLimits:
    """Per-app caps, so one runaway app can't degrade the platform (PRD §8)."""

    memory_mb: int
    cpus: float
    pids: int

    @classmethod
    def from_settings(cls, settings) -> "ResourceLimits":
        return cls(
            memory_mb=settings.memory_mb, cpus=settings.cpus, pids=settings.pids
        )


@dataclass
class BuildResult:
    image_tag: str
    dockerfile: str
    log: str
    used_existing_dockerfile: bool


@dataclass
class RunningApp:
    container_id: str
    container_name: str
    container_port: int
    # None when egress is denied: an app on an internal network cannot publish
    # a port to the host, so it is reachable only through the proxy.
    host_port: int | None
    # Address the router should dial to reach this app.
    upstream: str
    status: str


class ExecutionBackend(ABC):
    """Builds images and runs apps somewhere."""

    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """Whether this backend can currently do work.

        Used to fail a deploy with a clear message instead of a stack trace
        when, say, the Docker daemon isn't running.
        """

    @abstractmethod
    def build(
        self,
        source_dir: str | Path,
        detection: Detection,
        tag: str,
        *,
        on_log: LogSink | None = None,
    ) -> BuildResult:
        ...

    @abstractmethod
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
        """Start the app.

        ``volumes`` maps a backend-specific volume name to a mount spec. It is
        how an app persists anything at all, since the root filesystem is
        read-only.
        """

    @abstractmethod
    def status(self, app_id: str) -> str:
        """Current state, or "absent" when nothing is deployed for this app."""

    @abstractmethod
    def logs(self, app_id: str, *, tail: int = 200) -> str:
        ...

    @abstractmethod
    def stop(self, app_id: str) -> None:
        ...

    @abstractmethod
    def restart(self, app_id: str) -> None:
        ...

    @abstractmethod
    def remove(self, app_id: str, *, missing_ok: bool = False) -> None:
        ...

    @abstractmethod
    def host_port(self, app_id: str) -> int | None:
        """Published port, re-read from the backend rather than from the store."""

    def remove_data(self, app_id: str) -> bool:
        """Destroy the app's persistent storage. Irreversible.

        Not abstract: a backend with no notion of volumes is entitled to do
        nothing here, and should not be forced to implement a no-op.
        """
        return False
