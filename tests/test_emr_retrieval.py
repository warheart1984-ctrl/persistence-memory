"""Lexical retrieval, recency floor and admission rules in EMR.

Each test pins one behaviour the labelled benchmark (tests/fixtures/emr_bench)
showed was missing: word forms, function words, term rarity, old memories,
and memories that share nothing with the query.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.emr import (
    RECENCY_FLOOR,
    ExciteRequest,
    _stem,
    _terms,
    activate,
    excite,
    query_alignment,
    term_idf,
)
from app.models import MemoryRecord


def _rec(id: str, content: str, *, age_hours: float = 1.0, **kwargs) -> MemoryRecord:
    stamp = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    base = dict(
        id=id, content=content, created_at=stamp, updated_at=stamp,
        source_agent="test", session_id="sess-test", type="fact",
        confidence=0.9, evidence=[], status="verified", subject=None, tags=[],
        content_sha256=f"hash-{id}",
    )
    base.update(kwargs)
    return MemoryRecord(**base)


def test_word_forms_share_a_stem():
    for forms in (("restart", "restarts", "restarting"), ("merge", "merged", "merging"),
                  ("service", "services"), ("run", "runs", "running"), ("memory", "memories")):
        assert len({_stem(f) for f in forms}) == 1, forms


def test_function_words_are_not_terms():
    assert _terms("What is the name of the dog?") == {"nam", "dog"}


def test_function_words_do_not_dilute_a_question():
    rec = _rec("m", "Jon has a dog named Bruno.")
    assert query_alignment(rec, "What is the name of Jon's dog?") > query_alignment(rec, "dog cat bird")


def test_rare_terms_outweigh_common_ones():
    records = [_rec(f"g{i}", f"The gateway handles tenant {i}.") for i in range(8)]
    router = _rec("r", "The gateway router is a Netgear Nighthawk.")
    idf = term_idf([*records, router])
    # Both share "gateway"; only the router memory has "nighthawk".
    assert query_alignment(router, "gateway nighthawk", idf) > 0.8
    assert query_alignment(records[0], "gateway nighthawk", idf) < 0.2


def test_unseen_terms_do_not_swamp_a_tiny_ledger():
    rec = _rec("m", "All generated images must be signed bottom-right.", subject="image-signature")
    idf = term_idf([rec])
    # Two of five query terms match: plain overlap, not near zero.
    assert query_alignment(rec, "fantasy portrait image signature placement", idf) == 0.4


def test_decay_never_erases_an_old_memory():
    old = activate(_rec("old", "Jon has a dog named Bruno.", age_hours=24 * 365), query="dog")
    assert old.decay == RECENCY_FLOOR
    assert old.gate_A > 0.08  # still passes the evidence floor


def test_recent_memory_wins_a_tie():
    fresh = _rec("fresh", "Jon has a dog named Bruno.", age_hours=1, content_sha256="a")
    old = _rec("old", "Jon has a dog named Rex.", age_hours=24 * 90, content_sha256="b")
    res = excite([fresh, old], ExciteRequest(query="dog named", session_key="tie"), enforce_abstention=False)
    assert [e.memory_id for e in sorted(res.stm, key=lambda e: -e.activation)][0] == "fresh"


def test_old_exact_match_beats_fresh_unrelated_memories():
    records = [_rec(f"gw{i}", f"Gateway tenant {i} has a budget.", age_hours=1) for i in range(5)]
    dog = _rec("dog", "Jon has a dog named Bruno.", age_hours=24 * 60)
    # emr_recall's promotion threshold; excite's own default (0.12) is a
    # working-set setting that old memories cannot reach.
    res = excite([*records, dog], ExciteRequest(query="dog", theta_promote=0.001, session_key="old-dog"))
    assert not res.abstained
    assert [e.memory_id for e in res.stm] == ["dog"]


def test_memory_with_no_shared_term_is_not_admitted():
    match = _rec("match", "The printer is on 192.168.1.50.", content_sha256="a")
    other = _rec("other", "Jon drinks black coffee.", content_sha256="b")
    res = excite([match, other], ExciteRequest(query="printer address", theta_promote=0.0, session_key="admit"))
    assert [e.memory_id for e in res.stm] == ["match"]


def test_unrelated_question_abstains_on_score_floor():
    rec = _rec("m", "Garden irrigation runs at dawn.")
    res = excite([rec], ExciteRequest(query="capital of France", theta_promote=0.0, session_key="neg"))
    assert res.abstained
    assert res.abstention_reason == "top-score-below-floor"
    assert res.stm == []
