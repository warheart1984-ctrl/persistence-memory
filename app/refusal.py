"""Machine-readable refusal codes and the error response shape.

Three refusals a client must tell apart, because the right reaction differs:

* ``ledger_unavailable`` (HTTP 503, carries ``Retry-After``): the ledger cannot be served right now
  (database down, timeout, pool saturated, schema or role problem).  Retry **at most once**, after the
  ``Retry-After`` delay with some jitter - never in a tight loop, and not the way you would a conflict.
* ``version_conflict`` (HTTP 409, no ``Retry-After``): the record changed under you.  Re-read it and
  decide; retrying the same request unchanged cannot help.
* ``denied`` (HTTP 401/403, no ``Retry-After``): you may not do this.  Retrying will not change that.
* ``clause_v_violation`` (HTTP 422, no ``Retry-After``): Clause V refused the write (the ledger stores evidence, not
  memory).  The body lists ``reasons`` (each with its own ``code`` and ``field``).  Retrying unchanged cannot help:
  change the type or add evidence.

Any other 503 (for example a deployment that is missing a required key) uses the generic
``unavailable`` code and also carries ``Retry-After``.  The human-readable ``detail`` is unchanged and
never contains hosts, credentials or record ids.
"""

from __future__ import annotations

import os
from typing import Any

from starlette.responses import JSONResponse

LEDGER_UNAVAILABLE = "ledger_unavailable"
VERSION_CONFLICT = "version_conflict"
DENIED = "denied"
UNAVAILABLE = "unavailable"
CLAUSE_V_VIOLATION = "clause_v_violation"


def retry_after_seconds() -> int:
    """Seconds to advertise in ``Retry-After`` (JARVIS_RETRY_AFTER_SECONDS, 1..3600, default 5)."""
    try:
        value = int((os.getenv("JARVIS_RETRY_AFTER_SECONDS") or "").strip() or 5)
    except ValueError:
        return 5
    return value if 1 <= value <= 3600 else 5


def code_for_status(status: int) -> str | None:
    if status in (401, 403):
        return DENIED
    if status == 503:
        return UNAVAILABLE
    return None


def error_body(status: int, detail: Any, code: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"detail": detail}
    chosen = code or code_for_status(status)
    if chosen:
        body["code"] = chosen
    return body


def error_headers(status: int, headers: dict[str, str] | None = None) -> dict[str, str] | None:
    merged = dict(headers or {})
    if status == 503 and not any(k.lower() == "retry-after" for k in merged):
        merged["Retry-After"] = str(retry_after_seconds())
    return merged or None


def json_response(
    status: int, detail: Any, *, code: str | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=error_body(status, detail, code), headers=error_headers(status, headers)
    )
