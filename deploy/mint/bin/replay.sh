#!/usr/bin/env bash
# Replay Contracts (RC.Ledger.v1): rebuild the ledger as of a point in its history, take receipts at sealed points, and check them.
#
#   replay.sh state   [--at-seq N | --at-block H]            replay now (or at a point); prints the state root and counts
#   replay.sh receipt [--at-seq N | --at-block H]            store a receipt of the replay at a SEALED point (default: the end of
#                                                           the newest sealed block); the same replay always gives the same receipt
#   replay.sh receipts                                      list receipts
#   replay.sh check RECEIPT_ID                              ask the ledger to re-derive a receipt (fast, uses the running service)
#   replay.sh verify [--tenant T] [--receipt ID | --at-seq N | --at-block H] [--expect-root R] [--expect-block-hash H]
#                                                           replay from the RAW history rows in a one-off container (independent of
#                                                           the SQL the service uses); exit 0 only if everything agrees
# The operator key is read from secrets/api-key and never printed or put on a command line.
set -Eeuo pipefail
export LOG_NAME=replay
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[ $# -ge 1 ] || usage
cmd="$1"; shift

if [ "$cmd" = verify ]; then
  tenant=operator args=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --tenant) tenant="$2"; shift ;;
      --receipt|--at-seq|--at-block|--expect-root|--expect-block-hash) args+=("$1" "$2"); shift ;;
      *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
  done
  exec "${COMPOSE[@]}" run --rm --no-deps -T migrate python -m app.replay verify --tenant "$tenant" "${args[@]}"
fi

[ -s "$SECRETS_DIR/api-key" ] || die "no API key at $SECRETS_DIR/api-key"
base="http://127.0.0.1:$APP_PORT"
out="$(mktemp)"; trap 'rm -f "$out"' EXIT

call() {  # call METHOD PATH [BODY] : prints the HTTP status; the body is left in $out
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -m 120 -o "$out" -w '%{http_code}' -X "$method" -K -)
  [ -z "$body" ] || args+=(-H 'Content-Type: application/json' -d "$body")
  { printf 'header = "X-API-Key: %s"\n' "$(tr -d '\r\n' < "$SECRETS_DIR/api-key")"; } | curl "${args[@]}" "$base$path"
}

need_ok() {  # need_ok CODE WHAT : die with the ledger's own words unless CODE is 200
  [ "$1" = "200" ] && return 0
  local detail
  detail="$(python3 -c 'import json,sys
try:
    d = json.load(sys.stdin).get("detail", "")
    print(d if isinstance(d, str) else json.dumps(d))
except Exception:
    print("")' < "$out")"
  case "$1" in
    401) die "the ledger refused the API key (HTTP 401)" ;;
    501) die "the ledger is not on the PostgreSQL row store; there is nothing to replay (HTTP 501)" ;;
    404) [ -n "$detail" ] && [ "$detail" != "Not Found" ] && die "$2: $detail (HTTP 404)"
         die "the ledger has no replay endpoints: is the build with Replay Contracts deployed? (HTTP 404)" ;;
    *)   die "$2 failed: ${detail:-HTTP $1} (HTTP $1)" ;;
  esac
}

point_query() {  # --at-seq N | --at-block H  ->  query string / JSON body pieces
  at_seq="" at_block=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --at-seq) at_seq="$2"; shift ;;
      --at-block) at_block="$2"; shift ;;
      *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
  done
  [ -z "$at_seq" ] || [[ "$at_seq" =~ ^[0-9]+$ ]] || { echo "--at-seq needs a number" >&2; exit 2; }
  [ -z "$at_block" ] || [[ "$at_block" =~ ^[0-9]+$ ]] || { echo "--at-block needs a number" >&2; exit 2; }
  [ -z "$at_seq" ] || [ -z "$at_block" ] || { echo "give --at-seq or --at-block, not both" >&2; exit 2; }
}

case "$cmd" in
  state)
    point_query "$@"
    q="limit=1"; [ -z "$at_seq" ] || q="$q&at_seq=$at_seq"; [ -z "$at_block" ] || q="$q&at_block=$at_block"
    need_ok "$(call GET "/api/jarvis/replay/state?$q")" "replay"
    python3 -c '
import json, sys
s = json.load(sys.stdin)
b = s["block"]
where = ("sealed: block %d (%s...)%s" % (b["height"], b["block_hash"][:16], ", at its last entry" if s["at_block_boundary"] else "")) if b else "not covered by a sealed block"
print("%s as of seq %d of %d: %d record(s), %d deleted, state root %s; %s" % (s["contract"], s["at_seq"], s["history_seq"], s["record_count"], s["deleted_count"], s["state_root"], where))' < "$out" ;;
  receipt)
    point_query "$@"
    body="{}"; [ -z "$at_seq" ] || body="{\"at_seq\": $at_seq}"; [ -z "$at_block" ] || body="{\"at_block\": $at_block}"
    need_ok "$(call POST /api/jarvis/replay/receipts "$body")" "receipt"
    python3 -c '
import json, sys
r = json.load(sys.stdin)
s = r["state"]
print("receipt %s (%s) at seq %d, block %d, %d record(s), state root %s" % (r["receipt"]["id"], "created" if r["created"] else "already existed", s["at_seq"], s["block"]["height"], s["record_count"], s["state_root"]))' < "$out"
    log INFO "receipt issued: $(python3 -c 'import json,sys; print(json.load(sys.stdin)["receipt"]["id"])' < "$out")" ;;
  receipts)
    need_ok "$(call GET /api/jarvis/replay/receipts)" "list receipts"
    python3 -c '
import json, sys
d = json.load(sys.stdin)
if not d["receipts"]:
    print("no receipts yet")
for r in d["receipts"]:
    p = r["payload"]
    print("%s  seq %d  block %d  %d record(s)  root %s..." % (r["id"], p["at_seq"], p["block_height"], p["record_count"], p["state_root"][:16]))' < "$out" ;;
  check)
    [ $# -eq 1 ] || usage
    need_ok "$(call GET "/api/jarvis/replay/receipts/$1/verify")" "check"
    python3 -c '
import json, sys
v = json.load(sys.stdin)
for p in v["problems"]:
    print("PROBLEM [%s]: %s" % (p["check"], p["problem"]))
if v["ok"]:
    r = v["receipt"]
    print("ok: receipt %s re-derived: seq %d, %d record(s), state root %s, block %d" % (v["receipt_id"], r["at_seq"], r["record_count"], r["state_root"], r["block_height"]))
sys.exit(0 if v["ok"] else 1)' < "$out" ;;
  *) usage ;;
esac
