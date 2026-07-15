#!/usr/bin/env python3
"""Deterministic WOR-117 scheduler intent/execution regressions."""

from __future__ import annotations

import copy
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import openai  # noqa: F401
except ModuleNotFoundError:
    sys.modules["openai"] = SimpleNamespace(
        OpenAI=lambda **_kwargs: SimpleNamespace()
    )

try:
    import psutil  # noqa: F401
except ModuleNotFoundError:
    sys.modules["psutil"] = SimpleNamespace()

from osia import agent, conversations, cron, scheduled_tasks


def _completion(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=None)
        )]
    )


def _stream(content: str):
    return [SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content))]
    )]


class _SequenceCompletions:
    def __init__(self, contents: list[str]):
        self.contents = list(contents)
        self.calls = 0
        self.messages_by_call = []

    def create(self, **kwargs):
        self.messages_by_call.append(copy.deepcopy(kwargs.get("messages") or []))
        index = min(self.calls, len(self.contents) - 1)
        content = self.contents[index]
        self.calls += 1
        if kwargs.get("stream"):
            return _stream(content)
        return _completion(content)


def _client(*contents: str):
    completions = _SequenceCompletions(list(contents))
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def _schedule_call(title: str = "OSIA-WOR117-child") -> str:
    return (
        '<tool_call>{"tool":"create_scheduled_task",'
        f'"title":"{title}","instruction":"return synthetic marker",'
        '"schedule":{"type":"daily","time":"17:00"},'
        '"source":"chat"}</tool_call>'
    )


def _execution_context(task_id: str, schedule: str, *, recurring: bool) -> dict:
    occurrence_id = f"cron_occ_{task_id}"
    return {
        "source": "scheduled_task",
        "task_id": task_id,
        "occurrence_id": occurrence_id,
        "execution_id": occurrence_id,
        "conversation_id": None,
        "recurrence": {
            "schedule": schedule,
            "kind": "interval" if recurring else "once",
            "recurring": recurring,
        },
    }


def _job(
    task_id: str,
    task: str,
    schedule: str,
    next_run_at: str,
) -> dict:
    return {
        "id": task_id,
        "title": task_id,
        "task": task,
        "schedule": schedule,
        "enabled": True,
        "conversation_id": None,
        "source": "chat",
        "created_at": "2026-07-14T00:00:00",
        "updated_at": "2026-07-14T00:00:00",
        "last_run_at": None,
        "last_status": None,
        "next_run_at": next_run_at,
        "next_run_schedule": schedule,
    }


class Wor117SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.store = {"cron_jobs": []}
        self.lock = threading.RLock()
        self.old_scheduled_get = scheduled_tasks.config.get_setting
        self.old_scheduled_set = scheduled_tasks.config.set_setting
        self.old_cron_get = cron.get_setting
        self.old_cron_set = cron.set_setting
        self.old_conversation_dir = conversations.CONVERSATIONS_DIR
        self.temp_conversations = tempfile.TemporaryDirectory()
        conversations.CONVERSATIONS_DIR = Path(self.temp_conversations.name)

        def fake_get(key, default=None):
            with self.lock:
                return copy.deepcopy(self.store.get(key, default))

        def fake_set(key, value):
            with self.lock:
                self.store[key] = copy.deepcopy(value)

        self.fake_get = fake_get
        self.fake_set = fake_set
        scheduled_tasks.config.get_setting = fake_get
        scheduled_tasks.config.set_setting = fake_set
        cron.get_setting = fake_get
        cron.set_setting = fake_set
        scheduled_tasks.set_change_callback(None)

    def tearDown(self):
        scheduled_tasks.config.get_setting = self.old_scheduled_get
        scheduled_tasks.config.set_setting = self.old_scheduled_set
        cron.get_setting = self.old_cron_get
        cron.set_setting = self.old_cron_set
        scheduled_tasks.set_change_callback(None)
        conversations.CONVERSATIONS_DIR = self.old_conversation_dir
        self.temp_conversations.cleanup()

    @staticmethod
    def _wait(manager: cron.CronManager) -> None:
        deadline = time.time() + 3
        while manager._busy.is_set() and time.time() < deadline:
            time.sleep(0.01)
        if manager._busy.is_set():
            raise AssertionError("cron worker did not finish")

    @staticmethod
    def _agent_with(contents: list[str], lang: str = "en"):
        osia = agent.OSIAAgent(model="test-model", lang=lang)
        osia.provider = "ollama"
        osia.client, completions = _client(*contents)
        return osia, completions

    def test_immediate_sequences_do_not_authorize_or_leak_scheduler_failure(self):
        immediate = (
            "OSIA-WOR117-A inspect marker one, then marker two, then compare."
        )
        french = "OSIA-WOR117-A inspecte un, puis deux, puis compare."
        after_that = "Inspect marker one, after that inspect marker two."
        schedule_noun = "Review the project schedule, then compare two markers."
        temporal_noun = "Compare the words today and tomorrow, then summarize."
        for text in (
            immediate,
            french,
            after_that,
            schedule_noun,
            temporal_noun,
        ):
            self.assertFalse(scheduled_tasks.looks_like_schedule_request(text))
            self.assertFalse(
                scheduled_tasks.allows_scheduled_task_tool(
                    text,
                    "create_scheduled_task",
                )
            )

        action_name = "wor117_synthetic_action"
        old_tool = agent.TOOL_MAP.get(action_name)
        was_known = action_name in agent.KNOWN_TOOL_NAMES
        agent.TOOL_MAP[action_name] = lambda **_kwargs: "OSIA-WOR117-ACTION-OK"
        agent.KNOWN_TOOL_NAMES.add(action_name)
        try:
            first_round = (
                f'<tool_call>{{"tool":"{action_name}"}}</tool_call>'
                + _schedule_call("OSIA-WOR117-A-ignored")
            )
            osia, completions = self._agent_with([
                first_round,
                scheduled_tasks.not_scheduled_response("en"),
                "Valid comparison OSIA-WOR117-IMMEDIATE-PASS.",
            ])
            streamed = []
            reply = osia.run(immediate, history=[], on_sentence=streamed.append)
        finally:
            if old_tool is None:
                agent.TOOL_MAP.pop(action_name, None)
            else:
                agent.TOOL_MAP[action_name] = old_tool
            if not was_known:
                agent.KNOWN_TOOL_NAMES.discard(action_name)

        self.assertEqual(reply, "Valid comparison OSIA-WOR117-IMMEDIATE-PASS.")
        self.assertEqual(streamed, ["Valid comparison OSIA-WOR117-IMMEDIATE-PASS."])
        self.assertNotIn("could not schedule", " ".join(streamed).casefold())
        self.assertEqual(self.store["cron_jobs"], [])
        self.assertEqual(completions.calls, 3)
        assistants = [
            item for item in osia.exported_history()
            if item.get("role") == "assistant"
        ]
        self.assertEqual(
            [item.get("content") for item in assistants],
            ["Valid comparison OSIA-WOR117-IMMEDIATE-PASS."],
        )

    def test_genuine_schedule_failure_stays_visible_in_its_own_turn(self):
        request = "Schedule this every Monday at 9."
        osia, _completions = self._agent_with([
            (
                '<tool_call>{"tool":"create_scheduled_task",'
                '"title":"OSIA-WOR117-genuine-failure",'
                '"instruction":"return marker",'
                '"schedule":{"type":"weekly","days_of_week":["mon"],'
                '"time":"09:00"},"source":"chat"}</tool_call>'
            ),
            scheduled_tasks.not_scheduled_response("en"),
        ])
        reply = osia.run(request, history=[])
        self.assertTrue(
            scheduled_tasks.allows_scheduled_task_tool(
                request,
                "create_scheduled_task",
            )
        )
        self.assertTrue(scheduled_tasks.is_schedule_failure_response(reply))

    def test_scheduled_payloads_cannot_create_tasks_in_english_or_french(self):
        cases = (
            (
                "en",
                "Remind me to return OSIA-WOR117-ONESHOT-PASS.",
                "OSIA-WOR117-ONESHOT-PASS.",
            ),
            (
                "fr",
                "Rappelle-moi de retourner OSIA-WOR117-FR-PASS.",
                "OSIA-WOR117-FR-PASS.",
            ),
        )
        for index, (lang, payload, marker) in enumerate(cases):
            with self.subTest(lang=lang):
                osia, completions = self._agent_with(
                    [_schedule_call(f"OSIA-WOR117-child-{index}"), marker],
                    lang=lang,
                )
                context = _execution_context(
                    f"cron_wor117_{index}",
                    "once:2026-07-15T00:00",
                    recurring=False,
                )
                before = len(self.store["cron_jobs"])
                reply = osia.run_scheduled(payload, context, history=[])
                self.assertEqual(reply, marker)
                self.assertEqual(len(self.store["cron_jobs"]), before)
                wire = completions.messages_by_call[0]
                wire_text = "\n".join(str(item.get("content", "")) for item in wire)
                self.assertIn("OSIA SCHEDULED EXECUTION CONTEXT", wire_text)
                exported = str(osia.exported_history())
                self.assertNotIn("SCHEDULED EXECUTION CONTEXT", exported)
                self.assertIsNone(agent._SCHEDULED_EXECUTION_CONTEXT.get())

    def test_later_genuine_and_two_similar_user_reminders_remain_allowed(self):
        scheduled_agent, _ = self._agent_with([
            _schedule_call("OSIA-WOR117-blocked"),
            "OSIA-WOR117-executed.",
        ])
        scheduled_agent.run_scheduled(
            "Remind me to return OSIA-WOR117-executed.",
            _execution_context(
                "cron_wor117_existing",
                "once:2026-07-15T00:00",
                recurring=False,
            ),
            history=[],
        )
        self.assertEqual(len(self.store["cron_jobs"]), 0)

        scheduled_agent.client, _ = _client(
            _schedule_call("OSIA-WOR117-genuine"),
            "Scheduled.",
        )
        later = scheduled_agent.run(
            "Remind me every day to return OSIA-WOR117-genuine.",
            history=[],
        )
        self.assertEqual(later, "Scheduled.")
        self.assertEqual(len(self.store["cron_jobs"]), 1)

        for suffix in ("one", "two"):
            result = scheduled_tasks.create_scheduled_task(
                title=f"OSIA-WOR117-similar-{suffix}",
                instruction="Return the same legitimate marker.",
                schedule={"type": "daily", "time": "18:00"},
            )
            self.assertTrue(result["ok"])
        self.assertEqual(len(self.store["cron_jobs"]), 3)

    def test_one_shot_executes_once_without_child_and_cleans_context(self):
        self.store["cron_jobs"] = [_job(
            "cron_wor117_oneshot",
            "Remind me to return OSIA-WOR117-ONESHOT-PASS.",
            "once:2026-07-15T00:00",
            "2026-07-15T00:00:00",
        )]
        osia, _ = self._agent_with([
            _schedule_call("OSIA-WOR117-oneshot-child"),
            "OSIA-WOR117-ONESHOT-PASS.",
        ])
        executions = []

        def run_task(payload, _conversation_id, *, model=None, scheduled_execution=None):
            reply = osia.run_scheduled(payload, scheduled_execution, history=[])
            executions.append((copy.deepcopy(scheduled_execution), reply))
            return {"status": "completed"}

        manager = cron.CronManager(run_task)
        manager._check_jobs(datetime(2026, 7, 15, 1, 0))
        self._wait(manager)
        jobs = self.fake_get("cron_jobs", [])
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0][1], "OSIA-WOR117-ONESHOT-PASS.")
        self.assertEqual(len(jobs), 1)
        self.assertFalse(jobs[0]["enabled"])
        self.assertEqual(jobs[0]["last_status"], "completed")
        self.assertEqual(executions[0][0]["source"], "scheduled_task")
        self.assertEqual(executions[0][0]["task_id"], "cron_wor117_oneshot")
        self.assertFalse(executions[0][0]["recurrence"]["recurring"])
        self.assertEqual(
            jobs[0]["last_occurrence_id"],
            executions[0][0]["occurrence_id"],
        )
        self.assertFalse(manager._busy.is_set())
        self.assertIsNone(agent._SCHEDULED_EXECUTION_CONTEXT.get())

    def test_recurring_occurrence_preserves_one_task_and_restart_does_not_replay(self):
        original_schedule = "every 1 minutes"
        self.store["cron_jobs"] = [_job(
            "cron_wor117_recurring",
            "Remind me to return OSIA-WOR117-RECURRING-PASS.",
            original_schedule,
            "2026-07-15T01:00:00",
        )]
        osia, _ = self._agent_with([
            _schedule_call("OSIA-WOR117-recurring-child"),
            "OSIA-WOR117-RECURRING-PASS.",
        ])
        executions = []

        def run_task(payload, _conversation_id, *, model=None, scheduled_execution=None):
            reply = osia.run_scheduled(payload, scheduled_execution, history=[])
            executions.append((scheduled_execution["occurrence_id"], reply))
            return {"status": "completed"}

        now = datetime(2026, 7, 15, 1, 0)
        manager = cron.CronManager(run_task)
        manager._check_jobs(now)
        self._wait(manager)
        jobs = self.fake_get("cron_jobs", [])
        self.assertEqual(len(executions), 1)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["enabled"])
        self.assertEqual(jobs[0]["schedule"], original_schedule)
        self.assertEqual(jobs[0]["last_status"], "completed")

        restarted = cron.CronManager(run_task)
        restarted._check_jobs(now)
        self._wait(restarted)
        self.assertEqual(len(executions), 1)
        self.assertEqual(len(self.fake_get("cron_jobs", [])), 1)
        self.assertFalse(restarted._busy.is_set())

    def test_failed_recurring_execution_preserves_original_without_duplicate(self):
        original_schedule = "every 5 minutes"
        self.store["cron_jobs"] = [_job(
            "cron_wor117_failed",
            "Return OSIA-WOR117-FAILED.",
            original_schedule,
            "2026-07-15T01:00:00",
        )]
        contexts = []

        def fail_task(_payload, _conversation_id, *, model=None, scheduled_execution=None):
            contexts.append(copy.deepcopy(scheduled_execution))
            return {"status": "failed", "message": "synthetic failure"}

        manager = cron.CronManager(fail_task)
        manager._check_jobs(datetime(2026, 7, 15, 1, 0))
        self._wait(manager)
        jobs = self.fake_get("cron_jobs", [])
        self.assertEqual(len(contexts), 1)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["enabled"])
        self.assertEqual(jobs[0]["schedule"], original_schedule)
        self.assertEqual(jobs[0]["last_status"], "error")
        self.assertFalse(manager._busy.is_set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
