#!/usr/bin/env python3
"""Vision model routing (WOR-25).

Some chat models can't see images — DeepSeek (text-only), Groq-hosted Llama /
Qwen / Kimi, etc. When the user selects such a model as their main assistant,
the settings UI lets them pair it with a vision-capable companion (e.g.
DeepSeek text + Gemini vision; see osia.gui.main_popup and DEFAULT_SETTINGS'
``vision_provider`` / ``vision_model``).

This module resolves which model an image/screenshot task should use and builds
an OpenAI-compatible client for it. It deliberately does NOT import or mutate
``osia.agent`` — that module's client wiring is frozen per CLAUDE.md. Instead it
re-derives the same provider base-url + auth that ``OSIAAgent.set_model`` uses,
so vision requests honour the user's endpoint overrides identically.

Screenshot / image-understanding tools call :func:`get_vision_client` to obtain
``(provider, model, client)``:
  * main model is vision-capable  -> the main model itself (no separate hop)
  * main model is text-only       -> the configured ``vision_provider`` +
                                      ``vision_model`` companion
"""

from openai import OpenAI

from osia.config import load_settings, get_api_key
from osia.models import (
    PROVIDERS,
    model_has_vision,
    provider_for_model,
    endpoint_for_provider,
)


def resolve_vision_model(settings: dict | None = None) -> tuple[str | None, str | None]:
    """Return ``(provider, model_id)`` to use for vision tasks.

    Prefers the main selected model when it is vision-capable; otherwise the
    explicitly configured companion (``vision_provider`` / ``vision_model``).
    Falls back to the main model unchanged when no companion is configured, and
    returns ``(None, None)`` only when there is no model to route to at all.
    """
    if settings is None:
        settings = load_settings()

    main_model = (settings.get("selected_model") or "").strip()
    if main_model:
        main_provider = provider_for_model(main_model)
        if model_has_vision(main_provider, main_model):
            return main_provider, main_model

    companion = (settings.get("vision_model") or "").strip()
    if companion:
        provider = (settings.get("vision_provider") or "").strip()
        return (provider or provider_for_model(companion)), companion

    # No companion configured: hand back the main model unchanged. The caller
    # decides what to do when it can't actually process the image.
    if main_model:
        return provider_for_model(main_model), main_model
    return None, None


def _build_client(provider: str, api_key: str) -> OpenAI:
    """Construct an OpenAI-compatible client for ``provider``.

    Mirrors ``OSIAAgent.set_model``'s wiring: Anthropic authenticates with the
    ``x-api-key`` header, every other provider uses Bearer auth derived from
    ``api_key`` by the OpenAI client. The base-url honours the user's
    ``api_fallbacks`` primary override via :func:`endpoint_for_provider`.
    """
    cfg = PROVIDERS.get(provider, PROVIDERS["ollama"])
    base_url = endpoint_for_provider(provider) or cfg["base_url"]
    # Match agent.py exactly: default the key to the "ollama" placeholder FIRST,
    # then derive the Anthropic x-api-key header from that same value (so the
    # header and the client arg always carry identical credentials).
    key = api_key or "ollama"
    default_headers = {"x-api-key": key} if provider == "anthropic" else None
    return OpenAI(
        base_url=base_url,
        api_key=key,
        default_headers=default_headers,
    )


def get_vision_client(settings: dict | None = None):
    """Return ``(provider, model, client)`` for image/screenshot tasks.

    ``client`` is an OpenAI-compatible client already pointed at the resolved
    provider's base-url with the right auth. Returns ``(None, None, None)`` when
    no model can be resolved (no main model and no companion configured).
    """
    if settings is None:
        settings = load_settings()

    provider, model = resolve_vision_model(settings)
    if not model or not provider:
        return None, None, None

    api_key = "ollama" if provider == "ollama" else get_api_key(provider)
    client = _build_client(provider, api_key)
    return provider, model, client
