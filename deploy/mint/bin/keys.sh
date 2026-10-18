#!/usr/bin/env bash
# Per-tenant API keys (RFC phase 1) from the box: create tenants and keys, revoke, suspend, list. A thin, validating wrapper over `python -m app.tenant_keys`, which runs in the
# migrate container as the migrator role (the application role has no privilege on the control schema, and the public API cannot create or list keys or tenants).
#
#   keys.sh pepper-init                          create secrets/key-pepper (random, mode 600); never overwrites; prints no value
#   keys.sh tenant-create --label NAME           a new tenant (prints its t_... id)
#   keys.sh key-create --tenant t_<32 hex> --out NAME [--label L] [--scopes memory.read,memory.write] [--expires-days N]
#                                                a new key; the TOKEN is written to secrets/issued-keys/NAME.token (mode 600, never overwritten) and is NEVER printed
#   keys.sh key-revoke KEY_ID                    revoke one key (16 hex characters)
#   keys.sh tenant-status --tenant t_<hex> --status active|suspended|quiesced|restoring|deleting|deleted
#   keys.sh tenants                              list tenants
#   keys.sh keys [--tenant t_<hex>]              list keys (never a hash)
#   keys.sh status                               pepper present? is JARVIS_KEY_TENANTS set in the app env? how many tenants and keys
#
# Issuing a key does NOT turn key authentication on: the app only honours keys with JARVIS_KEY_TENANTS=1 and a pepper mounted for it (docs/tenant_keys.md). Every argument is
# validated before Docker is touched; a rejected combination exits 2. The pepper and the tokens are never printed, logged or put on a command line.
set -Eeuo pipefail
export LOG_NAME=keys
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
bad() { echo "$*" >&2; exit 2; }
[ $# -ge 1 ] || usage
cmd="$1"; shift

PEPPER_FILE="$SECRETS_DIR/key-pepper"
KEYS_DIR="$SECRETS_DIR/issued-keys"
TENANT_RE='^t_[0-9a-f]{32}$'
KEYID_RE='^[0-9a-f]{16}$'
STATUSES=" active suspended quiesced restoring deleting deleted "

tenant="" label="" scopes="" expires="" out="" status="" key_id="" have_label=0
parse_opts() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --tenant)       [ $# -ge 2 ] || bad "--tenant needs a value"; tenant="$2"; shift ;;
      --label)        [ $# -ge 2 ] || bad "--label needs a value"; label="$2"; have_label=1; shift ;;
      --scopes)       [ $# -ge 2 ] || bad "--scopes needs a value"; scopes="$2"; shift ;;
      --expires-days) [ $# -ge 2 ] || bad "--expires-days needs a value"; expires="$2"; shift ;;
      --out)          [ $# -ge 2 ] || bad "--out needs a value"; out="$2"; shift ;;
      --status)       [ $# -ge 2 ] || bad "--status needs a value"; status="$2"; shift ;;
      *) bad "unknown argument: $1" ;;
    esac
    shift
  done
}
need_tenant() { [[ "$tenant" =~ $TENANT_RE ]] || bad "--tenant must be t_ followed by 32 lowercase hex characters"; }
no_label_newline() { case "$label" in *$'\n'*|*$'\r'*) bad "--label must be a single line" ;; esac; [ "${#label}" -le 80 ] || bad "--label is at most 80 characters"; }

# the command inside the migrate container, as the host user so a token file belongs to the operator and not to uid 10001; the pepper is a read-only bind mount, never an env value
tk() {
  "${COMPOSE[@]}" run --rm --no-deps -T --user "$(id -u):$(id -g)" \
    -v "$PEPPER_FILE:/run/key-pepper:ro" -v "$KEYS_DIR:/keys" -e JARVIS_KEY_PEPPER_FILE=/run/key-pepper \
    migrate python -m app.tenant_keys "$@"
}
need_pepper() { [ -f "$PEPPER_FILE" ] || die "no pepper yet: run  jarvisctl keys pepper-init  first"; [ "$(stat -c %a "$PEPPER_FILE")" = 600 ] || die "$PEPPER_FILE must be mode 600"; mkdir -p "$KEYS_DIR"; chmod 700 "$KEYS_DIR"; }

case "$cmd" in
  pepper-init)
    parse_opts "$@"; [ -z "$tenant$label$scopes$expires$out$status" ] || bad "pepper-init takes no options"
    [ ! -e "$PEPPER_FILE" ] || die "$PEPPER_FILE already exists: keys issued under it would stop verifying if it changed, so it is never overwritten"
    mkdir -p "$SECRETS_DIR"
    ( umask 077; head -c 48 /dev/urandom | base64 -w0 > "$PEPPER_FILE.tmp.$$" && echo >> "$PEPPER_FILE.tmp.$$" && mv -n "$PEPPER_FILE.tmp.$$" "$PEPPER_FILE" )
    rm -f "$PEPPER_FILE.tmp.$$"
    chmod 600 "$PEPPER_FILE"
    log INFO "pepper written to $PEPPER_FILE (mode 600, $(head -c -1 "$PEPPER_FILE" | wc -c) characters; the value is not shown). Back it up with the secrets: keys issued under it verify only with it."
    ;;
  tenant-create)
    parse_opts "$@"; [ -z "$tenant$scopes$expires$out$status" ] || bad "tenant-create takes only --label"
    [ "$have_label" -eq 1 ] && [ -n "$label" ] || bad "tenant-create needs --label NAME"
    no_label_newline; need_pepper
    tk create-tenant --label "$label"
    ;;
  key-create)
    parse_opts "$@"; [ -z "$status" ] || bad "key-create has no --status"
    need_tenant; no_label_newline
    [[ "$out" =~ ^[A-Za-z0-9._-]{1,64}$ ]] && [ "$out" != . ] && [ "$out" != .. ] || bad "--out NAME is required (letters, digits, . _ - only; the token is never printed)"
    if [ -n "$scopes" ]; then [[ "$scopes" =~ ^(memory\.read|memory\.write)(,(memory\.read|memory\.write))*$ ]] || bad "--scopes must be memory.read and/or memory.write, comma-separated"; fi
    if [ -n "$expires" ]; then [[ "$expires" =~ ^[0-9]+(\.[0-9]+)?$ ]] || bad "--expires-days must be a non-negative number (0 expires the key now)"; fi
    need_pepper
    [ ! -e "$KEYS_DIR/$out.token" ] || die "$KEYS_DIR/$out.token already exists: a token file is never overwritten"
    args=(create-key --tenant "$tenant" --token-file "/keys/$out.token")
    [ "$have_label" -eq 0 ] || args+=(--label "$label")
    [ -z "$scopes" ] || args+=(--scopes "$scopes")
    [ -z "$expires" ] || args+=(--expires-days "$expires")
    tk "${args[@]}"
    ;;
  key-revoke)
    [ $# -eq 1 ] || bad "key-revoke takes exactly one KEY_ID"
    [[ "$1" =~ $KEYID_RE ]] || bad "KEY_ID must be 16 lowercase hex characters (the part after jlk_, never the whole token)"
    need_pepper; tk revoke-key "$1"
    ;;
  tenant-status)
    parse_opts "$@"; [ -z "$label$scopes$expires$out" ] || bad "tenant-status takes only --tenant and --status"
    need_tenant
    case "$STATUSES" in *" $status "*) ;; *) bad "--status must be one of:${STATUSES}" ;; esac
    need_pepper; tk set-status --tenant "$tenant" --status "$status"
    ;;
  tenants)
    [ $# -eq 0 ] || bad "tenants takes no options"
    need_pepper; tk list-tenants
    ;;
  keys)
    parse_opts "$@"; [ -z "$label$scopes$expires$out$status" ] || bad "keys takes only --tenant"
    [ -z "$tenant" ] || need_tenant
    need_pepper
    if [ -n "$tenant" ]; then tk list-keys --tenant "$tenant"; else tk list-keys; fi
    ;;
  status)
    [ $# -eq 0 ] || bad "status takes no options"
    if [ -f "$PEPPER_FILE" ]; then echo "pepper: present ($(stat -c %a "$PEPPER_FILE"))"; else echo "pepper: absent (jarvisctl keys pepper-init)"; fi
    # as the app reads it: the LAST assignment wins (compose env_file); a quoted value is what is inside the quotes (anything after the closing quote is a comment); an unquoted value ends
    # at a space-prefixed "#" (compose inline comment); key_tenants_enabled() lowercases (1/true/yes/on)
    flag="$(grep -E '^[[:space:]]*JARVIS_KEY_TENANTS[[:space:]]*=' "$SECRETS_DIR/app.env" 2>/dev/null | tail -1 | sed -E 's/^[^=]*=//; s/^[[:space:]]+//; /^"/{s/^"([^"]*)".*$/\1/;b done}; /^'"'"'/{s/^'"'"'([^'"'"']*)'"'"'.*$/\1/;b done}; s/[[:space:]]+#.*$//; :done; s/^[[:space:]]+//; s/[[:space:]]+$//' | tr '[:upper:]' '[:lower:]' || true)"
    case "$flag" in 1|true|yes|on) echo "key authentication: ON in secrets/app.env" ;; *) echo "key authentication: off (JARVIS_KEY_TENANTS not enabled in secrets/app.env)" ;; esac
    if [ -f "$PEPPER_FILE" ]; then
      # a failed query is reported as unavailable (and fails the command), never as an empty deployment
      rc=0
      if t="$(tk list-tenants 2>/dev/null)"; then echo "tenants: $(printf '%s\n' "$t" | grep -c '^t_' || true)"; else echo "tenants: unavailable (the control schema could not be queried)"; rc=1; fi
      if k="$(tk list-keys 2>/dev/null)"; then echo "keys:    $(printf '%s\n' "$k" | grep -c '^[0-9a-f]\{16\}' || true)"; else echo "keys:    unavailable (the control schema could not be queried)"; rc=1; fi
      exit "$rc"
    fi
    ;;
  *) usage ;;
esac
