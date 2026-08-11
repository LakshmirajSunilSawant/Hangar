#!/usr/bin/env bash
# Back up everything Hangar cannot regenerate.
#
#   RESTIC_REPOSITORY=/mnt/backup/hangar RESTIC_PASSWORD=... ./scripts/backup.sh
#
# What is deliberately NOT backed up: built images and the Caddy certificate
# store. Images rebuild from source, and Let's Encrypt reissues certificates on
# demand — backing either up costs a lot of space to save a little time.
#
# What IS backed up matters more than it looks:
#   - the control-plane database (which apps exist, who may reach them)
#   - .env, which holds HANGAR_SECRET_KEY. Per-app database passwords are
#     sealed with that key, so a database backup without it is undecryptable.
#   - every per-app data volume — the apps' actual contents
#   - extracted sources for zip-uploaded apps, which have no upstream to
#     re-fetch from, unlike repo-backed ones

set -euo pipefail

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY}"
: "${RESTIC_PASSWORD:?set RESTIC_PASSWORD}"

PROJECT_DIR="${HANGAR_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STAGING="$(mktemp -d)"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${KEEP_MONTHLY:-6}"

# Secrets briefly land in the staging directory, so make it unreadable to
# anyone else before anything is written into it.
chmod 700 "$STAGING"
trap 'rm -rf "$STAGING"' EXIT

log() { printf '[hangar-backup] %s\n' "$*"; }

cd "$PROJECT_DIR"

# --- control plane database ------------------------------------------------
log "dumping the control-plane database"
if docker compose ps --status running --services 2>/dev/null | grep -qx postgres; then
    docker compose exec -T postgres pg_dump -U hangar hangar > "$STAGING/hangar.sql"
else
    log "WARNING: postgres container is not running; skipping the database dump"
fi

# --- configuration ---------------------------------------------------------
if [ -f .env ]; then
    cp .env "$STAGING/env"
    log "captured .env (contains HANGAR_SECRET_KEY)"
else
    log "WARNING: no .env found — a database backup without HANGAR_SECRET_KEY"
    log "         cannot decrypt per-app database passwords"
fi

# --- per-app data volumes --------------------------------------------------
# Volumes can't be read from the host directly, so each is tarred from inside a
# throwaway container that mounts it read-only.
mkdir -p "$STAGING/volumes"
for volume in $(docker volume ls --format '{{.Name}}' | grep -E '^(hangar-data-|.*_hangar-sources$)' || true); do
    log "archiving volume $volume"
    docker run --rm \
        -v "$volume":/source:ro \
        -v "$STAGING/volumes":/backup \
        alpine tar czf "/backup/${volume}.tar.gz" -C /source . 2>/dev/null \
        || log "WARNING: could not archive $volume"
done

# --- send it ---------------------------------------------------------------
log "backing up to $RESTIC_REPOSITORY"
restic backup "$STAGING" --tag hangar --host "$(hostname)"

log "pruning old snapshots"
restic forget --tag hangar \
    --keep-daily "$KEEP_DAILY" \
    --keep-weekly "$KEEP_WEEKLY" \
    --keep-monthly "$KEEP_MONTHLY" \
    --prune

# Cheap integrity check. A backup that has never been verified is a guess, and
# this is the smallest honest version of verifying it.
log "checking repository integrity"
restic check --read-data-subset=5%

log "done. Snapshots:"
restic snapshots --tag hangar --latest 3
