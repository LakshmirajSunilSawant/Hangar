"""Configuration, read from the environment.

Twelve-factor: every deployment-varying value comes from an env var with a
local-development default, so the same image runs on a laptop and on the
Oracle VM without code changes.

Settings are read fresh on each call rather than cached at import time — the
cost is a few dict lookups, and it avoids the class of bug where a process
holds configuration from before an env change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MEMORY_MB = 512
DEFAULT_CPUS = 0.5
DEFAULT_PIDS = 256
DEFAULT_PORT = 8080

# Binding to anything outside this set makes the API reachable by other hosts.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_token: str | None
    backend: str
    sandbox_runtime: str | None
    public_base_url: str
    memory_mb: int
    cpus: float
    pids: int

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_token)

    def url_for_port(self, port: int) -> str:
        """Public URL for an app published on ``port``.

        Once Caddy fronts the apps this becomes a per-app hostname instead of a
        port, which is why callers go through here rather than formatting URLs
        themselves.
        """
        return f"{self.public_base_url.rstrip('/')}:{port}"


def settings() -> Settings:
    return Settings(
        database_url=database_url(),
        api_token=_str("HANGAR_API_TOKEN"),
        backend=os.environ.get("HANGAR_BACKEND", "docker"),
        # Set to "runsc" on a host with gVisor installed; that is the real
        # isolation boundary the PRD requires for untrusted code.
        sandbox_runtime=_str("HANGAR_RUNTIME"),
        public_base_url=os.environ.get("HANGAR_PUBLIC_BASE_URL", "http://localhost"),
        memory_mb=_int("HANGAR_APP_MEMORY_MB", DEFAULT_MEMORY_MB),
        cpus=_float("HANGAR_APP_CPUS", DEFAULT_CPUS),
        pids=_int("HANGAR_APP_PIDS", DEFAULT_PIDS),
    )


def database_url() -> str:
    """Resolve the control-plane database URL.

    Order: explicit HANGAR_DATABASE_URL, then DATABASE_URL (what Render and
    most hosts inject), then a local SQLite file.
    """
    explicit = _str("HANGAR_DATABASE_URL") or _str("DATABASE_URL")
    if explicit:
        return normalise_database_url(explicit)

    path = _str("HANGAR_DB")
    sqlite_path = Path(path) if path else Path.cwd() / ".hangar" / "hangar.db"
    return f"sqlite:///{sqlite_path}"


def normalise_database_url(url: str) -> str:
    """Make provider-supplied URLs usable by SQLAlchemy.

    Render (and Heroku before it) hand out `postgres://`, which SQLAlchemy
    dropped support for; and the bare `postgresql://` form picks psycopg2,
    which isn't what we install.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


# --------------------------------------------------------------------------
# Env parsing
# --------------------------------------------------------------------------


def _str(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
