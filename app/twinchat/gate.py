"""Chat-surface clause gate — mechanical filter, not semantic verification.

Reuse, don't fork: every reply sentence is checked by the SAME
``_check_sentence`` that gates narrator output (clause splitting,
HEDGE_CLAUSE, NUMBER_MISMATCH, CLAIM_WORD, whole-word ENTITY_UNSUPPORTED,
UNSUPPORTED_TEXT, URL_UNSUPPORTED). The pseudo-state is a recall bundle shaped so
``_state_entities`` enumerates the whole mentionable surface; inline
``[id]`` markers cite ``recalled[i]`` paths (real ids contain '-', which
cite-path tokens cannot express — list indexes can).

What this is NOT: a general semantic entailment model. The shared gate now
accepts cited source spans and a finite set of narrator render forms whose
values are checked against their cited TwinState fields; free-form paraphrase
is dropped. Citations remain evidence handles, not proof that a stored record
is true.
"""

from __future__ import annotations

import re

from app.narrator.gate import _check_sentence

from .models import DropFinding

# [m-xxxx] / [id] style inline markers; brackets make intent explicit.
_CITE_RE = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9_.\-]*)\]")

# Sentence boundary: '.', '!', '?' followed by whitespace/end, or a newline.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def pseudo_state(recalled: list[dict]) -> dict:
    """Shape the bundle so _state_entities sees every id/subject/tag."""
    state_items = [
        {"record_id": item["id"], "subject": item.get("subject") or ""}
        for item in recalled
    ]
    return {
        "recalled": recalled,
        "recent_accomplishments": state_items,
        "stale_commitments": state_items,
        "open_risks": [
            {"record_id": i["id"], "subject": i.get("subject") or "", "record_ids": [i["id"]]}
            for i in recalled
        ],
        "active_projects": [
            tag for item in recalled for tag in (item.get("tags") or [])
        ],
    }


_CITE_ONLY_RE = re.compile(rf"^(?:{_CITE_RE.pattern}|\s|[^\w])*$")


def split_sentences(text: str) -> list[str]:
    raw = [s.strip() for s in _SENT_SPLIT_RE.split(text.strip()) if s and s.strip()]
    out: list[str] = []
    for frag in raw:
        # A fragment of only [id] markers annotates the sentence before it —
        # the natural "claim. [id]" pattern must not orphan its cite.
        if out and _CITE_ONLY_RE.match(frag):
            out[-1] = out[-1] + " " + frag
        else:
            out.append(frag)
    return out


def gate_reply(text: str, recalled: list[dict]) -> dict:
    """Gate a raw reply string against the recall bundle it was shown.

    Returns {reply, kept, dropped, all_dropped} where ``reply`` is the
    enforce-filtered output (identical bytes under 'shadow'; 'off' never
    reaches this function — it skips the model call entirely).
    """
    state = pseudo_state(recalled)
    by_id = {item["id"]: i for i, item in enumerate(recalled)}
    kept: list[str] = []
    dropped: list[DropFinding] = []

    for i, sentence in enumerate(split_sentences(text)):
        markers = _CITE_RE.findall(sentence)
        indexes: list[int] = []
        bad = False
        for marker in markers:
            if marker not in by_id:
                bad = True
                break
            indexes.append(by_id[marker])
        if bad or not indexes:
            # No cite at all, or a cite to an un-recalled id — a hallucinated
            # memory id and a bare assertion die by the same code.
            dropped.append(DropFinding(index=i, reason="CITE_MISSING"))
            continue
        cites: list[str] = []
        for idx in indexes:
            # id is cited too: mentioning [m-x] must resolve to a cited value,
            # and the entity check treats every recalled id as mentionable.
            cites += [
                f"recalled[{idx}].id",
                f"recalled[{idx}].content",
                f"recalled[{idx}].subject",
            ]
        reason = _check_sentence(sentence, cites, state)
        if reason is None:
            kept.append(sentence)
        else:
            dropped.append(DropFinding(index=i, reason=reason))

    sentences = split_sentences(text)
    return {
        "reply": " ".join(kept),
        "kept": kept,
        "dropped": dropped,
        "all_dropped": bool(sentences) and not kept,
    }
