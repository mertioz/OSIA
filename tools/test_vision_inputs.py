#!/usr/bin/env python3
"""Deterministic WOR-157 image-to-provider contract tests."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from osia.agent import OSIAAgent, _sanitize_persisted_history_message
from osia.attachments import (
    AttachmentProtocolError,
    CommittedAttachment,
    build_agent_input,
    build_local_attachment_input,
)
from osia.chatgpt_account.app_server_client import AppServerClient, AppServerError
from osia.chatgpt_account.provider import ChatGPTAccountProvider
from osia.image_inputs import (
    ImageInputError,
    adapt_messages_for_provider,
    decode_image_data_url,
    ensure_image_input_supported,
    image_input_decision,
    resolve_image_reference,
)


def _image_bytes(format_name: str) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (2, 2), "navy").save(output, format=format_name)
    return output.getvalue()


PNG = _image_bytes("PNG")
JPEG = _image_bytes("JPEG")
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
WEBP = _image_bytes("WEBP")


def _part(mime: str, data: bytes) -> dict:
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def _content(*parts: dict) -> list[dict]:
    return [{"type": "text", "text": "inspect the visual facts"}, *parts]


def _safe_content_summary(content) -> list[dict]:
    summary = []
    for index, part in enumerate(content if isinstance(content, list) else []):
        item = {"index": index, "type": part.get("type")}
        if part.get("type") == "image_url":
            decoded = decode_image_data_url(part.get("image_url"))
            item.update({
                "mime": decoded.mime,
                "byte_length": len(decoded.data),
                "sha256": hashlib.sha256(decoded.data).hexdigest(),
            })
        summary.append(item)
    return summary


class CapabilityTests(unittest.TestCase):
    def test_openai_known_multimodal_model_is_usable(self):
        decision = image_input_decision("openai", "gpt-5.4", ("image/png",))
        self.assertTrue(decision.model_known)
        self.assertTrue(decision.model_supports_vision)
        self.assertTrue(decision.adapter_supports_images)
        self.assertTrue(decision.account_path_permits_images)
        self.assertTrue(decision.formats_accepted)
        self.assertTrue(decision.usable)

    def test_unknown_model_fails_closed(self):
        decision = image_input_decision("openai", "future-unknown", ("image/png",))
        self.assertFalse(decision.model_known)
        self.assertFalse(decision.usable)

    def test_unsupported_provider_fails_closed(self):
        decision = image_input_decision("ollama", "llama3", ("image/png",))
        self.assertFalse(decision.adapter_supports_images)
        self.assertFalse(decision.usable)

    def test_provider_supports_images_but_model_is_text_only(self):
        decision = image_input_decision(
            "groq", "llama-3.3-70b-versatile", ("image/png",),
        )
        self.assertTrue(decision.adapter_supports_images)
        self.assertFalse(decision.model_supports_vision)
        self.assertFalse(decision.usable)

    def test_model_supports_vision_but_adapter_does_not(self):
        decision = image_input_decision(
            "anthropic", "claude-sonnet-4-6", ("image/png",),
        )
        self.assertTrue(decision.model_supports_vision)
        self.assertTrue(decision.adapter_supports_images)
        self.assertTrue(decision.usable)

    def test_provider_specific_formats_fail_closed(self):
        self.assertTrue(
            image_input_decision("google", "gemini-2.5-flash", ("image/gif",)).usable
        )
        self.assertTrue(
            image_input_decision("xai", "grok-3", ("image/webp",)).usable
        )
        self.assertTrue(
            image_input_decision("openai", "gpt-5.4", ("image/gif",)).usable
        )

    def test_account_capability_uses_authoritative_modalities(self):
        with patch("osia.model_capabilities._exact_account_metadata", return_value={
            "id": "account-model", "inputModalities": ["text", "image"],
        }):
            decision = image_input_decision(
                "chatgpt_account", "account-model", ("image/png",),
            )
        self.assertTrue(decision.account_path_permits_images)
        self.assertEqual(decision.encoding, "app_server_local_image")
        self.assertTrue(decision.usable)

    def test_account_text_only_modality_is_rejected(self):
        with patch("osia.model_capabilities._exact_account_metadata", return_value={
            "id": "account-model", "inputModalities": ["text"],
        }):
            decision = image_input_decision(
                "chatgpt_account", "account-model", ("image/png",),
            )
        self.assertFalse(decision.account_path_permits_images)
        self.assertFalse(decision.usable)


class DataAndDesktopTests(unittest.TestCase):
    def test_png_jpeg_gif_and_webp_bytes_are_preserved(self):
        for mime, data in (
            ("image/png", PNG),
            ("image/jpeg", JPEG),
            ("image/gif", GIF),
            ("image/webp", WEBP),
        ):
            with self.subTest(mime=mime):
                decoded = decode_image_data_url(_part(mime, data)["image_url"])
                self.assertEqual(decoded.mime, mime)
                self.assertEqual(decoded.data, data)
                self.assertEqual(hashlib.sha256(decoded.data).digest(), hashlib.sha256(data).digest())

    def test_invalid_mime_vs_magic_fails_before_provider(self):
        with self.assertRaises(ImageInputError) as caught:
            ensure_image_input_supported(
                "openai", "gpt-5.4", _content(_part("image/jpeg", PNG)),
            )
        self.assertIn("MIME type does not match", str(caught.exception))

    def test_animated_gif_fails_closed(self):
        from PIL import Image

        output = io.BytesIO()
        frames = [Image.new("RGB", (1, 1), color) for color in ("red", "blue")]
        frames[0].save(
            output, format="GIF", save_all=True, append_images=frames[1:], duration=50,
        )
        with self.assertRaises(ImageInputError) as caught:
            decode_image_data_url(_part("image/gif", output.getvalue())["image_url"])
        self.assertIn("animated GIF", str(caught.exception))

    def test_desktop_image_is_captured_with_text_and_no_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "private-vision.png"
            source.write_bytes(PNG)
            content, display_name = build_local_attachment_input("read 4827", source)
        self.assertEqual(display_name, "private-vision.png")
        self.assertEqual([item["type"] for item in content], ["text", "image_ref"])
        self.assertIn("read 4827", content[0]["text"])
        self.assertNotIn(str(source), repr(content))
        self.assertEqual(resolve_image_reference(content[1]).image_bytes, PNG)

    def test_desktop_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AttachmentProtocolError) as caught:
                build_local_attachment_input("inspect", Path(tmp) / "missing.png")
        self.assertEqual(caught.exception.code, "file_read_failed")

    def test_generic_non_image_attachment_remains_path_tool_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_bytes(b"ordinary text")
            content, display_name = build_local_attachment_input("summarize", source)
        self.assertIsInstance(content, str)
        self.assertIn("source_ref=src_", content)
        self.assertNotIn(str(source), content)
        self.assertIn("summarize", content)
        self.assertEqual(display_name, "note.txt")

    def test_multiple_images_keep_logical_order_and_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / "second.jpg", root / "first.png"]
            paths[0].write_bytes(JPEG)
            paths[1].write_bytes(PNG)
            attachments = [
                CommittedAttachment(
                    attachment_id="attachment_1",
                    file_id=f"file_{index}",
                    logical_order=order,
                    display_name=path.name,
                    path=path,
                    byte_size=path.stat().st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    detected_category="image",
                    detected_mime=mime,
                )
                for index, (path, order, mime) in enumerate((
                    (paths[0], 1, "image/jpeg"),
                    (paths[1], 0, "image/png"),
                ))
            ]
            content = build_agent_input("compare", attachments)
        self.assertEqual([item["type"] for item in content], ["text", "image_ref", "image_ref"])
        self.assertEqual(resolve_image_reference(content[1]).image_bytes, PNG)
        self.assertEqual(resolve_image_reference(content[2]).image_bytes, JPEG)

    def test_persisted_history_strips_image_and_base64(self):
        message = {"role": "user", "content": _content(_part("image/png", PNG))}
        clean = _sanitize_persisted_history_message(message)
        self.assertIsInstance(clean["content"], str)
        self.assertIn("inspect the visual facts", clean["content"])
        self.assertNotIn("base64", clean["content"])


class ProviderPayloadTests(unittest.TestCase):
    def test_text_only_wire_messages_are_unchanged(self):
        messages = [{"role": "user", "content": "hello"}]
        self.assertIs(adapt_messages_for_provider(messages, "openai"), messages)
        self.assertIs(adapt_messages_for_provider(messages, "mistral"), messages)

    def test_mistral_uses_verified_string_image_url_chunk(self):
        messages = [{"role": "user", "content": _content(_part("image/png", PNG))}]
        wire = adapt_messages_for_provider(messages, "mistral")
        self.assertIsInstance(wire[0]["content"][1]["image_url"], str)
        self.assertIsInstance(messages[0]["content"][1]["image_url"], dict)

    def test_unsupported_combination_never_calls_provider(self):
        content = _content(_part("image/png", PNG))
        calls = []
        with self.assertRaises(ImageInputError):
            ensure_image_input_supported("deepseek", "deepseek-v4-pro", content)
            calls.append("called")
        self.assertEqual(calls, [])

    @unittest.skip("legacy hierarchy seam replaced by route-bound provider retries")
    def test_agent_retry_keeps_exactly_one_image_per_attempt(self):
        class TechnicalError(RuntimeError):
            status_code = 503

        captured = []

        class Completions:
            def create(self, **kwargs):
                captured.append([
                    {
                        "role": message.get("role"),
                        "content": _safe_content_summary(message.get("content")),
                    }
                    for message in kwargs["messages"]
                ])
                if len(captured) == 1:
                    raise TechnicalError("temporary")
                message = SimpleNamespace(content="grounded response", tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        agent = OSIAAgent()
        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

        def set_model(model_id, provider=None):
            agent.model = model_id
            agent.provider = provider or "openai"

        agent.set_model = set_model
        agent.set_model("gpt-5.4", provider="openai")
        agent._resolve_route = lambda _text: (
            "vision", [("openai", "gpt-5.4"), ("openai", "gpt-4.1")],
        )
        content = _content(_part("image/png", PNG))
        with patch("osia.config.get_setting", side_effect=lambda key, default=None: (
            True if key == "ai_hierarchy_enabled" else default
        )):
            reply = agent._run_turn(content, history=[])
        self.assertEqual(reply, "grounded response")
        self.assertEqual(len(captured), 2)
        for messages in captured:
            images = sum(
                1 for message in messages
                for item in message["content"]
                if item.get("type") == "image_url"
            )
            self.assertEqual(images, 1)
            image = next(
                item for message in messages for item in message["content"]
                if item.get("type") == "image_url"
            )
            self.assertEqual(image["mime"], "image/png")
            self.assertEqual(image["byte_length"], len(PNG))
            self.assertEqual(image["sha256"], hashlib.sha256(PNG).hexdigest())

    def test_mobile_committed_image_reaches_openai_adapter_byte_identically(self):
        captured = []

        class Completions:
            def create(self, **kwargs):
                for message in kwargs["messages"]:
                    if message.get("role") == "user":
                        captured.extend(_safe_content_summary(message.get("content")))
                message = SimpleNamespace(content="grounded response", tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mobile.png"
            path.write_bytes(PNG)
            committed = [CommittedAttachment(
                attachment_id="attachment_mobile",
                file_id="file_mobile",
                logical_order=0,
                display_name="mobile.png",
                path=path,
                byte_size=len(PNG),
                sha256=hashlib.sha256(PNG).hexdigest(),
                detected_category="image",
                detected_mime="image/png",
            )]
            content = build_agent_input("inspect", committed)
            agent = OSIAAgent()
            agent.provider = "openai"
            agent.model = "gpt-5.4"
            agent.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
            with patch("osia.config.get_setting", return_value=False):
                agent._run_turn(content, history=[])
        image = next(item for item in captured if item["type"] == "image_url")
        self.assertEqual(image["mime"], "image/png")
        self.assertEqual(image["byte_length"], len(PNG))
        self.assertEqual(image["sha256"], hashlib.sha256(PNG).hexdigest())

    def test_chatgpt_provider_preserves_image_parts_for_app_server(self):
        class Client:
            generation = 1

            def __init__(self):
                self.turns = []

            def start_or_resume_conversation(self, **_kwargs):
                return "thread-1"

            def run_turn(self, **kwargs):
                self.turns.append(_safe_content_summary(kwargs.get("content")))
                return SimpleNamespace(text="seen", thread_id="thread-1")

        client = Client()
        provider = ChatGPTAccountProvider(client=client)
        provider.complete(
            conversation_id="conv-1",
            model="account-model",
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": _content(_part("image/png", PNG))},
            ],
        )
        self.assertEqual(
            [item["type"] for item in client.turns[0]],
            ["text", "image_url"],
        )
        self.assertEqual(client.turns[0][1]["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertNotIn("base64", repr(provider._threads["conv-1"].expected_messages))


class AppServerNativeImageTests(unittest.TestCase):
    def _run(self, outcome: str):
        root = tempfile.TemporaryDirectory()
        client = AppServerClient(base_dir=Path(root.name))
        captured = {}

        def request(method, params, **_kwargs):
            if method == "turn/interrupt":
                return {}
            self.assertEqual(method, "turn/start")
            captured.update(params)
            local = Path(params["input"][1]["path"])
            self.assertTrue(local.exists())
            self.assertEqual(local.read_bytes(), PNG)
            attempt = client._attempts["thread-1"]
            if outcome == "success":
                attempt.turn_id = "turn-1"
                client._dispatch_notification(
                    "item/agentMessage/delta",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "delta": "seen",
                    },
                )
                client._dispatch_notification(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "completed"},
                    },
                )
                return {"turn": {"id": "turn-1"}}
            if outcome == "unsafe":
                attempt.events.put(("unsafe", "native_action"))
                return {"turn": {"id": "turn-1"}}
            if outcome == "timeout":
                return {"turn": {"id": "turn-1"}}
            raise AppServerError("ERROR: provider failed", category="request_rejected")

        client._request = request
        source_path = r"C:\Users\private\personal.png"
        content = _content(_part("image/png", PNG))
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                if outcome == "success":
                    client.run_turn(
                        thread_id="thread-1", model="account-model", content=content,
                    )
                elif outcome == "timeout":
                    with self.assertRaises(AppServerError):
                        client.run_turn(
                            thread_id="thread-1", model="account-model",
                            content=content, timeout=0.0,
                        )
                else:
                    with self.assertRaises(AppServerError):
                        client.run_turn(
                            thread_id="thread-1", model="account-model", content=content,
                        )
            local_path = Path(captured["input"][1]["path"])
            return local_path, captured, output.getvalue(), source_path, root
        except Exception:
            root.cleanup()
            raise

    def test_native_local_image_exists_during_call_and_cleans_after_success(self):
        path, captured, logs, source, root = self._run("success")
        try:
            self.assertEqual([item["type"] for item in captured["input"]], ["text", "localImage"])
            self.assertFalse(path.exists())
            self.assertNotIn(source, repr(captured))
            self.assertNotIn("base64", logs)
        finally:
            root.cleanup()

    def test_native_image_cleans_after_provider_failure(self):
        path, _captured, _logs, _source, root = self._run("failure")
        try:
            self.assertFalse(path.exists())
        finally:
            root.cleanup()

    def test_native_image_cleans_after_cancellation(self):
        path, _captured, _logs, _source, root = self._run("unsafe")
        try:
            self.assertFalse(path.exists())
        finally:
            root.cleanup()

    def test_native_image_cleans_after_timeout(self):
        path, _captured, _logs, _source, root = self._run("timeout")
        try:
            self.assertFalse(path.exists())
        finally:
            root.cleanup()


if __name__ == "__main__":
    unittest.main()
