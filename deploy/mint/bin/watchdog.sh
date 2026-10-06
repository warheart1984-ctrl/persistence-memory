#!/usr/bin/env bash
# Every 15 minutes: is everything that should be happening actually happening?
# Problems are logged to alerts.log and shown as a desktop notification (deduplicated); exit 1 if any.
set -Eeuo pipefail
export LOG_NAME=watchdog
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=anchors.sh
source "$(dirname "${BASH_SOURCE[0]}")/anchors.sh"

BACKUP_MAX_AGE="${BACKUP_MAX_AGE:-7800}"        # 130 min: an hourly job may be late, not missing
OFFSITE_MAX_AGE="${OFFSITE_MAX_AGE:-172800}"    # 48 h
DRILL_MAX_AGE="${DRILL_MAX_AGE:-864000}"        # 10 days
SEAL_MAX_AGE="${SEAL_MAX_AGE:-7800}"            # 130 min, like the backup it precedes
DISK_MIN_FREE_PCT="${DISK_MIN_FREE_PCT:-10}"

problems=0
problem() { problems=$((problems + 1)); log WARN "$1"; notify "Jarvis ledger: $2" "$1" critical; }

stale() {  # stale <state-file> <max-age> <title> <what>
  local f="$STATE_DIR/$1"
  if [ ! -f "$f" ]; then problem "$4 has never succeeded" "$3"; return; fi
  local age=$(( $(date +%s) - $(cat "$f") ))
  [ "$age" -le "$2" ] || problem "$4 last succeeded $(( age / 3600 )) h $(( age % 3600 / 60 )) min ago (limit $(( $2 / 3600 )) h)" "$3"
}

stale backup.last_ok "$BACKUP_MAX_AGE" "backup is stale" "the hourly backup"
[ ! -f "$SECRETS_DIR/offsite.conf" ] || stale offsite.last_ok "$OFFSITE_MAX_AGE" "offsite copy is stale" "the offsite copy"
stale drill.last_ok "$DRILL_MAX_AGE" "restore drill overdue" "the restore drill"
# the seal timer is optional: it is only watched once it has succeeded at least once (rm state/seal.last_ok after turning it off on purpose)
[ ! -f "$STATE_DIR/seal.last_ok" ] || stale seal.last_ok "$SEAL_MAX_AGE" "block seal is stale" "the block seal"

used="$(df -P "$BACKUP_DIR" | awk 'NR==2 { gsub("%", "", $5); print $5 }')"
[ $(( 100 - used )) -ge "$DISK_MIN_FREE_PCT" ] || problem "the backup disk is ${used}% full" "backup disk almost full"

for c in "$DB_CONTAINER" "$APP_CONTAINER"; do
  state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null || echo missing)"
  [ "$state" = "healthy" ] || problem "container $c is $state" "$c not healthy"
done
curl -fsS -m 10 "http://127.0.0.1:$APP_PORT/ready" >/dev/null 2>&1 || problem "GET /ready does not return 200" "ledger not ready"

if ! chain_problem="$(anchors_verify_chain "$BACKUP_DIR/anchors" 2>&1)"; then
  problem "anchors log chain broken: $chain_problem" "ANCHORS LOG TAMPERED"
fi

[ "$problems" -eq 0 ] && log INFO "watchdog ok"
exit $(( problems > 0 ))
