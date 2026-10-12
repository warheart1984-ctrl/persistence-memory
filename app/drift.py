"""Record-vs-summary drift watcher.

An agent summarizes ledger records; a human used to be the only one who could notice the summary was wrong (an agent once credited Codex
with a deploy Claude did, while the ledger was right).  This module takes the summary text and the ids of the records it was written
from, and checks every factual claim in it against those records.  It never writes to the ledger and uses no model: every verdict is a
deterministic rule over text, so the same input always gives the same findings and the same ``check_digest``.

Per claim:  supported | contradicted | unsupported.
  * contradicted  - a record field disagrees (the finding names the record, the field, the value claimed and the value found);
  * unsupported   - nothing in the cited records backs it (this is NOT "false": it means the records cannot confirm it);
  * supported     - the records back it and nothing contradicts it.
Overall verdict: ``drift`` (any contradiction), ``unverified`` (no contradiction, but something unsupported), ``pass`` (all supported).

The technique is borrowed from the twin's narrator gate (clause-level checks, whole-word matching, drop reasons such as
ENTITY_UNSUPPORTED / NUMBER_MISMATCH / CITE_MISSING / CLAIM_WORD) but this is new code for a different input: free text plus record ids,
with no typed state to cite into.  Nothing here is imported from the twin repository.

Honest limits (also in docs/drift_watcher.md): it knows only the rules below, so paraphrase it cannot map, misleading omissions, pronouns
and relative times ("yesterday") are outside it; ``unsupported`` is a false-negative-prone class by design; and it cannot know what an agent
was actually shown, only what the records say now (the call log proves a call and its hashes, not the text served).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from app import call_log

MAX_SUMMARY_CHARS = 20_000
MAX_RECORDS = 20
MAX_CLAIMS = 200
CHECK_VERSION = 1
KNOWN_AGENTS = ("codex", "claude", "devin", "opencode", "cursor", "kilo", "grok", "gemini", "copilot")

SUPPORTED, CONTRADICTED, UNSUPPORTED = "supported", "contradicted", "unsupported"
VERDICT_PASS, VERDICT_DRIFT, VERDICT_UNVERIFIED = "pass", "drift", "unverified"


class DriftInputError(Exception):
    """A request the checker refuses outright (never quietly shortened): carries a stable ``reason``."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


# ------------------------------------------------------------------------------------------------------------- config


def enabled() -> bool:
    """JARVIS_DRIFT_CHECK_ENABLED wins; unset means on in dev and test, off when JARVIS_ENV=production (a deploy turns it on explicitly)."""
    raw = os.environ.get("JARVIS_DRIFT_CHECK_ENABLED")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return (os.environ.get("JARVIS_ENV") or "").strip().lower() != "production"


def drift_log_dir() -> Path:
    explicit = (os.environ.get("JARVIS_DRIFT_LOG_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    store = (os.environ.get("JARVIS_STORE_PATH") or "").strip()
    return (Path(store).parent if store else Path("data")) / "drift-log"


_logs: dict[str, call_log.CallLog] = {}


def get_drift_log() -> call_log.CallLog:
    key = str(drift_log_dir().resolve())
    if key not in _logs:
        _logs[key] = call_log.CallLog(Path(key), file_prefix="drift")
    return _logs[key]


def reset_for_tests() -> None:
    _logs.clear()


# ------------------------------------------------------------------------------------------------------------ helpers

_STEM_SUFFIXES = ("ing", "ed", "es", "s", "ly")


def _stem(word: str) -> str:
    w = word.lower()
    for suf in _STEM_SUFFIXES:
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_ID_RE = re.compile(r"\bmem-[0-9a-f]{6,64}\b", re.I)
_COMMIT_RE = re.compile(r"(?<![0-9a-zA-Z_\-])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![0-9a-zA-Z_\-])", re.I)
_SHA256_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{64}(?![0-9a-fA-F])", re.I)
_DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
_NUM_RE = re.compile(r"(?<![0-9A-Za-z\-])v?(\d+(?:\.\d+)?)(?![0-9A-Za-z\-])")
_MARKUP_RE = re.compile(r"[*`~#>]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])|\[([^\]]*)\]\([^)]*\)")
_QUOTED_RE = re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d')
_STOP = frozenset("""a an the and or but so yet if then than that this these those there here it its it's is are was were be been being am
am has have had do does did will would can could should may might must shall of in on at by for from to with without into onto over under about
after before during while as also just only very more most some any each every such own same other another both either neither again further
once i we you he she they them their his her our your my me us who whom which what when where why how all not no nor""".split())
# "not/no/nor" are listed above ONLY so they are skipped as filler by default; negation is handled separately and must be backed by the record.
_NEGATIONS = frozenset({"not", "never", "no", "none", "cannot", "can't", "didn't", "wasn't", "isn't", "doesn't", "without"})
_HEDGE_RE = re.compile(r"\b(likely|probably|possibly|perhaps|maybe|might|may|seems?|appears?|presumably|apparently|i think|i believe|suggests?)\b", re.I)
_OPINION_RE = re.compile(r"^\s*(i think|i believe|in my opinion|my view|imo|we should|you should|please|let's|consider|note that|todo|next step)\b", re.I)
# generic words ABOUT the records themselves: they never need to appear in a record's own text
_GLUE = frozenset("""record records decision decisions entry entries memory memories note notes ledger item one ones first second third new newer newest old older oldest
earlier latest original previous supersede supersedes superseded replace replaces replaced corrects corrected correct overrides supplants status lifecycle field
value claim summary agent hash digest sha sha256 checksum commit date time""".split())
_CLAIM_WORDS = frozenset({"always", "never", "guaranteed", "guarantees", "guarantee", "proves", "proven", "definitely", "certainly", "fully", "completely",
                          "entirely", "everything", "impossible", "100%"})
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(\[])|\n+")
_CLAUSE_SPLIT = re.compile(r",\s+(?:and|but|so|yet|while|because|whereas|if|with)\b|;|—| -- |\s+–\s+")

_VERBS_WRITE = r"(?:wrote|written|writes|created|creates|recorded|stored|stores|saved|saves|authored|logged|filed|posted|added)"
_VERBS_ACTION = (r"(?:deployed|deploys|migrated|migrates|executed|executes|signed|merged|merges|fixed|fixes|built|rotated|"
                 r"restored|restores|drilled|released|shipped|rolled back|installed|enabled|disabled|upgraded|reverted)")
_ADVERB = r"(?:also|then|later|first|successfully|actually|already|originally|just|previously|finally|again|had|has|have|was|were)"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    text = _MARKUP_RE.sub(lambda m: m.group(1) or "", text)
    text = text.replace("→", " to ").replace("➜", " to ").replace("->", " to ").replace("@", " ").replace("’", "'")
    return re.sub(r"[ \t]+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in _WORD_RE.findall(text.lower())}


def _whole_word(needle: str, haystack: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])", haystack, re.I) is not None


# ------------------------------------------------------------------------------------------------------ record views


class RecordView:
    """What the checker reads of one ledger record: stored fields, the derived lifecycle and the searchable text."""

    def __init__(self, record_id: str, found: bool, data: dict[str, Any] | None = None):
        d = data or {}
        self.id, self.found = record_id, found
        self.source_agent = (d.get("source_agent") or "").strip()
        self.status = d.get("status") or ""
        self.supersedes = d.get("supersedes") or None
        self.superseded_by = d.get("superseded_by") or None
        self.type = d.get("type") or ""
        self.subject = d.get("subject") or ""
        self.content = d.get("content") or ""
        self.created_at = str(d.get("created_at") or "")
        self.version = d.get("version")
        self.content_sha256 = d.get("content_sha256") or ""
        self.evidence = list(d.get("evidence") or [])
        self.lifecycle = "archived" if self.status == "archived" else ("superseded" if self.superseded_by else "active")
        parts = [self.subject, self.content] + [f"{e.get('ref', '')} {e.get('note', '')}" for e in self.evidence]
        self.text = "\n".join(p for p in parts if p)
        self.words = _tokens(self.text)
        self.dates = set(_DATE_RE.findall(self.text)) | ({self.created_at[:10]} if re.match(r"20\d\d-\d\d-\d\d", self.created_at) else set())
        self.commits = {m.lower() for m in _COMMIT_RE.findall(self.text)}
        self.sha256s = {m.lower() for m in _SHA256_RE.findall(self.text)} | ({self.content_sha256.lower()} if self.content_sha256 else set())
        scrub = _SHA256_RE.sub(" ", _COMMIT_RE.sub(" ", _DATE_RE.sub(" ", _ID_RE.sub(" ", self.text))))
        self.numbers = set(_NUM_RE.findall(scrub))
        self.number_slots = _number_slots(scrub)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "found": self.found, "version": self.version, "content_sha256": self.content_sha256, "status": self.status,
                "lifecycle": self.lifecycle, "source_agent": self.source_agent, "supersedes": self.supersedes, "superseded_by": self.superseded_by}


def _number_slots(text: str) -> dict[str, set[str]]:
    """number -> the words next to it ("schema 9" -> {'9': {'schema'}}), so a claimed number can be compared in the same slot."""
    out: dict[str, set[str]] = {}
    words = re.findall(r"[A-Za-z]+|v?\d+(?:\.\d+)?", text.lower())
    for i, w in enumerate(words):
        m = re.fullmatch(r"v?(\d+(?:\.\d+)?)", w)
        if not m:
            continue
        near = {x for x in (words[i - 1] if i else None, words[i + 1] if i + 1 < len(words) else None) if x and x.isalpha() and x not in _STOP}
        out.setdefault(m.group(1), set()).update(near)
    return out


def load_records(store: Any, ids: list[str]) -> dict[str, RecordView]:
    """The cited records, read through the (already tenant-scoped) store.  Ids that do not exist for this tenant come back as not found."""
    wanted = set(ids)
    successor: dict[str, tuple[str, str]] = {}
    try:
        everything = store.list_memories(limit=100000)
    except Exception:  # noqa: BLE001 - a store that cannot list still lets us read the cited records one by one
        everything = []
    for m in everything:
        if getattr(m, "supersedes", None) in wanted:
            key = (str(m.created_at), m.id)
            if m.supersedes not in successor or key > successor[m.supersedes]:
                successor[m.supersedes] = key
    out: dict[str, RecordView] = {}
    for rid in ids:
        rec = store.get_memory(rid)
        if rec is None:
            out[rid] = RecordView(rid, False)
            continue
        data = rec.model_dump() if hasattr(rec, "model_dump") else dict(rec)
        data["superseded_by"] = successor[rid][1] if rid in successor else None
        out[rid] = RecordView(rid, True, data)
    return out


# ------------------------------------------------------------------------------------------------ input validation


def validate_request(body: Any) -> dict[str, Any]:
    """Return the cleaned request, or raise DriftInputError.  Anything over a limit is refused with the numbers, never shortened."""
    if not isinstance(body, dict):
        raise DriftInputError("INVALID_REQUEST", "the request body must be a JSON object")
    summary = body.get("summary")
    provided = body.get("claims")
    if summary is None and provided is None:
        raise DriftInputError("SUMMARY_REQUIRED", "give a summary (text) or claims")
    if summary is not None:
        if not isinstance(summary, str) or not summary.strip():
            raise DriftInputError("SUMMARY_EMPTY", "summary must be a non-empty string")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise DriftInputError("SUMMARY_TOO_LONG", f"summary is {len(summary)} characters; the limit is {MAX_SUMMARY_CHARS}. Nothing was checked.")
    records = body.get("records")
    if not isinstance(records, list) or not records or not all(isinstance(r, str) and re.fullmatch(r"[A-Za-z0-9_.:\-]{1,128}", r) for r in records):
        raise DriftInputError("RECORDS_REQUIRED", "records must be a non-empty list of record ids")
    ids = list(dict.fromkeys(records))
    if len(ids) > MAX_RECORDS:
        raise DriftInputError("TOO_MANY_RECORDS", f"{len(ids)} records were given; the limit is {MAX_RECORDS}. Nothing was checked.")
    claims: list[dict[str, Any]] | None = None
    if provided is not None:
        if not isinstance(provided, list) or not all(isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip() for c in provided):
            raise DriftInputError("INVALID_CLAIMS", "claims must be a list of {text, cites?} objects with non-empty text")
        claims = [{"text": c["text"].strip(), "cites": [x for x in (c.get("cites") or []) if isinstance(x, str)]} for c in provided]
    served_hashes = body.get("served_content_sha256") or {}
    if not isinstance(served_hashes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in served_hashes.items()):
        raise DriftInputError("INVALID_REQUEST", "served_content_sha256 must map record ids to hashes")
    return {"summary": summary, "records": ids, "claims": claims, "client": str(body.get("client") or "")[:128] or None,
            "served_digest": str(body["served_digest"]) if body.get("served_digest") else None, "served_content_sha256": served_hashes}


# ---------------------------------------------------------------------------------------------- claim extraction


def extract_claims(summary: str) -> list[dict[str, Any]]:
    """Clause-level claims in reading order.  Questions, opinions and fragments are skipped (and counted), never silently dropped."""
    claims: list[dict[str, Any]] = []
    for sentence in _SENT_SPLIT.split(_clean(summary)):
        sentence = sentence.strip(" -•\t")
        if not sentence:
            continue
        for clause in _CLAUSE_SPLIT.split(sentence):
            clause = clause.strip(" ,.:-•\t")
            if clause:
                claims.append({"text": clause, "cites": _ID_RE.findall(clause)})
    return claims


def _skip_reason(text: str) -> str | None:
    if text.endswith("?"):
        return "QUESTION"
    if _OPINION_RE.match(text):
        return "OPINION_OR_INSTRUCTION"
    if len(_WORD_RE.findall(text)) < 3 and not (_ID_RE.search(text) or _COMMIT_RE.search(text)):
        return "FRAGMENT"
    return None


# --------------------------------------------------------------------------------------------------------- checks


def _finding(status: str, reason: str, *, record: str | None = None, field: str | None = None, claimed: Any = None, found: Any = None,
             detail: str = "", evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"status": status, "reason": reason, "record": record, "field": field, "claimed": claimed, "found": found, "detail": detail, "evidence": evidence or []}


def _snippet(text: str, needle: str, width: int = 70) -> str:
    m = re.search(re.escape(needle), text, re.I)
    if not m:
        return text[: 2 * width]
    lo, hi = max(0, m.start() - width), min(len(text), m.end() + width)
    return ("…" if lo else "") + text[lo:hi].replace("\n", " ") + ("…" if hi < len(text) else "")


def _agent_names(records: list[RecordView]) -> list[str]:
    names = list(KNOWN_AGENTS)
    for r in records:
        sa = r.source_agent.lower()
        if sa and sa not in names and re.fullmatch(r"[a-z][a-z0-9_\-]{1,30}", sa):
            names.append(sa)
    return names


def check_actor(text: str, scope: list[RecordView], names: list[str]) -> list[dict[str, Any]]:
    """Who did it: authorship (source_agent) answers 'who wrote the record', the content answers 'who did the action'."""
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    active = re.compile(rf"\b(?P<actor>{alt})\b(?:'s)?(?P<mid>(?:\s+{_ADVERB}){{0,2}})\s+(?P<verb>{_VERBS_WRITE}|{_VERBS_ACTION})\b", re.I)
    passive = re.compile(rf"\b(?P<verb>{_VERBS_WRITE}|{_VERBS_ACTION})\b(?:\s+\w+){{0,6}}?\s+by\s+(?P<actor>{alt})\b", re.I)
    claims: list[tuple[str, str]] = []
    for rx in (active, passive):
        for m in rx.finditer(text):
            claims.append((m.group("actor").lower(), re.sub(r"\s+", " ", m.group("verb").lower())))
    out: list[dict[str, Any]] = []
    for actor, verb in dict.fromkeys(claims):
        is_write = re.fullmatch(_VERBS_WRITE, verb) is not None
        verdicts: list[dict[str, Any]] = []
        for r in scope:
            if not r.found:
                continue
            sa = r.source_agent.lower()
            if is_write:
                ev = [{"record": r.id, "field": "source_agent", "value": r.source_agent}]
                if sa == actor or (sa and actor in sa):
                    verdicts.append({**_finding(SUPPORTED, "ACTOR_SUPPORTED", record=r.id, field="source_agent", claimed=actor, found=r.source_agent, evidence=ev,
                                                detail=f"{r.id} was written by {r.source_agent}"), "words": [actor, *verb.split(), "by"]})
                else:
                    verdicts.append(_finding(CONTRADICTED, "ACTOR_CONTRADICTED", record=r.id, field="source_agent", claimed=actor, found=r.source_agent, evidence=ev,
                                             detail=f"the summary says {actor} {verb} it, but source_agent is {r.source_agent}"))
                continue
            named = {n.lower() for n in re.findall(rf"\bby\s+({alt})\b", r.content, re.I)}
            named |= {m.group(1).lower() for m in re.finditer(rf"\b({alt})\b(?:\s+{_ADVERB}){{0,2}}\s+{_VERBS_ACTION}\b", r.content, re.I)}
            if actor in named:
                verdicts.append({**_finding(SUPPORTED, "ACTOR_SUPPORTED", record=r.id, field="content", claimed=actor, found=sorted(named),
                                            evidence=[{"record": r.id, "field": "content", "value": _snippet(r.content, actor)}]), "words": [actor, *verb.split(), "by"]})
            elif named:
                other = sorted(named)[0]
                ev = [{"record": r.id, "field": "source_agent", "value": r.source_agent}, {"record": r.id, "field": "content", "value": _snippet(r.content, other)}]
                verdicts.append(_finding(CONTRADICTED, "ACTOR_CONTRADICTED", record=r.id, field="content", claimed=actor, found=sorted(named), evidence=ev,
                                         detail=f"the summary says {actor} {verb} it; the record says it was done by {', '.join(sorted(named))} (source_agent {r.source_agent or 'unknown'})"))
            else:
                ev = [{"record": r.id, "field": "source_agent", "value": r.source_agent}]
                extra = " (the claim may be confusing who wrote the record with who did the action)" if sa and sa == actor else ""
                verdicts.append(_finding(UNSUPPORTED, "ACTOR_AUTHORSHIP_ONLY", record=r.id, field="source_agent", claimed=actor, found=r.source_agent, evidence=ev,
                                         detail=f"{r.id} records who WROTE it (source_agent {r.source_agent or 'unknown'}); its content does not say who {verb} it{extra}"))
        out.extend(_pick(verdicts))
    return out


def _pick(verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Several records in scope: any contradiction wins, else any support, else the unsupported findings."""
    for status in (CONTRADICTED, SUPPORTED):
        hit = [v for v in verdicts if v["status"] == status]
        if hit:
            return hit
    return verdicts[:1]


_STATUS_WORDS = ("draft", "verified", "archived", "active", "superseded")
_STATUS_KEYED = re.compile(r"\bstatus\b[^\w\n]{0,6}(?:is|was|=|:)?\s*(?:still\s+|now\s+)?(?P<w>draft|verified|archived|active|superseded)\b|\b(?P<w2>draft|verified|archived|active|superseded)\s+status\b", re.I)
_STATUS_BARE = re.compile(r"\b(?:is|was|are|were|remains|stays|remain|marked|shown as|listed as)\s+(?:still\s+|now\s+|currently\s+|only\s+)?(?P<w>draft|verified|archived|active|superseded)\b(?!\s+by\b)|\ba\s+(?P<w2>draft|verified|archived)\s+(?:decision|record|memory|fact|note)\b", re.I)


def check_status(text: str, scope: list[RecordView], by_id: dict[str, RecordView] | None = None) -> list[dict[str, Any]]:
    """A status claim is checked against the record it is about: the nearest id named before the phrase, else every record in scope
    (ambiguous: supported if any record has that value, contradicted only if none does)."""
    claims: list[tuple[str, str, int]] = []  # (field, word, position)
    for m in _STATUS_KEYED.finditer(text):
        claims.append(("status", (m.group("w") or m.group("w2")).lower(), m.start()))
    for m in _STATUS_BARE.finditer(text):
        w = (m.group("w") or m.group("w2")).lower()
        if any(f == "status" and word == w for f, word, _ in claims):
            continue
        claims.append(("lifecycle" if w in ("active", "superseded") else "status", w, m.start()))
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for field, word, pos in claims:
        before = [i.lower() for i in _ID_RE.findall(text[:pos])]
        bound = by_id.get(before[-1]) if (by_id and before and before[-1] in by_id and by_id[before[-1]].found) else None
        targets = [bound] if bound else [r for r in scope if r.found]
        key = (field, word, bound.id if bound else "*")
        if key in seen:
            continue
        seen.add(key)
        verdicts = []
        for r in targets:
            actual = r.status if field == "status" else r.lifecycle
            ev = [{"record": r.id, "field": field, "value": actual}]
            reason = "STATUS_CONTRADICTED" if field == "status" else "LIFECYCLE_CONTRADICTED"
            if actual == word:
                verdicts.append({**_finding(SUPPORTED, reason.replace("CONTRADICTED", "SUPPORTED"), record=r.id, field=field, claimed=word, found=actual, evidence=ev), "words": [word, "status", "lifecycle"]})
            else:
                verdicts.append(_finding(CONTRADICTED, reason, record=r.id, field=field, claimed=word, found=actual, evidence=ev,
                                         detail=f"the summary says {field} is {word}; {r.id} has {field} {actual or 'unset'}"))
        if bound:
            out.extend(verdicts)
        else:  # ambiguous subject: one matching record is enough to support it
            hit = [v for v in verdicts if v["status"] == SUPPORTED]
            out.extend(hit[:1] or verdicts[:1])
    return out


_NEW_ROLE = r"(?:the\s+(?:new|newer|newest|replacement|latest|updated)(?:\s+(?:record|decision|one|entry|memory))?)"
_OLD_ROLE = r"(?:the\s+(?:old|older|oldest|original|earlier|previous|first)(?:\s+(?:record|decision|one|entry|memory))?)"
_ENDPOINT = rf"(?:mem-[0-9a-f]{{6,64}}|{_NEW_ROLE}|{_OLD_ROLE}|(?:the\s+)?(?:\w+)['’]s\s+(?:record|decision|entry|memory)|the\s+\w+\s+(?:record|decision|entry|memory))"
_SUP_ACTIVE = re.compile(rf"(?P<a>{_ENDPOINT})\s+(?:now\s+)?(?:supersedes|superseded|replaces|replaced|corrects|corrected|overrides|overrode|supplants|supplanted)\s+(?P<b>{_ENDPOINT})", re.I)
_SUP_PASSIVE = re.compile(rf"(?P<b>{_ENDPOINT})\s+(?:is|was|has been|had been)\s+(?:now\s+)?(?:superseded|replaced|corrected|overridden|supplanted)\s+by\s+(?P<a>{_ENDPOINT})", re.I)


def _resolve_endpoint(term: str, by_id: dict[str, RecordView], names: list[str]) -> tuple[str | None, str]:
    term = term.strip()
    m = _ID_RE.search(term)
    if m:
        return m.group(0).lower(), "id"
    low = term.lower()
    found = [r for r in by_id.values() if r.found]
    newer = [r for r in found if r.supersedes and r.supersedes in by_id and by_id[r.supersedes].found]
    older = [by_id[r.supersedes] for r in newer]
    if re.search(r"\b(new|newer|newest|replacement|latest|updated)\b", low):
        return (newer[0].id, "role") if len(newer) == 1 else (None, "role")
    if re.search(r"\b(old|older|oldest|original|earlier|previous|first)\b", low):
        return (older[0].id, "role") if len(older) == 1 else (None, "role")
    m = re.search(r"(\w+)['’]s\s+(?:record|decision|entry|memory)", low) or re.search(r"the\s+(\w+)\s+(?:record|decision|entry|memory)", low)
    if m and m.group(1) in names:
        who = [r for r in found if r.source_agent.lower() == m.group(1)]
        return (who[0].id, "author") if len(who) == 1 else (None, "author")
    return None, "unknown"


def check_supersede(text: str, by_id: dict[str, RecordView], names: list[str]) -> list[dict[str, Any]]:
    pairs: list[tuple[str, str]] = []
    for rx in (_SUP_ACTIVE, _SUP_PASSIVE):
        for m in rx.finditer(text):
            pairs.append((m.group("a"), m.group("b")))
    out: list[dict[str, Any]] = []
    for a_term, b_term in dict.fromkeys(pairs):
        a, _ = _resolve_endpoint(a_term, by_id, names)
        b, _ = _resolve_endpoint(b_term, by_id, names)
        if not a or not b or a not in by_id or b not in by_id:
            out.append(_finding(UNSUPPORTED, "SUPERSEDE_UNRESOLVED", claimed=f"{a_term} supersedes {b_term}", detail="the records given do not identify which record each phrase means"))
            continue
        ra, rb = by_id[a], by_id[b]
        if not ra.found or not rb.found:
            out.append(_finding(UNSUPPORTED, "SUPERSEDE_UNRESOLVED", claimed=f"{a} supersedes {b}", detail="one of the records was not found"))
            continue
        ev = [{"record": ra.id, "field": "supersedes", "value": ra.supersedes}, {"record": rb.id, "field": "supersedes", "value": rb.supersedes},
              {"record": rb.id, "field": "superseded_by", "value": rb.superseded_by}]
        if ra.supersedes == rb.id:
            out.append(_finding(SUPPORTED, "SUPERSEDE_SUPPORTED", record=ra.id, field="supersedes", claimed=f"{ra.id} supersedes {rb.id}", found=ra.supersedes, evidence=ev))
        elif rb.supersedes == ra.id:
            out.append(_finding(CONTRADICTED, "SUPERSEDE_DIRECTION", record=rb.id, field="supersedes", claimed=f"{ra.id} supersedes {rb.id}", found=f"{rb.id} supersedes {ra.id}",
                                evidence=ev, detail=f"the direction is reversed: {rb.id} supersedes {ra.id}, not the other way round"))
        else:
            out.append(_finding(CONTRADICTED, "SUPERSEDE_DIRECTION", record=ra.id, field="supersedes", claimed=f"{ra.id} supersedes {rb.id}", found=ra.supersedes, evidence=ev,
                                detail=f"{ra.id} supersedes {ra.supersedes or 'nothing'}, not {rb.id}"))
    return out


_SLOT_WORDS = {"main", "commit", "sha", "sha1", "sha256", "hash", "merge", "merged", "revision", "rev", "head", "digest", "checksum", "content_sha256"}
_DATE_SLOT = re.compile(r"\b(?:on|deployed|written|created|dated|since|as of|from|recorded|merged|shipped)\b", re.I)


def _slot_before(text: str, start: int) -> str | None:
    words = re.findall(r"[A-Za-z_0-9]+", text[max(0, start - 40):start].lower())
    for w in reversed(words[-3:]):
        if w in _SLOT_WORDS:
            return w
    return None


def check_tokens(text: str, scope: list[RecordView]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    live = [r for r in scope if r.found]
    scrubbed = _ID_RE.sub(lambda m: " " * len(m.group(0)), text)
    for m in _COMMIT_RE.finditer(scrubbed):
        tok = m.group(0).lower()
        if any(len(c) >= 7 and (c.startswith(tok) or tok.startswith(c)) for r in live for c in r.commits):
            out.append(_finding(SUPPORTED, "TOKEN_SUPPORTED", claimed=tok, detail="the commit appears in the record"))
            continue
        slot = _slot_before(scrubbed, m.start())
        other = sorted({c for r in live for c in r.commits})
        if slot and other and any(re.search(rf"\b{slot}\b[^\n]{{0,12}}?{c[:7]}", r.text, re.I) for r in live for c in r.commits):
            r0 = next(r for r in live if any(c in r.commits for c in other))
            out.append(_finding(CONTRADICTED, "TOKEN_MISMATCH", record=r0.id, field="content", claimed=tok, found=other[0],
                                evidence=[{"record": r0.id, "field": "content", "value": _snippet(r0.text, other[0])}],
                                detail=f"the summary gives {slot} {tok}; the record gives {other[0]}"))
        else:
            out.append(_finding(UNSUPPORTED, "TOKEN_UNSUPPORTED", claimed=tok, detail="this commit is not in the cited records"))
    for m in _SHA256_RE.finditer(scrubbed):
        tok = m.group(0).lower()
        if any(tok in r.sha256s for r in live):
            out.append(_finding(SUPPORTED, "TOKEN_SUPPORTED", claimed=tok, detail="the hash appears in the record"))
        else:
            others = sorted({h for r in live for h in r.sha256s})
            slot = _slot_before(scrubbed, m.start())
            if slot and others:
                out.append(_finding(CONTRADICTED, "TOKEN_MISMATCH", claimed=tok, found=others[0], detail=f"the summary gives {slot} {tok[:16]}…; the record's is {others[0][:16]}…"))
            else:
                out.append(_finding(UNSUPPORTED, "TOKEN_UNSUPPORTED", claimed=tok, detail="this hash is not in the cited records"))
    for m in _DATE_RE.finditer(scrubbed):
        d = m.group(1)
        if any(d in r.dates for r in live):
            out.append(_finding(SUPPORTED, "TOKEN_SUPPORTED", claimed=d, detail="the date appears in the record"))
            continue
        have = sorted({x for r in live for x in r.dates})
        if have and _DATE_SLOT.search(scrubbed[max(0, m.start() - 30):m.start()]):
            r0 = next(r for r in live if r.dates)
            out.append(_finding(CONTRADICTED, "TOKEN_MISMATCH", record=r0.id, field="created_at", claimed=d, found=have,
                                evidence=[{"record": r0.id, "field": "created_at", "value": r0.created_at}],
                                detail=f"the summary dates it {d}; the record's dates are {', '.join(have)}"))
        else:
            out.append(_finding(UNSUPPORTED, "TOKEN_UNSUPPORTED", claimed=d, detail="this date is not in the cited records"))
    return out


def check_numbers(text: str, scope: list[RecordView]) -> list[dict[str, Any]]:
    live = [r for r in scope if r.found]
    scrub = _SHA256_RE.sub(" ", _COMMIT_RE.sub(" ", _DATE_RE.sub(" ", _ID_RE.sub(" ", text))))
    slots = _number_slots(scrub)
    out: list[dict[str, Any]] = []
    for num in dict.fromkeys(_NUM_RE.findall(scrub)):
        if any(num in r.numbers for r in live):
            out.append(_finding(SUPPORTED, "NUMBER_SUPPORTED", claimed=num))
            continue
        mine = slots.get(num, set())
        clash = [(r, n) for r in live for n, near in r.number_slots.items() if near & mine and n != num]
        if clash:
            r0, n0 = clash[0]
            out.append(_finding(CONTRADICTED, "NUMBER_MISMATCH", record=r0.id, field="content", claimed=num, found=n0,
                                evidence=[{"record": r0.id, "field": "content", "value": _snippet(r0.text, n0)}],
                                detail=f"the summary says {num} next to '{sorted(mine)[0] if mine else 'it'}'; the record says {n0}"))
        else:
            out.append(_finding(UNSUPPORTED, "NUMBER_UNSUPPORTED", claimed=num, detail="this number is not in the cited records"))
    return out


# (topic, positive claim, record text that CONTRADICTS a positive claim, record text that SUPPORTS it, reason)
STATE_RULES = (
    (re.compile(r"\b(signature|signatures|signing|signer|attestation|attestations|signature machinery)\b", re.I),
     re.compile(r"\b(live|enabled|on|active|running|deployed|in use|working|operational|turned on|switched on)\b", re.I),
     re.compile(r"\b(shelved|warn mode|warn|no trust roots?|nothing (?:is |was |has been )?signed|not signed|unsigned|signing (?:stayed|is|was) (?:shelved|off)|no key)\b", re.I),
     re.compile(r"\b(signing (?:is|was|now) (?:live|enabled|on|active)|signatures? (?:are|is|were) (?:live|enabled|on|active))\b", re.I),
     "signatures"),
    (re.compile(r"\b(twin|narrator|digital twin)\b", re.I),
     re.compile(r"\b(live|enabled|on|active|running|deployed|in use|serving|available)\b", re.I),
     re.compile(r"\b(retired|removed|dark|disabled|returns? 404|404|flags? (?:are |is )?(?:0|off))\b", re.I),
     re.compile(r"\b(twin (?:is|was) (?:live|enabled|on|serving))\b", re.I),
     "twin"),
)


def check_state(text: str, scope: list[RecordView]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    live = [r for r in scope if r.found]
    for topic, positive, counter, support, name in STATE_RULES:
        t, p = topic.search(text), positive.search(text)
        if not (t and p) or re.search(r"\b(not|isn't|aren't|never|no longer|without)\b", text, re.I):
            continue
        against = [r for r in live if counter.search(r.text)]
        for_ = [r for r in live if support.search(r.text)]
        if against and not for_:
            r0 = against[0]
            hit = counter.search(r0.text).group(0)
            out.append(_finding(CONTRADICTED, "STATE_CLAIM_CONTRADICTED", record=r0.id, field="content", claimed=f"{name} {p.group(0)}", found=hit,
                                evidence=[{"record": r0.id, "field": "content", "value": _snippet(r0.text, hit)}],
                                detail=f"the summary says {name} is {p.group(0)}; the record says '{hit}'"))
        elif for_ and not against:
            out.append({**_finding(SUPPORTED, "STATE_CLAIM_SUPPORTED", record=for_[0].id, field="content", claimed=f"{name} {p.group(0)}"), "words": [*t.group(0).lower().split(), *p.group(0).lower().split()]})
        else:
            out.append(_finding(UNSUPPORTED, "STATE_CLAIM_UNSUPPORTED", claimed=f"{name} {p.group(0)}", detail="the cited records say nothing about this state"))
    return out


def check_claim_words(text: str, scope: list[RecordView]) -> list[dict[str, Any]]:
    out = []
    for w in dict.fromkeys(x.lower() for x in re.findall(r"[A-Za-z0-9%]+", text) if x.lower() in _CLAIM_WORDS):
        if not any(r.found and _whole_word(w, r.text) for r in scope):
            out.append(_finding(UNSUPPORTED, "CLAIM_WORD", claimed=w, detail=f"'{w}' is an absolute claim the cited records do not make"))
    return out


def check_entities(text: str, scope: list[RecordView], names: list[str], handled: set[str]) -> list[dict[str, Any]]:
    out = []
    live = [r for r in scope if r.found]
    for n in names:
        if n in handled or not _whole_word(n, text):
            continue
        if live and not any(_whole_word(n, r.text) or n == r.source_agent.lower() for r in live):
            out.append(_finding(UNSUPPORTED, "ENTITY_UNSUPPORTED", claimed=n, detail=f"'{n}' is not mentioned by the cited records"))
    return out


def grounding(text: str, scope: list[RecordView], consumed: set[str] | None = None) -> dict[str, Any] | None:
    """Every content word of a clause should be found (stemmed, whole word) in the cited records; None means grounded."""
    live = [r for r in scope if r.found]
    if not live:
        return _finding(UNSUPPORTED, "RECORD_NOT_FOUND", detail="none of the cited records exist for this tenant, so there is nothing to check this against")
    body = _ID_RE.sub(" ", _SHA256_RE.sub(" ", _COMMIT_RE.sub(" ", _DATE_RE.sub(" ", text))))
    words = [w for w in _WORD_RE.findall(body) if w.lower() not in _STOP or w.lower() in _NEGATIONS]
    skip = _GLUE | {c.lower() for c in (consumed or set())}
    content = [w for w in words if (len(w) >= 3 or w.lower() in _NEGATIONS) and w.lower() not in skip]
    # a word is grounded by the record text, or is the name of the record's own source_agent / a status word
    grounded_words = set().union(*(r.words for r in live)) | {r.source_agent.lower() for r in live} | {_stem(r.status) for r in live} | {r.lifecycle for r in live}
    missing = [w for w in dict.fromkeys(x.lower() for x in content) if _stem(w) not in grounded_words and w not in grounded_words]
    allowed = int(len(set(x.lower() for x in content)) * 0.15) if len(content) >= 7 else 0
    if len(missing) > allowed:
        return _finding(UNSUPPORTED, "UNSUPPORTED_TEXT", claimed=", ".join(missing[:6]), detail=f"words not found in the cited records: {', '.join(missing[:8])}")
    return None


# ------------------------------------------------------------------------------------------------------- the engine


def check_claim(claim: dict[str, Any], by_id: dict[str, RecordView], provided: list[str], names: list[str]) -> dict[str, Any]:
    shown = claim["text"]
    quoted = _QUOTED_RE.findall(shown)
    text = re.sub(r"\s+", " ", _QUOTED_RE.sub(" ", shown)).strip() if quoted else shown  # a quotation is not asserted by the author, so it is not checked
    mentioned = [i.lower() for i in _ID_RE.findall(text)] + [c.lower() for c in claim.get("cites", [])]
    findings: list[dict[str, Any]] = []
    for rid in dict.fromkeys(mentioned):
        if rid not in provided:
            findings.append(_finding(UNSUPPORTED, "CITE_MISSING", record=rid, claimed=rid, detail=f"{rid} was named but not submitted, so it was not checked"))
        elif not by_id[rid].found:
            findings.append(_finding(UNSUPPORTED, "RECORD_NOT_FOUND", record=rid, claimed=rid, detail=f"{rid} does not exist for this tenant"))
    cited_here = [by_id[r] for r in dict.fromkeys(mentioned) if r in by_id and by_id[r].found]
    scope = cited_here or [by_id[r] for r in provided]
    findings += check_actor(text, scope, names)
    findings += check_status(text, scope, by_id)
    findings += check_supersede(text, by_id, names)
    findings += check_tokens(text, scope)
    findings += check_numbers(text, scope)
    findings += check_state(text, scope)
    findings += check_claim_words(text, scope)
    handled = {str(f["claimed"]).lower() for f in findings if f["reason"].startswith("ACTOR_")}
    findings += check_entities(text, scope, names, handled)
    bad = [f for f in findings if f["status"] == CONTRADICTED]
    weak = [f for f in findings if f["status"] == UNSUPPORTED]
    if bad:
        status, reason = CONTRADICTED, bad[0]["reason"]
    elif weak:
        status, reason = UNSUPPORTED, weak[0]["reason"]
    else:
        consumed = {w for f in findings if f["status"] == SUPPORTED for w in f.get("words", [])}
        g = grounding(text, scope, consumed)
        if g:
            findings.append(g)
            status, reason = UNSUPPORTED, g["reason"]
        else:
            status, reason = SUPPORTED, "GROUNDED"
    return {"text": shown, "status": status, "reason": reason, "hedged": bool(_HEDGE_RE.search(text)), "quoted_text_not_checked": quoted, "records_checked": [r.id for r in scope],
            "findings": [f for f in findings if f["status"] != SUPPORTED] if status != SUPPORTED else [f for f in findings if f["status"] == SUPPORTED][:3]}


def run_check(request: dict[str, Any], store: Any) -> dict[str, Any]:
    """The whole check.  ``request`` is validate_request's output.  Pure with respect to the ledger: it only reads."""
    started = time.perf_counter()
    provided = request["records"]
    by_id = load_records(store, provided)
    names = _agent_names(list(by_id.values()))
    raw_claims = request["claims"] if request["claims"] is not None else extract_claims(request["summary"] or "")
    skipped: list[dict[str, Any]] = []
    todo: list[dict[str, Any]] = []
    for c in raw_claims:
        why = _skip_reason(c["text"])
        (skipped.append({"text": c["text"], "reason": why}) if why else todo.append(c))
    if len(todo) > MAX_CLAIMS:
        raise DriftInputError("TOO_MANY_CLAIMS", f"the summary has {len(todo)} claims; the limit is {MAX_CLAIMS}. Nothing was checked.")
    results = [check_claim(c, by_id, provided, names) for c in todo]
    warnings: list[str] = []
    for rid, served in request["served_content_sha256"].items():
        rv = by_id.get(rid)
        if rv and rv.found and rv.content_sha256 and rv.content_sha256 != served:
            warnings.append(f"RECORD_CHANGED_SINCE_SERVED: {rid} now has content_sha256 {rv.content_sha256[:16]}…, not the {served[:16]}… the agent says it was served")
    for rid, rv in by_id.items():
        if not rv.found:
            warnings.append(f"RECORD_NOT_FOUND: {rid} does not exist for this tenant")
    counts = {"claims": len(results), "supported": 0, "contradicted": 0, "unsupported": 0, "skipped": len(skipped)}
    for r in results:
        counts[r["status"]] += 1
    verdict = VERDICT_DRIFT if counts["contradicted"] else (VERDICT_UNVERIFIED if counts["unsupported"] or not results else VERDICT_PASS)
    basis = {"version": CHECK_VERSION, "summary_sha256": sha256_hex(request["summary"] or canonical(raw_claims)),
             "records": [by_id[i].summary() for i in provided],
             "claims": [{"text": r["text"], "status": r["status"], "reason": r["reason"], "findings": [{k: f[k] for k in ("status", "reason", "record", "field", "claimed", "found")} for f in r["findings"]]} for r in results],
             "verdict": verdict}
    return {"verdict": verdict, "counts": counts, "claims": results, "skipped": skipped, "warnings": warnings,
            "records": [by_id[i].summary() for i in provided], "check_digest": sha256_hex(canonical(basis)), "check_version": CHECK_VERSION,
            "method": "deterministic rules; no model", "duration_ms": max(0, int((time.perf_counter() - started) * 1000))}


# --------------------------------------------------------------------------------------- the call log: what was served


_SERVED_TOOLS = {"emr_fetch", "fetch", "emr_latest", "emr_search_ledger", "search", "emr_search", "emr_recall"}


def match_call_log(records: list[str], client: str | None, served_digest: str | None, limit: int = 10, *, tenant: str | None = None) -> dict[str, Any]:
    """Which logged calls could have served these records.  A match proves a call happened with these hashes, NOT what text was shown.

    Only calls made by the tenant whose records are being checked count: the call log is one chain for everyone, and a drift check must never return another
    tenant's seq, client name, transport or result digest. ``tenant`` is the label the entries are filed under (the operator's is "operator")."""
    if not call_log.enabled():
        return {"available": False, "matches": [], "note": "the call log is switched off"}
    try:
        log = call_log.get_call_log()
        wanted = tenant or "operator"
        by_args = {call_log.args_hash({"id": rid}): rid for rid in records}
        matches: list[dict[str, Any]] = []
        for e in log.entries_desc():
            if len(matches) >= limit:
                break
            if e.get("tool") not in _SERVED_TOOLS or e.get("outcome") != "ok":
                continue
            if e.get("tenant") != wanted:
                continue
            if client and client.lower() not in str(e.get("client_name") or "").lower():
                continue
            why = None
            if e.get("args_sha256") in by_args and e.get("tool") in ("emr_fetch", "fetch"):
                why = f"the logged arguments hash equals {{'id': '{by_args[e['args_sha256']]}'}}"
            elif served_digest and e.get("result_digest") == served_digest:
                why = "the logged result_digest equals the digest the agent reported"
            if why:
                matches.append({"seq": e.get("seq"), "ts": e.get("ts"), "tool": e.get("tool"), "client_name": e.get("client_name"),
                                "transport": e.get("transport"), "result_digest": e.get("result_digest"), "why": why})
        return {"available": True, "matches": matches, "note": "a match shows that a call with these hashes happened; it cannot show what text the agent was shown"}
    except Exception as exc:  # noqa: BLE001 - the check must not fail because the call log is unreadable
        return {"available": False, "matches": [], "note": f"the call log could not be read: {type(exc).__name__}"}


def log_entry(result: dict[str, Any], request: dict[str, Any], *, tenant: str | None, client_header: str | None, matches: dict[str, Any]) -> dict[str, Any]:
    name, version = call_log.split_client(client_header) if client_header else (request.get("client"), None)
    bad = [{"claim": c["text"][:300], "status": c["status"], "reason": c["reason"],
            "record": (c["findings"][0].get("record") if c["findings"] else None), "field": (c["findings"][0].get("field") if c["findings"] else None)}
           for c in result["claims"] if c["status"] != SUPPORTED]
    return {"transport": "http-api", "client_name": call_log.clean_client(name), "client_version": call_log.clean_client(version, 64), "client_self_reported": True,
            "tenant": tenant or call_log.default_tenant_label(), "tool": "emr_drift_check", "route": "/api/jarvis/drift/check", "method": "POST", "target": None,
            "args_sha256": sha256_hex(request["summary"] or ""), "summary_chars": len(request["summary"] or ""), "records": request["records"],
            "outcome": "ok", "error_code": None, "status_code": 200, "result_digest": result["check_digest"], "duration_ms": result["duration_ms"],
            "verdict": result["verdict"], "counts": result["counts"], "findings": bad[:50], "call_log_seqs": [m["seq"] for m in matches.get("matches", [])]}
