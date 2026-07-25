import asyncio
import base64
import threading
import time
import unittest
from unittest.mock import patch

from osia.server import server


def _meta(suffix: str = "one", *, tts_enabled: bool = True) -> dict:
    return {
        "conversation_id": f"wor55-conversation-{suffix}",
        "request_id": f"wor55-request-{suffix}",
        "client_request_id": f"wor55-request-{suffix}",
        "turn_id": f"wor55-turn-{suffix}",
        "user_message_id": f"wor55-user-{suffix}",
        "assistant_message_id": f"wor55-assistant-{suffix}",
        "_event_sequence": 0,
        "persisted": False,
        "tts_enabled": tts_enabled,
    }


class _Channel:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.first_audio = asyncio.Event()

    async def send_json(self, frame) -> None:
        copied = dict(frame)
        self.frames.append(copied)
        if copied.get("type") == "audio":
            self.first_audio.set()


class _Agent:
    model = "wor55-model"
    provider = "wor55-provider"
    _applied_selected_model = None
    _in_use = False

    def __init__(self, run_impl, final_text: str) -> None:
        self._run_impl = run_impl
        self._final_text = final_text

    def run(self, text, *, history, on_sentence, on_first_token, **_kwargs):
        return self._run_impl(on_sentence, on_first_token)

    def exported_history(self) -> list[dict]:
        return [
            {"role": "user", "content": "synthetic user"},
            {"role": "assistant", "content": self._final_text},
        ]


class ProgressiveTTSSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        tasks = list(server._POST_TURN_MEDIA_TASKS)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        server._POST_TURN_MEDIA_TASKS.clear()
        server._POST_TURN_MEDIA_BY_CONV.clear()

    async def test_first_sentence_precedes_final_and_tail_is_exactly_once(self) -> None:
        channel = _Channel()
        calls: list[str] = []

        def synthesize(text: str) -> bytes:
            calls.append(text)
            time.sleep(0.005)
            return text.encode("utf-8")

        session = server._ResponseTTSSession(
            channel,
            _meta(),
            time.perf_counter(),
            synthesize=synthesize,
        )
        task = session.start()
        session.enqueue("First sentence.")
        await asyncio.wait_for(channel.first_audio.wait(), timeout=1)
        self.assertFalse(session._input_finished)

        session.enqueue("Second sentence.")
        session.finish_input("First sentence. Second sentence. Unfinished tail")
        session.mark_text_final_sent()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(
            calls,
            ["First sentence.", "Second sentence.", "Unfinished tail"],
        )
        audio = [frame for frame in channel.frames if frame["type"] == "audio"]
        self.assertEqual(len(audio), 3)
        self.assertEqual([frame["media_terminal"] for frame in audio], [False, False, True])
        decoded = [base64.b64decode(frame["data"]).decode() for frame in audio]
        self.assertEqual(decoded, calls)
        self.assertEqual([seq for seq, _text in session.queued], [1, 2, 3])
        self.assertEqual([item[0] for item in session.delivered], [1, 2, 3])

    async def test_short_and_unpunctuated_answers_flush_at_final(self) -> None:
        for index, text in enumerate(("Yes.", "No terminal punctuation")):
            with self.subTest(text=text):
                channel = _Channel()
                calls: list[str] = []
                session = server._ResponseTTSSession(
                    channel,
                    _meta(f"short-{index}"),
                    time.perf_counter(),
                    synthesize=lambda value: calls.append(value) or b"mp3",
                )
                task = session.start()
                session.finish_input(text)
                session.mark_text_final_sent()
                await asyncio.wait_for(task, timeout=1)
                self.assertEqual(calls, [text])
                self.assertEqual(len(session.delivered), 1)

    async def test_pending_queue_is_bounded_and_coalesces_without_reordering(self) -> None:
        channel = _Channel()
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def synthesize(text: str) -> bytes:
            calls.append(text)
            if len(calls) == 1:
                started.set()
                release.wait(timeout=2)
            return text.encode()

        parts = [f"Sentence {index}." for index in range(12)]
        session = server._ResponseTTSSession(
            channel,
            _meta("bounded"),
            time.perf_counter(),
            synthesize=synthesize,
            max_pending=2,
        )
        task = session.start()
        session.enqueue(parts[0])
        await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
        for part in parts[1:]:
            session.enqueue(part)
        self.assertLessEqual(len(session._pending), 2)
        session.finish_input(" ".join(parts))
        session.mark_text_final_sent()
        release.set()
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(" ".join(calls), " ".join(parts))
        self.assertEqual(len(session.queued), len(parts))

    async def test_cancel_discards_pending_and_late_audio(self) -> None:
        channel = _Channel()
        started = threading.Event()
        release = threading.Event()

        def synthesize(_text: str) -> bytes:
            started.set()
            release.wait(timeout=2)
            return b"late"

        session = server._ResponseTTSSession(
            channel,
            _meta("cancel"),
            time.perf_counter(),
            synthesize=synthesize,
        )
        task = session.start()
        session.enqueue("Never deliver this.")
        session.enqueue("Nor this.")
        await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
        session.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.01)
        self.assertEqual(channel.frames, [])
        self.assertEqual(list(session._pending), [])
        self.assertTrue(task.done())

    async def test_partial_failure_retries_only_unsent_text(self) -> None:
        channel = _Channel()
        attempts: list[str] = []
        failed = False

        def synthesize(text: str) -> bytes:
            nonlocal failed
            attempts.append(text)
            if text == "Second sentence." and not failed:
                failed = True
                raise RuntimeError("controlled failure")
            return text.encode()

        session = server._ResponseTTSSession(
            channel,
            _meta("partial-failure"),
            time.perf_counter(),
            synthesize=synthesize,
        )
        task = session.start()
        session.enqueue("First sentence.")
        await asyncio.wait_for(channel.first_audio.wait(), timeout=1)
        session.enqueue("Second sentence.")
        session.finish_input("First sentence. Second sentence.")
        session.mark_text_final_sent()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(
            attempts,
            ["First sentence.", "Second sentence.", "Second sentence."],
        )
        self.assertEqual(
            [item[2] for item in session.delivered],
            ["First sentence.", "Second sentence."],
        )

    async def test_provider_switch_discards_remaining_server_audio(self) -> None:
        channel = _Channel()
        calls: list[str] = []
        settings = {"signature": ("openai", "alloy")}

        session = server._ResponseTTSSession(
            channel,
            _meta("provider-switch"),
            time.perf_counter(),
            synthesize=lambda text: calls.append(text) or text.encode(),
        )
        with patch.object(
            server,
            "_tts_settings_signature",
            side_effect=lambda: settings["signature"],
        ):
            task = session.start()
            session.enqueue("First sentence.")
            await asyncio.wait_for(channel.first_audio.wait(), timeout=1)
            settings["signature"] = ("google", "Aoede")
            session.enqueue("Never deliver this sentence.")
            session.finish_input(
                "First sentence. Never deliver this sentence. Final tail"
            )
            session.mark_text_final_sent()
            await asyncio.wait_for(task, timeout=1)

        self.assertEqual(calls, ["First sentence."])
        audio = [frame for frame in channel.frames if frame["type"] == "audio"]
        self.assertEqual(len(audio), 1)
        self.assertFalse(audio[0]["media_terminal"])
        self.assertEqual(channel.frames[-1]["type"], "media_status")
        self.assertEqual(channel.frames[-1]["status"], "error")

    async def test_total_tts_failure_emits_only_media_error(self) -> None:
        channel = _Channel()
        attempts: list[str] = []

        def fail(text: str) -> bytes:
            attempts.append(text)
            raise RuntimeError("controlled total failure")

        session = server._ResponseTTSSession(
            channel,
            _meta("total-failure"),
            time.perf_counter(),
            synthesize=fail,
        )
        task = session.start()
        session.finish_input("Valid assistant text")
        session.mark_text_final_sent()
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(attempts, ["Valid assistant text", "Valid assistant text"])
        self.assertFalse(any(frame["type"] == "audio" for frame in channel.frames))
        self.assertEqual(channel.frames[-1]["type"], "media_status")
        self.assertEqual(channel.frames[-1]["status"], "error")

    async def test_consecutive_turns_keep_correlation_and_queues_separate(self) -> None:
        channels = [_Channel(), _Channel()]
        sessions = []
        for index, channel in enumerate(channels):
            session = server._ResponseTTSSession(
                channel,
                _meta(f"turn-{index}"),
                time.perf_counter(),
                synthesize=lambda text: text.encode(),
            )
            sessions.append(session)
            session.start()
            session.finish_input(f"Turn {index}.")
            session.mark_text_final_sent()
        await asyncio.gather(*(session.task for session in sessions))
        for index, channel in enumerate(channels):
            audio = next(frame for frame in channel.frames if frame["type"] == "audio")
            self.assertEqual(audio["request_id"], f"wor55-request-turn-{index}")
            self.assertEqual(base64.b64decode(audio["data"]).decode(), f"Turn {index}.")

    async def test_server_shutdown_cancels_and_reaps_media_workers(self) -> None:
        started = asyncio.Event()

        async def linger() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(linger())
        server._POST_TURN_MEDIA_TASKS.add(task)
        server._POST_TURN_MEDIA_BY_CONV["wor55-shutdown"] = task
        await asyncio.wait_for(started.wait(), timeout=1)

        with (
            patch.object(server, "_SERVER_CRON_MANAGER", None),
            patch.object(server, "_SERVER_CRON_UNSUBSCRIBE", None),
            patch.object(server, "_release_single_runner"),
            patch.dict(server._autonomous, {"orch": None}),
        ):
            await server._stop_autonomous_on_shutdown()

        self.assertTrue(task.done())
        self.assertEqual(server._POST_TURN_MEDIA_TASKS, set())
        self.assertEqual(server._POST_TURN_MEDIA_BY_CONV, {})


class ProgressiveTTSTurnIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        tasks = list(server._POST_TURN_MEDIA_TASKS)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        server._POST_TURN_MEDIA_TASKS.clear()
        server._POST_TURN_MEDIA_BY_CONV.clear()

    async def _run_turn(self, agent: _Agent, meta: dict, synthesize):
        channel = _Channel()
        persisted: list[str] = []
        with (
            patch("osia.config.load_settings", return_value={}),
            patch.object(server, "_agent_needs_model_apply", return_value=False),
            patch.object(server, "_persist_accepted_user_message", return_value=True),
            patch.object(
                server,
                "_persist_completed_assistant_message",
                side_effect=lambda _conv, text, *_args: persisted.append(text) or True,
            ),
            patch.object(
                server, "_application_transport_coordination_enabled", return_value=False
            ),
            patch.object(server, "synthesize_to_mp3_bytes", side_effect=synthesize),
        ):
            result = await server._run_agent_and_respond(
                channel,
                "synthetic request",
                [],
                meta["conversation_id"],
                get_agent=lambda _conv: agent,
                turn_meta=meta,
            )
            tasks = list(server._POST_TURN_MEDIA_TASKS)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        return channel, persisted, result

    async def test_canonical_text_final_persistence_and_mobile_audio_order(self) -> None:
        final = "First sentence. Second sentence. Final unfinished tail"
        generation_final = threading.Event()
        synth_calls: list[str] = []

        def run(on_sentence, on_first_token):
            on_first_token()
            on_sentence("First sentence.")
            on_sentence("Second sentence.")
            generation_final.set()
            return final

        def synthesize(text: str) -> bytes:
            synth_calls.append(text)
            self.assertTrue(generation_final.is_set())
            return text.encode()

        channel, persisted, (history, conv_id) = await self._run_turn(
            _Agent(run, final), _meta("integration"), synthesize
        )
        self.assertEqual(conv_id, "wor55-conversation-integration")
        self.assertEqual(history[-1]["content"], final)
        self.assertEqual(persisted, [final])
        self.assertEqual(
            [frame["content"] for frame in channel.frames if frame["type"] == "response"],
            [final],
        )
        self.assertEqual(
            synth_calls,
            [final],
        )
        sequences = [frame["sequence"] for frame in channel.frames if "sequence" in frame]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))

    async def test_tool_markup_is_neither_streamed_nor_spoken(self) -> None:
        final = "Visible grounded result."

        def run(on_sentence, on_first_token):
            on_first_token()
            on_sentence('<tool_call>{"name":"secret"}</tool_call>')
            on_sentence(final)
            return final

        calls: list[str] = []
        channel, persisted, _result = await self._run_turn(
            _Agent(run, final),
            _meta("tools"),
            lambda text: calls.append(text) or b"mp3",
        )
        chunks = [frame["content"] for frame in channel.frames if frame["type"] == "response_chunk"]
        self.assertEqual(chunks, [final])
        self.assertEqual(calls, [final])
        self.assertEqual(persisted, [final])
        types = [frame["type"] for frame in channel.frames]
        self.assertLess(types.index("response_chunk"), types.index("response"))
        self.assertLess(types.index("response"), types.index("turn_status"))

    async def test_tts_failure_never_replaces_valid_text(self) -> None:
        final = "The valid answer survives."

        def run(on_sentence, on_first_token):
            on_first_token()
            on_sentence(final)
            return final

        channel, persisted, _result = await self._run_turn(
            _Agent(run, final),
            _meta("failure-text"),
            lambda _text: (_ for _ in ()).throw(RuntimeError("synthetic TTS error")),
        )
        self.assertEqual(persisted, [final])
        self.assertEqual(
            [frame["content"] for frame in channel.frames if frame["type"] == "response"],
            [final],
        )
        self.assertFalse(any(frame["type"] == "audio" for frame in channel.frames))
        self.assertEqual(channel.frames[-1]["type"], "media_status")

    async def test_voice_disabled_performs_zero_synthesis_and_closes_media(self) -> None:
        final = "Text only."

        def run(on_sentence, on_first_token):
            on_first_token()
            on_sentence(final)
            return final

        calls: list[str] = []
        channel, persisted, _result = await self._run_turn(
            _Agent(run, final),
            _meta("disabled", tts_enabled=False),
            lambda text: calls.append(text) or b"unexpected",
        )
        self.assertEqual(calls, [])
        self.assertEqual(persisted, [final])
        self.assertFalse(any(frame["type"] == "audio" for frame in channel.frames))
        self.assertEqual(channel.frames[-1]["type"], "media_status")
        self.assertEqual(channel.frames[-1]["status"], "done")

    async def test_request_completion_does_not_wait_for_tts(self) -> None:
        final = "Completion is independent."
        synth_started = threading.Event()
        release = threading.Event()

        def run(on_sentence, on_first_token):
            on_first_token()
            on_sentence(final)
            return final

        def synthesize(_text: str) -> bytes:
            synth_started.set()
            release.wait(timeout=2)
            return b"mp3"

        channel = _Channel()
        agent = _Agent(run, final)
        meta = _meta("completion")
        with (
            patch("osia.config.load_settings", return_value={}),
            patch.object(server, "_agent_needs_model_apply", return_value=False),
            patch.object(server, "_persist_accepted_user_message", return_value=True),
            patch.object(server, "_persist_completed_assistant_message", return_value=True),
            patch.object(
                server, "_application_transport_coordination_enabled", return_value=False
            ),
            patch.object(server, "synthesize_to_mp3_bytes", side_effect=synthesize),
        ):
            await asyncio.wait_for(
                server._run_agent_and_respond(
                    channel,
                    "synthetic request",
                    [],
                    meta["conversation_id"],
                    get_agent=lambda _conv: agent,
                    turn_meta=meta,
                ),
                timeout=1,
            )
            media_tasks = list(server._POST_TURN_MEDIA_TASKS)
            self.assertTrue(media_tasks)
            self.assertTrue(any(not task.done() for task in media_tasks))
            self.assertEqual(
                sum(frame["type"] == "response" for frame in channel.frames), 1
            )
            release.set()
            await asyncio.gather(*media_tasks)


class DesktopStreamingTTSTests(unittest.TestCase):
    def tearDown(self) -> None:
        from osia.voice import tts

        tts.shutdown(join_timeout=1)

    def test_session_token_rejects_late_turn_and_shutdown_reaps_worker(self) -> None:
        from osia.voice import tts

        spoken: list[str] = []
        with (
            patch.object(tts, "_AUDIO_OK", False),
            patch.object(tts, "_speak_blocking", side_effect=spoken.append),
        ):
            old_session = tts.start_streaming_session()
            tts.stop()
            tts.speak_streaming("stale sentence", session_id=old_session)
            new_session = tts.start_streaming_session()
            tts.speak_streaming("first sentence", session_id=new_session)
            tts.speak_streaming("second sentence", session_id=new_session)
            deadline = time.monotonic() + 1
            while len(spoken) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(spoken, ["first sentence", "second sentence"])
            tts.shutdown(join_timeout=1)
            self.assertTrue(tts._stream_thread is None or not tts._stream_thread.is_alive())

    def test_cancel_during_synthesis_cannot_start_late_playback(self) -> None:
        from osia.voice import tts

        synthesis_started = threading.Event()
        synthesis_release = threading.Event()
        synthesis_finished = threading.Event()
        played: list[object] = []

        def synthesize_then_play(_text: str) -> None:
            synthesis_started.set()
            synthesis_release.wait(timeout=2)
            try:
                tts._play_samples_if_current(object(), 24_000)
            finally:
                synthesis_finished.set()

        with (
            patch.object(tts, "_AUDIO_OK", True),
            patch.object(tts, "_speak_blocking", side_effect=synthesize_then_play),
            patch.object(tts.sd, "play", side_effect=lambda samples, **_kw: played.append(samples)),
            patch.object(tts.sd, "stop"),
        ):
            session = tts.start_streaming_session()
            tts.speak_streaming("in-flight sentence", session_id=session)
            self.assertTrue(synthesis_started.wait(timeout=1))
            tts.stop()
            synthesis_release.set()
            self.assertTrue(synthesis_finished.wait(timeout=1))

        self.assertEqual(played, [])

    def test_provider_switch_invalidates_active_desktop_session(self) -> None:
        from osia.gui import main_popup
        from osia.voice import tts

        class _Combo:
            @staticmethod
            def currentData():
                return "google"

        class _Popup:
            _tts_provider_combo = _Combo()
            _provider_default_voice = {"google": "en-US-Standard-F"}

            @staticmethod
            def _populate_voice_combo(_provider, _voice):
                return None

        with (
            patch.object(tts, "stop") as stop,
            patch.object(main_popup, "set_setting"),
        ):
            main_popup._SettingsPanel._on_tts_provider_changed(_Popup(), 0)

        stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
