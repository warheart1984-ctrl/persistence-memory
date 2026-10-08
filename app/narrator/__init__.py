"""Narrator registry — configured providers only, never caller-supplied.

Configuration (environment only):
  JARVIS_TWIN_PROVIDERS   JSON array of provider objects:
    [{"name": "local-ollama", "adapter": "ollama",
      "base_url": "http://localhost:11434", "model": "llama3.1",
      "api_key_env": ""}]
  JARVIS_TWIN_NARRATOR    shorthand single-provider name
  JARVIS_TWIN_MODEL       shorthand model
  JARVIS_TWIN_BASE_URL    shorthand base URL (allowlist-checked)
  JARVIS_TWIN_API_KEY_ENV shorthand: NAME of the env var holding the key
  JARVIS_TWIN_ALLOWED_URLS  comma-separated non-localhost base URLs

``none`` is always configured and is the default.  Adding a provider means
one adapter file + one line in _ADAPTERS.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .base import (
    NarratorAdapter,
    NarratorError,
    ProviderConfig,
    check_url_allowed,
)
from .anthropic import AnthropicAdapter
from .google import GoogleAdapter
from .llm_gateway import LlmGatewayAdapter
from .ollama import OllamaAdapter
from .openai_compatible import OpenAICompatibleAdapter
from .runner import narrate_state
from .template import template_narration

_ADAPTERS: dict[str, type] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "anthropic": AnthropicAdapter,
    "google": GoogleAdapter,
    "ollama": OllamaAdapter,
    "llm_gateway": LlmGatewayAdapter,
}

_NONE = ProviderConfig(name="none", adapter="none", model="template")


def configured_providers() -> dict[str, ProviderConfig]:
    """All configured providers.  'none' is always present."""
    providers: dict[str, ProviderConfig] = {"none": _NONE}

    raw = os.getenv("JARVIS_TWIN_PROVIDERS", "").strip()
    if raw:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NarratorError("NARRATOR_CONFIG", f"JARVIS_TWIN_PROVIDERS is not valid JSON: {exc}")
        if not isinstance(items, list):
            raise NarratorError("NARRATOR_CONFIG", "JARVIS_TWIN_PROVIDERS must be a JSON array")
        for item in items:
            cfg = _to_config(item)
            _validate(cfg)
            providers[cfg.name] = cfg

    # Shorthand single-provider env vars (documented, equivalent form).
    short = os.getenv("JARVIS_TWIN_NARRATOR", "").strip()
    if short:
        cfg = ProviderConfig(
            name=short,
            adapter=short,
            model=os.getenv("JARVIS_TWIN_MODEL", ""),
            base_url=os.getenv("JARVIS_TWIN_BASE_URL", ""),
            api_key_env=os.getenv("JARVIS_TWIN_API_KEY_ENV", ""),
            timeout_s=float(os.getenv("JARVIS_TWIN_TIMEOUT_S", "30") or 30),
        )
        _validate(cfg)
        providers[cfg.name] = cfg
    return providers


def _to_config(item: Any) -> ProviderConfig:
    if not isinstance(item, dict):
        raise NarratorError("NARRATOR_CONFIG", "provider entry must be an object")
    return ProviderConfig(
        name=str(item.get("name", "")).strip(),
        adapter=str(item.get("adapter", "")).strip(),
        model=str(item.get("model", "")),
        base_url=str(item.get("base_url", "")),
        api_key_env=str(item.get("api_key_env", "")),
        timeout_s=float(item.get("timeout_s", 30) or 30),
    )


def _validate(cfg: ProviderConfig) -> None:
    if not cfg.name:
        raise NarratorError("NARRATOR_CONFIG", "provider needs a name")
    if cfg.adapter == "none":
        return
    if cfg.adapter not in _ADAPTERS:
        raise NarratorError("NARRATOR_UNKNOWN", f"unknown adapter {cfg.adapter!r}")
    if cfg.adapter in ("openai_compatible", "anthropic", "google", "ollama", "llm_gateway"):
        # ollama defaults to its local port when base_url is empty
        if cfg.adapter == "ollama" and not cfg.base_url:
            return
        check_url_allowed(cfg.base_url)


def get_adapter(provider_name: str) -> tuple[ProviderConfig, NarratorAdapter | None]:
    """Resolve a configured provider name. Unknown -> NARRATOR_UNKNOWN."""
    providers = configured_providers()
    cfg = providers.get(provider_name or "none")
    if cfg is None:
        raise NarratorError("NARRATOR_UNKNOWN", f"provider {provider_name!r} is not configured")
    if cfg.adapter == "none":
        return cfg, None
    _validate(cfg)
    return cfg, _ADAPTERS[cfg.adapter](cfg)


def provider_catalog() -> list[dict[str, str]]:
    """Names + models only — never URLs or keys (safe for the UI picker)."""
    return [
        {"name": c.name, "adapter": c.adapter, "model": c.model}
        for c in configured_providers().values()
    ]


__all__ = [
    "ProviderConfig", "NarratorError", "NarratorAdapter",
    "configured_providers", "get_adapter", "provider_catalog",
    "narrate_state", "template_narration", "check_url_allowed",
]
