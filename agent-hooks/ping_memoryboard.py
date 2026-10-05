"""Diagnostic: is the ledger up, ready, and what does it hold?  Honors JARVIS_MEMORYBOARD_URL and JARVIS_API_KEY."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jarvis_common import http_json  # noqa: E402


def main():
    health = http_json("GET", "/health")
    print(f"Health: {health['status']}")
    ready = http_json("GET", "/ready")  # 503 (an error here) if the ledger cannot be served
    print(f"Ready: {ready['status']} {ready.get('checks', {})}")

    memories = http_json("GET", "/api/jarvis/memory")["memories"]
    print(f"Memories stored: {len(memories)}")
    for mem in memories:
        print(f"  [{mem['id']}] {mem['content'][:100]}")

    board = http_json("GET", "/api/jarvis/memory/board")
    print(f"Board: {board['memory_board']['summary']}")

    print("OK: service is live")


if __name__ == "__main__":
    main()
