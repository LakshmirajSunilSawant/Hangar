"""Execution backends — the seam between the control plane and whatever runs apps."""

from __future__ import annotations

from .. import config
from .base import (
    BackendError,
    BuildFailed,
    BuildResult,
    DeployError,
    ExecutionBackend,
    ResourceLimits,
    RunningApp,
)

_BACKENDS: dict[str, type[ExecutionBackend]] = {}


def register(name: str, backend: type[ExecutionBackend]) -> None:
    _BACKENDS[name] = backend


def get_backend(name: str | None = None) -> ExecutionBackend:
    """Instantiate the configured backend."""
    name = name or config.settings().backend
    _load_builtin()
    if name not in _BACKENDS:
        known = ", ".join(sorted(_BACKENDS)) or "none"
        raise BackendError(f"unknown execution backend {name!r} (known: {known})")
    return _BACKENDS[name]()


def _load_builtin() -> None:
    # Imported lazily so that a control plane configured for a remote runner
    # doesn't need the Docker SDK importable.
    if "docker" not in _BACKENDS:
        from .docker_backend import DockerBackend

        register("docker", DockerBackend)


__all__ = [
    "BackendError",
    "BuildFailed",
    "BuildResult",
    "DeployError",
    "ExecutionBackend",
    "ResourceLimits",
    "RunningApp",
    "get_backend",
    "register",
]
