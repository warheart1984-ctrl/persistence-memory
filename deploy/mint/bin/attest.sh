#!/usr/bin/env bash
# Signatures: run the signer (on this box, as this user, never in a container), install the trust roots, look at the state.
#
#   attest.sh status                 the signing key's id and whether a root authorized it, pending blocks, where the log ends
#   attest.sh sign [--dry-run]       sign every sealed block that has no attestation, then one checkpoint
#   attest.sh init-key               create the signing key in $JARVIS_HOME/keys (mode 600 in a mode 700 directory); refuses to overwrite
#   attest.sh install-roots          give the service the repository's trust/roots.pub (public keys) and restart it
#   attest.sh verify                 ask the service to verify every attestation and trust statement
#
# The private signing key is read only by ssh-keygen, here. It must never be in a backup, a volume or a container (docs/SIGNING_RUNBOOK.md);
# the signer refuses to run if it is not kept that way. The operator key is read from secrets/api-key and never printed.
set -Eeuo pipefail
export LOG_NAME=attest
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[ $# -ge 1 ] || usage
cmd="$1"; shift

repo="$(cd "$JARVIS_DEPLOY_DIR/../.." && pwd)"
py="${JARVIS_SIGNER_PYTHON:-python3}"
export JARVIS_HOME JARVIS_APP_PORT="$APP_PORT" JARVIS_API_KEY_FILE="$SECRETS_DIR/api-key" JARVIS_BACKUP_DIR="$BACKUP_DIR"
signer() { ( cd "$repo" && "$py" -m app.signer "$@" ); }

case "$cmd" in
  status) signer status ;;
  init-key) signer init-key ;;
  sign)
    dry=0
    while [ $# -gt 0 ]; do case "$1" in --dry-run) dry=1 ;; *) echo "unknown argument: $1" >&2; exit 2 ;; esac; shift; done
    single_instance sign
    [ -s "$SECRETS_DIR/api-key" ] || die "no API key at $SECRETS_DIR/api-key"
    if [ "$dry" -eq 1 ]; then signer sign --dry-run; exit $?; fi
    if out="$(signer sign 2>&1)"; then
      log INFO "$out"
      date +%s > "$STATE_DIR/sign.last_ok"
      [ -f "$STATE_DIR/sign.first_ok" ] || cp "$STATE_DIR/sign.last_ok" "$STATE_DIR/sign.first_ok"
    else
      rc=$?
      die "signing failed (exit $rc): $out"
    fi ;;
  install-roots)
    src="$repo/trust/roots.pub"
    [ -f "$src" ] || die "no $src"
    grep -Eq '^ssh-ed25519 [A-Za-z0-9+/=]+' "$src" || die "$src lists no root key yet (the key ceremony has not been done; docs/SIGNING_RUNBOOK.md)"
    grep -Eq 'PRIVATE KEY' "$src" && die "$src contains private key material; refusing"
    mkdir -p "$SECRETS_DIR"
    install -m 644 "$src" "$SECRETS_DIR/trust-roots.pub"
    log INFO "installed $(grep -Ec '^ssh-ed25519 ' "$SECRETS_DIR/trust-roots.pub") root key(s) into $SECRETS_DIR/trust-roots.pub"
    "${COMPOSE[@]}" up -d --force-recreate --no-deps app >/dev/null
    wait_healthy "$APP_CONTAINER" 180 || die "the app did not become healthy after installing the roots"
    echo "trust roots installed; the service was restarted" ;;
  verify)
    out="$(mktemp)"; trap 'rm -f "$out"' EXIT
    code="$({ printf 'header = "X-API-Key: %s"\n' "$(tr -d '\r\n' < "$SECRETS_DIR/api-key")"; } | curl -sS -m 120 -o "$out" -w '%{http_code}' -K - "http://127.0.0.1:$APP_PORT/api/jarvis/attestations/verify")" \
      || die "could not reach the ledger"
    [ "$code" = "200" ] || die "verify failed: HTTP $code (is the build with signatures deployed? 404 means no)"
    python3 -c '
import json, sys
v = json.load(sys.stdin)
for p in v["problems"]:
    print("PROBLEM [%s] %s: %s" % (p["check"], p["subject"], p["problem"]))
for w in v["warnings"]:
    print("WARNING: " + (w if isinstance(w, str) else w["problem"]))
for n in v["notes"]:
    print(n)
sys.exit(0 if v["ok"] else 1)' < "$out" ;;
  *) usage ;;
esac
