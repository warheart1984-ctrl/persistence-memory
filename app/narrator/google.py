"""Google adapter: POST {base}/v1beta/models/{model}:generateContent.

The API key travels in the x-goog-api-key header, never the URL — URLs get
logged and receipts must never carry secrets.
"""

from __future__ import annotations

import time

from .base import NarrationRequest, NarrationResponse, NarratorError, ProviderConfig, read_api_key


class GoogleAdapter:
    name = "google"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg

    def narrate(self, req: NarrationRequest) -> NarrationResponse:
        import httpx

        key = read_api_key(self.cfg)
        headers = {"content-type": "application/json"}
        if key:
            headers["x-goog-api-key"] = key
        model = req.model or self.cfg.model
        body = {
            "system_instruction": {"parts": [{"text": req.system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": req.user_prompt}]}],
            "generationConfig": {
                "temperature": req.temperature,
                "maxOutputTokens": req.max_tokens,
            },
        }
        if req.json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        url = self.cfg.base_url.rstrip("/") + f"/v1beta/models/{model}:generateContent"
        last_exc: Exception | None = None
        for _ in range(2):
            t0 = time.monotonic()
            try:
                resp = httpx.post(url, headers=headers, json=body, timeout=req.timeout_s)
                if resp.status_code // 100 != 2:
                    raise NarratorError("NARRATOR_HTTP", f"google returned HTTP {resp.status_code}")
                data = resp.json()
                parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                return NarrationResponse(
                    text=text, provider=self.name,
                    model=str(data.get("modelVersion") or model),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    usage=data.get("usageMetadata"),
                )
            except NarratorError:
                raise
            except Exception as exc:
                last_exc = exc
        raise NarratorError("NARRATOR_HTTP", f"google failed after retry: {type(last_exc).__name__}")
