#!/usr/bin/env bash
# Restore from a restic snapshot taken by backup.sh.
#
#   RESTIC_REPOSITORY=... RESTIC_PASSWORD=... ./scripts/restore.sh latest
#
# Deliberately does NOT run automatically end to end. Restoring overwrites the
# control-plane database and every app's data, so the last step is left for a
# human who has read what is about to happen.

set -euo pipefail

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY}"
: "${RESTIC_PASSWORD:?set RESTIC_PASSWORD}"

SNAPSHOT="${1:-latest}"
PROJECT_DIR="${HANGAR_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TARGET="$(mktemp -d)"
chmod 700 "$TARGET"

log() { printf '[hangar-restore] %s\n' "$*"; }

log "restoring snapshot $SNAPSHOT to $TARGET"
restic restore "$SNAPSHOT" --target "$TARGET"

PAYLOAD="$(find "$TARGET" -maxdepth 4 -name hangar.sql -printf '%h\n' -quit || true)"
if [ -z "$PAYLOAD" ]; then
    PAYLOAD="$(find "$TARGET" -maxdepth 4 -type d -name volumes -printf '%h\n' -quit)"
fi
[ -n "$PAYLOAD" ] || { log "could not find the backup payload in $TARGET"; exit 1; }

log "payload at: $PAYLOAD"
echo
echo "Contents:"
ls -la "$PAYLOAD"
[ -d "$PAYLOAD/volumes" ] && ls -la "$PAYLOAD/volumes"

cat <<EOF

Nothing has been overwritten yet. To finish, from $PROJECT_DIR:

  # 1. Configuration — HANGAR_SECRET_KEY must match the database, or per-app
  #    database passwords cannot be decrypted.
  cp "$PAYLOAD/env" .env

  # 2. Control-plane database, into a running but empty Postgres.
  docker compose up -d postgres
  docker compose exec -T postgres psql -U hangar -d hangar < "$PAYLOAD/hangar.sql"

  # 3. Each app's data volume.
  for archive in "$PAYLOAD"/volumes/*.tar.gz; do
    name=\$(basename "\$archive" .tar.gz)
    docker volume create "\$name"
    docker run --rm -v "\$name":/target -v "$PAYLOAD/volumes":/backup:ro \\
      alpine tar xzf "/backup/\$(basename "\$archive")" -C /target
  done

  # 4. Bring everything up, then redeploy each app to rebuild its image —
  #    images are not backed up, since they rebuild from source.
  docker compose up -d

EOF
