"""Constitutional write path — eligible proposals become draft ledger records.

Runs only when the caller asked AND JARVIS_TWIN_CHAT_PERSIST_ENABLED is on
(checked by the endpoint). Every write flows through ``store.create_memory``,
so Clause V enforcement is never bypassed. Before any write the base
ChatTurnReceipt is resolved and tenant/session/turn-matched — memory
evidence references that base digest, never an outcome receipt.

Failures never abort siblings: each proposal is its own try, recorded as
{proposal_index, code} on the linked ChatPersistReceipt — no rejected or
raw text is stored.
"""

from __future__ import annotations

from typing import Any

from app.clause_v import ClauseVViolation
from app.models import EvidenceLink, MemoryCreate

from .extract import is_persistable
from .models import ChatPersistReceipt, PersistFailure, ProposedClaim

CONFIDENCE_CAP = 0.6


def _source_agent(attribution: str, tenant_key: str) -> str:
    """Service-assigned only — client/model source_agent is never trusted."""
    if attribution == "user":
        return f"user:{tenant_key}"
    return "ai-twin"


def persist_claims(
    store: Any,
    *,
    tenant_key: str,
    session_id: str,
    turn_index: int,
    turn_receipt_digest: str,
    proposals: list[ProposedClaim],
    receipt_store: Any,
) -> ChatPersistReceipt:
    """Write eligible proposals; return the linked outcome receipt body.

    The base receipt is verified to exist and match this tenant/session/turn
    before the first write is attempted — a receipt ref that cannot be
    resolved here must never be cited as evidence.
    """
    base = receipt_store.get_receipt(tenant_key, turn_receipt_digest)
    if (
        base is None
        or base.get("session_id") != session_id
        or base.get("turn_index") != turn_index
    ):
        return ChatPersistReceipt(
            turn_receipt_digest=turn_receipt_digest,
            session_id=session_id,
            turn_index=turn_index,
            persisted_ids=[],
            failures=[PersistFailure(proposal_index=-1, code="RECEIPT_UNVERIFIED")],
        )

    persisted: list[str] = []
    failures: list[PersistFailure] = []

    for i, claim in enumerate(proposals):
        if not is_persistable(claim):
            continue  # visible on the base receipt; never written in v1
        body = MemoryCreate(
            type="decision",
            content=claim.content,
            source_agent=_source_agent(claim.attribution, tenant_key),
            session_id=session_id,
            subject=claim.subject,
            confidence=min(0.6, CONFIDENCE_CAP),
            status="draft",
            tags=["twin-chat"],
            evidence=[
                EvidenceLink(
                    kind="user-request",
                    ref=f"turn-receipt:{turn_receipt_digest}",
                    note="user decision captured in governed chat turn",
                )
            ],
        )
        try:
            rec = store.create_memory(body)
            persisted.append(rec.id)
        except ClauseVViolation as exc:
            code = exc.reasons[0].code if exc.reasons else "clause_v_violation"
            failures.append(PersistFailure(proposal_index=i, code=code))
        except ValueError:
            failures.append(PersistFailure(proposal_index=i, code="invalid"))
        except Exception:
            failures.append(PersistFailure(proposal_index=i, code="store_error"))

    return ChatPersistReceipt(
        turn_receipt_digest=turn_receipt_digest,
        session_id=session_id,
        turn_index=turn_index,
        persisted_ids=persisted,
        failures=failures,
    )
