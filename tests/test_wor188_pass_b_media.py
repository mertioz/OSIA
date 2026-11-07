"""Focused WOR-188 Pass B media identity, extraction, privacy, and reuse tests."""

from __future__ import annotations

import base64
import io
import json
import math
import tempfile
import wave
from pathlib import Path

import av
import numpy as np
import pytest
from PIL import Image, ImageDraw

from osia import (
    attachments,
    document_artifacts,
    document_reader,
    document_sources,
    media_reader,
)
from osia.document_identity import DocumentIdentityError, identify_document


def _image_bytes(fmt: str, *, size=(96, 48), frames=1, exif=None) -> bytes:
    output = io.BytesIO()
    images = [Image.new("RGB", size, (255, 255 - index * 20, 255)) for index in range(frames)]
    kwargs = {}
    if frames > 1:
        kwargs.update(save_all=True, append_images=images[1:], duration=120, loop=0)
    if exif is not None:
        kwargs["exif"] = exif
    images[0].save(output, format=fmt, **kwargs)
    return output.getvalue()


def _wav_bytes(seconds=1.0, *, sample_rate=16_000, channels=1) -> bytes:
    output = io.BytesIO()
    samples = int(seconds * sample_rate)
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\x00\x00" * samples * channels)
    return output.getvalue()


def _audio_file(path: Path, *, fmt: str, codec: str, seconds=1) -> bytes:
    container = av.open(str(path), mode="w", format=fmt)
    stream = container.add_stream(codec, rate=16_000)
    stream.layout = "mono"
    samples = np.zeros((1, 16_000 * seconds), dtype=np.int16)
    frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
    frame.sample_rate = 16_000
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path.read_bytes()


def _video_file(
    path: Path,
    *,
    fmt="mp4",
    codec="libx264",
    audio=False,
) -> bytes:
    container = av.open(str(path), mode="w", format=fmt)
    video = container.add_stream(codec, rate=2)
    video.width = 160
    video.height = 90
    video.pix_fmt = "yuv420p"
    audio_stream = None
    if audio:
        audio_stream = container.add_stream("aac", rate=16_000)
        audio_stream.layout = "mono"
    for index in range(6):
        pixels = np.zeros((90, 160, 3), dtype=np.uint8)
        pixels[:, :, index % 3] = 80 + index * 20
        frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
        for packet in video.encode(frame):
            container.mux(packet)
    for packet in video.encode():
        container.mux(packet)
    if audio_stream is not None:
        samples = np.zeros((1, 48_000), dtype=np.int16)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = 16_000
        for packet in audio_stream.encode(frame):
            container.mux(packet)
        for packet in audio_stream.encode():
            container.mux(packet)
    container.close()
    return path.read_bytes()


@pytest.fixture
def private_stores(tmp_path):
    artifacts = document_artifacts.ArtifactStore(tmp_path / "artifacts")
    sources = document_sources.DocumentSourceStore(tmp_path / "sources")
    previous_artifacts = document_artifacts.set_store_for_testing(artifacts)
    previous_sources = document_sources.set_source_store_for_testing(sources)
    try:
        yield artifacts, sources
    finally:
        document_artifacts.set_store_for_testing(previous_artifacts)
        document_sources.set_source_store_for_testing(previous_sources)


@pytest.mark.parametrize(
    ("name", "payload", "label", "family"),
    [
        ("photo.jpg", _image_bytes("JPEG"), "jpeg", "image"),
        ("graphic.png", _image_bytes("PNG"), "png", "image"),
        ("graphic.webp", _image_bytes("WEBP"), "webp", "image"),
        ("animation.gif", _image_bytes("GIF", frames=2), "gif", "image"),
        ("scan.tiff", _image_bytes("TIFF", frames=2), "tiff", "image"),
        ("bitmap.bmp", _image_bytes("BMP"), "bmp", "image"),
        ("speech.wav", _wav_bytes(), "wav", "audio"),
    ],
    ids=["jpeg", "png", "webp", "gif", "tiff", "bmp", "wav"],
)
def test_byte_first_identity_image_and_wav(name, payload, label, family):
    identity = identify_document(
        payload, display_name=name, declared_mime="application/octet-stream",
    )
    assert (identity.format_label, identity.family) == (label, family)


def test_byte_first_identity_compressed_audio_and_video(tmp_path):
    fixtures = [
        ("sample.mp3", _audio_file(tmp_path / "sample.mp3", fmt="mp3", codec="mp3"), "mp3", "audio"),
        ("sample.m4a", _audio_file(tmp_path / "sample.m4a", fmt="mp4", codec="aac"), "m4a", "audio"),
        ("sample.aac", _audio_file(tmp_path / "sample.aac", fmt="adts", codec="aac"), "aac", "audio"),
        ("sample.ogg", _audio_file(tmp_path / "sample.ogg", fmt="ogg", codec="libopus"), "ogg", "audio"),
        ("sample.flac", _audio_file(tmp_path / "sample.flac", fmt="flac", codec="flac"), "flac", "audio"),
        ("sample.mp4", _video_file(tmp_path / "sample.mp4"), "mp4", "video"),
        ("sample.mov", _video_file(tmp_path / "sample.mov", fmt="mov"), "mov", "video"),
        ("sample.webm", _video_file(tmp_path / "sample.webm", fmt="webm", codec="libvpx-vp9"), "webm", "video"),
        ("sample.mkv", _video_file(tmp_path / "sample.mkv", fmt="matroska"), "mkv", "video"),
    ]
    for name, payload, label, family in fixtures:
        identity = identify_document(payload, display_name=name)
        assert (identity.format_label, identity.family) == (label, family)


def test_identity_mismatch_corruption_encryption_and_drm():
    mismatch = identify_document(_image_bytes("PNG"), display_name="renamed.jpg")
    assert mismatch.warnings == ("file_identity_mismatch",)
    with pytest.raises(DocumentIdentityError, match="file_identity_mismatch"):
        identify_document(b"not media", display_name="fake.mp4")
    with pytest.raises(DocumentIdentityError, match="corrupted_media"):
        identify_document(b"RIFF\xff\xff\xff\x7fWAVE")
    with pytest.raises(DocumentIdentityError, match="drm_protected"):
        identify_document(b"\x00\x00\x00\x18ftypisom" + b"pssh" + b"\x00" * 8)
    with pytest.raises(DocumentIdentityError, match="encrypted_media"):
        identify_document(b"\x00\x00\x00\x18ftypisom" + b"sinf" + b"\x00" * 8)


def test_image_exif_rotation_multiframe_ocr_and_private_metadata(tmp_path, monkeypatch):
    exif = Image.Exif()
    exif[274] = 6
    exif[271] = "PRIVATE CAMERA"
    rotated = _image_bytes("JPEG", size=(80, 40), exif=exif)
    path = tmp_path / "rotated.jpg"
    path.write_bytes(rotated)
    monkeypatch.setattr(media_reader, "_ocr", lambda _image: ("TEXTE FRANÇAIS", 0.99))
    document = media_reader._extract_image(path, {
        "format_label": "jpeg", "warnings": [],
    })
    assert (document["metadata"]["width"], document["metadata"]["height"]) == (40, 80)
    assert document["ocr_blocks"][0]["text"] == "TEXTE FRANÇAIS"
    assert "PRIVATE CAMERA" not in json.dumps(document["metadata"])
    assert "PRIVATE CAMERA" in json.dumps(document["_private_metadata"])

    gif = tmp_path / "multi.gif"
    gif.write_bytes(_image_bytes("GIF", frames=4))
    animated = media_reader._extract_image(gif, {"format_label": "gif", "warnings": []})
    assert len(animated["frames"]) == 4
    assert animated["metadata"]["frame_durations_ms"] == [120] * 4

    tiff = tmp_path / "multi.tiff"
    tiff.write_bytes(_image_bytes("TIFF", frames=3))
    pages = media_reader._extract_image(tiff, {"format_label": "tiff", "warnings": []})
    assert pages["metadata"]["page_count"] == 3
    assert all(item["kind"] == "page" for item in pages["frames"])


def test_image_resource_limit_and_isolated_worker_cleanup(tmp_path, monkeypatch):
    oversized = tmp_path / "wide.bmp"
    Image.new("RGB", (8193, 1), "white").save(oversized, "BMP")
    monkeypatch.setattr(media_reader, "_ocr", lambda _image: ("", None))
    with pytest.raises(media_reader.MediaReadError, match="resource_limit_exceeded"):
        media_reader._extract_image(oversized, {"format_label": "bmp", "warnings": []})

    payload = _image_bytes("PNG")
    before = set(Path(tempfile.gettempdir()).glob("osia-media-job-*"))
    result = media_reader.extract_media(
        payload, identify_document(payload, display_name="image.png"),
    )
    after = set(Path(tempfile.gettempdir()).glob("osia-media-job-*"))
    assert result["status"] == "complete"
    assert after == before


def test_audio_wav_worker_mp3_ogg_metadata_and_segments(tmp_path, monkeypatch):
    wav = _wav_bytes()
    worker = media_reader.extract_media(
        wav, identify_document(wav, display_name="speech.wav"),
    )
    assert worker["metadata"]["duration_seconds"] == 1.0
    assert worker["tracks"][0]["codec"] == "pcm_s16le"
    assert worker["metadata"]["silence_regions"]
    assert worker["transcript"] == []

    transcript = [{
        "segment_id": "segment_000001", "track_id": "track_000001",
        "ordinal": 0, "start_seconds": 0.2, "end_seconds": 0.8,
        "text": "bonjour", "confidence": 0.9,
    }]
    monkeypatch.setattr(media_reader, "_transcribe", lambda *_args: (
        transcript, {"speech_language": "fr", "language_confidence": 0.95},
    ))
    monkeypatch.setattr(media_reader, "_silence_regions", lambda *_args: ([], False))
    for name, fmt, codec, expected in [
        ("speech.mp3", "mp3", "mp3", "mp3"),
        ("speech.ogg", "ogg", "libopus", "opus"),
    ]:
        path = tmp_path / name
        _audio_file(path, fmt=fmt, codec=codec)
        result = media_reader._extract_audio(path, {
            "format_label": path.suffix[1:], "warnings": [], "language": "fr",
        })
        assert result["tracks"][0]["codec"] == expected
        assert result["transcript"][0]["start_seconds"] == 0.2
        assert result["metadata"]["speech_language"] == "fr"


def test_unsupported_audio_codec_is_typed(tmp_path, monkeypatch):
    path = tmp_path / "unsupported.wav"
    _audio_file(path, fmt="wav", codec="pcm_mulaw")
    monkeypatch.setattr(media_reader, "_transcribe", pytest.fail)
    with pytest.raises(media_reader.MediaReadError, match="unsupported_codec"):
        media_reader._extract_audio(path, {
            "format_label": "wav", "warnings": [], "language": "",
        })


def test_video_mp4_webm_frames_ocr_transcript_and_timeline(tmp_path, monkeypatch):
    execution_order = []

    def fake_ocr(_image):
        execution_order.append("ocr")
        return "ÉCRAN FRANÇAIS", 0.98

    monkeypatch.setattr(media_reader, "_ocr", fake_ocr)
    transcript = [{
        "segment_id": "segment_000001", "track_id": "track_000002",
        "ordinal": 0, "start_seconds": 0.5, "end_seconds": 1.5,
        "text": "audio français", "confidence": 0.91,
    }]
    def fake_transcribe(path, *_args):
        execution_order.append("transcribe")
        assert Path(path).suffix == ".wav"
        assert Path(path).read_bytes().startswith(b"RIFF")
        return transcript, {
            "speech_language": "fr", "language_confidence": 0.97,
        }

    monkeypatch.setattr(media_reader, "_transcribe", fake_transcribe)
    mp4 = tmp_path / "scene.mp4"
    _video_file(mp4, audio=True)
    result = media_reader._extract_video(mp4, {
        "format_label": "mp4", "warnings": [], "language": "fr",
    })
    assert 1 <= len(result["frames"]) <= media_reader.MAX_VIDEO_FRAMES
    assert result["metadata"]["cumulative_frame_pixels"] <= media_reader.MAX_VIDEO_FRAME_PIXELS
    assert result["ocr_blocks"][0]["text"] == "ÉCRAN FRANÇAIS"
    assert result["transcript"][0]["text"] == "audio français"
    assert {item["kind"] for item in result["timeline"]} == {"frame", "ocr", "transcript"}
    assert execution_order[0] == "transcribe"

    webm = tmp_path / "scene.webm"
    data = _video_file(webm, fmt="webm", codec="libvpx-vp9")
    isolated = media_reader.extract_media(
        data, identify_document(data, display_name="scene.webm"),
    )
    assert isolated["format_label"] == "webm"
    assert isolated["frames"]


def test_transcription_failure_keeps_bounded_internal_substage(monkeypatch):
    class BrokenModel:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("private runtime detail")

    monkeypatch.setattr("faster_whisper.WhisperModel", BrokenModel)
    with pytest.raises(media_reader.MediaReadError) as caught:
        media_reader._transcribe(Path("private-input.wav"), "fr", "track_000001")

    assert caught.value.category == "extraction_failure"
    assert caught.value.stage == "transcription_model_load"
    assert "private" not in str(caught.value)


def test_transcription_auto_language_uses_local_detection(monkeypatch):
    constructor_kwargs = []
    transcription_kwargs = []

    class DetectingModel:
        def __init__(self, *_args, **kwargs):
            constructor_kwargs.append(kwargs)

        def transcribe(self, *_args, **kwargs):
            transcription_kwargs.append(kwargs)
            return iter(()), type("Info", (), {
                "language": "fr", "language_probability": 0.9,
            })()

    monkeypatch.setattr("faster_whisper.WhisperModel", DetectingModel)
    transcript, speech = media_reader._transcribe(
        Path("private-input.wav"), "auto", "track_000001",
    )

    assert constructor_kwargs == [{
        "device": "cpu", "compute_type": "int8", "local_files_only": True,
        "cpu_threads": 2, "num_workers": 1,
    }]
    assert transcription_kwargs[0]["language"] is None
    assert transcript == []
    assert speech["speech_language"] == "fr"




def test_media_artifact_queries_are_bounded_opaque_and_deterministic(tmp_path):
    store = document_artifacts.ArtifactStore(tmp_path / "artifacts")
    document = media_reader._base_document("mp4", "video", [])
    document.update({
        "metadata": {"duration_seconds": 2.0, "width": 160, "height": 90},
        "frames": [{
            "frame_id": "frame_000001", "ordinal": 0, "kind": "frame",
            "time_seconds": 0.5, "width": 160, "height": 90,
        }],
        "ocr_blocks": [{
            "ocr_block_id": "ocr_000001", "frame_id": "frame_000001",
            "time_seconds": 0.5, "text": "TEXTE", "confidence": 0.9,
        }],
        "tracks": [{
            "track_id": "track_000001", "ordinal": 0, "kind": "video",
            "codec": "h264", "duration_seconds": 2.0,
        }],
        "transcript": [{
            "segment_id": "segment_000001", "track_id": "track_000001",
            "ordinal": 0, "start_seconds": 0.4, "end_seconds": 1.0,
            "text": "bonjour", "confidence": 0.9,
        }],
        "timeline": [{
            "timeline_id": "timeline_000001", "kind": "transcript",
            "start_seconds": 0.4, "end_seconds": 1.0,
            "segment_id": "segment_000001", "text": "bonjour",
        }],
        "chunks": [{
            "chunk_id": "chunk_000001", "ordinal": 0, "text": "bonjour TEXTE",
            "provenance": [{"frame_id": "frame_000001"}],
        }],
        "supported_queries": [
            "summary", "metadata", "ocr", "frames", "transcript",
            "time_range", "timeline", "tracks", "provenance",
        ],
    })
    with document_artifacts.artifact_access_scope("owner"):
        reference = store.create_v2(document, {
            "source_hash": "a" * 64,
            "source_identity": "src_test",
            "parser_version": "wor188_media_v1",
            "policy_epoch": 1,
            "original_metadata": {"camera": "private"},
        })
        metadata = store.query(reference["artifact_id"], "metadata")
        timeline = store.query(
            reference["artifact_id"], "timeline",
            start_seconds=0.0, end_seconds=1.5,
        )
        frame = store.query(
            reference["artifact_id"], "frames", frame_id="frame_000001",
        )
    encoded = json.dumps([reference, metadata, timeline, frame])
    assert "src_test" not in encoded
    assert "private" not in encoded
    assert reference["frame_count"] == 1
    assert timeline["items"][0]["segment_id"] == "segment_000001"
    assert frame["items"][0]["frame_id"] == "frame_000001"


def test_chat_source_privacy_and_completed_extraction_reuse(
    tmp_path, private_stores, monkeypatch,
):
    _artifacts, sources = private_stores
    path = tmp_path / "photo.png"
    path.write_bytes(_image_bytes("PNG"))
    calls = []
    original = media_reader.extract_media

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(media_reader, "extract_media", counted)
    with document_artifacts.artifact_access_scope("conversation"):
        source = sources.create_for_current_owner(
            path, source_kind="mobile_attachment", display_name="photo.png",
        )
        prompt = (
            "[Document/media attachment: "
            f"source_ref={source['source_ref']} type={source['type_label']}]"
        )
        first = json.loads(document_reader.read_document(source_ref=source["source_ref"]))
        second = json.loads(document_reader.read_document(source_ref=source["source_ref"]))
    assert str(path) not in prompt
    assert base64.b64encode(path.read_bytes()).decode()[:32] not in prompt
    assert first["artifact_id"] == second["artifact_id"]
    assert len(calls) == 1




def test_microphone_strict_bounded_base64_and_exact_wav():
    from osia.server.server import decode_base64_wav

    raw = _wav_bytes(seconds=0.25)
    audio, rate = decode_base64_wav(base64.b64encode(raw).decode("ascii"))
    assert rate == 16_000
    assert audio.dtype == np.float32
    with pytest.raises(ValueError, match="invalid_audio_encoding"):
        decode_base64_wav("%%%")
    malformed = bytearray(raw)
    malformed[4:8] = (1).to_bytes(4, "little")
    with pytest.raises(ValueError, match="invalid_wav"):
        decode_base64_wav(base64.b64encode(malformed).decode("ascii"))
    too_long = _wav_bytes(seconds=61)
    with pytest.raises(ValueError, match="audio_duration_limit"):
        decode_base64_wav(base64.b64encode(too_long).decode("ascii"))


def test_screenshot_uses_local_artifact_path_without_vision_export(
    tmp_path, private_stores, monkeypatch,
):
    from osia import agent

    def capture(path):
        Image.new("RGB", (32, 16), "white").save(path, "PNG")
        return None

    monkeypatch.setattr(agent, "_capture_screen", capture)
    assert not hasattr(agent, "_describe_screenshot")
    with document_artifacts.artifact_access_scope("screen"):
        result = json.loads(agent.tool_take_screenshot())
    assert result["document_family"] == "image"
    assert result["supported_queries"]


def test_optional_provider_export_requires_capability_exact_permission_and_reencode(monkeypatch):
    from osia import image_inputs

    monkeypatch.setattr(
        image_inputs,
        "image_input_decision",
        lambda *_args: image_inputs.ImageInputDecision(
            provider="configured", model="vision", model_known=True,
            model_supports_vision=True, adapter_supports_images=True,
            account_path_permits_images=True, formats_accepted=True,
            encoding="openai_image_url", usable=True, reason="supported",
        ),
    )
    base = {
        "exact_source_authorized": True,
        "media_export_authorized": True,
        "canonical_reencoded": True,
        "same_provider": True,
    }
    assert image_inputs.media_export_decision(
        "configured", "vision", "image/png", **base,
    ).usable
    for key in base:
        values = dict(base)
        values[key] = False
        decision = image_inputs.media_export_decision(
            "configured", "vision", "image/png", **values,
        )
        assert decision.usable is False


def test_attachment_labels_are_byte_proved_and_agent_input_is_opaque(
    tmp_path, private_stores,
):
    path = tmp_path / "voice.bin"
    path.write_bytes(_wav_bytes())
    category, mime = attachments.detect_type(path, "misleading.bin")
    assert (category, mime) == ("audio", "audio/wav")
    committed = attachments.CommittedAttachment(
        attachment_id="att_" + "a" * 32,
        file_id="file_test",
        logical_order=0,
        display_name="voice.wav",
        path=path,
        byte_size=path.stat().st_size,
        sha256="b" * 64,
        detected_category=category,
        detected_mime=mime,
    )
    with document_artifacts.artifact_access_scope("conversation"):
        prompt = attachments.build_agent_input(
            "analyse", [committed], owner_id="conversation",
        )
    assert "source_ref=src_" in prompt
    assert str(path) not in prompt
    assert committed.sha256 not in prompt


def test_media_formats_explicitly_refuse_heic_svg_and_truncated_jpeg():
    with pytest.raises(DocumentIdentityError, match="unsupported_media"):
        identify_document(
            b"\x00\x00\x00\x18ftypheic" + b"\x00" * 12,
            display_name="photo.heic",
        )
    identity = identify_document(
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        display_name="drawing.svg",
    )
    assert identity.family == "document"
    assert identity.format_label not in media_reader.IMAGE_FORMATS
    with pytest.raises(media_reader.MediaReadError, match="corrupted_media"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.jpg"
            path.write_bytes(b"\xff\xd8\xff\xe0")
            media_reader._extract_image(path, {"format_label": "jpeg", "warnings": []})


def test_windows_package_collects_offline_media_dependencies_and_worker_smoke():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "packaging" / "windows" / "osia.spec").read_text("utf-8")
    launcher = (root / "packaging" / "windows" / "launcher.py").read_text("utf-8")
    for dependency in ("av", "faster_whisper", "PIL", "rapidocr", "soundfile"):
        assert f'"{dependency}"' in spec
    assert "MEDIA_WORKER_MODE" in launcher
    assert "run_packaged_media_smoke" in launcher
    assert "verify_media_dependencies" in launcher


def test_read_document_outer_timeout_contains_media_worker_envelope():
    from osia import agent

    assert agent._tool_timeout_seconds("read_document", {}) > (
        media_reader.VIDEO_TIMEOUT_SECONDS + 6
    )
    assert agent._tool_timeout_seconds("query_document", {}) == 40
