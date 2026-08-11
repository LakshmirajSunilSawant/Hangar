"""Deploy engine — running built images as sandboxed app containers.

WARNING: this uses Docker's default runtime, which shares the host kernel.
The PRD specifies gVisor/Kata, and that remains the real isolation boundary for
untrusted code. The hardening here (non-root, dropped capabilities, read-only
root filesystem, no privilege escalation, resource caps) is defence in depth on
top of that boundary, not a substitute for it. Set HANGAR_RUNTIME=runsc once
gVisor is available on the host and it will be used automatically.
"""

from __future__ import annotations

import socket
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from . import config
from .backends.base import DeployError, ResourceLimits, RunningApp

LABEL_MANAGED = "hangar.managed"
LABEL_APP_ID = "hangar.app_id"
LABEL_APP_NAME = "hangar.app_name"

__all__ = [
    "DeployError",
    "ResourceLimits",
    "RunningApp",
    "client",
    "host_port",
    "logs",
    "remove",
    "restart",
    "run",
    "status",
    "stop",
]


def client() -> docker.DockerClient:
    try:
        return docker.from_env()
    except DockerException as exc:
        raise DeployError(
            "could not reach the Docker daemon — is Docker running?"
        ) from exc


def run(
    image_tag: str,
    app_id: str,
    app_name: str,
    container_port: int,
    *,
    env: dict[str, str] | None = None,
    limits: ResourceLimits | None = None,
    volumes: dict[str, dict] | None = None,
    host_port: int | None = None,
    docker_client: docker.DockerClient | None = None,
) -> RunningApp:
    """Start ``image_tag`` as an app container and return how to reach it."""
    dc = docker_client or client()
    settings = config.settings()
    settings.validate()
    limits = limits or ResourceLimits.from_settings(settings)
    container_name = _container_name(app_id)

    # A redeploy of the same app replaces the previous container.
    remove(app_id, docker_client=dc, missing_ok=True)

    deny_egress = settings.egress_denied
    port = None if deny_egress else (host_port or _free_port())
    environment = {
        "PORT": str(container_port),
        "HANGAR_APP_ID": app_id,
        "HANGAR_APP_NAME": app_name,
        **(env or {}),
    }

    kwargs: dict[str, Any] = {
        "name": container_name,
        "detach": True,
        "environment": environment,
        "labels": {
            LABEL_MANAGED: "true",
            LABEL_APP_ID: app_id,
            LABEL_APP_NAME: app_name,
        },
        "mem_limit": f"{limits.memory_mb}m",
        # Docker takes CPU quota in billionths of a CPU.
        "nano_cpus": int(limits.cpus * 1_000_000_000),
        "pids_limit": limits.pids,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "read_only": True,
        # A read-only root still needs somewhere to write scratch files; keep it
        # small, in-memory, and non-executable.
        "tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
        "restart_policy": {"Name": "unless-stopped"},
    }

    if volumes:
        # Named volumes mount over the read-only root, so this is the only way
        # an app can write anything that outlives its container.
        kwargs["volumes"] = volumes

    if settings.sandbox_runtime:
        kwargs["runtime"] = settings.sandbox_runtime

    if deny_egress:
        # An internal network has no route off the host, which satisfies PRD §8's
        # default-deny. It also means Docker cannot publish a port for this
        # container, so the proxy has to be on this network to reach it.
        _ensure_internal_network(dc, settings.app_network)
        kwargs["network"] = settings.app_network
        # Container-to-container over Docker's embedded DNS, by name.
        upstream = f"{container_name}:{container_port}"
    else:
        kwargs["ports"] = {f"{container_port}/tcp": port}
        upstream = f"{settings.upstream_host}:{port}"

    try:
        container = dc.containers.run(image_tag, **kwargs)
    except ImageNotFound as exc:
        raise DeployError(f"image not found: {image_tag}") from exc
    except APIError as exc:
        raise DeployError(f"could not start container: {exc}") from exc

    container.reload()
    return RunningApp(
        container_id=container.id,
        container_name=container_name,
        container_port=container_port,
        host_port=port,
        upstream=upstream,
        status=container.status,
    )


def _ensure_internal_network(dc, name: str):
    """Create the app network if absent. `internal=True` is what denies egress."""
    try:
        network = dc.networks.get(name)
    except NotFound:
        try:
            return dc.networks.create(name, driver="bridge", internal=True)
        except APIError as exc:
            raise DeployError(f"could not create network {name!r}: {exc}") from exc
    except APIError as exc:
        raise DeployError(f"could not inspect network {name!r}: {exc}") from exc

    # An existing non-internal network would silently leave apps with full
    # outbound access while reporting that egress is denied.
    if not network.attrs.get("Internal", False):
        raise DeployError(
            f"network {name!r} exists but is not internal, so egress would not "
            "actually be denied. Remove it (docker network rm "
            f"{name}) and let Hangar recreate it."
        )
    return network


def status(app_id: str, *, docker_client: docker.DockerClient | None = None) -> str:
    container = _find(app_id, docker_client)
    if container is None:
        return "absent"
    container.reload()
    return container.status


def logs(
    app_id: str,
    *,
    tail: int = 200,
    docker_client: docker.DockerClient | None = None,
) -> str:
    container = _find(app_id, docker_client)
    if container is None:
        raise DeployError(f"no container for app {app_id}")
    raw = container.logs(tail=tail, timestamps=False)
    return raw.decode("utf-8", errors="replace")


def stop(app_id: str, *, docker_client: docker.DockerClient | None = None) -> None:
    container = _require(app_id, docker_client)
    try:
        container.stop(timeout=10)
    except APIError as exc:
        raise DeployError(f"could not stop container: {exc}") from exc


def restart(app_id: str, *, docker_client: docker.DockerClient | None = None) -> None:
    container = _require(app_id, docker_client)
    try:
        container.restart(timeout=10)
    except APIError as exc:
        raise DeployError(f"could not restart container: {exc}") from exc


def remove(
    app_id: str,
    *,
    docker_client: docker.DockerClient | None = None,
    missing_ok: bool = False,
) -> None:
    container = _find(app_id, docker_client)
    if container is None:
        if missing_ok:
            return
        raise DeployError(f"no container for app {app_id}")
    try:
        container.remove(force=True)
    except NotFound:
        # Raced with another removal; the desired end state already holds.
        return
    except APIError as exc:
        raise DeployError(f"could not remove container: {exc}") from exc


def remove_volume(
    app_id: str, *, docker_client: docker.DockerClient | None = None
) -> bool:
    """Destroy an app's data volume. Irreversible; returns whether one existed."""
    from .database import volume_name

    dc = docker_client or client()
    try:
        dc.volumes.get(volume_name(app_id)).remove(force=True)
        return True
    except NotFound:
        return False
    except APIError as exc:
        raise DeployError(f"could not remove data volume: {exc}") from exc


def host_port(app_id: str, *, docker_client: docker.DockerClient | None = None) -> int | None:
    """The published host port, re-read from Docker rather than trusted from the DB."""
    container = _find(app_id, docker_client)
    if container is None:
        return None
    container.reload()
    for bindings in (container.attrs.get("NetworkSettings", {}).get("Ports") or {}).values():
        if bindings:
            return int(bindings[0]["HostPort"])
    return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _container_name(app_id: str) -> str:
    return f"hangar-{app_id}"


def _find(app_id: str, docker_client: docker.DockerClient | None):
    dc = docker_client or client()
    try:
        return dc.containers.get(_container_name(app_id))
    except NotFound:
        return None
    except APIError as exc:
        raise DeployError(f"could not query container: {exc}") from exc


def _require(app_id: str, docker_client: docker.DockerClient | None):
    container = _find(app_id, docker_client)
    if container is None:
        raise DeployError(f"no container for app {app_id}")
    return container


def _free_port() -> int:
    """Ask the OS for an unused port.

    There's an unavoidable race between closing this socket and Docker binding
    the port; in practice the window is small and a collision surfaces as a
    clear start failure rather than silent breakage.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
