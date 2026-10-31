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
in_container "
import urllib.request
for path in ('/setup', '/static/css/app.css'):
    with urllib.request.urlopen('http://127.0.0.1:8080' + path, timeout=4) as response:
        assert response.status == 200, path
        assert response.headers['Content-Security-Policy'], path
" || fail "templates or static files are missing from the image"
# Not `grep -q`: it exits at the first match, the log command then dies of SIGPIPE, and
# `set -o pipefail` turns that into a failure even though the line was found.
docker logs "$NAME" 2>&1 | grep "Setup token: " >/dev/null || fail "setup token not logged"

if docker exec "$NAME" python -c "import playwright" 2>/dev/null; then
  echo "== headless Chromium (browser image)"
  # Serve a page whose job links only appear after JavaScript runs, then probe it.
  docker exec -i "$NAME" sh -c 'mkdir -p /tmp/site && cat > /tmp/site/index.html' <<'HTML'
<!doctype html><html><body><div id="jobs"></div><script>
document.getElementById("jobs").innerHTML = '<a href="/job/1">Smoke Test Role</a>';
</script></body></html>
HTML
  docker exec -d "$NAME" python -m http.server 8765 --bind 127.0.0.1 --directory /tmp/site
  sleep 1
  output=$(docker exec -e ALLOW_PRIVATE_URLS=true "$NAME" \
    jobmonitor probe http://127.0.0.1:8765/ --render browser 2>&1) || fail "probe failed: $output"
  echo "$output" | grep "Smoke Test Role" >/dev/null || fail "Chromium did not render the page: $output"
else
  echo "== no browser in this image (lite build)"
fi
docker logs "$NAME" 2>&1 | grep "Migrated database from empty" >/dev/null || fail "no initial migration"
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
  docker logs "$NAME" 2>&1 | tail -n 20 | grep "Database is up to date" >/dev/null \
    || fail "database was not reported up to date after $1"
  # Daily backups are expected; a pre-migration backup would mean the schema was migrated again.
  if docker exec "$NAME" sh -c 'ls /data/backups 2>/dev/null | grep -q "^pre-migrate-"'; then
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
