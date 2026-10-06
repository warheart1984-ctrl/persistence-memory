#!/usr/bin/env bash
# Restore drill: prove the newest backup set restores AND verifies, in a throwaway database that never
# touches the live one. Run weekly (jarvis-drill.timer). A backup nobody has restored is a hope, not a backup.
#
#   drill.sh [--backup jarvis-<UTC>] [--prove-detection]
#
# Checks: checksums; single-transaction restore; restored row counts == the set's counts; restored anchors ==
# the set's anchors; the history hash chain verifies for every tenant; the appdata archive lists cleanly.
# --prove-detection then tampers with the scratch copy (removes the newest history entry) and requires the
# verifier to FAIL, proving the alarm would actually ring; with sealed blocks it also removes the newest block and
# requires the anchors to notice, and alters an entry of the last block (then undoes it) and requires the replay to notice.
# With sealed blocks the drill also replays the restored copy (RC.Ledger.v1) at each tenant's last anchored block.
set -Eeuo pipefail
export LOG_NAME=drill
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=anchors.sh
source "$(dirname "${BASH_SOURCE[0]}")/anchors.sh"
umask 077

single_instance drill
backup="" prove=0
while [ $# -gt 0 ]; do
  case "$1" in
    --backup) backup="$2"; shift ;;
    --prove-detection) prove=1 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

name=jarvis-drill-db; net=jarvis-drill-net
cleanup() { rc=$?; docker rm -f "$name" >/dev/null 2>&1 || true; docker network rm "$net" >/dev/null 2>&1 || true
            [ "$rc" -eq 0 ] || [ "${NOTIFIED:-0}" = 1 ] || notify "Jarvis ledger: restore drill FAILED" "see $LOG_DIR/drill.log" critical; }
trap cleanup EXIT
cleanup_quiet() { docker rm -f "$name" >/dev/null 2>&1 || true; docker network rm "$net" >/dev/null 2>&1 || true; }
cleanup_quiet

rand() { if command -v openssl >/dev/null 2>&1; then openssl rand -hex 16; else python3 -c 'import secrets;print(secrets.token_hex(16))'; fi; }

if [ -z "$backup" ]; then
  base="$(ls -1 "$BACKUP_DIR" | sed -n 's/^\(jarvis-[0-9]\{8\}T[0-9]\{6\}Z\)\.sha256$/\1/p' | LC_ALL=C sort | tail -1)"
  [ -n "$base" ] || die "no complete backup set found in $BACKUP_DIR"
else
  base="$(basename "$backup")"; base="${base%.dump}"; base="${base%.sha256}"
fi
( cd "$BACKUP_DIR" && sha256sum -c --quiet "$base.sha256" ) || die "checksum mismatch in backup set $base"
log INFO "drill: restoring $base into a scratch database"

mig_pw="$(rand)"
docker network create "$net" >/dev/null
docker run -d --name "$name" --network "$net" \
  -e POSTGRES_PASSWORD="$(rand)" -e POSTGRES_DB=jarvis -e POSTGRES_INITDB_ARGS=--data-checksums \
  -e JARVIS_APP_PASSWORD="$(rand)" -e JARVIS_MIGRATOR_PASSWORD="$mig_pw" \
  "$DB_IMAGE" postgres -c config_file=/etc/postgresql/postgresql.conf >/dev/null
wait_db_ready "$name" 240 || die "scratch database did not become ready"

docker exec -i -u postgres "$name" pg_restore --single-transaction --exit-on-error -d jarvis < "$BACKUP_DIR/$base.dump" \
  || die "DRILL FAILED: the backup does not restore"

scratch_psql() { docker exec -i -u postgres "$name" psql -X -At -d jarvis "$@"; }
# a set taken after schema v5 also counts evidence_objects, after v6 also blocks; an older set has no such line and is
# checked as before
count_tables="memories boards record_history chain_heads history_counters"
if grep -q '^evidence_objects=' "$BACKUP_DIR/$base.counts"; then count_tables="$count_tables evidence_objects"; fi
if grep -q '^blocks=' "$BACKUP_DIR/$base.counts"; then count_tables="$count_tables blocks"; fi
count_re="^($(echo "$count_tables" | tr ' ' '|'))="
restored="$(for t in $count_tables; do
  printf '%s=%s\n' "$t" "$(scratch_psql -c "select count(*) from jarvis.$t")"; done | LC_ALL=C sort)"
expected="$(grep -E "$count_re" "$BACKUP_DIR/$base.counts" | LC_ALL=C sort)"
[ "$restored" = "$expected" ] || die "DRILL FAILED: restored row counts differ from the backup set"

scratch_anchors() { anchors_collect scratch_psql; }
diff <(scratch_anchors) "$BACKUP_DIR/$base.anchors" >/dev/null || die "DRILL FAILED: restored anchors differ from the backup set"

verify_scratch() {
  local t rc=0
  for t in $(scratch_psql -c "select distinct tenant_key from jarvis.record_history order by 1"); do
    docker run --rm --network "$net" -e "JARVIS_DATABASE_MIGRATE_URL=postgresql://jarvis_migrator:$mig_pw@$name:5432/jarvis" \
      -e JARVIS_DATABASE_SCHEMA=jarvis "$APP_IMAGE" python -m app.pg_verify --tenant "$t" >/dev/null 2>&1 || rc=1
  done
  return "$rc"
}
verify_scratch || die "DRILL FAILED: the history chain does not verify in the restored copy"

# Replay (RC.Ledger.v1) at each tenant's last anchored block, in the restored copy, against the set's own anchors: the state is
# rebuilt from the raw history entries and the sealed block that covers it must be the one the anchors name (hash included).
# Skipped, loudly, when the set has no sealed blocks or the app image predates the replay module.
replay_scratch() {
  local t h bh rc=0
  while IFS='|' read -r t h bh; do
    [ -n "$t" ] || continue
    docker run --rm --network "$net" -e "JARVIS_DATABASE_MIGRATE_URL=postgresql://jarvis_migrator:$mig_pw@$name:5432/jarvis" \
      -e JARVIS_DATABASE_SCHEMA=jarvis "$APP_IMAGE" python -m app.replay verify --tenant "$t" --at-block "$h" --expect-block-hash "$bh" >/dev/null 2>&1 || rc=1
  done < <(anchors_tip_blocks "$BACKUP_DIR/$base.anchors")
  return "$rc"
}
replayed=0
if [ -z "$(anchors_tip_blocks "$BACKUP_DIR/$base.anchors")" ]; then
  log INFO "drill: replay step skipped (the set has no sealed blocks)"
elif ! docker run --rm --entrypoint python "$APP_IMAGE" -c "import app.replay" >/dev/null 2>&1; then
  log WARN "drill: replay step skipped (the app image predates RC.Ledger.v1; rebuild with jarvisctl up)"
else
  replay_scratch || die "DRILL FAILED: replaying the restored copy at its last anchored block does not verify"
  replayed=1
  log INFO "drill: replay at the last anchored block verifies ($(anchors_tip_blocks "$BACKUP_DIR/$base.anchors" | tr '\n' ' '| sed 's/|[0-9a-f]\{64\}//g'))"
fi

entries="$(docker run --rm -i --entrypoint tar "$APP_IMAGE" -tf - < "$BACKUP_DIR/$base.data.tar" | wc -l)" \
  || die "DRILL FAILED: the appdata archive is unreadable"

if [ "$prove" -eq 1 ]; then
  if [ "$replayed" -eq 1 ]; then
    # alter the newest entry of the last anchored block, require the replay to fail, then put it back exactly and require it to pass
    IFS='|' read -r pt ph pbh < <(anchors_tip_blocks "$BACKUP_DIR/$base.anchors" | head -1)
    if [[ "$pt" =~ ^[A-Za-z0-9_.@:-]+$ ]] && [[ "$ph" =~ ^[0-9]+$ ]]; then
      plast="$(scratch_psql -c "select last_seq from jarvis.blocks where tenant_key = '$pt' and height = $ph")"
      porig="$(scratch_psql -c "select row_hash from jarvis.record_history where tenant_key = '$pt' and seq = $plast")"
      scratch_psql -c "ALTER TABLE jarvis.record_history DISABLE TRIGGER record_history_no_update" >/dev/null
      scratch_psql -c "UPDATE jarvis.record_history SET row_hash = repeat('e', 64) WHERE tenant_key = '$pt' AND seq = $plast" >/dev/null
      if replay_scratch; then die "DRILL FAILED: an altered entry in the last block was NOT detected by the replay"; fi
      scratch_psql -c "UPDATE jarvis.record_history SET row_hash = '$porig' WHERE tenant_key = '$pt' AND seq = $plast" >/dev/null
      scratch_psql -c "ALTER TABLE jarvis.record_history ENABLE TRIGGER record_history_no_update" >/dev/null
      replay_scratch || die "DRILL FAILED: the restored copy does not replay again after the proof was undone"
      log INFO "drill: an altered entry in the last block was detected by the replay, as it must be (and undone)"
    else
      log WARN "drill: replay proof skipped (unexpected tenant or height in the anchors)"
    fi
  fi
  if [ "$(scratch_psql -c "select count(*) from jarvis.record_history")" -gt 0 ]; then
    scratch_psql -c "ALTER TABLE jarvis.record_history DISABLE TRIGGER record_history_no_delete" >/dev/null
    scratch_psql -c "DELETE FROM jarvis.record_history WHERE seq = (SELECT max(seq) FROM jarvis.record_history)" >/dev/null
    if verify_scratch; then die "DRILL FAILED: tampering with the restored history was NOT detected"; fi
    log INFO "drill: tampering with the scratch copy was detected, as it must be"
  else
    log WARN "drill: --prove-detection skipped (no history yet)"
  fi
  if [ "$(scratch_psql -c "select case when to_regclass('jarvis.blocks') is null then 0 else (select count(*) from jarvis.blocks) end")" -gt 0 ]; then
    # the anchor must notice a removed newest block, which nothing inside the database can
    scratch_psql -c "ALTER TABLE jarvis.blocks DISABLE TRIGGER blocks_no_delete" >/dev/null
    scratch_psql -c "DELETE FROM jarvis.blocks WHERE height = (SELECT max(height) FROM jarvis.blocks)" >/dev/null
    if diff <(scratch_anchors) "$BACKUP_DIR/$base.anchors" >/dev/null; then die "DRILL FAILED: removing the newest block was NOT detected by the anchors"; fi
    log INFO "drill: removing the newest block of the scratch copy was detected by the anchors, as it must be"
  else
    log WARN "drill: block anchor proof skipped (no sealed blocks yet)"
  fi
fi

date +%s > "$STATE_DIR/drill.last_ok"
log INFO "drill OK: $base restored, counts and anchors match, history verifies$([ "$replayed" -eq 1 ] && echo ", replay at the last anchored block verifies"), appdata archive has $entries entries"
