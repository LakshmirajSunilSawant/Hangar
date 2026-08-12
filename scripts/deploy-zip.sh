#!/usr/bin/env bash
# Zip a source directory and deploy it, the way the dashboard's upload does.
#
#   ./scripts/deploy-zip.sh examples/fastapi-hello hello [sqlite]
#
# Exists mostly so the demo has a terminal deploy that is one level of quoting
# deep. Nesting PowerShell inside wsl inside bash inside python is a good way
# to lose ten minutes of a recording to a parse error.

set -euo pipefail

SOURCE="${1:?usage: deploy-zip.sh <source-dir> <app-name> [none|sqlite|postgres]}"
NAME="${2:?usage: deploy-zip.sh <source-dir> <app-name> [none|sqlite|postgres]}"
DATABASE="${3:-none}"
BASE="${HANGAR_URL:-http://127.0.0.1:8080}"
TOKEN="${HANGAR_API_TOKEN:-}"

[ -d "$SOURCE" ] || { echo "no such directory: $SOURCE" >&2; exit 1; }

if [ -z "$TOKEN" ] && [ -f .env ]; then
    TOKEN="$(grep -E '^HANGAR_API_TOKEN=' .env | cut -d= -f2- || true)"
fi
[ -n "$TOKEN" ] || { echo "set HANGAR_API_TOKEN, or run from a directory with .env" >&2; exit 1; }

ARCHIVE="$(mktemp -d)/${NAME}.zip"
trap 'rm -rf "$(dirname "$ARCHIVE")"' EXIT

# Top-level files only, matching what someone would zip by hand from a small
# generated app. Subdirectories would need the recursive walk ingest.py already
# handles on the receiving side.
python3 - "$SOURCE" "$ARCHIVE" <<'PY'
import pathlib, sys, zipfile

source, archive = pathlib.Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(archive, "w") as bundle:
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            bundle.write(path, path.relative_to(source))
PY

echo "uploading $(basename "$ARCHIVE") ($(stat -c%s "$ARCHIVE") bytes) as '$NAME'"

APP_ID="$(curl -fsS -H "Authorization: Bearer $TOKEN" \
    -X POST "$BASE/apps/upload" \
    -F "name=$NAME" -F "database=$DATABASE" -F "file=@$ARCHIVE" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')"

echo "app $APP_ID — building"

for _ in $(seq 1 100); do
    read -r status url runtime framework error < <(
        curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE/apps/$APP_ID" \
        | python3 -c '
import sys, json
a = json.load(sys.stdin)
print(a["status"], a["url"] or "-", a["runtime"] or "-",
      a["framework"] or "-", (a["error"] or "-").replace(" ", "_"))'
    )
    [ "$status" = "queued" ] || [ "$status" = "building" ] || break
    sleep 2
done

echo "  status  : $status"
echo "  detected: $runtime/$framework"
echo "  url     : $url"
[ "$error" != "-" ] && echo "  error   : ${error//_/ }"
[ "$status" = "running" ]
