#!/usr/bin/env bash
# Seal the unsealed ledger history into Continuity Blocks: POST /api/jarvis/blocks/seal with the operator key.
#
#   seal.sh [--force] [--status]
#
# The ledger decides what is due: a block is sealed when 500 history entries are waiting or the oldest waiting entry is
# an hour old (--force seals whatever is waiting). Sealing never changes a record, never blocks writers, and does nothing
# when there is nothing new. --status only prints the newest block and the size of the unsealed tail.
# The key is read from secrets/api-key and never printed or put on a command line.
set -Eeuo pipefail
export LOG_NAME=seal
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

force=false status=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=true ;;
    --status) status=1 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -s "$SECRETS_DIR/api-key" ] || die "no API key at $SECRETS_DIR/api-key"
base="http://127.0.0.1:$APP_PORT"
out="$(mktemp)"; trap 'rm -f "$out"' EXIT

# call METHOD PATH [BODY] : prints the HTTP status; the response body is left in $out
call() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -m 120 -o "$out" -w '%{http_code}' -X "$method" -K -)
  [ -z "$body" ] || args+=(-H 'Content-Type: application/json' -d "$body")
  { printf 'header = "X-API-Key: %s"\n' "$(tr -d '\r\n' < "$SECRETS_DIR/api-key")"; } | curl "${args[@]}" "$base$path"
}

summary() {  # one line from a head object on stdin
  python3 -c '
import json, sys
h = json.load(sys.stdin)
if "head" in h:
    h = h["head"]
tip = h.get("tip")
print("newest block: %s, sealed through seq %s, %s entries unsealed (history at seq %s)" % (
    tip["height"] if tip else "none", h["sealed_seq"], h["unsealed_entries"], h["history_seq"]))'
}

if [ "$status" -eq 1 ]; then
  code="$(call GET /api/jarvis/blocks/head)" || die "could not reach the ledger at $base"
  [ "$code" = "200" ] || die "GET /api/jarvis/blocks/head returned HTTP $code"
  summary < "$out"
  exit 0
fi

code="$(call POST /api/jarvis/blocks/seal "{\"force\": $force}")" || die "could not reach the ledger at $base"
case "$code" in
  200) ;;
  401) die "the ledger refused the API key (HTTP 401)" ;;
  404) die "the ledger has no block endpoints: is it at schema v6 (jarvisctl up)? (HTTP 404)" ;;
  501) die "the ledger is not on the PostgreSQL row store; there are no blocks to seal (HTTP 501)" ;;
  *)   die "sealing failed: HTTP $code (see: jarvisctl logs app)" ;;
esac

sealed="$(python3 -c '
import json, sys
r = json.load(sys.stdin)
print(len(r["sealed"]), ",".join(str(b["height"]) for b in r["sealed"]))' < "$out")"
n="${sealed%% *}"; heights="${sealed#* }"
if [ "$n" -gt 0 ]; then log INFO "sealed $n block(s) (height $heights); $(summary < "$out")"
else log INFO "nothing sealed: $(python3 -c 'import json,sys; print(json.load(sys.stdin)["reason"])' < "$out"); $(summary < "$out")"; fi
date +%s > "$STATE_DIR/seal.last_ok"
