"""Catalog, migration and actual serialized HTTP image-transport sentinels."""

import base64
import copy
import io
import threading
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from openai import OpenAI

from osia import config, image_inputs, mobile_model_catalogue as mobile, models, reasoning
from osia.agent import OSIAAgent
from osia.attachments import CommittedAttachment, build_agent_input
from osia.deepseek import LEGACY_API_IDS, MODEL_ID, migrate_model_id, migrate_settings
from osia.desktop_attachments import prepare_desktop_turn, stage_attachment
from osia.image_inputs import ImageInputError, ensure_image_input_supported, release_media_owner
from osia.model_capabilities import effective_route_capabilities




def synthetic_image() -> bytes:
    picture = Image.new("RGB", (96, 64), "red")
    picture.paste("blue", (48, 0, 96, 64))
    output = io.BytesIO()
    picture.save(output, format="PNG")
    return output.getvalue()


def staged_input(path: Path, prompt: str, surface: str, owner: str):
    if surface == "desktop":
        return prepare_desktop_turn(
            prompt, [stage_attachment(path, 0)], provider="deepseek",
            model=MODEL_ID, owner_id=owner,
        ).provider_content
    data = path.read_bytes()
    return build_agent_input(prompt, [CommittedAttachment(
        attachment_id="att_wor298", file_id="file_wor298", logical_order=0,
        display_name=path.name, path=path, byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(), detected_category="image",
        detected_mime="image/png",
    )], owner_id=owner)


def probe_agent(content, client, mode="auto", owner="wor298"):
    """Use production message/reasoning/stream builders with a fake client."""
    agent = OSIAAgent.__new__(OSIAAgent)
    agent.provider, agent.model = "deepseek", MODEL_ID
    agent.messages = [{"role": "user", "content": content}]
    agent.turn_context, agent.lang = None, "en"
    agent._chatgpt_conversation_id = owner
    agent._execution_generation_local = threading.local()
    agent._trace_reasoning_binding = mode
    agent.client = client
    return agent


def wire_evidence(request: httpx.Request, *, mode: str, surface: str) -> dict:
    body = json.loads(request.content)
    parts = [
        part
        for message in body["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
    ]
    images = [part for part in parts if part.get("type") == "image_url"]
    has_text = any(
        part.get("type") == "text" and part.get("text") for part in parts
    ) or any(
        isinstance(message.get("content"), str) and message["content"]
        for message in body["messages"]
    )
    image_hashes = [
        hashlib.sha256(
            base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
        ).hexdigest()
        for part in images
    ]
    return {
        "requested_provider": "deepseek",
        "requested_model": MODEL_ID,
        "effective_provider": (
            "deepseek" if request.url.host == "api.deepseek.com"
            else "unverified_endpoint"
        ),
        "effective_model": body["model"],
        "endpoint_host": request.url.host,
        "endpoint_path": request.url.path,
        "surface": surface,
        "image_parts_present": bool(images),
        "image_part_count": len(images),
        "text_present": bool(has_text),
        "text_and_image": bool(has_text and images),
        "image_sha256": image_hashes,
        "reasoning_requested": mode,
        "reasoning_effective": "high (native default)" if mode == "auto" else mode,
        "thinking": body.get("thinking"),
        "reasoning_effort": body.get("reasoning_effort"),
        "compatibility_transformations": (
            (["image_ref -> image_url data URL"] if images else [])
            + (["SDK extra_body -> top-level JSON"] if "thinking" in body else [])
        ),
        "sanitized_request": {
            "model": body["model"],
            "stream": body.get("stream", False),
            "messages": [
                {
                    "role": message["role"],
                    "content": [
                        {
                            "type": part.get("type"),
                            **(
                                {"image_url": {"url": "data:image/png;base64,<redacted>"}}
                                if part.get("type") == "image_url"
                                else {"text": "<redacted>"}
                            ),
                        }
                        for part in message["content"]
                    ]
                    if isinstance(message.get("content"), list) else "<redacted>"
                }
                for message in body["messages"]
            ],
            **({"thinking": body["thinking"]} if "thinking" in body else {}),
            **({"reasoning_effort": body["reasoning_effort"]}
               if "reasoning_effort" in body else {}),
        },
    }
MODES = {"auto": {}, "off": {"thinking": {"type": "disabled"}}, **{
    mode: {"thinking": {"type": "enabled"}, "reasoning_effort": mode}
    for mode in ("low", "high", "max")
}}


@pytest.fixture
def image_file(tmp_path):
    path = tmp_path / "sample.png"
    path.write_bytes(synthetic_image())
    return path


def test_catalog_one_model_even_with_stale_remote_cache(monkeypatch):
    models.invalidate_remote_models()
    stale = [{"provider": "deepseek", "id": old} for old in LEGACY_API_IDS | {"deepseek-future"}]
    monkeypatch.setattr(models, "get_ollama_models", lambda: [])
    monkeypatch.setattr(models, "cached_ollama_models", lambda: [])
    for inventory in (models.HARDCODED_MODELS,
                      models.get_all_models({}, remote_models=stale),
                      models.get_all_models_cached({}, remote_models=stale)):
        entries = [m for m in inventory if m["provider"] == "deepseek"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["id"] == MODEL_ID
        assert entry["label"] == "DeepSeek V4.1 Flash"
        assert entry["vision"] and entry["tool_calls"]
        assert entry["inputModalities"] == ["text", "image"]
    capabilities = effective_route_capabilities("deepseek", MODEL_ID)
    assert capabilities.vision_supported is True
    assert capabilities.reasoning_controls == tuple(MODES)
    for old in LEGACY_API_IDS:
        assert reasoning.get_reasoning_options("deepseek", old) == []


def test_pc_mobile_parity_and_stale_catalog_rejection():
    stale = [{"provider": "deepseek", "id": old} for old in LEGACY_API_IDS]
    inventory = mobile.build_model_inventory(models.HARDCODED_MODELS + stale, models.API_PROVIDERS)
    entries = [m for m in inventory if m["provider_id"] == "deepseek"]
    assert len(entries) == 1
    pc = entries[0]
    phone = mobile._mobile_model(pc)
    assert pc["model_id"] == phone["id"] == MODEL_ID
    assert pc["model_display_name"] == phone["displayName"] == "DeepSeek V4.1 Flash"
    assert pc["input_modalities"] == phone["inputModalities"] == ["text", "image"]
    assert phone["vision"] and phone["selectable"] and pc["capabilities"]["vision"]
    assert pc["reasoning_options"] == phone["reasoning"]["options"]
    assert [o["id"] for o in pc["reasoning_options"]] == list(MODES)
    assert [o["label"] for o in pc["reasoning_options"]] == ["Default", "Non-thinking", "Low", "High", "Max"]


def test_non_deepseek_before_after_snapshot_identical():
    baseline = json.loads((Path(__file__).parent / "fixtures/wor298_non_deepseek.json").read_text(encoding="utf-8"))
    actual = {
        "models": [m for m in models.HARDCODED_MODELS if m["provider"] != "deepseek"],
        "providers": {p: v for p, v in models.PROVIDERS.items() if p != "deepseek"},
        "reasoning": {p + ":" + m: v for (p, m), v in reasoning.MODEL_REASONING_CAPABILITIES.items() if p != "deepseek"},
        "complex": [m for m in models.COMPLEX_PREFERENCE if m[1] != "deepseek"],
        "simple": [m for m in models.SIMPLE_API_FALLBACK if m[1] != "deepseek"],
        "image_adapters": {p: [v[0], sorted(v[1])] for p, v in image_inputs._ADAPTERS.items() if p != "deepseek"},
        "pc_inventory": [m for m in mobile.build_model_inventory(models.HARDCODED_MODELS, models.API_PROVIDERS) if m["provider_id"] != "deepseek"],
        "provider_order": mobile.PROVIDER_ORDER,
    }
    # WOR-355 intentionally evolves other providers. Preserve this historical
    # test's real boundary: DeepSeek cannot modify their established contracts.
    for key in ("complex", "simple", "image_adapters", "provider_order"):
        assert json.loads(json.dumps(actual[key])) == baseline[key]
    for old in baseline["models"]:
        if old["provider"] == "xai":
            continue  # WOR-356: documented Grok retirement and Mini vision fix.
        assert old in actual["models"]
    for pair, capability in baseline["reasoning"].items():
        assert actual["reasoning"][pair] == capability



@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("surface", ["desktop", "mobile"])
def test_actual_sdk_http_image_body_preserves_pixels_and_text(image_file, mode, surface):
    owner = "wor298-test-" + surface + mode
    content = staged_input(image_file, "Name both colors.", surface, owner)
    before = copy.deepcopy(content)
    evidence, bodies = [], []

    def handle(request):
        bodies.append(json.loads(request.content))
        evidence.append(wire_evidence(request, mode=mode, surface=surface))
        chunk = {"id": "safe", "object": "chat.completion.chunk", "created": 1,
                 "model": MODEL_ID, "choices": [{"index": 0, "delta": {"content": "red blue"}, "finish_reason": None}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    try:
        ensure_image_input_supported("deepseek", MODEL_ID, content, owner_id=owner)
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            with OpenAI(api_key="test-only", base_url=models.PROVIDERS["deepseek"]["base_url"], http_client=http) as client:
                assert "".join(probe_agent(content, client, mode, owner)._stream_deltas()) == "red blue"
        assert content == before
        receipt, body = evidence[0], bodies[0]
        assert len(evidence) == 1
        assert receipt["effective_provider"] == "deepseek"
        assert receipt["effective_model"] == MODEL_ID
        assert receipt["image_part_count"] == 1
        assert receipt["text_and_image"] is True
        assert receipt["image_sha256"] == [hashlib.sha256(synthetic_image()).hexdigest()]
        assert any(p.get("text", "").startswith("Name both colors.") for m in body["messages"] for p in m["content"])
        assert {key: body[key] for key in ("thinking", "reasoning_effort") if key in body} == MODES[mode]
        assert "extra_body" not in body
        assert "max_tokens" not in body and "max_completion_tokens" not in body
        assert "test-only" not in json.dumps(receipt)
    finally:
        release_media_owner(owner)


@pytest.mark.parametrize("mode", ["medium", "minimal", "ultra", "xhigh", "normal"])
def test_unsupported_reasoning_is_not_silently_remapped(mode):
    with pytest.raises(ValueError, match="ERROR: unsupported DeepSeek reasoning"):
        reasoning.build_reasoning_request("deepseek", MODEL_ID, {"default": mode})
    agent = probe_agent("Hello", None, mode)
    with pytest.raises(ValueError, match="ERROR: unsupported DeepSeek reasoning"):
        agent._reasoning_request()


def test_native_numeric_metadata_is_distinct_from_hosted_api_support():
    capability = reasoning.get_reasoning_capability("deepseek", MODEL_ID)
    numeric = capability["native_numeric_effort"]
    assert (numeric["minimum"], numeric["maximum"]) == (1, 100)
    assert numeric["presets"] == {"low": 50, "high": 75, "max": 100}
    assert numeric["api_exposed"] is False
    assert capability["provider_default"] == "high"


@pytest.mark.parametrize("legacy", sorted(LEGACY_API_IDS | {"deepseek-v3", "deepseek-r1"}))
def test_legacy_settings_migrate_only_deepseek_and_preserve_chain_order(legacy):
    settings = {"selected_model": legacy, "selected_model_provider": "deepseek",
                "autonomous_model": legacy, "autonomous_model_provider": "ollama",
                "ai_hierarchy": {"heavy": ["openai:gpt-5.5", "deepseek:" + legacy, "ollama:" + legacy], "medium": []},
                "reasoning": {"models": {legacy: "low", "gpt-5.5": "high"}},
                "api_fallbacks": {"deepseek": {"fallback": "https://example.invalid/v1"}},
                "unrelated": {"keep": True}}
    original = copy.deepcopy(settings)
    migrated = migrate_settings(settings)
    assert settings == original
    assert migrated["selected_model"] == MODEL_ID
    assert migrated["selected_model_provider"] == "deepseek"
    assert migrated["autonomous_model"] == legacy
    assert migrated["autonomous_model_provider"] == "ollama"
    assert migrated["ai_hierarchy"]["heavy"] == ["openai:gpt-5.5", "deepseek:" + MODEL_ID, "ollama:" + legacy]
    assert migrated["reasoning"]["models"][MODEL_ID] == "low"
    assert migrated["api_fallbacks"] == original["api_fallbacks"]
    assert migrate_settings(migrated) == migrated


def test_legacy_missing_provider_only_infers_unambiguous_official_api_aliases():
    assert migrate_model_id(None, "deepseek-v4-pro") == MODEL_ID
    assert migrate_model_id(None, "deepseek-r1:8b") == "deepseek-r1:8b"
    assert migrate_model_id("ollama", "deepseek-v4-pro") == "deepseek-v4-pro"
    assert migrate_model_id("openai", "deepseek-chat") == "deepseek-chat"


def test_old_mobile_selection_does_not_fall_back_to_another_provider(monkeypatch):
    # WOR-303: the dedicated Autonomous model resolution is gone.  The existing
    # Chat model validation still accepts the migrated DeepSeek identity and
    # never silently falls back to another provider.
    # Supply configured-provider facts explicitly; never depend on or write the
    # operator's credentials/settings, or probe a live account/local runtime.
    monkeypatch.setattr(mobile, "load_api_keys", lambda: {"deepseek": "test-only"})
    monkeypatch.setattr(mobile, "get_all_models", lambda **_kw: models.HARDCODED_MODELS)
    monkeypatch.setattr(mobile, "account_snapshot", lambda **_kw: {})
    monkeypatch.setattr(mobile, "load_settings", lambda: {})
    selected = {}
    monkeypatch.setattr(mobile, "set_setting", lambda key, value: selected.update({key: value}))
    inventory = mobile.build_model_inventory(models.HARDCODED_MODELS, models.API_PROVIDERS)
    assert inventory
    assert mobile.validate_and_select("deepseek", MODEL_ID) is True
    assert mobile.validate_and_select("deepseek", "deepseek-does-not-exist") is False
    assert selected == {"selected_model": MODEL_ID, "selected_model_provider": "deepseek"}


def test_settings_read_migrates_in_memory_and_save_persists_atomically(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    raw = {"selected_model": "deepseek-v4-pro", "selected_model_provider": "deepseek"}
    path.write_text(json.dumps(raw))
    original = path.read_bytes()
    monkeypatch.setattr(config, "SETTINGS_FILE", str(path))
    monkeypatch.setattr(config, "_ensure_config_dir", lambda: None)
    loaded = config.load_settings()
    assert loaded["selected_model"] == MODEL_ID
    assert path.read_bytes() == original
    config.save_settings(loaded)
    assert json.loads(path.read_text())["selected_model"] == MODEL_ID


def test_runtime_and_scheduled_model_loading_keep_deepseek(monkeypatch):
    import osia.agent as agent_module
    from osia.scheduled_tasks import _resolve_model_metadata
    monkeypatch.setattr(agent_module, "OpenAI", lambda **kwargs: SimpleNamespace(kwargs=kwargs))
    monkeypatch.setattr(OSIAAgent, "_provider_key_chain", lambda *args: ["test-only"])
    monkeypatch.setattr(models, "get_ollama_models", lambda: [])
    agent = probe_agent("Hello", None)
    agent.set_model("deepseek-v4-pro", provider="deepseek")
    assert (agent.provider, agent.model) == ("deepseek", MODEL_ID)
    assert agent.client.kwargs["base_url"] == "https://api.deepseek.com/v1"
    scheduled = _resolve_model_metadata(model={"provider": "deepseek", "id": "deepseek-v4-pro"})
    assert scheduled["provider"] == "deepseek" and scheduled["id"] == MODEL_ID


def test_unsupported_image_route_fails_honestly(image_file):
    owner = "wor298-unsupported"
    content = staged_input(image_file, "Name colors", "mobile", owner)
    try:
        for provider, model in [("deepseek", "unknown-model"), ("groq", "llama-3.3-70b-versatile")]:
            with pytest.raises(ImageInputError, match="ERROR:"):
                ensure_image_input_supported(provider, model, content, owner_id=owner)
        with pytest.raises(ImageInputError, match="ERROR:"):
            image_inputs.adapt_messages_for_provider([{"role": "user", "content": content}], "unsupported-provider", owner_id=owner)
    finally:
        release_media_owner(owner)


def test_planning_preserves_images_through_real_nonstream_transport(image_file, monkeypatch):
    # DeepSeek's existing run_planning path delegates to the standard agent;
    # it does not enter the Account-only V2 semantic transport.
    owner = ""  # run_planning has no conversation ID; use its process-owned staging scope.
    content = staged_input(image_file, "Name both colors briefly.", "mobile", owner)
    bodies = []
    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "safe", "object": "chat.completion", "created": 1,
            "model": MODEL_ID, "choices": [{"index": 0, "message": {"role": "assistant", "content": "red blue"}, "finish_reason": "stop"}]})
    monkeypatch.setattr(OSIAAgent, "_provider_key_chain", lambda *args: ["test-only"])
    try:
        agent = OSIAAgent(lang="en")
        agent.set_model(MODEL_ID, "deepseek")
        agent._trace_reasoning_binding = "low"
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            with OpenAI(api_key="test-only", base_url=models.PROVIDERS["deepseek"]["base_url"], http_client=http) as client:
                agent.client = client
                result = agent.run_planning(content, output_schema={"type": "object"})
        assert result
        assert bodies and all(body["model"] == MODEL_ID for body in bodies)
        assert all(any(p.get("type") == "image_url" for m in body["messages"] if isinstance(m.get("content"), list) for p in m["content"]) for body in bodies)
        assert all(body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "low" for body in bodies)
    finally:
        release_media_owner(owner)
