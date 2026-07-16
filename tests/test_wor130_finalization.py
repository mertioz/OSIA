"""Focused WOR-130 desktop response finalization regressions."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from osia import conversations, scheduled_tasks
from osia.agent import (
    KNOWN_TOOL_NAMES,
    TOOL_MAP,
    OSIAAgent,
    claims_tool_execution,
)
from osia.gui.main_popup import _ChatPage
from osia.gui.streaming import DesktopTextStreamController
from osia.main import _persist_agent_delta


_NO_ACTION_ACK = "Compris. Aucune action n’a été effectuée."
_INFORMATIONAL_SENTENCE = (
    "Une bibliothèque a été créée pour organiser des connaissances dans un "
    "espace soigneusement planifié."
)


def _chunk(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
    )


class _SequenceCompletions:
    def __init__(self, responses: list[str | list[str]]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.messages_by_call: list[list[dict]] = []

    def create(self, **kwargs):
        self.messages_by_call.append(copy.deepcopy(kwargs.get("messages") or []))
        index = min(self.calls, len(self.responses) - 1)
        response = self.responses[index]
        self.calls += 1
        text_chunks = response if isinstance(response, list) else [response]
        if kwargs.get("stream"):
            return [_chunk(text) for text in text_chunks]
        content = "".join(text_chunks)
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _agent_with(*responses: str | list[str]):
    agent = OSIAAgent(model="test-model", lang="fr")
    agent.provider = "ollama"
    completions = _SequenceCompletions(list(responses))
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return agent, completions


class AgentFinalizationTests(unittest.TestCase):
    def test_long_text_only_response_has_one_canonical_final(self):
        final = " ".join([_INFORMATIONAL_SENTENCE] * 80)
        split = len(final) // 3
        agent, completions = _agent_with(
            [final[:split], final[split : split * 2], final[split * 2 :]],
            _NO_ACTION_ACK,
        )
        streamed: list[str] = []

        reply = agent._run_turn(
            "Explique une bibliothèque en détail.",
            history=[],
            on_sentence=streamed.append,
        )

        self.assertEqual(completions.calls, 1)
        self.assertEqual(reply, final)
        self.assertEqual(" ".join(streamed), final)
        assistants = [
            item for item in agent.exported_history()
            if item.get("role") == "assistant"
        ]
        self.assertEqual([item.get("content") for item in assistants], [final])

    def test_informational_passive_and_schedule_words_are_not_action_claims(self):
        self.assertFalse(claims_tool_execution(_INFORMATIONAL_SENTENCE))
        self.assertFalse(
            scheduled_tasks.claims_scheduled_task_saved(_INFORMATIONAL_SENTENCE)
        )
        self.assertTrue(claims_tool_execution("Le fichier a été créé."))
        self.assertTrue(claims_tool_execution("J’ai créé le fichier."))
        self.assertTrue(
            scheduled_tasks.claims_scheduled_task_saved("C’est planifié.")
        )
        self.assertTrue(
            scheduled_tasks.claims_scheduled_task_saved(
                "Votre rappel a été planifié."
            )
        )

    def test_rejected_action_candidate_is_internal_and_not_persisted(self):
        agent, completions = _agent_with(
            "J’ai ouvert le navigateur.",
            _NO_ACTION_ACK,
        )

        reply = agent._run_turn(
            "Ouvre le navigateur.",
            history=[],
            on_sentence=lambda _sentence: None,
        )

        self.assertEqual(completions.calls, 2)
        self.assertEqual(reply, _NO_ACTION_ACK)
        assistants = [
            item for item in agent.exported_history()
            if item.get("role") == "assistant"
        ]
        self.assertEqual(
            [item.get("content") for item in assistants],
            [_NO_ACTION_ACK],
        )

    def test_tool_free_response_persists_once_with_exact_text(self):
        final = "Réponse finale exacte, sans outil et sans seconde conclusion."
        agent, _completions = _agent_with(final)
        reply = agent._run_turn("Question informative.", history=[])

        original_dir = conversations.CONVERSATIONS_DIR
        with tempfile.TemporaryDirectory(prefix="osia-wor130-") as tmp:
            conversations.CONVERSATIONS_DIR = Path(tmp)
            try:
                conv = conversations.create_conversation()
                _persist_agent_delta(conv["id"], agent, 0)
                stored = conversations.get_conversation(conv["id"])
            finally:
                conversations.CONVERSATIONS_DIR = original_dir

        messages = (stored or {}).get("messages") or []
        self.assertEqual([item.get("role") for item in messages], ["user", "assistant"])
        self.assertEqual(messages[-1].get("content"), reply)

    def test_tool_result_precedes_and_alone_drives_final_stream(self):
        tool_name = "wor130_safe_tool"
        tool_call = f'<tool_call>{{"tool":"{tool_name}"}}</tool_call>'
        final = "Le résultat sûr de l’outil est arrivé avant cette réponse finale."
        agent, completions = _agent_with(tool_call, final)
        old_tool = TOOL_MAP.get(tool_name)
        was_known = tool_name in KNOWN_TOOL_NAMES
        TOOL_MAP[tool_name] = lambda **_kwargs: "WOR130_TOOL_OK"
        KNOWN_TOOL_NAMES.add(tool_name)
        streamed: list[str] = []
        try:
            reply = agent._run_turn(
                "Utilise l’outil sûr.",
                history=[],
                on_sentence=streamed.append,
            )
        finally:
            if old_tool is None:
                TOOL_MAP.pop(tool_name, None)
            else:
                TOOL_MAP[tool_name] = old_tool
            if not was_known:
                KNOWN_TOOL_NAMES.discard(tool_name)

        self.assertEqual(completions.calls, 2)
        self.assertEqual(reply, final)
        self.assertEqual(streamed, [final])
        second_wire = completions.messages_by_call[1]
        self.assertTrue(
            any(
                "WOR130_TOOL_OK" in str(item.get("content") or "")
                for item in second_wire
            )
        )


class DesktopFinalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stream_then_final_is_exact_without_duplicate_tail(self):
        controller = DesktopTextStreamController(reduced_motion=lambda: False)
        seen = QSignalSpy(controller.text_changed)
        controller.append_chunk("Début progressif. ")
        controller.append_chunk("Fin en attente.")
        controller._on_tick()
        canonical = "Début progressif. Fin canonique exacte."

        controller.finalize(canonical)

        self.assertEqual(seen[-1][0], canonical)
        self.assertEqual(controller.target_text, canonical)
        self.assertEqual(controller.displayed_text, canonical)
        self.assertEqual(controller.displayed_text.count("Fin canonique exacte."), 1)
        self.assertFalse(controller.active)
        self.assertFalse(controller.timer_active)

    def test_late_status_after_valid_final_does_not_change_bubble(self):
        chat = _ChatPage()
        chat.show()
        self.app.processEvents()
        final = "Une réponse valide qui doit rester visible."
        chat.append_assistant_stream_chunk(final)
        chat._stream._on_tick()
        bubble = chat._stream_bubble
        chat.finalize_assistant_stream(final)

        chat.set_busy(False)
        self.app.processEvents()

        self.assertIsNotNone(bubble)
        self.assertEqual(bubble.text(), final)
        self.assertNotEqual(bubble.text(), _NO_ACTION_ACK)
        chat.close()
        chat.deleteLater()

    def test_cancel_and_error_finalize_only_the_current_stream(self):
        controller = DesktopTextStreamController(reduced_motion=lambda: False)
        controller.append_chunk("Réponse partielle ordonnée.")
        controller.cancel(flush=True)
        self.assertEqual(controller.displayed_text, "Réponse partielle ordonnée.")
        self.assertFalse(controller.active)

        controller.append_chunk("Nouvelle réponse partielle.")
        controller.error("Error: arrêt contrôlé")
        self.assertEqual(controller.displayed_text, "Error: arrêt contrôlé")
        self.assertFalse(controller.active)
        self.assertFalse(controller.timer_active)

    def test_conversation_switch_discards_only_the_old_pending_stream(self):
        chat = _ChatPage()
        chat.show()
        chat.append_assistant_stream_chunk("Ancienne conversation en cours.")
        self.assertTrue(chat._stream.active)

        chat.set_messages([{"role": "user", "content": "Nouvelle conversation"}])

        self.assertFalse(chat._stream.active)
        self.assertFalse(chat._stream.timer_active)
        self.assertEqual(chat._stream.target_text, "")
        self.assertIsNone(chat._stream_bubble)
        chat.close()
        chat.deleteLater()
        QCoreApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
