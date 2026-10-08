"""AI Twin: deterministic self-model over a tenant's Continuity Ledger.

The twin is not a learned model.  It is a pure function over the ledger's own
records: every output (score, brief, mission, explanation) is derived from the
same component values, so an explanation can never drift from the decision it
explains.  The twin writes its briefs back into the ledger as ordinary
memories (``source_agent="ai-twin"``, ``type="research"``) so its own outputs
are evidence with lineage — but those records are excluded from its scoring
inputs, so the twin can never raise its own standing by generating.

Invariants (mirrors the ledger's constitutional rules):
  - Sovereignty: the twin scores only the records it is handed.  Tenant
    isolation is enforced below this layer (RLS / store); the twin has no
    cross-tenant view by construction.
  - No silent merge: unresolved ConflictSets are surfaced verbatim.  The twin
    reports disagreement; it never adjudicates it.
  - Fail closed: a ledger with zero scoreable records yields score 0 and a
    mission to produce evidence — never an invented narrative.

Status: declared — sketch for the next deploy.  Pure functions; no I/O.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import MemoryRecord

TWIN_AGENT = "ai-twin"

# Records the twin wrote about itself can never feed its own score.
_SELF_DEALING_AGENTS = frozenset({TWIN_AGENT})

# Weighted component vector.  Weights sum to 1.0 — every point on the scale
# has a named, inspectable cause.
_WEIGHTS: dict[str, float] = {
    "V": 0.20,  # verified: status == "verified" ratio
    "P": 0.16,  # provenance: records carrying >=1 evidence link
    "L": 0.12,  # lineage: records in a supersedes chain (either direction)
    "W": 0.12,  # weight: ledger depth, saturates at 50 records
    "S": 0.12,  # coverage: distinct MemoryTypes in use, out of 7
    "T": 0.10,  # temporal: active span of the ledger, saturates at 90 days
    "C": 0.10,  # conflict health: resolved sets / all sets (1.0 if none)
    "N": 0.08,  # participation: distinct non-twin source_agents, sat. at 5
}

_MEMORY_TYPES = 7  # len of the MemoryType literal
_W_SATURATION = 50
_T_SATURATION_DAYS = 90
_N_SATURATION = 5


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def _scoreable(records: list[MemoryRecord]) -> list[MemoryRecord]:
    """Drop the twin's own outputs so measurement is never participation."""
    return [r for r in records if r.source_agent not in _SELF_DEALING_AGENTS]


def _lineage_ids(records: list[MemoryRecord]) -> set[str]:
    """Ids involved in a supersedes chain — pointed at or pointing."""
    ids = {r.id for r in records}
    involved: set[str] = set()
    for r in records:
        if r.supersedes:
            involved.add(r.id)
            if r.supersedes in ids:
                involved.add(r.supersedes)
    return involved


def _temporal_span_days(records: list[MemoryRecord]) -> float:
    stamps = []
    for r in records:
        try:
            stamps.append(datetime.fromisoformat(r.created_at.replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            continue
    if len(stamps) < 2:
        return 0.0
    return max(0.0, (max(stamps) - min(stamps)).total_seconds() / 86400.0)


def twin_components(
    records: list[MemoryRecord],
    conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
    """Compute the component vector over *one tenant's* records.

    ``conflicts`` items are ConflictSet-shaped dicts ({subject, unresolved}).
    """
    base = _scoreable(records)
    total = len(base)
    if not total:
        # No records = no signal; "no conflicts" is not evidence of health.
        return {k: 0.0 for k in _WEIGHTS}

    verified = sum(1 for r in base if r.status == "verified")
    provenance = sum(1 for r in base if r.evidence)
    lineage = len(_lineage_ids(base) & {r.id for r in base})
    distinct_types = len({r.type for r in base})
    distinct_agents = len({r.source_agent for r in base})

    sets = conflicts or []
    resolved = sum(1 for c in sets if not c.get("unresolved", True))
    conflict_health = resolved / len(sets) if sets else 1.0

    return {
        "V": _clamp(verified / total),
        "P": _clamp(provenance / total),
        "L": _clamp(lineage / total),
        "W": _clamp(total / _W_SATURATION),
        "S": _clamp(distinct_types / _MEMORY_TYPES),
        "T": _clamp(_temporal_span_days(base) / _T_SATURATION_DAYS),
        "C": _clamp(conflict_health),
        "N": _clamp(distinct_agents / _N_SATURATION),
    }


def twin_score(components: dict[str, float]) -> float:
    return round(sum(_WEIGHTS[k] * components.get(k, 0.0) for k in _WEIGHTS), 4)


def weakest_component(components: dict[str, float]) -> str:
    return min(_WEIGHTS, key=lambda k: components.get(k, 0.0))


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


def twin_mission(components: dict[str, float]) -> str:
    return _MISSIONS[weakest_component(components)]


def generate_twin_intelligence(
    records: list[MemoryRecord],
    conflicts: list[dict[str, Any]] | None = None,
    *,
    identity_id: str = "default",
) -> dict[str, Any]:
    """Daily intelligence packet — the twin's whole output, fully explainable.

    ``records`` must already be tenant-scoped by the caller/store layer.
    """
    components = twin_components(records, conflicts)
    score = twin_score(components)
    base = _scoreable(records)
    open_conflicts = [c for c in (conflicts or []) if c.get("unresolved", True)]

    brief = [
        f"{len(base)} memories on the ledger; score {score:.2f}.",
        f"{len(open_conflicts)} unresolved conflict set{'s' if len(open_conflicts) != 1 else ''}." if open_conflicts else "No unresolved conflicts.",
        f"Weakest component: {weakest_component(components)}.",
    ]

    return {
        "identity": identity_id,
        "score": score,
        "components": components,
        "brief": brief,
        "mission": twin_mission(components),
        "conflicts": {
            "unresolved": len(open_conflicts),
            "subjects": [str(c.get("subject", "")) for c in open_conflicts],
            # Surfaced verbatim — the twin never adjudicates a ConflictSet.
            "policy_hint": "Do not merge. Ledger preserves both records with provenance.",
        },
        "explanations": {
            "reputation": {
                "target": "reputation",
                "reasoning": (
                    f"Score driven primarily by verified records (V={components['V']:.2f}) "
                    f"and provenance coverage (P={components['P']:.2f}); "
                    f"weakest is {weakest_component(components)}."
                ),
                "payload": {"components": components, "weights": _WEIGHTS},
            },
        },
        "reasoning": [
            "Loaded tenant-scoped memory records (twin's own outputs excluded).",
            "Computed 8-component vector over status, evidence, lineage, types, span, conflicts, agents.",
            "Derived mission from weakest component.",
            "Surfaced unresolved conflict sets verbatim.",
        ],
    }


def twin_memory_payload(packet: dict[str, Any], session_id: str) -> dict[str, Any]:
    """A MemoryCreate-shaped dict to write the twin's brief back into the ledger.

    The caller posts it via the normal write path (with write auth) — the twin
    holds no privileged write channel.  Tagged so retrieval can find twin
    output and scoring can exclude it.
    """
    return {
        "content": " | ".join(packet["brief"]) + " Mission: " + packet["mission"],
        "source_agent": TWIN_AGENT,
        "session_id": session_id,
        "type": "research",
        "confidence": packet["score"],
        "status": "draft",
        "subject": f"twin:daily:{packet['identity']}",
        "tags": ["twin-brief"],
    }
