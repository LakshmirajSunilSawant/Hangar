"""Sign-in, invitations, and sharing.

Split out of api.py because these routes are about *people*, and mixing them
with the app lifecycle made that file hard to read.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import config, identity, store
from .auth import current_principal, require_admin
from .identity import IdentityError, Principal
from .permissions import Action, describe
from .store import Role

auth_router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(
    prefix="/users", tags=["users"], dependencies=[Depends(require_admin)]
)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: str = Field(min_length=10)


class InviteRequest(BaseModel):
    email: str
    is_admin: bool = False


class UserView(BaseModel):
    id: str
    email: str
    is_admin: bool
    is_active: bool
    created_at: str

    @classmethod
    def of(cls, user: store.User) -> "UserView":
        return cls(
            id=user.id,
            email=user.email,
            is_admin=user.is_admin,
            is_active=user.is_active,
            created_at=user.created_at.isoformat(),
        )


class InviteView(BaseModel):
    user: UserView
    invite_token: str = Field(
        description="One-time. Hangar sends no email; pass this to the person."
    )


class WhoAmI(BaseModel):
    kind: str
    email: str | None = None
    is_admin: bool = False
    authenticated: bool


class GrantRequest(BaseModel):
    email: str
    role: str = Field(description="owner, editor, or viewer.")


class GrantView(BaseModel):
    user_id: str
    email: str
    role: str
    can: list[str]
    granted_at: str


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


def _set_cookie(request: Request, response: Response, token: str) -> None:
    settings = config.settings()

    # Secure is set from the scheme this request actually arrived over, not
    # from configuration. Marking a cookie Secure on a plain-HTTP deployment
    # makes the browser drop it silently, and the failure looks like "login
    # does nothing" rather than anything about cookies. Behind Caddy the
    # forwarded scheme is what counts.
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    scheme = forwarded or request.url.scheme

    response.set_cookie(
        identity.SESSION_COOKIE,
        token,
        max_age=settings.session_hours * 3600,
        # HttpOnly: a session the page's own scripts cannot read is not
        # exfiltrable by an XSS in a deployed app sharing the parent domain.
        httponly=True,
        samesite="lax",
        secure=scheme == "https",
        path="/",
        # Scoped to the parent domain when apps live on sibling hostnames,
        # or the browser never sends the session to them.
        domain=settings.cookie_domain,
    )


@auth_router.post("/login", response_model=WhoAmI)
def login(
    request: LoginRequest, http_request: Request, response: Response
) -> WhoAmI:
    provider = identity.get_provider()
    with store.session() as sess:
        try:
            user = provider.authenticate(sess, request.email, request.password)
        except IdentityError as exc:
            # Deliberately vague, and the provider spends the same time on a
            # missing user as a wrong password.
            raise HTTPException(401, str(exc)) from exc

        _, token = identity.start_session(sess, user)
        _set_cookie(http_request, response, token)
        return WhoAmI(
            kind="user", email=user.email, is_admin=user.is_admin, authenticated=True
        )


@auth_router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(identity.SESSION_COOKIE, "")
    if token:
        with store.session() as sess:
            identity.end_session(sess, token)
    # Same domain as when it was set, or the browser keeps the old cookie.
    response.delete_cookie(
        identity.SESSION_COOKIE, path="/", domain=config.settings().cookie_domain
    )


@auth_router.post("/accept-invite", response_model=WhoAmI)
def accept_invite(
    request: AcceptInviteRequest, http_request: Request, response: Response
) -> WhoAmI:
    """Set a password using a one-time invitation token, and sign in."""
    with store.session() as sess:
        try:
            user = identity.accept_invite(sess, request.token, request.password)
        except IdentityError as exc:
            raise HTTPException(400, str(exc)) from exc

        _, token = identity.start_session(sess, user)
        _set_cookie(http_request, response, token)
        return WhoAmI(
            kind="user", email=user.email, is_admin=user.is_admin, authenticated=True
        )


@auth_router.get("/me", response_model=WhoAmI)
def me(principal: Principal = Depends(current_principal)) -> WhoAmI:
    return WhoAmI(
        kind=principal.kind,
        email=principal.email,
        is_admin=principal.is_admin,
        authenticated=principal.is_authenticated,
    )


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


@users_router.post("", response_model=InviteView, status_code=201)
def invite_user(request: InviteRequest) -> InviteView:
    """Invite someone. Returns a one-time token to pass along.

    No email is sent — a mail service costs money, and the PRD's flow is the
    owner sharing a link anyway.
    """
    with store.session() as sess:
        try:
            user, token = identity.invite(
                sess, request.email, is_admin=request.is_admin
            )
        except IdentityError as exc:
            raise HTTPException(400, str(exc)) from exc
        return InviteView(user=UserView.of(user), invite_token=token)


@users_router.get("", response_model=list[UserView])
def list_users() -> list[UserView]:
    with store.session() as sess:
        return [UserView.of(u) for u in store.list_users(sess)]


@users_router.delete("/{user_id}", status_code=204)
def delete_user(user_id: str) -> None:
    from sqlmodel import select

    with store.session() as sess:
        user = store.get_user(sess, user_id)
        if user is None:
            raise HTTPException(404, f"no user with id {user_id}")

        # Their grants and sessions go too, or a deleted account keeps access.
        for permission in sess.exec(
            select(store.Permission).where(store.Permission.user_id == user_id)
        ).all():
            sess.delete(permission)
        for session_row in sess.exec(
            select(store.UserSession).where(store.UserSession.user_id == user_id)
        ).all():
            sess.delete(session_row)

        # Emit those DELETEs before the user's. SQLAlchemy is not told about
        # the relationships, so it will not order them itself, and a database
        # that enforces foreign keys rejects the whole transaction.
        sess.flush()

        sess.delete(user)
        sess.commit()


# --------------------------------------------------------------------------
# Sharing — mounted under /apps by api.py
# --------------------------------------------------------------------------


def grant_view(user: store.User, permission: store.Permission) -> GrantView:
    return GrantView(
        user_id=user.id,
        email=user.email,
        role=permission.role,
        can=describe(permission.role),
        granted_at=permission.granted_at.isoformat(),
    )


def validate_role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError as exc:
        raise HTTPException(
            422, f"role must be one of: {', '.join(r.value for r in Role)}"
        ) from exc


__all__ = [
    "Action",
    "GrantRequest",
    "GrantView",
    "auth_router",
    "grant_view",
    "users_router",
    "validate_role",
]
