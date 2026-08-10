"""Getting source code into Hangar — from a zip upload or a GitHub repo.

Archives are the most hostile input the platform accepts. An archive entry
chooses its *own* path, so a naive extract lets an attacker write anywhere the
process can reach: `../../etc/cron.d/x`, or an absolute `/root/.ssh/authorized_keys`.
That is the Zip Slip class of bug, and it lands before any sandbox exists,
because extraction happens on the control plane host.

So extraction here is deliberately paranoid and does not delegate to
`ZipFile.extractall` or `TarFile.extractall`:

* every entry's resolved destination must stay inside the target directory
* symlinks, hardlinks, devices, and other non-regular members are dropped —
  a symlink is a path-traversal primitive that survives the path check
* total uncompressed size and entry count are capped, so a small archive
  cannot expand into a disk-filling one

GitHub is read through the REST API's tarball endpoint rather than by shelling
out to `git`, which keeps the dependency surface small and needs no git binary
on the host.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from . import config

log = logging.getLogger("hangar.ingest")

# An agent-generated internal tool is small. These ceilings are generous for
# that and still make an archive bomb harmless.
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024      # 100 MB compressed
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB expanded
MAX_ENTRIES = 20_000
DOWNLOAD_TIMEOUT = 60
CHUNK = 64 * 1024

GITHUB_API = "https://api.github.com"

# github.com/owner/repo, with optional .git, /tree/<ref>, or a bare owner/repo.
_REPO_PATTERNS = (
    re.compile(
        r"^(?:https?://)?(?:www\.)?github\.com/"
        r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
        r"(?:/tree/(?P<ref>[^/\s]+))?/?$"
    ),
    re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?$"),
)


class IngestError(Exception):
    """The source could not be obtained, or was refused."""


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    ref: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    def tarball_url(self) -> str:
        ref = self.ref or ""
        return f"{GITHUB_API}/repos/{self.owner}/{self.name}/tarball/{ref}".rstrip("/")


# --------------------------------------------------------------------------
# Where extracted sources live
# --------------------------------------------------------------------------


def sources_root() -> Path:
    root = config.settings().source_root
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def source_dir_for(app_id: str) -> Path:
    return sources_root() / app_id


def discard_source(app_id: str) -> None:
    """Remove an app's extracted source. Safe to call when there isn't one."""
    shutil.rmtree(source_dir_for(app_id), ignore_errors=True)


def _fresh_dir(app_id: str) -> Path:
    """An empty directory for this app, replacing anything already there."""
    target = source_dir_for(app_id)
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    return target


# --------------------------------------------------------------------------
# Zip
# --------------------------------------------------------------------------


def from_zip(data: bytes, app_id: str) -> Path:
    """Extract an uploaded zip into this app's source directory."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise IngestError(
            f"archive is {_mb(len(data))} MB, over the {_mb(MAX_ARCHIVE_BYTES)} MB limit"
        )
    if not data:
        raise IngestError("uploaded file is empty")

    target = _fresh_dir(app_id)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _extract_zip(archive, target)
    except zipfile.BadZipFile as exc:
        discard_source(app_id)
        raise IngestError(f"not a valid zip archive: {exc}") from exc
    except IngestError:
        discard_source(app_id)
        raise

    return _unwrap_single_directory(target)


def _extract_zip(archive: zipfile.ZipFile, target: Path) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise IngestError(f"archive has {len(infos)} entries, over {MAX_ENTRIES}")

    total = sum(info.file_size for info in infos)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise IngestError(
            f"archive expands to {_mb(total)} MB, over the "
            f"{_mb(MAX_UNCOMPRESSED_BYTES)} MB limit"
        )

    written = 0
    for info in infos:
        if _is_zip_symlink(info):
            log.info("skipping symlink in archive: %s", info.filename)
            continue

        destination = _safe_destination(target, info.filename)
        if destination is None:
            raise IngestError(
                f"archive entry escapes the extraction directory: {info.filename!r}"
            )

        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, destination.open("wb") as out:
            written += _copy_capped(source, out, MAX_UNCOMPRESSED_BYTES - written)


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix mode lives in the top 16 bits of external_attr; 0xA000 is S_IFLNK.
    return (info.external_attr >> 16) & 0xF000 == 0xA000


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


def parse_repo(reference: str, ref: str | None = None) -> Repo:
    """Accept the URL shapes people actually paste."""
    candidate = reference.strip()
    for pattern in _REPO_PATTERNS:
        match = pattern.match(candidate)
        if match:
            groups = match.groupdict()
            return Repo(
                owner=groups["owner"],
                name=groups["repo"],
                ref=ref or groups.get("ref"),
            )
    raise IngestError(
        f"could not read {reference!r} as a GitHub repository — expected "
        "https://github.com/owner/repo or owner/repo"
    )


def from_github(reference: str, app_id: str, ref: str | None = None) -> Repo:
    """Download a repo tarball through the REST API and extract it."""
    repo = parse_repo(reference, ref)
    settings = config.settings()

    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"

    try:
        response = requests.get(
            repo.tarball_url(),
            headers=headers,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise IngestError(f"could not reach GitHub: {exc}") from exc

    if response.status_code == 404:
        raise IngestError(
            f"repository {repo.slug!r} not found. If it is private, set "
            "HANGAR_GITHUB_TOKEN."
        )
    if response.status_code == 403 and "rate limit" in response.text.lower():
        raise IngestError(
            "GitHub rate limit reached — set HANGAR_GITHUB_TOKEN to raise it"
        )
    if response.status_code >= 400:
        raise IngestError(
            f"GitHub returned {response.status_code} for {repo.slug}: "
            f"{response.text.strip()[:200]}"
        )

    data = _read_capped(response, MAX_ARCHIVE_BYTES)
    target = _fresh_dir(app_id)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            # GitHub wraps everything in <owner>-<repo>-<sha>/.
            _extract_tar(archive, target, strip_components=1)
    except tarfile.TarError as exc:
        discard_source(app_id)
        raise IngestError(f"could not read the repository archive: {exc}") from exc
    except IngestError:
        discard_source(app_id)
        raise

    return repo


def _extract_tar(archive: tarfile.TarFile, target: Path, strip_components: int = 0):
    members = archive.getmembers()
    if len(members) > MAX_ENTRIES:
        raise IngestError(f"archive has {len(members)} entries, over {MAX_ENTRIES}")

    total = sum(m.size for m in members if m.isfile())
    if total > MAX_UNCOMPRESSED_BYTES:
        raise IngestError(
            f"archive expands to {_mb(total)} MB, over the "
            f"{_mb(MAX_UNCOMPRESSED_BYTES)} MB limit"
        )

    written = 0
    for member in members:
        # Anything that isn't a plain file or directory is dropped. Symlinks
        # and hardlinks in particular can point outside the tree even when
        # their own path looks innocent.
        if not (member.isfile() or member.isdir()):
            log.info("skipping non-regular archive member: %s", member.name)
            continue

        name = _strip(member.name, strip_components)
        if not name:
            continue

        destination = _safe_destination(target, name)
        if destination is None:
            raise IngestError(
                f"archive entry escapes the extraction directory: {member.name!r}"
            )

        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            continue

        extracted = archive.extractfile(member)
        if extracted is None:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as out:
            written += _copy_capped(extracted, out, MAX_UNCOMPRESSED_BYTES - written)


def _strip(name: str, components: int) -> str:
    """Drop the first ``components`` path segments — GitHub's tarball prefix."""
    parts = _path_parts(name)
    return "/".join(parts[components:]) if len(parts) > components else ""


def _path_parts(name: str) -> list[str]:
    return [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]


# --------------------------------------------------------------------------
# Shared safety helpers
# --------------------------------------------------------------------------


def _safe_destination(target: Path, name: str) -> Path | None:
    """Resolve ``name`` under ``target``, or None if it would escape.

    Checked after resolution rather than by looking for ".." in the string,
    since encodings and mixed separators make substring checks unreliable.
    """
    if name.startswith("/") or name.startswith("\\") or re.match(r"^[A-Za-z]:", name):
        return None

    candidate = (target / name).resolve()
    root = target.resolve()
    if candidate == root or root in candidate.parents:
        return candidate
    return None


def _copy_capped(source, out, remaining: int) -> int:
    """Copy with a hard ceiling, so a lying header can't fill the disk."""
    written = 0
    while True:
        chunk = source.read(CHUNK)
        if not chunk:
            return written
        written += len(chunk)
        if written > remaining:
            raise IngestError("archive is larger than its declared size")
        out.write(chunk)


def _read_capped(response: requests.Response, limit: int) -> bytes:
    buffer = io.BytesIO()
    size = 0
    for chunk in response.iter_content(CHUNK):
        size += len(chunk)
        if size > limit:
            raise IngestError(
                f"download exceeded the {_mb(limit)} MB limit"
            )
        buffer.write(chunk)
    if size == 0:
        raise IngestError("GitHub returned an empty archive")
    return buffer.getvalue()


def _unwrap_single_directory(target: Path) -> Path:
    """Zips usually contain one top-level folder; build from inside it.

    Otherwise a zip of `myapp/` would look like a directory containing only a
    directory, and detection would find no entrypoint.
    """
    entries = [p for p in target.iterdir() if not p.name.startswith("__MACOSX")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return target


def _mb(size: int) -> int:
    return size // (1024 * 1024)
