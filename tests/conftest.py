import pytest
from fastapi.testclient import TestClient

from hangar import backends, store
from hangar.api import api
from hangar.backends.base import (
    BuildResult,
    DeployError,
    ExecutionBackend,
    RunningApp,
)

# Env vars that would otherwise leak a developer's real configuration into the
# tests — a set DATABASE_URL would silently point the suite at a real database.
ISOLATED_ENV = (
    "HANGAR_DATABASE_URL",
    "DATABASE_URL",
    "HANGAR_API_TOKEN",
    "HANGAR_BACKEND",
    "HANGAR_RUNTIME",
    "HANGAR_PUBLIC_BASE_URL",
    "HANGAR_APP_MEMORY_MB",
    "HANGAR_APP_CPUS",
    "HANGAR_APP_PIDS",
    # A set idle timeout would start a reaper thread under every test and stop
    # apps mid-assertion.
    "HANGAR_IDLE_TIMEOUT",
    "HANGAR_IDLE_CHECK_INTERVAL",
    "HANGAR_METRICS_INTERVAL",
    "HANGAR_METRICS_HISTORY",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for name in ISOLATED_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def clean_process_state():
    """Last-seen times and samples are process-global; leaking them is a trap."""
    from hangar import idle, metrics

    idle.TRACKER.clear()
    metrics.HISTORY.clear()
    yield
    idle.TRACKER.clear()
    metrics.HISTORY.clear()


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
def client(db, monkeypatch):
    # Entering TestClient runs the app's lifespan, which starts background
    # threads for whatever is enabled. Metrics are on by default, and a
    # collector polling Docker underneath every test is noise at best and a
    # race against the per-test database at worst; tests that want one build
    # it themselves.
    monkeypatch.setenv("HANGAR_METRICS_INTERVAL", "0")
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


# --------------------------------------------------------------------------
# A backend that does nothing, for tests that shouldn't touch Docker
# --------------------------------------------------------------------------


class FakeState:
    """Shared, resettable state — backends are instantiated per call."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.calls: list[tuple[str, str]] = []
        self.errors: dict[str, str] = {}
        self.port: int | None = None
        self.runtime_log: str = ""
        self.is_available: bool = True
        # What the deploy pipeline last handed the backend.
        self.last_env: dict[str, str] = {}
        self.last_volumes: dict[str, dict] = {}
        self.removed_data: list[str] = []

    def record(self, method: str, app_id: str) -> None:
        self.calls.append((method, app_id))
        if method in self.errors:
            raise DeployError(self.errors[method])

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


FAKE = FakeState()


class FakeBackend(ExecutionBackend):
    name = "fake"

    def available(self) -> bool:
        return FAKE.is_available

    def build(self, source_dir, detection, tag, *, on_log=None) -> BuildResult:
        FAKE.record("build", tag)
        return BuildResult(
            image_tag=tag, dockerfile="", log="", used_existing_dockerfile=False
        )

    def run(
        self,
        image_tag,
        app_id,
        app_name,
        container_port,
        *,
        env=None,
        limits=None,
        volumes=None,
    ):
        FAKE.record("run", app_id)
        FAKE.last_env = dict(env or {})
        FAKE.last_volumes = dict(volumes or {})
        port = FAKE.port or 12345
        return RunningApp(
            container_id="fake",
            container_name=f"hangar-{app_id}",
            container_port=container_port,
            host_port=port,
            upstream=f"127.0.0.1:{port}",
            status="running",
        )

    def status(self, app_id: str) -> str:
        FAKE.record("status", app_id)
        return "running"

    def logs(self, app_id: str, *, tail: int = 200) -> str:
        FAKE.record("logs", app_id)
        return FAKE.runtime_log

    def stop(self, app_id: str) -> None:
        FAKE.record("stop", app_id)

    def start(self, app_id: str) -> None:
        FAKE.record("start", app_id)

    def restart(self, app_id: str) -> None:
        FAKE.record("restart", app_id)

    def remove(self, app_id: str, *, missing_ok: bool = False) -> None:
        FAKE.record("remove", app_id)

    def host_port(self, app_id: str) -> int | None:
        FAKE.record("host_port", app_id)
        return FAKE.port

    def remove_data(self, app_id: str) -> bool:
        FAKE.record("remove_data", app_id)
        FAKE.removed_data.append(app_id)
        return True


@pytest.fixture
def fake_backend(monkeypatch):
    """Select a no-op execution backend and hand back its state for assertions."""
    backends.register("fake", FakeBackend)
    monkeypatch.setenv("HANGAR_BACKEND", "fake")
    FAKE.reset()
    yield FAKE
    FAKE.reset()
