"""Request-scoped identity and tenant namespace helpers.

The public MCP surface must never let a model-supplied board id select another
user's records.  The authenticated OAuth subject is the sole tenant selector.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    issuer: str


_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "jarvis_principal", default=None
)


_acting_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar("jarvis_acting_tenant", default=None)


@contextlib.contextmanager
def acting_for(tenant: str):
    """Run code as the operator administering ONE tenant's ledger (the box's per-tenant jobs: seal, sign, verify).

    Internal to a process that the operator started on the box: nothing in the HTTP stack ever sets it, and no request parameter, header or token can,
    so the public API still cannot be made to select a tenant. "operator" is the operator's own namespace (the default one)."""
    token = _acting_tenant.set(tenant)
    try:
        yield
    finally:
        _acting_tenant.reset(token)


def set_principal(principal: Principal):
    return _principal.set(principal)


def reset_principal(token) -> None:
    _principal.reset(token)


def current_principal() -> Principal | None:
    return _principal.get()


def current_tenant_key() -> str | None:
    """Return an opaque, filesystem-safe namespace for the current subject."""
    acting = _acting_tenant.get()
    if acting is not None:                 # the operator, administering that tenant (see acting_for): its key as it is stored; the operator's namespace has none
        return None if acting == "operator" else acting
    principal = current_principal()
    if principal is None:
        return None
    # Keep the raw subject out of filenames and logs.  Issuer prevents collisions
    # between otherwise identical subjects from different identity providers.
    raw = f"{principal.issuer}\x1f{principal.subject}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalized_subject(value: object) -> str | None:
    subject = str(value or "").strip()
    if not subject or len(subject) > 512 or not re.fullmatch(r"[^\x00-\x1f]+", subject):
        return None
    return subject
