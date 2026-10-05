#!/usr/bin/env bash
# Shared helpers. Source this; do not execute it.
# shellcheck shell=bash
# shellcheck disable=SC2034  # the variables below are this library's interface to the scripts that source it

JARVIS_DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export JARVIS_DEPLOY_DIR
# Local, non-secret tuning (deploy/mint/.env, gitignored; docker compose reads the same file).
if [ -f "$JARVIS_DEPLOY_DIR/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$JARVIS_DEPLOY_DIR/.env"
  set +a
fi
export JARVIS_HOME="${JARVIS_HOME:-$HOME/jarvis-ledger}"

BACKUP_DIR="${JARVIS_BACKUP_DIR:-$JARVIS_HOME/backups}"
LOG_DIR="${JARVIS_LOG_DIR:-$JARVIS_HOME/logs}"
STATE_DIR="${JARVIS_STATE_DIR:-$JARVIS_HOME/state}"
SECRETS_DIR="$JARVIS_DEPLOY_DIR/secrets"

PROJECT=jarvis-ledger
DB_CONTAINER=jarvis-db
APP_CONTAINER=jarvis-app
APP_IMAGE=jarvis-ledger-app:local
DB_IMAGE=jarvis-ledger-db:16
VOL_PG="${PROJECT}_pgdata"
VOL_DATA="${PROJECT}_appdata"
NETWORK="${PROJECT}_ledger"
APP_PORT="${JARVIS_APP_PORT:-8011}"

COMPOSE=(docker compose -f "$JARVIS_DEPLOY_DIR/docker-compose.yml")

mkdir -p "$BACKUP_DIR" "$LOG_DIR" "$STATE_DIR"
chmod 700 "$JARVIS_HOME" "$BACKUP_DIR" 2>/dev/null || true

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
stamp_utc() { date -u +%Y%m%dT%H%M%SZ; }

# log LEVEL message... : to stderr and to $LOG_DIR/$LOG_NAME.log
log() {
  local level="$1"; shift
  printf '%s %-5s %s\n' "$(now_utc)" "$level" "$*" | tee -a "$LOG_DIR/${LOG_NAME:-jarvis}.log" >&2
}

die() { log ERROR "$*"; NOTIFIED=1; notify "Jarvis ledger: ${LOG_NAME:-task} failed" "$*" critical; exit 1; }

# notify TITLE MESSAGE [urgency]
# Always appended to alerts.log. A desktop notification is added when the box has a session bus and
# notify-send; the same title is shown at most once per NOTIFY_EVERY_SECONDS (default 6 h) so an hourly
# failure does not become an hourly popup. Never put secrets in a message.
notify() {
  local title="$1" message="$2" urgency="${3:-normal}"
  printf '%s %s | %s\n' "$(now_utc)" "$title" "$message" >> "$LOG_DIR/alerts.log"
  local key
  key="$STATE_DIR/notified.$(printf '%s' "$title" | tr -c 'A-Za-z0-9' '_')"
  local every="${NOTIFY_EVERY_SECONDS:-21600}"
  if [ -f "$key" ] && [ $(( $(date +%s) - $(cat "$key" 2>/dev/null || echo 0) )) -lt "$every" ]; then
    return 0
  fi
  if command -v notify-send >/dev/null 2>&1; then
    local bus="${DBUS_SESSION_BUS_ADDRESS:-}"
    if [ -z "$bus" ] && [ -S "/run/user/$(id -u)/bus" ]; then bus="unix:path=/run/user/$(id -u)/bus"; fi
    if [ -n "$bus" ]; then
      DBUS_SESSION_BUS_ADDRESS="$bus" notify-send -u "$urgency" -a "Jarvis ledger" "$title" "$message" 2>/dev/null \
        && date +%s > "$key" || true
    fi
  fi
}

# Run a command in the database container as the postgres OS user (peer auth over the local socket).
pg_exec() { docker exec -i -u postgres "$DB_CONTAINER" "$@"; }

require_db() {
  [ "$(docker inspect -f '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null)" = "true" ] \
    || die "database container $DB_CONTAINER is not running"
}

# Wait until $1 (a container) reports healthy, up to $2 seconds.
wait_healthy() {
  local name="$1" limit="${2:-120}" i=0 state
  while [ "$i" -lt "$limit" ]; do
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || true)"
    [ "$state" = "healthy" ] && return 0
    sleep 2; i=$((i + 2))
  done
  return 1
}

# Wait until a database container is fully initialised and serving: the FINAL server answers on TCP (the
# temporary init server does not) and the ledger roles exist (the init script has run).
wait_db_ready() {
  local c="$1" limit="${2:-180}" i=0
  while [ "$i" -lt "$limit" ]; do
    if docker exec "$c" pg_isready -q -h 127.0.0.1 -U postgres -d jarvis 2>/dev/null        && docker exec -u postgres "$c" psql -X -At -d jarvis -c "select 1 from pg_roles where rolname = 'jarvis_app'" 2>/dev/null | grep -q 1; then
      return 0
    fi
    sleep 2; i=$((i + 2))
  done
  return 1
}

# file age in seconds
age_seconds() { echo $(( $(date +%s) - $(stat -c %Y "$1") )); }

# Hold an exclusive lock for the rest of the script; exit quietly if another run holds it.
single_instance() {
  exec 9>"$STATE_DIR/$1.lock"
  flock -n 9 || { log WARN "another $1 run is in progress; skipping"; exit 0; }
}
