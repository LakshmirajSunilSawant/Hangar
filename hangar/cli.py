"""`hangar` command line entrypoint."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hangar", description="Cloud for small software."
    )
    parser.add_argument("--version", action="version", version=f"hangar {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the control plane API")
    # Loopback by default; binding wider requires HANGAR_API_TOKEN.
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=config_default_port())
    serve.add_argument("--reload", action="store_true")

    sub.add_parser("config", help="show resolved configuration")

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    if args.command == "config":
        return _show_config()

    parser.print_help()
    return 1


def config_default_port() -> int:
    """PORT is what most hosts inject to tell a process where to listen."""
    import os

    from . import config

    raw = os.environ.get("PORT", "").strip()
    return int(raw) if raw.isdigit() else config.DEFAULT_PORT


def _show_config() -> int:
    from . import backends, config, routing

    settings = config.settings()
    backend = backends.get_backend()
    router = routing.get_router()

    # The token is deliberately never printed, only its presence.
    print(f"database_url     {_redact(settings.database_url)}")
    print(f"backend          {settings.backend} (available: {backend.available()})")
    print(f"router           {settings.router} (available: {router.available()})")
    print(f"app_domain       {settings.app_domain or 'unset — apps get host:port URLs'}")
    print(f"auth             {'enabled' if settings.auth_enabled else 'DISABLED'}")
    print(f"sandbox_runtime  {settings.sandbox_runtime or 'docker default (not gVisor)'}")
    print(f"public_base_url  {settings.public_base_url}")
    print(
        f"app limits       {settings.memory_mb} MB, {settings.cpus} CPU, "
        f"{settings.pids} pids"
    )
    return 0


def _redact(url: str) -> str:
    """Hide credentials in a database URL before printing it."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


def _serve(args) -> int:
    import uvicorn

    from . import config

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    settings = config.settings()
    if not settings.auth_enabled and not config.is_loopback(args.host):
        # Refusing is the whole point: an anonymous control plane on a public
        # interface lets anyone deploy code and delete apps.
        print(
            f"refusing to bind to {args.host} without authentication.\n"
            "Set HANGAR_API_TOKEN to a secret value, or bind to 127.0.0.1 for "
            "local development.",
            file=sys.stderr,
        )
        return 2

    if not settings.auth_enabled:
        print(
            "note: HANGAR_API_TOKEN is unset — the API is unauthenticated and "
            "bound to loopback only.",
            file=sys.stderr,
        )

    uvicorn.run(
        "hangar.api:api",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
