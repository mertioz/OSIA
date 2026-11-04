#!/usr/bin/env python3
"""Authoritative provider/model reasoning capabilities and request receipts."""

from __future__ import annotations

import copy
import json
import logging

from osia.config import (
    get_reasoning_settings,
    normalize_reasoning_level,
    normalize_reasoning_settings,
    set_reasoning_settings,
)
from osia.models import provider_for_model


_LOG = logging.getLogger(__name__)


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
        "label_key": (
            f"reasoning.{option_id}" if label_key is None else label_key
        ),
        "kwargs": copy.deepcopy(kwargs or {}),
        "default": bool(default),
    }


_AUTO_OPTION = _option(
    "auto", "Default", default=True, label_key="reasoning.default"
)


def _anthropic_adaptive_kwargs(effort: str) -> dict:
    return {
        "extra_body": {
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": effort},
        }
    }


def _deepseek_kwargs(effort: str) -> dict:
    # ``extra_body`` is the OpenAI SDK escape hatch. Its children are emitted
    # as top-level DeepSeek JSON fields on the wire.
    return {
        "extra_body": {
            "thinking": {"type": "enabled"},
            "reasoning_effort": effort,
        }
    }


def _options(*values: dict) -> list[dict]:
    return [copy.deepcopy(value) for value in values]


def _register(
    target: dict,
    provider: str,
    model_ids: tuple[str, ...],
    options: list[dict],
    evidence: str,
) -> None:
    for model_id in model_ids:
        target[(provider, model_id)] = {
            "options": _options(*options),
            "evidence": evidence,
        }


# This exact-pair registry is the sole static capability authority. The PC UI,
# Mobile catalogue, persistence resolver and provider adapter all consume it.
# ChatGPT Account models are deliberately absent: App Server ``model/list``
# metadata is authoritative for that provider at runtime.
MODEL_REASONING_CAPABILITIES: dict[tuple[str, str], dict] = {}

_register(
    MODEL_REASONING_CAPABILITIES,
    "anthropic",
    ("claude-opus-4-8", "claude-opus-4-7"),
    _options(
        _AUTO_OPTION,
        _option("off", "Off", {"extra_body": {"thinking": {"type": "disabled"}}}),
        _option("low", "Low", _anthropic_adaptive_kwargs("low")),
        _option("medium", "Medium", _anthropic_adaptive_kwargs("medium")),
        _option("high", "High", _anthropic_adaptive_kwargs("high")),
        _option("xhigh", "Extra high", _anthropic_adaptive_kwargs("xhigh")),
        _option("max", "Max", _anthropic_adaptive_kwargs("max")),
    ),
    "https://platform.claude.com/docs/en/build-with-claude/effort",
)
_register(
    MODEL_REASONING_CAPABILITIES,
    "anthropic",
    ("claude-opus-4-6", "claude-sonnet-4-6"),
    _options(
        _AUTO_OPTION,
        _option("off", "Off", {"extra_body": {"thinking": {"type": "disabled"}}}),
        _option("low", "Low", _anthropic_adaptive_kwargs("low")),
        _option("medium", "Medium", _anthropic_adaptive_kwargs("medium")),
        _option("high", "High", _anthropic_adaptive_kwargs("high")),
        _option("max", "Max", _anthropic_adaptive_kwargs("max")),
    ),
    "https://platform.claude.com/docs/en/build-with-claude/effort",
)
_register(
    MODEL_REASONING_CAPABILITIES,
    "openai",
    ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini"),
    _options(
        _AUTO_OPTION,
        _option("off", "Off", {"reasoning_effort": "none"}),
        _option("low", "Low", {"reasoning_effort": "low"}),
        _option("medium", "Medium", {"reasoning_effort": "medium"}),
        _option("high", "High", {"reasoning_effort": "high"}),
        _option("xhigh", "Extra high", {"reasoning_effort": "xhigh"}),
    ),
    "https://developers.openai.com/api/docs/models",
)
_register(
    MODEL_REASONING_CAPABILITIES,
    "deepseek",
    ("deepseek-flash",),
    _options(
        _AUTO_OPTION,
        _option("off", "Non-thinking", {"extra_body": {"thinking": {"type": "disabled"}}}, label_key=""),
        _option("low", "Low", _deepseek_kwargs("low")),
        _option("high", "High", _deepseek_kwargs("high")),
        _option("max", "Max", _deepseek_kwargs("max")),
    ),
    "https://api-docs.deepseek.com/guides/thinking_mode",
)
MODEL_REASONING_CAPABILITIES[("deepseek", "deepseek-flash")].update({
    "provider_default": "high",
    # The model encoder supports numbers; hosted Chat Completions documents
    # named presets only. Do not claim numeric API support or approximate it.
    "native_numeric_effort": {
        "minimum": 1, "maximum": 100,
        "presets": {"low": 50, "high": 75, "max": 100},
        "api_exposed": False,
        "evidence": "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/encoding/README.md",
    },
})
_register(
    MODEL_REASONING_CAPABILITIES,
    "google",
    ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite"),
    _options(
        _AUTO_OPTION,
        _option("minimal", "Minimal", {"reasoning_effort": "minimal"}),
        _option("low", "Low", {"reasoning_effort": "low"}),
        _option("medium", "Medium", {"reasoning_effort": "medium"}),
        _option("high", "High", {"reasoning_effort": "high"}),
    ),
    "https://ai.google.dev/gemini-api/docs/openai",
)
_register(
    MODEL_REASONING_CAPABILITIES,
    "google",
    ("gemini-3.1-pro-preview",),
    _options(
        _AUTO_OPTION,
        _option("low", "Low", {"reasoning_effort": "low"}),
        _option("medium", "Medium", {"reasoning_effort": "medium"}),
        _option("high", "High", {"reasoning_effort": "high"}),
    ),
    "https://ai.google.dev/gemini-api/docs/thinking",
)
_register(
    MODEL_REASONING_CAPABILITIES,
    "google",
    ("gemini-2.5-flash", "gemini-2.5-flash-lite"),
    _options(
        _AUTO_OPTION,
        _option("off", "Off", {"reasoning_effort": "none"}),
        _option("minimal", "Minimal", {"reasoning_effort": "minimal"}),
        _option("low", "Low", {"reasoning_effort": "low"}),
        _option("medium", "Medium", {"reasoning_effort": "medium"}),
        _option("high", "High", {"reasoning_effort": "high"}),
    ),
    "https://ai.google.dev/gemini-api/docs/openai",
)
_register(
    MODEL_REASONING_CAPABILITIES,
    "groq",
    ("openai/gpt-oss-120b", "openai/gpt-oss-20b"),
    _options(
        _AUTO_OPTION,
        _option("low", "Low", {"reasoning_effort": "low"}),
        _option("medium", "Medium", {"reasoning_effort": "medium"}),
        _option("high", "High", {"reasoning_effort": "high"}),
    ),
    "https://console.groq.com/docs/reasoning",
)
_register(
    MODEL_REASONING_CAPABILITIES,
    "groq",
    ("qwen/qwen3.6-27b",),
    _options(
        _AUTO_OPTION,
        _option("off", "Off", {"reasoning_effort": "none"}),
    ),
    "https://console.groq.com/docs/reasoning",
)
_register(
    MODEL_REASONING_CAPABILITIES,
    "mistral",
    ("mistral-medium-3-5", "mistral-small-2603"),
    _options(
        _AUTO_OPTION,
        _option("off", "Off", {"reasoning_effort": "none"}),
        _option("high", "High", {"reasoning_effort": "high"}),
    ),
    "https://docs.mistral.ai/inference/conversations/reasoning",
)


_register(
    MODEL_REASONING_CAPABILITIES, "openai",
    ("gpt-6.1-sol", "gpt-6-astra"),
    _options(_AUTO_OPTION, *[_option(level, level.title(), {"reasoning_effort": level})
                           for level in ("low", "medium", "high", "xhigh", "max")]),
    "https://developers.openai.com/api/docs/models/gpt-6.1-sol",
)
_register(
    MODEL_REASONING_CAPABILITIES, "openai", ("gpt-6-luna", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
    _options(_AUTO_OPTION, _option("off", "Off", {"reasoning_effort": "none"}),
             *[_option(level, level.title(), {"reasoning_effort": level})
               for level in ("low", "medium", "high", "xhigh", "max")]),
    "https://developers.openai.com/api/docs/models/gpt-6-luna",
)
_register(
    MODEL_REASONING_CAPABILITIES, "openai", ("gpt-6.1-terra",),
    _options(_AUTO_OPTION, _option("off", "Off", {"reasoning_effort": "none"}),
             *[_option(level, level.title(), {"reasoning_effort": level})
               for level in ("low", "medium", "high", "xhigh", "max")]),
    "https://developers.openai.com/api/docs/models/gpt-6.1-terra",
)
_register(
    MODEL_REASONING_CAPABILITIES, "google", ("gemini-4-argon", "gemini-3.8-flash"),
    _options(_AUTO_OPTION, *[_option(level, level.title(), {"reasoning_effort": level})
                           for level in ("low", "medium", "high")]),
    "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash",
)
_register(
    MODEL_REASONING_CAPABILITIES, "groq", ("qwen/qwen3.8-27b",),
    _options(_AUTO_OPTION, _option("off", "Off", {"reasoning_effort": "none"}),
             *[_option(level, level.title(), {"reasoning_effort": level})
               for level in ("low", "medium", "high")]),
    "https://console.groq.com/docs/reasoning",
)

_register(
    MODEL_REASONING_CAPABILITIES, "groq", ("meta-models/muse-spark-open",),
    _options(_AUTO_OPTION, *[_option(level, level.title(), {"reasoning_effort": level})
                            for level in ("low", "medium", "high", "xhigh")]),
    "https://console.groq.com/docs/reasoning",
)
_register(
    MODEL_REASONING_CAPABILITIES, "xai", ("grok-4.7", "grok-4.6"),
    _options(_AUTO_OPTION, *[_option(level, level.title(), {"reasoning_effort": level})
                           for level in ("low", "medium", "high", "xhigh")]),
    "https://docs.x.ai/developers/model-capabilities/text/reasoning",
)
_register(
    MODEL_REASONING_CAPABILITIES, "mistral", ("mistral-large-4", "mistral-large-4-0"),
    _options(_AUTO_OPTION, *[_option(level, level.title(), {"reasoning_effort": level})
                            for level in ("low", "medium", "high")]),
    "https://docs.mistral.ai/inference/conversations/reasoning",
)
_register(
    MODEL_REASONING_CAPABILITIES, "mistral", ("zai-glm-5-3", "zai-glm-5-3-flash"),
    _options(_AUTO_OPTION, *[_option(level, level.title(), {"reasoning_effort": level})
                            for level in ("low", "high", "max")]),
    "https://docs.mistral.ai/inference/conversations/reasoning",
)

def normalize_level(value) -> str:
    """Return a normalized canonical reasoning option id."""
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


def _account_capability(model_id: str) -> dict | None:
    try:
        from osia.chatgpt_account.provider import get_cached_account_models

        entry = next(
            (
                item
                for item in get_cached_account_models()
                if str(item.get("id") or "") == model_id
            ),
            None,
        )
    except Exception:
        entry = None
    if not entry:
        return None

    options = [_AUTO_OPTION]
    for effort in entry.get("supportedReasoningEfforts") or []:
        provider_effort = str(effort.get("id") or "").strip().lower()
        effort_id = normalize_level(provider_effort)
        if effort_id == "auto":
            continue
        label = "Extra high" if effort_id == "xhigh" else effort_id.title()
        options.append(
            _option(
                effort_id,
                label,
                {"effort": provider_effort},
                # Ultra has the same spelling in supported UI languages; an
                # empty key deliberately uses the sanitized runtime label.
                label_key="" if effort_id == "ultra" else None,
            )
        )
    if len(options) == 1:
        return None
    return {
        "options": _options(*options),
        "evidence": "chatgpt_app_server:model/list",
        "provider_default": normalize_level(entry.get("defaultReasoningEffort")),
    }


def get_reasoning_capability(provider, model) -> dict | None:
    """Return one exact-pair capability, copied for safe caller mutation."""
    provider_id = _provider_id(provider, model)
    model_id = _model_id(model)
    if not provider_id or not model_id:
        return None
    if provider_id == "chatgpt_account":
        capability = _account_capability(model_id)
    else:
        capability = MODEL_REASONING_CAPABILITIES.get((provider_id, model_id))
        if capability is None and provider_id == "openai":
            from osia.models import model_metadata
            metadata = model_metadata(provider_id, model_id) or {}
            capability = MODEL_REASONING_CAPABILITIES.get(
                (provider_id, metadata.get("reasoning_model_id")))
        if provider_id == "xai":
            from osia.models import model_metadata
            metadata = model_metadata(provider_id, model_id) or {}
            efforts = metadata.get("supportedReasoningEfforts")
            if isinstance(efforts, list) and efforts:
                options = [_AUTO_OPTION, *[_option("off" if level == "none" else level,
                    "Off" if level == "none" else level.title(), {"reasoning_effort": level}) for level in efforts]]
                capability = {"options": options, "evidence": "xai:language-models.capabilities"}
        if provider_id == "openrouter":
            from osia.models import model_metadata
            metadata = model_metadata(provider_id, model_id) or {}
            efforts = metadata.get("supportedReasoningEfforts") or []
            if efforts:
                options = [_AUTO_OPTION, *[_option(
                    "off" if effort == "none" else effort,
                    "Off" if effort == "none" else effort.title(),
                    {"reasoning_effort": effort},
                ) for effort in efforts]]
                capability = {
                    "options": options, "evidence": "openrouter:models.reasoning",
                    "provider_default": normalize_level(metadata.get("defaultReasoningEffort")),
                }
        if provider_id == "anthropic":
            from osia.models import model_metadata
            metadata = model_metadata(provider_id, model_id) or {}
            if metadata.get("adaptive_thinking"):
                options = [_AUTO_OPTION]
                if metadata.get("thinking_disabled"):
                    options.append(_option("off", "Off", {"extra_body": {"thinking": {"type": "disabled"}}}))
                options.extend(_option(level, level.title(), _anthropic_adaptive_kwargs(level))
                               for level in metadata.get("supportedReasoningEfforts", []))
                capability = {"options": options, "evidence": "anthropic:models.capabilities"}

    return copy.deepcopy(capability) if capability else None


def get_reasoning_options(provider, model) -> list[dict]:
    """Return only options with an exact provider/model mapping."""
    capability = get_reasoning_capability(provider, model)
    return list(capability.get("options") or []) if capability else []


def model_reasoning_settings_key(provider_id: str, model_id: str) -> str:
    return (
        f"{provider_id}:{model_id}"
        if provider_id in {"chatgpt_account", "openrouter"} and model_id
        else model_id
    )


def _requested_level(provider_id: str, model_id: str, cfg: dict) -> tuple[str, str]:
    model_key = model_reasoning_settings_key(provider_id, model_id)
    model_levels = cfg.get("models") or {}
    if model_key and model_key in model_levels:
        return normalize_level(model_levels.get(model_key)), "model"

    provider_levels = cfg.get("providers") or {}
    if provider_id and provider_id in provider_levels:
        return normalize_level(provider_levels.get(provider_id)), "provider"

    return normalize_level(cfg.get("default")), "default"


def _migrate_invalid_to_default(
    cfg: dict,
    provider_id: str,
    model_id: str,
) -> None:
    """Pin one invalid effective selection to canonical ``auto``."""
    model_key = model_reasoning_settings_key(provider_id, model_id)
    if not model_key:
        return
    migrated = copy.deepcopy(cfg)
    models = dict(migrated.get("models") or {})
    if models.get(model_key) == "auto":
        return
    models[model_key] = "auto"
    migrated["models"] = models
    set_reasoning_settings(migrated)


def _resolve_selection(provider, model, settings=None) -> dict:
    provider_id = _provider_id(provider, model)
    model_id = _model_id(model)
    cfg = _reasoning_settings(settings)
    requested, source = _requested_level(provider_id, model_id, cfg)
    capability = get_reasoning_capability(provider_id, model_id)
    options = list(capability.get("options") or []) if capability else []
    option_ids = _known_option_ids(options)
    if provider_id == "chatgpt_account" and requested != "auto" and requested not in option_ids:
        # The Account catalogue is process-local. A saved explicit choice must
        # be checked against model/list before this turn can pin its reasoning.
        try:
            from osia.chatgpt_account.provider import get_account_provider

            get_account_provider().list_models(force=True)
        except Exception as exc:
            raise ValueError(
                "ERROR: ChatGPT Account reasoning support could not be verified."
            ) from exc
        capability = get_reasoning_capability(provider_id, model_id)
        options = list(capability.get("options") or []) if capability else []
        option_ids = _known_option_ids(options)
        if requested not in option_ids:
            raise ValueError(
                "ERROR: the selected ChatGPT Account model does not support "
                f"reasoning level {requested!r}."
            )
    accepted = requested == "auto" or requested in option_ids
    effective = requested if accepted else "auto"
    if not accepted and settings is None and provider_id != "deepseek":
        try:
            _migrate_invalid_to_default(cfg, provider_id, model_id)
        except Exception:
            pass
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "requested": requested,
        "effective": effective,
        "selection_source": source,
        "mapping_state": "accepted" if accepted else "rejected",
        "options": options,
        "evidence": str((capability or {}).get("evidence") or ""),
        "provider_default": str((capability or {}).get("provider_default") or ""),
    }


def resolve_reasoning_option_id(provider, model, settings=None) -> str:
    """Resolve one exact option; persist invalid saved values as ``auto``."""
    return str(_resolve_selection(provider, model, settings).get("effective") or "auto")


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
    """Return True only for an authoritative exact-pair capability."""
    return bool(get_reasoning_options(provider, model))


def _provider_parameters(kwargs: dict) -> list[dict]:
    parameters: list[dict] = []

    def visit(value, prefix: str = "") -> None:
        if not isinstance(value, dict):
            if prefix and isinstance(value, (str, bool, int, float)):
                parameters.append({"name": prefix, "value": value})
            return
        for key, child in value.items():
            key_text = str(key)
            # SDK-only wrapper; its children are the provider JSON fields.
            child_prefix = prefix if not prefix and key_text == "extra_body" else (
                f"{prefix}.{key_text}" if prefix else key_text
            )
            visit(child, child_prefix)

    visit(kwargs)
    return parameters


def _primary_parameter(parameters: list[dict]) -> dict:
    for wanted in (
        "reasoning_effort",
        "output_config.effort",
        "effort",
        "thinking.type",
    ):
        match = next((item for item in parameters if item.get("name") == wanted), None)
        if match:
            return dict(match)
    return dict(parameters[0]) if parameters else {"name": None, "value": None}


def build_reasoning_request(provider, model, settings=None) -> dict:
    """Build provider kwargs plus a sanitized mapping receipt.

    The receipt contains enums and exact pair identity only. It never includes
    prompts, responses, keys, hidden reasoning, or chain-of-thought content.
    """
    resolved = _resolve_selection(provider, model, settings)
    if resolved["provider_id"] == "deepseek" and resolved["mapping_state"] == "rejected":
        raise ValueError("ERROR: unsupported DeepSeek reasoning mode; choose Default, Non-thinking, Low, High or Max.")
    kwargs = (
        _option_kwargs(resolved["options"], resolved["effective"])
        if resolved["effective"] != "auto"
        else {}
    )
    parameters = _provider_parameters(kwargs)
    primary = _primary_parameter(parameters)
    diagnostic = {
        "provider_id": resolved["provider_id"],
        "model_id": resolved["model_id"],
        "requested_reasoning": resolved["requested"],
        "effective_reasoning": resolved["effective"],
        "selection_source": resolved["selection_source"],
        "mapping_state": resolved["mapping_state"],
        "provider_parameter_name": primary.get("name"),
        "provider_parameter_value": primary.get("value"),
        "provider_parameters": parameters,
        "provider_default": resolved["provider_default"],
        "evidence": resolved["evidence"],
    }
    return {"kwargs": copy.deepcopy(kwargs), "diagnostic": diagnostic}


def build_reasoning_kwargs(provider, model, settings=None) -> dict:
    """Compatibility helper returning only the exact provider request kwargs."""
    return build_reasoning_request(provider, model, settings)["kwargs"]


def record_reasoning_outcome(
    diagnostic: dict,
    *,
    accepted: bool,
    error: BaseException | None = None,
) -> dict:
    """Record a sanitized provider acceptance/rejection receipt."""
    receipt = {
        key: copy.deepcopy(diagnostic.get(key))
        for key in (
            "provider_id",
            "model_id",
            "requested_reasoning",
            "effective_reasoning",
            "selection_source",
            "mapping_state",
            "provider_parameter_name",
            "provider_parameter_value",
            "provider_parameters",
            "provider_default",
            "evidence",
        )
    }
    receipt["state"] = "accepted" if accepted else "rejected"
    if error is not None:
        receipt["error_type"] = type(error).__name__
    _LOG.info("reasoning_provider_payload %s", json.dumps(receipt, sort_keys=True))
    return receipt
