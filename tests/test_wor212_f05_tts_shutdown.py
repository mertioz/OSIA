"""Deterministic WOR-212 F05 TTS ownership and shutdown regressions."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from osia import main
from osia.voice import tts


class _FakeSoundDevice:
    def __init__(self) -> None:
        self.play_calls: list[tuple[object, int]] = []
        self.stop_calls = 0
        self.play_started = threading.Event()
        self.wait_release = threading.Event()
        self.wait_release.set()
        self.default = SimpleNamespace(device=(None, 0))

    def query_devices(self, *, kind: str):
        assert kind == "output"
        return {"name": "WOR-212 fake output", "default_samplerate": 24_000}

    def play(self, samples, *, samplerate: int) -> None:
        self.play_calls.append((samples, samplerate))
        self.play_started.set()

    def wait(self) -> None:
        self.wait_release.wait(timeout=1)

    def stop(self) -> None:
        self.stop_calls += 1
        self.wait_release.set()


@pytest.fixture(autouse=True)
def _isolated_tts_state(monkeypatch):
    fake = _FakeSoundDevice()
    monkeypatch.setattr(tts, "sd", fake)
    monkeypatch.setattr(tts, "_AUDIO_OK", True)
    monkeypatch.setattr(tts, "_audio_device_logged", False)
    tts.start_tts_session()
    yield fake
    tts.shutdown(join_timeout=0.2)


def _delayed_playback(fake: _FakeSoundDevice):
    started = threading.Event()
    release = threading.Event()

    def synthesize_then_play(*_args) -> None:
        started.set()
        release.wait(timeout=1)
        if tts._play_samples_if_current(np.ones(4), 24_000):
            fake.wait()

    return started, release, synthesize_then_play


def test_delayed_direct_greeting_is_discarded_after_stop(
    _isolated_tts_state, monkeypatch,
) -> None:
    fake = _isolated_tts_state
    started = threading.Event()
    release = threading.Event()

    def delayed_synthesis(*_args):
        started.set()
        release.wait(timeout=1)
        return np.ones(4), 24_000

    monkeypatch.setattr(tts, "_synthesize_to_buffer", delayed_synthesis)
    monkeypatch.setattr(tts.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(tts.os, "makedirs", lambda *_a, **_kw: None)
    monkeypatch.setattr(tts.sf, "write", lambda *_a, **_kw: None)
    monkeypatch.setattr(tts.os, "replace", lambda *_a, **_kw: None)
    session = tts.start_tts_session()
    worker = threading.Thread(
        target=tts.speak_greeting_cached,
        args=("Synthetic greeting",),
        kwargs={"session_id": session},
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=1)

    tts.stop()
    release.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert fake.play_calls == []
    assert tts._playback_activity == 0


@pytest.mark.parametrize(
    "message",
    ("Synthetic not-understood fallback", "Synthetic provider error"),
)
def test_delayed_direct_fallback_or_error_is_discarded_after_stop(
    _isolated_tts_state, message,
) -> None:
    fake = _isolated_tts_state
    started, release, delayed = _delayed_playback(fake)
    session = tts.start_tts_session()
    with patch.object(tts, "_speak_blocking", side_effect=delayed):
        worker = threading.Thread(
            target=tts.speak,
            args=(message,),
            kwargs={"session_id": session},
            daemon=True,
        )
        worker.start()
        assert started.wait(timeout=1)
        tts.stop()
        release.set()
        worker.join(timeout=1)

    assert fake.play_calls == []
    assert tts._playback_activity == 0


def test_stale_result_is_rejected_at_atomic_playback_boundary(
    _isolated_tts_state,
) -> None:
    fake = _isolated_tts_state
    stale_session = tts.start_tts_session()
    tts.stop()

    assert not tts._play_samples_if_current(
        np.ones(4), 24_000, session_id=stale_session,
    )
    assert fake.play_calls == []


def test_valid_current_non_streaming_session_plays_once(
    _isolated_tts_state,
) -> None:
    fake = _isolated_tts_state
    session = tts.start_tts_session()

    def play_current(*_args) -> None:
        assert tts._play_samples_if_current(np.ones(4), 24_000)
        fake.wait()

    with patch.object(tts, "_speak_blocking", side_effect=play_current):
        tts.speak("Synthetic current speech", session_id=session)

    assert len(fake.play_calls) == 1
    assert not tts.playback_active(session)


def test_streaming_stale_segment_is_rejected_and_current_fifo_is_preserved(
    _isolated_tts_state,
) -> None:
    spoken: list[str] = []
    stale = tts.start_streaming_session()
    tts.stop()
    with patch.object(tts, "_speak_blocking", side_effect=spoken.append):
        tts.speak_streaming("stale", session_id=stale)
        current = tts.start_streaming_session()
        tts.speak_streaming("first", session_id=current)
        tts.speak_streaming("second", session_id=current)
        assert tts.wait_for_playback_complete(
            timeout=1, session_id=current,
        )

    assert spoken == ["first", "second"]


def test_stopping_old_speech_cannot_drain_a_new_sessions_queue(monkeypatch):
    draining = threading.Event()
    release = threading.Event()
    queued = threading.Event()
    get_nowait = tts._stream_queue.get_nowait
    session_ids = []

    def pause_old_drain():
        if threading.current_thread() is stopping:
            draining.set()
            assert release.wait(timeout=2)
        return get_nowait()

    def enqueue_new_session():
        session = tts.start_tts_session()
        session_ids.append(session)
        tts.speak_streaming("New speech", session_id=session)
        queued.set()

    stopping = threading.Thread(target=tts.stop, daemon=True)
    starting = threading.Thread(target=enqueue_new_session, daemon=True)
    with monkeypatch.context() as patcher:
        patcher.setattr(tts._stream_queue, "get_nowait", pause_old_drain)
        patcher.setattr(tts, "_stream_thread", SimpleNamespace(is_alive=lambda: True))
        stopping.start()
        try:
            assert draining.wait(timeout=1)
            starting.start()
            assert not queued.wait(timeout=0.1)
        finally:
            release.set()
            stopping.join(timeout=2)
            if starting.ident is not None:
                starting.join(timeout=2)
        assert queued.is_set()
        assert get_nowait() == (session_ids[0], "New speech", None)


def test_stop_during_active_playback_releases_waiter_and_counters(
    _isolated_tts_state,
) -> None:
    fake = _isolated_tts_state
    session = tts.start_tts_session()
    fake.wait_release.clear()
    waiter_done = threading.Event()
    waiter_result: list[bool] = []

    def blocking_play(*_args) -> None:
        assert tts._play_samples_if_current(np.ones(4), 24_000)
        fake.wait()

    with patch.object(tts, "_speak_blocking", side_effect=blocking_play):
        worker = threading.Thread(
            target=tts.speak,
            args=("Synthetic active speech",),
            kwargs={"session_id": session},
            daemon=True,
        )
        worker.start()
        assert fake.play_started.wait(timeout=1)

        def wait_for_completion() -> None:
            waiter_result.append(
                tts.wait_for_playback_complete(timeout=1, session_id=session)
            )
            waiter_done.set()

        waiter = threading.Thread(target=wait_for_completion, daemon=True)
        waiter.start()
        assert not waiter_done.wait(timeout=0.02)
        tts.stop()
        assert waiter_done.wait(timeout=0.5)
        worker.join(timeout=1)
        waiter.join(timeout=1)

    assert fake.stop_calls >= 1
    assert waiter_result == [True]
    assert tts._playback_activity == 0
    assert tts._stream_pending == 0


def test_stop_during_synthesis_discards_result_without_counter_leak(
    _isolated_tts_state,
) -> None:
    fake = _isolated_tts_state
    started, release, delayed = _delayed_playback(fake)
    session = tts.start_tts_session()
    with patch.object(tts, "_speak_blocking", side_effect=delayed):
        worker = threading.Thread(
            target=tts.speak,
            args=("Synthetic slow synthesis",),
            kwargs={"session_id": session},
            daemon=True,
        )
        worker.start()
        assert started.wait(timeout=1)
        tts.stop()
        assert tts.wait_for_playback_complete(timeout=0.1, session_id=session)
        release.set()
        worker.join(timeout=1)

    assert fake.play_calls == []
    assert tts._playback_activity == 0
    assert tts._stream_pending == 0


def test_shutdown_orders_cancellation_before_tts_and_is_bounded(
    _isolated_tts_state,
) -> None:
    fake = _isolated_tts_state
    order: list[str] = []
    started, release, delayed = _delayed_playback(fake)
    session = tts.start_tts_session()
    with patch.object(tts, "_speak_blocking", side_effect=delayed):
        voice_worker = threading.Thread(
            target=tts.speak,
            args=("Synthetic shutdown speech",),
            kwargs={"session_id": session},
            daemon=True,
        )
        voice_worker.start()
        assert started.wait(timeout=1)
        before = time.monotonic()
        stopped = main._shutdown_voice_runtime(
            close_acceptance=lambda: order.append("close_acceptance"),
            cancel_pending_activation=lambda: order.append("cancel_pending"),
            signal_worker_cancel=lambda: order.append("cancel_worker"),
            invalidate_tts=lambda: (order.append("invalidate_tts"), tts.stop()),
            get_active_worker=lambda: voice_worker,
            stop_before_tts_shutdown=(lambda: order.append("stop_background"),),
            shutdown_tts=lambda: (
                order.append("tts_shutdown"),
                tts.shutdown(join_timeout=0.02),
            ),
            release_voice_gate=lambda: order.append("release_gate"),
            join_timeout=0.02,
        )
        elapsed = time.monotonic() - before
        assert stopped is False
        assert elapsed < 0.25
        release.set()
        voice_worker.join(timeout=1)

    assert order == [
        "close_acceptance",
        "cancel_pending",
        "cancel_worker",
        "invalidate_tts",
        "stop_background",
        "tts_shutdown",
        "release_gate",
    ]
    assert fake.play_calls == []


def test_new_session_after_stop_plays_and_obsolete_session_stays_rejected(
    _isolated_tts_state,
) -> None:
    fake = _isolated_tts_state
    obsolete = tts.start_tts_session()
    tts.stop()
    current = tts.start_tts_session()

    assert not tts._play_samples_if_current(
        np.ones(4), 24_000, session_id=obsolete,
    )

    def play_current(*_args) -> None:
        assert tts._play_samples_if_current(np.ones(4), 24_000)

    with patch.object(tts, "_speak_blocking", side_effect=play_current):
        tts.speak("Synthetic replacement speech", session_id=current)

    assert len(fake.play_calls) == 1


def test_wake_gate_close_releases_active_owner_once_and_refuses_reopen() -> None:
    handler_lock = threading.Lock()
    from osia.voice.wake import WakeAcceptanceGate

    gate = WakeAcceptanceGate(handler_lock)
    assert gate.reserve(0)
    assert gate.consume(0, is_current=True)
    gate.close()
    gate.finish()

    assert not handler_lock.locked()
    assert not gate.reserve(0)
