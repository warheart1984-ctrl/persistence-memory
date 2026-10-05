#!/usr/bin/env bash
# Run as ROOT inside the WSL distro by rehearse-wsl.sh (via `wsl -u root`).
#   prep     make sure Docker is up, give the user a systemd user manager that survives logout (linger) and that has
#            the docker group, so user timers can run docker like they will on the Mint box
#   restore  undo the linger setting (the rehearsal's only system-level change besides the packages you approved)
set -euo pipefail
user="${REHEARSAL_USER:-randj}"
case "${1:-}" in
  prep)
    systemctl is-active --quiet docker || systemctl start docker
    loginctl enable-linger "$user"
    # the manager must be (re)started AFTER the user joined the docker group to inherit it
    systemctl restart "user@$(id -u "$user").service"
    for _ in $(seq 1 30); do [ -S "/run/user/$(id -u "$user")/bus" ] && break; sleep 1; done
    echo "docker: $(systemctl is-active docker) | linger: $(loginctl show-user "$user" -p Linger --value) | user bus: $(ls "/run/user/$(id -u "$user")/bus")"
    ;;
  restore)
    loginctl disable-linger "$user" || true
    echo "linger disabled for $user"
    ;;
  *) echo "usage: wsl-root-prep.sh prep|restore" >&2; exit 2 ;;
esac
