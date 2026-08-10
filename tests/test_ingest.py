"""Ingestion tests, weighted towards hostile archives.

Extraction happens on the control-plane host, before any sandbox exists, so a
path-traversal bug here writes to the host filesystem as the Hangar process.
That makes these the highest-stakes tests in the suite.
"""

import io
import tarfile
import zipfile

import pytest

from hangar import config, ingest
from hangar.ingest import IngestError, Repo, parse_repo


@pytest.fixture(autouse=True)
def sources(tmp_path, monkeypatch):
    monkeypatch.setenv("HANGAR_SOURCE_ROOT", str(tmp_path / "sources"))
    return tmp_path / "sources"


def make_zip(entries: dict[str, str], symlinks: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            # 0xA1FF = S_IFLNK | 0777, shifted into the Unix-mode half.
            info.external_attr = 0xA1FF << 16
            archive.writestr(info, target)
    return buffer.getvalue()


def make_tar(entries: dict[str, str], extra_members=None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in entries.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra_members or []:
            archive.addfile(info)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Zip Slip and friends
# --------------------------------------------------------------------------


def test_rejects_parent_directory_traversal(tmp_path):
    data = make_zip({"app/main.py": "x = 1\n", "../../escaped.txt": "pwned"})

    with pytest.raises(IngestError, match="escapes the extraction directory"):
        ingest.from_zip(data, "app1")

    assert not (tmp_path.parent / "escaped.txt").exists()


def test_rejects_absolute_paths(tmp_path):
    data = make_zip({"/etc/cron.d/evil": "* * * * * root sh"})
    with pytest.raises(IngestError, match="escapes the extraction directory"):
        ingest.from_zip(data, "app2")


def test_rejects_windows_absolute_paths():
    data = make_zip({"C:/Windows/System32/evil.dll": "x"})
    with pytest.raises(IngestError, match="escapes the extraction directory"):
        ingest.from_zip(data, "app3")


def test_rejects_deeply_nested_traversal():
    data = make_zip({"a/b/../../../../../../tmp/evil": "x"})
    with pytest.raises(IngestError, match="escapes"):
        ingest.from_zip(data, "app4")


def test_symlinks_are_dropped_not_extracted(sources):
    """A symlink's own path looks innocent while pointing anywhere on the host."""
    data = make_zip(
        {"app/main.py": "x = 1\n"},
        symlinks={"app/passwd": "/etc/passwd"},
    )
    extracted = ingest.from_zip(data, "app5")

    assert (extracted / "main.py").exists()
    assert not (extracted / "passwd").exists()


def test_failed_extraction_leaves_nothing_behind(sources):
    with pytest.raises(IngestError):
        ingest.from_zip(make_zip({"../evil": "x"}), "app6")

    assert not ingest.source_dir_for("app6").exists()


# --------------------------------------------------------------------------
# Resource exhaustion
# --------------------------------------------------------------------------


def test_rejects_too_many_entries(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_ENTRIES", 5)
    data = make_zip({f"f{i}.txt": "x" for i in range(10)})

    with pytest.raises(IngestError, match="entries, over"):
        ingest.from_zip(data, "app7")


def test_rejects_archives_that_expand_too_far(monkeypatch):
    """A zip bomb is small on disk and enormous once extracted."""
    monkeypatch.setattr(ingest, "MAX_UNCOMPRESSED_BYTES", 1024)
    data = make_zip({"big.txt": "A" * 10_000})

    with pytest.raises(IngestError, match="expands to"):
        ingest.from_zip(data, "app8")


def test_rejects_oversized_uploads(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_ARCHIVE_BYTES", 100)
    with pytest.raises(IngestError, match="over the"):
        ingest.from_zip(b"x" * 500, "app9")


def test_rejects_empty_uploads():
    with pytest.raises(IngestError, match="empty"):
        ingest.from_zip(b"", "app10")


def test_rejects_files_that_are_not_zips():
    with pytest.raises(IngestError, match="not a valid zip"):
        ingest.from_zip(b"this is plainly not a zip file", "app11")


# --------------------------------------------------------------------------
# Normal use
# --------------------------------------------------------------------------


def test_extracts_a_normal_archive(sources):
    data = make_zip({
        "requirements.txt": "fastapi\n",
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "sub/helper.py": "VALUE = 1\n",
    })
    extracted = ingest.from_zip(data, "good1")

    assert (extracted / "main.py").read_text().startswith("from fastapi")
    assert (extracted / "sub" / "helper.py").exists()


def test_single_top_level_directory_is_unwrapped(sources):
    """A zip of a folder would otherwise look like a directory of one directory."""
    data = make_zip({"myapp/main.py": "x = 1\n", "myapp/requirements.txt": "fastapi\n"})
    extracted = ingest.from_zip(data, "good2")

    assert (extracted / "main.py").exists()
    assert extracted.name == "myapp"


def test_multiple_top_level_entries_are_not_unwrapped(sources):
    data = make_zip({"main.py": "x = 1\n", "lib/util.py": "y = 2\n"})
    extracted = ingest.from_zip(data, "good3")

    assert (extracted / "main.py").exists()
    assert extracted == ingest.source_dir_for("good3")


def test_macos_metadata_folder_is_ignored_when_unwrapping(sources):
    """Zips made on macOS carry a __MACOSX sibling that hides the real folder."""
    data = make_zip({
        "myapp/main.py": "x = 1\n",
        "__MACOSX/._main.py": "junk",
    })
    assert ingest.from_zip(data, "good4").name == "myapp"


def test_reingesting_replaces_the_previous_source(sources):
    ingest.from_zip(make_zip({"old.py": "x\n"}), "reuse")
    extracted = ingest.from_zip(make_zip({"new.py": "y\n"}), "reuse")

    assert (extracted / "new.py").exists()
    assert not (extracted / "old.py").exists()


def test_discard_source_removes_the_directory(sources):
    ingest.from_zip(make_zip({"main.py": "x\n"}), "gone")
    assert ingest.source_dir_for("gone").exists()

    ingest.discard_source("gone")
    assert not ingest.source_dir_for("gone").exists()


def test_discarding_a_missing_source_is_not_an_error():
    ingest.discard_source("never-existed")


# --------------------------------------------------------------------------
# Tarballs (the GitHub path)
# --------------------------------------------------------------------------


def test_tar_traversal_is_rejected(tmp_path):
    buffer = io.BytesIO(make_tar({"repo-abc/../../evil.txt": "pwned"}))
    target = tmp_path / "out"
    target.mkdir()

    with tarfile.open(fileobj=buffer, mode="r:gz") as archive:
        with pytest.raises(IngestError, match="escapes"):
            ingest._extract_tar(archive, target, strip_components=1)


def test_tar_symlinks_and_devices_are_skipped(tmp_path):
    link = tarfile.TarInfo("repo-abc/evil-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"

    buffer = io.BytesIO(make_tar({"repo-abc/main.py": "x = 1\n"}, extra_members=[link]))
    target = tmp_path / "out"
    target.mkdir()

    with tarfile.open(fileobj=buffer, mode="r:gz") as archive:
        ingest._extract_tar(archive, target, strip_components=1)

    assert (target / "main.py").exists()
    assert not (target / "evil-link").exists(follow_symlinks=False)


def test_strip_components_removes_githubs_wrapper_directory(tmp_path):
    """GitHub tarballs wrap everything in <owner>-<repo>-<sha>/."""
    buffer = io.BytesIO(
        make_tar({"owner-repo-9f8e7d/main.py": "x = 1\n",
                  "owner-repo-9f8e7d/lib/util.py": "y = 2\n"})
    )
    target = tmp_path / "out"
    target.mkdir()

    with tarfile.open(fileobj=buffer, mode="r:gz") as archive:
        ingest._extract_tar(archive, target, strip_components=1)

    assert (target / "main.py").exists()
    assert (target / "lib" / "util.py").exists()


# --------------------------------------------------------------------------
# Repo URL parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("https://github.com/owner/repo", Repo("owner", "repo")),
        ("http://github.com/owner/repo", Repo("owner", "repo")),
        ("https://www.github.com/owner/repo", Repo("owner", "repo")),
        ("github.com/owner/repo", Repo("owner", "repo")),
        ("https://github.com/owner/repo.git", Repo("owner", "repo")),
        ("https://github.com/owner/repo/", Repo("owner", "repo")),
        ("owner/repo", Repo("owner", "repo")),
        ("https://github.com/owner/repo/tree/dev", Repo("owner", "repo", "dev")),
        ("https://github.com/my-org/my.repo", Repo("my-org", "my.repo")),
    ],
)
def test_parses_the_url_shapes_people_paste(reference, expected):
    assert parse_repo(reference) == expected


def test_explicit_ref_overrides_one_in_the_url():
    assert parse_repo("https://github.com/o/r/tree/main", ref="v2").ref == "v2"


@pytest.mark.parametrize(
    "reference",
    ["", "not a url", "https://gitlab.com/owner/repo", "https://github.com/owner"],
)
def test_rejects_references_that_are_not_github_repos(reference):
    with pytest.raises(IngestError, match="GitHub repository"):
        parse_repo(reference)


def test_tarball_url_targets_the_rest_api():
    assert (
        parse_repo("owner/repo", "v1.2").tarball_url()
        == "https://api.github.com/repos/owner/repo/tarball/v1.2"
    )


def test_tarball_url_without_a_ref_uses_the_default_branch():
    assert parse_repo("owner/repo").tarball_url().endswith("/tarball")


# --------------------------------------------------------------------------
# GitHub errors surface usefully
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code, text="", chunks=()):
        self.status_code = status_code
        self.text = text
        self._chunks = chunks

    def iter_content(self, size):
        yield from self._chunks


def test_missing_repo_mentions_the_token(monkeypatch):
    monkeypatch.setattr(
        ingest.requests, "get", lambda *a, **kw: FakeResponse(404, "Not Found")
    )
    with pytest.raises(IngestError, match="HANGAR_GITHUB_TOKEN"):
        ingest.from_github("owner/private", "app-x")


def test_rate_limit_is_reported_as_such(monkeypatch):
    monkeypatch.setattr(
        ingest.requests,
        "get",
        lambda *a, **kw: FakeResponse(403, "API rate limit exceeded"),
    )
    with pytest.raises(IngestError, match="rate limit"):
        ingest.from_github("owner/repo", "app-y")


def test_network_failure_is_reported_clearly(monkeypatch):
    def boom(*a, **kw):
        raise ingest.requests.ConnectionError("no route to host")

    monkeypatch.setattr(ingest.requests, "get", boom)
    with pytest.raises(IngestError, match="could not reach GitHub"):
        ingest.from_github("owner/repo", "app-z")


def test_token_is_sent_when_configured(monkeypatch):
    seen = {}

    def capture(url, headers=None, **kw):
        seen.update(headers or {})
        return FakeResponse(404)

    monkeypatch.setenv("HANGAR_GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setattr(ingest.requests, "get", capture)

    with pytest.raises(IngestError):
        ingest.from_github("owner/repo", "app-t")

    assert seen["Authorization"] == "Bearer ghp_secret"


def test_no_authorization_header_without_a_token(monkeypatch):
    seen = {}

    def capture(url, headers=None, **kw):
        seen.update(headers or {})
        return FakeResponse(404)

    monkeypatch.delenv("HANGAR_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(ingest.requests, "get", capture)

    with pytest.raises(IngestError):
        ingest.from_github("owner/repo", "app-n")

    assert "Authorization" not in seen


def test_download_size_is_capped(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_ARCHIVE_BYTES", 100)
    monkeypatch.setattr(
        ingest.requests,
        "get",
        lambda *a, **kw: FakeResponse(200, chunks=[b"x" * 200]),
    )
    with pytest.raises(IngestError, match="exceeded"):
        ingest.from_github("owner/repo", "app-big")


def test_extracts_a_repo_tarball(monkeypatch, sources):
    tarball = make_tar({
        "owner-repo-abc123/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "owner-repo-abc123/requirements.txt": "fastapi\n",
    })
    monkeypatch.setattr(
        ingest.requests,
        "get",
        lambda *a, **kw: FakeResponse(200, chunks=[tarball]),
    )

    repo = ingest.from_github("https://github.com/owner/repo", "repo-app", ref="main")

    assert repo == Repo("owner", "repo", "main")
    extracted = ingest.source_dir_for("repo-app")
    assert (extracted / "main.py").exists()
    assert (extracted / "requirements.txt").read_text() == "fastapi\n"
