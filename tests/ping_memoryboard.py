"""Kept for old habits: runs the real diagnostic in agent-hooks/ (one copy, so they cannot drift)."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "agent-hooks" / "ping_memoryboard.py"), run_name="__main__")
