"""Secret sealing.

PRD §8 forbids storing secrets in plaintext, and the per-app Postgres passwords
Hangar generates have to survive between provisioning and the next deploy.
"""

import base64

import pytest

from hangar import secrets
from hangar.secrets import SecretError

KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("HANGAR_SECRET_KEY", KEY)


def test_round_trip(keyed):
    assert secrets.open_sealed(secrets.seal("hunter2")) == "hunter2"


def test_ciphertext_does_not_contain_the_plaintext(keyed):
    assert "hunter2" not in secrets.seal("hunter2")


def test_each_sealing_differs(keyed):
    """A fresh nonce per call, so identical passwords don't look identical."""
    assert secrets.seal("same") != secrets.seal("same")


def test_unicode_survives(keyed):
    value = "pässwörd–✓"
    assert secrets.open_sealed(secrets.seal(value)) == value


def test_missing_key_refuses_rather_than_inventing_one(monkeypatch):
    """An auto-generated key would change on restart and orphan every secret."""
    monkeypatch.delenv("HANGAR_SECRET_KEY", raising=False)

    with pytest.raises(SecretError, match="hangar gen-key"):
        secrets.seal("x")
    assert secrets.is_configured() is False


def test_wrong_key_is_detected(keyed, monkeypatch):
    sealed = secrets.seal("hunter2")
    monkeypatch.setenv("HANGAR_SECRET_KEY", base64.b64encode(b"z" * 32).decode())

    with pytest.raises(SecretError, match="may have changed"):
        secrets.open_sealed(sealed)


def test_tampering_is_detected(keyed):
    """Authenticated encryption: a modified ciphertext must not decrypt."""
    sealed = secrets.seal("hunter2")
    raw = bytearray(base64.b64decode(sealed))
    raw[-1] ^= 0x01
    tampered = base64.b64encode(bytes(raw)).decode()

    with pytest.raises(SecretError):
        secrets.open_sealed(tampered)


@pytest.mark.parametrize(
    "key,match",
    [
        ("not base64!!", "base64"),
        (base64.b64encode(b"short").decode(), "32 bytes"),
    ],
)
def test_malformed_keys_are_rejected(monkeypatch, key, match):
    monkeypatch.setenv("HANGAR_SECRET_KEY", key)
    with pytest.raises(SecretError, match=match):
        secrets.seal("x")


def test_generated_keys_work(monkeypatch):
    monkeypatch.setenv("HANGAR_SECRET_KEY", secrets.generate_key())
    assert secrets.open_sealed(secrets.seal("x")) == "x"


def test_generated_keys_are_unique():
    assert secrets.generate_key() != secrets.generate_key()
