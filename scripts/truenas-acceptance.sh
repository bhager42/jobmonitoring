#!/usr/bin/env bash
# Run docs/truenas-app.yaml exactly as TrueNAS would (Docker Compose), swapping only the image and
# host port, and check the plan's §11 acceptance list. Three storage set-ups:
#   1. the default named volume: installs and turns healthy, web UI loads, setup token in the logs,
#      database owned by 568, data survives stop/start and an image update (container re-created)
#   2. the commented-out dataset folder, with the apps user's permission: works the same way
#   3. a dataset folder the apps user can't write to: the app must stay up and explain the problem
#      (503 + message), not exit into a restart loop that TrueNAS can't stop
#
# Usage: scripts/truenas-acceptance.sh <image> [host-port]
set -euo pipefail

IMAGE=${1:?usage: $0 <image> [host-port]}
PORT=${2:-30880}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
PROJECT=""
COMPOSE=""

as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }
compose() { docker compose -p "$PROJECT" -f "$COMPOSE" "$@"; }

cleanup() {
  [ -n "$PROJECT" ] && compose down -v >/dev/null 2>&1 || true
  as_root rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  compose logs >&2 || true
  exit 1
}

# Write the compose file for a scenario: same YAML, with the image, port and (optionally) the
# /data mount swapped.
prepare() {
  PROJECT="jm-accept-$1-$$"
  COMPOSE="$WORK/$1.yaml"
  local mount=${2:-}
  sed -e "s#image: ghcr.io/bhager42/jobmonitoring:latest#image: $IMAGE#" \
      -e "s#\"30880:8080\"#\"$PORT:8080\"#" \
      "$ROOT/docs/truenas-app.yaml" > "$COMPOSE"
  grep -q "image: $IMAGE" "$COMPOSE" || fail "could not substitute the image"
  if [ -n "$mount" ]; then
    sed -i "s#^      - jobmonitor:/data\$#      - $mount:/data#" "$COMPOSE"
    grep -q -- "- $mount:/data" "$COMPOSE" || fail "could not substitute the data mount"
  fi
}

container() { compose ps -aq jobmonitor; }

wait_healthy() {
  local status=""
  for _ in $(seq 1 45); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$(container)")
    [ "$status" = healthy ] && return 0
    sleep 2
  done
  fail "not healthy after 90 s (status: $status)"
}

http() { python3 -c "
import sys, urllib.request, urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
try:
    r = urllib.request.build_opener(NoRedirect).open(sys.argv[1], timeout=5)
    print(r.status); print(r.read().decode(errors='replace'))
except urllib.error.HTTPError as e:
    print(e.code); print(e.read().decode(errors='replace'))
except OSError:
    print(0)
" "http://127.0.0.1:$PORT$1"; }
status_of() { http "$1" | head -n 1; }

in_app() { compose exec -T jobmonitor "$@"; }

# --- 1. default: named volume --------------------------------------------------------------
echo "== 1. named volume (default): install"
prepare volume
compose up -d --quiet-pull
wait_healthy
[ "$(status_of /healthz)" = 200 ] || fail "/healthz did not answer on port $PORT"
[ "$(status_of /)" = 303 ] || fail "/ should redirect to /setup on a new install"
[ "$(status_of /setup)" = 200 ] || fail "the setup page did not load"
# Not `grep -q`: it exits at the first match, the log command then dies of SIGPIPE, and
# `set -o pipefail` turns that into a failure even though the line was found.
compose logs | grep "Setup token: " >/dev/null || fail "no setup token in the app logs"
[ "$(in_app stat -c %u:%g /data/jobmonitor.db)" = 568:568 ] || fail "database not owned by 568:568"

in_app python -c "
import sqlite3
conn = sqlite3.connect('/data/jobmonitor.db')
conn.execute(\"INSERT INTO settings (key, value) VALUES ('acceptance', 'kept')\")
conn.commit()
"

check_kept() {
  in_app python -c "
import sqlite3, sys
row = sqlite3.connect('/data/jobmonitor.db').execute(
    \"SELECT value FROM settings WHERE key='acceptance'\").fetchone()
sys.exit(0 if row == ('kept',) else 1)
" || fail "data lost after $1"
  compose logs --since 2m | grep "Database is up to date" >/dev/null || fail "no clean start after $1"
}

echo "== 1. named volume: stop / start"
compose stop
compose start
wait_healthy
check_kept "stop/start"

echo "== 1. named volume: image update (container re-created)"
compose up -d --force-recreate
wait_healthy
check_kept "re-create"
compose down -v >/dev/null

# --- 2. dataset folder with the apps user's permission ---------------------------------------
echo "== 2. dataset folder (apps user has Modify)"
DATASET="$WORK/dataset"
mkdir -p "$DATASET"
as_root chown 568:568 "$DATASET"
prepare dataset "$DATASET"
compose up -d --quiet-pull
wait_healthy
[ "$(status_of /setup)" = 200 ] || fail "the setup page did not load (dataset folder)"
[ "$(stat -c %u:%g "$DATASET/jobmonitor.db")" = 568:568 ] || fail "database in the dataset not owned by 568:568"
compose down -v >/dev/null

# --- 3. dataset folder the apps user can't write to ------------------------------------------
echo "== 3. dataset folder without permission: stays up and explains"
LOCKED="$WORK/locked"
mkdir -p "$LOCKED"
as_root chown 0:0 "$LOCKED"
as_root chmod 755 "$LOCKED"
prepare locked "$LOCKED"
compose up -d --quiet-pull
for _ in $(seq 1 30); do
  [ "$(status_of /healthz)" = 503 ] && break
  sleep 1
done
[ "$(status_of /healthz)" = 503 ] || fail "/healthz should answer 503 when /data isn't writable"
http /setup | grep "is not writable" >/dev/null || fail "the page should explain the problem"
sleep 10
[ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$(container)")" = "true 0" ] \
  || fail "the container should keep running without restarting"
compose logs | grep "ERROR: data directory /data is not writable" >/dev/null \
  || fail "the problem should be in the log"
compose down -v >/dev/null

echo "PASS: $IMAGE behaves as the TrueNAS acceptance test expects"
