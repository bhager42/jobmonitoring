#!/usr/bin/env bash
# Run docs/truenas-app.yaml exactly as TrueNAS would (Docker Compose), with only the image, data
# folder and host port swapped, and check the plan's §11 acceptance list:
#   installs and turns healthy · web UI loads · setup token in the logs · database owned by 568
#   · data survives stop/start · data survives an image update (container re-created)
#
# Usage: scripts/truenas-acceptance.sh <image> [host-port]
set -euo pipefail

IMAGE=${1:?usage: $0 <image> [host-port]}
PORT=${2:-30880}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
DATA="$WORK/dataset"            # stands in for /mnt/<pool>/apps/jobmonitor
PROJECT=jm-acceptance-$$
COMPOSE="$WORK/compose.yaml"

as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }

cleanup() {
  docker compose -p "$PROJECT" -f "$COMPOSE" down -v >/dev/null 2>&1 || true
  as_root rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  docker compose -p "$PROJECT" -f "$COMPOSE" logs >&2 || true
  exit 1
}

mkdir -p "$DATA"
as_root chown 568:568 "$DATA"     # what the dataset's "apps" permission gives you on TrueNAS

sed -e "s#image: ghcr.io/bhager42/jobmonitoring:latest#image: $IMAGE#" \
    -e "s#/mnt/tank/apps/jobmonitor:/data#$DATA:/data#" \
    -e "s#\"30880:8080\"#\"$PORT:8080\"#" \
    "$ROOT/docs/truenas-app.yaml" > "$COMPOSE"
grep -q "image: $IMAGE" "$COMPOSE" || fail "could not substitute the image"
grep -q "$DATA:/data" "$COMPOSE" || fail "could not substitute the data folder"

compose() { docker compose -p "$PROJECT" -f "$COMPOSE" "$@"; }

wait_healthy() {
  local id status=""
  id=$(compose ps -q jobmonitor)
  for _ in $(seq 1 45); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$id")
    [ "$status" = healthy ] && return 0
    sleep 2
  done
  fail "not healthy after 90 s (status: $status)"
}

http_status() { python3 -c "
import sys, urllib.request, urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
try:
    print(urllib.request.build_opener(NoRedirect).open(sys.argv[1], timeout=5).status)
except urllib.error.HTTPError as e:
    print(e.code)
" "http://127.0.0.1:$PORT$1"; }

echo "== install"
compose up -d --quiet-pull
wait_healthy
[ "$(http_status /healthz)" = 200 ] || fail "/healthz did not answer on port $PORT"
[ "$(http_status /)" = 303 ] || fail "/ should redirect to /setup on a new install"
[ "$(http_status /setup)" = 200 ] || fail "the setup page did not load"
# Not `grep -q`: it exits at the first match, the log command then dies of SIGPIPE, and
# `set -o pipefail` turns that into a failure even though the line was found.
compose logs | grep "Setup token: " >/dev/null || fail "no setup token in the app logs"
[ -f "$DATA/jobmonitor.db" ] || fail "jobmonitor.db was not created in the dataset"
[ "$(stat -c %u:%g "$DATA/jobmonitor.db")" = 568:568 ] || fail "database not owned by 568:568"

# A marker row to prove the data survives.
as_root python3 -c "
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute(\"INSERT INTO settings (key, value) VALUES ('acceptance', 'kept')\")
conn.commit()
" "$DATA/jobmonitor.db"

check_kept() {
  as_root python3 -c "
import sqlite3, sys
row = sqlite3.connect(sys.argv[1]).execute(\"SELECT value FROM settings WHERE key='acceptance'\").fetchone()
sys.exit(0 if row == ('kept',) else 1)
" "$DATA/jobmonitor.db" || fail "data lost after $1"
  compose logs --since 2m | grep "Database is up to date" >/dev/null || fail "no clean start after $1"
}

echo "== stop / start"
compose stop
compose start
wait_healthy
check_kept "stop/start"

echo "== image update (container re-created)"
compose up -d --force-recreate
wait_healthy
check_kept "re-create"
[ "$(http_status /setup)" = 200 ] || fail "UI not answering after the update"

echo "PASS: $IMAGE behaves as the TrueNAS acceptance test expects"
