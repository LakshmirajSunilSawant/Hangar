"""Image building.

Takes a source directory plus a `Detection` and produces a container image.

The source tree is never built in place. It's copied into a staging directory
first, so a generated Dockerfile can be written next to it without mutating the
user's folder, and so the build context excludes vendored junk (node_modules,
.venv) that would otherwise be shipped into the image.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import docker
from docker.errors import BuildError, DockerException

from .backends.base import BuildFailed, BuildResult, LogSink
from .database import DATA_MOUNT
from .detect import IGNORED_DIRS, Detection, requirement_name

# Framework packages the start command needs, which an app's own dependency
# file often omits (a Flask app rarely declares gunicorn, for example).
RUNTIME_PACKAGES = {
    "fastapi": ["fastapi", "uvicorn[standard]"],
    "flask": ["flask", "gunicorn"],
    "streamlit": ["streamlit"],
}


def build(
    source_dir: str | Path,
    detection: Detection,
    tag: str,
    *,
    on_log: LogSink | None = None,
    client: docker.DockerClient | None = None,
) -> BuildResult:
    """Build an image for the app in ``source_dir`` and tag it ``tag``."""
    source = Path(source_dir)
    client = client or _client()
    lines: list[str] = []

    def emit(line: str) -> None:
        lines.append(line)
        if on_log:
            on_log(line)

    staging = _stage(source)
    try:
        existing = (staging / "Dockerfile").is_file()
        if existing:
            # The app author was explicit about how to build; respect that.
            dockerfile = (staging / "Dockerfile").read_text(
                encoding="utf-8", errors="replace"
            )
            emit("using the Dockerfile shipped with the app")
        else:
            dockerfile = render_dockerfile(detection)
            (staging / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            emit(f"generated Dockerfile for {detection.framework} ({detection.runtime})")
            for note in detection.evidence:
                emit(f"  detection: {note}")

        emit(f"building image {tag}")
        try:
            for line in _stream_build(client, staging, tag):
                emit(line)
        except (BuildError, DockerException) as exc:
            emit(f"build failed: {exc}")
            raise BuildFailed(str(exc)) from exc

        emit(f"built {tag}")
        return BuildResult(
            image_tag=tag,
            dockerfile=dockerfile,
            log="\n".join(lines),
            used_existing_dockerfile=existing,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def render_dockerfile(detection: Detection) -> str:
    if detection.runtime == "python":
        return _python_dockerfile(detection)
    return _node_dockerfile(detection)


# --------------------------------------------------------------------------
# Dockerfile templates
# --------------------------------------------------------------------------


def _python_dockerfile(d: Detection) -> str:
    lines = [
        f"FROM {d.base_image}",
        "",
        "ENV PYTHONDONTWRITEBYTECODE=1 \\",
        "    PYTHONUNBUFFERED=1 \\",
        "    PIP_NO_CACHE_DIR=1 \\",
        "    PIP_DISABLE_PIP_VERSION_CHECK=1",
        "",
        "WORKDIR /app",
        "",
    ]

    # Dependencies are copied and installed before the rest of the source so
    # that editing app code doesn't invalidate the (slow) install layer.
    if d.dependency_file == "requirements.txt":
        lines += [
            "COPY requirements.txt ./",
            "RUN pip install --no-cache-dir -r requirements.txt",
        ]
    elif d.dependency_file == "pyproject.toml":
        lines += [
            "COPY pyproject.toml ./",
            # Installing the project itself needs the full source, so this
            # layer only warms the wheel cache for declared dependencies.
            "COPY . .",
            "RUN pip install --no-cache-dir .",
        ]

    # Only install what the app didn't pin itself — reinstalling a declared
    # package is a wasted layer and risks overriding the app's chosen version.
    missing = [
        p for p in RUNTIME_PACKAGES.get(d.framework, [])
        if requirement_name(p) not in d.declared_dependencies
    ]
    if missing:
        quoted = " ".join(f'"{p}"' for p in missing)
        lines.append(f"RUN pip install --no-cache-dir {quoted}")

    if d.dependency_file != "pyproject.toml":
        lines.append("COPY . .")

    lines += _common_tail(d, home="/app")
    return "\n".join(lines) + "\n"


def _node_dockerfile(d: Detection) -> str:
    lines = [
        f"FROM {d.base_image}",
        "",
        "ENV NODE_ENV=production \\",
        "    NPM_CONFIG_UPDATE_NOTIFIER=false \\",
        "    NPM_CONFIG_FUND=false",
        "",
        "WORKDIR /app",
        "",
        # The glob keeps this working whether or not a lockfile is present.
        "COPY package.json package-lock.json* ./",
        # `npm ci` when there's a lockfile, `npm install` otherwise.
        'RUN if [ -f package-lock.json ]; then npm ci --omit=dev; '
        'else npm install --omit=dev; fi',
        "",
        "COPY . .",
    ]

    if d.framework == "next":
        # Next has to be compiled before `next start` will serve anything.
        lines += ["", "RUN npm run build"]

    lines += _common_tail(d, home="/app")
    return "\n".join(lines) + "\n"


def _common_tail(d: Detection, home: str) -> list[str]:
    command = ", ".join(f'"{part}"' for part in d.start_command)
    return [
        "",
        # Untrusted code must not run as root inside the container. This is a
        # defence-in-depth measure, not the isolation boundary itself — see the
        # sandbox note in README.
        #
        # /data is created here, owned by the app user, even when no database
        # is attached. Docker seeds a fresh named volume from the image's
        # directory at the mount point, so this is what makes the volume
        # writable by a non-root process; without it the app gets a root-owned
        # mount it cannot write to.
        "RUN useradd --create-home --uid 10001 hangar \\",
        f"    && mkdir -p {DATA_MOUNT} \\",
        f"    && chown -R hangar:hangar {home} {DATA_MOUNT}",
        "USER hangar",
        "",
        f"ENV PORT={d.port}",
        f"EXPOSE {d.port}",
        "",
        f"CMD [{command}]",
    ]


# --------------------------------------------------------------------------
# Docker plumbing
# --------------------------------------------------------------------------


def _client() -> docker.DockerClient:
    try:
        return docker.from_env()
    except DockerException as exc:
        raise BuildFailed(
            "could not reach the Docker daemon — is Docker running?"
        ) from exc


def _stream_build(
    client: docker.DockerClient, context: Path, tag: str
) -> Iterator[str]:
    """Run the build, yielding log lines as they arrive."""
    stream = client.api.build(
        path=str(context),
        tag=tag,
        rm=True,
        forcerm=True,
        pull=False,
        decode=True,
    )
    for chunk in stream:
        if "stream" in chunk:
            text = chunk["stream"].strip()
            if text:
                yield text
        elif "error" in chunk:
            raise BuildFailed(chunk["error"].strip())
        elif "status" in chunk:
            yield chunk["status"].strip()


def _stage(source: Path) -> Path:
    """Copy the source into a scratch directory that becomes the build context."""
    root = Path(tempfile.gettempdir()) / "hangar-builds"
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="build-", dir=root))
    shutil.copytree(
        source,
        staging,
        ignore=shutil.ignore_patterns(*IGNORED_DIRS),
        symlinks=False,
        dirs_exist_ok=True,
    )
    return staging
