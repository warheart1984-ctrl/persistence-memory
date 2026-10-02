"""Regression floor for the labelled EMR benchmark.

The thresholds sit a little under the scores this retrieval reached when they
were set, so noise does not fail CI but a real regression does. Raise them
when retrieval improves; lowering one needs a reason in the PR.
"""

from __future__ import annotations

import pytest

from app.emr_bench import run_bench


@pytest.fixture(scope="module")
def bench():
    return run_bench()


def test_benchmark_safety_gates_pass(bench):
    assert bench["safety_status"] == "pass", bench["safety_gates"]


def test_answerable_questions_are_recalled(bench):
    overall = bench["summary"]["overall"]
    assert overall["hit_at_k"] >= 0.85
    assert overall["top1"] >= 0.62
    assert overall["wrongly_abstained"] <= 0.08


def test_old_memories_stay_recallable(bench):
    by_age = bench["summary"]["by_age"]
    assert by_age["1-4w"]["hit_at_k"] >= 0.85
    assert by_age[">1mo"]["hit_at_k"] >= 0.8


def test_unrelated_questions_recall_nothing(bench):
    assert bench["summary"]["by_category"]["neg"]["false_positive"] == 0.0


def test_keyword_and_word_form_queries(bench):
    cats = bench["summary"]["by_category"]
    assert cats["kw"]["hit_at_k"] >= 0.95
    assert cats["morph"]["hit_at_k"] >= 0.9
    assert cats["nq"]["hit_at_k"] >= 0.9
