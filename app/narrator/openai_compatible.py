"""OpenAI-compatible adapter: POST {base}/chat/completions.

Covers OpenAI, xAI/Grok, Groq, Together, Mistral, DeepSeek, OpenRouter,
LM Studio, vLLM, llama.cpp server, Ollama's /v1 endpoint — any
/v1/chat/completions server. Request shape matches amul_llm's existing call.
"""

from __future__ import annotations

import time

from .base import NarrationRequest, NarrationResponse, NarratorError, ProviderConfig, read_api_key


class OpenAICompatibleAdapter:
    name = "openai_compatible"

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
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.json_mode:
            body["response_format"] = {"type": "json_object"}
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        return _post(url, headers, body, req, self.name)


def _post(url: str, headers: dict, body: dict, req: NarrationRequest, provider: str) -> NarrationResponse:
    import httpx

    last_exc: Exception | None = None
    for attempt in range(2):  # at most one retry
        t0 = time.monotonic()
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=req.timeout_s)
            if resp.status_code // 100 != 2:
                raise NarratorError("NARRATOR_HTTP", f"{provider} returned HTTP {resp.status_code}")
            data = resp.json()
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content") or msg.get("reasoning_content") or ""
            return NarrationResponse(
                text=text, provider=provider,
                model=str(data.get("model") or body.get("model") or ""),
                latency_ms=int((time.monotonic() - t0) * 1000),
                usage=data.get("usage"),
            )
        except NarratorError:
            raise
        except Exception as exc:  # httpx.TimeoutException, TransportError, JSON, ...
            last_exc = exc
    raise NarratorError("NARRATOR_HTTP", f"{provider} failed after retry: {type(last_exc).__name__}")
