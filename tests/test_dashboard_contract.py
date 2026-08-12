"""The API must return exactly what the dashboard's TypeScript expects.

The dashboard is typed against these responses (dashboard/src/types.ts), but
TypeScript checks nothing at runtime — a renamed or dropped field type-checks
on the frontend and then renders `undefined` in the browser. These tests read
the field names out of types.ts and assert the API actually produces them, so
the two can't drift apart silently.
"""

import re
from pathlib import Path

import pytest

from hangar import api as api_mod
from hangar import deploy as deploy_mod
from hangar import store

TYPES_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "src" / "types.ts"


@pytest.fixture(autouse=True)
def backend(fake_backend):
    return fake_backend


@pytest.fixture(autouse=True)
def no_real_deploys(monkeypatch):
    monkeypatch.setattr(deploy_mod, "deploy", lambda app_id: None)
    monkeypatch.setattr(api_mod.deploy_mod, "deploy", lambda app_id: None)


def interface_fields(name: str) -> set[str]:
    """Field names declared on a TypeScript interface."""
    source = TYPES_FILE.read_text(encoding="utf-8")
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", source, re.DOTALL)
    if not match:
        pytest.fail(f"interface {name} not found in {TYPES_FILE}")
    return set(re.findall(r"^\s*(\w+)[?]?:", match.group(1), re.MULTILINE))


@pytest.fixture
def app_id(client, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (source / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    return client.post(
        "/apps", json={"name": "contract-app", "source_path": str(source)}
    ).json()["id"]


def test_app_response_matches_the_typescript_interface(client, app_id):
    body = client.get(f"/apps/{app_id}").json()
    assert interface_fields("App") <= set(body)


def test_logs_response_matches(client, app_id):
    assert interface_fields("Logs") <= set(client.get(f"/apps/{app_id}/logs").json())


def test_scan_response_matches(client, app_id):
    with store.session() as sess:
        sess.add(store.Deployment(app_id=app_id, scan_report="{}"))
        sess.commit()

    assert interface_fields("Scan") <= set(client.get(f"/apps/{app_id}/scan").json())


def test_health_response_matches(client):
    assert interface_fields("Health") <= set(client.get("/healthz").json())


def test_metrics_response_matches(client, app_id):
    assert interface_fields("Metrics") <= set(
        client.get(f"/apps/{app_id}/metrics").json()
    )


def test_sample_shape_matches(client, app_id):
    """Samples are plotted field by field, so a rename would draw a flat line."""
    from hangar import metrics

    metrics.HISTORY.record(
        app_id,
        metrics.Sample(
            at=1.0, cpu_percent=1.0, memory_mb=1.0, memory_limit_mb=512.0
        ),
    )

    samples = client.get(f"/apps/{app_id}/metrics").json()["samples"]
    assert interface_fields("Sample") <= set(samples[0])


def test_finding_shape_matches(client, app_id):
    """Findings are rendered field by field, so a rename would show blanks."""
    report = (
        '{"findings": [{"tool": "t", "rule": "r", "severity": "high", '
        '"message": "m", "file": "f.py", "line": 1}], "counts": {}, '
        '"tools_run": [], "tools_skipped": {}, "highest_severity": "high"}'
    )
    with store.session() as sess:
        sess.add(store.Deployment(app_id=app_id, scan_report=report))
        sess.commit()

    findings = client.get(f"/apps/{app_id}/scan").json()["findings"]
    assert interface_fields("Finding") <= set(findings[0])


def declared_union(source: str, name: str) -> set[str]:
    """The string literals of a TypeScript union type.

    Comments are stripped first: a `;` inside one would otherwise end the
    match early, and this test would quietly go on passing while checking only
    the members above it.
    """
    body = re.search(
        rf"export type {name} =(.*?);",
        re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.DOTALL),
        re.DOTALL,
    )
    assert body, f"no `export type {name}` in types.ts"
    return set(re.findall(r'"(\w+)"', body.group(1)))


def test_app_status_values_are_all_known_to_the_frontend():
    """An unmapped status renders an undefined CSS class and no colour."""
    declared = declared_union(TYPES_FILE.read_text(encoding="utf-8"), "AppStatus")
    assert {s.value for s in store.AppStatus} == declared


def test_the_union_check_would_notice_a_missing_status():
    """Guard the guard — this check has been silently narrowed once already."""
    source = 'export type AppStatus =\n  | "running"\n  /* a; comment */\n  | "failed";\n'
    assert declared_union(source, "AppStatus") == {"running", "failed"}


def test_scan_status_values_are_all_known_to_the_frontend():
    source = TYPES_FILE.read_text(encoding="utf-8")
    declared = set(
        re.findall(r'"(\w+)"', re.search(r'status: "skipped"(.*?);', source).group(0))
    )
    assert {s.value for s in store.ScanStatus} <= declared


def test_source_types_are_all_known_to_the_frontend():
    source = TYPES_FILE.read_text(encoding="utf-8")
    declared = set(
        re.findall(r'"(\w+)"', re.search(r"export type SourceType =(.*?);", source).group(1))
    )
    assert {"path", "zip", "repo"} == declared


# --------------------------------------------------------------------------
# Identity and sharing types
# --------------------------------------------------------------------------


def test_whoami_response_matches(client):
    assert interface_fields("WhoAmI") <= set(client.get("/auth/me").json())


def test_grant_response_matches(client, app_id, monkeypatch):
    from hangar import identity, store

    with store.session() as sess:
        _, token = identity.invite(sess, "person@example.com")
        identity.accept_invite(sess, token, "correct-horse-battery")

    # Anonymous access stops the moment any account exists, so this needs a
    # credential even though the rest of the module doesn't.
    monkeypatch.setenv("HANGAR_API_TOKEN", "contract-token")

    response = client.put(
        f"/apps/{app_id}/access",
        json={"email": "person@example.com", "role": "viewer"},
        headers={"Authorization": "Bearer contract-token"},
    )
    assert response.status_code == 200, response.text
    assert interface_fields("Grant") <= set(response.json())


def test_user_and_invite_responses_match(client):
    body = client.post("/users", json={"email": "new@example.com"}).json()

    assert interface_fields("Invite") <= set(body)
    assert interface_fields("User") <= set(body["user"])


def test_roles_are_all_known_to_the_frontend():
    """An unmapped role renders no summary text in the sharing panel."""
    import re

    from hangar.store import Role

    source = TYPES_FILE.read_text(encoding="utf-8")
    declared = set(
        re.findall(r'"(\w+)"', re.search(r"export type Role =(.*?);", source).group(1))
    )
    assert {r.value for r in Role} == declared


def test_role_summaries_cover_every_role():
    import re

    from hangar.store import Role

    source = TYPES_FILE.read_text(encoding="utf-8")
    block = re.search(r"ROLE_SUMMARY: Record<Role, string> = \{(.*?)\};", source, re.DOTALL)
    described = set(re.findall(r"^\s*(\w+):", block.group(1), re.MULTILINE))
    assert {r.value for r in Role} == described
