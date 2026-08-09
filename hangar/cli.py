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
    # Localhost by default: there is no auth layer yet (PRD Milestone 3).
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)

    parser.print_help()
    return 1


def _serve(args) -> int:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    if args.host not in ("127.0.0.1", "localhost"):
        print(
            f"warning: binding to {args.host} exposes an API with no authentication",
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
