"""Anthropic adapter: POST {base}/v1/messages (Messages API)."""

from __future__ import annotations

import time

from .base import NarrationRequest, NarrationResponse, NarratorError, ProviderConfig, read_api_key


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg

    def narrate(self, req: NarrationRequest) -> NarrationResponse:
        import httpx

        key = read_api_key(self.cfg)
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if key:
            headers["x-api-key"] = key
        body = {
            "model": req.model or self.cfg.model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "system": req.system_prompt,
            "messages": [{"role": "user", "content": req.user_prompt}],
        }
        url = self.cfg.base_url.rstrip("/") + "/v1/messages"
        last_exc: Exception | None = None
        for _ in range(2):
            t0 = time.monotonic()
            try:
                resp = httpx.post(url, headers=headers, json=body, timeout=req.timeout_s)
                if resp.status_code // 100 != 2:
                    raise NarratorError("NARRATOR_HTTP", f"anthropic returned HTTP {resp.status_code}")
                data = resp.json()
                parts = data.get("content") or []
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                return NarrationResponse(
                    text=text, provider=self.name,
                    model=str(data.get("model") or body["model"]),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    usage=data.get("usage"),
                )
            except NarratorError:
                raise
            except Exception as exc:
                last_exc = exc
        raise NarratorError("NARRATOR_HTTP", f"anthropic failed after retry: {type(last_exc).__name__}")
