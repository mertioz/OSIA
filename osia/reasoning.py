#!/usr/bin/env python3
"""Provider-specific reasoning/thinking request parameters for OSIA chat calls."""

from __future__ import annotations

import copy

from osia.config import (
    get_reasoning_settings,
    normalize_reasoning_level,
    normalize_reasoning_settings,
)
from osia.models import HARDCODED_MODELS, provider_for_model


def _hardcoded_model_ids(provider: str) -> frozenset[str]:
    return frozenset(
        str(m.get("id") or "").strip()
        for m in HARDCODED_MODELS
        if str(m.get("provider") or "").strip().lower() == provider
        and str(m.get("id") or "").strip()
    )


_GOOGLE_REASONING_MODELS = _hardcoded_model_ids("google")
_GOOGLE_OFF_MODELS = frozenset(
    model_id
    for model_id in _GOOGLE_REASONING_MODELS
    if model_id in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}
)
_DEEPSEEK_REASONING_MODELS = _hardcoded_model_ids("deepseek")
_GROQ_GPT_OSS_REASONING_MODELS = frozenset(
    model_id
    for model_id in _hardcoded_model_ids("groq")
    if model_id.startswith("openai/gpt-oss-")
)
_MISTRAL_REASONING_MODELS = frozenset(
    model_id
    for model_id in _hardcoded_model_ids("mistral")
    if model_id == "mistral-medium-3-5"
)
_OPENAI_REASONING_MODELS = frozenset(
    model_id
    for model_id in _hardcoded_model_ids("openai")
    if model_id in {"gpt-5.5", "gpt-5.4", "gpt-5.4-mini"}
)
_ANTHROPIC_ADAPTIVE_XHIGH_MODELS = frozenset(
    model_id
    for model_id in _hardcoded_model_ids("anthropic")
    if model_id in {"claude-opus-4-8", "claude-opus-4-7"}
)
_ANTHROPIC_ADAPTIVE_REASONING_MODELS = _ANTHROPIC_ADAPTIVE_XHIGH_MODELS | frozenset(
    model_id
    for model_id in _hardcoded_model_ids("anthropic")
    if model_id in {"claude-opus-4-6", "claude-sonnet-4-6"}
)


def _option(
    option_id: str,
    label: str,
    kwargs: dict | None = None,
    *,
    default: bool = False,
    label_key: str | None = None,
) -> dict:
    return {
        "id": option_id,
        "label": label,
        "label_key": label_key or f"reasoning.{option_id}",
        "kwargs": copy.deepcopy(kwargs or {}),
        "default": bool(default),
    }


_AUTO_OPTION = _option("auto", "Default", default=True, label_key="reasoning.default")


def _anthropic_adaptive_kwargs(effort: str) -> dict:
    return {
        "extra_body": {
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": effort},
        }
    }


def normalize_level(value) -> str:
    """Return a supported normalized reasoning option id."""
    return normalize_reasoning_level(value)


def _model_id(model) -> str:
    return str(model or "").strip()


def _provider_id(provider, model) -> str:
    provider_id = str(provider or "").strip().lower()
    if provider_id:
        return provider_id
    model_id = _model_id(model)
    if not model_id:
        return ""
    try:
        return str(provider_for_model(model_id) or "").strip().lower()
    except Exception:
        return ""


def _reasoning_settings(settings=None) -> dict:
    if settings is None:
        try:
            return get_reasoning_settings()
        except Exception:
            return normalize_reasoning_settings(None)
    if isinstance(settings, dict) and "reasoning" in settings:
        return normalize_reasoning_settings(settings.get("reasoning"))
    return normalize_reasoning_settings(settings)


def _known_option_ids(options: list[dict]) -> set[str]:
    return {
        str(opt.get("id") or "").strip().lower()
        for opt in options
        if str(opt.get("id") or "").strip()
    }


def _default_option(options: list[dict]) -> dict | None:
    for opt in options:
        if opt.get("default"):
            return opt
    return options[0] if options else None


def _find_option(options: list[dict], option_id: str) -> dict | None:
    wanted = normalize_level(option_id)
    for opt in options:
        if str(opt.get("id") or "").strip().lower() == wanted:
            return opt
    return None


def _option_kwargs(options: list[dict], option_id: str) -> dict:
    opt = _find_option(options, option_id) or _default_option(options)
    if not opt:
        return {}
    return copy.deepcopy(opt.get("kwargs") or {})


def get_reasoning_options(provider, model) -> list[dict]:
    """Return model-specific reasoning options safe for the current call path."""
    provider_id = _provider_id(provider, model)
    model_id = _model_id(model)
    if not provider_id or not model_id:
        return []

    if provider_id == "chatgpt_account":
        # App Server model/list is the source of truth. Keep account reasoning
        # separate from the OpenAI API model tables and request parameters.
        try:
            from osia.chatgpt_account.provider import get_cached_account_models
            entry = next(
                (item for item in get_cached_account_models()
                 if str(item.get("id") or "") == model_id),
                None,
            )
        except Exception:
            entry = None
        if not entry:
            return []
        options = [_AUTO_OPTION]
        for effort in entry.get("supportedReasoningEfforts") or []:
            effort_id = str(effort.get("id") or "").strip().lower()
            if effort_id and effort_id != "auto":
                label = "Extra high" if effort_id == "xhigh" else effort_id.title()
                options.append(_option(effort_id, label, {"effort": effort_id}))
        return [copy.deepcopy(opt) for opt in options]

    if provider_id == "openai" and model_id in _OPENAI_REASONING_MODELS:
        options = [
            _AUTO_OPTION,
            _option("off", "Off", {"reasoning_effort": "none"}),
            _option("minimal", "Minimal", {"reasoning_effort": "minimal"}),
            _option("low", "Low", {"reasoning_effort": "low"}),
            _option("medium", "Medium", {"reasoning_effort": "medium"}),
            _option("high", "High", {"reasoning_effort": "high"}),
            _option("xhigh", "Extra high", {"reasoning_effort": "xhigh"}),
        ]
        return [copy.deepcopy(opt) for opt in options]

    if (
        provider_id == "anthropic"
        and model_id in _ANTHROPIC_ADAPTIVE_REASONING_MODELS
    ):
        options = [
            _AUTO_OPTION,
            _option(
                "off",
                "Off",
                {"extra_body": {"thinking": {"type": "disabled"}}},
            ),
            _option("low", "Low", _anthropic_adaptive_kwargs("low")),
            _option("medium", "Medium", _anthropic_adaptive_kwargs("medium")),
            _option("high", "High", _anthropic_adaptive_kwargs("high")),
        ]
        if model_id in _ANTHROPIC_ADAPTIVE_XHIGH_MODELS:
            options.append(
                _option("xhigh", "Extra high", _anthropic_adaptive_kwargs("xhigh"))
            )
        options.append(_option("max", "Max", _anthropic_adaptive_kwargs("max")))
        return [copy.deepcopy(opt) for opt in options]

    if provider_id == "google" and model_id in _GOOGLE_REASONING_MODELS:
        options = [_AUTO_OPTION]
        if model_id in _GOOGLE_OFF_MODELS:
            options.append(_option("off", "Off", {"reasoning_effort": "none"}))
        options.extend(
            [
                _option("minimal", "Minimal", {"reasoning_effort": "minimal"}),
                _option("low", "Low", {"reasoning_effort": "low"}),
                _option("medium", "Medium", {"reasoning_effort": "medium"}),
                _option("high", "High", {"reasoning_effort": "high"}),
            ]
        )
        return [copy.deepcopy(opt) for opt in options]

    if provider_id == "deepseek" and model_id in _DEEPSEEK_REASONING_MODELS:
        options = [
            _AUTO_OPTION,
            _option(
                "off",
                "Off",
                {"extra_body": {"thinking": {"type": "disabled"}}},
            ),
            _option(
                "normal",
                "Normal",
                {
                    "extra_body": {
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": "high",
                    }
                },
            ),
            _option(
                "max",
                "Max",
                {
                    "extra_body": {
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": "max",
                    }
                },
            ),
        ]
        return [copy.deepcopy(opt) for opt in options]

    if provider_id == "groq" and model_id in _GROQ_GPT_OSS_REASONING_MODELS:
        options = [
            _AUTO_OPTION,
            _option("low", "Low", {"reasoning_effort": "low"}),
            _option("medium", "Medium", {"reasoning_effort": "medium"}),
            _option("high", "High", {"reasoning_effort": "high"}),
        ]
        return [copy.deepcopy(opt) for opt in options]

    if provider_id == "mistral" and model_id in _MISTRAL_REASONING_MODELS:
        options = [
            _AUTO_OPTION,
            _option("off", "Off", {"reasoning_effort": "none"}),
            _option("minimal", "Minimal", {"reasoning_effort": "minimal"}),
            _option("low", "Low", {"reasoning_effort": "low"}),
            _option("medium", "Medium", {"reasoning_effort": "medium"}),
            _option("high", "High", {"reasoning_effort": "high"}),
            _option("xhigh", "Extra high", {"reasoning_effort": "xhigh"}),
        ]
        return [copy.deepcopy(opt) for opt in options]

    return []


def resolve_reasoning_option_id(provider, model, settings=None) -> str:
    """Resolve model override > provider default > global default."""
    cfg = _reasoning_settings(settings)
    provider_id = _provider_id(provider, model)
    model_id = _model_id(model)
    options = get_reasoning_options(provider_id, model_id)
    option_ids = _known_option_ids(options)

    model_levels = cfg.get("models") or {}
    # Account and API catalogs can expose the same id. Namespace only account
    # overrides so they can never collide with OpenAI API reasoning settings.
    model_key = (
        f"chatgpt_account:{model_id}"
        if provider_id == "chatgpt_account" and model_id
        else model_id
    )
    if model_key and model_key in model_levels:
        selected = normalize_level(model_levels.get(model_key))
        return selected if selected in option_ids else "auto"

    provider_levels = cfg.get("providers") or {}
    if provider_id and provider_id in provider_levels:
        selected = normalize_level(provider_levels.get(provider_id))
        return selected if selected in option_ids else "auto"

    selected = normalize_level(cfg.get("default"))
    return selected if selected in option_ids else "auto"


def resolve_reasoning_level(provider, model, settings=None) -> str:
    """Backward-compatible alias for the resolved reasoning option id."""
    return resolve_reasoning_option_id(provider, model, settings)


def get_selected_reasoning_label(provider, model, settings=None) -> str | None:
    """Return the selected non-default display label, or None for auto/unsupported."""
    options = get_reasoning_options(provider, model)
    if not options:
        return None
    selected = resolve_reasoning_option_id(provider, model, settings)
    opt = _find_option(options, selected) or _default_option(options)
    if not opt or opt.get("default"):
        return None
    return str(opt.get("label") or selected)


def provider_supports_reasoning(provider, model) -> bool:
    """Return True only for verified current provider/model call paths."""
    return bool(get_reasoning_options(provider, model))


def build_reasoning_kwargs(provider, model, settings=None) -> dict:
    """Build provider request kwargs for the current chat-completions path.

    Unsupported or unverified providers/models return an empty dict. "auto"
    intentionally returns an empty dict so providers keep their native defaults.
    """
    provider_id = _provider_id(provider, model)
    model_id = _model_id(model)
    options = get_reasoning_options(provider_id, model_id)
    if not options:
        return {}

    selected = resolve_reasoning_option_id(provider_id, model_id, settings)
    return _option_kwargs(options, selected)
