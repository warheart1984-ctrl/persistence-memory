#!/usr/bin/env bash
# Replace the live database (and the appdata files) with a backup set. DESTRUCTIVE.
#
#   restore.sh --yes-destroy-current-data [--backup jarvis-<UTC> | /path/to/jarvis-<UTC>.dump] [--no-start]
#
# Order of events - and the app stays DOWN until every gate has passed:
#   1. the set's checksums and table of contents are verified;
#   2. a safety dump of whatever is running now is taken (pre-restore-<UTC>.dump, never pruned);
#   3. app and database are removed; database and appdata volumes are recreated empty;
#   4. the database initialises (roles are created from secrets/db.env) and the dump is restored in ONE
#      transaction (all or nothing);
#   5. gates: restored row counts == the set's counts; restored chain-head anchors == the set's anchors;
#      the history hash chain verifies for every tenant;
#   6. only then the app is started and must report /ready.
# This is the interim, scripted form of "verify against exported anchors after a restore, before serving".
set -Eeuo pipefail
export LOG_NAME=restore
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=anchors.sh
source "$(dirname "${BASH_SOURCE[0]}")/anchors.sh"
umask 077

backup="" confirmed=0 start=1
while [ $# -gt 0 ]; do
  case "$1" in
    --backup) backup="$2"; shift ;;
    --yes-destroy-current-data) confirmed=1 ;;
    --no-start) start=0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$confirmed" -eq 1 ] || { echo "restore replaces the live database. Re-run with --yes-destroy-current-data to proceed." >&2; exit 2; }
if [ -t 0 ]; then read -r -p "Type RESTORE to destroy the current ledger and restore a backup: " answer; [ "$answer" = "RESTORE" ] || exit 2; fi

# ---- 1. pick and verify the set ------------------------------------------------------------------------
if [ -z "$backup" ]; then
  base="$(ls -1 "$BACKUP_DIR" | sed -n 's/^\(jarvis-[0-9]\{8\}T[0-9]\{6\}Z\)\.sha256$/\1/p' | LC_ALL=C sort | tail -1)"
  [ -n "$base" ] || die "no complete backup set found in $BACKUP_DIR"
else
  base="$(basename "$backup")"; base="${base%.dump}"; base="${base%.sha256}"
fi
for part in dump globals.sql data.tar counts anchors sha256; do
  [ -s "$BACKUP_DIR/$base.$part" ] || { [ "$part" = "globals.sql" ] && [ -e "$BACKUP_DIR/$base.$part" ]; } || die "backup set $base is incomplete (missing .$part)"
done
( cd "$BACKUP_DIR" && sha256sum -c --quiet "$base.sha256" ) || die "checksum mismatch in backup set $base - it is damaged"
log INFO "restoring from $base"

# ---- 2. safety dump of whatever is running now ---------------------------------------------------------
safety="$BACKUP_DIR/pre-restore-$(stamp_utc).dump"
if [ "$(docker inspect -f '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null || echo false)" = "true" ]; then
  if pg_exec pg_dump -Fc -d jarvis > "$safety" 2>/dev/null && [ -s "$safety" ]; then
    log INFO "safety dump of the current database: $safety"
  else
    rm -f "$safety"; log WARN "could not take a safety dump of the current database; continuing"
  fi
fi

# ---- 3. tear down and recreate empty volumes -------------------------------------------------------------
"${COMPOSE[@]}" stop app >/dev/null 2>&1 || true
"${COMPOSE[@]}" rm -sf app migrate db >/dev/null 2>&1 || true
docker volume rm -f "$VOL_PG" "$VOL_DATA" >/dev/null
docker volume create --label "com.docker.compose.project=$PROJECT" --label com.docker.compose.volume=pgdata "$VOL_PG" >/dev/null
docker volume create --label "com.docker.compose.project=$PROJECT" --label com.docker.compose.volume=appdata "$VOL_DATA" >/dev/null

# appdata files (AMUL field, STM overlay, RAG): the app image's /data is owned by uid 10001
docker run --rm -i -v "$VOL_DATA":/data --entrypoint tar "$APP_IMAGE" -xf - -C /data < "$BACKUP_DIR/$base.data.tar" \
  || die "could not restore the appdata files"

# ---- 4. database ---------------------------------------------------------------------------------------------
"${COMPOSE[@]}" up -d --no-deps db >/dev/null
wait_db_ready "$DB_CONTAINER" 240 || die "the new database did not become ready"
pg_exec pg_restore --single-transaction --exit-on-error -d jarvis < "$BACKUP_DIR/$base.dump" \
  || die "pg_restore failed (the app has NOT been started; the database volume is freshly initialised and empty)"

# ---- 5. gates -----------------------------------------------------------------------------------------------------
restored_counts="$(for t in memories boards record_history chain_heads history_counters; do
  printf '%s=%s\n' "$t" "$(pg_exec psql -X -At -d jarvis -c "select count(*) from jarvis.$t")"; done | LC_ALL=C sort)"
expected_counts="$(grep -E '^(memories|boards|record_history|chain_heads|history_counters)=' "$BACKUP_DIR/$base.counts" | LC_ALL=C sort)"
[ "$restored_counts" = "$expected_counts" ] \
  || die "row counts after restore differ from the backup set (restored: $(echo "$restored_counts" | tr '\n' ' ') expected: $(echo "$expected_counts" | tr '\n' ' ')). App NOT started."

if ! diff_out="$(diff <(anchors_from_db) "$BACKUP_DIR/$base.anchors")"; then
  die "chain-head anchors after restore differ from the backup set. App NOT started. $(echo "$diff_out" | head -3 | tr '\n' ';')"
fi

tenants="$(pg_exec psql -X -At -d jarvis -c "select distinct tenant_key from jarvis.record_history order by 1")"
for tenant in $tenants; do
  "${COMPOSE[@]}" run --rm --no-deps -T migrate python -m app.pg_verify --tenant "$tenant" >/dev/null \
    || die "history chain verification FAILED for tenant $tenant after restore. App NOT started."
done
log INFO "gates passed: counts match, anchors match, history chains verify ($(echo "$tenants" | wc -w) tenant(s))"

# ---- 6. serve -------------------------------------------------------------------------------------------------------
if [ "$start" -eq 1 ]; then
  "${COMPOSE[@]}" up -d >/dev/null
  wait_healthy "$APP_CONTAINER" 180 || die "the app did not become healthy after the restore"
  curl -fsS -m 10 "http://127.0.0.1:$APP_PORT/ready" >/dev/null || die "/ready failed after the restore"
fi
log INFO "restore of $base complete"
notify "Jarvis ledger: restore completed" "restored $base" normal
