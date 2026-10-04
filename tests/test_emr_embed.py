"""emr_embed: optional semantic alignment, with a fake model (no download).

The real model is exercised by the benchmark (tests/test_emr_bench.py), which
skips when the `embed` extra is not installed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import app.emr_embed as emr_embed
from app.emr import ExciteRequest, activate, excite
from app.models import MemoryRecord

# Toy 3-d "meaning" space: pets, networking, everything else.
_VECTORS = {
    "dog": [1.0, 0.0, 0.0],
    "pet": [1.0, 0.0, 0.0],
    "router": [0.0, 1.0, 0.0],
}


def _vec(text: str) -> list[float]:
    for word, vec in _VECTORS.items():
        if word in text.lower():
            return vec
    return [0.0, 0.0, 1.0]


class FakeModel:
    def __init__(self):
        self.embedded: list[str] = []

    def embed(self, texts):
        self.embedded.extend(texts)
        return [_vec(t) for t in texts]

    def query_embed(self, texts):
        return [_vec(t) for t in texts]


@pytest.fixture()
def fake_model(monkeypatch):
    model = FakeModel()
    monkeypatch.setenv("JARVIS_EMR_EMBEDDINGS", "1")
    monkeypatch.setattr(emr_embed, "_get_model", lambda: model)
    return model


def _rec(id: str, content: str) -> MemoryRecord:
    now = datetime.now(timezone.utc).isoformat()
    return MemoryRecord(
        id=id, content=content, created_at=now, updated_at=now, source_agent="t",
        session_id="s", type="fact", confidence=0.9, evidence=[], status="verified",
        content_sha256=f"hash-{id}",
    )


def test_off_by_default_so_scoring_stays_lexical():
    assert emr_embed.similarities("dog", [_rec("a", "Jon has a dog.")]) is None
    br = activate(_rec("a", "Jon has a dog."), query="dog")
    assert br.Q_sem is None and br.Q == br.Q_lex


def test_unloadable_model_falls_back_to_lexical(monkeypatch):
    monkeypatch.setenv("JARVIS_EMR_EMBEDDINGS", "1")
    monkeypatch.setenv("JARVIS_EMR_EMBED_MODEL", "no-such/model-for-tests")
    assert emr_embed.similarities("dog", [_rec("a", "Jon has a dog.")]) is None


def test_cosine_maps_onto_q_scale():
    assert emr_embed.semantic_alignment(emr_embed.SIM_FLOOR - 0.1) == 0.0
    assert emr_embed.semantic_alignment(emr_embed.SIM_FULL + 0.1) == 1.0
    mid = (emr_embed.SIM_FLOOR + emr_embed.SIM_FULL) / 2
    assert emr_embed.semantic_alignment(mid) == pytest.approx(0.5)


def test_paraphrase_without_shared_words_is_recalled(fake_model):
    dog = _rec("dog", "Jon has a dog named Bruno.")
    router = _rec("router", "The router is a Nighthawk.")
    res = excite([dog, router], ExciteRequest(query="what is the pet called", theta_promote=0.001, session_key="p"))
    assert not res.abstained
    assert [e.memory_id for e in res.stm] == ["dog"]


def test_q_is_the_stronger_of_lexical_and_semantic(fake_model):
    br = activate(_rec("dog", "Jon has a dog named Bruno."), query="pet", semantic=1.0)
    assert br.Q_lex == 0.05 and br.Q_sem == 1.0 and br.Q == 1.0


def test_memory_vectors_are_cached_by_content_hash(fake_model, tmp_path):
    records = [_rec("dog", "Jon has a dog."), _rec("router", "The router is a Nighthawk.")]
    emr_embed.similarities("pet", records)
    emr_embed.similarities("router", records)
    assert len(fake_model.embedded) == 2  # each memory embedded once
    saved = json.loads((tmp_path / "emr-embeddings.json").read_text())
    assert saved["model"] == emr_embed.model_name()
    assert set(saved["vectors"]) == {"hash-dog", "hash-router"}


def test_cache_for_another_model_is_ignored(fake_model, tmp_path, monkeypatch):
    (tmp_path / "emr-embeddings.json").write_text(
        json.dumps({"model": "some/other-model", "vectors": {"hash-dog": [0.0, 0.0, 1.0]}}))
    sims = emr_embed.similarities("pet", [_rec("dog", "Jon has a dog.")])
    assert sims["dog"] == pytest.approx(1.0)  # recomputed, not the stale vector
