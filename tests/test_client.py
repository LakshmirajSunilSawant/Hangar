"""The deploy CLI and its HTTP client."""

import zipfile
from io import BytesIO

import pytest

from hangar import cli
from hangar.client import Client, ClientError, zip_directory


# --------------------------------------------------------------------------
# Packaging a directory
# --------------------------------------------------------------------------


def names(data: bytes) -> set[str]:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        return set(archive.namelist())


def test_zips_the_source_tree(tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "util.py").write_text("y = 2\n", encoding="utf-8")

    assert names(zip_directory(tmp_path)) == {"main.py", "sub/util.py"}


def test_skips_vendored_directories(tmp_path):
    """node_modules would dwarf the source, and the build reinstalls anyway."""
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    for junk in ("node_modules", ".venv", ".git", "__pycache__"):
        (tmp_path / junk).mkdir()
        (tmp_path / junk / "big.bin").write_text("x" * 1000, encoding="utf-8")

    assert names(zip_directory(tmp_path)) == {"main.py"}


def test_refuses_an_empty_directory(tmp_path):
    with pytest.raises(ClientError, match="no files to deploy"):
        zip_directory(tmp_path)


def test_refuses_a_file(tmp_path):
    target = tmp_path / "not-a-dir.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ClientError, match="not a directory"):
        zip_directory(target)


# --------------------------------------------------------------------------
# Default names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,is_dir,expected",
    [
        ("/srv/apps/My Tool", True, "my-tool"),
        ("owner/repo", False, "repo"),
        ("https://github.com/owner/my-repo.git", False, "my-repo"),
        ("https://github.com/owner/repo/", False, "repo"),
        ("/srv/UPPER_case", True, "upper-case"),
    ],
)
def test_default_names_are_valid_app_names(source, is_dir, expected):
    from hangar.api import NAME_PATTERN

    name = cli._default_name(source, is_dir)
    assert name == expected
    assert NAME_PATTERN.match(name), f"{name!r} would be rejected by the API"


# --------------------------------------------------------------------------
# Client behaviour against a stub transport
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"x"
        self.text = text

    def json(self):
        return self._payload


def test_errors_surface_the_servers_detail(monkeypatch):
    monkeypatch.setattr(
        "hangar.client.requests.request",
        lambda *a, **kw: FakeResponse(409, {"detail": "an app named 'x' already exists"}),
    )
    with pytest.raises(ClientError, match="already exists"):
        Client("http://h").get("abc")


def test_validation_errors_are_readable(monkeypatch):
    """FastAPI returns a list of objects; a raw repr would be unreadable."""
    monkeypatch.setattr(
        "hangar.client.requests.request",
        lambda *a, **kw: FakeResponse(
            422, {"detail": [{"loc": ["body", "name"], "msg": "field required"}]}
        ),
    )
    with pytest.raises(ClientError, match="field required"):
        Client("http://h").get("abc")


def test_unreachable_control_plane_is_reported_clearly(monkeypatch):
    import requests

    def boom(*a, **kw):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("hangar.client.requests.request", boom)
    with pytest.raises(ClientError, match="could not reach"):
        Client("http://h").health()


def test_token_is_sent_when_given(monkeypatch):
    seen = {}

    def capture(method, url, headers=None, **kw):
        seen.update(headers or {})
        return FakeResponse(200, {})

    monkeypatch.setattr("hangar.client.requests.request", capture)
    Client("http://h", token="secret").health()
    assert seen["Authorization"] == "Bearer secret"


def test_no_authorization_header_without_a_token(monkeypatch):
    seen = {}

    def capture(method, url, headers=None, **kw):
        seen.update(headers or {})
        return FakeResponse(200, {})

    monkeypatch.setattr("hangar.client.requests.request", capture)
    Client("http://h").health()
    assert "Authorization" not in seen


def test_wait_returns_once_the_status_settles(monkeypatch):
    statuses = iter(["queued", "building", "building", "running"])
    monkeypatch.setattr(
        "hangar.client.requests.request",
        lambda *a, **kw: FakeResponse(200, {"status": next(statuses), "id": "x"}),
    )
    monkeypatch.setattr("hangar.client.POLL_INTERVAL", 0)

    assert Client("http://h").wait("x")["status"] == "running"


def test_wait_reports_each_new_status_once(monkeypatch):
    statuses = iter(["queued", "queued", "building", "running"])
    monkeypatch.setattr(
        "hangar.client.requests.request",
        lambda *a, **kw: FakeResponse(200, {"status": next(statuses), "id": "x"}),
    )
    monkeypatch.setattr("hangar.client.POLL_INTERVAL", 0)

    seen = []
    Client("http://h").wait("x", on_status=seen.append)
    assert seen == ["queued", "building", "running"]


def test_wait_gives_up_rather_than_hanging(monkeypatch):
    monkeypatch.setattr(
        "hangar.client.requests.request",
        lambda *a, **kw: FakeResponse(200, {"status": "building", "id": "x"}),
    )
    monkeypatch.setattr("hangar.client.POLL_INTERVAL", 0)

    with pytest.raises(ClientError, match="still building"):
        Client("http://h").wait("x", timeout=0.05)


def test_failed_deploy_exits_nonzero_and_shows_the_build_log(monkeypatch, capsys, tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    def responses(method, url, **kw):
        if url.endswith("/apps"):
            return FakeResponse(200, [])
        if url.endswith("/apps/upload"):
            return FakeResponse(200, {"id": "abc", "status": "queued"})
        if url.endswith("/logs"):
            return FakeResponse(200, {"build_log": "step 1\nERROR: boom"})
        return FakeResponse(200, {"id": "abc", "status": "failed", "error": "boom"})

    monkeypatch.setattr("hangar.client.requests.request", responses)
    monkeypatch.setattr("hangar.client.POLL_INTERVAL", 0)

    assert cli.main(["deploy", str(tmp_path), "--name", "thing"]) == 1
    err = capsys.readouterr().err
    assert "deploy failed" in err
    assert "ERROR: boom" in err


def test_successful_deploy_prints_only_the_url(monkeypatch, capsys, tmp_path):
    """So it can be piped: URL on stdout, progress on stderr."""
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    def responses(method, url, **kw):
        if url.endswith("/apps"):
            return FakeResponse(200, [])
        if url.endswith("/apps/upload"):
            return FakeResponse(200, {"id": "abc", "status": "queued"})
        return FakeResponse(
            200, {"id": "abc", "status": "running", "url": "https://thing.example.com"}
        )

    monkeypatch.setattr("hangar.client.requests.request", responses)
    monkeypatch.setattr("hangar.client.POLL_INTERVAL", 0)

    assert cli.main(["deploy", str(tmp_path), "--name", "thing"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "https://thing.example.com"


def test_deploying_an_existing_name_redeploys(monkeypatch, capsys, tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    calls = []

    def responses(method, url, **kw):
        calls.append((method, url))
        if url.endswith("/apps") and method == "GET":
            return FakeResponse(200, [{"id": "abc", "name": "thing"}])
        return FakeResponse(
            200, {"id": "abc", "status": "running", "url": "https://thing.example.com"}
        )

    monkeypatch.setattr("hangar.client.requests.request", responses)
    monkeypatch.setattr("hangar.client.POLL_INTERVAL", 0)

    assert cli.main(["deploy", str(tmp_path), "--name", "thing"]) == 0
    assert any(url.endswith("/redeploy") for _, url in calls)
    assert not any(url.endswith("/upload") for _, url in calls)
