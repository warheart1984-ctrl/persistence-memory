"""EMR -> STM -> LTM consolidation pipeline (governed, draft-only).

The Memoryboard memory hierarchy, made explicit as a single orchestratable
flow:

    LTM (Continuity Ledger)  --excite-->  STM (active working set)
    STM  ----consolidate---->  LTM (governed DRAFT write back)

Constitutional rules honored here (parity with emr_write / emr.py):
- EMR reads LTM and decides what becomes active cognition (STM).
- Promotion / eviction are dormancy transitions; LTM is never deleted.
- STM / LLM never write the ledger directly: every consolidation is routed
  through the governed ``emr_write.emr_remember`` gateway, which is
  DRAFT-ONLY, conflict-checked, transcript-gated, and provenance-preserving.
- Compression never becomes truth: every STM payload retains its
  ``memory_id`` provenance in the consolidated record's evidence.
- The pipeline NEVER auto-verifies. Verification stays operator/off-band.

Consolidation policy: the pipeline writes ONE governed DRAFT summary record
per run capturing the session's consolidated working set. The source LTM
records already exist; the draft is a durable provenance-bearing note that
"this session distilled these LTM ids into working memory", not a duplicate
of every promoted LTM record (which would merely re-add what is already live).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.continuity import content_sha256, normalize_content
from app.emr import ExciteRequest, excite
from app.emr_write import EmrRememberRequest, emr_remember
from app.models import EvidenceLink, MemoryType
from app.store import JarvisStore

PROTOCOL = "emr-pipeline-v1"


class ConsolidationRequest(BaseModel):
    """Inputs for the EMR->STM->LTM pipeline run."""

    query: str = Field(..., min_length=1, max_length=2000)
    session_key: str = Field(default="default", max_length=128)
    memory_type: MemoryType = "fact"
    subject: str | None = Field(default=None, max_length=256)
    source_agent: str = Field(default="emr-pipeline", max_length=128)
    session_id: str = Field(..., min_length=1, max_length=128)
    # Optional positive-outcome signal: reinforcement of promoted entries only
    # (governed — truth fields are never written by this flag).
    reinforce_selected: bool = False
    user_requested: bool = True
    token_budget: int = Field(default=512, ge=32, le=8000)


class ConsolidateOutcome(BaseModel):
    """Outcome of the STM->LTM consolidation step."""

    memory_id: str
    outcome: str  # consolidated | abstained | gate_refused
    detail: str = ""


class PipelineTrace(BaseModel):
    """Replayable record of one pipeline run: EMR->STM then STM->LTM."""

    session_key: str
    query: str
    stm: list[dict[str, Any]]
    promoted: list[str]
    consolidate: ConsolidateOutcome
    manifest: dict[str, Any]


def _build_summary_body(entries: list[dict[str, Any]]) -> str:
    lines = []
    for e in entries:
        mid = e.get("memory_id") or ""
        sum_ = e.get("summary") or e.get("payload") or ""
        lines.append(f"[{mid}] {sum_}")
    return "\n".join(lines) if lines else ""


def consolidate_to_ltm(
    store: JarvisStore,
    *,
    promoted: list[dict[str, Any]],
    source_agent: str,
    session_id: str,
    memory_type: MemoryType,
    subject: str | None,
    user_requested: bool,
) -> ConsolidateOutcome:
    """Write one governed DRAFT summary record for the promoted working set.

    Routed through ``emr_remember`` so the conflict membrane, transcript
    gate, and draft-only policy all apply. If nothing was promoted or the
    body is empty, the pipeline abstains (no junk writes).
    """
    if not promoted:
        return ConsolidateOutcome(
            memory_id="", outcome="abstained",
            detail="no entries promoted this run; nothing to consolidate",
        )

    source_ids = sorted({e.get("memory_id") or "" for e in promoted})
    body = _build_summary_body(promoted)
    if not body:
        return ConsolidateOutcome(
            memory_id="", outcome="abstained",
            detail="no summary payload across promoted entries",
        )

    # Conflict membrane safety: the consolidated record is a working-set
    # summary, not a new truth claim on the caller's subject. If that subject
    # already has active claims, falling back to an un-subjected draft (or a
    # distinct subject) preserves the membrane instead of co-admitting.
    effective_subject: str | None = subject
    if subject is not None:
        active = store.list_memories(
            limit=9999, truth_scope="live", subject=subject,
        )
        if len(active) > 0:
            effective_subject = None

    resp = emr_remember(
        store,
        EmrRememberRequest(
            content=normalize_content(body),
            source_agent=source_agent,
            session_id=session_id,
            type=memory_type,
            subject=effective_subject,
            tags=sorted(set([*["emr-pipeline", "stm-consolidated"], *source_ids])),
            evidence=[
                EvidenceLink(
                    kind="stm-provenance",
                    ref=sid,
                    note="source LTM memory_id consolidated into STM working set",
                )
                for sid in source_ids
            ],
            confidence=0.5,
            user_requested=user_requested,
            user_statement=(
                "Pipeline consolidation from the EMR STM working set; "
                "draft pending verification."
            ),
        ),
    )

    if resp.accepted:
        return ConsolidateOutcome(
            memory_id=resp.memory["id"] if resp.memory else "",
            outcome="consolidated",
            detail=(
                f"draft written: {resp.provenance.id if resp.provenance else ''}"
                + (f"; subject {subject!r} had active claim, wrote un-subjected" if effective_subject is None and subject is not None else "")
            ),
        )
    return ConsolidateOutcome(
        memory_id="",
        outcome="gate_refused",
        detail=f"{resp.refuse_reason or 'gate_refused'}: {resp.refuse_detail or ''}",
    )


def pipeline(
    store: JarvisStore,
    req: ConsolidationRequest,
    *,
    enforce_abstention: bool = True,
) -> PipelineTrace:
    """Run EMR->STM (excite) then STM->LTM (governed draft consolidation)."""
    excir = ExciteRequest(
        query=req.query,
        session_key=req.session_key,
        token_budget=req.token_budget,
        reinforce_selected=req.reinforce_selected,
    )
    candidates = store.list_memories(limit=9999, truth_scope="live")
    excite_resp = excite(candidates, excir, enforce_abstention=enforce_abstention)

    promoted_entries = [
        e.model_dump() for e in excite_resp.stm if e.memory_id in excite_resp.promoted
    ]

    outcome = consolidate_to_ltm(
        store,
        promoted=promoted_entries,
        source_agent=req.source_agent,
        session_id=req.session_id,
        memory_type=req.memory_type,
        subject=req.subject,
        user_requested=req.user_requested,
    )

    manifest = {
        "protocol": PROTOCOL,
        "stm_promoted": len(promoted_entries),
        "consolidated": 1 if outcome.outcome == "consolidated" else 0,
        "abstained": 1 if outcome.outcome == "abstained" else 0,
        "gate_refused": 1 if outcome.outcome == "gate_refused" else 0,
        "verified": 0,  # the pipeline never auto-verifies
    }

    return PipelineTrace(
        session_key=req.session_key,
        query=req.query,
        stm=[e.model_dump() for e in excite_resp.stm],
        promoted=excite_resp.promoted,
        consolidate=outcome,
        manifest=manifest,
    )


def _hash(content: str) -> str:
    return content_sha256(normalize_content(content))
