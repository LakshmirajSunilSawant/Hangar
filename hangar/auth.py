"""API authentication.

A single shared bearer token, read from HANGAR_API_TOKEN. This is deliberately
the smallest thing that closes the open-write-API hole — the control plane can
create, inspect, and delete deployments, so it must not be anonymously
reachable once it has a public URL.

It is *not* the auth layer the PRD describes. PRD §8 wants per-user identity
through Ory Kratos with owner/editor/viewer roles and identity headers injected
by a proxy. That is Milestone 3, and nothing here forecloses it: this token
guards the control plane's own API, which will still need protecting once
end-user identity exists.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config

# auto_error=False so a missing header produces our own 401 with a useful
# message rather than FastAPI's bare "Not authenticated".
_scheme = HTTPBearer(auto_error=False, description="HANGAR_API_TOKEN")


def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_scheme),
) -> None:
    """Reject the request unless it carries the configured bearer token."""
    settings = config.settings()

    if not settings.auth_enabled:
        # No token configured: allowed, so local development needs no setup.
        # `hangar serve` refuses to bind to a non-loopback interface in this
        # state, so an unauthenticated API cannot be exposed by accident.
        return

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            401,
            "missing bearer token — send 'Authorization: Bearer <HANGAR_API_TOKEN>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time: a plain == leaks token content through timing.
    if not secrets.compare_digest(credentials.credentials, settings.api_token):
        raise HTTPException(
            403,
            "invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
