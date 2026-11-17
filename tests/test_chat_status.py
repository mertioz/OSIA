"""Minimal status reads existing facts and labels incomplete billing estimates."""
from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace
import json

import pytest

from osia import chat_status as status


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch):
    from osia import conversation_context as context
    monkeypatch.setattr(context, "_USAGE_BY_CONVERSATION", OrderedDict())
    monkeypatch.setattr(context, "resolve_context_limit", lambda *_: 1050000)


def conversation(messages=None, **extra):
    return {"id": "test-chat", "messages": messages or [], **extra}


def message(role, text, provider="openai", model="gpt-6.1-sol", turn="t1"):
    return {"role": role, "content": text, "_provider_id": provider, "_model_id": model, "_turn_id": turn}


def build(conv=None, provider="openai", model="gpt-6.1-sol", **kwargs):
    return status.build_chat_status("test-chat", provider, model, conversation=conv or conversation(),
                                    now=datetime(2026, 11, 17, tzinfo=timezone.utc), **kwargs)


def test_account_status_contains_only_current_quota_model_and_context():
    conv = conversation(_context_usage={"provider": "chatgpt_account", "model": "gpt-6.1-sol", "used": 100001, "maximum": 828400, "estimated": False})
    quota = {"rateLimits": {"primary": {"usedPercent": 12, "windowDurationMins": 300}, "secondary": {"usedPercent": 40, "windowDurationMins": 10080}},
             "account": {"email": "private@example.invalid"}, "access_token": "PRIVATE_SENTINEL"}
    result = build(conv, provider="chatgpt_account", quota_payload=quota)
    assert result["connection"] == "account" and result["usage"] is None
    assert result["context"] == {"used": 100001, "maximum": 828400, "estimated": False}
    assert result["quota"] == {"state": "available", "windows": [{"label": "5 h", "remaining_percent": 88}, {"label": "Weekly", "remaining_percent": 60}]}
    assert "PRIVATE_SENTINEL" not in json.dumps(result) and "private@example" not in json.dumps(result)
    assert set(result) == {"schema_version", "command_id", "model", "connection", "context", "quota", "usage"}


def test_account_quota_uses_existing_read_when_not_supplied(monkeypatch):
    from osia.chatgpt_account import provider
    calls = []
    def rate_limits():
        calls.append("read")
        return {"rateLimits": {"primary": {"usedPercent": 95}}}
    monkeypatch.setattr(provider, "get_account_provider", lambda: SimpleNamespace(rate_limits=rate_limits))
    conv = conversation(_context_usage={"used": 1, "maximum": 828400, "estimated": False})
    assert build(conv, provider="chatgpt_account")["quota"]["windows"] == [{"label": "Primary", "remaining_percent": 5}]
    assert calls == ["read"]


def test_account_quota_failure_does_not_invent_unlimited_or_zero(monkeypatch):
    from osia.chatgpt_account import provider
    def unavailable():
        raise OSError("offline")
    monkeypatch.setattr(provider, "get_account_provider", unavailable)
    assert build(provider="chatgpt_account")["quota"] == {"state": "unavailable", "windows": []}


def test_api_status_never_reads_account_quota(monkeypatch):
    monkeypatch.setattr(status, "_quota", lambda *_: pytest.fail("account quota read for API"))
    result = build()
    assert result["connection"] == "api" and result["quota"] is None
    assert result["usage"]["estimated"] and result["context"]["estimated"]
    assert result["usage"]["models"][0]["pricing"]["input_per_million"] == 2


def test_api_estimate_counts_repeated_visible_inputs_per_response():
    from osia.conversation_context import estimate_message_tokens
    messages = [message("user", "one", turn="t1"), message("assistant", "first", turn="t1"),
                message("user", "two", turn="t2"), message("assistant", "second", turn="t2")]
    usage = build(conversation(messages))["usage"]
    tokens = [estimate_message_tokens(m) for m in messages]
    assert usage["input_tokens"] == 3 + tokens[0] + 3 + sum(tokens[:3])
    assert usage["output_tokens"] == tokens[1] + tokens[3]
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert usage["cost_usd"] == pytest.approx((usage["input_tokens"]*2 + usage["output_tokens"]*10)/1e6)
    assert usage["basis"] == "visible_conversation"


def test_same_turn_fragments_do_not_double_charge_the_input_projection():
    from osia.conversation_context import estimate_message_tokens
    messages = [message("user", "one"), message("assistant", "partial"), message("assistant", "final")]
    usage = build(conversation(messages))["usage"]
    assert usage["input_tokens"] == 3 + estimate_message_tokens(messages[0])
    assert usage["output_tokens"] == sum(estimate_message_tokens(m) for m in messages[1:])


def test_each_model_keeps_its_own_token_and_price_estimate():
    messages = [message("user", "one"), message("assistant", "first"),
                message("user", "two", "deepseek", "deepseek-flash", "t2"),
                message("assistant", "second", "deepseek", "deepseek-flash", "t2")]
    rows = build(conversation(messages))["usage"]["models"]
    assert [(r["model"]["provider_id"], r["model"]["id"]) for r in rows] == [("openai", "gpt-6.1-sol"), ("deepseek", "deepseek-flash")]
    assert all(r["total_tokens"] > 0 and r["cost_usd"] > 0 for r in rows)
    assert rows[1]["pricing"]["assumption"] == "peak_paid_uncached"


def test_unrecorded_legacy_route_is_not_charged_to_the_selected_model():
    usage = build(conversation([{"role": "user", "content": "one"}, {"role": "assistant", "content": "first"}]))["usage"]
    assert usage["models"][0]["model"] == {"provider_id": "unknown", "id": "unknown", "label": "unknown"}
    assert usage["models"][0]["total_tokens"] > 0
    assert usage["models"][1]["total_tokens"] == 0
    assert usage["cost_usd"] is None and not usage["pricing_known"]


def test_no_account_subscription_price_is_added_to_api_costs():
    messages = [message("user", "one", "chatgpt_account"), message("assistant", "account reply", "chatgpt_account"),
                message("user", "two", turn="t2"), message("assistant", "api reply", turn="t2")]
    rows = build(conversation(messages))["usage"]["models"]
    assert len(rows) == 1 and rows[0]["model"]["provider_id"] == "openai"


def test_internal_messages_and_compaction_notices_are_not_billed_as_visible_turns():
    messages = [message("user", "one"), {**message("assistant", "hidden"), "_internal": True},
                {"role": "system", "content": "Context compacted."}, message("assistant", "answer")]
    original = deepcopy(messages)
    usage = build(conversation(messages))["usage"]
    expected = build(conversation([messages[0], messages[-1]]))["usage"]
    assert usage == expected and messages == original


def test_saved_context_from_another_model_is_not_shown_as_current():
    conv = conversation([message("user", "one")], _context_usage={"provider": "openai", "model": "gpt-4.1", "used": 100000, "maximum": 200000, "estimated": False})
    result = build(conv)
    assert result["context"]["maximum"] == 1050000 and result["context"]["used"] < 100000
    assert result["context"]["estimated"]


def test_foreign_conversation_cannot_be_used_for_status():
    with pytest.raises(ValueError, match="status_conversation_mismatch"):
        build({"id": "other", "messages": []})


def accepted_turn(provider="chatgpt_account", model="gpt-6.1-sol", turn="active", *, conversation_id="test-chat"):
    return {**message("user", "current objective", provider, model, turn),
            "_conversation_id": conversation_id, "_request_id": "request-"+turn,
            "_run_id": turn, "_effective_reasoning": "high",
            "_assistant_message_id": "assistant-"+turn,
            "_terminality_objective": "current objective"}


def test_status_reads_the_accepted_account_owner_instead_of_global_api_selection(monkeypatch):
    from osia import conversations
    monkeypatch.setattr(conversations, "_has_exact_user_cancelled_terminal", lambda _message: False)
    monkeypatch.setattr(conversations, "_write_atomic", lambda *_args: pytest.fail("status changed stored state"))
    conv = conversation([accepted_turn()], _context_usage={"provider": "chatgpt_account", "model": "gpt-6.1-sol", "used": 54321, "maximum": 828400, "estimated": False})
    before = deepcopy(conv)
    result = build(conv, provider="openai", model="gpt-4.1", model_label="Unrelated API label",
                   context_usage={"used": 999, "maximum": 32000, "estimated": False},
                   quota_payload={"rateLimits": {"primary": {"usedPercent": 25}}})
    assert result["model"] == {"provider_id": "chatgpt_account", "id": "gpt-6.1-sol", "label": "gpt-6.1-sol"}
    assert result["connection"] == "account" and result["usage"] is None
    assert result["context"] == {"used": 54321, "maximum": 828400, "estimated": False}
    assert result["quota"]["windows"][0]["remaining_percent"] == 75
    assert conv == before


def test_active_api_owner_ignores_global_account_and_inline_price(monkeypatch):
    from osia import conversations
    monkeypatch.setattr(conversations, "_has_exact_user_cancelled_terminal", lambda _message: False)
    monkeypatch.setattr(status, "_quota", lambda *_args: pytest.fail("account quota read for an API owner"))
    conv = conversation([accepted_turn("deepseek", "deepseek-flash")])
    result = build(conv, provider="chatgpt_account", model="gpt-6.1-sol", model_label="Account",
                   pricing_metadata={"input_per_million": 999, "output_per_million": 999})
    assert result["model"] == {"provider_id": "deepseek", "id": "deepseek-flash", "label": "deepseek-flash"}
    assert result["connection"] == "api" and result["quota"] is None
    assert result["usage"]["models"][-1]["pricing"]["input_per_million"] == .3


def test_no_accepted_turn_uses_selected_model_and_label():
    result = build(conversation(), model_label="Selected Sol")
    assert result["model"] == {"provider_id": "openai", "id": "gpt-6.1-sol", "label": "Selected Sol"}


def test_exact_committed_final_restores_selected_model_status():
    user = accepted_turn()
    final = {key: value for key, value in user.items() if key.startswith("_")}
    final.update(role="assistant", content="complete", _message_id=user["_assistant_message_id"], _terminal=True)
    result = build(conversation([user, final]), model_label="Selected Sol")
    assert result["connection"] == "api" and result["model"]["label"] == "Selected Sol"


def test_cancelled_accepted_turn_does_not_own_status(monkeypatch):
    from osia import conversations
    calls = []
    def read_stop(user):
        calls.append(user["_turn_id"])
        return True
    monkeypatch.setattr(conversations, "_has_exact_user_cancelled_terminal", read_stop)
    result = build(conversation([accepted_turn()]), model_label="Selected Sol")
    assert result["connection"] == "api" and result["model"]["label"] == "Selected Sol"
    assert calls == ["active"]


@pytest.mark.parametrize("messages", [
    [accepted_turn(turn="one"), accepted_turn("deepseek", "deepseek-flash", "two")],
    [accepted_turn(conversation_id="foreign-chat")],
    [message("user", "not a complete accepted binding", "chatgpt_account")],
])
def test_ambiguous_foreign_or_incomplete_binding_never_guesses_a_model(messages, monkeypatch):
    from osia import conversations
    monkeypatch.setattr(conversations, "_has_exact_user_cancelled_terminal", lambda _message: False)
    result = build(conversation(messages), model_label="Selected Sol")
    assert result["connection"] == "api" and result["model"]["label"] == "Selected Sol"


def test_local_and_unknown_prices_are_unavailable_not_fake_zero():
    result = build(provider="ollama", model="local-custom")
    assert result["connection"] == "local" and result["quota"] is None
    assert result["usage"]["cost_usd"] is None and not result["usage"]["pricing_known"]
    assert status.model_pricing("openai", "gpt-6.1-sol-custom") is None
    assert status.model_pricing("unknown", "gpt-6.1-sol") is None


@pytest.mark.parametrize("bad", [None, True, -1, "nan", "inf", "unknown", "1e308"])
def test_invalid_upstream_rates_remain_unknown(bad):
    assert status.model_pricing("openrouter", "vendor/model", metadata={"pricing": {"prompt": bad, "completion": "0.00001"}}) is None


def test_openrouter_rates_are_converted_from_per_token_without_alias_guessing():
    price = status.model_pricing("openrouter", "vendor/model", metadata={"pricing": {"prompt": "0.000002", "completion": "0.000008"}})
    assert (price["input_per_million"], price["output_per_million"]) == (2, 8)


def test_openrouter_cached_discovery_keeps_only_safe_rates_without_network(monkeypatch):
    from osia import models
    def no_network(*args, **kwargs):
        pytest.fail("status fetched a new catalogue")
    monkeypatch.setattr(models.requests, "get", no_network)
    raw = {"id": "fixture/model", "name": "Fixture Model", "context_length": 128000,
           "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
           "supported_parameters": ["tools"],
           "pricing": {"prompt": "0.000002", "completion": "0.000008", "input_cache_read": "0.000001", "credential": "PRIVATE_SENTINEL"}}
    found = models._discovered_model("openrouter", raw)
    assert found["pricing"] == {"prompt": .000002, "completion": .000008, "input_cache_read": .000001}
    monkeypatch.setattr(models, "_DISCOVERY", {"openrouter": {"models": [found], "status": "ready"}})
    monkeypatch.setattr(models, "_DISCOVERY_INVALIDATED", set())
    price = status.model_pricing("openrouter", "fixture/model")
    assert (price["input_per_million"], price["output_per_million"], price["cached_input_per_million"]) == (2, 8, 1)
    assert "PRIVATE_SENTINEL" not in json.dumps(found)


def test_invalid_pricing_does_not_remove_or_modify_model_capabilities():
    from osia import models
    raw = {"id": "fixture/model", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
           "supported_parameters": ["tools"], "pricing": {"prompt": "nan", "completion": "-1"}}
    found = models._discovered_model("openrouter", raw)
    assert found["available"] is True and found["tool_calls"] is True
    assert "pricing" not in found


def test_price_metadata_cannot_cross_model_or_forward_credentials():
    assert status.model_pricing("openrouter", "vendor/one", metadata={"id": "vendor/two", "pricing": {"prompt": "1", "completion": "2"}}) is None
    price = status.model_pricing("fixture", "one", metadata={"input_per_million": 1, "output_per_million": 2,
        "source_url": "https://example.invalid/pricing?key=PRIVATE_SENTINEL#PRIVATE_SENTINEL", "verified_on": "PRIVATE_SENTINEL"})
    assert price["source_url"] == "https://example.invalid/pricing"
    assert price["verified_on"] == ""
    assert "PRIVATE_SENTINEL" not in json.dumps(price)
    assert status.model_pricing("fixture", "one", metadata={"input_per_million": 1, "output_per_million": 2,
        "source_url": "https://" + "username:PRIVATE_SENTINEL" + "@example.invalid/pricing"})["source_url"] == ""


def test_explicit_free_upstream_rate_is_a_real_zero():
    price = status.model_pricing("openrouter", "vendor/model:free", metadata={"pricing": {"prompt": "0", "completion": "0"}})
    assert price["input_per_million"] == price["output_per_million"] == 0


def test_promotional_and_scheduled_price_changes_do_not_silently_stale():
    assert status.model_pricing("openai", "gpt-5.6-sol", today=date(2026, 11, 17))
    assert status.model_pricing("openai", "gpt-5.6-sol", today=date(2026, 12, 1)) is None
    flash = status.model_pricing("google", "gemini-3.8-flash", today=date(2027, 1, 1))
    assert (flash["input_per_million"], flash["output_per_million"]) == (1.5, 7.5)


def test_long_context_multiplier_uses_each_request_not_cumulative_tokens(monkeypatch):
    from osia import conversation_context as context
    monkeypatch.setattr(context, "estimate_message_tokens", lambda message: message["n"])
    messages = [{**message("user", "one"), "n": 272001}, {**message("assistant", "answer"), "n": 100}]
    result = build(conversation(messages))["usage"]
    assert result["cost_usd"] == pytest.approx((272004*4 + 100*15)/1e6)
    smaller = [{**message("user", "one"), "n": 100000}, {**message("assistant", "answer"), "n": 1},
               {**message("user", "two", turn="t2"), "n": 100000}, {**message("assistant", "answer", turn="t2"), "n": 1}]
    usage = build(conversation(smaller))["usage"]
    assert usage["input_tokens"] > 272000
    assert usage["cost_usd"] == pytest.approx((usage["input_tokens"]*2+usage["output_tokens"]*10)/1e6)
