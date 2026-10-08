"""Twin State v1 — the deterministic fact sheet a narrator may describe.

Built on top of the hardened twin (app/twin.py).  Pure functions, no I/O,
injected ``now``.  Every field is copied verbatim from tenant-scoped,
filtered records — never paraphrased — so the narration gate can check a
model's sentences against exact state values.

Field rules (each is also documented in docs/AI_TWIN.md):
  active_projects        distinct TAGS with >=1 live non-twin record in the
                         last ACTIVE_WINDOW_DAYS days.  Tags are the
                         ledger's shared grouping axis; subjects are
                         free-text phrases, not prefixes.
  recent_accomplishments status == "verified", newest first, top 5;
                         {record_id, subject, summary} with summary copied
                         verbatim from record content.
  open_risks             unresolved ConflictSets (verbatim subject + record
                         ids) plus records tagged exactly "risk" — the tag
                         is the whole convention; "security"/"todo" do NOT
                         widen it.
  stale_commitments      type == "task", not archived, not superseded, and
                         no newer non-twin record on the same subject in the
                         last STALE_DAYS days.
  confidence             deliberately absent — coverage_index is the only
                         scalar; a second number would be undefended.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .models import MemoryRecord
from .twin import (
    FilteredRecords,
    _require_filtered,
    _parse_ts,
    coverage_index,
    twin_components,
    twin_conflicts,
    twin_input_digest,
    twin_mission,
    weakest_component,
)

SCHEMA = "TwinState.v1"
ACTIVE_WINDOW_DAYS = 14
STALE_DAYS = 14
RISK_TAG = "risk"          # exact tag; deliberately does not include "security"/"todo"
TOP_ACCOMPLISHMENTS = 5


def _iso_now(now: datetime | None) -> str | None:
    return now.astimezone(timezone.utc).isoformat() if now else None


def _live(records: tuple[MemoryRecord, ...]) -> list[MemoryRecord]:
    return [r for r in records if r.status != "archived"]


def _active_projects(records: list[MemoryRecord], now: datetime | None) -> list[str]:
    if now is None:
        return sorted({t for r in records for t in r.tags})
    cutoff = now.timestamp() - ACTIVE_WINDOW_DAYS * 86400
    tags = {
        t for r in records
        if (ts := _parse_ts(r.created_at)) is not None and ts.timestamp() >= cutoff
        for t in r.tags
    }
    return sorted(tags)


def _recent_accomplishments(records: list[MemoryRecord]) -> list[dict[str, str]]:
    verified = [r for r in records if r.status == "verified"]
    verified.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return [
        {"record_id": r.id, "subject": r.subject or "", "summary": r.content}
        for r in verified[:TOP_ACCOMPLISHMENTS]
    ]


def _open_risks(records: list[MemoryRecord], conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    for c in conflicts:
        if c["unresolved"]:
            risks.append({
                "kind": "conflict",
                "subject": c["subject"],
                "record_ids": sorted(c["record_ids"]),  # input-order independent
            })
    tagged = sorted(
        (r for r in records if RISK_TAG in (r.tags or [])),
        key=lambda r: r.id,
    )
    for r in tagged:
        risks.append({"kind": "tag", "record_id": r.id, "text": r.content})
    risks.sort(key=lambda r: (
        r["kind"], r.get("subject", ""),
        tuple(r.get("record_ids", [])), r.get("record_id", ""),
    ))
    return risks


def _stale_commitments(
    records: list[MemoryRecord], now: datetime | None
) -> list[dict[str, Any]]:
    if now is None:
        return []
    # Newest record per subject across the filtered set.
    newest_by_subject: dict[str, float] = {}
    for r in records:
        if not r.subject:
            continue
        ts = _parse_ts(r.created_at)
        if ts is None:
            continue
        newest_by_subject[r.subject] = max(
            newest_by_subject.get(r.subject, float("-inf")), ts.timestamp()
        )
    superseded_ids = {
        r.supersedes for r in records
        if r.supersedes and r.supersedes in {x.id for x in records}
    }
    cutoff = now.timestamp() - STALE_DAYS * 86400
    stale = []
    for r in records:
        if r.type != "task" or r.id in superseded_ids:
            continue
        ts = _parse_ts(r.created_at)
        if ts is None:
            continue
        # For a subjectless task its own timestamp is the last activity;
        # otherwise the newest same-subject record's timestamp.
        last_activity = newest_by_subject.get(r.subject, ts.timestamp()) if r.subject else ts.timestamp()
        if last_activity < cutoff:
            stale.append({
                "record_id": r.id,
                "subject": r.subject or "",
                "summary": r.content,
                "days_since_update": int((now.timestamp() - last_activity) / 86400),
            })
    stale.sort(key=lambda s: (s["record_id"]))
    return stale


def build_twin_state(
    records: FilteredRecords,
    *,
    identity_id: str = "default",
    now: datetime | None = None,
) -> dict[str, Any]:
    """TwinState.v1 — deterministic, citable, digest-bound."""
    fr = _require_filtered(records)
    live = _live(fr.records)
    components = twin_components(fr, now=now)
    index = coverage_index(components)
    conflicts = twin_conflicts(fr)

    state: dict[str, Any] = {
        "schema": SCHEMA,
        "as_of": _iso_now(now),
        "identity": identity_id,
        "twin_input_digest": twin_input_digest(fr),
        "coverage_index": index,
        "disclaimer": (
            "This index measures how well-structured and evidenced the ledger "
            "is. It does not say whether any memory is true."
        ),
        "components": components,
        "weakest_component": weakest_component(components),
        "active_projects": _active_projects(live, now),
        "recent_accomplishments": _recent_accomplishments(live),
        "open_risks": _open_risks(live, conflicts),
        "stale_commitments": _stale_commitments(live, now),
        "recommended_mission": twin_mission(components),
        "record_count": len(fr),
        "skipped_records": list(fr.skipped),
    }
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    state["state_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return state
