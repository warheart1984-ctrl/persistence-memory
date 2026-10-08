"""AI Twin: deterministic coverage index over a tenant's Continuity Ledger.

The twin is not a learned model.  It is a pure function over the ledger's own
records: every output (index, brief, mission, explanation) is derived from the
same component values, so an explanation can never drift from the number it
explains.  The twin may write its briefs back into the ledger as ordinary
memories (``source_agent="ai-twin"``, ``type="research"``) through the normal
authenticated write path — but those records are excluded from its inputs, so
the twin can never raise its own standing by generating.

What the number is NOT: the coverage index measures how well-structured and
evidenced the ledger is.  It does not say whether any memory is true.

Invariants (mirrors the ledger's constitutional rules):
  - Sovereignty: the twin scores only the records it is handed.  Tenant
    isolation is enforced below this layer (RLS / store); the twin has no
    cross-tenant view by construction.
  - No silent merge: unresolved ConflictSets are surfaced verbatim with their
    record ids.  The twin reports disagreement; it never adjudicates it.
  - Fail closed: missing data yields 0 / "no data" / refusal — never
    "healthy" by default.  Records that fail validation are skipped and
    counted, never silently included.
  - No self-grading: every computation runs on ``FilteredRecords``, which can
    only be built through the twin-authorship filter.  A twin-authored
    supersede or evidence link cannot change any component.
  - Determinism: same filtered records in any order → byte-identical output.
    Wall-clock time is only consumed through the injected ``now``.

Status: partial — core proven by tests/test_twin.py; endpoint is dark by
default (JARVIS_TWIN_ENABLED).  See docs/AI_TWIN.md.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from .continuity import content_sha256, detect_conflicts
from .models import MemoryRecord, MemoryType

TWIN_AGENT = "ai-twin"

DISCLAIMER = (
    "This index measures how well-structured and evidenced the ledger is. "
    "It does not say whether any memory is true."
)

# Weighted component vector.  Weights sum to 1.0 — every point on the scale
# has a named, inspectable cause.
_WEIGHTS: dict[str, float] = {
    "V": 0.20,  # verified: status == "verified" ratio
    "P": 0.16,  # provenance: records carrying >=1 evidence link
    "L": 0.12,  # lineage: records in a supersedes chain (either direction)
    "W": 0.12,  # weight: ledger depth, saturates at 50 records
    "S": 0.12,  # coverage: distinct MemoryTypes in use, out of 7
    "T": 0.10,  # temporal: active span of the ledger, saturates at 90 days
    "C": 0.10,  # conflict health: benign-duplicate sets / all sets
    "N": 0.08,  # participation: distinct non-twin source_agents, sat. at 5
}

_VALID_TYPES = frozenset(MemoryType.__args__)  # type: ignore[attr-defined]
_W_SATURATION = 50
_T_SATURATION_DAYS = 90
_N_SATURATION = 5

DIGEST_TAG_PREFIX = "twin_digest:"


def is_twin_authored(record: Any) -> bool:
    """Single decision point for twin authorship — used by every input path.

    NOTE (not proven): ``source_agent`` is caller-supplied free text on
    MemoryCreate; the store does not stamp a verified writer identity.  Any
    caller with write access can spoof it.  A future verified-identity step
    (Wicket witness) makes this check trustworthy.  Until then the twin also
    refuses to *count* records whose agent is missing or blank.
    """
    return getattr(record, "source_agent", None) == TWIN_AGENT


def _clamp(v: float) -> float:
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        return 0.0
    return max(0.0, min(1.0, float(v)))


class FilteredRecords:
    """The only record set twin computations may read.

    Built exclusively through ``from_records`` — which drops twin-authored
    records (self-dealing) and records that fail MemoryRecord validation
    (fail-closed).  Component functions require this wrapper so a raw list
    can never reach them.
    """

    __slots__ = ("records", "skipped", "twin_ids")

    def __init__(
        self,
        records: tuple[MemoryRecord, ...],
        skipped: list[dict[str, Any]],
        twin_ids: frozenset[str],
    ):
        self.records = records
        self.skipped = skipped
        # Ids of twin-authored records seen in the input — kept so an evidence
        # ref or supersedes link pointing at twin output can be discounted
        # even though the twin record itself is invisible to scoring.
        self.twin_ids = twin_ids

    @classmethod
    def from_records(cls, raw: Iterable[Any]) -> "FilteredRecords":
        kept: list[MemoryRecord] = []
        skipped: list[dict[str, Any]] = []
        twin_ids: set[str] = set()
        for item in raw:
            if isinstance(item, MemoryRecord):
                rec = item
            elif isinstance(item, Mapping):
                try:
                    rec = MemoryRecord.model_validate(item)
                except Exception:
                    skipped.append({
                        "id": str(item.get("id", "")),
                        "reason": "invalid_record",
                    })
                    continue
            else:
                skipped.append({"id": "", "reason": "not_a_record"})
                continue
            if is_twin_authored(rec):
                twin_ids.add(rec.id)
                continue  # twin output is excluded, not "skipped" data
            kept.append(rec)
        return cls(tuple(kept), skipped, frozenset(twin_ids))

    def __len__(self) -> int:
        return len(self.records)


def _require_filtered(records: Any) -> FilteredRecords:
    if not isinstance(records, FilteredRecords):
        raise TypeError(
            "twin components require FilteredRecords — build via "
            "FilteredRecords.from_records() so the twin-authorship gate applies"
        )
    return records


def _lineage_ids(records: tuple[MemoryRecord, ...]) -> set[str]:
    """Ids in a *resolved* supersedes chain — both ends must be in the set.

    A supersedes pointer to a filtered-out (twin) or missing record is a
    dangling reference, not lineage; counting it would let twin output leak
    into L through other records' pointers.
    """
    ids = {r.id for r in records}
    involved: set[str] = set()
    for r in records:
        if r.supersedes and r.supersedes in ids:
            involved.add(r.id)
            involved.add(r.supersedes)
    return involved


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _temporal_span_days(records: tuple[MemoryRecord, ...], now: datetime | None) -> float:
    stamps = [ts for ts in (_parse_ts(r.created_at) for r in records) if ts is not None]
    if len(stamps) < 2:
        return 0.0
    # Never let a future-dated record inflate the span: the top of the window
    # is the earlier of the newest record and the injected clock.
    top = max(stamps)
    if now is not None and now < top:
        top = now
    span = (top - min(stamps)).total_seconds() / 86400.0
    return span if span > 0 else 0.0


def twin_input_digest(records: FilteredRecords) -> str:
    """SHA-256 over sorted ``id|version|content_sha256`` of the filtered input.

    Anyone can recompute this over the same ledger state to confirm which
    records a brief was computed from.  Uses ``content_sha256`` because
    ``row_hash`` lives inside the pg history tables and is not on the wire.
    """
    fr = _require_filtered(records)
    # content_sha256 may be empty on records that never passed through the
    # store's write path; fall back to the canonical content hash exactly as
    # detect_conflicts does.
    lines = sorted(
        f"{r.id}|{r.version}|{r.content_sha256 or content_sha256(r.content)}"
        for r in fr.records
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def twin_conflicts(records: FilteredRecords) -> list[dict[str, Any]]:
    """Conflict sets over FILTERED records only.

    Twin-authored records do not exist for this computation — so a
    twin-authored supersede can never collapse a conflict set, and C counts
    only resolutions that came from non-twin records.  Inputs are never
    mutated.
    """
    fr = _require_filtered(records)
    sets = detect_conflicts(list(fr.records))
    out = []
    for s in sets:
        out.append({
            "subject": s.subject,
            "unresolved": s.unresolved,
            "record_ids": [m.id for m in s.memories],
            "policy_hint": s.policy_hint,
        })
    return out


def twin_components(records: FilteredRecords, *, now: datetime | None = None) -> dict[str, float]:
    """The 8-component vector over *one tenant's* filtered records.

    Unknown status/type values can never inflate V or S: MemoryRecord
    validation rejects them at the filter boundary, and the S count is
    intersected with the declared MemoryType set regardless.
    """
    fr = _require_filtered(records)
    base = fr.records
    total = len(base)
    if not total:
        # No records = no signal; "no conflicts" is not evidence of health.
        return {k: 0.0 for k in _WEIGHTS}

    verified = sum(1 for r in base if r.status == "verified")
    # An evidence link whose ref resolves to a twin-authored record id does not
    # count as provenance — the twin cannot be cited to justify a record.
    provenance = sum(
        1 for r in base
        if r.evidence and any(e.ref not in fr.twin_ids for e in r.evidence)
    )
    lineage = len(_lineage_ids(base))
    distinct_types = len({r.type for r in base} & _VALID_TYPES)
    agents = {r.source_agent for r in base if r.source_agent and not is_twin_authored(r)}
    conflicts = twin_conflicts(fr)
    benign = sum(1 for c in conflicts if not c["unresolved"])

    return {
        "V": _clamp(verified / total),
        "P": _clamp(provenance / total),
        "L": _clamp(lineage / total),
        "W": _clamp(total / _W_SATURATION),
        "S": _clamp(distinct_types / len(_VALID_TYPES)),
        "T": _clamp(_temporal_span_days(base, now) / _T_SATURATION_DAYS),
        "C": _clamp(benign / len(conflicts)) if conflicts else 1.0,
        "N": _clamp(len(agents) / _N_SATURATION),
    }


def coverage_index(components: Mapping[str, float]) -> float:
    value = sum(_WEIGHTS[k] * _clamp(components.get(k, 0.0)) for k in _WEIGHTS)
    return round(_clamp(value), 4)


def weakest_component(components: Mapping[str, float]) -> str:
    return min(_WEIGHTS, key=lambda k: _clamp(components.get(k, 0.0)))


_EMPTY_MISSION = "Write the first evidenced memory."
_MISSIONS: dict[str, str] = {
    "V": "Promote draft memories to verified — attach evidence and confirm them.",
    "P": "Attach evidence links (paths, test ids, receipts) to records that have none.",
    "L": "Record corrections with supersedes=<id> instead of overwriting, so lineage is preserved.",
    "W": "Persist more durable decisions and facts — the ledger is still shallow.",
    "S": "Diversify memory types — decisions, tasks, and architecture are underrepresented.",
    "T": "The ledger is young. Keep writing across sessions to build temporal depth.",
    "C": "Resolve open conflict sets — adjudicate or supersede one side with evidence.",
    "N": "More agents should write to this ledger — multi-agent participation is thin.",
}


def twin_mission(components: Mapping[str, float]) -> str:
    if all(_clamp(components.get(k, 0.0)) == 0.0 for k in _WEIGHTS):
        return _EMPTY_MISSION
    return _MISSIONS[weakest_component(components)]


def generate_twin_intelligence(
    records: FilteredRecords,
    *,
    identity_id: str = "default",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Daily intelligence packet — the twin's whole output, fully explainable.

    ``records`` must be a ``FilteredRecords`` built from an already
    tenant-scoped source; raw lists are refused.  ``now`` is injected —
    the core never reads the wall clock.
    """
    fr = _require_filtered(records)
    components = twin_components(fr, now=now)
    index = coverage_index(components)
    conflicts = twin_conflicts(fr)
    open_conflicts = [c for c in conflicts if c["unresolved"]]

    brief = [
        (
            f"{len(fr)} {'memory' if len(fr) == 1 else 'memories'} on the "
            f"ledger; coverage index {index:.2f}."
        ) if len(fr) else "No data on the ledger.",
        (
            f"{len(open_conflicts)} unresolved conflict set"
            f"{'s' if len(open_conflicts) != 1 else ''} "
            f"({'; '.join(c['subject'] + ': ' + ' vs '.join(c['record_ids']) for c in open_conflicts)})."
            if open_conflicts else "No unresolved conflicts."
        ),
        f"Weakest component: {weakest_component(components)}.",
    ]

    return {
        "identity": identity_id,
        "generated_at": now.astimezone(timezone.utc).isoformat() if now else None,
        "coverage_index": index,
        "disclaimer": DISCLAIMER,
        "components": components,
        "skipped_records": list(fr.skipped),
        "twin_input_digest": twin_input_digest(fr),
        "brief": brief,
        "mission": twin_mission(components),
        "conflicts": {
            "unresolved": len(open_conflicts),
            "sets": conflicts,  # verbatim, with record ids — never adjudicated
            "policy_hint": "Do not merge. Ledger preserves both records with provenance.",
        },
        "explanations": {
            "coverage_index": {
                "target": "coverage_index",
                "reasoning": (
                    f"Index driven primarily by verified records (V={components['V']:.2f}) "
                    f"and provenance coverage (P={components['P']:.2f}); "
                    f"weakest is {weakest_component(components)}. {DISCLAIMER}"
                ),
                "payload": {"components": components, "weights": _WEIGHTS},
            },
        },
        "reasoning": [
            "Loaded tenant-scoped records through the twin-authorship filter.",
            f"Skipped {len(fr.skipped)} record(s) that failed validation.",
            "Computed 8-component vector over status, evidence, lineage, types, span, conflicts, agents.",
            "Derived mission from weakest component.",
            "Surfaced conflict sets verbatim with record ids.",
        ],
    }


def twin_memory_payload(
    packet: Mapping[str, Any],
    session_id: str,
    *,
    day: str,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """A MemoryCreate-shaped dict for the normal authenticated write path.

    ``day`` (YYYY-MM-DD from the injected clock) keys the subject, so idempotent
    persist lookups never link across days: a same-day rewrite supersedes the
    earlier record; the first persist of a new day links nothing.
    """
    digest = str(packet.get("twin_input_digest", ""))
    return {
        "content": " | ".join(packet["brief"]) + " Mission: " + packet["mission"],
        "source_agent": TWIN_AGENT,
        "session_id": session_id,
        "type": "research",
        "confidence": float(packet.get("coverage_index", 0.0)),
        "status": "draft",
        "subject": f"twin:daily:{packet['identity']}:{day}",
        "tags": ["twin-brief", f"{DIGEST_TAG_PREFIX}{digest}"],
        "supersedes": supersedes,
    }
