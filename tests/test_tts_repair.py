"""Opt-in desktop speech and one shared, offline-capable local speech engine."""
from pathlib import Path
import io
import json
import sys
import types

import numpy as np
import pytest
import soundfile as sf

from osia import config
from osia.voice import tts


def test_desktop_speech_is_off_without_an_explicit_preference(monkeypatch, tmp_path):
    assert config.DEFAULT_SETTINGS["desktop_speech_enabled"] is False
    example = json.loads((Path(__file__).resolve().parents[1]/"config/settings.example.json").read_text())
    assert example["desktop_speech_enabled"] is False
    settings = {}
    monkeypatch.setattr(tts, "get_setting", lambda name, default=None: settings.get(name, default))
    assert not tts.desktop_speech_enabled()
    settings["desktop_speech_enabled"] = True
    assert tts.desktop_speech_enabled()
    settings["desktop_speech_enabled"] = False
    assert not tts.desktop_speech_enabled()


@pytest.mark.parametrize("text,expected", [
    ("Salut !", "fr"), ("Ça va ?", "fr"), ("Oui.", "fr"),
    ("Oui, ça va bien, merci !", "fr"), ("Bonjour.", "fr"),
    ("Hello.", "en"), ("Thank you.", "en"),
    ("La vérification est terminée.", "fr"),
])
def test_short_replies_select_a_real_packaged_voice_without_rewriting_text(text, expected):
    assert tts._authoritative_tts_language(None, text) == expected


def test_explicit_and_unambiguous_unsupported_locales_are_not_reinterpreted():
    assert tts._authoritative_tts_language("it", "Hello.") == "it"
    assert tts._authoritative_tts_language(None, "La verifica è terminata.") == "it"
    assert tts._authoritative_tts_language(None, "こんにちは。") == "ja"


def test_source_pipelines_share_one_model_across_languages(monkeypatch):
    calls = []
    class Pipeline:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.model = kwargs.get("model", object())
    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=Pipeline, KModel=object))
    monkeypatch.setattr(tts, "_kokoro_model", None)
    monkeypatch.setattr(tts, "_kokoro_pipelines", {})
    monkeypatch.setattr(tts, "_patch_windows_espeak_data_path", lambda: None)
    monkeypatch.setattr(tts, "bundled_model_path", lambda name: None)
    french = tts._get_kokoro_pipeline("f")
    english = tts._get_kokoro_pipeline("a")
    assert french.model is english.model
    assert calls[1]["model"] is french.model
    assert tts._get_kokoro_pipeline("f") is french
    assert len(calls) == 2


def test_desktop_mobile_and_buffer_use_identical_local_synthesis(monkeypatch):
    from osia.server import server
    calls = []
    def pipeline(text, **kwargs):
        calls.append((text, kwargs))
        yield text, "phonemes", np.ones(2400, np.float32) * 0.1
    monkeypatch.setattr(tts, "_get_kokoro_pipeline", lambda code: pipeline)
    monkeypatch.setattr(tts, "get_setting", lambda key, default=None: True if key == "desktop_speech_enabled" else default)
    monkeypatch.setattr(tts, "_play_samples_if_current", lambda *a, **kw: True)
    monkeypatch.setattr(tts, "sd", types.SimpleNamespace(stop=lambda: None, wait=lambda: None))
    monkeypatch.setattr(tts, "_log_audio_device", lambda: None)
    tts.start_tts_session()
    try:
        assert tts.speak_kokoro("Ça va ?")
        samples, sr = tts._synthesize_to_buffer("Ça va ?", "local", None)
        monkeypatch.setattr(server, "_wav_to_mp3", lambda data: data)
        audio = server._kokoro_to_mp3_bytes("Ça va ?")
        wave, wave_sr = sf.read(io.BytesIO(audio))
        assert sr == wave_sr == 24000
        np.testing.assert_allclose(samples, wave, atol=1/32768)
        assert len(calls) == 3
        assert all(text == "Ça va ?" and options["voice"] == "ff_siwis" for text, options in calls)
    finally:
        tts.stop()
