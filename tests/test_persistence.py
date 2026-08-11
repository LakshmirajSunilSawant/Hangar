"""Does data actually survive? Real Docker, real writes, real restart.

Everything else about per-app databases is plumbing. This is the property the
plumbing exists for, and the one that would be embarrassing to get wrong: an
app writes a row, the container is destroyed and recreated, and the row is
still there.
"""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hangar import database, runtime

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def require_docker(docker_available):
    if not docker_available:
        pytest.skip("Docker daemon not reachable")


@pytest.fixture
def notes_app(client):
    """Deploy the notes sample with a SQLite database; clean up after."""
    response = client.post(
        "/apps",
        json={
            "name": "notes-app",
            "source_path": str(EXAMPLES / "sqlite-notes"),
            "database": "sqlite",
        },
    )
    assert response.status_code == 202, response.text
    app_id = response.json()["id"]

    yield app_id, client.get(f"/apps/{app_id}").json()

    runtime.remove(app_id, missing_ok=True)
    runtime.remove_volume(app_id)


def get_json(url: str, timeout: float = 30.0):
    """Poll until the app answers, then return the parsed body."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            time.sleep(0.25)
    raise AssertionError(f"{url} never answered in {timeout}s (last: {last})")


def post_note(url: str, body: str) -> dict:
    request = urllib.request.Request(
        f"{url}/notes",
        data=json.dumps({"body": body}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode())


def test_data_survives_a_restart(notes_app, client):
    """The whole point of Milestone 4."""
    app_id, app = notes_app
    assert app["status"] == "running", app.get("error")

    url = app["url"]
    assert get_json(url)["count"] == 0

    post_note(url, "first note")
    post_note(url, "second note")
    assert get_json(url)["count"] == 2

    # Stop and start: a new container process, same volume.
    client.post(f"/apps/{app_id}/stop")
    restarted = client.post(f"/apps/{app_id}/restart").json()
    assert restarted["status"] == "running"

    after = get_json(restarted["url"])
    assert after["count"] == 2, "data was lost across a restart"
    assert [n["body"] for n in after["notes"]] == ["first note", "second note"]


def test_data_survives_a_redeploy(notes_app, client):
    """Redeploy rebuilds the image and replaces the container entirely."""
    app_id, app = notes_app
    url = app["url"]
    get_json(url)
    post_note(url, "written before redeploy")

    client.post(f"/apps/{app_id}/redeploy")
    fresh = client.get(f"/apps/{app_id}").json()
    assert fresh["status"] == "running", fresh.get("error")

    after = get_json(fresh["url"])
    assert after["count"] == 1
    assert after["notes"][0]["body"] == "written before redeploy"


def test_the_database_is_mounted_where_the_app_expects(notes_app):
    _, app = notes_app
    body = get_json(app["url"])
    assert body["database"] == "/data/app.db"


def test_without_a_database_the_app_cannot_write(client):
    """Confirms the problem this feature solves is real, not theoretical."""
    app_id = client.post(
        "/apps",
        json={
            "name": "unwritable-app",
            "source_path": str(EXAMPLES / "sqlite-notes"),
            "database": "none",
        },
    ).json()["id"]
    try:
        app = client.get(f"/apps/{app_id}").json()
        assert app["status"] == "running", app.get("error")

        # The app falls back to /tmp, which is a tmpfs — writable, but the
        # data is gone the moment the container is replaced.
        body = get_json(app["url"])
        assert body["database"].startswith("/tmp")
    finally:
        runtime.remove(app_id, missing_ok=True)


def test_deleting_the_app_destroys_the_volume(notes_app, client):
    app_id, app = notes_app
    get_json(app["url"])
    post_note(app["url"], "doomed")

    import docker as docker_sdk

    docker = docker_sdk.from_env()
    name = database.volume_name(app_id)
    assert docker.volumes.get(name)  # exists

    assert client.delete(f"/apps/{app_id}").status_code == 204

    with pytest.raises(Exception):
        docker.volumes.get(name)


def test_keep_data_leaves_the_volume_behind(notes_app, client):
    app_id, app = notes_app
    get_json(app["url"])

    import docker as docker_sdk

    docker = docker_sdk.from_env()
    name = database.volume_name(app_id)

    assert client.delete(f"/apps/{app_id}?keep_data=true").status_code == 204
    assert docker.volumes.get(name), "volume removed despite keep_data"
