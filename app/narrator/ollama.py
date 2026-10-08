"""Ollama adapter: POST {base}/api/chat (native local endpoint, stream off)."""

from __future__ import annotations

import time

from .base import NarrationRequest, NarrationResponse, NarratorError, ProviderConfig


class OllamaAdapter:
    name = "ollama"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg

    def narrate(self, req: NarrationRequest) -> NarrationResponse:
        import httpx

        body = {
            "model": req.model or self.cfg.model,
            "messages": [
                {"role": "system", "content": req.system_prompt},
                {"role": "user", "content": req.user_prompt},
            ],
            "stream": False,
            "options": {"temperature": req.temperature, "num_predict": req.max_tokens},
        }
        if req.json_mode:
            body["format"] = "json"
        url = (self.cfg.base_url or "http://localhost:11434").rstrip("/") + "/api/chat"
        last_exc: Exception | None = None
        for _ in range(2):
            t0 = time.monotonic()
            try:
                resp = httpx.post(url, json=body, timeout=req.timeout_s)
                if resp.status_code // 100 != 2:
                    raise NarratorError("NARRATOR_HTTP", f"ollama returned HTTP {resp.status_code}")
                data = resp.json()
                text = (data.get("message") or {}).get("content") or ""
                usage = None
                if "eval_count" in data or "prompt_eval_count" in data:
                    usage = {
                        "completion_tokens": data.get("eval_count"),
                        "prompt_tokens": data.get("prompt_eval_count"),
                    }
                return NarrationResponse(
                    text=text, provider=self.name,
                    model=str(data.get("model") or body["model"]),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    usage=usage,
                )
            except NarratorError:
                raise
            except Exception as exc:
                last_exc = exc
        raise NarratorError("NARRATOR_HTTP", f"ollama failed after retry: {type(last_exc).__name__}")
