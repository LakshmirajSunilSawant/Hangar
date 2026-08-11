"""Identity: invitations, passwords, sessions."""

import pytest

from hangar import identity, store
from hangar.identity import IdentityError

PASSWORD = "correct-horse-battery"


@pytest.fixture
def sess(db):
    with store.session() as s:
        yield s


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def test_password_round_trip():
    hashed = identity.hash_password(PASSWORD)
    assert identity.verify_password(PASSWORD, hashed)
    assert not identity.verify_password("wrong", hashed)


def test_hash_is_not_the_password():
    assert PASSWORD not in identity.hash_password(PASSWORD)


def test_hashes_are_salted():
    """Identical passwords must not produce identical hashes."""
    assert identity.hash_password(PASSWORD) != identity.hash_password(PASSWORD)


def test_short_passwords_are_rejected():
    with pytest.raises(IdentityError, match="at least 10"):
        identity.hash_password("short")


def test_verifying_against_an_empty_hash_fails():
    """An invited-but-inactive user has no hash; that must not authenticate."""
    assert identity.verify_password("anything", "") is False


# --------------------------------------------------------------------------
# Invitations
# --------------------------------------------------------------------------


def test_invite_creates_an_inactive_user(sess):
    user, token = identity.invite(sess, "Alice@Example.com ")

    assert user.email == "alice@example.com"  # normalised
    assert user.is_active is False
    assert token


def test_invite_token_is_stored_hashed(sess):
    user, token = identity.invite(sess, "a@example.com")
    assert user.invite_hash and token not in user.invite_hash


def test_accepting_an_invite_activates_the_account(sess):
    _, token = identity.invite(sess, "a@example.com")
    user = identity.accept_invite(sess, token, PASSWORD)

    assert user.is_active
    assert identity.verify_password(PASSWORD, user.password_hash)


def test_an_invite_can_only_be_used_once(sess):
    """The stored hash is cleared on use, so the token stops matching at all."""
    _, token = identity.invite(sess, "a@example.com")
    user = identity.accept_invite(sess, token, PASSWORD)
    assert user.invite_hash == ""

    with pytest.raises(IdentityError, match="not valid"):
        identity.accept_invite(sess, token, "another-password")


def test_unknown_invite_tokens_are_rejected(sess):
    with pytest.raises(IdentityError, match="not valid"):
        identity.accept_invite(sess, "made-up", PASSWORD)


def test_reinviting_an_active_user_is_refused(sess):
    _, token = identity.invite(sess, "a@example.com")
    identity.accept_invite(sess, token, PASSWORD)

    with pytest.raises(IdentityError, match="already accepted"):
        identity.invite(sess, "a@example.com")


def test_reinviting_a_pending_user_issues_a_new_token(sess):
    """Losing the link shouldn't mean losing the account."""
    _, first = identity.invite(sess, "a@example.com")
    _, second = identity.invite(sess, "a@example.com")

    assert first != second
    identity.accept_invite(sess, second, PASSWORD)


@pytest.mark.parametrize("email", ["nope", "@example.com", "user@", ""])
def test_unusable_emails_are_rejected(sess, email):
    with pytest.raises(IdentityError, match="not a usable email"):
        identity.invite(sess, email)


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_authenticate_accepts_correct_credentials(sess):
    _, token = identity.invite(sess, "a@example.com")
    identity.accept_invite(sess, token, PASSWORD)

    user = identity.get_provider().authenticate(sess, "a@example.com", PASSWORD)
    assert user.email == "a@example.com"


def test_authenticate_rejects_a_wrong_password(sess):
    _, token = identity.invite(sess, "a@example.com")
    identity.accept_invite(sess, token, PASSWORD)

    with pytest.raises(IdentityError, match="invalid email or password"):
        identity.get_provider().authenticate(sess, "a@example.com", "nope")


def test_unknown_and_wrong_are_indistinguishable(sess):
    """The message must not reveal whether an account exists."""
    _, token = identity.invite(sess, "a@example.com")
    identity.accept_invite(sess, token, PASSWORD)

    with pytest.raises(IdentityError) as unknown:
        identity.get_provider().authenticate(sess, "nobody@example.com", PASSWORD)
    with pytest.raises(IdentityError) as wrong:
        identity.get_provider().authenticate(sess, "a@example.com", "nope")

    assert str(unknown.value) == str(wrong.value)


def test_pending_users_cannot_sign_in(sess):
    identity.invite(sess, "pending@example.com")
    with pytest.raises(IdentityError):
        identity.get_provider().authenticate(sess, "pending@example.com", PASSWORD)


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def make_user(sess, email="a@example.com"):
    _, token = identity.invite(sess, email)
    return identity.accept_invite(sess, token, PASSWORD)


def test_session_round_trip(sess):
    user = make_user(sess)
    _, token = identity.start_session(sess, user)

    assert identity.resolve_session(sess, token).id == user.id


def test_session_tokens_are_stored_hashed(sess):
    """A leaked database must not hand over live sessions."""
    user = make_user(sess)
    record, token = identity.start_session(sess, user)

    assert token not in record.token_hash
    assert record.token_hash == identity.hash_token(token)


def test_unknown_session_tokens_resolve_to_nobody(sess):
    assert identity.resolve_session(sess, "made-up") is None
    assert identity.resolve_session(sess, "") is None


def test_expired_sessions_are_rejected_and_cleared(sess, monkeypatch):
    from datetime import timedelta

    user = make_user(sess)
    record, token = identity.start_session(sess, user)
    record.expires_at = store.utcnow() - timedelta(seconds=1)
    sess.add(record)
    sess.commit()

    assert identity.resolve_session(sess, token) is None
    assert store.session_by_hash(sess, identity.hash_token(token)) is None


def test_logout_invalidates_the_session(sess):
    user = make_user(sess)
    _, token = identity.start_session(sess, user)

    identity.end_session(sess, token)
    assert identity.resolve_session(sess, token) is None


def test_unknown_identity_provider_is_reported(monkeypatch):
    monkeypatch.setenv("HANGAR_IDENTITY", "kratos")
    with pytest.raises(IdentityError, match="unknown identity provider"):
        identity.get_provider()
