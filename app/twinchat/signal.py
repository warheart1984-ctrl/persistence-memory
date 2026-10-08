"""Optional HumanSignal module — a bounded style hint, never a dependency.

Enabled only by explicit operator opt-in (``JARVIS_TWIN_SIGNAL_ENABLED`` +
``JARVIS_TWIN_SIGNAL_URL``, allowlist-checked). When on, the raw user
message is POSTed to ``{url}/compile`` — that data flow is documented in
the operator docs. 1.5s timeout, no retries; any failure → ``available=
False`` and the pipeline continues. The reading lands on the receipt as a
style hint — it is never citable evidence and never raw spans.
"""

from __future__ import annotations

import os

from app.narrator.base import check_url_allowed

from .models import SignalReading

TIMEOUT_S = 1.5


def signal_enabled() -> bool:
    return os.getenv("JARVIS_TWIN_SIGNAL_ENABLED", "").strip().lower() in ("1", "true", "yes")


def compile_turn(message: str) -> SignalReading | None:
    """None when disabled or unreachable; never raises."""
    if not signal_enabled():
        return None
    url = os.getenv("JARVIS_TWIN_SIGNAL_URL", "").strip()
    if not url:
        return SignalReading(available=False)
    try:
        check_url_allowed(url)
    except Exception:
        return SignalReading(available=False)
    try:
        import httpx

        resp = httpx.post(
            url.rstrip("/") + "/compile",
            json={"text": message},
            timeout=TIMEOUT_S,
        )
        if resp.status_code // 100 != 2:
            return SignalReading(available=False)
        data = resp.json()
        emo = data.get("emotional_state") or {}
        style = data.get("recommended_response_style") or {}
        strength = data.get("signal_strength") or {}
        return SignalReading(
            available=True,
            dominant_emotion=emo.get("dominant"),
            signal_strength=strength.get("score") if isinstance(strength, dict) else None,
            recommended_style=style.get("style") if isinstance(style, dict) else None,
            evidence_features=[
                str(e.get("feature"))
                for e in (emo.get("evidence") or [])[:8]
                if isinstance(e, dict) and e.get("feature")
            ],
        )
    except Exception:
        return SignalReading(available=False)
