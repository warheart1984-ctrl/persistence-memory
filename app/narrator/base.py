"""Narrator adapter contract — one interface for every kind of model.

The model never decides facts: it receives a deterministic TwinState JSON and
returns text that the narration gate (app/narrator/gate.py) checks sentence by
sentence before anything is shown.  Adapters are plain HTTP; no vendor SDKs.

Secrets: an API key is read from the env var NAMED by ``api_key_env`` at call
time.  Keys are never logged, never put in receipts, never written to the
ledger, and never placed in URLs (headers only).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class NarratorError(Exception):
    """Refusals: NARRATOR_UNKNOWN, NARRATOR_URL_NOT_ALLOWED, NARRATOR_HTTP, ..."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(detail or code)


@dataclass(frozen=True)
class NarrationRequest:
    model: str
    system_prompt: str
    user_prompt: str            # contains the TwinState JSON
    max_tokens: int = 900
    temperature: float = 0.0
    timeout_s: float = 30.0
    json_mode: bool = True


@dataclass(frozen=True)
class NarrationResponse:
    text: str                   # raw model text
    provider: str
    model: str
    latency_ms: int
    usage: dict | None = None


@runtime_checkable
class NarratorAdapter(Protocol):
    """One method: send a request, return raw text or raise NarratorError."""

    name: str

    def narrate(self, req: NarrationRequest) -> NarrationResponse: ...


@dataclass(frozen=True)
class ProviderConfig:
    """A configured narrator provider — name only ever reaches the UI."""

    name: str
    adapter: str                # registry key: openai_compatible|anthropic|google|ollama|llm_gateway|none
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""       # NAME of the env var holding the key — never the key itself
    timeout_s: float = 30.0
    extra: dict = field(default_factory=dict)


def read_api_key(cfg: ProviderConfig) -> str:
    """Resolve the key at call time. Missing env var -> refused, not crash."""
    if not cfg.api_key_env:
        return ""
    key = os.getenv(cfg.api_key_env, "").strip()
    if not key:
        raise NarratorError(
            "NARRATOR_NO_KEY",
            f"provider {cfg.name!r} names env var {cfg.api_key_env!r} but it is unset or empty",
        )
    return key


def _is_localhost(url: str) -> bool:
    u = url.strip().lower()
    return any(u.startswith(p) for p in (
        "http://localhost", "https://localhost",
        "http://127.", "http://[::1]", "http://0.0.0.0",
    ))


def check_url_allowed(url: str) -> None:
    """Fail closed: a base URL must be localhost or on JARVIS_TWIN_ALLOWED_URLS."""
    if not url:
        raise NarratorError("NARRATOR_URL_NOT_ALLOWED", "empty base_url")
    if _is_localhost(url):
        return
    allowed = {
        u.strip().rstrip("/")
        for u in os.getenv("JARVIS_TWIN_ALLOWED_URLS", "").split(",")
        if u.strip()
    }
    if url.rstrip("/") not in allowed:
        raise NarratorError(
            "NARRATOR_URL_NOT_ALLOWED",
            f"{url!r} is not localhost and not in JARVIS_TWIN_ALLOWED_URLS",
        )
