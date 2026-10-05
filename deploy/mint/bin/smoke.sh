#!/usr/bin/env bash
# Acceptance checks for a running stack. Prints PASS/FAIL per check, exits non-zero if any FAIL.
#   smoke.sh [--no-write]      (--no-write skips the one write/history/delete round trip)
# Reads the API key from secrets/api-key; never prints it.
set -Eeuo pipefail
export LOG_NAME=smoke
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

write=1; [ "${1:-}" = "--no-write" ] && write=0
fails=0
pass() { printf 'PASS  %s\n' "$1"; }
note() { printf 'NOTE  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi; }

base="http://127.0.0.1:$APP_PORT"
key="$(cat "$SECRETS_DIR/api-key")"
http() { curl -sS -m 15 -o /dev/null -w '%{http_code}' "$@"; }

echo "== containers"
for c in "$DB_CONTAINER" "$APP_CONTAINER"; do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null)" = "healthy" ] && pass "$c is healthy" || fail "$c is not healthy"
done

echo "== exposure"
[ -z "$(docker port "$DB_CONTAINER" 2>/dev/null)" ] && pass "database publishes no port" || fail "database publishes a port: $(docker port "$DB_CONTAINER")"
bad="$(docker port "$APP_CONTAINER" 2>/dev/null | grep -v ' -> 127\.0\.0\.1:' || true)"
[ -z "$bad" ] && pass "app is published on 127.0.0.1 only" || fail "app is published beyond loopback: $bad"
if command -v ss >/dev/null 2>&1; then
  listeners="$(ss -ltnH 2>/dev/null | awk '{print $4}')"
  # Any listener on 5432 may belong to something else on the host (e.g. a native PostgreSQL); what matters is that
  # none of OUR containers publishes it.
  [ -z "$(docker ps -q --filter publish=5432)" ] && pass "no container publishes 5432" || fail "a container publishes 5432"
  other="$(echo "$listeners" | grep -E '(^|:)5432$' | tr '\n' ' ')"
  [ -z "$other" ] || note "something that is not this stack listens on $other"
  if echo "$listeners" | grep -E ":$APP_PORT\$" | grep -vq '^127\.0\.0\.1:'; then fail "port $APP_PORT is open beyond loopback"; else pass "port $APP_PORT only on loopback"; fi
fi

echo "== service"
[ "$(http "$base/health")" = "200" ] && pass "/health 200" || fail "/health"
ready="$(curl -sS -m 15 "$base/ready" || true)"
echo "$ready" | grep -q '"status":"ready"' && pass "/ready ready: $ready" || fail "/ready: $ready"
[ "$(http "$base/api/jarvis/memory")" = "401" ] && pass "no key -> 401" || fail "request without a key was not refused"
[ "$(http -H "X-API-Key: wrong-key" "$base/api/jarvis/memory")" = "401" ] && pass "wrong key -> 401" || fail "wrong key was not refused"
[ "$(http -H "X-API-Key: $key" "$base/api/jarvis/memory")" = "200" ] && pass "right key -> 200" || fail "right key was refused"

echo "== container hardening"
[ "$(docker exec "$APP_CONTAINER" id -u)" = "10001" ] && pass "app runs as uid 10001" || fail "app does not run as uid 10001"
docker exec "$APP_CONTAINER" sh -c 'touch /app/x 2>/dev/null' && fail "app root filesystem is writable" || pass "app root filesystem is read-only"
[ "$(docker exec "$APP_CONTAINER" sh -c "grep CapEff /proc/self/status | tr -d '[:space:]'")" = "CapEff:0000000000000000" ] \
  && pass "app has no Linux capabilities" || fail "app has capabilities"
[ "$(docker exec "$APP_CONTAINER" sh -c "awk '/NoNewPrivs/ {print \$2}' /proc/self/status")" = "1" ] \
  && pass "no-new-privileges is on" || fail "no-new-privileges is off"

echo "== database roles"
flags="$(pg_exec psql -X -At -d jarvis -c "select string_agg(rolname || ':' || rolsuper::int || rolbypassrls::int || rolcreaterole::int || rolcreatedb::int || rolreplication::int, ',' order by rolname) from pg_roles where rolname in ('jarvis_app','jarvis_migrator')")"
[ "$flags" = "jarvis_app:00000,jarvis_migrator:00000" ] && pass "both ledger roles are ordinary (no superuser/bypassrls/createrole/createdb/replication)" || fail "role flags: $flags"
docker exec "$APP_CONTAINER" python -c "
import os, sys, psycopg
url = os.environ['JARVIS_DATABASE_URL'].replace('//jarvis_app:', '//postgres:')
try:
    psycopg.connect(url, connect_timeout=5)
except psycopg.Error as e:
    sys.exit(0 if 'reject' in str(e).lower() or 'authentication' in str(e).lower() else 1)
sys.exit(1)" && pass "the superuser cannot log in over the network" || fail "the superuser could log in over the network"

docker exec -i "$APP_CONTAINER" python - <<'PY' && pass "app role is refused DDL and history writes" || fail "app role was allowed something it must not do"
import os, sys, psycopg
conn = psycopg.connect(os.environ["JARVIS_DATABASE_URL"], options="-c search_path=jarvis", connect_timeout=5, autocommit=True)
for stmt in ("CREATE TABLE jarvis.sneaky (x int)", "UPDATE jarvis.record_history SET actor = 'x'",
             "DELETE FROM jarvis.chain_heads", "ALTER TABLE jarvis.memories DISABLE ROW LEVEL SECURITY"):
    try:
        conn.execute(stmt); sys.exit(1)
    except psycopg.errors.InsufficientPrivilege:
        pass
    except psycopg.Error:
        sys.exit(1)
PY

if [ "$write" -eq 1 ]; then
  echo "== one write, history, verify, delete (leaves a trace in history, by design)"
  created="$(curl -sS -m 15 -H "X-API-Key: $key" -H 'Content-Type: application/json' \
     -d '{"content":"smoke test record - safe to ignore","source_agent":"smoke","session_id":"smoke","type":"fact","subject":"smoke-test"}' "$base/api/jarvis/memory")"
  id="$(echo "$created" | sed -n 's/.*"id":"\(mem-[0-9a-f]*\)".*/\1/p' | head -1)"
  [ -n "$id" ] && pass "write created $id" || fail "write failed: $created"
  if [ -n "$id" ]; then
    hist="$(curl -sS -m 15 -H "X-API-Key: $key" "$base/api/jarvis/memory/$id/history")"
    echo "$hist" | grep -q '"op":"create"' && pass "history read shows the create" || fail "history read: $hist"
    verify="$(curl -sS -m 15 -H "X-API-Key: $key" "$base/api/jarvis/memory/history/verify")"
    echo "$verify" | grep -q '"ok":true' && pass "history chain verifies" || fail "verify: $verify"
    [ "$(http -X DELETE -H "X-API-Key: $key" "$base/api/jarvis/memory/$id")" = "200" ] && pass "delete ok" || fail "delete failed"
  fi
  # endpoints that write files outside Postgres must work with a read-only root filesystem
  [ "$(http -X POST -H "X-API-Key: $key" -H 'Content-Type: application/json' -d '{"query":"smoke test","session_key":"smoke","theta_promote":0.0}' "$base/api/jarvis/memory/emr/excite")" = "200" ] \
    && pass "EMR excite (writes the overlay file) works" || fail "EMR excite failed"
  docker logs "$APP_CONTAINER" 2>&1 | grep -qi 'read-only file system' && fail "the app log reports a read-only filesystem error" || pass "no read-only filesystem errors in the app log"
fi

echo
[ "$fails" -eq 0 ] && { echo "ALL CHECKS PASSED"; exit 0; } || { echo "$fails CHECK(S) FAILED"; exit 1; }
