"""Request-scoped identity and tenant namespace helpers.

The public MCP surface must never let a model-supplied board id select another
user's records.  The authenticated OAuth subject is the sole tenant selector.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import os
import re
from dataclasses import dataclass

OPERATOR_TENANT = "operator"          # the operator's own namespace (the shared-key deployment's one tenant)
UNAUTHENTICATED_LABEL = "_unauth"      # what a log entry says when no identity was resolved (strict mode): never "operator"


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    issuer: str
    kind: str = "tenant"               # "tenant" (an OAuth subject) or "operator" (the configured ledger key): who the caller IS, not how the deployment is set up


def operator_principal() -> Principal:
    """The identity of a request that presented the configured operator credential (or the explicit local-dev opt-out)."""
    return Principal(subject=OPERATOR_TENANT, scopes=frozenset({"memory.read", "memory.write"}), issuer="jarvis-operator-key", kind="operator")


def multi_tenant_mode() -> bool:
    """JARVIS_MULTI_TENANT: strict identity. No request may be served without a resolved principal, and "no identity" is never treated as the operator."""
    return (os.getenv("JARVIS_MULTI_TENANT") or "").strip().lower() in ("1", "true", "yes", "on")


_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "jarvis_principal", default=None
)


_acting_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar("jarvis_acting_tenant", default=None)


def in_operator_job() -> bool:
    """True only inside ``acting_for``: the box's own per-tenant job, started by the operator. Set by nothing else, so no request can make it true (an OAuth
    tenant's token certainly cannot); the operator-only gates honor it even when the deployment authenticates users with OAuth."""
    return _acting_tenant.get() is not None


@contextlib.contextmanager
def acting_for(tenant: str):
    """Run code as the operator administering ONE tenant's ledger (the box's per-tenant jobs: seal, sign, verify).

    Internal to a process that the operator started on the box: nothing in the HTTP stack ever sets it, and no request parameter, header or token can,
    so the public API still cannot be made to select a tenant. "operator" is the operator's own namespace (the default one)."""
    token = _acting_tenant.set(tenant)
    principal_token = _principal.set(operator_principal())     # the operator's own process: the operator-only gates must pass for it, in strict mode too
    try:
        yield
    finally:
        _principal.reset(principal_token)
        _acting_tenant.reset(token)


def set_principal(principal: Principal):
    return _principal.set(principal)


def reset_principal(token) -> None:
    _principal.reset(token)


def current_principal() -> Principal | None:
    return _principal.get()


@contextlib.contextmanager
def system_scope():
    """Run infrastructure code (the readiness check) as the operator namespace, explicitly. It concerns the database and its roles, not any tenant, and it must
    keep working in strict mode, where a request with no identity is refused a store."""
    token = _principal.set(operator_principal())
    try:
        yield
    finally:
        _principal.reset(token)


def is_operator() -> bool:
    """True only for a request that authenticated as the operator."""
    principal = current_principal()
    return principal is not None and principal.kind == "operator"


def request_tenant() -> str | None:
    """The tenant this request's data belongs to, or None when it cannot be resolved.

    An OAuth subject is its opaque hash; the authenticated operator is "operator".  With no principal the legacy deployment (shared key) treats the
    request as the operator, because the key middleware has already refused anyone who is not; in JARVIS_MULTI_TENANT mode there is no such default.
    """
    acting = _acting_tenant.get()
    if acting is not None:                 # the box's per-tenant jobs (acting_for): the tenant they were started for, in strict mode too
        return acting
    tenant = current_tenant_key()
    if tenant is not None:
        return tenant
    if is_operator():
        return OPERATOR_TENANT
    return None if multi_tenant_mode() else OPERATOR_TENANT


def current_tenant_key() -> str | None:
    """Return an opaque, filesystem-safe namespace for the current subject (None for the operator, whose namespace is the default one)."""
    acting = _acting_tenant.get()
    if acting is not None:                 # the operator, administering that tenant (see acting_for): its key as it is stored; the operator's namespace has none
        return None if acting == OPERATOR_TENANT else acting
    principal = current_principal()
    if principal is None or principal.kind == "operator":
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
