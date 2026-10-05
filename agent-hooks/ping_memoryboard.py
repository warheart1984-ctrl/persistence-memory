"""Diagnostic: is the ledger up, ready, and what does it hold?

Needs JARVIS_MEMORYBOARD_URL (no default: it refuses to guess an address) and, for a protected server,
JARVIS_API_KEY or JARVIS_API_KEY_FILE.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jarvis_common import BaseURLNotSet, http_json  # noqa: E402

LIST_LIMIT = 200  # the largest page the API serves


def main():
    health = http_json("GET", "/health")
    print(f"Health: {health['status']}")
    ready = http_json("GET", "/ready")  # 503 (an error here) if the ledger cannot be served
    print(f"Ready: {ready['status']} {ready.get('checks', {})}")

    memories = http_json("GET", f"/api/jarvis/memory?limit={LIST_LIMIT}")["memories"]
    if len(memories) >= LIST_LIMIT:
        print(f"Memories shown: {len(memories)} (capped at {LIST_LIMIT}; the ledger may hold more)")
    else:
        print(f"Memories stored: {len(memories)}")
    for mem in memories:
        print(f"  [{mem['id']}] {mem['content'][:100]}")

    board = http_json("GET", "/api/jarvis/memory/board")
    print(f"Board: {board['memory_board']['summary']}")

    print("OK: service is live")


if __name__ == "__main__":
    try:
        main()
    except BaseURLNotSet as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
