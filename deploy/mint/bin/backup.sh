#!/usr/bin/env bash
# Hourly backup set:  jarvis-<UTC>.{dump,globals.sql,data.tar,counts,anchors,sha256}
#
#  * pg_dump runs as the container's postgres SUPERUSER over its local socket. That matters: the ledger uses
#    forced row-level security, and a dump as an ordinary role either fails or (with --enable-row-security)
#    silently writes ZERO rows. Every dump is therefore checked: table-of-contents, per-table row counts
#    read back out of the dump itself, and a shrink alarm against the previous set.
#  * anchors (chain-head hashes) are written outside the database; a new set must be a legitimate successor
#    of the previous one (nothing vanished, nothing moved backwards).
#  * the set is published atomically; the .sha256 file is written last and marks it complete.
set -Eeuo pipefail
export LOG_NAME=backup
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=anchors.sh
source "$(dirname "${BASH_SOURCE[0]}")/anchors.sh"

single_instance backup
umask 077

ts="$(stamp_utc)"
base="jarvis-$ts"
tmp="$BACKUP_DIR/.tmp-$ts"
mkdir -p "$tmp" "$BACKUP_DIR/anchors"
cleanup() { rc=$?; rm -rf "$tmp"; [ "$rc" -eq 0 ] || [ "${NOTIFIED:-0}" = 1 ] || notify "Jarvis ledger: backup FAILED" "see $LOG_DIR/backup.log" critical; }
trap cleanup EXIT

require_db
log INFO "backup $base starting"

# 1. the database (consistent snapshot; includes schema, RLS policies, triggers, grants)
pg_exec pg_dump -Fc -d jarvis > "$tmp/$base.dump" || die "pg_dump failed"
[ -s "$tmp/$base.dump" ] || die "pg_dump produced an empty file"

# 2. is it a real dump of the ledger?
pg_exec pg_restore --list < "$tmp/$base.dump" > "$tmp/toc.txt" || die "pg_restore --list rejects the dump"
for table in memories boards record_history chain_heads history_counters schema_version; do
  grep -Eq "TABLE DATA jarvis $table " "$tmp/toc.txt" || die "dump has no data section for jarvis.$table"
done

# 3. how many rows did it capture? (counted out of the dump itself)
pg_exec pg_restore --data-only -f - -n jarvis < "$tmp/$base.dump" | awk '
  /^COPY jarvis\./ { split($2, a, "."); table = a[2]; rows[table] = 0; inside = 1; next }
  /^\\\.$/        { inside = 0; next }
  inside          { rows[table]++ }
  END { for (t in rows) print t "=" rows[t] }' | LC_ALL=C sort > "$tmp/$base.counts"
for table in memories boards record_history chain_heads history_counters; do
  grep -q "^$table=" "$tmp/$base.counts" || die "could not count rows of $table in the dump"
done

# 4. shrink alarm: a ledger that suddenly holds far fewer records than the last set deserves a human look
prev_counts="$(ls -1 "$BACKUP_DIR"/jarvis-*.counts 2>/dev/null | LC_ALL=C sort | tail -1 || true)"
if [ -n "$prev_counts" ]; then
  was="$(sed -n 's/^memories=//p' "$prev_counts")"; now_n="$(sed -n 's/^memories=//p' "$tmp/$base.counts")"
  if [ "${was:-0}" -ge 10 ] && [ "$now_n" -lt $(( was * 8 / 10 )) ]; then
    log WARN "memories dropped from $was to $now_n since the previous backup"
    notify "Jarvis ledger: record count dropped" "memories $was -> $now_n between backups" critical
  fi
fi

# 5. anchors: must be a legitimate successor of the last set, and chained to the previous anchors log entry
anchors_from_dump "$tmp/$base.dump" > "$tmp/$base.anchors"
grep -q '^head|' "$tmp/$base.anchors" || [ "$(sed -n 's/^memories=//p' "$tmp/$base.counts")" = "0" ] \
  || die "no chain-head anchors found in a non-empty dump"
prev_anchors="$(ls -1 "$BACKUP_DIR"/jarvis-*.anchors 2>/dev/null | LC_ALL=C sort | tail -1 || true)"
if [ -n "$prev_anchors" ]; then
  if ! problems="$(anchors_check "$prev_anchors" "$tmp/$base.anchors")"; then
    log ERROR "anchor regression: $problems"
    notify "Jarvis ledger: HISTORY ANCHORS REGRESSED" "$(printf '%s' "$problems" | head -3 | tr '\n' ';')" critical
    mkdir -p "$BACKUP_DIR/quarantine"; mv "$tmp/$base.dump" "$BACKUP_DIR/quarantine/$base.dump"
    die "the new dump is not a legitimate successor of the previous backup (quarantined)"
  fi
fi

# 6. the rest of the set
pg_exec pg_dumpall --globals-only > "$tmp/$base.globals.sql" || die "pg_dumpall --globals-only failed"
docker run --rm -v "$VOL_DATA":/data:ro --entrypoint tar "$APP_IMAGE" -cf - -C /data . > "$tmp/$base.data.tar" \
  || die "could not archive the appdata volume"

# 7. long-lived anchors log (tiny, never pruned): only when something changed, each entry hash-chained to the last
last_log="$(ls -1 "$BACKUP_DIR"/anchors/anchors-*.txt 2>/dev/null | LC_ALL=C sort | tail -1 || true)"
if [ -z "$last_log" ] || ! diff -q <(grep -v '^#' "$last_log") "$tmp/$base.anchors" >/dev/null 2>&1; then
  { printf '# prev_sha256=%s\n' "$( [ -n "$last_log" ] && anchor_file_sha "$last_log" || echo 0 )"
    printf '# taken=%s\n' "$(now_utc)"
    cat "$tmp/$base.anchors"; } > "$tmp/anchors-$ts.txt"
  mv "$tmp/anchors-$ts.txt" "$BACKUP_DIR/anchors/anchors-$ts.txt"
fi
anchors_verify_chain "$BACKUP_DIR/anchors" || die "the anchors log chain is broken (an old anchors file was altered or removed)"

# 8. publish: move the parts, then write the checksum file last - it marks the set complete
( cd "$tmp" && sha256sum "$base.dump" "$base.globals.sql" "$base.data.tar" "$base.counts" "$base.anchors" > "$base.sha256" )
for part in dump globals.sql data.tar counts anchors; do mv "$tmp/$base.$part" "$BACKUP_DIR/$base.$part"; done
mv "$tmp/$base.sha256" "$BACKUP_DIR/$base.sha256"

date +%s > "$STATE_DIR/backup.last_ok"
size="$(du -sk "$BACKUP_DIR/$base.dump" | cut -f1)"
log INFO "backup $base ok dump=${size}KB $(tr '\n' ' ' < "$BACKUP_DIR/$base.counts")"

"$(dirname "${BASH_SOURCE[0]}")/prune.sh" || log WARN "pruning failed (backups are kept)"
