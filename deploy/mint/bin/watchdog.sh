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
SIGN_MAX_AGE="${SIGN_MAX_AGE:-7800}"            # 130 min: the signer runs a minute after the seal
COSIGN_MAX_AGE="${COSIGN_MAX_AGE:-259200}"      # 72 h: the root cosign is made by hand on the PC with the daily offsite copy
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
# signing is watched once it has succeeded at least once (rm state/sign.last_ok state/sign.first_ok after turning it off on purpose)
[ ! -f "$STATE_DIR/sign.last_ok" ] || stale sign.last_ok "$SIGN_MAX_AGE" "attestation signing is stale" "the attestation signing"
if [ -f "$STATE_DIR/sign.first_ok" ] && [ -s "$SECRETS_DIR/api-key" ]; then
  # the newest root cosign (made on the PC); if there never was one, count from the first successful signing
  newest="$({ printf 'header = "X-API-Key: %s"\n' "$(tr -d '\r\n' < "$SECRETS_DIR/api-key")"; } | curl -fsS -m 10 -K - "http://127.0.0.1:$APP_PORT/api/jarvis/trust/statements?limit=1000" 2>/dev/null \
    | python3 -c '
import json, sys
from datetime import datetime
try:
    rows = [s for s in json.load(sys.stdin)["statements"] if s["kind"] == "cosign"]
    print(int(max(datetime.fromisoformat(s["stored_at"]).timestamp() for s in rows)) if rows else "")
except Exception:
    print("")' || true)"
  since="${newest:-$(cat "$STATE_DIR/sign.first_ok")}"
  cosign_age=$(( $(date +%s) - since ))
  [ "$cosign_age" -le "$COSIGN_MAX_AGE" ] || problem "no root cosign for $(( cosign_age / 3600 )) h (limit $(( COSIGN_MAX_AGE / 3600 )) h): the PC witness has not cosigned a checkpoint (docs/SIGNING_RUNBOOK.md)" "root cosign overdue"
fi

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
