#!/usr/bin/env bash
# Install the backup/offsite/drill/watchdog timers as systemd USER units (so they run as you, can use docker
# and can raise desktop notifications). Linger keeps them running when you are logged out.
#
#   install-units.sh [--dest DIR] [--no-enable]      (--dest/--no-enable are for testing)
set -Eeuo pipefail
export LOG_NAME=install-units
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" enable=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dest) dest="$2"; shift ;;
    --no-enable) enable=0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$dest"
for unit in "$JARVIS_DEPLOY_DIR"/systemd/*.service "$JARVIS_DEPLOY_DIR"/systemd/*.timer; do
  sed -e "s#@DEPLOY_DIR@#$JARVIS_DEPLOY_DIR#g" -e "s#@JARVIS_HOME@#$JARVIS_HOME#g" "$unit" > "$dest/$(basename "$unit")"
done
echo "units written to $dest"

if [ "$enable" -eq 1 ]; then
  systemctl --user daemon-reload
  for t in backup offsite drill watchdog; do
    if [ "$t" = offsite ] && [ ! -f "$SECRETS_DIR/offsite.conf" ]; then
      echo "skipping jarvis-offsite.timer: no $SECRETS_DIR/offsite.conf yet (enable it later with: systemctl --user enable --now jarvis-offsite.timer)"
      continue
    fi
    systemctl --user enable --now "jarvis-$t.timer"
  done
  loginctl enable-linger "$USER" 2>/dev/null \
    || echo "could not enable linger; run: sudo loginctl enable-linger $USER   (so the timers run when you are logged out)"
  systemctl --user list-timers 'jarvis-*' --no-pager
fi
