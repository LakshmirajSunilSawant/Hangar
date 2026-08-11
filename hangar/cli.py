"""`hangar` command line entrypoint."""

from __future__ import annotations

import argparse
import logging
import os
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
    sub.add_parser("gen-key", help="generate a HANGAR_SECRET_KEY")

    deploy = sub.add_parser("deploy", help="deploy to a Hangar and wait for it")
    deploy.add_argument(
        "source",
        help="a local directory, or a GitHub repo (owner/repo or a full URL)",
    )
    deploy.add_argument("--name", help="app name (defaults to the directory or repo)")
    deploy.add_argument(
        "--url",
        default=os.environ.get("HANGAR_URL", "http://127.0.0.1:8080"),
        help="control plane URL (or set HANGAR_URL)",
    )
    deploy.add_argument(
        "--token",
        default=os.environ.get("HANGAR_TOKEN") or os.environ.get("HANGAR_API_TOKEN"),
        help="API token (or set HANGAR_TOKEN)",
    )
    deploy.add_argument("--ref", help="branch, tag or commit, for repos")
    deploy.add_argument(
        "--database", choices=("none", "sqlite", "postgres"), help="per-app storage"
    )
    deploy.add_argument(
        "--no-wait", action="store_true", help="return as soon as it is queued"
    )

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    if args.command == "config":
        return _show_config()
    if args.command == "gen-key":
        return _gen_key()
    if args.command == "deploy":
        return _deploy(args)

    parser.print_help()
    return 1


def config_default_port() -> int:
    """PORT is what most hosts inject to tell a process where to listen."""
    import os

    from . import config

    raw = os.environ.get("PORT", "").strip()
    return int(raw) if raw.isdigit() else config.DEFAULT_PORT


def _deploy(args) -> int:
    """Deploy a directory or a repo, then wait for the outcome."""
    from pathlib import Path

    from .client import Client, ClientError

    source = Path(args.source).expanduser()
    is_directory = source.is_dir()

    name = args.name or _default_name(args.source, is_directory)
    client = Client(base_url=args.url, token=args.token)

    try:
        existing = client.find_by_name(name)
        if existing:
            # Deploying the same name again means "update it", not "fail".
            print(f"{name} already exists — redeploying", file=sys.stderr)
            if is_directory:
                print(
                    "note: redeploy re-uses the previously uploaded source. "
                    "Delete the app first to upload a new zip.",
                    file=sys.stderr,
                )
            app = client.redeploy(existing["id"])
        elif is_directory:
            print(f"uploading {source}", file=sys.stderr)
            app = client.deploy_directory(source, name, args.database)
        else:
            app = client.deploy_repo(args.source, name, args.ref, args.database)

        if args.no_wait:
            print(app["id"])
            return 0

        # ASCII only: this prints to whatever console the user has, and a
        # Windows terminal in cp1252 mangles anything fancier.
        final = client.wait(
            app["id"], on_status=lambda s: print(f"  {s}...", file=sys.stderr)
        )
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if final["status"] != "running":
        print(f"deploy failed: {final.get('error')}", file=sys.stderr)
        # The build log is where the actual reason lives.
        try:
            log = client.logs(final["id"])["build_log"]
            print("\n--- build log (last 40 lines) ---", file=sys.stderr)
            print("\n".join(log.splitlines()[-40:]), file=sys.stderr)
        except ClientError:
            pass
        return 1

    print(final["url"])
    return 0


def _default_name(source: str, is_directory: bool) -> str:
    """A sensible app name from a path or a repo reference."""
    import re
    from pathlib import Path

    raw = Path(source).name if is_directory else source.rstrip("/").split("/")[-1]
    cleaned = re.sub(r"[^a-z0-9-]+", "-", raw.lower().removesuffix(".git")).strip("-")
    return cleaned or "app"


def _gen_key() -> int:
    from . import secrets

    print(secrets.generate_key())
    print(
        "\nSet this as HANGAR_SECRET_KEY and keep it somewhere durable.\n"
        "If it changes, secrets encrypted with the old key cannot be read back.",
        file=sys.stderr,
    )
    return 0


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
    print(f"app databases    {settings.app_db}")
    print(f"secret key       {'set' if settings.secret_key else 'not set'}")
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
