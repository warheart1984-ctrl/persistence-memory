#!/usr/bin/env bash
# Daily offsite copy: the newest complete backup set, encrypted with age and sent to your Windows PC over scp.
#
#  * Only the age PUBLIC key lives on this box (secrets/age_recipient.txt). The private key stays offline with
#    you (USB + printed copy); nothing here can decrypt a bundle, so a stolen backup directory stays unreadable.
#  * The plaintext bundle is never written to disk: tar | age -> file.
#  * The remote host key is pinned (secrets/offsite_known_hosts, StrictHostKeyChecking=yes) and the upload is
#    verified by hashing it again on the far side.
#  * Configuration: secrets/offsite.conf (copy offsite.conf.example).
set -Eeuo pipefail
export LOG_NAME=offsite
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
umask 077

single_instance offsite
tmp="$STATE_DIR/offsite-tmp"
cleanup() { rc=$?; rm -rf "$tmp"; [ "$rc" -eq 0 ] || [ "${NOTIFIED:-0}" = 1 ] || notify "Jarvis ledger: offsite copy FAILED" "see $LOG_DIR/offsite.log" critical; }
trap cleanup EXIT
rm -rf "$tmp"; mkdir -p "$tmp"

conf="$SECRETS_DIR/offsite.conf"
[ -f "$conf" ] || die "missing $conf (copy deploy/mint/offsite.conf.example there and fill it in)"
OFFSITE_PORT=22 OFFSITE_OS=linux
# shellcheck disable=SC1090
source "$conf"
: "${OFFSITE_HOST:?}" "${OFFSITE_USER:?}" "${OFFSITE_PATH:?}" "${OFFSITE_KEY:?}"
command -v age >/dev/null 2>&1 || die "age is not installed (sudo apt install age)"
recipient="$(grep -E '^age1[a-z0-9]+$' "$SECRETS_DIR/age_recipient.txt" 2>/dev/null | head -1 || true)"
[ -n "$recipient" ] || die "no age public key (a line starting with age1) in $SECRETS_DIR/age_recipient.txt"
[ -f "$SECRETS_DIR/offsite_known_hosts" ] || die "missing $SECRETS_DIR/offsite_known_hosts (pin the remote host key first; see the README)"

base="$(ls -1 "$BACKUP_DIR" | sed -n 's/^\(jarvis-[0-9]\{8\}T[0-9]\{6\}Z\)\.sha256$/\1/p' | LC_ALL=C sort | tail -1)"
[ -n "$base" ] || die "no complete backup set to send"
if grep -qx "$base" "$STATE_DIR/offsite.sent" 2>/dev/null; then
  log INFO "$base was already sent; nothing new"; date +%s > "$STATE_DIR/offsite.last_ok"; exit 0
fi

bundle="$base.bundle.tar.age"
sig_part=(); [ ! -f "$BACKUP_DIR/$base.signatures.json" ] || sig_part=("$base.signatures.json")  # sets from before schema v7 have none
( cd "$BACKUP_DIR" && tar -cf - "$base.dump" "$base.globals.sql" "$base.data.tar" "$base.counts" "$base.anchors" "${sig_part[@]}" "$base.sha256" anchors ) \
  | age -r "$recipient" -o "$tmp/$bundle" || die "could not build the encrypted bundle"
local_sha="$(sha256sum "$tmp/$bundle" | cut -d' ' -f1)"

ssh_opts=(-i "$OFFSITE_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$SECRETS_DIR/offsite_known_hosts" -o ConnectTimeout=15 -o ServerAliveInterval=15)
remote="$OFFSITE_USER@$OFFSITE_HOST"
scp -q -P "$OFFSITE_PORT" "${ssh_opts[@]}" "$tmp/$bundle" "$remote:$OFFSITE_PATH/$bundle" \
  || die "scp to $OFFSITE_HOST failed (is the PC on? is its SSH server up?)"

if [ "$OFFSITE_OS" = "windows" ]; then
  remote_sha="$(ssh -p "$OFFSITE_PORT" "${ssh_opts[@]}" "$remote" \
    "powershell -NoProfile -Command \"(Get-FileHash -Algorithm SHA256 -LiteralPath '$OFFSITE_PATH/$bundle').Hash.ToLower()\"" | tr -d '\r\n ')" \
    || die "could not hash the uploaded bundle on $OFFSITE_HOST"
else
  remote_sha="$(ssh -p "$OFFSITE_PORT" "${ssh_opts[@]}" "$remote" "sha256sum '$OFFSITE_PATH/$bundle'" | cut -d' ' -f1)" \
    || die "could not hash the uploaded bundle on $OFFSITE_HOST"
fi
[ "$remote_sha" = "$local_sha" ] || die "uploaded bundle hash differs from the local one (upload corrupted); keeping the set unsent"

echo "$base" >> "$STATE_DIR/offsite.sent"
date +%s > "$STATE_DIR/offsite.last_ok"
log INFO "offsite ok: $bundle ($(du -k "$tmp/$bundle" | cut -f1) KB, sha256 verified on $OFFSITE_HOST)"
