"""OpenAI deep-research / company-knowledge compatibility on EMR recall + resolve.

Read-only: ``emr_search`` wraps ``emr_recall``; ``emr_fetch`` reads LTM by id.
Does not mutate ledger truth or reinforcement overlays.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from app.models import MemoryRecord
from app.store import JarvisStore

DEFAULT_SEARCH_MAX = 12
SEARCH_MAX_CAP = 16


class EmrSearchRequest(BaseModel):
    """OpenAI ``search`` tool input — single query string."""

    query: str = Field(..., min_length=1, max_length=2000)
    max_memories: int = Field(default=DEFAULT_SEARCH_MAX, ge=1, le=SEARCH_MAX_CAP)


class EmrFetchRequest(BaseModel):
    """OpenAI ``fetch`` tool input — memory id from a prior search result."""

    id: str = Field(..., min_length=1, max_length=64)


def citation_url(memory_id: str) -> str:
    """Stable citation URL for ChatGPT deep-research citations."""
    base = (os.getenv("JARVIS_LEDGER_CITATION_BASE") or "").strip()
    if base:
        return f"{base.rstrip('/')}/{memory_id}"
    return f"ledger://{memory_id}"


def _title_from_record(rec: MemoryRecord) -> str:
    if rec.subject and rec.subject.strip():
        return rec.subject.strip()
    content = (rec.content or "").strip()
    if len(content) > 80:
        return content[:77] + "..."
    return content or rec.type


def _title_from_bundle(*, subject: str | None, content: str, type_: str) -> str:
    if subject and subject.strip():
        return subject.strip()
    text = (content or "").strip()
    if len(text) > 80:
        return text[:77] + "..."
    return text or type_


def emr_search(store: JarvisStore, req: EmrSearchRequest) -> dict[str, Any]:
    """Governed recall → OpenAI search result shape (id, title, url)."""
    from app.emr_tool import EmrRecallRequest, emr_recall

    capped = min(req.max_memories, SEARCH_MAX_CAP)
    recall = emr_recall(
        store,
        EmrRecallRequest(
            intent="research",
            query=req.query,
            max_memories=capped,
            include_provenance=False,
            session_key="tool-emr-search",
        ),
    )
    results: list[dict[str, str]] = []
    for item in recall.bundle:
        results.append(
            {
                "id": item.memory_id,
                "title": _title_from_bundle(
                    subject=item.subject,
                    content=item.content,
                    type_=item.type,
                ),
                "url": citation_url(item.memory_id),
            }
        )
    return {"results": results}


def emr_fetch(store: JarvisStore, req: EmrFetchRequest) -> dict[str, Any]:
    """Full LTM record by id — OpenAI fetch result shape."""
    rec = store.get_memory(req.id)
    if rec is None:
        raise ValueError(f"Memory not found: {req.id}")
    return _fetch_payload(rec)


def _fetch_payload(rec: MemoryRecord) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "type": rec.type,
        "status": rec.status,
        "subject": rec.subject,
        "tags": list(rec.tags),
        "evidence": [e.model_dump() for e in rec.evidence],
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "content_sha256": rec.content_sha256,
        "source_agent": rec.source_agent,
        "session_id": rec.session_id,
        "confidence": rec.confidence,
        "supersedes": rec.supersedes,
    }
    return {
        "id": rec.id,
        "title": _title_from_record(rec),
        "text": rec.content,
        "url": citation_url(rec.id),
        "metadata": metadata,
    }


# OpenAI / tool-calling schema exports
EMR_SEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search",
        "description": (
            "Read-only company knowledge search over the Continuity Ledger. "
            "Returns memory ids, titles, and citation URLs. Does not write or mutate truth."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query",
                },
                "max_memories": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": SEARCH_MAX_CAP,
                    "default": DEFAULT_SEARCH_MAX,
                },
            },
            "required": ["query"],
        },
    },
}

EMR_FETCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "fetch",
        "description": (
            "Read-only fetch of one Continuity Ledger memory by id (from search). "
            "Returns full text and metadata. Does not write or mutate truth."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Memory id from a prior search result",
                },
            },
            "required": ["id"],
        },
    },
}

# Aliases for hosts that namespace EMR tools
EMR_SEARCH_ALIAS_SCHEMA: dict[str, Any] = {
    **EMR_SEARCH_TOOL_SCHEMA,
    "function": {
        **EMR_SEARCH_TOOL_SCHEMA["function"],
        "name": "emr_search",
    },
}

EMR_FETCH_ALIAS_SCHEMA: dict[str, Any] = {
    **EMR_FETCH_TOOL_SCHEMA,
    "function": {
        **EMR_FETCH_TOOL_SCHEMA["function"],
        "name": "emr_fetch",
    },
}
