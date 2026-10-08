"""Template narrator — the deterministic fallback and default.

Writes the five narration sections straight from TwinState with fixed
sentence templates.  Every number and entity in every sentence is inside its
own cites, so the output passes the gate by construction; the property test
proves that over 500 random ledgers.  Used when the provider is ``none``,
when the model fails, and for any section the gate emptied.
"""

from __future__ import annotations

from typing import Any

from .gate import SECTIONS


def template_narration(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    idx = state.get("coverage_index", 0.0)
    n = state.get("record_count", 0)
    weakest = state.get("weakest_component", "")
    mission = state.get("recommended_mission", "")
    projects = state.get("active_projects", [])
    risks = state.get("open_risks", [])
    stale = state.get("stale_commitments", [])
    accompl = state.get("recent_accomplishments", [])

    sections: dict[str, list[dict[str, Any]]] = {s: [] for s in SECTIONS}

    assessment = [
        {"text": f"Coverage index {idx:.2f} over {n} ledger memories.",
         "cites": ["coverage_index", "record_count"]},
        {"text": f"Weakest component is {weakest}.",
         "cites": ["weakest_component"]},
    ]
    if projects:
        listed = ", ".join(projects[:8])
        assessment.append({"text": f"Active areas: {listed}.",
                           "cites": ["active_projects"]})
    sections["assessment"] = assessment

    if accompl:
        top = accompl[0]
        sections["opportunity"] = [
            {"text": f"Latest recorded accomplishment: {top['summary']}",
             "cites": [f"recent_accomplishments[0].summary",
                       f"recent_accomplishments[0].record_id"]},
            {"text": mission, "cites": ["recommended_mission"]},
        ]
    else:
        sections["opportunity"] = [
            {"text": mission, "cites": ["recommended_mission"]},
        ]

    if risks:
        for i, r in enumerate(risks):
            if r.get("kind") == "conflict":
                ids = ", ".join(r["record_ids"])
                sections["risk"].append({
                    "text": f"Unresolved conflict on {r['subject']} across records {ids}.",
                    "cites": [f"open_risks[{i}].subject", f"open_risks[{i}].record_ids"],
                })
            else:
                sections["risk"].append({
                    "text": f"Risk: {r['text']}",
                    "cites": [f"open_risks[{i}].text", f"open_risks[{i}].record_id"],
                })
    else:
        sections["risk"].append({"text": "No unresolved conflicts or tagged risks.",
                                 "cites": ["open_risks"]})

    if stale:
        s = stale[0]
        sections["next_action"] = [
            {"text": mission, "cites": ["recommended_mission"]},
            {"text": f"Oldest stale task on {s['subject']} idle {s['days_since_update']} days.",
             "cites": ["stale_commitments[0].subject", "stale_commitments[0].days_since_update",
                       "stale_commitments[0].record_id"]},
        ]
    else:
        sections["next_action"] = [{"text": mission, "cites": ["recommended_mission"]}]

    sections["explanation"] = [
        {"text": state.get("disclaimer", ""), "cites": ["disclaimer"]},
        {"text": f"Computed deterministically from {n} records; nothing here is a model claim.",
         "cites": ["record_count", "schema"]},
    ]
    return sections
