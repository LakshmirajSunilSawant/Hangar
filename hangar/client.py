"""Deploying to a Hangar from somewhere else.

PRD §5 step 1: "triggers deploy via a CLI". This talks to a remote control
plane over HTTP, so it does not import the server package's runtime pieces and
works from a laptop with nothing installed but Hangar itself.

A local directory is zipped and uploaded rather than requiring the source to
already exist on the server, which is the case the plain API can't cover.
"""

from __future__ import annotations

import io
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from .detect import IGNORED_DIRS

POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 30


class ClientError(Exception):
    """The remote control plane refused, or could not be reached."""


@dataclass
class Client:
    base_url: str
    token: str | None = None

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def _check(self, response: requests.Response) -> dict:
        if response.status_code >= 400:
            raise ClientError(_detail(response))
        return response.json() if response.content else {}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = requests.request(
                method,
                self._url(path),
                headers=self._headers,
                timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ClientError(f"could not reach {self.base_url}: {exc}") from exc
        return self._check(response)

    # -- operations ------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/healthz")

    def get(self, app_id: str) -> dict:
        return self._request("GET", f"/apps/{app_id}")

    def logs(self, app_id: str) -> dict:
        return self._request("GET", f"/apps/{app_id}/logs")

    def list(self) -> list:
        return self._request("GET", "/apps")  # type: ignore[return-value]

    def deploy_directory(self, source: Path, name: str, database: str | None) -> dict:
        archive = zip_directory(source)
        form = {"name": (None, name)}
        if database:
            form["database"] = (None, database)
        form["file"] = (f"{name}.zip", archive, "application/zip")
        # Uploads can be large and slow; don't hold the default timeout.
        return self._request("POST", "/apps/upload", files=form, timeout=300)

    def deploy_repo(
        self, repo: str, name: str, ref: str | None, database: str | None
    ) -> dict:
        payload = {"name": name, "repo_url": repo}
        if ref:
            payload["ref"] = ref
        if database:
            payload["database"] = database
        return self._request("POST", "/apps", json=payload)

    def redeploy(self, app_id: str) -> dict:
        return self._request("POST", f"/apps/{app_id}/redeploy")

    def find_by_name(self, name: str) -> dict | None:
        for app in self.list():
            if app["name"] == name:
                return app
        return None

    def wait(self, app_id: str, timeout: float = 600, on_status=None) -> dict:
        """Poll until the app stops being queued or building."""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            app = self.get(app_id)
            if app["status"] != last:
                last = app["status"]
                if on_status:
                    on_status(last)
            if app["status"] not in ("queued", "building"):
                return app
            time.sleep(POLL_INTERVAL)
        raise ClientError(f"still {last} after {timeout:.0f}s — check the build log")


def zip_directory(source: Path) -> bytes:
    """Zip a source tree, skipping the directories a build never wants."""
    if not source.is_dir():
        raise ClientError(f"not a directory: {source}")

    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            # node_modules and .venv would dwarf the actual source, and the
            # build reinstalls dependencies anyway.
            if IGNORED_DIRS & set(relative.parts):
                continue
            archive.write(path, relative.as_posix())
            count += 1

    if count == 0:
        raise ClientError(f"no files to deploy in {source}")
    return buffer.getvalue()


def _detail(response: requests.Response) -> str:
    try:
        body = response.json()
        detail = body.get("detail", body)
        if isinstance(detail, list):
            return "; ".join(str(d.get("msg", d)) for d in detail)
        return str(detail)
    except ValueError:
        return response.text.strip()[:300] or f"HTTP {response.status_code}"
