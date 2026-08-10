"""Configuration resolution tests."""

import pytest

from hangar import config


def test_defaults_to_local_sqlite(monkeypatch, tmp_path):
    monkeypatch.delenv("HANGAR_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.database_url().startswith("sqlite:///")


def test_hangar_database_url_wins_over_generic(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://a/generic")
    monkeypatch.setenv("HANGAR_DATABASE_URL", "postgresql://a/explicit")
    assert "explicit" in config.database_url()


def test_generic_database_url_is_used_when_hangar_specific_is_absent(monkeypatch):
    """Render and most hosts inject DATABASE_URL, not our own name."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@host/db")
    assert config.database_url() == "postgresql+psycopg://user:pw@host/db"


@pytest.mark.parametrize(
    "given,expected",
    [
        # Render and Heroku hand out the scheme SQLAlchemy dropped.
        ("postgres://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        # Bare postgresql:// would select psycopg2, which we don't install.
        ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        # Already explicit — leave alone.
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("sqlite:///local.db", "sqlite:///local.db"),
    ],
)
def test_database_url_normalisation(given, expected):
    assert config.normalise_database_url(given) == expected


def test_auth_is_disabled_without_a_token(monkeypatch):
    monkeypatch.delenv("HANGAR_API_TOKEN", raising=False)
    assert config.settings().auth_enabled is False


def test_blank_token_counts_as_unset(monkeypatch):
    """An empty env var is a common deploy mistake; it must not look like auth."""
    monkeypatch.setenv("HANGAR_API_TOKEN", "   ")
    assert config.settings().auth_enabled is False


def test_resource_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("HANGAR_APP_MEMORY_MB", "256")
    monkeypatch.setenv("HANGAR_APP_CPUS", "0.25")
    monkeypatch.setenv("HANGAR_APP_PIDS", "64")

    settings = config.settings()
    assert (settings.memory_mb, settings.cpus, settings.pids) == (256, 0.25, 64)


def test_bad_numeric_setting_fails_loudly(monkeypatch):
    monkeypatch.setenv("HANGAR_APP_MEMORY_MB", "lots")
    with pytest.raises(ValueError, match="HANGAR_APP_MEMORY_MB"):
        config.settings()


def test_public_base_url_drives_app_urls(monkeypatch):
    monkeypatch.setenv("HANGAR_PUBLIC_BASE_URL", "https://apps.example.com/")
    assert config.settings().url_for_port(8000) == "https://apps.example.com:8000"


def test_settings_are_not_cached_across_env_changes(monkeypatch):
    monkeypatch.setenv("HANGAR_BACKEND", "docker")
    assert config.settings().backend == "docker"
    monkeypatch.setenv("HANGAR_BACKEND", "fake")
    assert config.settings().backend == "fake"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts(host):
    assert config.is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "10.0.0.1"])
def test_non_loopback_hosts(host):
    assert not config.is_loopback(host)
