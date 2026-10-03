"""Deterministic WOR-247 Chat streaming and tool-wait regressions."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import types
from unittest.mock import patch

import pytest

from osia import agent as agent_module
from osia.agent import OSIAAgent, _sanitize_persisted_history_message
from osia.durable_transport import DurableTransportStore
from osia.server import server
from osia.tool_attempts import (
    ToolAttemptCoordinator,
    WAITING_TOOL_RESULT,
    is_internal_tool_control_text,
)
from osia.turn_context import ValidatedLiveAssistantChunk, fixed_turn_context


@pytest.fixture(autouse=True)
def _isolated_durable_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_DURABLE_TRANSPORT",
        DurableTransportStore(tmp_path / "transport"),
    )


class _Channel:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, frame) -> None:
        self.frames.append(dict(frame))


def _meta(suffix: str, *, language: str = "fr") -> dict:
    return {
        "conversation_id": f"wor247-conv-{suffix}",
        "request_id": f"wor247-request-{suffix}",
        "client_request_id": f"wor247-request-{suffix}",
        "turn_id": f"wor247-turn-{suffix}",
        "run_id": f"wor247-run-{suffix}",
        "provider_id": "wor247-provider",
        "model_id": "wor247-model",
        "effective_reasoning": "auto",
        "user_message_id": f"wor247-user-{suffix}",
        "assistant_message_id": f"wor247-assistant-{suffix}",
        "client_locale": f"{language}-{language.upper()}",
        "_event_sequence": 0,
        "persisted": False,
        "tts_enabled": False,
    }


class _StreamingAgent:
    model = "wor247-model"
    provider = "wor247-provider"
    _applied_selected_model = None
    _in_use = False
    _chat_autonomous_context = None
    last_run_artifacts = []
    last_run_diagnostic = None

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.first_callback_returned = threading.Event()

    def run(self, _text, *, on_sentence, on_first_token, **_kwargs):
        on_first_token()
        on_sentence(ValidatedLiveAssistantChunk(
            "Première phrase visible avant la fin du fournisseur."
        ))
        self.first_callback_returned.set()
        assert self.release.wait(2.0)
        on_sentence(ValidatedLiveAssistantChunk(
            "Deuxième phrase diffusée progressivement."
        ))
        return (
            "Première phrase visible avant la fin du fournisseur. "
            "Deuxième phrase diffusée progressivement."
        )

    def exported_history(self):
        return []


def test_server_reveals_only_verified_canonical_text_after_provider_completion():
    async def exercise():
        release = threading.Event()
        agent = _StreamingAgent(release)
        channel = _Channel()
        persisted: list[str] = []
        meta = _meta("live")
        with (
            patch("osia.config.load_settings", return_value={}),
            patch.object(server, "_agent_needs_model_apply", return_value=False),
            patch.object(server, "_persist_accepted_user_message", return_value=True),
            patch.object(
                server,
                "_persist_completed_assistant_message",
                side_effect=lambda _conv, text, *_args: persisted.append(text) or True,
            ),
        ):
            task = asyncio.create_task(
                server._run_agent_and_respond(
                    channel,
                    "Réponds avec deux phrases.",
                    [],
                    meta["conversation_id"],
                    get_agent=lambda _conv: agent,
                    turn_meta=meta,
                )
            )
            assert await asyncio.to_thread(agent.first_callback_returned.wait, 1.0)
            deadline = time.monotonic() + 1.0
            while not any(
                frame.get("type") == "response_chunk" for frame in channel.frames
            ) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            early = [
                frame["content"] for frame in channel.frames
                if frame.get("type") == "response_chunk"
            ]
            assert early == []
            assert not any(frame.get("type") == "response" for frame in channel.frames)
            release.set()
            await task

        chunks = [
            frame["content"] for frame in channel.frames
            if frame.get("type") == "response_chunk"
        ]
        finals = [
            frame["content"] for frame in channel.frames
            if frame.get("type") == "response"
        ]
        expected = (
            "Première phrase visible avant la fin du fournisseur. "
            "Deuxième phrase diffusée progressivement."
        )
        assert chunks == [expected]
        assert finals == persisted == [expected]

    asyncio.run(exercise())


def test_agent_language_guard_releases_a_bounded_prefix_before_completion():
    agent = OSIAAgent(lang="fr")
    release = threading.Event()
    first_fragment = threading.Event()
    visible: list[str] = []
    result: list[str] = []

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        if on_first_token is not None:
            on_first_token()
        callback("Cette première phrase française est visible immédiatement.")
        first_fragment.set()
        assert release.wait(2.0)
        callback("La deuxième arrive avant la réponse finale.")
        return (
            "Cette première phrase française est visible immédiatement. "
            "La deuxième arrive avant la réponse finale."
        )

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)
    worker = threading.Thread(
        target=lambda: result.append(agent.run(
            "Réponds progressivement en français.",
            on_sentence=visible.append,
            turn_context=fixed_turn_context("fr", source="wor247-test"),
        )),
        daemon=True,
    )
    worker.start()
    assert first_fragment.wait(1.0)
    assert visible == [
        "Cette première phrase française est visible immédiatement."
    ]
    assert isinstance(visible[0], ValidatedLiveAssistantChunk)
    release.set()
    worker.join(2.0)
    assert not worker.is_alive()
    assert result == [" ".join(visible)]


class _PendingAgent:
    model = "wor247-model"
    provider = "wor247-provider"
    _applied_selected_model = None
    _in_use = False
    _chat_autonomous_context = None
    last_run_artifacts = []
    last_run_diagnostic = None

    def __init__(self) -> None:
        self.callbacks = None
        self.terminal = None

    def configure_tool_attempts(self, **kwargs):
        self.callbacks = kwargs

    def run(self, _text, *, on_sentence, **_kwargs):
        on_sentence(ValidatedLiveAssistantChunk("Vérification en cours."))
        on_sentence(WAITING_TOOL_RESULT)
        return WAITING_TOOL_RESULT

    def activate_pending_tool_attempt(self):
        return True

    def pending_tool_attempt_record(self):
        return {
            "tool_attempt_id": "wor247-attempt-waiting",
            "status": "pending",
        }

    def terminality_result(self, _turn_id):
        return self.terminal

    def exported_history(self):
        return [
            {"role": "assistant", "content": WAITING_TOOL_RESULT},
        ]

    def clear_tool_attempts(self):
        return None


def _complete_pending_fixture(agent: _PendingAgent, cancelled: bool) -> bool:
    final_text = (
        "" if cancelled else "Résultat canonique après reprise."
    )
    agent.terminal = {
        "terminal_state": (
            "user_cancelled" if cancelled else "success_verified"
        ),
        "reason_code": (
            "authoritative_user_cancellation" if cancelled else "verified"
        ),
        "final_text": final_text,
    }
    return agent.callbacks["final_callback"](
        "wor247-attempt-waiting",
        "Résultat tardif qui ne doit pas remplacer Stop."
        if cancelled else final_text,
        None,
    )


@pytest.mark.parametrize("cancelled", [False, True])
def test_wait_sentinel_never_streams_persists_or_terminalizes_chat(cancelled):
    async def exercise():
        channel = _Channel()
        agent = _PendingAgent()
        meta = _meta("waiting")
        meta["tts_enabled"] = True
        persisted: list[str] = []
        tts_sessions = []

        class TTSSession:
            def __init__(self, *_args, **_kwargs):
                self.inputs = []
                self.cancelled = False
                tts_sessions.append(self)

            def start(self):
                return None

            def enqueue(self, text):
                self.inputs.append(str(text))

            def finish_input(self, text):
                self.inputs.append(str(text))

            def mark_text_final_sent(self):
                return None

            def cancel(self):
                self.cancelled = True

        class WaitingStore:
            def find_compatible_suspended_turn(self, conversation_id):
                assert conversation_id == meta["conversation_id"]
                return {
                    "tool_attempt_id": "wor247-attempt-waiting",
                    "conversation_id": conversation_id,
                }

        class WaitingCoordinator:
            store = WaitingStore()

            def request_reconciliation(self, _conversation_id):
                raise AssertionError("an unrelated follow-up must not reconcile")

            def request_cancellation(self, _conversation_id):
                return False

        coordinator = WaitingCoordinator()
        current = asyncio.current_task()
        server._ACTIVE_TURNS_BY_CONV[meta["conversation_id"]] = current
        server._ACTIVE_TURN_META_BY_CONV[meta["conversation_id"]] = meta
        try:
            with (
                patch("osia.config.load_settings", return_value={}),
                patch.object(server, "_agent_needs_model_apply", return_value=False),
                patch.object(
                    server, "_persist_accepted_user_message", return_value=True,
                ),
                patch.object(
                    server,
                    "_persist_completed_assistant_message",
                    side_effect=lambda _conv, text, *_args: persisted.append(text)
                    or True,
                ),
                patch.object(server, "_ResponseTTSSession", TTSSession),
                patch.object(server, "_track_response_audio_task", return_value=True),
                patch("osia.tool_attempts._DEFAULT_COORDINATOR", coordinator),
            ):
                await server._run_agent_and_respond(
                    channel,
                    "outil lent synthétique",
                    [],
                    meta["conversation_id"],
                    get_agent=lambda _conv: agent,
                    turn_meta=meta,
                )
                holder = server._ACTIVE_TURNS_BY_CONV[meta["conversation_id"]]
                assert holder is not current
                assert not holder.done()
                assert persisted == []
                assert WAITING_TOOL_RESULT not in str(channel.frames)
                assert not any(
                    frame.get("type") == "response" for frame in channel.frames
                )
                assert [
                    frame.get("content") for frame in channel.frames
                    if frame.get("type") == "response_chunk"
                ] == []
                assert any(
                    frame.get("type") == "turn_status"
                    and frame.get("status") == "waiting_tool_result"
                    for frame in channel.frames
                )
                assert tts_sessions[0].cancelled is True
                assert tts_sessions[0].inputs == []

                follow_up = _Channel()
                assert not await server._handle_waiting_tool_command(
                    follow_up, meta["conversation_id"], "alors",
                )
                assert not any(
                    frame.get("type") == "conversation_busy"
                    for frame in follow_up.frames
                )

                finalized = await asyncio.to_thread(
                    _complete_pending_fixture,
                    agent,
                    cancelled,
                )
                assert finalized is True
                assert all(
                    WAITING_TOOL_RESULT not in session.inputs
                    for session in tts_sessions
                )
                responses = [
                    frame for frame in channel.frames
                    if frame.get("type") == "response"
                ]
                if cancelled:
                    assert persisted == []
                    assert responses == []
                else:
                    assert tts_sessions[-1].inputs == [
                        persisted[0], persisted[0],
                    ]
                    assert len(responses) == 1
                    assert persisted == [
                        "Résultat canonique après reprise."
                    ]
                assert server._is_conversation_busy(meta["conversation_id"]) is False
        finally:
            pending = server._ACTIVE_TURNS_BY_CONV.pop(
                meta["conversation_id"], None,
            )
            server._ACTIVE_TURN_META_BY_CONV.pop(meta["conversation_id"], None)
            if pending is not None and not pending.done():
                pending.set_result(None)

    asyncio.run(exercise())


def test_internal_control_markers_are_not_persistable_assistant_prose():
    for marker in (
        WAITING_TOOL_RESULT,
        "WAITING_TOOL",
        "RECONCILING",
        "INTERNAL_RETRY",
    ):
        assert is_internal_tool_control_text(marker)
        assert _sanitize_persisted_history_message({
            "role": "assistant", "content": marker,
        }) is None


def test_chained_wait_is_not_delivered_to_the_final_callback():
    class Store:
        def __init__(self) -> None:
            self.completed = threading.Event()

        def get_attempt(self, _attempt_id):
            return {
                "conversation_id": "wor247-conv-chain",
                "status": "succeeded",
                "continuation_state": "not_claimed",
                "final_response_message_id": "wor247-final-message",
            }

        def find_compatible_suspended_turn(self, conversation_id):
            assert conversation_id == "wor247-conv-chain"
            return {"tool_attempt_id": "wor247-attempt-chained"}

        def claim_continuation(self, _attempt_id):
            return "claim-wor247"

        def complete_continuation(
            self, _attempt_id, _claim, *, final_response_message_id,
        ):
            assert final_response_message_id == "wor247-final-message"
            self.completed.set()
            return True

        def release_continuation(self, *_args):
            raise AssertionError("a chained continuation must complete")

    store = Store()
    coordinator = ToolAttemptCoordinator(store)
    finals: list[str] = []
    coordinator.register_suspended(
        "wor247-attempt-original",
        resume=lambda *_args: WAITING_TOOL_RESULT,
        finalize=lambda _attempt, text, _error: finals.append(text) or True,
    )
    coordinator._schedule_timer = lambda *_args, **_kwargs: None
    assert coordinator.activate("wor247-attempt-original")
    coordinator.notify_terminal("wor247-attempt-original", {"terminal": True})
    assert store.completed.wait(1.0)
    assert finals == []


def _install_rounds(agent: OSIAAgent, rounds) -> None:
    remaining = iter(rounds)

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        content, fragments = next(remaining)
        if on_first_token is not None:
            on_first_token()
        for fragment in fragments:
            callback(fragment)
        return content

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


def test_failed_list_files_is_reresolved_before_another_dispatch(
    tmp_path, monkeypatch,
):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "synthetic-cv.txt").write_text("fixture", encoding="utf-8")
    missing = str(desktop / "missing")
    calls: list[str] = []

    def list_files(path):
        calls.append(path)
        if path == missing:
            return "ERROR: synthetic directory does not exist"
        return "synthetic-cv.txt"

    monkeypatch.setitem(agent_module.TOOL_MAP, "list_files", list_files)
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    agent = OSIAAgent(lang="en")
    missing_json = json.dumps(missing)
    desktop_json = json.dumps(str(desktop))
    _install_rounds(agent, [
        (f'<tool_call>{{"tool":"list_files","path":{missing_json}}}</tool_call>', []),
        (f'<tool_call>{{"tool":"list_files","path":{missing_json}}}</tool_call>', []),
        (
            '<tool_call>{"tool":"list_files","path":'
            + desktop_json
            + "}</tool_call>",
            [],
        ),
        ("Found the synthetic Desktop fixture.", [
            "Found the synthetic Desktop fixture.",
        ]),
        # WOR-303: the canonical turn keeps the same objective and asks once
        # more when postcondition evidence is still missing.
        ("Found the synthetic Desktop fixture.", [
            "Found the synthetic Desktop fixture.",
        ]),
    ])

    final = agent.run(
        "Find the synthetic file on Desktop.", on_sentence=lambda _text: None,
    )

    assert final == "Found the synthetic Desktop fixture."
    # The existing bounded low-level recovery makes one immediate retry.  The
    # next identical provider dispatch is rejected before execution, and only
    # the repaired path reaches the tool again.
    assert calls == [missing, missing, str(desktop)]
