"""An app that does no authentication at all, and still knows who you are.

That is the point of PRD §8's platform-level auth: with HANGAR_APP_AUTH on,
visitors sign in to Hangar before this app is ever reached, and arrive with
identity headers already attached. This file contains no login code, no
sessions, and no user table.

Never trust these headers on an app that is reachable directly — they are only
meaningful because the app can be reached solely through the proxy that sets
them.
"""

from fastapi import FastAPI, Request

app = FastAPI(title="Hangar sample — whoami")


@app.get("/")
def whoami(request: Request) -> dict:
    return {
        "app": "whoami",
        "message": "Deployed by Hangar.",
        "user": request.headers.get("x-hangar-user"),
        "user_id": request.headers.get("x-hangar-user-id"),
        "role": request.headers.get("x-hangar-role"),
        "authenticated_by": "the platform, not this app",
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
