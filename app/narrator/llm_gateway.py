"""llm-gateway adapter: POST {base}/chat/complete (Jon's own gateway).

Request/response shape mirrors app/amul_llm.py's existing call so the twin
speaks to the gateway exactly as AMUL does.
"""

from __future__ import annotations

import time

from .base import NarrationRequest, NarrationResponse, NarratorError, ProviderConfig, read_api_key


class LlmGatewayAdapter:
    name = "llm_gateway"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg

    def narrate(self, req: NarrationRequest) -> NarrationResponse:
        import httpx

        headers = {"Content-Type": "application/json"}
        key = read_api_key(self.cfg)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": req.model or self.cfg.model,
            "messages": [
                {"role": "system", "content": req.system_prompt},
                {"role": "user", "content": req.user_prompt},
            ],
            "params": {"temperature": req.temperature, "max_tokens": req.max_tokens},
        }
        url = self.cfg.base_url.rstrip("/") + "/chat/complete"
        last_exc: Exception | None = None
        for _ in range(2):
            t0 = time.monotonic()
            try:
                resp = httpx.post(url, headers=headers, json=body, timeout=req.timeout_s)
                if resp.status_code // 100 != 2:
                    raise NarratorError("NARRATOR_HTTP", f"llm-gateway returned HTTP {resp.status_code}")
                data = resp.json()
                text = data.get("content") or data.get("reasoning") or ""
                return NarrationResponse(
                    text=text, provider=self.name,
                    model=str(data.get("model") or body["model"] or ""),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    usage=data.get("usage"),
                )
            except NarratorError:
                raise
            except Exception as exc:
                last_exc = exc
        raise NarratorError("NARRATOR_HTTP", f"llm-gateway failed after retry: {type(last_exc).__name__}")
