import pytest
from fastapi.testclient import TestClient

from hangar import store
from hangar.api import api


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the store at a throwaway database for each test."""
    path = tmp_path / "hangar.db"
    monkeypatch.setenv("HANGAR_DB", str(path))
    store.reset_engine()
    store.engine(path)
    yield path
    store.reset_engine()


@pytest.fixture
def client(db):
    with TestClient(api) as c:
        yield c


@pytest.fixture
def docker_available():
    """True when a reachable Docker daemon is present."""
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False
