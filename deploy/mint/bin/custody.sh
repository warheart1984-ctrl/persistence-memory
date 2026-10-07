#!/usr/bin/env bash
# Key custody scan: the private signing key must never be in a backup set.
#
# Two different questions, answered differently on purpose:
#
#  * EXACT: does the set contain THIS box's signing key? (markers derived from the key's private seed, in every base64 alignment and hex; see
#    keymarkers.py). That is never legitimate anywhere, in a file or in the database, so it always stops the backup. The markers go to grep
#    through a mode-600 temporary file, never on a command line (so `ps` cannot show them), and are never printed.
#  * GENERIC: does any text look like the start of a private key (an OpenSSH, RSA, EC, DSA or PKCS#8 BEGIN line)? In FILES (the appdata
#    archive, the globals, the exports) that means someone dropped a key where it does not belong, so it stops the backup. In the
#    DATABASE it can be a harmless note ABOUT keys, and the history is append-only: refusing would block every future backup for good
#    and nothing could be cleaned. There it is only a warning.
# shellcheck shell=bash

# custody_key_markers : markers (one per line) that identify THIS box's signing key's secret in any text, or nothing if there is no readable
# key file. Derived from the private seed itself (keymarkers.py), so public keys, signatures and the header lines every OpenSSH key shares
# never match. Meant to be read through a pipe or file descriptor, never shown.
custody_key_markers() {
  local f="${JARVIS_SIGN_KEY:-${JARVIS_HOME:-$HOME/jarvis-ledger}/keys/jarvis-sign-ed25519}"
  [ -r "$f" ] || return 0
  python3 "$(dirname "${BASH_SOURCE[0]}")/keymarkers.py" "$f" 2>/dev/null || true
}

# custody_hits : stdin -> how many lines look like the start of a private key. Never prints the text.
custody_hits() {
  grep -aEc -e '-----BEGIN ([A-Z0-9]+ )*PRIVATE KEY-----' || true
}

# custody_exact_hits : stdin -> how many lines contain this box's signing key (0 when there is no key file). Never prints the text.
custody_exact_hits() {
  local markers
  markers="$(mktemp)"
  chmod 600 "$markers"
  custody_key_markers > "$markers"
  if [ ! -s "$markers" ]; then rm -f "$markers"; cat > /dev/null; echo 0; return 0; fi
  grep -aFc -f "$markers" || true
  rm -f "$markers"
}

# custody_check_set DIR BASE : the plain parts and the appdata archive of a set. Prints what is wrong and returns 1, else nothing and 0.
custody_check_set() {
  local dir="$1" base="$2" part n bad=0
  for part in globals.sql counts anchors signatures.json; do
    [ -f "$dir/$base.$part" ] || continue
    n="$(custody_hits < "$dir/$base.$part")"
    [ "${n:-0}" = "0" ] || { echo "$base.$part contains $n private-key header line(s)"; bad=1; }
    n="$(custody_exact_hits < "$dir/$base.$part")"
    [ "${n:-0}" = "0" ] || { echo "$base.$part contains the signing key"; bad=1; }
  done
  if [ -f "$dir/$base.data.tar" ]; then
    n="$(tar -xOf "$dir/$base.data.tar" 2>/dev/null | custody_hits)"
    [ "${n:-0}" = "0" ] || { echo "$base.data.tar contains $n private-key header line(s)"; bad=1; }
    n="$(tar -xOf "$dir/$base.data.tar" 2>/dev/null | custody_exact_hits)"
    [ "${n:-0}" = "0" ] || { echo "$base.data.tar contains the signing key"; bad=1; }
  fi
  return "$bad"
}

# custody_check_data FILE : the database's data as text. Returns 1 (and says so) only if it holds the signing key itself; a key-shaped
# header is reported as a WARN line on stdout prefixed "WARN:" and does not fail.
custody_check_data() {
  local f="$1" n
  n="$(custody_exact_hits < "$f")"
  [ "${n:-0}" = "0" ] || { echo "the database holds the signing key ($n line(s))"; return 1; }
  n="$(custody_hits < "$f")"
  [ "${n:-0}" = "0" ] || echo "WARN: the database holds $n line(s) that look like a private-key header (a note about keys, or a pasted key: check; the backup goes ahead because the history cannot be edited)"
  return 0
}
