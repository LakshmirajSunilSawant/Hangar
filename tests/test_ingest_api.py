"""Ingestion through the API: zip upload and repo registration."""

import io
import zipfile

import pytest

from hangar import deploy as deploy_mod
from hangar import ingest, store

APP_FILES = {
    "requirements.txt": "fastapi\n",
    "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
}


@pytest.fixture(autouse=True)
def backend(fake_backend):
    return fake_backend


@pytest.fixture(autouse=True)
def sources(tmp_path, monkeypatch):
    monkeypatch.setenv("HANGAR_SOURCE_ROOT", str(tmp_path / "sources"))


def zip_bytes(entries=None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in (entries or APP_FILES).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def upload(client, data: bytes, name="zip-app", filename="app.zip"):
    return client.post(
        "/apps/upload",
        data={"name": name},
        files={"file": (filename, data, "application/zip")},
    )


# --------------------------------------------------------------------------
# Zip upload
# --------------------------------------------------------------------------


def test_upload_creates_and_deploys_an_app(client):
    response = upload(client, zip_bytes())
    assert response.status_code == 202

    body = response.json()
    assert body["source_type"] == "zip"
    assert body["source_ref"] == "app.zip"

    app = client.get(f"/apps/{body['id']}").json()
    assert app["status"] == "running", app.get("error")
    assert app["runtime"] == "python"


def test_uploaded_source_is_extracted_under_the_source_root(client):
    app_id = upload(client, zip_bytes()).json()["id"]

    with store.session() as sess:
        app = store.get_app(sess, app_id)

    assert app.source_dir
    assert (ingest.source_dir_for(app_id)).exists()


def test_upload_rejects_a_malicious_archive(client):
    """A traversal attempt must be a 422, not a written file."""
    response = upload(client, zip_bytes({"../../evil.txt": "pwned"}))

    assert response.status_code == 422
    assert "escapes" in response.json()["detail"]


def test_upload_rejects_a_non_zip(client):
    response = upload(client, b"definitely not a zip")
    assert response.status_code == 422
    assert "not a valid zip" in response.json()["detail"]


def test_upload_validates_the_name(client):
    assert upload(client, zip_bytes(), name="Bad Name!").status_code == 422


def test_upload_rejects_a_duplicate_name(client):
    upload(client, zip_bytes(), name="dupe-app")
    assert upload(client, zip_bytes(), name="dupe-app").status_code == 409


def test_rejected_upload_leaves_no_app_behind(client):
    upload(client, zip_bytes({"../../evil.txt": "x"}), name="ghost-app")
    assert [a for a in client.get("/apps").json() if a["name"] == "ghost-app"] == []


# --------------------------------------------------------------------------
# GitHub repos
# --------------------------------------------------------------------------


def create_repo_app(client, repo_url="https://github.com/owner/repo", **extra):
    return client.post(
        "/apps", json={"name": "repo-app", "repo_url": repo_url, **extra}
    )


def test_repo_url_is_accepted_and_normalised(client, monkeypatch):
    monkeypatch.setattr(deploy_mod, "deploy", lambda app_id: None)

    body = create_repo_app(client).json()
    assert body["source_type"] == "repo"
    assert body["source_ref"] == "owner/repo"


def test_ref_is_recorded(client, monkeypatch):
    monkeypatch.setattr(deploy_mod, "deploy", lambda app_id: None)
    body = create_repo_app(client, ref="v1.0").json()
    assert body["source_revision"] == "v1.0"


def test_ref_in_the_url_is_understood(client, monkeypatch):
    monkeypatch.setattr(deploy_mod, "deploy", lambda app_id: None)
    body = create_repo_app(client, "https://github.com/owner/repo/tree/dev").json()
    assert body["source_revision"] == "dev"


def test_malformed_repo_url_is_rejected_immediately(client):
    """A 422 now beats a failed deploy discovered by polling later."""
    response = create_repo_app(client, "https://gitlab.com/owner/repo")
    assert response.status_code == 422
    assert "GitHub repository" in response.json()["detail"]


def test_repo_app_fetches_and_deploys(client, monkeypatch, tmp_path):
    """The fetch is stubbed; everything after it is the real pipeline."""
    def fake_fetch(reference, app_id, ref=None):
        target = ingest.source_dir_for(app_id)
        target.mkdir(parents=True, exist_ok=True)
        for name, content in APP_FILES.items():
            (target / name).write_text(content, encoding="utf-8")
        return ingest.Repo("owner", "repo", ref)

    monkeypatch.setattr(deploy_mod.ingest, "from_github", fake_fetch)

    app_id = create_repo_app(client).json()["id"]
    app = client.get(f"/apps/{app_id}").json()

    assert app["status"] == "running", app.get("error")
    assert app["framework"] == "fastapi"


def test_repo_app_refetches_on_redeploy(client, monkeypatch):
    """Redeploy should mean "deploy what's on the branch now"."""
    fetches = []

    def fake_fetch(reference, app_id, ref=None):
        fetches.append(reference)
        target = ingest.source_dir_for(app_id)
        target.mkdir(parents=True, exist_ok=True)
        for name, content in APP_FILES.items():
            (target / name).write_text(content, encoding="utf-8")
        return ingest.Repo("owner", "repo", ref)

    monkeypatch.setattr(deploy_mod.ingest, "from_github", fake_fetch)

    app_id = create_repo_app(client).json()["id"]
    assert len(fetches) == 1

    client.post(f"/apps/{app_id}/redeploy")
    assert len(fetches) == 2


def test_fetch_failure_marks_the_app_failed(client, monkeypatch):
    def boom(reference, app_id, ref=None):
        raise ingest.IngestError("repository 'owner/repo' not found")

    monkeypatch.setattr(deploy_mod.ingest, "from_github", boom)

    app_id = create_repo_app(client).json()["id"]
    app = client.get(f"/apps/{app_id}").json()

    assert app["status"] == "failed"
    assert "not found" in app["error"]


# --------------------------------------------------------------------------
# Source selection and cleanup
# --------------------------------------------------------------------------


def test_exactly_one_source_must_be_given(client, tmp_path):
    both = client.post(
        "/apps",
        json={"name": "two-sources", "source_path": str(tmp_path), "repo_url": "o/r"},
    )
    neither = client.post("/apps", json={"name": "no-source"})

    assert both.status_code == 422
    assert neither.status_code == 422
    assert "exactly one" in both.json()["detail"]


def test_deleting_a_zip_app_removes_its_extracted_source(client):
    app_id = upload(client, zip_bytes()).json()["id"]
    assert ingest.source_dir_for(app_id).exists()

    assert client.delete(f"/apps/{app_id}").status_code == 204
    assert not ingest.source_dir_for(app_id).exists()


def test_deleting_a_path_app_leaves_the_users_directory_alone(client, tmp_path):
    """That directory belongs to the user, not to Hangar."""
    source = tmp_path / "mine"
    source.mkdir()
    for name, content in APP_FILES.items():
        (source / name).write_text(content, encoding="utf-8")

    app_id = client.post(
        "/apps", json={"name": "path-app", "source_path": str(source)}
    ).json()["id"]

    client.delete(f"/apps/{app_id}")
    assert source.exists()
    assert (source / "main.py").exists()
