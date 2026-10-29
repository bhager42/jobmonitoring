#!/usr/bin/env bash
# Smoke-test a built image with the same hardening as the TrueNAS app:
# runs as UID 568 with a read-only root filesystem, becomes healthy, and keeps /data across
# both a restart and a container re-create (which is what a TrueNAS image update does).
#
# Usage: scripts/container-smoke-test.sh <image>
set -euo pipefail

IMAGE=${1:?usage: $0 <image>}
NAME=jm-smoke-$$
VOLUME=jm-smoke-data-$$

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  docker logs "$NAME" >&2 || true
  exit 1
}

start() {
  docker run -d --name "$NAME" --init --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges:true \
    -v "$VOLUME:/data" "$IMAGE" >/dev/null
}

wait_healthy() {
  local status=""
  for _ in $(seq 1 30); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$NAME")
    [ "$status" = healthy ] && return 0
    sleep 2
  done
  fail "container not healthy (status: $status)"
}

in_container() {
  docker exec "$NAME" python -c "$1"
}

echo "== first start"
start
wait_healthy
[ "$(docker exec "$NAME" id -u)" = 568 ] || fail "not running as UID 568"
docker exec "$NAME" touch /app/should-fail 2>/dev/null && fail "root filesystem is writable"
in_container "
import json, urllib.request
body = json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4))
assert body['status'] == 'ok', body
" || fail "/healthz did not return ok"
docker logs "$NAME" 2>&1 | grep -q "Migrated database from empty" || fail "no initial migration"
in_container "
import sqlite3
conn = sqlite3.connect('/data/jobmonitor.db')
conn.execute(\"INSERT INTO settings (key, value) VALUES ('smoke_test', 'persisted')\")
conn.commit()
" || fail "could not write to /data/jobmonitor.db"

check_persisted() {
  in_container "
import sqlite3
row = sqlite3.connect('/data/jobmonitor.db').execute(
    \"SELECT value FROM settings WHERE key = 'smoke_test'\").fetchone()
assert row == ('persisted',), row
" || fail "data did not survive $1"
  docker logs "$NAME" 2>&1 | tail -n 20 | grep -q "Database is up to date" \
    || fail "database was not reported up to date after $1"
  if docker exec "$NAME" sh -c 'ls /data/backups 2>/dev/null | grep -q .'; then
    fail "unexpected pre-migration backup after $1"
  fi
}

echo "== restart"
docker restart -t 10 "$NAME" >/dev/null
wait_healthy
check_persisted "restart"

echo "== re-create container with the same volume"
docker rm -f "$NAME" >/dev/null
start
wait_healthy
check_persisted "re-create"

echo "PASS: $IMAGE"
