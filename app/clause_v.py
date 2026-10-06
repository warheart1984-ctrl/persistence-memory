"""Clause V gate: the ledger stores evidence, not memory.

Clause V (agent-hooks/CONSTITUTIONAL_BOUNDARY_CLAUSE.md): continuity stores evidence, not chat dumps, emotion, transient
state or ungoverned context. This module is the write-side gate. It runs inside the stores' ``create_memory`` and
``update_memory`` so that EVERY path that writes a ledger record (REST, the EMR tools, promote, the pipeline, the MCP
servers, the hooks) goes through it.

Two kinds of rule, with different consequences:

HARD rules (refuse with HTTP 422 ``clause_v_violation``; mode ``JARVIS_CLAUSE_V``, default ``enforce``)
    * the type must be one of ``decision``, ``architecture``, ``research``, ``fact``
      (``preference`` -> ``clause_v_preference``; ``task`` -> ``clause_v_transient_state``;
      ``external_context`` -> ``clause_v_external_context``);
    * evidence is required (``clause_v_evidence_required``): a ``decision`` needs at least one evidence link (a
      ``user-request`` link counts: the operator's statement is the authority); every other type needs at least one
      link whose ``kind`` is on the allowlist below (a chat message is not evidence for a fact).

SOFT rules (warn only for now; mode ``JARVIS_CLAUSE_V_SOFT``: ``warn`` (default), ``enforce`` or ``off``)
    * ``clause_v_emotion``: reaction, laughter, praise and first-person feeling markers;
    * ``clause_v_transient_state``: present-progressive operational state and "just now" language;
    * ``clause_v_transcript_dump``: text shaped like a chat transcript.
    A soft hit is logged (codes and a content hash, never the content) and returned to the caller as
    ``clause_v_warnings``; nothing is refused until the operator reviews the warnings and flips the mode.

Updates are gated only where they could admit something to the constitutional path: a record that would be (or stay)
``verified``, a change of ``type`` or ``evidence``, or leaving ``archived``. Archiving, deleting and ordinary edits to
an existing draft are always allowed, so records written before the gate existed stay readable and manageable.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger("jarvis.clause_v")

CLAUSE_V_VIOLATION = "clause_v_violation"

ALLOWED_TYPES = frozenset({"decision", "architecture", "research", "fact"})

# Evidence for anything but a decision must point at something checkable. A chat message is not evidence.
EVIDENCE_KINDS = frozenset({"file", "url", "commit", "test", "receipt", "command", "document", "doc", "issue", "pr", "log"})

PREFERENCE = "clause_v_preference"
TRANSIENT = "clause_v_transient_state"
EXTERNAL = "clause_v_external_context"
TYPE_NOT_ALLOWED = "clause_v_type_not_allowed"
EVIDENCE_REQUIRED = "clause_v_evidence_required"
EMOTION = "clause_v_emotion"
TRANSCRIPT = "clause_v_transcript_dump"

_EMOTION_RE = re.compile(
    r"""
      \b(?:lol|lmao|rofl|haha+|hehe+)\b
    | \bi(?:'m|\s+am|\s+feel|\s+felt)\s+(?:so\s+|really\s+|very\s+)?(?:happy|sad|proud|excited|angry|scared|thrilled|amazed|upset|afraid)\b
    | \bmoment\s+of\s+(?:profound|genuine|well-earned|disbelief|reflection|alignment|recognition)\b
    | \b(?:profound|revolutionary|remarkable)\s+(?:insight|moment|alignment|revelation|realization|recognition)\b
    | \b(?:bad\s*ass|smart\s+as\s+a\s+whip|you\s+are\s+a\s+genius)\b
    | \bhumble[-\s]brag\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_TRANSIENT_RE = re.compile(
    r"""
      \b(?:is|are)\s+(?:now\s+|currently\s+|successfully\s+)?(?:running|listening|operational|started|stopped)\b
    | \b(?:right\s+now|just\s+now|at\s+this\s+moment|currently)\b
    | \b\d+\s+(?:minutes?|hours?)\s+ago\b
    | \bsession\s+\S+\s+(?:ended|started)\b
    | \b(?:in\s+progress|work\s+in\s+progress|wip|todo)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_TRANSCRIPT_RE = re.compile(
    r"^(?:user|assistant|system|human|chatgpt)\s*:\s|\b(transcript|conversation dump|chat log)\b|(?:\n.*){25,}",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class Reason:
    code: str
    message: str
    field: str
    hard: bool = True

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


class ClauseVViolation(Exception):
    """A write refused by Clause V. Not a ValueError on purpose: it must not be mistaken for a bad request."""

    def __init__(self, reasons: list[Reason]):
        self.reasons = reasons
        self.message = "Clause V: the ledger stores evidence, not memory. " + "; ".join(r.code for r in reasons)
        super().__init__(self.message)

    def body(self) -> dict[str, Any]:
        return {"detail": self.message, "code": CLAUSE_V_VIOLATION, "clause": "V", "reasons": [r.to_dict() for r in self.reasons]}


# --- configuration ------------------------------------------------------------------------------------------

def hard_mode() -> str:
    """``enforce`` (default) or ``off``. Anything unrecognised means enforce: a typo must not switch the gate off."""
    return "off" if (os.getenv("JARVIS_CLAUSE_V") or "").strip().lower() == "off" else "enforce"


def soft_mode() -> str:
    """``warn`` (default), ``enforce`` or ``off`` for the emotion / transient / transcript rules."""
    value = (os.getenv("JARVIS_CLAUSE_V_SOFT") or "").strip().lower()
    return value if value in ("warn", "enforce", "off") else "warn"


# --- evaluation ---------------------------------------------------------------------------------------------

def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _evidence_reason(mem_type: str, evidence: Iterable[Any], resolve: Any = None) -> Reason | None:
    from app import evidence as evidence_objects  # local import: evidence.py is independent of this module

    links = list(evidence or [])
    if mem_type == "decision":
        if any(str(_get(e, "ref", "") or "").strip() for e in links):
            return None
        return Reason(EVIDENCE_REQUIRED, "A decision needs at least one evidence link (a user-request link is accepted).", "evidence")
    if any(str(_get(e, "kind", "") or "").strip().lower() in EVIDENCE_KINDS and str(_get(e, "ref", "") or "").strip() for e in links):
        return None
    if any(evidence_objects.counts_as_fact_evidence(e, resolve) for e in links):
        return None
    kinds = ", ".join(sorted(EVIDENCE_KINDS))
    return Reason(
        EVIDENCE_REQUIRED,
        f"A {mem_type} needs at least one checkable evidence link (kind one of: {kinds}, or an evidence-object link to an "
        f"intact {evidence_objects.CES_FACT} object). A chat message is not evidence.",
        "evidence",
    )


def hard_reasons(*, type: str, evidence: Iterable[Any], resolve: Any = None) -> list[Reason]:
    out: list[Reason] = []
    if type == "preference":
        out.append(Reason(PREFERENCE, "Preferences are memory, not evidence (Clause V). Record a decision or an architecture note instead.", "type"))
    elif type == "task":
        out.append(Reason(TRANSIENT, "Tasks are transient state (Clause V); they do not belong in the ledger.", "type"))
    elif type == "external_context":
        out.append(Reason(EXTERNAL, "External context is ungoverned context (Clause V). Record a decision that cites it as evidence.", "type"))
    elif type not in ALLOWED_TYPES:
        out.append(Reason(TYPE_NOT_ALLOWED, f"Type {type!r} is not allowed on the constitutional path (allowed: {', '.join(sorted(ALLOWED_TYPES))}).", "type"))
    er = _evidence_reason(type, evidence, resolve)
    if er is not None:
        out.append(er)
    return out


def soft_reasons(*, content: str, subject: str | None, tags: Iterable[str]) -> list[Reason]:
    out: list[Reason] = []
    text = " ".join([content or "", subject or "", " ".join(tags or [])])
    if _EMOTION_RE.search(text):
        out.append(Reason(EMOTION, "Reads like a reaction, praise or a feeling (Clause V excludes emotion).", "content", hard=False))
    if _TRANSIENT_RE.search(content or ""):
        out.append(Reason(TRANSIENT, "Reads like transient state, not a lasting fact (Clause V).", "content", hard=False))
    if _TRANSCRIPT_RE.search(content or ""):
        out.append(Reason(TRANSCRIPT, "Looks like a chat transcript dump (Clause V).", "content", hard=False))
    return out


# --- warnings handed back to the API layer ------------------------------------------------------------------

_warnings: contextvars.ContextVar[list[Reason] | None] = contextvars.ContextVar("clause_v_warnings", default=None)


def take_warnings() -> list[dict[str, str]]:
    """The soft warnings the last gate call in this request produced (and forget them)."""
    got = _warnings.get() or []
    _warnings.set(None)
    return [r.to_dict() for r in got]


def _log(kind: str, reasons: list[Reason], *, content: str, source_agent: str | None, session_id: str | None, mem_type: str, action: str) -> None:
    digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:12]
    logger.warning(
        "clause_v %s: action=%s codes=%s type=%s source_agent=%s session_id=%s content_sha256=%s",
        kind, action, ",".join(r.code for r in reasons), mem_type, source_agent, session_id, digest,
    )


def _judge(*, action: str, check_hard: bool, check_soft: bool = True, type: str, content: str, subject: str | None, tags: Iterable[str],
           evidence: Iterable[Any], source_agent: str | None, session_id: str | None, resolve: Any = None) -> None:
    _warnings.set(None)
    if hard_mode() == "off":
        return
    reasons: list[Reason] = hard_reasons(type=type, evidence=evidence, resolve=resolve) if check_hard else []
    soft: list[Reason] = [] if (soft_mode() == "off" or not check_soft) else soft_reasons(content=content, subject=subject, tags=tags)
    if soft and soft_mode() == "enforce":
        reasons += soft
        soft = []
    if reasons:
        _log("refused", reasons, content=content, source_agent=source_agent, session_id=session_id, mem_type=type, action=action)
        raise ClauseVViolation(reasons)
    if soft:
        _log("soft hit (warn only)", soft, content=content, source_agent=source_agent, session_id=session_id, mem_type=type, action=action)
        _warnings.set(soft)


def gate_create(data: Any, resolve: Any = None) -> None:
    """Called by every store before it writes a new record. Raises ClauseVViolation, or leaves warnings behind.

    ``resolve`` (optional) maps an evidence-object id to what it resolved to, so such links can count as evidence."""
    _judge(action="create", check_hard=True, type=data.type, content=data.content, subject=data.subject, tags=data.tags,
           evidence=data.evidence, source_agent=data.source_agent, session_id=data.session_id, resolve=resolve)


def gate_update(existing: Any, update: Any, resolve: Any = None) -> None:
    """Called by every store before it applies an update to ``existing``."""
    def pick(name: str) -> Any:
        value = getattr(update, name, None)
        return getattr(existing, name) if value is None else value

    status, mem_type, evidence = pick("status"), pick("type"), pick("evidence")
    content, subject, tags = pick("content"), pick("subject"), pick("tags")
    admits = (
        status == "verified"
        or (update.type is not None and update.type != existing.type)
        or update.evidence is not None
        or (existing.status == "archived" and status != "archived")
    )
    text_changed = any(getattr(update, name, None) is not None for name in ("content", "subject", "tags"))
    _judge(action="update", check_hard=admits and status != "archived", check_soft=text_changed and status != "archived",
           type=mem_type, content=content, subject=subject, tags=tags, evidence=evidence,
           source_agent=pick("source_agent"), session_id=pick("session_id"), resolve=resolve)
