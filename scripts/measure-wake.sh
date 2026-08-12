#!/usr/bin/env bash
# Measure wake-from-idle: the PRD §9 number, end to end, through the proxy.
#
#   HANGAR_URL=http://127.0.0.1:8080 \
#   LOGIN_URL=https://hangar.example.com \
#   EMAIL=you@demo.local PASSWORD=... \
#   ./scripts/measure-wake.sh notes
#
# What it does, per round: put the app to sleep, confirm its container really
# stopped, then fetch its public URL with a stopwatch running. The clock covers
# everything a person waits for — proxy, forward-auth, container start, the
# app's own startup, and the response — because that is what "cold start" means
# to whoever clicked the link.
#
# This deliberately does NOT use POST /apps/{id}/wake. That returns as soon as
# the container is started, which is a smaller and much flattering number: it
# excludes the app's own boot, which for anything with an interpreter and a
# framework is most of the wait.
#
# Run it against the real host with the real sandbox runtime. A wake measured
# with runsc off is not the number the PRD is asking for.

set -euo pipefail

APP_NAME="${1:?usage: measure-wake.sh <app-name> [rounds]}"
ROUNDS="${2:-5}"
BASE="${HANGAR_URL:-http://127.0.0.1:8080}"
# Where to sign in. Must be the dashboard's public hostname when the session
# cookie is scoped to a domain — see the sign-in step below.
LOGIN_URL="${LOGIN_URL:-$BASE}"
TOKEN="${HANGAR_API_TOKEN:-}"
EMAIL="${EMAIL:-}"
PASSWORD="${PASSWORD:-}"
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

log() { printf '[wake] %s\n' "$*"; }
die() { printf '[wake] ERROR: %s\n' "$*" >&2; exit 1; }

# .env is where the token lives on a compose deployment.
if [ -z "$TOKEN" ] && [ -f .env ]; then
    TOKEN="$(grep -E '^HANGAR_API_TOKEN=' .env | cut -d= -f2- || true)"
fi
[ -n "$TOKEN" ] || die "set HANGAR_API_TOKEN, or run from a directory with .env"

api() { curl -fsS -H "Authorization: Bearer $TOKEN" "$@"; }

json() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }

# --- find the app ----------------------------------------------------------

APP_JSON="$(api "$BASE/apps" | python3 -c "
import sys, json
name = '$APP_NAME'
for app in json.load(sys.stdin):
    if app['name'] == name:
        print(json.dumps(app)); break
else:
    raise SystemExit(f'no app named {name!r}')
")" || die "could not find an app named '$APP_NAME'"

APP_ID="$(printf '%s' "$APP_JSON" | json "['id']")"
APP_URL="$(printf '%s' "$APP_JSON" | json "['url'] or ''")"
[ -n "$APP_URL" ] || die "$APP_NAME has no public URL — is a router configured?"

IDLE="$(api "$BASE/healthz" | json "['idle_timeout']")"
[ "$IDLE" != "0" ] || die "scale-to-zero is off (HANGAR_IDLE_TIMEOUT=0); nothing to measure"

RUNTIME="$(api "$BASE/healthz" | json "['sandbox_runtime']")"
log "app     : $APP_NAME ($APP_ID)"
log "url     : $APP_URL"
log "sandbox : $RUNTIME"
if [ "$RUNTIME" = "docker-default" ]; then
    log "WARNING: no sandbox runtime — this number is not the PRD's, which"
    log "         assumes gVisor. Set HANGAR_RUNTIME=runsc."
fi

# --- sign in, if apps are gated --------------------------------------------

if [ -n "$EMAIL" ]; then
    # Sign in at the *public* dashboard hostname, not at a loopback address.
    # With HANGAR_COOKIE_DOMAIN set the session cookie is scoped to the app
    # domain, and curl silently discards a cookie whose domain doesn't match
    # the host it was fetched from — leaving every measurement a 401.
    curl -fsS -c "$COOKIE_JAR" -X POST "$LOGIN_URL/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" >/dev/null \
        || die "sign-in failed for $EMAIL at $LOGIN_URL"

    if [ ! -s "$COOKIE_JAR" ]; then
        die "signed in, but no cookie was stored — HANGAR_COOKIE_DOMAIN is
     probably scoped to the app domain, so set LOGIN_URL to the dashboard's
     public hostname (e.g. https://hangar.$(api "$BASE/healthz" | j "['app_domain']"))"
    fi
    log "signed in as $EMAIL at $LOGIN_URL"
else
    log "no EMAIL set — assuming apps are not behind platform auth"
fi

# --- rounds ----------------------------------------------------------------

printf '\n%-7s %-10s %s\n' "round" "wake (s)" "status"
TIMES=()

for round in $(seq 1 "$ROUNDS"); do
    api -X POST "$BASE/apps/$APP_ID/sleep" >/dev/null \
        || die "could not put $APP_NAME to sleep"

    # Trust but verify: a status field says nothing about whether the process
    # is actually gone.
    state="$(docker inspect "hangar-$APP_ID" --format '{{.State.Status}}' 2>/dev/null || echo '?')"
    [ "$state" = "exited" ] || log "WARNING: container state is '$state', expected 'exited'"

    read -r elapsed status < <(
        curl -s -o /dev/null -b "$COOKIE_JAR" \
            -w '%{time_total} %{http_code}\n' \
            --max-time 120 "$APP_URL"
    )
    printf '%-7s %-10s %s\n' "$round" "$elapsed" "$status"
    [ "$status" = "200" ] && TIMES+=("$elapsed")
done

# --- summary ---------------------------------------------------------------

if [ "${#TIMES[@]}" -eq 0 ]; then
    die "no successful wake — the app never answered"
fi

printf '\n'
printf '%s\n' "${TIMES[@]}" | python3 -c "
import sys, statistics
times = [float(line) for line in sys.stdin]
print(f'rounds  : {len(times)}')
print(f'median  : {statistics.median(times):.2f}s')
print(f'fastest : {min(times):.2f}s')
print(f'slowest : {max(times):.2f}s')
print()
target = 3.0
verdict = 'meets' if statistics.median(times) < target else 'MISSES'
print(f'{verdict} the PRD §9 target of under {target:.0f}s (median).')
"
