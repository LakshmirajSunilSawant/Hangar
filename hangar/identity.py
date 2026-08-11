"""Who someone is.

PRD §8: "Auth is platform-level, not app-level — recipients authenticate to
Ory Kratos/Keycloak before ever reaching the app's own routes." The *platform*
part is the requirement; Kratos is the PRD's suggested implementation of it.

Kratos is a separate Go service with its own database, which is a lot to ask of
a 12 GB box that also has to run K3s, Postgres, Caddy and the app sandboxes.
So this defines an `IdentityProvider` interface with a built-in provider that
needs no extra services, exactly as `backends` and `routing` do. A Kratos
provider implements the same three methods and changes one env var.

The built-in provider is invite-based rather than open-registration: an owner
invites an email, gets a one-time link, and hands it over — the "share it like
a Google Doc" flow from the PRD, minus a mail server, which would cost money
and break the project's $0 rule.

Passwords are hashed with argon2id through libsodium, which is already a
dependency for secret sealing. No new dependency, and a memory-hard KDF rather
than a fast hash.
"""

from __future__ import annotations

import hashlib
import logging
import secrets as pysecrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta

from nacl import exceptions as nacl_exceptions
from nacl import pwhash

from . import config, store
from .store import User, UserSession, utcnow

log = logging.getLogger("hangar.identity")

SESSION_COOKIE = "hangar_session"
TOKEN_BYTES = 32


class IdentityError(Exception):
    """Authentication failed, or could not be attempted."""


@dataclass(frozen=True)
class Principal:
    """Whoever is making the current request."""

    kind: str  # "admin" | "user"
    user_id: str | None = None
    email: str | None = None
    is_admin: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.kind in ("admin", "user")

    @property
    def label(self) -> str:
        return self.email or self.kind


# The shared API token, which predates user accounts. Kept because scripts, CI
# and the deploy pipeline need a non-interactive credential, and because
# locking an operator out of their own control plane would be worse than the
# alternative.
ADMIN = Principal(kind="admin", email=None, is_admin=True)


def hash_token(token: str) -> str:
    """Session and invite tokens are stored hashed, never in the clear."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return pysecrets.token_urlsafe(TOKEN_BYTES)


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise IdentityError("password must be at least 10 characters")
    return pwhash.argon2id.str(password.encode()).decode()


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return pwhash.verify(hashed.encode(), password.encode())
    except nacl_exceptions.InvalidkeyError:
        return False
    except nacl_exceptions.CryptoError:
        return False


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class IdentityProvider(ABC):
    name: str = "base"

    @abstractmethod
    def authenticate(self, sess, email: str, password: str) -> User:
        """Return the user, or raise IdentityError."""

    @abstractmethod
    def supports_local_passwords(self) -> bool:
        """Whether this provider manages passwords itself."""


class LocalProvider(IdentityProvider):
    """Invite-based accounts stored in the control plane's own database."""

    name = "local"

    def authenticate(self, sess, email: str, password: str) -> User:
        user = store.user_by_email(sess, email)

        # Verify against a dummy hash when the user is missing, so a wrong
        # email and a wrong password take the same time and don't distinguish
        # "no such account" from "bad password".
        stored = user.password_hash if user and user.password_hash else _DUMMY_HASH
        ok = verify_password(password, stored)

        if user is None or not ok:
            raise IdentityError("invalid email or password")
        if not user.is_active:
            raise IdentityError("this invitation has not been accepted yet")
        return user

    def supports_local_passwords(self) -> bool:
        return True


# Computed once: hashing is deliberately slow, and this only needs to cost the
# same as a real verification.
_DUMMY_HASH = pwhash.argon2id.str(b"not-a-real-password").decode()


_PROVIDERS: dict[str, type[IdentityProvider]] = {"local": LocalProvider}


def register(name: str, provider: type[IdentityProvider]) -> None:
    _PROVIDERS[name] = provider


def get_provider(name: str | None = None) -> IdentityProvider:
    name = name or config.settings().identity_provider
    if name not in _PROVIDERS:
        known = ", ".join(sorted(_PROVIDERS))
        raise IdentityError(f"unknown identity provider {name!r} (known: {known})")
    return _PROVIDERS[name]()


# --------------------------------------------------------------------------
# Users and invitations
# --------------------------------------------------------------------------


def normalise_email(email: str) -> str:
    cleaned = email.strip().lower()
    if "@" not in cleaned or cleaned.startswith("@") or cleaned.endswith("@"):
        raise IdentityError(f"not a usable email address: {email!r}")
    return cleaned


def invite(sess, email: str, *, is_admin: bool = False) -> tuple[User, str]:
    """Create (or re-invite) a user. Returns the user and a one-time token.

    The token is returned rather than emailed: sending mail needs a paid
    service, and the PRD's flow is the owner passing a link along anyway.
    """
    address = normalise_email(email)
    token = new_token()

    user = store.user_by_email(sess, address)
    if user is None:
        user = User(email=address, is_admin=is_admin)
    elif user.is_active:
        raise IdentityError(f"{address} has already accepted an invitation")

    user.invite_hash = hash_token(token)
    user.invited_at = utcnow()
    if is_admin:
        user.is_admin = True

    sess.add(user)
    sess.commit()
    sess.refresh(user)
    return user, token


def accept_invite(sess, token: str, password: str) -> User:
    """Turn an invitation into a usable account."""
    from sqlmodel import select

    user = sess.exec(
        select(User).where(User.invite_hash == hash_token(token))
    ).first()
    if user is None:
        raise IdentityError("this invitation link is not valid")
    if user.is_active:
        raise IdentityError("this invitation has already been used")

    user.password_hash = hash_password(password)
    # One-time: the token stops working the moment it is used.
    user.invite_hash = ""
    sess.add(user)
    sess.commit()
    sess.refresh(user)
    log.info("invitation accepted by %s", user.email)
    return user


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def start_session(sess, user: User) -> tuple[UserSession, str]:
    """Create a session. Returns the record and the raw token for the cookie."""
    token = new_token()
    record = UserSession(
        token_hash=hash_token(token),
        user_id=user.id,
        expires_at=utcnow() + timedelta(hours=config.settings().session_hours),
    )
    sess.add(record)
    sess.commit()
    sess.refresh(record)
    return record, token


def resolve_session(sess, token: str) -> User | None:
    """The user behind a session token, or None if it's invalid or expired."""
    if not token:
        return None

    record = store.session_by_hash(sess, hash_token(token))
    if record is None:
        return None
    if record.is_expired():
        # Expired sessions are removed on sight rather than accumulating.
        sess.delete(record)
        sess.commit()
        return None

    user = store.get_user(sess, record.user_id)
    return user if user and user.is_active else None


def end_session(sess, token: str) -> None:
    record = store.session_by_hash(sess, hash_token(token))
    if record is not None:
        sess.delete(record)
        sess.commit()
