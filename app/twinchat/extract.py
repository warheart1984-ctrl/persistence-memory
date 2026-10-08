"""Claim extraction — what a user utterance may become as a ledger proposal.

Deterministic rules run always. A proposal is a *candidate*, not a write:
a receipt proves the utterance occurred, not that its content is true.
V1 persists only user-attributed ``decision`` proposals; preferences and
bare factual assertions stay on the receipt as visible, non-persistable
proposals. Model-assisted extraction (``JARVIS_TWIN_CHAT_EXTRACT=model``)
may add proposals, but model output alone is never evidence — model
proposals are never persistable in v1.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .models import ProposedClaim

# --- deterministic patterns over the USER message ---------------------------
# Each rule maps a pattern to (claim_type, evidence_kind). The captured group
# is the claim content; it is normalized but never rewritten into facts.

_DECISION_RES = [
    re.compile(r"\bi(?:'ve| have)? decided (?:to |that )?(.+)", re.IGNORECASE),
    re.compile(r"\bwe decided (?:to |that )?(.+)", re.IGNORECASE),
    re.compile(r"\bwe'?re going with (.+)", re.IGNORECASE),
    re.compile(r"\bthe choice is (.+)", re.IGNORECASE),
    re.compile(r"\blet'?s (?:use|go with|pick) (.+)", re.IGNORECASE),
    re.compile(r"\bdecision\s*:\s*(.+)", re.IGNORECASE),
    re.compile(r"\bfinal answer is (.+)", re.IGNORECASE),
]

_FACT_RES = [
    re.compile(r"\bi prefer (.+)", re.IGNORECASE),
    re.compile(r"\bmy ([a-z][\w -]{1,40}?) (?:is|are) (.+)", re.IGNORECASE),
    re.compile(r"\bi (?:use|run|work on) (.+)", re.IGNORECASE),
]

_RESEARCH_RES = [
    re.compile(r"\bactually[, ]+(?:it'?s|it is) (.+)", re.IGNORECASE),
    re.compile(r"\bcorrection\s*:\s*(.+)", re.IGNORECASE),
    re.compile(r"\bi was wrong[,:]? (.+)", re.IGNORECASE),
]

_WS_RE = re.compile(r"\s+")
_TRAIL_RE = re.compile(r"[.!?,;:\s]+$")


def _clean(text: str, limit: int = 300) -> str:
    t = _WS_RE.sub(" ", text).strip()
    t = _TRAIL_RE.sub("", t)
    return t[:limit].strip()


def _subject_hint(content: str) -> str | None:
    """Short subject guess: first few tokens, lowercased; None if too vague."""
    words = re.findall(r"[A-Za-z0-9_.\-]+", content.lower())
    if not words:
        return None
    return " ".join(words[:6])[:64]


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text.strip().lower())


def _dedup(candidates: list[ProposedClaim], existing: list[Any]) -> list[ProposedClaim]:
    """Skip proposals identical to an existing live memory on the same subject."""
    seen = {
        (_norm(getattr(m, "subject", None) or ""), _norm(getattr(m, "content", "") or ""))
        for m in existing
    }
    out = []
    for c in candidates:
        key = (_norm(c.subject or ""), _norm(c.content))
        if key not in seen:
            out.append(c)
    return out


def is_persistable(claim: ProposedClaim) -> bool:
    """v1 rule: only rule-extracted, user-attributed decisions write."""
    return (
        claim.attribution == "user"
        and claim.claim_type == "decision"
        and claim.extractor.startswith("rule:")
    )


def extract(
    message: str,
    *,
    existing: list[Any] | Callable[[], list[Any]] | None = None,
) -> list[ProposedClaim]:
    """Deterministic proposals from the user message. Order-stable.

    ``existing`` may be a list or a zero-arg callable returning one; the
    callable is only invoked when candidate patterns actually matched, so a
    message with nothing extractable never triggers a ledger scan.
    """
    out: list[ProposedClaim] = []
    for i, rx in enumerate(_DECISION_RES):
        for m in rx.finditer(message):
            content = _clean(m.group(1))
            if len(content) < 4:
                continue
            out.append(ProposedClaim(
                claim_type="decision",
                content=content,
                subject=_subject_hint(content),
                attribution="user",
                evidence_kind="user-request",
                extractor=f"rule:decision_{i}",
            ))
    for i, rx in enumerate(_FACT_RES):
        for m in rx.finditer(message):
            content = _clean(m.group(0))
            if len(content) < 4:
                continue
            out.append(ProposedClaim(
                claim_type="fact",
                content=content,
                subject=_subject_hint(content),
                attribution="user",
                evidence_kind="receipt",
                extractor=f"rule:fact_{i}",
            ))
    for i, rx in enumerate(_RESEARCH_RES):
        for m in rx.finditer(message):
            content = _clean(m.group(0))
            if len(content) < 4:
                continue
            out.append(ProposedClaim(
                claim_type="research",
                content=content,
                subject=_subject_hint(content),
                attribution="user",
                evidence_kind="receipt",
                extractor=f"rule:research_{i}",
            ))
    if not out:
        return out
    if callable(existing):
        existing = existing()
    return _dedup(out, existing or [])
