from __future__ import annotations

import inspect
from types import SimpleNamespace

import numpy as np

from osia.voice import commands, stt


class _FakeInputStream:
    def __init__(self, owner, amplitudes):
        self._owner = owner
        self._amplitudes = iter(amplitudes)

    def __enter__(self):
        self._owner.active_streams += 1
        self._owner.max_active_streams = max(
            self._owner.max_active_streams,
            self._owner.active_streams,
        )
        self._owner.events.append("stream_enter")
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self._owner.events.append("stream_exit")
        self._owner.active_streams -= 1

    def read(self, chunk_size):
        self._owner.events.append("read")
        amplitude = next(self._amplitudes, 0.0)
        return np.full((chunk_size, 1), amplitude, dtype=np.float32), False


class _FakeSoundDevice:
    def __init__(self, stream_plans):
        self.stream_plans = list(stream_plans)
        self.events = []
        self.calibration_calls = 0
        self.stream_calls = 0
        self.active_streams = 0
        self.max_active_streams = 0

    def rec(self, n_samples, **_kwargs):
        self.calibration_calls += 1
        self.events.append("calibration_rec")
        return np.full((n_samples, 1), 0.002, dtype=np.float32)

    def wait(self):
        self.events.append("calibration_wait")

    def InputStream(self, **_kwargs):
        plan = self.stream_plans[self.stream_calls]
        self.stream_calls += 1
        return _FakeInputStream(self, plan)


class _FakeWhisperModel:
    def __init__(self, plans):
        self._plans = iter(plans)
        self.calls = 0
        self.active_calls = 0
        self.max_active_calls = 0
        self.audio_inputs = []
        self.kwargs = []

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        self.audio_inputs.append(audio.copy())
        self.kwargs.append(kwargs)
        segments, info = next(self._plans)

        def _iter_segments():
            try:
                yield from segments
            finally:
                self.active_calls -= 1

        return _iter_segments(), info


def _command_chunks(amplitude=0.2):
    return [amplitude] * 6 + [0.0] * 2


def _segment(
    text,
    *,
    avg_logprob=-0.2,
    no_speech_prob=0.05,
    compression_ratio=1.1,
):
    return SimpleNamespace(
        text=text,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
        compression_ratio=compression_ratio,
    )


def _info(language="fr", language_probability=0.98):
    return SimpleNamespace(
        language=language,
        language_probability=language_probability,
        transcription_options=SimpleNamespace(
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
        ),
    )


def _plan(text, **segment_kwargs):
    language = segment_kwargs.pop("language", "fr")
    language_probability = segment_kwargs.pop("language_probability", 0.98)
    segments = [] if text is None else [_segment(text, **segment_kwargs)]
    return segments, _info(language, language_probability)


def _install_fakes(monkeypatch, transcript_plans, *, attempts=None):
    attempts = attempts or len(transcript_plans)
    fake_sd = _FakeSoundDevice([_command_chunks()] * attempts)
    fake_model = _FakeWhisperModel(transcript_plans)
    monkeypatch.setattr(stt, "sd", fake_sd)
    monkeypatch.setattr(stt, "_AUDIO_OK", True)
    monkeypatch.setattr(stt, "_calibrated_silence_rms", None)
    monkeypatch.setattr(stt, "_get_model", lambda _size=None: (fake_model, "small"))
    monkeypatch.setattr(
        stt,
        "get_setting",
        lambda key, default=None: "local" if key == "stt_provider" else default,
    )
    return fake_sd, fake_model


def _run_downstream(text, calls):
    if not text:
        return
    calls["conversation"].append(text)
    if commands.is_new_chat_command(text):
        calls["local_command"].append(text)
        return
    calls["agent"].append(text)
    calls["tool"].append(text)


def _side_effect_calls():
    return {
        "conversation": [],
        "local_command": [],
        "agent": [],
        "tool": [],
    }


def test_strong_french_result_is_preserved_and_reaches_downstream_once(monkeypatch):
    expected = "Réponds simplement bonjour."
    _fake_sd, fake_model = _install_fakes(
        monkeypatch,
        [_plan(expected), _plan(expected)],
    )
    downstream = []

    text = stt.listen_for_command(max_retries=1)
    if text:
        downstream.append(text)

    assert text == expected
    assert downstream == [expected]
    result = stt._transcribe_local_result(np.ones(32, dtype=np.float32))
    assert result.language == "fr"
    assert result.language_probability == 0.98
    assert result.segment_count == 1
    assert result.accepted
    assert fake_model.calls == 2


def test_strong_english_result_is_accepted_without_french_bias(monkeypatch):
    expected = "Open the browser please."
    _install_fakes(
        monkeypatch,
        [_plan(expected, language="en", language_probability=0.82)],
    )

    assert stt.listen_for_command(max_retries=1) == expected


def test_strong_short_command_is_not_rejected_for_word_count(monkeypatch):
    _install_fakes(monkeypatch, [_plan("Bonjour")])

    assert stt.listen_for_command(max_retries=1) == "Bonjour"


def test_empty_result_retries_and_never_reaches_downstream(monkeypatch):
    fake_sd, fake_model = _install_fakes(
        monkeypatch,
        [_plan(None), _plan(None)],
    )
    calls = _side_effect_calls()

    text = stt.listen_for_command(max_retries=2)
    _run_downstream(text, calls)

    assert text == ""
    assert fake_model.calls == 2
    assert fake_sd.stream_calls == 2
    assert all(not values for values in calls.values())


def test_weak_low_probability_result_is_rejected_before_any_turn(monkeypatch):
    weak = "commande pourtant assez longue"
    _install_fakes(monkeypatch, [_plan(weak, avg_logprob=-1.01)])
    provider_calls = []
    monkeypatch.setattr(
        stt,
        "get_setting",
        lambda key, default=None: {
            "stt_provider": "local",
            "stt_fallback_provider": "groq",
        }.get(key, default),
    )
    monkeypatch.setattr(
        stt,
        "_transcribe_groq",
        lambda *_args, **_kwargs: provider_calls.append("groq") or "guessed text",
    )
    calls = _side_effect_calls()

    text = stt.listen_for_command(max_retries=1)
    _run_downstream(text, calls)

    assert text == ""
    assert provider_calls == []
    assert all(not values for values in calls.values())


def test_high_no_speech_and_weak_log_probability_are_rejected(monkeypatch):
    _install_fakes(
        monkeypatch,
        [_plan("texte incertain ici", avg_logprob=-1.2, no_speech_prob=0.61)],
    )

    result = stt._transcribe_result(
        np.ones(32, dtype=np.float32),
        stt.WHISPER_SAMPLE_RATE,
        None,
        None,
    )

    assert not result.accepted
    assert result.rejection_reasons == (
        "low_average_log_probability",
        "probable_no_speech",
    )


def test_degenerate_repetition_is_rejected_by_compression_metadata(monkeypatch):
    repeated = "la la la la la la la la"
    _install_fakes(monkeypatch, [_plan(repeated, compression_ratio=2.41)])

    result = stt._transcribe_result(
        np.ones(32, dtype=np.float32),
        stt.WHISPER_SAMPLE_RATE,
        None,
        None,
    )

    assert not result.accepted
    assert result.rejection_reasons == ("degenerate_compression_ratio",)


def test_weak_then_strong_retries_once_without_recalibration_or_overlap(monkeypatch):
    accepted = "commande claire au second essai"
    fake_sd, fake_model = _install_fakes(
        monkeypatch,
        [
            _plan("première commande incertaine", avg_logprob=-1.2),
            _plan(accepted),
        ],
    )
    ready = []

    text = stt.listen_for_command(
        max_retries=3,
        on_ready=lambda: ready.append("ready"),
    )

    assert text == accepted
    assert fake_sd.calibration_calls == 1
    assert fake_sd.stream_calls == 2
    assert fake_sd.max_active_streams == 1
    assert fake_model.max_active_calls == 1
    assert ready == ["ready"]


def test_three_rejected_attempts_use_existing_empty_fallback_and_no_side_effects(
    monkeypatch,
):
    plans = [
        _plan("faible tentative une", avg_logprob=-1.2),
        _plan("la la la la la la", compression_ratio=2.5),
        _plan(None),
    ]
    fake_sd, fake_model = _install_fakes(monkeypatch, plans)
    calls = _side_effect_calls()

    text = stt.listen_for_command(max_retries=3)
    _run_downstream(text, calls)

    assert text == ""
    assert fake_model.calls == 3
    assert fake_sd.stream_calls == 3
    assert all(not values for values in calls.values())

    from osia import main

    source = inspect.getsource(main.wake_mode)
    rejection_guard = source.index("if not user_text:")
    assert rejection_guard < source.index("is_new_chat_command(user_text)")
    assert rejection_guard < source.index("_ensure_active_conversation(user_text)")
    assert rejection_guard < source.index("_run_agent(user_text")


def test_weak_new_chat_text_never_reaches_local_command(monkeypatch):
    _install_fakes(monkeypatch, [_plan("nouveau chat", avg_logprob=-1.3)])
    calls = _side_effect_calls()

    text = stt.listen_for_command(max_retries=1)
    _run_downstream(text, calls)

    assert text == ""
    assert calls["local_command"] == []
    assert calls["conversation"] == []


def test_weak_action_like_text_never_reaches_agent_or_tool(monkeypatch, capsys):
    weak = "Open the browser right now"
    _install_fakes(
        monkeypatch,
        [_plan(weak, language="en", avg_logprob=-1.4)],
    )
    calls = _side_effect_calls()

    text = stt.listen_for_command(max_retries=1)
    _run_downstream(text, calls)

    assert text == ""
    assert calls["agent"] == []
    assert calls["tool"] == []
    assert weak not in capsys.readouterr().out


def test_f02_ready_boundary_and_first_retained_sample_remain_unchanged(monkeypatch):
    fake_sd, fake_model = _install_fakes(
        monkeypatch,
        [_plan("commande claire conservée")],
    )
    ready = []

    text = stt.listen_for_command(
        max_retries=1,
        on_ready=lambda: (ready.append("ready"), fake_sd.events.append("ready")),
    )

    assert text == "commande claire conservée"
    assert ready == ["ready"]
    assert fake_sd.events.index("calibration_wait") < fake_sd.events.index("ready")
    assert fake_sd.events.index("stream_enter") < fake_sd.events.index("ready")
    assert fake_sd.events.index("ready") < fake_sd.events.index("read")
    assert fake_model.audio_inputs[0][0] == np.float32(0.2)
    assert not np.any(np.isclose(fake_model.audio_inputs[0], 0.002))
