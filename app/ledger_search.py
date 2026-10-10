"""``emr_search_ledger``: ranked full-text search over the tenant's own ledger records.

One implementation behind ``GET /api/jarvis/memory/search``, ``POST /api/jarvis/tools/emr_search_ledger`` and the MCP
tool.  The stores only narrow candidates (Postgres through ``memories_search_idx``, schema V9; the JSON store in
memory), using exactly the same tokens; ranking happens here, once, so both backends return the same records in the
same order.  Read-only; no new stored field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Mirrors jarvis_search_tokens() in app/pg_schema.py (V9) character for character: ASCII letters lower-cased, split on
# anything that is not an ASCII letter/digit and not a non-ASCII character.  No stemming, no stop words.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_SPLIT = re.compile("[^0-9a-z\u0080-\U0010ffff]+")

MAX_QUERY_CHARS = 500
MAX_QUERY_TOKENS = 16
MAX_CANDIDATES = 5000  # newest-first cap on matching records considered for ranking (same on both backends)
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

# Weights per query token, by where it occurs.  Integers so the score (and the digest) is exact.
W_SUBJECT, W_TAG, W_CONTENT, CONTENT_TF_CAP, PHRASE_BONUS = 6, 4, 1, 3, 5


def tokens(text: str | None) -> list[str]:
    return [t for t in _SPLIT.split((text or "").translate(_ASCII_LOWER)) if t]


def record_tokens(subject: str | None, content: str | None, tags: list[str] | None) -> list[str]:
    """The tokens Postgres indexes for a record: subject, content and tags joined with spaces."""
    return tokens(" ".join([subject or "", content or "", " ".join(tags or [])]))


def query_tokens(query: Any) -> list[str]:
    """Distinct tokens of the query in first-seen order; refuses an empty, overlong or token-less query."""
    from app.emr_latest import LatestError

    if not isinstance(query, str) or not query.strip():
        raise LatestError(422, "invalid_request", "QUERY_EMPTY", "query must be a non-empty string")
    if len(query) > MAX_QUERY_CHARS:
        raise LatestError(422, "invalid_request", "QUERY_TOO_LONG", f"query must be at most {MAX_QUERY_CHARS} characters")
    seen: list[str] = []
    for tok in tokens(query):
        if tok not in seen:
            seen.append(tok)
    if not seen:
        raise LatestError(422, "invalid_request", "QUERY_EMPTY", "query has no searchable words")
    if len(seen) > MAX_QUERY_TOKENS:
        raise LatestError(422, "invalid_request", "QUERY_TOO_LONG", f"query must have at most {MAX_QUERY_TOKENS} distinct words")
    return seen


def _contains_run(seq: list[str], run: list[str]) -> bool:
    """Whether ``run`` occurs as consecutive items of ``seq`` (Knuth-Morris-Pratt: O(len(seq) + len(run)), no slicing)."""
    m = len(run)
    if m == 0 or m > len(seq):
        return m == 0
    fail = [0] * m
    k = 0
    for i in range(1, m):
        while k and run[i] != run[k]:
            k = fail[k - 1]
        if run[i] == run[k]:
            k += 1
        fail[i] = k
    k = 0
    for item in seq:
        while k and item != run[k]:
            k = fail[k - 1]
        if item == run[k]:
            k += 1
            if k == m:
                return True
    return False


def score(record: Any, qtoks: list[str], phrase: list[str] | None = None) -> int:
    """Deterministic integer relevance of one candidate (which already contains every query token).

    ``qtoks`` are the distinct query words; ``phrase`` is the whole query as written, repeats included (default: ``qtoks``),
    because the phrase bonus is for the whole query: ``foo bar foo`` is not matched by a record that only says "foo bar"."""
    phrase = qtoks if phrase is None else phrase
    subject = set(tokens(record.subject))
    tag_toks = set(tokens(" ".join(record.tags or [])))
    content = tokens(record.content)
    total = 0
    for q in qtoks:
        if q in subject:
            total += W_SUBJECT
        if q in tag_toks:
            total += W_TAG
        total += W_CONTENT * min(CONTENT_TF_CAP, content.count(q))
    if len(phrase) > 1:
        for seq in (tokens(record.subject), content):
            if _contains_run(seq, phrase):
                total += PHRASE_BONUS
                break
    return total


@dataclass(frozen=True)
class SearchParams:
    query: str = ""
    limit: int = DEFAULT_LIMIT
    include_superseded: bool = False
    include_archived: bool = False
    include_twin: bool = False
    type: str | None = None


def search_ledger(store: Any, *, tenant: str | None, params: SearchParams, operator: bool) -> dict[str, Any]:
    """The single implementation of ledger search.  ``store`` must already be scoped to ``tenant``."""
    from app.emr_latest import TENANT_UNRESOLVED, LatestError, _shape, ledger_head, parse_limit, result_digest

    if not tenant:
        raise LatestError(403, "denied", TENANT_UNRESOLVED, "tenant could not be resolved")
    limit = parse_limit(params.limit)
    qtoks = query_tokens(params.query)
    phrase = tokens(params.query)  # the whole query in order, repeats kept, for the phrase bonus
    candidates = store.list_latest(
        limit=MAX_CANDIDATES + 1,
        memory_type=params.type,
        include_superseded=params.include_superseded,
        include_archived=params.include_archived,
        include_twin=params.include_twin,
        tokens=qtoks,
    )
    capped = len(candidates) > MAX_CANDIDATES
    # Candidates arrive newest first, (created_at, id) descending; a stable sort on -score keeps that as the tie-break.
    ranked = sorted(candidates[:MAX_CANDIDATES], key=lambda pair: -score(pair[0], qtoks, phrase))[:limit]
    records = []
    for rec, succ in ranked:
        shaped = _shape(rec, succ)
        shaped["score"] = score(rec, qtoks, phrase)
        records.append(shaped)
    return {
        "query": params.query,
        "tokens": qtoks,
        "records": records,
        "candidates_capped": capped,
        "tenant": tenant,
        "ledger_head": ledger_head(store, operator=operator),
        "result_digest": result_digest([(r["id"], r["created_at"], r["status"], r["lifecycle"], r["score"]) for r in records]),
        "provenance": "ledger",
    }
