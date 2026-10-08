"""Narration gate — every model sentence is checked against TwinState before it renders.

The model never decides facts. Each sentence carries cites into the TwinState;
the gate resolves them, then checks the sentence's numbers, entities, claim
words, URLs — and its clauses — against only what those cites contain.
Anything unsupported is dropped with a reason and never displayed.

Drop reasons: BAD_JSON, CITE_MISSING, NUMBER_MISMATCH, ENTITY_UNSUPPORTED,
UNSUPPORTED_TEXT, CLAIM_WORD, URL_UNSUPPORTED, HEDGE_CLAUSE.  An ungroundable clause
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

# These words express sentence structure, not ledger facts. Domain words must
# come from a cited value or from the cited TwinState field's name.
_GROUNDING_GLUE = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "on", "at", "to", "from", "in", "for", "and", "or", "but", "as",
    "says", "said", "there", "it", "this", "that", "with", "over", "under",
    "by", "across", "has", "have", "had", "one", "two", "first", "last", "since",
})

_FIELD_GROUNDING_ALIASES = {
    "record_count": {"record", "records", "memory", "memories", "ledger"},
    "coverage_index": {"coverage"},
    "active_projects": {"area", "areas", "tag", "tags"},
    "recent_accomplishments": {"latest", "recent", "recorded", "record", "note", "accomplishment"},
    "open_risks": {"unresolved", "conflict", "conflicts", "risk", "risks", "tag", "tags", "tagged"},
    "stale_commitments": {"oldest", "stale", "task", "tasks", "idle"},
    "days_since_update": {"idle"},
}


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


def _grounding_check(text: str, cites: list[str], cited_values: list[Any],
                     empty_cites: set[str], state: dict) -> str | None:
    """Reject domain words absent from cited values and the cited field labels.

    This intentionally fails closed on free-form paraphrase: the gate has no
    semantic entailment model, so allowing arbitrary prose would turn a cite
    into a blank check. Only a small list of summary-field aliases and grammar
    words can be added around text that is actually present in the evidence.
    """
    supported: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)
        elif isinstance(value, str):
            supported.update(w.lower() for w in _WORD_RE.findall(value))

    for value in cited_values:
        collect(value)

    cite_fields: set[str] = set()
    for cite in cites:
        parts = [m.group(1).lower() for m in _CITE_TOKEN_RE.finditer(cite)]
        cite_fields.update(parts)
        supported.update(w for part in parts
                         for w in re.split(r"[_\W]+", part.lower()) if w)
        for part in parts:
            supported.update(_FIELD_GROUNDING_ALIASES.get(part, set()))

    # This exact narrator footer is a static process description, not a claim
    # inferred from a memory value.
    footer = re.fullmatch(
        r"Computed deterministically from \d+(?:\.\d+)? records; "
        r"nothing here is a model claim\.", text.strip(), re.IGNORECASE)
    if footer and {"record_count", "schema"}.issubset(cite_fields):
        return None

    words = [w.lower() for w in _WORD_RE.findall(_strip_markup(text))]
    # A negative summary is supported only by an explicitly cited empty
    # risk/conflict collection; ordinary positive records cannot support it.
    risk_collection_empty = any(
        cite in empty_cites and any(
            m.group(1).lower() in {"open_risks", "conflicts"}
            for m in _CITE_TOKEN_RE.finditer(cite))
        for cite in cites)

    for word in words:
        # Numeric validity was checked immediately above using the more
        # permissive rounded/percentage matcher.
        if re.fullmatch(r"\d+(?:\.\d+)?", word):
            continue
        if word in supported or word in _GROUNDING_GLUE:
            continue
        if word in {"no", "none"} and risk_collection_empty:
            continue
        if any(_inflection(word, base) for base in supported if len(base) >= 3):
            continue
        return "UNSUPPORTED_TEXT"

    # Vocabulary overlap alone cannot establish a relation ("Bob defeated
    # Alice" vs. cited "Alice defeated Bob"). After screening vocabulary,
    # accept only verbatim evidence or one of the narrator's closed render
    # forms whose values are checked against the cited field.
    if not _supported_render_form(text, cites, empty_cites, state):
        return "UNSUPPORTED_TEXT"
    return None


def _normalized_words(text: str) -> str:
    return " ".join(_WORD_RE.findall(_strip_markup(text).lower()))


def _supports_field_number(field: str, literal: str, cites: list[str], state: dict) -> bool:
    numbers: list[float] = []
    for cite in cites:
        if cite == field:
            found, values = _resolve_cite(cite, state)
            if found:
                numbers.extend(_cited_numbers(values))
    return bool(numbers) and _number_supported(literal, numbers)


def _supported_render_form(text: str, cites: list[str], empty_cites: set[str],
                           state: dict) -> bool:
    clean = _strip_markup(text).strip()
    clean = re.sub(r"\s*\[[A-Za-z0-9][A-Za-z0-9_.-]*\]", "", clean).strip()
    normalized = _normalized_words(clean)
    cite_set = set(cites)

    # Verbatim extraction from one cited string, with only familiar narrator
    # labels surrounding it. This preserves source wording without paraphrase.
    for cite in cites:
        found, values = _resolve_cite(cite, state)
        if not found:
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            source = _normalized_words(value)
            extract = normalized.removeprefix("the ")
            extract_at = source.find(extract) if extract else -1
            preceding = source[:extract_at].split() if extract_at >= 0 else []
            polarity_preserved = not preceding or preceding[-1] not in {
                "not", "no", "never", "without", "neither", "nor",
            }
            if source and (normalized == source or normalized.endswith(" " + source)
                           or (extract_at >= 0 and polarity_preserved)):
                prefix = normalized[:-len(source)].strip()
                if prefix in {"", "one record says", "latest note",
                              "latest recorded accomplishment", "risk"}:
                    return True

    # Coverage/count templates use only the number and field cited.
    m = re.fullmatch(r"Coverage(?: index)?(?: is)? (\d+(?:\.\d+)?%?)\.?", clean, re.I)
    if m and "coverage_index" in cite_set:
        return _supports_field_number("coverage_index", m.group(1), cites, state)
    m = re.fullmatch(r"There are (\d+(?:\.\d+)?) memories(?: on the ledger)?\.?", clean, re.I)
    if m and "record_count" in cite_set:
        return _supports_field_number("record_count", m.group(1), cites, state)
    m = re.fullmatch(
        r"Coverage index (\d+(?:\.\d+)?) over (\d+(?:\.\d+)?) ledger memories\.?",
        clean, re.I)
    if m and {"coverage_index", "record_count"}.issubset(cite_set):
        return (_supports_field_number("coverage_index", m.group(1), cites, state)
                and _supports_field_number("record_count", m.group(2), cites, state))

    # Fixed-field summaries require the rendered value to equal the cited one.
    m = re.fullmatch(r"Weakest component is (.+?)\.?", clean, re.I)
    if m and "weakest_component" in cite_set:
        found, values = _resolve_cite("weakest_component", state)
        return found and any(_normalized_words(m.group(1)) == _normalized_words(str(v))
                             for v in values)
    m = re.fullmatch(r"The (.+?) tag is active\.?", clean, re.I)
    if m and "active_projects" in cite_set:
        found, values = _resolve_cite("active_projects", state)
        return found and any(_normalized_words(m.group(1)) == _normalized_words(str(v))
                             for v in values)
    m = re.fullmatch(r"Active areas: (.+?)\.?", clean, re.I)
    if m and "active_projects" in cite_set:
        found, values = _resolve_cite("active_projects", state)
        rendered = [part.strip() for part in m.group(1).split(",")]
        return found and rendered == [str(v) for v in values[:8]]

    m = re.fullmatch(r"Unresolved conflict on (.+?) across records (.+?)\.?", clean, re.I)
    if m:
        for cite in cites:
            index = re.fullmatch(r"open_risks\[(\d+)\]\.subject", cite)
            if not index:
                continue
            subject_path = cite
            ids_path = f"open_risks[{index.group(1)}].record_ids"
            if ids_path not in cite_set:
                continue
            item_index = int(index.group(1))
            risks = state.get("open_risks", [])
            if (not isinstance(risks, list) or item_index >= len(risks)
                    or risks[item_index].get("kind") != "conflict"):
                continue
            found_subject, subjects = _resolve_cite(subject_path, state)
            found_ids, ids = _resolve_cite(ids_path, state)
            rendered_ids = [part.strip() for part in m.group(2).split(",")]
            if (found_subject and found_ids and str(subjects[0]) == m.group(1)
                    and rendered_ids == [str(value) for value in ids]):
                return True

    m = re.fullmatch(r"Oldest stale task on (.*?) idle (\d+(?:\.\d+)?) days\.?", clean, re.I)
    if m and {"stale_commitments[0].subject", "stale_commitments[0].days_since_update"}.issubset(cite_set):
        found_subject, subjects = _resolve_cite("stale_commitments[0].subject", state)
        found_days, days = _resolve_cite("stale_commitments[0].days_since_update", state)
        return (found_subject and found_days and str(subjects[0]) == m.group(1)
                and _supports_field_number("stale_commitments[0].days_since_update", m.group(2), cites, state))

    if clean == "No unresolved conflicts or tagged risks." and "open_risks" in empty_cites:
        return True
    if (re.fullmatch(r"Computed deterministically from \d+(?:\.\d+)? records; "
                     r"nothing here is a model claim\.", clean, re.I)
            and {"record_count", "schema"}.issubset(cite_set)):
        return True

    # Static disclaimer and recommendation sentences are exact cited values.
    for field in ("disclaimer", "recommended_mission"):
        if field in cite_set:
            found, values = _resolve_cite(field, state)
            if found and any(_normalized_words(clean) == _normalized_words(str(v)) for v in values):
                return True
    return False


def _clauses(sentence: str) -> list[str]:
    parts = _CLAUSE_SPLIT_RE.split(sentence)
    return [p.strip() for p in parts if p and p.strip()]


def _check_sentence(text: str, cites: list[str], state: dict) -> str | None:
    """Returns None if the sentence survives, else the drop reason."""
    if not cites:
        return "CITE_MISSING"
    cited_values: list[Any] = []
    empty_cites: set[str] = set()
    for c in cites:
        found, values = _resolve_cite(c, state)
        if not found:
            return "CITE_MISSING"
        if not values:
            empty_cites.add(c)
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
    grounding = _grounding_check(clean, cites, cited_values, empty_cites, state)
    if grounding is not None:
        return grounding
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
