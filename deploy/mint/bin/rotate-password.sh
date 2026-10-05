#!/usr/bin/env bash
# Rotate a credential without you ever seeing or typing it:
#   rotate-password.sh app       the jarvis_app role password        (restarts the app)
#   rotate-password.sh migrator  the jarvis_migrator role password
#   rotate-password.sh api-key   the API key your hooks send         (restarts the app; update your other machines)
#
# A new random value is generated, set in the database through stdin with statement logging off, written to the
# env files atomically (mode 600), and never printed.
set -Eeuo pipefail
export LOG_NAME=rotate
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
umask 077

what="${1:-}"
rand() { if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24; else python3 -c 'import secrets; print(secrets.token_hex(24))'; fi; }
rewrite() {  # rewrite FILE SED-EXPRESSION : atomic, keeps mode 600
  local f="$1" expr="$2"
  sed -E "$expr" "$f" > "$f.new" && chmod 600 "$f.new" && mv "$f.new" "$f"
}
set_role_password() {  # role password
  printf "SET log_statement = 'none'; SET log_min_error_statement = 'panic'; ALTER ROLE %s PASSWORD '%s';\n" "$1" "$2" \
    | pg_exec psql -X -q -d jarvis -v ON_ERROR_STOP=1
}

pw="$(rand)"
case "$what" in
  app)
    require_db
    set_role_password jarvis_app "$pw"
    rewrite "$SECRETS_DIR/app.env" "s#(postgresql://jarvis_app:)[^@]+@#\\1$pw@#"
    rewrite "$SECRETS_DIR/db.env" "s#^(JARVIS_APP_PASSWORD=).*#\\1$pw#"
    "${COMPOSE[@]}" up -d --force-recreate --no-deps app >/dev/null
    ;;
  migrator)
    require_db
    set_role_password jarvis_migrator "$pw"
    rewrite "$SECRETS_DIR/migrate.env" "s#(postgresql://jarvis_migrator:)[^@]+@#\\1$pw@#"
    rewrite "$SECRETS_DIR/db.env" "s#^(JARVIS_MIGRATOR_PASSWORD=).*#\\1$pw#"
    ;;
  api-key)
    rewrite "$SECRETS_DIR/app.env" "s#^(JARVIS_API_KEY=).*#\\1$pw#"
    printf '%s\n' "$pw" > "$SECRETS_DIR/api-key.new"; chmod 600 "$SECRETS_DIR/api-key.new"; mv "$SECRETS_DIR/api-key.new" "$SECRETS_DIR/api-key"
    "${COMPOSE[@]}" up -d --force-recreate --no-deps app >/dev/null
    ;;
  *) echo "usage: rotate-password.sh app|migrator|api-key" >&2; exit 2 ;;
esac
pw=""

if [ "$what" != "migrator" ]; then
  wait_healthy "$APP_CONTAINER" 120 || die "the app did not become healthy after rotating $what"
fi
log INFO "rotated $what"
[ "$what" != "api-key" ] || echo "Now copy secrets/api-key to the machines that run your hooks (JARVIS_API_KEY_FILE)."
