#!/usr/bin/env bash
# Every minute: if the database or app container is stopped when it should be running, start it again.
#
# Why this exists: `restart: unless-stopped` makes Docker restart a container after a crash, an OOM kill or a
# reboot, but Docker deliberately never restarts a container that was stopped or killed through the API
# (`docker kill`, `docker stop`). This closes that gap. `jarvisctl down` leaves a marker so a stop on purpose
# is respected until the next `jarvisctl up`.
set -Eeuo pipefail
export LOG_NAME=heal
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ ! -f "$STATE_DIR/stopped-on-purpose" ] || exit 0
[ -f "$SECRETS_DIR/app.env" ] || exit 0   # not set up yet

rc=0
for c in "$DB_CONTAINER" "$APP_CONTAINER"; do
  status="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
  case "$status" in
    exited|dead)
      log WARN "container $c was $status; starting it"
      if docker start "$c" >/dev/null; then
        notify "Jarvis ledger: $c was down" "$c had stopped ($status) and was started again by the self-heal timer" normal
      else
        notify "Jarvis ledger: $c will not start" "docker start $c failed; see: jarvisctl logs" critical; rc=1
      fi ;;
  esac
done
exit "$rc"
