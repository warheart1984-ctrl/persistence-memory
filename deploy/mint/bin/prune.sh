#!/usr/bin/env bash
# Retention for backup sets (never touches the anchors log or quarantine/):
#   keep the newest 48 sets, plus the newest set of each of the last 14 UTC days,
#   plus the newest set of each of the last 12 ISO weeks. Everything else is deleted.
#
#   prune.sh [--dry-run] [--now YYYYmmddTHHMMSSZ]
set -Eeuo pipefail
export LOG_NAME=${LOG_NAME:-prune}
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

KEEP_RECENT="${KEEP_RECENT:-48}" KEEP_DAYS="${KEEP_DAYS:-14}" KEEP_WEEKS="${KEEP_WEEKS:-12}"
dry=0; now_stamp=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1 ;;
    --now) now_stamp="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

to_epoch() {  # 20261005T010203Z -> epoch
  local s="$1"
  date -u -d "${s:0:4}-${s:4:2}-${s:6:2} ${s:9:2}:${s:11:2}:${s:13:2}" +%s
}
now_epoch="$( [ -n "$now_stamp" ] && to_epoch "$now_stamp" || date +%s )"

# complete sets only (the .sha256 file is written last), newest first
mapfile -t sets < <(ls -1 "$BACKUP_DIR" 2>/dev/null | sed -n 's/^\(jarvis-[0-9]\{8\}T[0-9]\{6\}Z\)\.sha256$/\1/p' | LC_ALL=C sort -r)
[ "${#sets[@]}" -gt 0 ] || exit 0

declare -A keep=() seen_day=() seen_week=()
i=0
for base in "${sets[@]}"; do
  stamp="${base#jarvis-}"; epoch="$(to_epoch "$stamp")"; age_days=$(( (now_epoch - epoch) / 86400 ))
  day="${stamp:0:8}"; week="$(date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2}" +%G-%V)"
  [ "$i" -lt "$KEEP_RECENT" ] && keep[$base]=1
  if [ "$age_days" -lt "$KEEP_DAYS" ] && [ -z "${seen_day[$day]:-}" ]; then keep[$base]=1; fi
  if [ "$age_days" -lt $(( KEEP_WEEKS * 7 )) ] && [ -z "${seen_week[$week]:-}" ]; then keep[$base]=1; fi
  seen_day[$day]=1; seen_week[$week]=1
  i=$((i + 1))
done

removed=0
for base in "${sets[@]}"; do
  [ -n "${keep[$base]:-}" ] && continue
  if [ "$dry" -eq 1 ]; then echo "would delete $base"; else
    rm -f "$BACKUP_DIR/$base".dump "$BACKUP_DIR/$base".globals.sql "$BACKUP_DIR/$base".data.tar \
          "$BACKUP_DIR/$base".counts "$BACKUP_DIR/$base".anchors "$BACKUP_DIR/$base".sha256
  fi
  removed=$((removed + 1))
done
[ "$dry" -eq 1 ] || { [ "$removed" -eq 0 ] || log INFO "pruned $removed old backup set(s); ${#sets[@]} -> $(( ${#sets[@]} - removed ))"; }
