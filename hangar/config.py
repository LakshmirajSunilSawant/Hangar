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
    router: str
    app_domain: str | None
    app_scheme: str
    caddy_admin_url: str
    caddy_server: str
    caddy_listen: str
    upstream_host: str
    scan_policy: str
    scan_block_severity: str
    egress: str
    app_network: str
    source_root: str
    github_token: str | None
    app_db: str
    app_db_admin_url: str | None
    secret_key: str | None

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_token)

    @property
    def scan_enabled(self) -> bool:
        return self.scan_policy != "off"

    @property
    def egress_denied(self) -> bool:
        return self.egress == "deny"

    def validate(self) -> None:
        """Reject combinations that cannot work, rather than failing at deploy."""
        if self.egress_denied and self.router == "none":
            raise ValueError(
                "HANGAR_EGRESS=deny puts apps on an internal network with no "
                "published ports, so they are only reachable through a proxy. "
                "Set HANGAR_ROUTER=caddy (with Caddy attached to "
                f"{self.app_network!r}), or use HANGAR_EGRESS=allow."
            )

    def url_for_port(self, port: int) -> str:
        """Direct URL for an app published on ``port``.

        Only used when no router fronts the apps; with routing enabled an app
        is reached by hostname and the port stays internal.
        """
        return f"{self.public_base_url.rstrip('/')}:{port}"

    def url_for_app(self, app_name: str) -> str:
        """Stable public URL for an app, once a router is in front of it."""
        if not self.app_domain:
            raise ValueError(
                "HANGAR_APP_DOMAIN must be set to give apps stable URLs"
            )
        return f"{self.app_scheme}://{self.hostname_for_app(app_name)}"

    def hostname_for_app(self, app_name: str) -> str:
        """Per-app hostname. App names are already validated as DNS labels."""
        if not self.app_domain:
            raise ValueError(
                "HANGAR_APP_DOMAIN must be set to give apps stable URLs"
            )
        return f"{app_name}.{self.app_domain.strip('.')}"


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
        # "none" keeps the direct host:port URLs, which is all a laptop needs.
        router=os.environ.get("HANGAR_ROUTER", "none"),
        app_domain=_str("HANGAR_APP_DOMAIN"),
        app_scheme=os.environ.get("HANGAR_APP_SCHEME", "https"),
        caddy_admin_url=os.environ.get(
            "HANGAR_CADDY_ADMIN_URL", "http://localhost:2019"
        ),
        caddy_server=os.environ.get("HANGAR_CADDY_SERVER", "srv0"),
        caddy_listen=os.environ.get("HANGAR_CADDY_LISTEN", ":80"),
        # Where Caddy should dial to reach app containers. Loopback when Caddy
        # runs on the host; host.docker.internal when Caddy is containerised.
        upstream_host=os.environ.get("HANGAR_UPSTREAM_HOST", "127.0.0.1"),
        # PRD §8: "flag (don't necessarily block in v1)". Findings are always
        # recorded; "block" additionally refuses the deploy.
        scan_policy=_choice(
            "HANGAR_SCAN_POLICY", "flag", ("flag", "block", "off")
        ),
        scan_block_severity=_choice(
            "HANGAR_SCAN_BLOCK_SEVERITY", "high", ("low", "medium", "high")
        ),
        # "deny" puts apps on an internal Docker network with no route out.
        # That also removes host port publishing, so it requires a router on
        # the same network — see Settings.validate().
        egress=_choice("HANGAR_EGRESS", "allow", ("allow", "deny")),
        app_network=os.environ.get("HANGAR_APP_NETWORK", "hangar-apps"),
        source_root=os.environ.get(
            "HANGAR_SOURCE_ROOT", str(Path.cwd() / ".hangar" / "sources")
        ),
        github_token=_str("HANGAR_GITHUB_TOKEN"),
        # Default per-app database when a request doesn't ask for one.
        app_db=_choice("HANGAR_APP_DB", "none", ("none", "sqlite", "postgres")),
        # An admin connection Hangar can create databases and roles with. Only
        # needed for the postgres mode.
        app_db_admin_url=_normalise_optional(_str("HANGAR_APP_DB_ADMIN_URL")),
        secret_key=_str("HANGAR_SECRET_KEY"),
    )


def _normalise_optional(url: str | None) -> str | None:
    return normalise_database_url(url) if url else None


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


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = (_str(name) or default).lower()
    if value not in allowed:
        raise ValueError(
            f"{name} must be one of {', '.join(allowed)}, got {value!r}"
        )
    return value


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
