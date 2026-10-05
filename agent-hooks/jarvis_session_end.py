#!/usr/bin/env python3
"""Cursor sessionEnd hook: RETIRED. It posts nothing to the ledger.

Clause V (agent-hooks/CONSTITUTIONAL_BOUNDARY_CLAUSE.md): the ledger stores evidence, not memory. A session-end note
is a chat summary, which is memory, so this hook no longer writes one (and the API now refuses the types and the
evidence it used to send). Decisions go into the ledger only when someone states them on purpose: through the
`write` tool of the ledger MCP server (type `decision`, with the user's words as evidence) or the REST API.

The file is kept as a no-op so a hooks.json that still lists it keeps working; remove the entry when convenient.
Nothing is read, written or sent.
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
