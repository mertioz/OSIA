"""Read-only Chat status: factual account quota and labelled API estimates.

No billing history is created and no conversation is changed. Token/cost figures
are estimates of persisted visible turns, not a provider invoice: unpersisted
system/tool rounds, hidden reasoning, cache discounts and extra fees are unknown.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime, timezone
import math
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


_VERIFIED_ON = "2026-11-17"
_LOCAL = frozenset({"ollama", "lmstudio"})


def _price(input_rate, output_rate, source_url, cached=None, **extra):
    result = {"input_per_million": input_rate, "output_per_million": output_rate,
              "source_url": source_url, "verified_on": _VERIFIED_ON,
              "assumption": "standard_paid_uncached", **extra}
    if cached is not None:
        result["cached_input_per_million"] = cached
    return result


_OPENAI = "https://developers.openai.com/api/docs/models/"
_CLAUDE = "https://platform.claude.com/docs/en/about-claude/pricing"
_GOOGLE = "https://ai.google.dev/gemini-api/docs/pricing"
_MISTRAL = "https://docs.mistral.ai/inference/pricing"
# Exact public model identities only. Unknown/dynamic IDs are not fuzzy-matched
# to a similarly named model. Prices are USD/1M text tokens, verified above.
_PRICES = {
    ("openai", "gpt-6-astra"): _price(10, 50, _OPENAI+"gpt-6-astra", 1, long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-6.1-sol"): _price(2, 10, _OPENAI+"gpt-6.1-sol", .1, long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-6.1-terra"): _price(1, 5, _OPENAI+"gpt-6.1-terra", .05, long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-6-luna"): _price(.1, .5, _OPENAI+"gpt-6-luna", .01, long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-6-sol"): _price(2, 10, _OPENAI+"gpt-6-sol", .2, long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-5.6-sol"): _price(4, 20, _OPENAI+"gpt-5.6-sol", .4, valid_until="2026-11-30", long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-5.6-terra"): _price(2, 12, _OPENAI+"gpt-5.6-terra", .2, long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-5.6-luna"): _price(.2, 1.2, _OPENAI+"gpt-5.6-luna", .02, long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-5.5"): _price(5, 30, _OPENAI+"gpt-5.5", .5, long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-5.4"): _price(2.5, 15, _OPENAI+"gpt-5.4", .25, long_context_threshold=272000, long_input_multiplier=2, long_output_multiplier=1.5),
    ("openai", "gpt-5.4-mini"): _price(.75, 4.5, _OPENAI+"gpt-5.4-mini", .075),
    ("openai", "gpt-4.1"): _price(2, 8, _OPENAI+"gpt-4.1", .5),
    ("openai", "gpt-4.1-mini"): _price(.4, 1.6, _OPENAI+"gpt-4.1-mini", .1),
    ("openai", "gpt-4.1-nano"): _price(.1, .4, _OPENAI+"gpt-4.1-nano", .025),
    ("anthropic", "claude-fable-5-2"): _price(8, 40, _CLAUDE, .2),
    ("anthropic", "claude-opus-5-5"): _price(4, 20, _CLAUDE, .2),
    ("anthropic", "claude-sonnet-5-5"): _price(2, 10, _CLAUDE, .1),
    ("anthropic", "claude-haiku-5-5"): _price(.1, .5, _CLAUDE, .01, long_context_threshold=100000, long_input_multiplier=5, long_output_multiplier=5),
    ("anthropic", "claude-opus-5"): _price(5, 25, _CLAUDE, .5),
    ("anthropic", "claude-opus-4-8"): _price(5, 25, _CLAUDE, .5),
    ("anthropic", "claude-opus-4-7"): _price(5, 25, _CLAUDE, .5),
    ("anthropic", "claude-opus-4-6"): _price(5, 25, _CLAUDE, .5),
    ("anthropic", "claude-sonnet-5"): _price(2, 10, _CLAUDE, .2),
    ("anthropic", "claude-sonnet-4-6"): _price(3, 15, _CLAUDE, .3),
    ("anthropic", "claude-haiku-4-5-20251001"): _price(1, 5, _CLAUDE, .1),
    ("deepseek", "deepseek-flash"): _price(.3, 1.2, "https://api-docs.deepseek.com/quick_start/pricing/", .006, assumption="peak_paid_uncached"),
    ("google", "gemini-3.8-flash"): _price(.75, 3.75, _GOOGLE, .075, valid_until="2026-12-31", next_input_per_million=1.5, next_output_per_million=7.5, next_cached_input_per_million=.15),
    ("google", "gemini-3.5-flash"): _price(1.5, 9, _GOOGLE, .15),
    ("google", "gemini-3.1-flash-lite"): _price(.25, 1.5, _GOOGLE, .025),
    ("google", "gemini-2.5-flash-lite"): _price(.1, .4, _GOOGLE, .01),
    ("xai", "grok-4.7"): _price(2, 6, "https://docs.x.ai/developers/models"),
    ("groq", "openai/gpt-oss-120b"): _price(.15, .6, "https://console.groq.com/docs/models", .075),
    ("groq", "openai/gpt-oss-20b"): _price(.075, .3, "https://console.groq.com/docs/models"),
    ("mistral", "mistral-large-2512"): _price(.5, 1.5, _MISTRAL, .05),
    ("mistral", "mistral-medium-3-5"): _price(1.5, 7.5, _MISTRAL, .15),
    ("mistral", "mistral-small-2603"): _price(.15, .6, _MISTRAL, .015),
    ("mistral", "codestral-2508"): _price(.3, .9, _MISTRAL, .03),
}


def _text(value: Any, maximum=192) -> str:
    return "".join(c for c in value if ord(c) >= 32)[:maximum].strip() if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _price_source(value: Any) -> str:
    """A price reference never forwards embedded auth, query strings or fragments."""
    try:
        parsed = urlsplit(_text(value, 512))
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _date_label(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else ""


def model_pricing(provider_id: str, model_id: str, *, metadata: Mapping | None = None,
                  today: date | None = None) -> dict | None:
    """Return exact dated list rates or supplied upstream model pricing.

    ``metadata`` may be an existing OpenRouter Models API record (per-token
    prompt/completion strings), or an explicit USD-per-million rate record.
    No network calls, API keys or unrelated model-name heuristics are used.
    """
    provider_id, model_id = _text(provider_id, 64), _text(model_id)
    if metadata is None and provider_id == "openrouter":
        from osia.models import model_metadata
        cached = model_metadata(provider_id, model_id)
        if isinstance(cached, Mapping) and isinstance(cached.get("pricing"), Mapping):
            metadata = cached
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            return None
        if metadata.get("id") and metadata.get("id") != model_id:
            return None
        raw = metadata.get("pricing", metadata)
        if not isinstance(raw, Mapping):
            return None
        if provider_id == "openrouter" and "prompt" in raw:
            input_rate, output_rate = _number(raw.get("prompt")), _number(raw.get("completion"))
            if input_rate is None or output_rate is None:
                return None
            input_rate, output_rate = _number(input_rate*1e6), _number(output_rate*1e6)
            if input_rate is None or output_rate is None:
                return None
            result = _price(input_rate, output_rate,
                            "https://openrouter.ai/api/v1/models", assumption="upstream_list_uncached",
                            verified_on=_date_label(metadata.get("pricing_verified_on")))
            cached_input = _number(raw.get("input_cache_read"))
            if cached_input is not None and _number(cached_input*1e6) is not None:
                result["cached_input_per_million"] = cached_input*1e6
            return result
        input_rate = _number(raw.get("input_per_million"))
        output_rate = _number(raw.get("output_per_million"))
        if input_rate is None or output_rate is None:
            return None
        return {"input_per_million": input_rate, "output_per_million": output_rate,
                "source_url": _price_source(raw.get("source_url")),
                "verified_on": _date_label(raw.get("verified_on")),
                "assumption": "supplied_list_uncached"}
    result = deepcopy(_PRICES.get((provider_id, model_id)))
    if result is None:
        return None
    today = today or datetime.now(timezone.utc).date()
    until = result.pop("valid_until", "")
    if until and today.isoformat() > until:
        if "next_input_per_million" not in result:
            return None
        for field in ("input_per_million", "output_per_million", "cached_input_per_million"):
            result[field] = result.pop("next_"+field)
    for field in tuple(result):
        if field.startswith("next_"):
            result.pop(field)
    return result


def _conversation(conversation_id: str, supplied: dict | None) -> dict:
    if supplied is not None:
        if not isinstance(supplied, dict) or str(supplied.get("id") or "") != conversation_id:
            raise ValueError("status_conversation_mismatch")
        return supplied
    if conversation_id:
        from osia.conversations import get_conversation
        try:
            result = get_conversation(conversation_id)
            if isinstance(result, dict) and result.get("id") == conversation_id:
                return result
        except (OSError, ValueError, TypeError, RecursionError):
            pass
    return {"id": conversation_id, "messages": []}


def _context(conversation_id: str, provider_id: str, model_id: str,
             conversation: dict, supplied: dict | None) -> dict | None:
    from osia.conversation_context import (
        _context_usage_snapshot, conversation_context_usage,
        estimate_messages_tokens, resolve_context_limit,
    )
    def scoped(raw):
        snap = _context_usage_snapshot(raw)
        if not snap or (snap["provider"] and snap["provider"] != provider_id) or (snap["model"] and snap["model"] != model_id):
            return None
        return {key: snap[key] for key in ("used", "maximum", "estimated")}
    usage = scoped(supplied)
    if usage is None:
        usage = scoped(conversation_context_usage(conversation_id, conversation=conversation))
    if usage is None and provider_id == "chatgpt_account":
        try:
            from osia.chatgpt_account.provider import get_account_provider
            usage = scoped(get_account_provider().conversation_context_usage(conversation_id, model=model_id))
        except Exception:
            pass
    if usage is not None:
        return usage
    try:
        maximum = resolve_context_limit(provider_id, model_id)
        messages = [m for m in conversation.get("messages", [])
                    if isinstance(m, dict) and m.get("role") in {"user", "assistant", "tool"} and not m.get("_internal")]
        used = estimate_messages_tokens(messages) if messages else 0
        if type(maximum) is int and maximum > 0:
            return {"used": used, "maximum": maximum, "estimated": True}
    except Exception:
        pass
    return None


def _quota(payload: Any = None) -> dict:
    from osia.chatgpt_account.rate_limits import quota_windows
    if payload is None:
        try:
            from osia.chatgpt_account.provider import get_account_provider
            payload = get_account_provider().rate_limits()
        except Exception:
            payload = {}
    windows = [{"label": item["label"], "remaining_percent": item["remainingPercent"]}
               for item in quota_windows(payload)]
    return {"state": "available" if windows else "unavailable", "windows": windows}


def _route(message: dict) -> tuple[str, str] | None:
    provider = _text(message.get("_provider_id"), 64)
    model = _text(message.get("_model_id"))
    return (provider, model) if provider and model else None


def _accepted_model_route(conversation_id: str, conversation: dict) -> tuple[str, str] | None:
    """Read one exact unresolved owner; this never selects or changes execution."""
    from osia.conversations import _unresolved_canonical_chat_users

    try:
        owners = list(_unresolved_canonical_chat_users(conversation))
    except Exception:
        return None
    if len(owners) != 1:
        return None
    message, binding = owners[0]
    if binding.get("conversation_id") != conversation_id:
        return None
    # The helper preserves canonical identity strings; malformed imported
    # metadata is not converted into a displayed model label.
    if not all(isinstance(message.get("_"+key), str) for key in ("provider_id", "model_id")):
        return None
    provider, model = _text(binding.get("provider_id"), 64), _text(binding.get("model_id"))
    return (provider, model) if provider and model else None


def _estimated_usage(messages: list, selected: tuple[str, str], model_label: str,
                     pricing_metadata: Mapping | None, today: date) -> dict:
    from osia.conversation_context import estimate_message_tokens
    rows: OrderedDict[tuple[str, str], dict] = OrderedDict()
    inputs = 3
    previous_user_route = None
    previous_user_turn = ""
    groups = {}

    def row_for(route):
        if route not in rows:
            metadata = None
            if isinstance(pricing_metadata, Mapping):
                metadata = pricing_metadata.get(route) or pricing_metadata.get(":".join(route))
                if metadata is None and route == selected and ("pricing" in pricing_metadata or "input_per_million" in pricing_metadata):
                    metadata = pricing_metadata
            price = model_pricing(*route, metadata=metadata, today=today)
            rows[route] = {"model": {"provider_id": route[0], "id": route[1], "label": model_label if route == selected else route[1]},
                           "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                           "cost_usd": 0.0 if price is not None else None, "pricing": price}
        return rows[route]

    for index, message in enumerate(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict) or message.get("_internal"):
            continue
        role = message.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        tokens = estimate_message_tokens(message)
        turn = _text(message.get("_turn_id"), 192)
        if role == "user":
            previous_user_route, previous_user_turn = _route(message), turn
        elif role == "assistant":
            route = _route(message)
            if route is None and previous_user_route and turn and turn == previous_user_turn:
                route = previous_user_route
            route = route or ("unknown", "unknown")
            if route[0] != "chatgpt_account":
                row = row_for(route)
                group = (route, turn or f"message-{index}")
                first = group not in groups
                if first:
                    groups[group] = inputs
                    row["input_tokens"] += inputs
                row["output_tokens"] += tokens
                row["total_tokens"] = row["input_tokens"] + row["output_tokens"]
                price = row["pricing"]
                if price is not None and row["cost_usd"] is not None:
                    long = groups[group] > price.get("long_context_threshold", math.inf)
                    input_rate = price["input_per_million"] * (price.get("long_input_multiplier", 1) if long else 1)
                    output_rate = price["output_per_million"] * (price.get("long_output_multiplier", 1) if long else 1)
                    cost = row["cost_usd"] + ((inputs / 1e6 * input_rate if first else 0) + tokens / 1e6 * output_rate)
                    row["cost_usd"] = cost if math.isfinite(cost) else None
        inputs += tokens
    row_for(selected)  # expose the selected model's rates even before its first response
    models = list(rows.values())
    known = all(item["pricing"] is not None and item["cost_usd"] is not None for item in models)
    for item in models:
        if item["cost_usd"] is not None:
            item["cost_usd"] = round(item["cost_usd"], 10)
    cost = sum(r["cost_usd"] for r in models) if known else None
    if cost is not None and not math.isfinite(cost):
        known, cost = False, None
    return {"estimated": True, "input_tokens": sum(r["input_tokens"] for r in models),
            "output_tokens": sum(r["output_tokens"] for r in models),
            "total_tokens": sum(r["total_tokens"] for r in models),
            "cost_usd": round(cost, 10) if cost is not None else None,
            "currency": "USD", "pricing_known": known, "basis": "visible_conversation", "models": models,
            "limitations": ["unpersisted_system_tool_and_reasoning_tokens_not_counted", "cache_discounts_and_extra_fees_not_included"]}


def build_chat_status(conversation_id: str, provider_id: str, model_id: str, model_label: str = "", *,
                      conversation: dict | None = None, context_usage: dict | None = None,
                      quota_payload: Any = None, pricing_metadata: Mapping | None = None,
                      now: datetime | None = None) -> dict:
    """Build the safe PC-owned status payload; callers run this off the UI loop."""
    conversation_id = _text(conversation_id, 128)
    provider_id, model_id = _text(provider_id, 64), _text(model_id)
    model_label = _text(model_label) or model_id
    conv = _conversation(conversation_id, conversation)
    accepted_route = _accepted_model_route(conversation_id, conv)
    if accepted_route is not None and accepted_route != (provider_id, model_id):
        provider_id, model_id = accepted_route
        model_label = model_id
        context_usage = None  # the caller's selected-route meter may be unrelated
        if isinstance(pricing_metadata, Mapping) and ("pricing" in pricing_metadata or "input_per_million" in pricing_metadata):
            pricing_metadata = None  # preserve route-keyed maps, not an unrelated inline quote
    connection = "account" if provider_id == "chatgpt_account" else "local" if provider_id in _LOCAL else "api"
    return {"schema_version": 1, "command_id": "status.info",
            "model": {"provider_id": provider_id, "id": model_id, "label": model_label},
            "connection": connection,
            "context": _context(conversation_id, provider_id, model_id, conv, context_usage),
            "quota": _quota(quota_payload) if connection == "account" else None,
            "usage": None if connection == "account" else _estimated_usage(conv.get("messages", []), (provider_id, model_id), model_label, pricing_metadata, (now or datetime.now(timezone.utc)).date())}


__all__ = ["build_chat_status", "model_pricing"]
