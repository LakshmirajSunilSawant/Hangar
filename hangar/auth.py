"""Working out who is calling, and whether they may.

Two credentials reach the control plane:

* the **shared admin token** (`HANGAR_API_TOKEN`) — non-interactive, for
  scripts, CI and the deploy pipeline. It predates user accounts and keeps
  full access; locking an operator out of their own control plane would be
  worse than the alternative.
* a **session cookie** — a person who logged in. Scoped to whatever apps they
  have been granted, per PRD §7's owner/editor/viewer.

With neither configured nor presented, the API stays open, which keeps local
development setup-free. `hangar serve` refuses to bind to a non-loopback
interface in that state, so it cannot be exposed by accident.
"""

from __future__ import annotations

import secrets as pysecrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config, identity, store
from .identity import ADMIN, Principal
from .permissions import Action, allows
from .store import Role

# auto_error=False so a missing header produces our own message rather than
# FastAPI's bare "Not authenticated".
_scheme = HTTPBearer(auto_error=False, description="HANGAR_API_TOKEN")

# Used when nothing is configured at all — local development.
ANONYMOUS = Principal(kind="anonymous")


def current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_scheme),
) -> Principal:
    """Resolve the caller. Raises 401 only when a credential was expected."""
    settings = config.settings()

    if credentials and credentials.credentials:
        if not settings.auth_enabled:
            # A token was sent to a control plane that has none configured.
            # Accepting it would be pretending to check something.
            raise HTTPException(
                401, "this control plane has no API token configured"
            )
        # Constant-time: a plain == leaks token content through timing.
        if not pysecrets.compare_digest(credentials.credentials, settings.api_token):
            raise HTTPException(
                403, "invalid API token", headers={"WWW-Authenticate": "Bearer"}
            )
        return ADMIN

    cookie = request.cookies.get(identity.SESSION_COOKIE, "")
    if cookie:
        with store.session() as sess:
            user = identity.resolve_session(sess, cookie)
            if user is not None:
                return Principal(
                    kind="user",
                    user_id=user.id,
                    email=user.email,
                    is_admin=user.is_admin,
                )
        raise HTTPException(401, "your session has expired — sign in again")

    if settings.auth_enabled or _has_users():
        raise HTTPException(
            401,
            "sign in, or send 'Authorization: Bearer <HANGAR_API_TOKEN>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return ANONYMOUS


def _has_users() -> bool:
    """Once anyone has an account, anonymous access stops being reasonable."""
    with store.session() as sess:
        return bool(store.list_users(sess))


def require_token(principal: Principal = Depends(current_principal)) -> Principal:
    """Any recognised caller. Route-level authorisation happens per app."""
    return principal


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    """Operator-level actions: managing users, creating apps."""
    if principal.kind == "anonymous":
        return principal
    if principal.kind == "admin" or principal.is_admin:
        return principal
    raise HTTPException(403, "this action requires an administrator")


# --------------------------------------------------------------------------
# Per-app authorisation
# --------------------------------------------------------------------------


def role_for(sess, principal: Principal, app_id: str) -> Role | None:
    """The caller's role on one app.

    Admins and the shared token are treated as owners everywhere; an operator
    who can restart the box can already reach everything on it, so pretending
    otherwise would be theatre.
    """
    if principal.kind in ("admin", "anonymous") or principal.is_admin:
        return Role.OWNER
    if principal.user_id is None:
        return None

    permission = store.permission_for(sess, app_id, principal.user_id)
    return Role(permission.role) if permission else None


def authorize(sess, principal: Principal, app_id: str, action: Action) -> Role:
    """Check one action against one app, or raise.

    A caller with no access at all gets 404 rather than 403: whether an app
    exists is itself information, and 403 would confirm the name.
    """
    role = role_for(sess, principal, app_id)
    if role is None:
        raise HTTPException(404, f"no app with id {app_id}")
    if not allows(role, action):
        raise HTTPException(
            403,
            f"your role on this app ({role.value}) does not allow {action.value}",
        )
    return role
