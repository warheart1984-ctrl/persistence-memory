#!/usr/bin/env python3
"""Print markers that identify THIS signing key's secret in any text (one per line), for the backup's custody scan.

The secret is the 32-byte Ed25519 seed inside the OpenSSH private key file.  A pasted copy of the key can show it in several forms: the
whole PEM-style file (base64 wrapped at 70 columns, in any alignment), the seed alone in base64, or in hex.  So the markers are the
base64 of the seed at each of the three byte alignments (leaving out the characters that depend on neighbouring bytes), and the hex,
each cut in two halves (a line break can split one half, never both).  Nothing here is a public value: a public key, a signature or the
constant header lines every OpenSSH key shares never match.

Usage:  keymarkers.py KEYFILE      (prints nothing and exits 0 if there is no readable key; exits 3 after printing the fallback markers
if the file cannot be parsed).  Standard library only.  The output is meant to be read through a pipe by ``grep -F -f`` and never shown.
"""

from __future__ import annotations

import base64
import struct
import sys


def read_string(buf: bytes, off: int) -> tuple[bytes, int]:
    (n,) = struct.unpack_from(">I", buf, off)
    return buf[off + 4:off + 4 + n], off + 4 + n


def seed_of(path: str) -> bytes:
    lines = [l.strip() for l in open(path, encoding="ascii", errors="strict").read().splitlines()]
    body = "".join(l for l in lines if l and not l.startswith("-----"))
    blob = base64.b64decode(body)
    if not blob.startswith(b"openssh-key-v1\x00"):
        raise ValueError("not an OpenSSH private key")
    off = len(b"openssh-key-v1\x00")
    cipher, off = read_string(blob, off)
    kdf, off = read_string(blob, off)
    _, off = read_string(blob, off)
    if cipher != b"none" or kdf != b"none":
        raise ValueError("encrypted key")
    (count,) = struct.unpack_from(">I", blob, off)
    off += 4
    if count != 1:
        raise ValueError("expected one key")
    _, off = read_string(blob, off)  # the public blob
    private, off = read_string(blob, off)
    p = 8  # two check integers
    keytype, p = read_string(private, p)
    if keytype != b"ssh-ed25519":
        raise ValueError("not an Ed25519 key")
    _, p = read_string(private, p)  # the public key again
    secret, p = read_string(private, p)  # seed (32) + public (32)
    if len(secret) != 64:
        raise ValueError("unexpected key length")
    return secret[:32]


def halves(text: str) -> list[str]:
    mid = len(text) // 2
    return [text[:mid], text[mid:]] if len(text) >= 40 else [text]


def markers(seed: bytes) -> list[str]:
    out: list[str] = []
    for pad, drop_front in ((0, 0), (1, 2), (2, 3)):
        text = base64.b64encode(bytes(pad) + seed).decode().rstrip("=")
        out += halves(text[drop_front:len(text) - 2])
    out += halves(seed.hex())
    return out


def fallback(path: str) -> list[str]:
    """If the file cannot be parsed: every body line except the first (the first is the same constant text in every unencrypted key)."""
    lines = [l.strip() for l in open(path, encoding="ascii", errors="ignore").read().splitlines() if l.strip() and not l.startswith("-----")]
    return [l for l in lines[1:] if len(l) >= 20]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: keymarkers.py KEYFILE", file=sys.stderr)
        return 2
    try:
        open(argv[1], "rb").close()
    except OSError:
        return 0
    try:
        found = markers(seed_of(argv[1]))
        status = 0
    except Exception:  # noqa: BLE001  (any parse problem: fall back, loudly via the exit status)
        found, status = fallback(argv[1]), 3
    for m in found:
        print(m)
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
