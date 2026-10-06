#!/usr/bin/env python3
"""Cursor afterAgentResponse hook: RETIRED. It no longer caches the reply anywhere.

It used to save the last assistant message to a local file in the hook state folder, for the sessionEnd hook. That hook
is retired, so the file served nothing and only kept chat text on disk. This is now a no-op (it reads nothing from the
reply, writes nothing and sends nothing). The file is kept so a hooks.json that still lists it keeps working; remove the
entry when convenient. The sessionStart hook, which loads the ledger into the session, is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis_common import emit, read_stdin_json  # noqa: E402


def main() -> int:
    read_stdin_json()  # drain what the host sends; nothing is done with it
    emit({})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - hooks must fail open
        emit({})
        raise SystemExit(0)
