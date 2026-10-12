#!/usr/bin/env bash
# Per-tenant jobs: seal, sign and check EACH tenant's own chain, from the box. NEW: the timers, seal.sh and attest.sh are unchanged and keep working on the
# operator tenant exactly as before; this is a separate command for when there is more than one tenant.
#
#   tenants.sh list                                   the tenants that have history (listed with the superuser path, as the backups do)
#   tenants.sh seal   [--force] [--tenants a,b]       seal each tenant's unsealed history
#   tenants.sh sign   [--dry-run] [--tenants a,b]     sign each tenant's pending blocks (the key must have been authorized in THAT tenant's trust log)
#   tenants.sh status [--tenants a,b]                 each tenant's signing state
#   tenants.sh verify [--tenants a,b]                 verify each tenant's history chain, blocks and signatures
#
# Rules: one tenant's failure never stops the others, and every failure is logged, listed at the end, and makes the exit code non-zero. With only the
# operator tenant the output is what seal.sh / attest.sh print today. It writes its own state files (tenants-seal.last_ok, tenants-sign.last_ok), never the
# ones the watchdog reads. The public API cannot select a tenant; the tenant is bound by the process this script starts (app.tenant_jobs).
set -Eeuo pipefail
export LOG_NAME=tenants
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[ $# -ge 1 ] || usage
cmd="$1"; shift

repo="$(cd "$JARVIS_DEPLOY_DIR/../.." && pwd)"
py="${JARVIS_SIGNER_PYTHON:-python3}"

list_tenants() {
  require_db
  pg_exec psql -X -At -d jarvis -c "SELECT t FROM (SELECT tenant_key AS t FROM jarvis.chain_heads UNION SELECT tenant_key FROM jarvis.history_counters) x ORDER BY (t <> 'operator'), t"
}

force=0 dry=0 tenants="" tenants_given=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=1 ;;
    --dry-run) dry=1 ;;
    --tenants) tenants="${2:-}"; tenants_given=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
# `--tenants ''` is a selection of nobody, not an omitted option: it must never fall back to "every tenant"
# An explicit list is normalized first: names trimmed, blanks dropped, duplicates removed (" t_bob" is not "t_bob": pg_verify treats a tenant with no rows as an
# intact empty ledger, so an untrimmed name would be reported verified while the real tenant was never checked).
if [ "$tenants_given" -eq 1 ]; then
  tenants="$(printf '%s' "$tenants" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | awk 'NF && !seen[$0]++' | paste -sd, -)"
  if [ -z "$tenants" ]; then echo "--tenants was given but names no tenant" >&2; exit 2; fi
fi
# a flag that does not apply to the command is an error, never silently ignored (`seal --dry-run` must not seal; `sign --force` must not sign)
case "$cmd" in
  list)   [ "$force" -eq 0 ] && [ "$dry" -eq 0 ] && [ -z "$tenants" ] || { echo "list takes no options" >&2; exit 2; } ;;
  seal)   [ "$dry" -eq 0 ] || { echo "seal has no --dry-run (it seals; use --force only to seal below the block minimum)" >&2; exit 2; } ;;
  sign)   [ "$force" -eq 0 ] || { echo "sign has no --force" >&2; exit 2; } ;;
  status|verify) [ "$force" -eq 0 ] && [ "$dry" -eq 0 ] || { echo "$cmd takes only --tenants" >&2; exit 2; } ;;
  *) usage ;;
esac
[ "$cmd" = list ] || [ -n "$tenants" ] || tenants="$(list_tenants | paste -sd, -)"

[ "$cmd" = list ] || [ -n "$tenants" ] || die "no tenants found (nothing to $cmd)"

case "$cmd" in
  list) list_tenants ;;
  seal)
    [ -n "$tenants" ] || die "no tenants found"
    single_instance tenants-seal
    args=(--tenants "$tenants"); [ "$force" -eq 0 ] || args+=(--force)
    rc=0; out="$("${COMPOSE[@]}" run --rm --no-deps -T migrate python -m app.tenant_jobs seal "${args[@]}" 2>/dev/null)" || rc=$?
    [ -n "$out" ] || die "the per-tenant seal printed nothing (exit $rc): is the migrate service available? (jarvisctl logs migrate)"
    while IFS= read -r line; do
      case "$line" in ERROR*) log ERROR "${line#ERROR }" ;; *) log INFO "$line" ;; esac
    done <<< "$out"
    if [ "$rc" -ne 0 ]; then
      notify "Jarvis ledger: per-tenant seal FAILED for some tenants" "$(printf '%s\n' "$out" | grep -E '^ERROR tenants that FAILED' | head -1)" critical
      exit "$rc"
    fi
    date +%s > "$STATE_DIR/tenants-seal.last_ok" ;;
  sign)
    single_instance tenants-sign
    sargs=(sign --tenants "$tenants"); [ "$dry" -eq 0 ] || sargs+=(--dry-run)
    export JARVIS_HOME JARVIS_BACKUP_DIR="$BACKUP_DIR"
    rc=0; out="$(cd "$repo" && "$py" -m app.signer "${sargs[@]}" 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
      while IFS= read -r line; do log INFO "$line"; done <<< "$out"
      [ "$dry" -eq 1 ] || date +%s > "$STATE_DIR/tenants-sign.last_ok"
    else
      while IFS= read -r line; do log ERROR "$line"; done <<< "$out"
      notify "Jarvis ledger: per-tenant signing FAILED for some tenants" "$(printf '%s\n' "$out" | grep -E '^FAILED tenants' | head -1)" critical
      exit "$rc"
    fi ;;
  status)
    export JARVIS_HOME
    (cd "$repo" && "$py" -m app.signer status --tenants "$tenants") ;;
  verify)
    fail=()
    IFS=, read -ra list <<< "$tenants"
    for t in "${list[@]}"; do
      if out="$("${COMPOSE[@]}" run --rm --no-deps -T migrate python -m app.pg_verify --tenant "$t" 2>&1)"; then
        log INFO "tenant $t: $(printf '%s\n' "$out" | tail -1)"
      else
        log ERROR "tenant $t: verification FAILED: $(printf '%s\n' "$out" | head -3 | tr '\n' ';')"; fail+=("$t")
      fi
    done
    if [ "${#fail[@]}" -gt 0 ]; then
      log ERROR "tenants that FAILED verification: $(IFS=,; echo "${fail[*]}") ($(( ${#list[@]} - ${#fail[@]} )) of ${#list[@]} ok)"
      exit 1
    fi
    [ "${#list[@]}" -le 1 ] || log INFO "all ${#list[@]} tenants verify" ;;
  *) usage ;;
esac
