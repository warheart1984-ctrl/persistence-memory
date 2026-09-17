"""Canonical FastAPI app — re-export from ``app.main``.

The memoryboard service must always expose EMR tool routes
(``POST /api/jarvis/tools/emr_recall``, etc.).  A legacy copy of this module
drifted without those routes; import the real app to avoid stale 404s.
"""

from app.main import app

__all__ = ["app"]
