#!/usr/bin/env python3
"""Focused WOR-89 Phase B/B2 tests for agent memory behavior."""

from __future__ import annotations

import sys
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.agent import OSIAAgent, TOOL_MAP, get_system_prompt
from osia.memory import MemoryStore
from osia.memory_runtime import (
    build_explicit_memory_entry,
    build_memory_context,
    execute_save_memory_tool,
    detect_candidate_memory,
    detect_explicit_memory_instruction,
    lookup_verified_app_route,
    parse_save_memory_tool_result,
    remember_verified_route,
    remember_verified_site,
)


class FakeCompletions:
    def __init__(self, content: str = "RÃ©ponse test."):
        self.responses = list(content) if isinstance(content, list) else [content]
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        message = SimpleNamespace(content=self.responses[idx], tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content: str = "RÃ©ponse test."):
        self.chat = SimpleNamespace(completions=FakeCompletions(content))

    @property
    def calls(self) -> list[dict]:
        return self.chat.completions.calls


class BrokenVerifyStore:
    def upsert_entry(self, entry=None, **fields):
        return {"id": "mem_unverified"}

    def get_entry(self, entry_id):
        return None


def _patch_hierarchy_disabled():
    import osia.config as config

    original = config.get_setting

    def fake_get_setting(key, default=None):
        if key == "ai_hierarchy_enabled":
            return False
        return default

    config.get_setting = fake_get_setting
    return config, original


def _save_memory_call(**kwargs) -> str:
    payload = {
        "tool": "save_memory",
        "type": kwargs.get("type", "preference"),
        "category": kwargs.get("category", "user"),
        "key": kwargs.get("key", "frites"),
        "title": kwargs.get("title", "Preference: frites"),
        "value": kwargs.get(
            "value",
            {"kind": "text", "data": "L'utilisateur aime les frites."},
        ),
        "confidence": kwargs.get("confidence", 1.0),
        "tags": kwargs.get("tags", []),
    }
    return "<tool_call>\n" + json.dumps(payload, ensure_ascii=False) + "\n</tool_call>"


class MemoryAgentTests(unittest.TestCase):
    def make_agent(self, store, response: str = "RÃ©ponse test.") -> OSIAAgent:
        agent = OSIAAgent(lang="fr")
        agent.client = FakeClient(response)
        agent._memory_store = store
        return agent

    def test_save_memory_tool_is_in_prompt_and_tool_map(self):
        prompt = get_system_prompt("en", "test-model", "test-provider")

        self.assertIn("save_memory", TOOL_MAP)
        self.assertIn("save_memory", prompt)
        self.assertIn('{"ok":true}', prompt)
        self.assertIn("Claim that memory was saved only after", prompt)

    def test_save_memory_tool_writes_and_returns_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")

            result = execute_save_memory_tool(
                {
                    "type": "preference",
                    "category": "user",
                    "key": "tea",
                    "title": "Preference: tea",
                    "value": {"kind": "text", "data": "The user likes tea."},
                    "confidence": 1.0,
                    "tags": ["drink"],
                },
                store=store,
                user_text="remember this: I like tea",
            )

            self.assertTrue(result["ok"])
            self.assertIn("entry_id", result)
            entry = store.get_entry(result["entry_id"])
            self.assertIsNotNone(entry)
            self.assertEqual(entry["source"], "explicit_user")
            self.assertEqual(entry["value"]["data"], "The user likes tea.")

    def test_save_memory_tool_rejects_secret_and_invalid_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")

            secret = execute_save_memory_tool(
                {
                    "type": "fact",
                    "category": "user",
                    "key": "token",
                    "title": "Token",
                    "value": {"kind": "text", "data": "sk-" + ("a" * 40)},
                    "confidence": 1.0,
                },
                store=store,
                user_text="remember this",
            )
            invalid = execute_save_memory_tool(
                {
                    "type": "unknown",
                    "category": "user",
                    "key": "bad",
                    "title": "Bad",
                    "value": {"kind": "text", "data": "Bad."},
                },
                store=store,
                user_text="remember this",
            )

            self.assertFalse(secret["ok"])
            self.assertEqual(secret["code"], "secret_rejected")
            self.assertFalse(invalid["ok"])
            self.assertEqual(invalid["code"], "validation_error")
            self.assertEqual(store.list_entries(), [])

    def test_complex_project_note_saves_through_save_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            note = (
                "Project GHOST uses Flutter and FastAPI; status is prototype; "
                "deadline is 2026-08-15; next step is auth QA."
            )

            result = execute_save_memory_tool(
                {
                    "type": "fact",
                    "category": "workflow",
                    "key": "ghost-project",
                    "title": "Project GHOST",
                    "value": {"kind": "text", "data": note},
                    "confidence": 0.95,
                    "tags": ["project", "ghost"],
                },
                store=store,
                user_text="please remember this project note",
            )

            self.assertTrue(result["ok"])
            entry = store.get_entry(result["entry_id"])
            self.assertIn("GHOST", entry["title"])
            self.assertIn("deadline is 2026-08-15", entry["value"]["data"])
            self.assertIn("Flutter", entry["value"]["data"])

    def test_multilingual_instruction_saves_through_tool_without_new_regex(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                agent = self.make_agent(store, response=[
                    _save_memory_call(
                        key="short-answers",
                        title="Preference: short answers",
                        value={"kind": "text", "data": "The user prefers short answers."},
                    ),
                    "Saved.",
                ])

                result = agent.run("please remember that I prefer short answers", history=[])

                self.assertEqual(result, "Saved.")
                entries = store.list_entries()
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["source"], "explicit_user")
                self.assertEqual(entries[0]["key"], "short-answers")
        finally:
            config.get_setting = original_get_setting

    def test_explicit_memory_phrases_are_detected(self):
        cases = {
            "retiens que j'aime les frites": "j'aime les frites",
            "garde ca en tete : j'aime les frites": "j'aime les frites",
            r"a retenir : mon dossier projets est dans D:\Projects": (
                r"mon dossier projets est dans D:\Projects"
            ),
            "pour plus tard, je prefere les reponses courtes": (
                "je prefere les reponses courtes"
            ),
            "note dans ta memoire que mon navigateur principal est Brave": (
                "mon navigateur principal est Brave"
            ),
            "souviens t'en : j'adore le cafÃ©": "j'adore le cafÃ©",
            "souviens-toi, j'utilise Brave": "j'utilise Brave",
            "remember this: I prefer concise answers": "I prefer concise answers",
            "keep in mind that I like tea": "I like tea",
            "for later, my project folder is C:\\Projects": (
                "my project folder is C:\\Projects"
            ),
            "save this for later: I use Linear": "I use Linear",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                instruction = detect_explicit_memory_instruction(text)
                self.assertIsNotNone(instruction)
                self.assertEqual(instruction.content, expected)

        self.assertEqual(
            build_explicit_memory_entry("j'adore le cafe")["type"],
            "preference",
        )

    def test_explicit_memory_save_writes_valid_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            agent = self.make_agent(store, response=[
                _save_memory_call(),
                "Saved.",
            ])

            result = agent.run("retiens que j'aime les frites", history=[])

            self.assertEqual(result, "Saved.")
            self.assertEqual(len(agent.client.calls), 2)
            entries = store.list_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["type"], "preference")
            self.assertEqual(entries[0]["category"], "user")
            self.assertEqual(entries[0]["key"], "frites")
            self.assertEqual(
                entries[0]["value"]["data"],
                "L'utilisateur aime les frites.",
            )
            self.assertEqual(entries[0]["source"], "explicit_user")

    def test_explicit_varied_route_and_english_preference_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            agent = self.make_agent(store, response=[
                _save_memory_call(
                    type="route",
                    category="folder",
                    key="projets",
                    title="Folder: projets",
                    value={"kind": "path", "data": r"D:\Projects"},
                ),
                "Route saved.",
                _save_memory_call(
                    type="preference",
                    category="user",
                    key="concise-answers",
                    title="Preference: concise answers",
                    value={"kind": "text", "data": "The user prefers concise answers."},
                ),
                "Preference saved.",
            ])

            route_result = agent.run(
                r"a retenir : mon dossier projets est dans D:\Projects",
                history=[],
            )
            pref_result = agent.run(
                "remember this: I prefer concise answers",
                history=[],
            )

            self.assertEqual(route_result, "Route saved.")
            self.assertEqual(pref_result, "Preference saved.")
            self.assertEqual(len(agent.client.calls), 4)
            entries = store.list_entries()
            self.assertEqual(len(entries), 2)
            route = next(entry for entry in entries if entry["category"] == "folder")
            preference = next(entry for entry in entries if entry["type"] == "preference")
            self.assertEqual(route["type"], "route")
            self.assertEqual(route["value"], {"kind": "path", "data": r"D:\Projects"})
            self.assertEqual(preference["value"]["data"], "The user prefers concise answers.")
            return

            self.assertEqual(route_result, "Câ€™est mÃ©morisÃ©.")
            self.assertEqual(pref_result, "Câ€™est mÃ©morisÃ©.")
            self.assertEqual(agent.client.calls, [])
            entries = store.list_entries()
            self.assertEqual(len(entries), 2)
            route = next(entry for entry in entries if entry["category"] == "folder")
            preference = next(entry for entry in entries if entry["type"] == "preference")
            self.assertEqual(route["type"], "route")
            self.assertEqual(route["value"], {"kind": "path", "data": r"D:\Projects"})
            self.assertEqual(preference["value"]["data"], "The user prefers concise answers.")

    def test_confirmation_requires_verified_write(self):
        agent = self.make_agent(BrokenVerifyStore(), response=[
            _save_memory_call(),
            "C'est memorise.",
            "Je n'ai rien ajoute a la memoire locale.",
        ])

        result = agent.run("retiens que j'aime les frites", history=[])

        self.assertEqual(result, "Je n'ai rien ajoute a la memoire locale.")
        self.assertEqual(len(agent.client.calls), 3)
        return

        self.assertEqual(agent.client.calls, [])

    def test_secret_like_memory_is_rejected_and_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            agent = self.make_agent(store, response=[
                _save_memory_call(
                    key="token",
                    title="Token",
                    value={"kind": "text", "data": "sk-" + ("a" * 40)},
                ),
                "C'est memorise.",
                "Je n'ai pas pu memoriser cette information.",
            ])

            result = agent.run("retiens que sk-" + ("a" * 40), history=[])

            self.assertEqual(result, "Je n'ai pas pu memoriser cette information.")
            self.assertEqual(len(agent.client.calls), 3)
            self.assertEqual(store.list_entries(), [])
            return

            self.assertEqual(agent.client.calls, [])
            self.assertEqual(store.list_entries(), [])

    def test_about_me_query_builds_memory_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            store.upsert_entry({
                "type": "preference",
                "category": "user",
                "key": "frites",
                "title": "Preference: les frites",
                "value": {
                    "kind": "text",
                    "data": "L'utilisateur aime les frites.",
                },
                "confidence": 1.0,
                "source": "explicit_user",
            })

            context = build_memory_context(
                "tu sais quoi de moi ?",
                store=store,
            )

            self.assertIsNotNone(context)
            self.assertIn("Local OSIA memory", context)
            self.assertIn("L'utilisateur aime les frites.", context)

    def test_auto_candidate_detection(self):
        preference = detect_candidate_memory("j'aime les frites")
        folder = detect_candidate_memory(r"mon dossier projets est dans D:\Projects")
        workflow = detect_candidate_memory("quand je dis travail, ouvre Linear et Gmail")

        self.assertIsNotNone(preference)
        self.assertEqual(preference.entry["type"], "preference")
        self.assertIsNotNone(folder)
        self.assertEqual(folder.entry["category"], "folder")
        self.assertIsNotNone(workflow)
        self.assertEqual(workflow.entry["type"], "workflow")

    def test_auto_preference_social_tail_and_color_detection(self):
        cases = [
            (
                "et j'aime aussi le chocolat tu savais ?",
                "chocolat",
                "L'utilisateur aime le chocolat.",
            ),
            (
                "j'aime la couleur bleu",
                "couleur-bleu",
                "L'utilisateur aime la couleur bleu.",
            ),
            (
                "j'aime la couleur bleue",
                "couleur-bleue",
                "L'utilisateur aime la couleur bleue.",
            ),
            (
                "je pr\u00e9f\u00e8re la couleur bleue",
                "couleur-bleue",
                "L'utilisateur pr\u00e9f\u00e8re la couleur bleue.",
            ),
            (
                "ma couleur pr\u00e9f\u00e9r\u00e9e est le bleu",
                "couleur-preferee",
                "ma couleur pr\u00e9f\u00e9r\u00e9e est le bleu.",
            ),
        ]
        for text, key_part, expected_value in cases:
            with self.subTest(text=text):
                candidate = detect_candidate_memory(text)

                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.entry["type"], "preference")
                self.assertIn(key_part, candidate.entry["key"])
                self.assertEqual(candidate.entry["value"]["data"], expected_value)

    def test_auto_preference_real_questions_are_not_saved(self):
        cases = [
            "est-ce que tu aimes les frites ?",
            "tu crois que j'aime le chocolat ?",
            "j'aime le chocolat ?",
            "est-ce que le bleu est ma couleur pr\u00e9f\u00e9r\u00e9e ?",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(detect_candidate_memory(text))

    def test_auto_preference_save_does_not_bypass_provider_call(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                agent = self.make_agent(store, response=[
                    _save_memory_call(),
                    "Bien recu.",
                ])

                result = agent.run("j'aime les frites", history=[])

                self.assertEqual(result, "Bien recu.")
                self.assertEqual(len(agent.client.calls), 2)
                entries = store.list_entries()
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["source"], "agent_candidate")
                self.assertEqual(entries[0]["type"], "preference")
                self.assertEqual(
                    entries[0]["value"]["data"],
                    "L'utilisateur aime les frites.",
                )
                latest_wire_text = "\n".join(
                    str(message.get("content", ""))
                    for message in agent.client.calls[-1]["messages"]
                )
                self.assertIn("save_memory", latest_wire_text)
                self.assertIn('"ok": true', latest_wire_text)
        finally:
            config.get_setting = original_get_setting

    def test_natural_preference_autosave_writes_visible_entries(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                agent = self.make_agent(store, response=[
                    _save_memory_call(
                        key="chocolat",
                        title="Preference: chocolat",
                        value={"kind": "text", "data": "L'utilisateur aime le chocolat."},
                    ),
                    "OK.",
                    _save_memory_call(
                        key="couleur-bleu",
                        title="Preference: couleur bleu",
                        value={"kind": "text", "data": "L'utilisateur aime la couleur bleu."},
                    ),
                    "OK.",
                    _save_memory_call(
                        key="couleur-preferee",
                        title="Preference: couleur preferee",
                        value={"kind": "text", "data": "ma couleur préférée est le bleu."},
                    ),
                    "OK.",
                ])

                agent.run("et j'aime aussi le chocolat tu savais ?", history=[])
                agent.run("j'aime la couleur bleu")
                agent.run("ma couleur pr\u00e9f\u00e9r\u00e9e est le bleu")

                entries = store.list_entries()
                values = {
                    entry["value"]["data"]
                    for entry in entries
                    if entry.get("type") == "preference"
                }
                self.assertIn("L'utilisateur aime le chocolat.", values)
                self.assertIn("L'utilisateur aime la couleur bleu.", values)
                self.assertIn("ma couleur pr\u00e9f\u00e9r\u00e9e est le bleu.", values)
                self.assertEqual(len(agent.client.calls), 6)
        finally:
            config.get_setting = original_get_setting

    def test_auto_route_and_workflow_save(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                agent = self.make_agent(store, response=[
                    _save_memory_call(
                        type="route",
                        category="folder",
                        key="projets",
                        title="Folder: projets",
                        value={"kind": "path", "data": r"D:\Projects"},
                    ),
                    "OK.",
                    _save_memory_call(
                        type="workflow",
                        category="workflow",
                        key="travail",
                        title="Workflow: travail",
                        value={"kind": "steps", "data": ["ouvre Linear", "ouvre Gmail"]},
                    ),
                    "OK.",
                ])

                agent.run(r"mon dossier projets est dans D:\Projects", history=[])
                agent.run("quand je dis travail, ouvre Linear et Gmail", history=[])

                self.assertEqual(len(agent.client.calls), 4)
                entries = store.list_entries()
                route = next(entry for entry in entries if entry["type"] == "route")
                workflow = next(entry for entry in entries if entry["type"] == "workflow")
                self.assertEqual(route["category"], "folder")
                self.assertEqual(route["value"], {"kind": "path", "data": r"D:\Projects"})
                self.assertEqual(workflow["category"], "workflow")
                self.assertEqual(workflow["value"]["kind"], "steps")
                self.assertIn("Linear", workflow["value"]["data"][0])
        finally:
            config.get_setting = original_get_setting

    def test_auto_non_save_cases_still_call_provider(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            cases = [
                "est-ce que tu aimes les frites ?",
                "j'aime le chocolat ?",
                "aujourd'hui je mange des frites",
                "mon token est sk-" + ("a" * 40),
            ]
            for text in cases:
                with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                    store = MemoryStore(Path(tmp) / "memory.json")
                    agent = self.make_agent(store, response="Normal.")

                    result = agent.run(text, history=[])

                    self.assertEqual(result, "Normal.")
                    self.assertEqual(len(agent.client.calls), 1)
                    self.assertEqual(store.list_entries(), [])
        finally:
            config.get_setting = original_get_setting

    def test_rejected_memory_like_statement_warns_model_not_saved(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                agent = self.make_agent(store, response=[
                    "C'est memorise.",
                    "Normal.",
                ])

                result = agent.run("mon token est sk-" + ("a" * 40), history=[])

                self.assertEqual(result, "Normal.")
                self.assertEqual(store.list_entries(), [])
                wire_text = "\n".join(
                    str(message.get("content", ""))
                    for message in agent.client.calls[-1]["messages"]
                )
                self.assertIn("No save_memory tool result with ok=true", wire_text)
                self.assertIn("Do not claim", wire_text)
                exported_text = "\n".join(
                    str(message.get("content", ""))
                    for message in agent.exported_history()
                )
                self.assertNotIn("No save_memory tool result", exported_text)
        finally:
            config.get_setting = original_get_setting

    def test_normal_unrelated_prompt_does_not_create_memory(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                agent = self.make_agent(store, response="Bonjour.")

                result = agent.run("bonjour", history=[])

                self.assertEqual(result, "Bonjour.")
                self.assertEqual(len(agent.client.calls), 1)
                self.assertEqual(store.list_entries(), [])
        finally:
            config.get_setting = original_get_setting

    def test_recall_query_includes_auto_saved_preference(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                agent = self.make_agent(store, response=[
                    _save_memory_call(),
                    "OK.",
                    "Tu aimes les frites.",
                ])

                agent.run("j'aime les frites", history=[])
                result = agent.run("qu'est-ce que tu sais de mes preferences ?")

                self.assertEqual(result, "Tu aimes les frites.")
                latest_wire_text = "\n".join(
                    str(message.get("content", ""))
                    for message in agent.client.calls[-1]["messages"]
                )
                self.assertIn("Local OSIA memory", latest_wire_text)
                self.assertIn("L'utilisateur aime les frites.", latest_wire_text)
        finally:
            config.get_setting = original_get_setting

    def test_memory_context_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            for idx in range(10):
                store.upsert_entry({
                    "type": "fact",
                    "category": "user",
                    "key": f"fact-{idx}",
                    "title": f"Fact {idx}",
                    "value": {
                        "kind": "text",
                        "data": f"Fact utilisateur numero {idx}.",
                    },
                    "confidence": 1.0,
                    "source": "explicit_user",
                })

            context = build_memory_context(
                "tu sais quoi de moi ?",
                store=store,
                max_entries=3,
                max_total_chars=260,
            )

            self.assertIsNotNone(context)
            self.assertLessEqual(len(context), 260)
            self.assertLessEqual(context.count("\n- "), 3)

    def test_recall_context_only_lists_current_memorystore_entries(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                store.upsert_entry({
                    "type": "preference",
                    "category": "user",
                    "key": "frites",
                    "title": "Preference: frites",
                    "value": {
                        "kind": "text",
                        "data": "L'utilisateur aime les frites.",
                    },
                    "confidence": 1.0,
                    "source": "explicit_user",
                })
                agent = self.make_agent(store, response="Tu aimes les frites.")
                history = [
                    {"role": "user", "content": "j'aime le chocolat"},
                    {"role": "assistant", "content": "Tu as dit aimer le chocolat."},
                ]

                agent.run("tu sais quoi de moi ?", history=history)

                memory_frame = next(
                    message for message in agent.messages
                    if isinstance(message, dict) and message.get("_memory_context")
                )
                content = str(memory_frame.get("content", ""))
                self.assertIn("L'utilisateur aime les frites.", content)
                self.assertNotIn("chocolat", content)
                self.assertIn("only entries below are loaded", content)
                self.assertIn("Do not add stored facts", content)
        finally:
            config.get_setting = original_get_setting

    def test_recall_after_clear_gets_no_memory_instruction(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                store.upsert_entry({
                    "type": "preference",
                    "category": "user",
                    "key": "frites",
                    "title": "Preference: frites",
                    "value": {
                        "kind": "text",
                        "data": "L'utilisateur aime les frites.",
                    },
                    "confidence": 1.0,
                    "source": "explicit_user",
                })
                store.clear()
                agent = self.make_agent(store, response="Je n'ai rien en mÃ©moire.")

                agent.run("tu sais quoi de moi ?", history=[])

                wire_text = "\n".join(
                    str(message.get("content", ""))
                    for message in agent.client.calls[-1]["messages"]
                )
                self.assertIn("No local OSIA memories are available", wire_text)
                self.assertIn("Do not claim to know stored user facts", wire_text)
                self.assertNotIn("L'utilisateur aime les frites.", wire_text)
                exported_text = "\n".join(
                    str(message.get("content", ""))
                    for message in agent.exported_history()
                )
                self.assertNotIn("No local OSIA memories are available", exported_text)
        finally:
            config.get_setting = original_get_setting

    def test_history_internal_memory_frame_is_dropped_before_run(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                agent = self.make_agent(store, response="Bonjour.")
                stale = "Local OSIA memory relevant to this turn: stale"

                agent.run(
                    "bonjour",
                    history=[
                        {
                            "role": "user",
                            "content": stale,
                            "_internal": True,
                            "_memory_context": True,
                        }
                    ],
                )

                wire_text = "\n".join(
                    str(message.get("content", ""))
                    for message in agent.client.calls[-1]["messages"]
                )
                self.assertNotIn(stale, wire_text)
                self.assertNotIn(stale, "\n".join(
                    str(message.get("content", ""))
                    for message in agent.exported_history()
                ))
        finally:
            config.get_setting = original_get_setting

    def test_agent_candidate_entries_are_recalled_and_listed_for_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            candidate = store.upsert_entry({
                "type": "preference",
                "category": "user",
                "key": "bleu",
                "title": "Preference: bleu",
                "value": {
                    "kind": "text",
                    "data": "L'utilisateur aime la couleur bleue.",
                },
                "confidence": 0.85,
                "source": "agent_candidate",
            })
            route = store.upsert_entry({
                "type": "route",
                "category": "folder",
                "key": "projets",
                "title": "Folder: projets",
                "value": {
                    "kind": "path",
                    "data": r"D:\Projects",
                },
                "confidence": 0.9,
                "source": "mobile_settings",
            })

            context = build_memory_context("tu sais quoi de moi ?", store=store)

            self.assertIsNotNone(context)
            self.assertIn("L'utilisateur aime la couleur bleue.", context)
            self.assertIn(r"D:\Projects", context)
            snapshot_ids = {entry["id"] for entry in store.export_snapshot()["entries"]}
            listed_ids = {entry["id"] for entry in store.list_entries()}
            self.assertEqual(snapshot_ids, {candidate["id"], route["id"]})
            self.assertEqual(listed_ids, snapshot_ids)

            from osia.gui.main_popup import _SettingsPanel

            displayed_ids = {
                entry["id"] for entry in _SettingsPanel._memory_entries_for_display(store)
            }
            self.assertEqual(displayed_ids, snapshot_ids)

    def test_fake_client_request_includes_transient_memory_context(self):
        config, original_get_setting = _patch_hierarchy_disabled()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = MemoryStore(Path(tmp) / "memory.json")
                store.upsert_entry({
                    "type": "preference",
                    "category": "user",
                    "key": "frites",
                    "title": "Preference: les frites",
                    "value": {
                        "kind": "text",
                        "data": "L'utilisateur aime les frites.",
                    },
                    "confidence": 1.0,
                    "source": "explicit_user",
                })
                agent = self.make_agent(store, response="Tu aimes les frites.")

                result = agent.run("tu sais quoi de moi ?", history=[])

                self.assertEqual(result, "Tu aimes les frites.")
                self.assertEqual(len(agent.client.calls), 1)
                wire_messages = agent.client.calls[0]["messages"]
                wire_text = "\n".join(
                    str(message.get("content", "")) for message in wire_messages
                )
                self.assertIn("Local OSIA memory", wire_text)
                self.assertIn("L'utilisateur aime les frites.", wire_text)

                exported_text = "\n".join(
                    str(message.get("content", ""))
                    for message in agent.exported_history()
                )
                self.assertNotIn("Local OSIA memory", exported_text)
                self.assertTrue(
                    any(
                        isinstance(message, dict)
                        and message.get("_internal")
                        and message.get("_memory_context")
                        for message in agent.messages
                    )
                )

                agent.run("bonjour")
                latest_wire_text = "\n".join(
                    str(message.get("content", ""))
                    for message in agent.client.calls[-1]["messages"]
                    if message.get("role") != "system"
                )
                self.assertNotIn("Local OSIA memory", latest_wire_text)
                self.assertFalse(
                    any(
                        isinstance(message, dict)
                        and message.get("_memory_context")
                        for message in agent.messages
                    )
                )
        finally:
            config.get_setting = original_get_setting

    def test_verified_app_route_save_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(
            dir=ROOT
        ) as app_tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            app_path = Path(app_tmp) / "Discord.exe"
            app_path.write_text("", encoding="utf-8")

            first = remember_verified_route(
                category="app",
                label="Discord",
                path=str(app_path),
                store=store,
            )
            second = remember_verified_route(
                category="app",
                label="discord",
                path=str(app_path),
                store=store,
            )

            self.assertTrue(first.saved)
            self.assertTrue(second.saved)
            entries = store.list_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["type"], "route")
            self.assertEqual(entries[0]["category"], "app")
            self.assertEqual(entries[0]["source"], "verified_tool")
            self.assertEqual(entries[0]["value"], {"kind": "path", "data": str(app_path)})

    def test_verified_route_refuses_nonexistent_path(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(
            dir=ROOT
        ) as app_tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            result = remember_verified_route(
                category="app",
                label="Missing",
                path=str(Path(app_tmp) / "Missing.exe"),
                store=store,
            )

            self.assertFalse(result.saved)
            self.assertEqual(result.error_code, "validation")
            self.assertEqual(store.list_entries(), [])

    def test_verified_site_refuses_secret_like_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            result = remember_verified_site(
                "https://example.com",
                label="sk-" + ("a" * 40),
                store=store,
            )

            self.assertFalse(result.saved)
            self.assertEqual(result.error_code, "secret")
            self.assertEqual(store.list_entries(), [])

    def test_implicit_route_does_not_overwrite_explicit_user_route(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(
            dir=ROOT
        ) as app_tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            store.upsert_entry({
                "type": "route",
                "category": "app",
                "key": "discord",
                "title": "Discord explicit",
                "value": {
                    "kind": "path",
                    "data": r"C:\Explicit\Discord.exe",
                },
                "confidence": 1.0,
                "source": "explicit_user",
            })
            app_path = Path(app_tmp) / "Discord.exe"
            app_path.write_text("", encoding="utf-8")

            result = remember_verified_route(
                category="app",
                label="Discord",
                path=str(app_path),
                store=store,
            )

            self.assertFalse(result.saved)
            self.assertEqual(result.error_code, "protected_explicit")
            entries = store.list_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["value"]["data"], r"C:\Explicit\Discord.exe")
            self.assertEqual(entries[0]["source"], "explicit_user")

    def test_lookup_verified_app_route_returns_existing_path(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(
            dir=ROOT
        ) as app_tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            app_path = Path(app_tmp) / "Linear.exe"
            app_path.write_text("", encoding="utf-8")
            saved = remember_verified_route(
                category="app",
                label="Linear",
                path=str(app_path),
                store=store,
            )

            route = lookup_verified_app_route("linear", store=store)

            self.assertTrue(saved.saved)
            self.assertIsNotNone(route)
            self.assertEqual(route["id"], saved.entry["id"])
            self.assertEqual(route["path"], str(app_path))

    def test_lookup_verified_app_route_ignores_stale_path(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(
            dir=ROOT
        ) as app_tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            app_path = Path(app_tmp) / "Slack.exe"
            app_path.write_text("", encoding="utf-8")
            remember_verified_route(
                category="app",
                label="Slack",
                path=str(app_path),
                store=store,
            )
            app_path.unlink()

            self.assertIsNone(lookup_verified_app_route("slack", store=store))


if __name__ == "__main__":
    unittest.main(verbosity=2)
