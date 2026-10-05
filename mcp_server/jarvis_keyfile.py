"""Read an API key from a file that Windows may have saved in any of its usual encodings.

PowerShell 5.1 writes UTF-16 with a byte-order mark for ``>`` and ``Out-File``; Notepad and other tools add a UTF-8
BOM. A plain ``read_text(encoding="utf-8")`` then either crashes (UTF-16) or returns a key that starts with an
invisible U+FEFF (UTF-8 BOM), which is not the key. This accepts the key whichever way the file was saved, and refuses
anything that is not clearly one printable ASCII word, with an error that never contains the file's content.

Kept free of other imports so the stdio servers can load it whether they are run as ``-m mcp_server...`` or as a
script. ``agent-hooks/jarvis_common.py`` carries a copy of the same logic (a test keeps the two in step).
"""

from __future__ import annotations

from pathlib import Path

_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


class KeyFileError(ValueError):
    """The key file cannot give a usable key. The message completes the sentence "The key file ...".""" 


def _decode(raw: bytes) -> str:
    try:
        if raw.startswith(_UTF8_BOM):
            return raw.decode("utf-8-sig")
        if raw.startswith(_UTF16_BOMS):
            return raw.decode("utf-16")  # the BOM chooses the byte order
        if b"\x00" in raw:
            # UTF-16 without a BOM: ASCII text has a zero in every other byte
            if len(raw) >= 2 and raw[0] != 0 and raw[1] == 0:
                return raw.decode("utf-16-le")
            if len(raw) >= 2 and raw[0] == 0 and raw[1] != 0:
                return raw.decode("utf-16-be")
            raise KeyFileError("is not text")
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise KeyFileError("is not valid text (expected UTF-8 or UTF-16)") from None


def parse_key_text(raw: bytes) -> str:
    """The key held in ``raw``: its first non-empty line, decoded and trimmed. Raises KeyFileError."""
    text = _decode(raw).lstrip("﻿")
    lines = text.strip().splitlines()
    key = lines[0].strip() if lines else ""
    if not key:
        raise KeyFileError("is empty")
    if not key.isascii() or not key.isprintable() or any(ch.isspace() for ch in key):
        raise KeyFileError("does not hold a valid API key (one printable ASCII word on its first line)")
    return key


def read_key_file(path: str | Path) -> str:
    """Read and parse a key file. Raises KeyFileError for a missing, unreadable, empty or malformed file."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise KeyFileError("is missing or cannot be read") from None
    return parse_key_text(raw)
