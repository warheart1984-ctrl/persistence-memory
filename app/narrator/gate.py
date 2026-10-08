"""Narration gate — every model sentence is checked against TwinState before it renders.

The model never decides facts. Each sentence carries cites into the TwinState;
the gate resolves them, then checks the sentence's numbers, entities, claim
words, URLs — and its clauses — against only what those cites contain.
Anything unsupported is dropped with a reason and never displayed.

Drop reasons: BAD_JSON, CITE_MISSING, NUMBER_MISMATCH, ENTITY_UNSUPPORTED,
CLAIM_WORD, URL_UNSUPPORTED, HEDGE_CLAUSE.  An ungroundable clause
(hedged/speculative tail) is HEDGE_CLAUSE — a first-class reason, not an
entity mismatch in disguise.

Clause rule (ported from cslm-genesis): a hypothetical marker scopes rightward
— it can never rescue the asserted text before it.  A sentence is therefore
split at hedge boundaries; a clause carrying a hedge marker is ungroundable
and drops the whole sentence (a dropped mid-sentence clause would leave
broken grammar behind).
"""

from __future__ import annotations

import json
import re
from typing import Any

SECTIONS = ("assessment", "opportunity", "risk", "next_action", "explanation")

_CLAIM_WORDS = frozenset({
    "proven", "verified", "complete", "secure", "guaranteed",
    "merged", "deployed", "fixed",
})

_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[._\-][A-Za-z0-9]+)*")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```")
_MARKUP_RE = re.compile(r"[*_`~]|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")
_TAG_RE = re.compile(r"<[^>]+>")

_HEDGE_RE = re.compile(
    r"\b(?:let'?s|let us|imagine|suppose|supposedly|assuming|assume|"
    r"hypothetically|probably|perhaps|maybe|hopefully|presumably|"
    r"i think|we think|it seems|seems like)\b",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r",\s+(?:and|but|so|yet|while|because|whereas|if)\b|[;—]| -- ")

_CITE_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[\d+\])?")


def _strip_markup(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    text = _MARKUP_RE.sub(lambda m: m.group(1) or " ", text)
    return text


def _resolve_cite(path: str, state: dict) -> tuple[bool, list[Any]]:
    """Resolve 'a.b[2].c' against the state; returns (found, leaf values)."""
    node: Any = state
    for m in _CITE_TOKEN_RE.finditer(path.strip()):
        name, idx = m.group(1), m.group(2)
        if not isinstance(node, dict) or name not in node:
            return False, []
        node = node[name]
        if idx is not None:
            i = int(idx[1:-1])
            if not isinstance(node, list) or i >= len(node):
                return False, []
            node = node[i]
    values: list[Any] = []

    def _walk(v: Any) -> None:
        if isinstance(v, dict):
            for x in v.values():
                _walk(x)
        elif isinstance(v, list):
            for x in v:
                _walk(x)
        else:
            values.append(v)

    _walk(node)
    return True, values


def _cited_numbers(values: list[Any]) -> list[float]:
    out: list[float] = []
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(float(v))
        elif isinstance(v, str):
            out.extend(float(t) for t in _NUM_RE.findall(v.replace("%", "")))
    return out


def _number_supported(token: str, cited_nums: list[float]) -> bool:
    is_pct = token.endswith("%")
    n = float(token.rstrip("%"))
    for c in cited_nums:
        if n == c:
            return True
        if abs(n - c) <= 5e-3:
            return True
        if round(c, 2) == n or round(c) == n:
            return True
        if is_pct and (abs(n / 100 - c) <= 5e-3 or abs(n - c * 100) <= 0.5):
            return True
        if not is_pct and abs(n * 100 - c) <= 5e-3:  # 0.4 written as 40 by the model
            return True
    return False


def _state_entities(state: dict) -> list[str]:
    """Ids, subjects, tags, project names — the mentionable surface."""
    ents: list[str] = []
    for k in ("recent_accomplishments", "stale_commitments"):
        for item in state.get(k, []):
            ents += [item.get("record_id", ""), item.get("subject", "")]
    for r in state.get("open_risks", []):
        ents.append(r.get("record_id", ""))
        ents.append(r.get("subject", ""))
        ents += list(r.get("record_ids", []))
    ents += list(state.get("active_projects", []))
    return [e for e in ents if e]


def _whole_word(needle: str, haystack: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])", haystack))


def _inflection(word: str, base: str) -> bool:
    """Common English inflections of a cited word are not entity attacks."""
    return word in (base + s for s in ("s", "es", "d", "ed", "ing", "er", "est", "ly"))


def _entity_check(text: str, cited_texts: list[str], state: dict) -> str | None:
    """Whole-word entity matching — a cited 'gate' never backs 'gateway'."""
    cited_blob = " ".join(cited_texts)
    cited_words = {w.lower() for t in cited_texts for w in _WORD_RE.findall(t)}

    # (a) every state entity mentioned must be inside a cited value
    for ent in _state_entities(state):
        if _whole_word(ent, text) and not _whole_word(ent, cited_blob):
            return ent

    # (b) sentence words that merely CONTAIN a cited word are not supported —
    # e.g. a cited tag 'gate' must not launder the sentence word 'gateway'.
    # Ordinary inflections (task/tasks) are exempt; anything else must be
    # whole-word or exactly cited.
    for w in {w.lower() for w in _WORD_RE.findall(text)}:
        if len(w) < 4 or w in cited_words:
            continue
        if any(len(v) >= 3 and v != w and v in w and not _inflection(w, v)
               for v in cited_words):
            return w
    return None


def _clauses(sentence: str) -> list[str]:
    parts = _CLAUSE_SPLIT_RE.split(sentence)
    return [p.strip() for p in parts if p and p.strip()]


def _check_sentence(text: str, cites: list[str], state: dict) -> str | None:
    """Returns None if the sentence survives, else the drop reason."""
    if not cites:
        return "CITE_MISSING"
    cited_values: list[Any] = []
    for c in cites:
        found, values = _resolve_cite(c, state)
        if not found:
            return "CITE_MISSING"
        cited_values += values
    cited_texts = [v for v in cited_values if isinstance(v, str)]

    clean = _strip_markup(text)

    if _URL_RE.search(clean):
        for u in _URL_RE.findall(clean):
            if not any(_whole_word(u, t) or u in t for t in cited_texts):
                return "URL_UNSUPPORTED"

    # Clause-level: a hedged tail can never rescue — and speculation can't
    # ship.  Hedge words inside verbatim-quoted cited values are *data*
    # (a record may legitimately say "assume"), so they are stripped first;
    # a hedge marker the model wrote itself drops the whole sentence.
    for clause in _clauses(clean):
        remainder = clause
        for t in cited_texts:
            remainder = remainder.replace(t, " ")
        if _HEDGE_RE.search(remainder):
            return "HEDGE_CLAUSE"

    cited_nums = _cited_numbers(cited_values)
    for tok in _NUM_RE.findall(clean):
        if not _number_supported(tok, cited_nums):
            return "NUMBER_MISMATCH"

    for w in _WORD_RE.findall(clean):
        if w.lower() in _CLAIM_WORDS:
            if not any(_whole_word(w.lower(), t.lower()) for t in cited_texts):
                return "CLAIM_WORD"

    ent = _entity_check(clean, cited_texts, state)
    if ent is not None:
        return "ENTITY_UNSUPPORTED"
    return None


def extract_json(raw: str) -> Any | None:
    text = _FENCE_RE.sub("", raw.strip())
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # tolerate leading/trailing prose around the object
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def gate_narration(raw: str, state: dict) -> dict[str, Any]:
    """Gate raw model text against the TwinState.

    Returns {ok, bad_json, sections: {name: [{text, cites}]}, dropped: [...]}.
    ``ok`` means the raw output was valid JSON with the five sections —
    individual sentences may still have been dropped.
    """
    data = extract_json(raw)
    dropped: list[dict[str, Any]] = []
    if not isinstance(data, dict) or not isinstance(data.get("sections"), dict):
        return {"ok": False, "bad_json": True, "sections": {}, "dropped": [
            {"section": "*", "index": 0, "reason": "BAD_JSON"}
        ]}

    out_sections: dict[str, list[dict[str, Any]]] = {s: [] for s in SECTIONS}
    for name in SECTIONS:
        items = data["sections"].get(name)
        if not isinstance(items, list):
            items = []
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                dropped.append({"section": name, "index": i, "reason": "BAD_JSON"})
                continue
            cites = item.get("cites")
            cites = [c for c in cites if isinstance(c, str)] if isinstance(cites, list) else []
            reason = _check_sentence(item["text"], cites, state)
            if reason is None:
                out_sections[name].append({"text": item["text"], "cites": cites})
            else:
                dropped.append({"section": name, "index": i, "reason": reason})
    return {"ok": True, "bad_json": False, "sections": out_sections, "dropped": dropped}
