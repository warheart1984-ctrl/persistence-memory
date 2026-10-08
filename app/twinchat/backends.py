"""ChatBackend — one protocol for every kind of model, vendor-agnostic by design.

Primary path is llm-gateway (``POST {base}/chat/complete`` — governed,
multi-tenant, upstream vendor is the gateway's problem: Groq / OpenRouter /
NVIDIA NIM / self-hosted).  Fallback path is any configured narrator provider.
Last resort is the deterministic ``none`` renderer — no model call at all.

Secret discipline is the narrator's: an API key lives in the env var NAMED by
``api_key_env``, is read at call time, travels in headers only, and is never
logged, receipted, placed in a URL, or written to the ledger.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from app.narrator import get_adapter
from app.narrator.base import (
    NarrationRequest,
    NarratorError,
    ProviderConfig,
    check_url_allowed,
    read_api_key,
)

Message = dict[str, str]


class BackendResult(BaseModel):
    text: str
    model: str
    latency_ms: int
    usage: dict | None = None


@runtime_checkable
class ChatBackend(Protocol):
    name: str

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_s: float,
    ) -> BackendResult: ...


class GatewayBackend:
    """llm-gateway: multi-message chat through Jon's governed Rust front door."""

    name = "llm-gateway"

    def __init__(self, base_url: str, api_key_env: str = "", default_model: str = ""):
        check_url_allowed(base_url)
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.default_model = default_model

    def _key(self) -> str:
        key = os.getenv(self.api_key_env, "").strip() if self.api_key_env else ""
        if self.api_key_env and not key:
            raise NarratorError(
                "NARRATOR_NO_KEY",
                f"gateway names env var {self.api_key_env!r} but it is unset or empty",
            )
        return key

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_s: float,
    ) -> BackendResult:
        import httpx

        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["x-api-key"] = key  # llm-gateway authenticates on x-api-key
        body = {
            "model": model or self.default_model,
            "messages": messages,
            "params": {"temperature": temperature, "max_tokens": max_tokens},
        }
        url = self.base_url + "/chat/complete"
        last_exc: Exception | None = None
        for _ in range(2):
            t0 = time.monotonic()
            try:
                resp = httpx.post(url, headers=headers, json=body, timeout=timeout_s)
                if resp.status_code // 100 != 2:
                    raise NarratorError("NARRATOR_HTTP", f"llm-gateway returned HTTP {resp.status_code}")
                data = resp.json()
                text = data.get("content") or data.get("reasoning") or ""
                return BackendResult(
                    text=text,
                    model=str(data.get("model") or body["model"] or ""),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    usage=data.get("usage"),
                )
            except NarratorError:
                raise
            except Exception as exc:
                last_exc = exc
        raise NarratorError(
            "NARRATOR_HTTP", f"llm-gateway failed after retry: {type(last_exc).__name__}"
        )


class NarratorBackend:
    """Any configured narrator provider, flattened into system+user messages."""

    def __init__(self, cfg: ProviderConfig, adapter: Any):
        self.cfg = cfg
        self.adapter = adapter
        self.name = cfg.name

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_s: float,
    ) -> BackendResult:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        rest = [m for m in messages if m["role"] != "system"]
        user = "\n\n".join(
            f"[{m['role']}] {m['content']}" if m["role"] != "user" else m["content"]
            for m in rest
        )
        req = NarrationRequest(
            model=model or self.cfg.model,
            system_prompt=system,
            user_prompt=user,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
            json_mode=False,
        )
        resp = self.adapter.narrate(req)
        return BackendResult(
            text=resp.text,
            model=resp.model or self.cfg.model,
            latency_ms=resp.latency_ms,
            usage=resp.usage,
        )


def none_reply(
    *,
    recalled: list[dict],
    conflict_subjects: list[str],
    abstained: bool,
    abstention_reason: str | None,
    reason: str,
) -> str:
    """Deterministic renderer when no model is configured or reachable.

    Reports abstention or lists only the recall bundle's own [id] markers.
    It never infers facts or invents subjects.
    """
    lines = [f"No model response is available ({reason})."]
    if abstained:
        lines.append(
            f"The ledger had nothing confident to say here"
            + (f" — {abstention_reason}." if abstention_reason else ".")
        )
    elif recalled:
        lines.append("What the ledger currently holds:")
        for item in recalled:
            subj = f" subject={item['subject']}" if item.get("subject") else ""
            lines.append(
                f"- [{item['id']}] type={item['type']} status={item['status']}{subj}:"
                f" {item['content'][:200]}"
            )
    else:
        lines.append("The ledger holds no relevant memories for this turn.")
    if conflict_subjects:
        lines.append(
            "Unresolved conflicts are recorded for: "
            + ", ".join(conflict_subjects)
            + ". The recorded claims conflict."
        )
    return "\n".join(lines)


class NoneBackend:
    """No model call — deterministic rendering of the recall facts only."""

    name = "none"

    def __init__(self):
        self._recalled: list[dict] = []
        self._conflict_subjects: list[str] = []
        self._abstained = False
        self._abstention_reason: str | None = None
        self._reason = "no model backend configured"

    def set_context(
        self,
        *,
        recalled: list[dict],
        conflict_subjects: list[str],
        abstained: bool,
        abstention_reason: str | None,
        reason: str,
    ) -> None:
        self._recalled = recalled
        self._conflict_subjects = conflict_subjects
        self._abstained = abstained
        self._abstention_reason = abstention_reason
        self._reason = reason

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_s: float,
    ) -> BackendResult:
        return BackendResult(
            text=none_reply(
                recalled=self._recalled,
                conflict_subjects=self._conflict_subjects,
                abstained=self._abstained,
                abstention_reason=self._abstention_reason,
                reason=self._reason,
            ),
            model="none",
            latency_ms=0,
            usage=None,
        )


def resolve_backend(provider: str | None) -> ChatBackend:
    """request provider → JARVIS_TWIN_CHAT_BACKEND → 'none'.

    'llm-gateway' resolves through the shorthand env vars; any other name
    resolves through the narrator provider registry. Unknown → NARRATOR_UNKNOWN.
    """
    name = provider or os.getenv("JARVIS_TWIN_CHAT_BACKEND", "").strip() or "none"
    if name == "none":
        return NoneBackend()
    if name == "llm-gateway":
        url = os.getenv("JARVIS_TWIN_CHAT_GATEWAY_URL", "").strip()
        if not url:
            raise NarratorError(
                "TWIN_CHAT_NO_GATEWAY_URL",
                "JARVIS_TWIN_CHAT_BACKEND=llm-gateway but JARVIS_TWIN_CHAT_GATEWAY_URL is unset",
            )
        return GatewayBackend(
            base_url=url,
            api_key_env=os.getenv("JARVIS_TWIN_CHAT_GATEWAY_KEY_ENV", "").strip(),
            default_model=os.getenv("JARVIS_TWIN_CHAT_MODEL", "").strip(),
        )
    cfg, adapter = get_adapter(name)
    if adapter is None:
        return NoneBackend()
    return NarratorBackend(cfg, adapter)
