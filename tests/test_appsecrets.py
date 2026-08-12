"""Per-app secrets: sealed at rest, injected at deploy, never readable back.

The properties worth testing here are mostly negative — what the API refuses
to return, what never reaches a log, what a rotated key does — because those
are the ones whose absence looks exactly like working software.
"""

import pytest

from hangar import appsecrets, secrets, store
from hangar import deploy as deploy_mod
from hangar.secrets import SecretError
from hangar.store import App, AppSecret

KEY = "c2VjcmV0LWtleS1mb3ItdGVzdHMtMzItYnl0ZXMtbG9uZyE="  # 32 bytes, base64


@pytest.fixture(autouse=True)
def sealing_key(monkeypatch):
    monkeypatch.setenv("HANGAR_SECRET_KEY", secrets.generate_key())


@pytest.fixture
def app_id(db):
    with store.session() as sess:
        app = App(name="notes", source_ref="/s", source_dir="/s")
        store.save(sess, app)
        return app.id


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["API_KEY", "S", "SLACK_TOKEN_2", "A_B_C"])
def test_conventional_names_are_accepted(name):
    assert appsecrets.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["lowercase", "1LEADING_DIGIT", "HAS-DASH", "HAS SPACE", "", "WITH.DOT"],
)
def test_names_that_are_not_environment_variables_are_refused(name):
    with pytest.raises(appsecrets.SecretNameError):
        appsecrets.validate_name(name)


@pytest.mark.parametrize("name", ["PORT", "HANGAR_APP_ID", "HANGAR_APP_NAME"])
def test_names_hangar_sets_itself_are_refused(name):
    """An app given a conflicting PORT is undebuggable from the dashboard."""
    with pytest.raises(appsecrets.SecretNameError, match="Hangar itself"):
        appsecrets.validate_name(name)


def test_database_url_is_refused_only_when_hangar_provides_storage():
    """An app with no per-app database may point at one somewhere else."""
    assert appsecrets.validate_name("DATABASE_URL", app_has_database=False)
    with pytest.raises(appsecrets.SecretNameError, match="database=none"):
        appsecrets.validate_name("DATABASE_URL", app_has_database=True)


def test_oversized_values_are_refused():
    with pytest.raises(appsecrets.SecretNameError, match="limit is"):
        appsecrets.validate_value("x" * (appsecrets.MAX_VALUE_BYTES + 1))


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_the_stored_value_is_not_the_plaintext(db, app_id):
    with store.session() as sess:
        appsecrets.put(sess, app_id, "API_KEY", "hunter2")
        stored = appsecrets.get(sess, app_id, "API_KEY").sealed_value

    assert "hunter2" not in stored
    assert secrets.open_sealed(stored) == "hunter2"


def test_setting_the_same_name_twice_replaces_rather_than_duplicates(db, app_id):
    with store.session() as sess:
        appsecrets.put(sess, app_id, "API_KEY", "first")
        appsecrets.put(sess, app_id, "API_KEY", "second")

        assert len(appsecrets.list_for(sess, app_id)) == 1
        assert appsecrets.env_for(sess, app_id) == {"API_KEY": "second"}


def test_each_sealing_uses_a_fresh_nonce(db, app_id):
    """Identical values must not produce identical ciphertext."""
    with store.session() as sess:
        appsecrets.put(sess, app_id, "A", "same")
        appsecrets.put(sess, app_id, "B", "same")
        sealed = {r.name: r.sealed_value for r in appsecrets.list_for(sess, app_id)}

    assert sealed["A"] != sealed["B"]


def test_secrets_are_scoped_to_one_app(db, app_id):
    with store.session() as sess:
        other = App(name="other", source_ref="/s")
        store.save(sess, other)
        appsecrets.put(sess, app_id, "API_KEY", "mine")

        assert appsecrets.env_for(sess, other.id) == {}


def test_a_rotated_key_is_reported_rather_than_silently_dropped(
    db, app_id, monkeypatch
):
    """Starting an app without the credentials it needs would crash-loop."""
    with store.session() as sess:
        appsecrets.put(sess, app_id, "API_KEY", "hunter2")

    monkeypatch.setenv("HANGAR_SECRET_KEY", secrets.generate_key())
    with store.session() as sess:
        with pytest.raises(SecretError, match="may have changed"):
            appsecrets.env_for(sess, app_id)


# --------------------------------------------------------------------------
# The API
# --------------------------------------------------------------------------


def test_the_api_never_returns_a_value(client, app_id):
    client.put(f"/apps/{app_id}/secrets/API_KEY", json={"value": "hunter2"})

    response = client.get(f"/apps/{app_id}/secrets")
    body = response.text
    assert response.json()[0]["name"] == "API_KEY"
    assert "hunter2" not in body
    assert "value" not in response.json()[0]


def test_setting_a_secret_reports_when_it_was_set(client, app_id):
    body = client.put(f"/apps/{app_id}/secrets/API_KEY", json={"value": "x"}).json()
    assert body["name"] == "API_KEY"
    assert body["created_at"]


def test_a_reserved_name_is_refused_with_a_reason(client, app_id):
    response = client.put(f"/apps/{app_id}/secrets/PORT", json={"value": "9"})
    assert response.status_code == 422
    assert "Hangar itself" in response.json()["detail"]


def test_deleting_a_secret(client, app_id):
    client.put(f"/apps/{app_id}/secrets/API_KEY", json={"value": "x"})

    assert client.delete(f"/apps/{app_id}/secrets/API_KEY").status_code == 204
    assert client.get(f"/apps/{app_id}/secrets").json() == []


def test_deleting_a_secret_that_is_not_there(client, app_id):
    assert client.delete(f"/apps/{app_id}/secrets/NOPE").status_code == 404


def test_secrets_for_an_unknown_app_are_a_404(client, db):
    assert client.get("/apps/nope/secrets").status_code == 404


def test_without_a_sealing_key_the_value_is_refused_not_stored(
    client, app_id, monkeypatch
):
    """Storing it in plaintext instead is the one thing PRD §8 forbids."""
    monkeypatch.delenv("HANGAR_SECRET_KEY", raising=False)

    response = client.put(f"/apps/{app_id}/secrets/API_KEY", json={"value": "x"})
    assert response.status_code == 503
    with store.session() as sess:
        assert appsecrets.list_for(sess, app_id) == []


def test_deleting_an_app_takes_its_secrets_with_it(client, app_id, fake_backend):
    """Otherwise credentials outlive the thing they were for."""
    client.put(f"/apps/{app_id}/secrets/API_KEY", json={"value": "x"})
    client.delete(f"/apps/{app_id}")

    with store.session() as sess:
        assert sess.get(AppSecret, app_id) is None
        assert appsecrets.list_for(sess, app_id) == []


# --------------------------------------------------------------------------
# Injection
# --------------------------------------------------------------------------


def test_secrets_reach_the_container(db, app_id, fake_backend, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (source / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    with store.session() as sess:
        app = store.get_app(sess, app_id)
        app.source_dir = str(source)
        store.save(sess, app)
        appsecrets.put(sess, app_id, "API_KEY", "hunter2")

    deploy_mod.deploy(app_id)

    assert fake_backend.last_env["API_KEY"] == "hunter2"


def test_the_deploy_log_records_names_but_never_values(
    db, app_id, fake_backend, tmp_path
):
    """Deploy logs are shown in the dashboard and readable by editors."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (source / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    with store.session() as sess:
        app = store.get_app(sess, app_id)
        app.source_dir = str(source)
        store.save(sess, app)
        appsecrets.put(sess, app_id, "API_KEY", "hunter2")

    deploy_mod.deploy(app_id)

    with store.session() as sess:
        log = store.latest_deployment(sess, app_id).build_log

    assert "API_KEY" in log
    assert "hunter2" not in log
