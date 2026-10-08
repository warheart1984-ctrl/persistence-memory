"""Turn orchestrator — the governed pipeline, fail-closed at every stage.

Order matters: authenticate/derive tenant at the endpoint → lease the
session → window → optional signal → tenant-scoped recall → prompt →
backend → gate → deterministic fallback if needed → extract → immutable
base receipt → optional persist + linked outcome receipt → window-append.

Model-side failures degrade to honest deterministic output; receipt-store
exhaustion raises ReceiptError before any output is generated.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from app.emr_tool import EmrRecallRequest, emr_recall
from app.narrator.base import NarratorError
from app.twin import is_twin_authored

from .backends import ChatBackend, Message, none_reply, resolve_backend
from .extract import extract
from .gate import gate_reply
from .models import (
    ChatPersistReceipt,
    ChatRequest,
    ChatResponse,
    ChatTurnReceipt,
    Turn,
)
from .persist import persist_claims
from .receipts import ReceiptError, ReceiptStore, get_receipt_store
from .session import SessionStore
from .signal import compile_turn
from .prompt import SYSTEM_PROMPT, build_prompt

MAX_TOKENS = 900
TEMPERATURE = 0.0
TIMEOUT_S = 30.0

_sessions = SessionStore()


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _gate_mode() -> str:
    v = os.getenv("JARVIS_TWIN_CHAT_GATE", "enforce").strip().lower()
    return v if v in ("off", "shadow", "enforce") else "enforce"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request_digest(req: ChatRequest) -> str:
    return _sha(json.dumps(
        {"session_id": req.session_id, "message": req.message,
         "provider": req.provider, "persist": req.persist},
        sort_keys=True, separators=(",", ":"),
    ))


def _recalled_for_prompt(store: Any, result: Any) -> list[dict]:
    """Bundle items → prompt rows, tagging twin-authored records."""
    out = []
    for item in result.bundle:
        rec = store.get_memory(item.memory_id)
        out.append({
            "id": item.memory_id,
            "type": item.type,
            "status": item.status,
            "confidence": item.confidence,
            "subject": item.subject,
            "content": item.content,
            "tags": list(item.tags or []),
            "twin_authored": bool(rec is not None and is_twin_authored(rec)),
        })
    return out


def run_turn(
    store: Any,
    req: ChatRequest,
    *,
    tenant_key: str,
    receipt_store: ReceiptStore | None = None,
) -> ChatResponse:
    """One governed turn. ReceiptStore failures propagate as ReceiptError."""
    receipts = receipt_store or get_receipt_store()
    acquired, stale_takeover, lease_token = receipts.acquire_lease(
        tenant_key, req.session_id
    )
    if not acquired:
        raise ReceiptError("SESSION_BUSY", "a turn is already in flight for this session")
    try:
        return _run(store, req, tenant_key, receipts, context_reset_hint=stale_takeover)
    finally:
        # Ownership-bound release: if this turn outlived its lease and
        # another turn took over, this release must not evict theirs.
        receipts.release_lease(tenant_key, req.session_id, token=lease_token)


def _run(
    store: Any,
    req: ChatRequest,
    tenant_key: str,
    receipts: ReceiptStore,
    *,
    context_reset_hint: bool,
) -> ChatResponse:
    t0 = time.monotonic()

    # 1. Session window (process-local, tenant-bound). A known session with no
    #    live window — restart, eviction, or stale-lease takeover — is a reset.
    head = receipts.head(tenant_key, req.session_id)
    window = _sessions.load(tenant_key, req.session_id)
    context_reset = bool(context_reset_hint or (head is not None and not window))

    # 2. Optional signal — bounded hint only.
    signal = compile_turn(req.message)

    # 3. Governed recall — abstention and conflict membrane enforced by EMR.
    recall = emr_recall(
        store,
        EmrRecallRequest(
            intent="chat",
            query=req.message,
            max_memories=8,
            truth_scope="live",
            session_key=f"twin-chat:{req.session_id}"[:128],
            include_provenance=False,
        ),
    )
    conflict_subjects = sorted({c.subject for c in recall.conflicts})
    recalled = _recalled_for_prompt(store, recall)

    # 4. Prompt.
    system, messages = build_prompt(
        recalled=recalled,
        conflict_subjects=conflict_subjects,
        abstained=recall.abstained,
        abstention_reason=recall.abstention_reason,
        session_turns=window,
        signal=signal,
        message=req.message,
    )

    # 5. Backend. "off" gate mode never calls a model at all.
    mode = _gate_mode()
    backend: ChatBackend | None = None
    raw = ""
    model = ""
    usage: dict | None = None
    fallback_reason: str | None = None
    if mode == "off":
        fallback_reason = "GATE_OFF"
    else:
        try:
            backend = resolve_backend(req.provider)
        except NarratorError:
            raise
        except Exception as exc:
            fallback_reason = f"NARRATOR_ERROR:{type(exc).__name__}"
        if backend is not None and backend.name == "none":
            fallback_reason = "NO_MODEL_BACKEND"
        if backend is not None and backend.name != "none":
            try:
                res = backend.chat(
                    messages, model=getattr(backend, "default_model", "") or "",
                    temperature=TEMPERATURE, max_tokens=MAX_TOKENS, timeout_s=TIMEOUT_S,
                )
                raw = res.text
                model = res.model
                usage = res.usage
                latency = res.latency_ms
            except NarratorError as exc:
                fallback_reason = exc.code
            except Exception as exc:
                fallback_reason = f"NARRATOR_ERROR:{type(exc).__name__}"

    # 6. Gate — enforce and shadow return identical filtered/fallback bytes.
    dropped: list = []
    if raw and not fallback_reason:
        gated = gate_reply(raw, recalled)
        dropped = gated["dropped"]
        if gated["all_dropped"] or not gated["reply"].strip():
            # No citable content survived — whitespace-only and bare-cite
            # output land here too; an empty reply is never an answer.
            fallback_reason = fallback_reason or "ALL_DROPPED"
            reply = none_reply(
                recalled=recalled, conflict_subjects=conflict_subjects,
                abstained=recall.abstained,
                abstention_reason=recall.abstention_reason,
                reason="model output failed the claim gate",
            )
        else:
            reply = gated["reply"]
    else:
        reply = none_reply(
            recalled=recalled, conflict_subjects=conflict_subjects,
            abstained=recall.abstained,
            abstention_reason=recall.abstention_reason,
            reason=fallback_reason or "no model backend configured",
        )
    fallback_used = bool(fallback_reason)
    latency = int((time.monotonic() - t0) * 1000)
    backend_name = backend.name if backend is not None else "none"

    # 7. Extraction — deterministic proposals, never writes. The dedup scan
    #    is lazy: a message with zero candidate patterns never triggers a
    #    full-ledger read.
    proposals = extract(
        req.message,
        existing=lambda: store.list_memories(limit=100000, truth_scope="live"),
    )

    # 8. Immutable base turn receipt (allocated/inserted atomically).
    receipt_body = ChatTurnReceipt(
        session_id=req.session_id,
        at=datetime.now(timezone.utc).isoformat(),
        request_digest=_request_digest(req),
        state_digest=None,  # v1: metadata slot reserved, not populated per turn
        context_reset=context_reset,
        recalled_ids=[i["id"] for i in recalled],
        conflict_subjects=conflict_subjects,
        abstained=recall.abstained,
        abstention_reason=recall.abstention_reason,
        signal=signal,
        backend=backend_name,
        model=model or "none",
        gate_mode=mode,
        gate_dropped=dropped,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        prompt_digest=_sha(system + "\n" + json.dumps(messages, sort_keys=True)),
        raw_output_digest=_sha(raw),
        reply_digest=_sha(reply),
        proposed_claims=proposals,
        latency_ms=latency,
        usage=usage,
    ).model_dump(mode="json")
    receipt_body = receipts.append_turn_receipt(tenant_key, receipt_body)

    # 9. Optional persist — endpoint has already enforced the flag+write auth.
    persist_body = None
    if req.persist:
        outcome = persist_claims(
            store,
            tenant_key=tenant_key,
            session_id=req.session_id,
            turn_index=receipt_body["turn_index"],
            turn_receipt_digest=receipt_body["receipt_digest"],
            proposals=proposals,
            receipt_store=receipts,
        )
        persist_body = receipts.append_persist_receipt(
            tenant_key, outcome.model_dump(mode="json")
        )

    # 10. Window-append — the turn joins server-owned context. Context is
    #     bounded by the Turn contract: a reply longer than the bound is
    #     truncated for the window ONLY — the receipt already digests the
    #     full reply and this must never raise post-commit.
    _sessions.append(tenant_key, req.session_id, [
        Turn(role="user", content=req.message[:8000]),
        Turn(role="assistant", content=reply[:8000],
             receipt_digest=receipt_body["receipt_digest"]),
    ])

    return ChatResponse(
        turn_id=receipt_body["receipt_digest"],
        reply=reply,
        receipt=ChatTurnReceipt(**receipt_body),
        persist_receipt=ChatPersistReceipt(**persist_body) if persist_body else None,
        degraded=fallback_used or backend_name == "none",
    )
