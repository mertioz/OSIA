"""WOR-266 native multimodal attachment and history regressions."""

from __future__ import annotations

import io
import hashlib
import json
import types
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from osia.agent import OSIAAgent
from osia.attachments import (
    AttachmentProtocolError,
    CommittedAttachment,
    build_agent_input,
)
from osia.desktop_attachments import (
    DesktopAttachmentError,
    prepare_desktop_turn,
    stage_attachment,
)
from osia.image_inputs import (
    ImageInputError,
    ImageSourceType,
    adapt_messages_for_provider,
    content_with_images,
    create_image_artifact,
    ensure_image_input_supported,
    resolve_image_artifact,
)


MODEL = "gpt-5.5"
PROVIDER = "openai"
PIXEL_TOKEN = "PX7Q9"


def _image_bytes(format_name: str, *, text: str = PIXEL_TOKEN) -> bytes:
    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).text((30, 45), text, fill="black")
    output = io.BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()


def _capture_agent_wire(agent: OSIAAgent, answer: str):
    captured = []

    def stream(self, _callback, *, on_first_token=None, guard=None):
        del on_first_token, guard
        captured.append(self._wire_messages())
        return answer

    agent._stream_with_sentences = types.MethodType(stream, agent)
    return captured


def _active_agent(answer: str = f"The text is {PIXEL_TOKEN}."):
    agent = OSIAAgent(lang="en")
    agent.provider = PROVIDER
    agent.model = MODEL
    # No shared app-server event queue: this is an isolated provider/image-input
    # projection test, so another test's legacy permission/attempt event is not
    # part of this turn.
    from types import SimpleNamespace

    agent.client = SimpleNamespace(
        provider=PROVIDER,
        set_conversation_id=lambda *_args, **_kwargs: None,
        consume_capability_events=lambda: (),
        consume_native_tool_handoff=lambda: {},
    )
    return agent, _capture_agent_wire(agent, answer)


def test_direct_attachment_is_materialized_for_the_selected_model(tmp_path):
    source = tmp_path / "ordinary-upload.png"
    pixels = _image_bytes("PNG")
    source.write_bytes(pixels)
    provider_content = build_agent_input(
        "what text is in this image?",
        [CommittedAttachment(
            attachment_id="att_native_multimodal",
            file_id="file_native_multimodal",
            logical_order=0,
            display_name="ordinary-upload.png",
            path=source,
            byte_size=len(pixels),
            sha256=hashlib.sha256(pixels).hexdigest(),
            detected_category="image",
            detected_mime="image/png",
        )],
        owner_id="conv_native_image",
    )
    safe_request = json.dumps(provider_content, sort_keys=True)
    assert PIXEL_TOKEN not in safe_request
    assert PIXEL_TOKEN not in source.name

    agent = OSIAAgent(lang="en")
    agent.provider = PROVIDER
    agent.model = MODEL
    agent._chatgpt_conversation_id = "conv_native_image"
    agent.messages = [{"role": "user", "content": provider_content}]
    wire = agent._wire_messages()
    source.unlink()

    assert agent.provider == PROVIDER and agent.model == MODEL
    wire_parts = [
        part
        for message in wire
        for part in message.get("content", [])
        if isinstance(message.get("content"), list) and isinstance(part, dict)
    ]
    assert sum(part.get("type") == "image_url" for part in wire_parts) == 1
    user_wire = json.dumps(
        [message for message in wire if message.get("role") == "user"],
        sort_keys=True,
    )
    assert PIXEL_TOKEN not in user_wire
    assert "source_ref" not in user_wire
    assert "query_document" not in user_wire
    assert "open_file" not in user_wire

@pytest.mark.parametrize(
    ("format_name", "suffix", "mime"),
    [
        ("PNG", ".png", "image/png"),
        ("JPEG", ".jpg", "image/jpeg"),
        ("WEBP", ".webp", "image/webp"),
    ],
)

def test_png_jpeg_webp_uploads_share_one_artifact_path(
    tmp_path, format_name, suffix, mime,
):
    source = tmp_path / f"upload{suffix}"
    source.write_bytes(_image_bytes(format_name))
    prepared = prepare_desktop_turn(
        "describe it",
        [stage_attachment(source, 0)],
        provider=PROVIDER,
        model=MODEL,
        owner_id="conv_formats",
    )
    reference = prepared.provider_content[1]
    artifact = resolve_image_artifact(
        reference["image_id"], owner_id="conv_formats",
    )
    assert artifact.mime_type == mime
    assert artifact.width == 320 and artifact.height == 120
    wire = adapt_messages_for_provider(
        [{"role": "user", "content": prepared.provider_content}], PROVIDER,
        owner_id="conv_formats",
    )
    assert wire[0]["content"][1]["image_url"]["url"].startswith(
        f"data:{mime};base64,"
    )


def test_invalid_empty_and_text_only_routes_fail_before_provider(tmp_path):
    with pytest.raises(ImageInputError, match="empty"):
        create_image_artifact(b"", "image/png", ImageSourceType.USER_UPLOAD)

    invalid = tmp_path / "invalid.png"
    invalid.write_bytes(b"\x89PNG\r\n\x1a\nnot-an-image")
    item = stage_attachment(invalid, 0)
    with pytest.raises(DesktopAttachmentError, match="invalid_image"):
        prepare_desktop_turn(
            "describe", [item], provider=PROVIDER, model=MODEL,
        )
    with pytest.raises(AttachmentProtocolError, match="invalid_image"):
        build_agent_input(
            "describe",
            [CommittedAttachment(
                attachment_id="att_invalid_image",
                file_id="file_invalid_image",
                logical_order=0,
                display_name="invalid.png",
                path=invalid,
                byte_size=invalid.stat().st_size,
                sha256=hashlib.sha256(invalid.read_bytes()).hexdigest(),
                detected_category="image",
                detected_mime="image/png",
            )],
        )

    disguised = tmp_path / "disguised.png"
    disguised.write_bytes(b"plain text with an image extension")
    with pytest.raises(DesktopAttachmentError, match="invalid_image"):
        prepare_desktop_turn(
            "describe",
            [stage_attachment(disguised, 0)],
            provider=PROVIDER,
            model=MODEL,
        )

    valid = tmp_path / "valid.png"
    valid.write_bytes(_image_bytes("PNG"))
    with pytest.raises(DesktopAttachmentError, match="unsupported_model"):
        prepare_desktop_turn(
            "describe",
            [stage_attachment(valid, 0)],
            provider="deepseek",
            model="deepseek-v4-pro",
        )
    artifact = create_image_artifact(
        _image_bytes("PNG"),
        "image/png",
        ImageSourceType.USER_UPLOAD,
    )
    direct_content = content_with_images(
        "describe", [artifact], owner_id="conv_direct",
    )
    with pytest.raises(ImageInputError, match="IMAGE_INPUT_UNAVAILABLE"):
        ensure_image_input_supported(
            "deepseek", "deepseek-v4-pro", direct_content,
            owner_id="conv_direct",
        )


def test_multiple_images_and_text_are_one_multimodal_user_message(tmp_path):
    paths = []
    for index, color in enumerate(("black", "blue")):
        image = Image.new("RGB", (40, 30), color)
        path = tmp_path / f"image-{index}.png"
        image.save(path)
        paths.append(path)
    prepared = prepare_desktop_turn(
        "compare these images",
        [stage_attachment(path, index) for index, path in enumerate(paths)],
        provider=PROVIDER,
        model=MODEL,
        owner_id="conv_multiple",
    )
    assert prepared.provider_content[0] == {
        "type": "text", "text": "compare these images",
    }
    assert sum(
        part.get("type") == "image_ref"
        for part in prepared.provider_content
    ) == 2


def test_followup_reuses_ephemeral_image_without_reopening_source(tmp_path):
    source = tmp_path / "followup.png"
    source.write_bytes(_image_bytes("PNG", text="BOTTOM"))
    prepared = prepare_desktop_turn(
        "what is this?",
        [stage_attachment(source, 0)],
        provider=PROVIDER,
        model=MODEL,
        owner_id="conv_followup_image",
    )
    reference = prepared.image_references[0]
    source.unlink()
    history = [
        {
            "role": "user",
            "content": "📎 followup.png\nwhat is this?",
            "_attachments": [{"category": "image", **reference}],
        },
        {"role": "assistant", "content": "It is a screenshot."},
    ]
    agent, captured = _active_agent("There is another detail at the bottom.")
    result = agent.run(
        "and the button at the bottom?",
        history=history,
        on_sentence=lambda _text: None,
        current_conversation_id="conv_followup_image",
    )
    assert "bottom" in result
    assert sum(
        part.get("type") == "image_url"
        for message in captured[0]
        for part in (
            message.get("content")
            if isinstance(message.get("content"), list) else []
        )
    ) == 1


def test_artifact_diagnostics_are_pixel_and_path_free(tmp_path):
    source = tmp_path / "private-name.png"
    source.write_bytes(_image_bytes("PNG"))
    prepared = prepare_desktop_turn(
        "describe",
        [stage_attachment(source, 0)],
        provider=PROVIDER,
        model=MODEL,
        owner_id="conv_private",
    )
    artifact = resolve_image_artifact(
        prepared.image_references[0]["image_id"],
        owner_id="conv_private",
    )
    diagnostic = repr(artifact) + json.dumps(artifact.safe_metadata())
    assert str(tmp_path) not in diagnostic
    assert "base64" not in diagnostic
    assert "image_bytes" not in diagnostic


def test_ephemeral_reference_is_conversation_bound_until_explicitly_shared():
    artifact = create_image_artifact(
        _image_bytes("PNG"),
        "image/png",
        ImageSourceType.USER_UPLOAD,
    )
    content_with_images("first", [artifact], owner_id="conv_owner_a")

    with pytest.raises(ImageInputError, match="another conversation"):
        resolve_image_artifact(artifact.image_id, owner_id="conv_owner_b")

    content_with_images("second", [artifact], owner_id="conv_owner_b")
    assert resolve_image_artifact(
        artifact.image_id, owner_id="conv_owner_a",
    ) is artifact
    assert resolve_image_artifact(
        artifact.image_id, owner_id="conv_owner_b",
    ) is artifact


def test_identical_pixels_on_different_surfaces_keep_distinct_provenance():
    pixels = _image_bytes("PNG")
    first = create_image_artifact(
        pixels,
        "image/png",
        ImageSourceType.DESKTOP_CAPTURE,
        hwnd="101",
        surface_id="surface-a",
        generation="generation-a",
        bounds=(0, 0, 320, 120),
    )
    second = create_image_artifact(
        pixels,
        "image/png",
        ImageSourceType.DESKTOP_CAPTURE,
        hwnd="202",
        surface_id="surface-b",
        generation="generation-b",
        bounds=(400, 0, 720, 120),
    )

    assert first.content_hash == second.content_hash
    assert first.image_id != second.image_id
    assert first.safe_metadata()["surface_id"] == "surface-a"
    assert second.safe_metadata()["surface_id"] == "surface-b"


def test_legacy_upload_with_opaque_extension_is_still_native_image_input(
    tmp_path,
):
    from osia.server import server

    source = tmp_path / "upload.bin"
    source.write_bytes(_image_bytes("PNG"))
    assert server._legacy_attachment_manifest([str(source)])[0][
        "category"
    ] == "image"
    pending = [str(source)]
    artifacts = []

    content = server._consume_pending_files(
        "describe",
        pending,
        owner_id="conv_legacy_native",
        image_artifacts_out=artifacts,
    )

    assert pending == []
    assert len(artifacts) == 1
    assert any(
        isinstance(part, dict) and part.get("type") == "image_ref"
        for part in content
    )
    assert "source_ref" not in json.dumps(content, sort_keys=True)
