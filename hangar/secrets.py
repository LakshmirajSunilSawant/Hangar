"""Encryption for secrets held by the control plane.

PRD §8: "Secrets are never stored in plaintext." The per-app database
passwords Hangar generates have to live somewhere between provisioning and the
next deploy, and that somewhere is the control-plane database — so they are
sealed with libsodium's secretbox (XSalsa20-Poly1305, authenticated) under a
key supplied through the environment.

The key is deliberately *not* auto-generated when missing. A key that changes
on restart silently turns every stored secret into undecryptable noise, and the
failure only shows up later when an app can't reach its database. Refusing to
start is the kinder failure.

This is the PRD's "simpler" option. OpenBao remains the right answer once
secrets outlive a single control plane.
"""

from __future__ import annotations

import base64

from nacl import encoding, exceptions, secret, utils

from . import config

KEY_BYTES = secret.SecretBox.KEY_SIZE  # 32


class SecretError(Exception):
    """A secret could not be sealed or opened."""


def generate_key() -> str:
    """A fresh base64 key, for HANGAR_SECRET_KEY."""
    return base64.b64encode(utils.random(KEY_BYTES)).decode()


def _box() -> secret.SecretBox:
    raw = config.settings().secret_key
    if not raw:
        raise SecretError(
            "HANGAR_SECRET_KEY is not set, so secrets cannot be stored safely. "
            "Generate one with `hangar gen-key` and keep it somewhere durable — "
            "losing it makes existing secrets unrecoverable."
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise SecretError("HANGAR_SECRET_KEY must be base64") from exc

    if len(key) != KEY_BYTES:
        raise SecretError(
            f"HANGAR_SECRET_KEY must decode to {KEY_BYTES} bytes, got {len(key)}"
        )
    return secret.SecretBox(key)


def seal(plaintext: str) -> str:
    """Encrypt for storage. Each call uses a fresh nonce."""
    return _box().encrypt(plaintext.encode(), encoder=encoding.Base64Encoder).decode()


def open_sealed(token: str) -> str:
    """Decrypt a value produced by `seal`."""
    try:
        return _box().decrypt(token.encode(), encoder=encoding.Base64Encoder).decode()
    except exceptions.CryptoError as exc:
        # Authenticated encryption, so this means the wrong key or tampering —
        # not a corrupted-but-usable value.
        raise SecretError(
            "could not decrypt a stored secret — HANGAR_SECRET_KEY may have "
            "changed since it was written"
        ) from exc


def is_configured() -> bool:
    try:
        _box()
        return True
    except SecretError:
        return False
