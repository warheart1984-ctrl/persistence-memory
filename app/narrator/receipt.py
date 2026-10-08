"""TwinNarrationReceipt.v1 — digests over everything that produced the output.

Returned to the caller, never persisted in this task (persist comes later,
behind Wicket's witness).  No API keys or auth headers can appear — digests
cover only prompts and output text.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA = "TwinNarrationReceipt.v1"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_receipt(
    *,
    state: dict[str, Any],
    provider: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    raw_output: str,
    final_sections: dict[str, Any],
    dropped: list[dict[str, Any]],
    fallback_used: bool,
    latency_ms: int,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "state_digest": "sha256:" + str(state.get("state_digest", "")),
        "twin_input_digest": "sha256:" + str(state.get("twin_input_digest", "")),
        "provider": provider,
        "model": model,
        "prompt_digest": _sha(system_prompt + "\n" + user_prompt),
        "raw_output_digest": _sha(raw_output),
        "final_output_digest": _sha(json.dumps(final_sections, sort_keys=True)),
        "dropped": [{"section": d["section"], "index": d["index"], "reason": d["reason"]}
                    for d in dropped],
        "fallback_used": fallback_used,
        "latency_ms": latency_ms,
    }
