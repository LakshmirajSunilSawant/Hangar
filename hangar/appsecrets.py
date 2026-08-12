"""Per-app secrets — the environment variables an app needs but must not ship.

Hangar could already give an app a database. It could not give it an API key,
which meant the one thing every generated tool eventually needs — talk to
Slack, to OpenAI, to an internal service — had no answer except committing the
key to the repo, where the security scan would (rightly) flag it.

Values are sealed with libsodium before they touch the control-plane database
(secrets.py), and injected into the container's environment at deploy time. The
control plane is the only thing that ever holds the plaintext, and only for as
long as it takes to start a container.

Two properties that matter more than the code length suggests:

* **A stored secret is never readable back through the API.** Names, yes;
  values, never. If someone loses one they set, they set it again. The
  alternative is an endpoint whose entire job is disclosing secrets, guarded by
  the same session cookie as everything else.
* **Nothing here is logged.** Deploy logs are shown in the dashboard and are
  readable by editors, so this records how many secrets were injected and what
  they are called, and never what they contain.
"""

from __future__ import annotations

import re

from sqlmodel import select

from . import secrets, store
from .store import AppSecret, utcnow

# Environment-variable convention, and long enough for anything reasonable.
NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# 8KB holds a private key with room to spare. The limit exists so a stray file
# upload can't be parked in the control-plane database.
MAX_VALUE_BYTES = 8192

# Hangar sets these itself, and an app given a conflicting value would behave
# in a way nobody could debug from the dashboard.
RESERVED = frozenset({"PORT", "HANGAR_APP_ID", "HANGAR_APP_NAME"})

# Reserved only when Hangar is actually providing storage — an app with no
# per-app database has every right to point at one somewhere else.
RESERVED_WITH_DATABASE = "DATABASE_URL"


class SecretNameError(ValueError):
    """The name is not usable as an injected environment variable."""


def validate_name(name: str, *, app_has_database: bool = False) -> str:
    name = name.strip()
    if not NAME_PATTERN.match(name):
        raise SecretNameError(
            f"{name!r} is not a usable name — use A-Z, 0-9 and underscores, "
            "starting with a letter (the convention for environment variables)"
        )
    if name in RESERVED:
        raise SecretNameError(
            f"{name} is set by Hangar itself and cannot be overridden"
        )
    if name == RESERVED_WITH_DATABASE and app_has_database:
        raise SecretNameError(
            "DATABASE_URL is set by Hangar for apps with a per-app database. "
            "Deploy this app with database=none if it should connect somewhere "
            "else."
        )
    return name


def validate_value(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_VALUE_BYTES:
        raise SecretNameError(
            f"secret is {len(encoded)} bytes; the limit is {MAX_VALUE_BYTES}"
        )
    return value


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def get(sess, app_id: str, name: str) -> AppSecret | None:
    return sess.exec(
        select(AppSecret)
        .where(AppSecret.app_id == app_id)
        .where(AppSecret.name == name)
    ).first()


def list_for(sess, app_id: str) -> list[AppSecret]:
    """Every secret for an app. Callers must render names only."""
    return list(
        sess.exec(
            select(AppSecret)
            .where(AppSecret.app_id == app_id)
            .order_by(AppSecret.name)
        ).all()
    )


def put(sess, app_id: str, name: str, value: str, *, app_has_database: bool = False):
    """Create or replace one secret. Returns the stored record."""
    name = validate_name(name, app_has_database=app_has_database)
    validate_value(value)

    record = get(sess, app_id, name)
    sealed = secrets.seal(value)
    if record is None:
        record = AppSecret(app_id=app_id, name=name, sealed_value=sealed)
    else:
        record.sealed_value = sealed
        record.updated_at = utcnow()
    store.save(sess, record)
    return record


def delete(sess, app_id: str, name: str) -> bool:
    record = get(sess, app_id, name)
    if record is None:
        return False
    sess.delete(record)
    sess.commit()
    return True


def delete_all(sess, app_id: str) -> int:
    """Used when an app is deleted; its secrets have no meaning without it."""
    records = list_for(sess, app_id)
    for record in records:
        sess.delete(record)
    sess.commit()
    return len(records)


# --------------------------------------------------------------------------
# Injection
# --------------------------------------------------------------------------


def env_for(sess, app_id: str) -> dict[str, str]:
    """Decrypt every secret for an app, for handing to the container.

    The only place plaintext exists outside the container. Keep the result out
    of logs, error messages and anything that ends up in the store.
    """
    return {
        record.name: secrets.open_sealed(record.sealed_value)
        for record in list_for(sess, app_id)
    }
