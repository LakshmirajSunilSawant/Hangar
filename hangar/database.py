"""Per-app databases (PRD Milestone 4).

Apps run on a read-only root filesystem with only an ephemeral `/tmp`, so
without this they cannot persist anything at all — a tool that stores even one
row is impossible. This provisions storage scoped to a single app and injects
its connection string as `DATABASE_URL`, which is the variable every framework
in Hangar's supported set already looks for.

Two modes, per the PRD:

* **sqlite** — a Docker volume mounted at `/data`, holding one file. No server,
  no credentials, no network. The right default for a three-person internal
  tool, and it keeps working when egress is denied.
* **postgres** — a dedicated database *and* role on a Postgres that Hangar
  administers. A separate database rather than a shared schema, so one app
  cannot read another's tables even if it goes looking; the role is granted
  nothing outside its own database.

Generated Postgres passwords are sealed with libsodium before they touch the
control-plane database (see secrets.py), because PRD §8 forbids storing secrets
in plaintext.
"""

from __future__ import annotations

import logging
import re
import secrets as pysecrets
import string
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text

from . import config, secrets
from .store import AppDatabase

log = logging.getLogger("hangar.database")

DATA_MOUNT = "/data"
SQLITE_FILENAME = "app.db"

# Postgres allows no bind parameters anywhere in DDL — not for identifiers and
# not for CREATE ROLE ... PASSWORD either. So everything interpolated into DDL
# has to be provably safe rather than merely escaped.
SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,48}$")

# Passwords are drawn from this alphabet only, which contains no quote,
# backslash, or newline, so a single-quoted literal cannot be broken out of.
# 32 characters of 62 is about 190 bits — far more than enough.
PASSWORD_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 32
SAFE_PASSWORD = re.compile(r"^[A-Za-z0-9]{16,128}$")


def generate_password() -> str:
    return "".join(
        pysecrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH)
    )


def _quote_password(password: str) -> str:
    """Render a password as a SQL literal, refusing anything not provably safe."""
    if not SAFE_PASSWORD.match(password):
        raise DatabaseError(
            "refusing to interpolate a password containing characters outside "
            "[A-Za-z0-9] into DDL"
        )
    return f"'{password}'"


class DatabaseError(Exception):
    """Provisioning or teardown failed."""


@dataclass
class Provisioned:
    """What the deploy pipeline needs in order to wire the app up."""

    kind: str
    env: dict[str, str] = field(default_factory=dict)
    volumes: dict[str, dict] = field(default_factory=dict)
    # Human-readable, never containing a password.
    connection_ref: str = ""


def volume_name(app_id: str) -> str:
    return f"hangar-data-{app_id}"


def identifier(app_id: str) -> str:
    """A Postgres-safe database and role name derived from the app id."""
    name = f"app_{app_id}".lower()
    if not SAFE_IDENTIFIER.match(name):
        raise DatabaseError(f"cannot derive a safe database name from {app_id!r}")
    return name


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def provision(sess, app) -> Provisioned:
    """Ensure the app's database exists and return how to attach it."""
    kind = app.db_type or config.settings().app_db
    if kind == "none":
        return Provisioned(kind="none")
    if kind == "sqlite":
        return _provision_sqlite(sess, app)
    if kind == "postgres":
        return _provision_postgres(sess, app)
    raise DatabaseError(f"unknown database type {kind!r}")


def deprovision(sess, app_id: str) -> None:
    """Destroy an app's database. Irreversible."""
    record = _record(sess, app_id)
    if record is None:
        return

    if record.db_type == "postgres":
        _drop_postgres(app_id)
    # The SQLite volume is removed by the runtime alongside the container,
    # since it is Docker that owns it.

    sess.delete(record)
    sess.commit()
    log.info("deprovisioned %s database for app %s", record.db_type, app_id)


def _record(sess, app_id: str) -> AppDatabase | None:
    from sqlmodel import select

    return sess.exec(
        select(AppDatabase).where(AppDatabase.app_id == app_id)
    ).first()


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------


def _provision_sqlite(sess, app) -> Provisioned:
    name = volume_name(app.id)
    path = f"{DATA_MOUNT}/{SQLITE_FILENAME}"
    # Four slashes: sqlite:/// plus an absolute path.
    url = f"sqlite:////{path.lstrip('/')}"

    if _record(sess, app.id) is None:
        sess.add(
            AppDatabase(app_id=app.id, db_type="sqlite", connection_ref=name)
        )
        sess.commit()

    return Provisioned(
        kind="sqlite",
        env={"DATABASE_URL": url, "HANGAR_DATA_DIR": DATA_MOUNT},
        # A named volume is writable even though the root filesystem is not,
        # which is what makes persistence possible under the §8 hardening.
        volumes={name: {"bind": DATA_MOUNT, "mode": "rw"}},
        connection_ref=name,
    )


# --------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------


def _admin_engine():
    admin_url = config.settings().app_db_admin_url
    if not admin_url:
        raise DatabaseError(
            "HANGAR_APP_DB_ADMIN_URL must point at a Postgres that Hangar may "
            "create databases and roles on"
        )
    # CREATE DATABASE cannot run inside a transaction block.
    return create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)


def _provision_postgres(sess, app) -> Provisioned:
    if not secrets.is_configured():
        raise DatabaseError(
            "postgres app databases need HANGAR_SECRET_KEY so the generated "
            "password is not stored in plaintext (`hangar gen-key`)"
        )

    name = identifier(app.id)
    record = _record(sess, app.id)

    if record is not None and record.secret:
        password = secrets.open_sealed(record.secret)
    else:
        password = generate_password()

    _create_postgres(name, password)

    if record is None:
        sess.add(
            AppDatabase(
                app_id=app.id,
                db_type="postgres",
                connection_ref=name,
                secret=secrets.seal(password),
            )
        )
        sess.commit()

    return Provisioned(
        kind="postgres",
        env={"DATABASE_URL": _app_url(name, password)},
        connection_ref=name,
    )


def _create_postgres(name: str, password: str) -> None:
    """Create the role and database if absent, idempotently."""
    engine = _admin_engine()
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}
            ).first()

            # No bind parameters are possible here: Postgres rejects them
            # throughout DDL, including CREATE ROLE ... PASSWORD. Both values
            # are therefore validated against strict patterns before being
            # interpolated — `identifier()` for the name, `_quote_password()`
            # for the secret — rather than escaped after the fact.
            literal = _quote_password(password)
            verb = "ALTER" if exists else "CREATE"
            conn.execute(text(f'{verb} ROLE "{name}" WITH LOGIN PASSWORD {literal}'))

            db_exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).first()
            if not db_exists:
                conn.execute(text(f'CREATE DATABASE "{name}" OWNER "{name}"'))

            # The app owns its own database and nothing else.
            conn.execute(text(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC'))
            conn.execute(
                text(f'GRANT ALL PRIVILEGES ON DATABASE "{name}" TO "{name}"')
            )
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(f"could not provision Postgres database: {exc}") from exc
    finally:
        engine.dispose()


def _drop_postgres(app_id: str) -> None:
    name = identifier(app_id)
    engine = _admin_engine()
    try:
        with engine.connect() as conn:
            # Open connections keep a database alive; close them first.
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            conn.execute(text(f'DROP ROLE IF EXISTS "{name}"'))
    except Exception as exc:
        raise DatabaseError(f"could not drop Postgres database: {exc}") from exc
    finally:
        engine.dispose()


def _app_url(name: str, password: str) -> str:
    """Rewrite the admin URL to point at the app's own database and role."""
    admin = urlsplit(config.settings().app_db_admin_url)
    host = admin.hostname or "localhost"
    port = f":{admin.port}" if admin.port else ""
    return f"postgresql://{name}:{password}@{host}{port}/{name}"


def redacted_url(record: AppDatabase) -> str:
    """Something safe to show an owner."""
    if record.db_type == "sqlite":
        return f"sqlite:///{DATA_MOUNT}/{SQLITE_FILENAME}"
    return f"postgresql://{record.connection_ref}:***@…/{record.connection_ref}"
