#!/usr/bin/env bash
# Create the three env files the stack reads, with fresh random passwords and API key.
# Run it once, on the box. It never prints a secret and refuses to overwrite existing files.
#
#   deploy/mint/secrets/db.env       bootstrap password (never used over the network) + the two role passwords
#   deploy/mint/secrets/app.env      JARVIS_DATABASE_URL (ordinary role) + JARVIS_API_KEY
#   deploy/mint/secrets/migrate.env  JARVIS_DATABASE_MIGRATE_URL (DDL role), schema, app role
#   deploy/mint/secrets/api-key      the same API key, alone, for your hooks (JARVIS_API_KEY_FILE)
set -Eeuo pipefail
export LOG_NAME=gen-secrets
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

umask 077
mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"

for f in db.env app.env migrate.env api-key; do
  [ ! -e "$SECRETS_DIR/$f" ] || { echo "refusing to overwrite $SECRETS_DIR/$f (delete it yourself if you really mean to start over)" >&2; exit 1; }
done

rand() {  # 48 hex characters; URL-safe, no quoting issues anywhere
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
  else python3 -c 'import secrets; print(secrets.token_hex(24))'; fi
}

pg_pw="$(rand)"; app_pw="$(rand)"; mig_pw="$(rand)"; api_key="$(rand)"

printf 'POSTGRES_PASSWORD=%s\nJARVIS_APP_PASSWORD=%s\nJARVIS_MIGRATOR_PASSWORD=%s\n' "$pg_pw" "$app_pw" "$mig_pw" > "$SECRETS_DIR/db.env"
printf 'JARVIS_DATABASE_URL=postgresql://jarvis_app:%s@db:5432/jarvis\nJARVIS_API_KEY=%s\n' "$app_pw" "$api_key" > "$SECRETS_DIR/app.env"
printf 'JARVIS_DATABASE_MIGRATE_URL=postgresql://jarvis_migrator:%s@db:5432/jarvis\nJARVIS_DATABASE_SCHEMA=jarvis\nJARVIS_DATABASE_APP_ROLE=jarvis_app\n' "$mig_pw" > "$SECRETS_DIR/migrate.env"
printf '%s\n' "$api_key" > "$SECRETS_DIR/api-key"
chmod 600 "$SECRETS_DIR"/db.env "$SECRETS_DIR"/app.env "$SECRETS_DIR"/migrate.env "$SECRETS_DIR"/api-key
ensure_trust_roots

echo "created (mode 600, values not shown): db.env app.env migrate.env api-key in $SECRETS_DIR"
echo "next: put your age PUBLIC key in $SECRETS_DIR/age_recipient.txt and fill in $SECRETS_DIR/offsite.conf"
