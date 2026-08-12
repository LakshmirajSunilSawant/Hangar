"""The volume half of backup/restore, which is the half that fails quietly.

`scripts/backup.sh` archives each app's data volume from inside a throwaway
container, and `scripts/restore.sh` prints the commands to unpack it into a
fresh one. The database dump is easy to eyeball — you can open the .sql and
see your rows. The volume round trip is not: a restored volume with the wrong
owner looks completely fine until an app that runs as uid 10001 tries to write
to it and gets EACCES, long after the person restoring has moved on.

So this round-trips a volume through the exact commands the scripts use and
asserts both halves: the bytes, and the ownership.

Marked slow. Needs a Docker daemon; skips without one.
"""

import subprocess
import uuid

import pytest

pytestmark = pytest.mark.slow

# The uid builder.py creates apps under. A restored volume owned by root is
# unwritable by the app it belongs to.
APP_UID = 10001
ALPINE = "alpine"


@pytest.fixture
def docker(docker_available):
    if not docker_available:
        pytest.skip("Docker daemon not reachable")
    import docker as docker_sdk

    client = docker_sdk.from_env()
    client.images.pull(ALPINE)
    return client


def run(client, *, volumes, command):
    """Run a throwaway container the way the scripts do, returning its output."""
    return client.containers.run(
        ALPINE, command=command, volumes=volumes, remove=True
    ).decode()


def test_a_volume_survives_the_round_trip_with_its_ownership(docker, tmp_path):
    source = f"hangar-test-src-{uuid.uuid4().hex[:8]}"
    target = f"hangar-test-dst-{uuid.uuid4().hex[:8]}"
    staging = tmp_path / "volumes"
    staging.mkdir()

    docker.volumes.create(source)
    try:
        # An app's data volume as it really looks: a file owned by the app user.
        run(
            docker,
            volumes={source: {"bind": "/data", "mode": "rw"}},
            command=[
                "sh", "-c",
                f"echo 'the only copy' > /data/app.db && "
                f"chown -R {APP_UID}:{APP_UID} /data",
            ],
        )

        # --- backup.sh's archive step ---
        run(
            docker,
            volumes={
                source: {"bind": "/source", "mode": "ro"},
                str(staging): {"bind": "/backup", "mode": "rw"},
            },
            command=["tar", "czf", f"/backup/{source}.tar.gz", "-C", "/source", "."],
        )
        archive = staging / f"{source}.tar.gz"
        assert archive.exists() and archive.stat().st_size > 0

        # --- restore.sh's unpack step, into a volume that did not exist ---
        docker.volumes.create(target)
        run(
            docker,
            volumes={
                target: {"bind": "/target", "mode": "rw"},
                str(staging): {"bind": "/backup", "mode": "ro"},
            },
            command=["tar", "xzf", f"/backup/{source}.tar.gz", "-C", "/target"],
        )

        listing = run(
            docker,
            volumes={target: {"bind": "/data", "mode": "ro"}},
            command=["sh", "-c", "cat /data/app.db && stat -c '%u:%g' /data/app.db"],
        )
    finally:
        for name in (source, target):
            try:
                docker.volumes.get(name).remove(force=True)
            except Exception:  # noqa: BLE001
                pass

    assert "the only copy" in listing, "the data did not survive the round trip"
    assert f"{APP_UID}:{APP_UID}" in listing, (
        "the restored file is owned by the wrong user — the app runs as "
        f"uid {APP_UID} and would not be able to write to its own database"
    )


def test_backup_selects_hangar_volumes_and_nothing_else():
    """The filter decides what gets backed up, so a typo loses data silently."""
    import re
    from pathlib import Path

    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "backup.sh"
    ).read_text(encoding="utf-8")
    pattern = re.search(r"grep -E '([^']+)'", script).group(1)

    matches = lambda name: re.search(pattern, name) is not None  # noqa: E731

    # Per-app data and the extracted sources of zip-uploaded apps, which have
    # no upstream to re-fetch from.
    assert matches("hangar-data-abc123")
    assert matches("hangar-demo_hangar-sources")
    # Not the control plane's own Postgres — that is dumped with pg_dump — and
    # not Caddy's certificate store, which reissues on demand.
    assert not matches("hangar-demo_postgres-data")
    assert not matches("hangar-demo_caddy-data")


def test_restore_is_not_automatic():
    """Overwriting every app's data must be a decision, not a side effect.

    The destructive commands appear in restore.sh either way — the whole point
    is that it prints them. So this splits the script at the here-document and
    checks they live only in the part that is printed, never in the part that
    runs.
    """
    from pathlib import Path

    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "restore.sh"
    ).read_text(encoding="utf-8")

    executed, _, printed = script.partition("cat <<EOF")
    assert printed, "restore.sh no longer ends with printed instructions"

    assert "restic restore" in executed, "restore.sh does not restore anything"
    for destructive in ("psql", "docker volume create", "docker compose up"):
        assert destructive not in executed, (
            f"restore.sh runs {destructive!r} itself instead of printing it"
        )
        assert destructive in printed, f"the instructions no longer mention {destructive!r}"


def test_the_secret_key_is_in_the_backup():
    """A dump without it cannot decrypt any sealed secret it contains."""
    from pathlib import Path

    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "backup.sh"
    ).read_text(encoding="utf-8")
    assert "cp .env" in script
    assert "HANGAR_SECRET_KEY" in script
