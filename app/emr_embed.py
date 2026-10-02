"""Optional semantic similarity for EMR query alignment.

Lexical alignment cannot see that "what is the pet called" asks about "Jon has
a dog named Bruno". A small local embedding model can. This module is
optional: without the `embed` extra installed, or with it switched off, EMR
scores lexically and nothing here runs.

    pip install ".[embed]"
    JARVIS_EMR_EMBEDDINGS=1              switch it on (default off)
    JARVIS_EMR_EMBED_MODEL=...           fastembed model (default bge-small-en-v1.5)
    JARVIS_EMR_EMBED_MODEL_DIR=...       where model files are kept
    JARVIS_EMR_EMBED_CACHE=...           memory-vector cache (JSON, by content hash)

The model runs on the CPU through ONNX (fastembed), and is downloaded once
into JARVIS_EMR_EMBED_MODEL_DIR. Nothing leaves the machine at query time.

Cosine similarity is mapped onto Q's [0, 1] scale by `SIM_FLOOR` and
`SIM_FULL`: unrelated text scores around 0.35-0.45 with bge-small, so a
cosine at or below the floor adds nothing, and one at the ceiling counts as
full alignment. Both were calibrated on half of the EMR benchmark and checked
on the other half.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import MemoryRecord

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
SIM_FLOOR = 0.65
SIM_FULL = 0.85

_lock = threading.Lock()
_model = None
_model_name: str | None = None
_load_failed = False
_cache: dict[str, list[float]] | None = None
_cache_path: str | None = None


def enabled() -> bool:
    return os.getenv("JARVIS_EMR_EMBEDDINGS", "").strip().lower() in ("1", "true", "yes", "on")


def model_name() -> str:
    return os.getenv("JARVIS_EMR_EMBED_MODEL") or DEFAULT_MODEL


def _model_dir() -> str:
    return os.getenv("JARVIS_EMR_EMBED_MODEL_DIR") or os.path.join("data", "emr-embed-model")


def _get_model():
    """The embedding model, loaded once; None if it cannot be loaded."""
    global _model, _model_name, _load_failed
    name = model_name()
    with _lock:
        if _model is not None and _model_name == name:
            return _model
        if _load_failed:
            return None
        try:
            from fastembed import TextEmbedding

            _model = TextEmbedding(name, cache_dir=_model_dir())
            _model_name = name
        except Exception:
            # Missing extra, no network for the first download, bad model name:
            # EMR stays lexical rather than failing recall.
            _load_failed = True
            return None
        return _model


def _load_cache() -> dict[str, list[float]]:
    global _cache, _cache_path
    path = os.getenv("JARVIS_EMR_EMBED_CACHE") or os.path.join("data", "emr-embeddings.json")
    if _cache is not None and _cache_path == path:
        return _cache
    _cache_path, _cache = path, {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("model") == model_name():
            _cache = raw.get("vectors", {})
    except (OSError, ValueError, AttributeError):
        pass
    return _cache


def _save_cache() -> None:
    if _cache_path is None or _cache is None:
        return
    try:
        path = Path(_cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"model": model_name(), "vectors": _cache}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # a cold cache only costs recomputation


def _memory_text(rec: "MemoryRecord") -> str:
    return f"{rec.subject}: {rec.content}" if rec.subject else rec.content


def _unit(vec) -> list[float]:
    values = [float(x) for x in vec]  # plain floats: numpy's are not JSON
    norm = sum(x * x for x in values) ** 0.5 or 1.0
    return [x / norm for x in values]


def similarities(query: str, records: list["MemoryRecord"]) -> dict[str, float] | None:
    """Cosine similarity of `query` to each record, by record id.

    None when embeddings are off or unavailable, so callers fall back to
    lexical alignment. Memory vectors are cached by content hash and model.
    """
    if not enabled() or not query.strip() or not records:
        return None
    model = _get_model()
    if model is None:
        return None
    with _lock:
        cache = _load_cache()
        missing = {}
        for rec in records:
            key = rec.content_sha256 or rec.id
            if key not in cache and key not in missing:
                missing[key] = _memory_text(rec)
        if missing:
            for key, vec in zip(missing, model.embed(list(missing.values()))):
                cache[key] = _unit(vec)
            _save_cache()
        q = _unit(next(iter(model.query_embed([query]))))
        return {
            rec.id: sum(a * b for a, b in zip(q, cache[rec.content_sha256 or rec.id]))
            for rec in records
        }


def semantic_alignment(cosine: float) -> float:
    """Map a cosine onto Q's [0, 1] scale (see SIM_FLOOR / SIM_FULL)."""
    return max(0.0, min(1.0, (cosine - SIM_FLOOR) / (SIM_FULL - SIM_FLOOR)))


def reset_for_tests() -> None:
    global _model, _model_name, _load_failed, _cache, _cache_path
    with _lock:
        _model = _model_name = _cache = _cache_path = None
        _load_failed = False
