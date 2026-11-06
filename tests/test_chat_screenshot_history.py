from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import sys
import time
from collections import OrderedDict
from types import SimpleNamespace

import pytest
from PIL import Image

from osia import chat_screenshots, computer_use_v4_integration, conversations, image_inputs, visual_history
from osia.tools import desktop_control


@pytest.fixture
def visual_store(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(chat_screenshots, "SCREENSHOTS_DIR", tmp_path / "screenshots")
    monkeypatch.setattr(chat_screenshots, "_OBSERVATIONS", {})
    monkeypatch.setattr(image_inputs, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(image_inputs, "_IMAGE_STORE", OrderedDict())
    monkeypatch.setattr(image_inputs, "_IMAGE_STORE_BYTES", 0)


def _image(source="user_upload", color="navy"):
    output = io.BytesIO()
    Image.new("RGB", (100, 80), color).save(output, format="PNG")
    return image_inputs.create_image_artifact(output.getvalue(), "image/png", source)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SendInput ABI")
def test_native_unicode_entry_preserves_accents_and_surrogate_pairs(monkeypatch):
    events = []

    def send_input(count, inputs, size):
        assert size == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        assert all(item.type == 1 for item in inputs)
        events.extend((key.virtual_key, key.scan, key.flags)
                      for key in (item.data.key for item in inputs))
        return count

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: SimpleNamespace(SendInput=send_input))
    monkeypatch.setattr(desktop_control, "_intended_foreground_error", lambda: None)
    text = "L’élève écrit déjà à Noël, ça coûte 2 € 🙂"
    assert desktop_control.type_text(text + "\r\n\t").startswith("typed ")
    down, up = events[::2], events[1::2]
    assert up == [(virtual, scan, flags | 2) for virtual, scan, flags in down]
    assert down[-2:] == [(0x0D, 0, 0), (0x09, 0, 0)]
    decoded = b"".join(scan.to_bytes(2, "little") for _, scan, _ in down[:-2])
    assert decoded.decode("utf-16-le") == text


@pytest.mark.parametrize("provider", (
    "openai", "anthropic", "chatgpt_account", "deepseek", "google", "groq",
    "xai", "mistral", "openrouter", "lmstudio",
))
def test_previous_images_reach_the_actual_provider_adapters(visual_store, monkeypatch, provider):
    monkeypatch.setattr(visual_history, "image_input_decision", lambda *_args: SimpleNamespace(usable=True))
    upload = image_inputs.register_image_artifact(_image(), owner_id="conv-visual")
    messages = [
        {"role": "user", "_visual_history": True, "content": [{"type": "text", "text": "Start here."}, upload]},
        {"role": "assistant", "_visual_history": True, "content": "I saw it."},
        {"role": "user", "content": "Continue with the second item."},
    ]
    result = visual_history.project_visual_history(
        messages, provider=provider, model="fixture-vision", conversation_id="conv-visual", cache={},
    )
    wire = image_inputs.adapt_messages_for_provider(result, provider, owner_id="conv-visual")
    images = image_inputs.image_parts(wire[0]["content"])
    assert len(images) == 1 and images[0]["type"] == "image_url"
    assert wire[-1]["content"] == "Continue with the second item."


def test_text_only_history_uses_local_ocr_and_respects_conversation_scope(visual_store, monkeypatch):
    monkeypatch.setattr(visual_history, "_extract_text", lambda _artifact: "Éléments à traiter")
    reference = image_inputs.register_image_artifact(_image(), owner_id="conv-visual")

    def wire(conversation_id):
        return visual_history.project_visual_history([
            {"role": "user", "_visual_history": True, "_conversation_id": "conv-visual", "content": [
                {"type": "text", "text": "Earlier input"}, reference,
            ]},
            {"role": "user", "content": "Next step"},
        ], provider="ollama", model="text-model", conversation_id=conversation_id, cache={})

    assert "Éléments à traiter" in wire("conv-visual")[0]["content"]
    assert wire("other-conversation")[0]["content"] == "Earlier input"


def test_expired_history_does_not_block_new_user_input(visual_store):
    now = time.time()
    image = _image()
    expired = image_inputs.create_image_artifact(
        image.image_bytes, image.mime_type, "user_upload",
        created_at=now - 20, expires_at=now - 1,
    )
    reference = image_inputs.register_image_artifact(expired, owner_id="conv-visual")
    result = visual_history.project_visual_history([
        {"role": "user", "_visual_history": True, "content": [reference]},
        {"role": "assistant", "_visual_history": True, "content": "Earlier answer"},
        {"role": "user", "content": "Keep going."},
    ], provider="openai", model="gpt-4o", conversation_id="conv-visual", cache={})
    assert "no longer available" in result[0]["content"]
    assert result[-1]["content"] == "Keep going."


def test_special_mode_hides_pixels_and_restores_history_when_reenabled(visual_store):
    from osia.agent import OSIAAgent
    from osia.slash_commands import chat_command_scope

    conv_id = conversations.create_conversation()["id"]
    reference = image_inputs.register_image_artifact(_image(), owner_id=conv_id)
    agent = SimpleNamespace(
        provider="openai", model="gpt-4o", lang="en", turn_context=None,
        _chatgpt_conversation_id=conv_id,
        messages=[
            {"role": "user", "_visual_history": True,
             "content": [{"type": "text", "text": "Earlier input."}, reference]},
            {"role": "assistant", "content": "Earlier reply."},
            {"role": "user", "content": "Continue."},
        ],
    )
    conversations.set_conversation_special_mode(conv_id, True)
    with chat_command_scope(conv_id, None):
        wire = OSIAAgent._wire_messages(agent)
        assert wire[0]["content"] == "Earlier input."
        assert all(not image_inputs.has_image_content(frame.get("content")) for frame in wire)
    conversations.set_conversation_special_mode(conv_id, False)
    with chat_command_scope(conv_id, None):
        wire = OSIAAgent._wire_messages(agent)
        assert image_inputs.image_parts(wire[0]["content"])[0]["type"] == "image_url"


def test_screenshot_is_persisted_and_downloadable_only_for_its_exact_message(visual_store, monkeypatch):
    from osia.server import server

    conversation = conversations.create_conversation()
    conversation_id = conversation["id"]
    turn_id = "turn-proof"
    artifact = _image("desktop_capture")
    metadata = {"_conversation_id": conversation_id, "_turn_id": turn_id, "_request_id": "request-proof"}
    assert conversations.commit_message(conversation_id, "user", "Show me the result.",
                                        metadata={**metadata, "_message_id": "user-proof"})
    frame = SimpleNamespace(to_image_artifact=lambda: artifact, captured_at=time.time())
    monkeypatch.setattr(computer_use_v4_integration, "_resolve_window", lambda _window: 7)
    monkeypatch.setattr(computer_use_v4_integration, "_loop", lambda: SimpleNamespace(screenshot=lambda _hwnd: frame))
    with computer_use_v4_integration.computer_use_v4_turn_scope(
        "ollama", "text-model", conversation_id=conversation_id, turn_id=turn_id,
    ):
        saved = computer_use_v4_integration.tool_save_screenshot()
    assert saved["receipt"]["reason_code"] == "CHAT_SCREENSHOT_SAVED"

    transient = image_inputs.register_image_artifact(
        artifact, owner_id=image_inputs.turn_scoped_media_owner(conversation_id, turn_id),
    )
    chat_screenshots.remember_chat_observation(conversation_id, turn_id, transient)
    receipt = conversations.commit_message(conversation_id, "assistant", "Here it is.",
                                           metadata={**metadata, "_message_id": "assistant-proof"})
    message = receipt["conversation"]["messages"][-1]
    record = message["_screenshots"][0]
    assert record["id"] == saved["receipt"]["screenshot_id"]
    assert chat_screenshots.read_chat_screenshot(conversation_id, turn_id, record) == artifact.image_bytes
    historic = image_inputs.resolve_image_reference(message["_observed_image"], owner_id=conversation_id)
    assert historic.lifecycle == "durable_backed" and historic.expires_at > artifact.expires_at
    projected = server._conversation_history_frame(conversations.get_conversation(conversation_id))
    assert projected["messages"][-1]["screenshots"] == [record]
    assert "_observed_image" not in projected["messages"][-1]

    monkeypatch.setattr(visual_history, "image_input_decision", lambda *_args: SimpleNamespace(usable=True))
    earlier = [{**item, "_visual_history": True} for item in receipt["conversation"]["messages"]]
    earlier.append({"role": "user", "content": "What should we do next?"})
    recalled = visual_history.project_visual_history(
        earlier, provider="deepseek", model="deepseek-flash",
        conversation_id=conversation_id, cache={},
    )
    assert len(image_inputs.image_references(recalled[0]["content"])) == 1
    assert recalled[-1]["content"] == "What should we do next?"

    class Channel:
        def __init__(self):
            self.frames = []

        async def send_json(self, frame):
            self.frames.append(frame)

    request = {"conversation_id": conversation_id, "turn_id": turn_id,
               "message_id": "assistant-proof", "request_id": "open-proof", "screenshot_id": record["id"]}
    channel = Channel()
    monkeypatch.setattr(server, "ATTACHMENT_CHUNK_BYTES", 128)
    asyncio.run(server._send_chat_screenshot(channel, request))
    assert len(channel.frames) > 1
    assert [frame["index"] for frame in channel.frames] == list(range(len(channel.frames)))
    assert all(frame["request_id"] == "open-proof" for frame in channel.frames)
    preview = b"".join(base64.b64decode(frame["data"]) for frame in channel.frames)
    with Image.open(io.BytesIO(preview)) as image:
        assert image.size == (100, 80)

    wrong_turn = Channel()
    asyncio.run(server._send_chat_screenshot(wrong_turn, {**request, "turn_id": "different-turn"}))
    assert wrong_turn.frames[0]["error"] == "screenshot_unavailable"
    assert "data" not in wrong_turn.frames[0]
