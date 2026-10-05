#!/usr/bin/env bash
# Windows-side driver: rehearse the whole Mint deployment inside a WSL Ubuntu 24.04 distro with ITS OWN Docker Engine
# (not Docker Desktop) and real systemd, BEFORE the real box is touched. Run from Git Bash:
#
#   deploy/mint/rehearse/rehearse-wsl.sh
#
# Phase A  : build, run and exercise everything, then take a snapshot of the ledger.
# Reboot   : `wsl --terminate <distro>` - only this distro dies (systemd, dockerd, every container, hard); it is then
#            started again. Docker Desktop and its containers are NOT touched. Nothing is started by hand afterwards.
# Phase B  : the stack, the data and the timers must have come back by themselves; then rotation, a deliberate
#            DESTROY of every volume, RESTORE from backup, and a final audit.
# Everything lives under ~/jarvis-rehearsal in the distro and is removed at the end (KEEP=1 keeps it).
set -uo pipefail
export MSYS_NO_PATHCONV=1

DISTRO="${DISTRO:-Ubuntu-24.04}"
USER_NAME="${REHEARSAL_USER:-randj}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_win="$(cd "$here/../../.." && pwd -W 2>/dev/null || pwd)"      # e.g. G:/persistence-memory
repo_wsl="$(wsl -d "$DISTRO" -u root -- wslpath -u "$repo_win" | tr -d '\r\0')"
scen="$repo_wsl/deploy/mint/rehearse/scenario.sh"
prep="$repo_wsl/deploy/mint/rehearse/wsl-root-prep.sh"

as_user() { wsl -d "$DISTRO" -u "$USER_NAME" --cd "~" -- env SRC_FROM="$repo_wsl" bash "$scen" "$@"; }
as_root() { wsl -d "$DISTRO" -u root -- env REHEARSAL_USER="$USER_NAME" bash "$@"; }

echo "distro: $DISTRO | repo in WSL: $repo_wsl"
rc_total=0
cleanup() {
  if [ "${KEEP:-0}" != 1 ]; then
    as_user teardown >/dev/null 2>&1
    as_root "$prep" restore >/dev/null 2>&1
  fi
}
trap cleanup EXIT

echo "== prep (as root): docker, linger, user manager"
as_root "$prep" prep || { echo "prep failed"; exit 2; }

echo; echo "################ PHASE A ################"
as_user phase-a; rc=$?; [ "$rc" -eq 0 ] || rc_total=1
echo "phase A exit: $rc"

echo; echo "################ REBOOT (wsl --terminate $DISTRO) ################"
wsl --terminate "$DISTRO"
sleep 5
echo "distro state after terminate: $(wsl -l -v | tr -d '\0\r' | awk -v d="$DISTRO" '$0 ~ d {print $(NF-1)}')"
echo "booting it again (systemd starts docker and the restart-policy containers on its own)..."
wsl -d "$DISTRO" -u root -- true
for _ in $(seq 1 60); do
  state="$(wsl -d "$DISTRO" -u root -- systemctl is-system-running 2>/dev/null | tr -d '\r\0')"
  case "$state" in running|degraded) break ;; esac
  sleep 3
done
echo "systemd: $state"

echo; echo "################ PHASE B ################"
as_user phase-b; rc=$?; [ "$rc" -eq 0 ] || rc_total=1
echo "phase B exit: $rc"

echo
[ "$rc_total" -eq 0 ] && echo "REHEARSAL PASSED" || echo "REHEARSAL FAILED"
exit "$rc_total"
