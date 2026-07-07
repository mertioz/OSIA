#!/usr/bin/env python3
"""Focused regression tests for scheduled-task agent tool dispatch."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import psutil  # noqa: F401
except ModuleNotFoundError:
    sys.modules["psutil"] = SimpleNamespace()

try:
    import openai  # noqa: F401
except ModuleNotFoundError:
    sys.modules["openai"] = SimpleNamespace(
        OpenAI=lambda **_kwargs: SimpleNamespace()
    )

from osia import agent, conversations, scheduled_tasks


@contextmanager
def in_memory_cron_jobs(initial=None):
    store = {"cron_jobs": copy.deepcopy(initial or [])}
    orig_get = scheduled_tasks.config.get_setting
    orig_set = scheduled_tasks.config.set_setting
    orig_cb = scheduled_tasks._CHANGE_CALLBACK

    def fake_get(key, default=None):
        return copy.deepcopy(store.get(key, default))

    def fake_set(key, value):
        store[key] = copy.deepcopy(value)

    scheduled_tasks.config.get_setting = fake_get
    scheduled_tasks.config.set_setting = fake_set
    scheduled_tasks.set_change_callback(None)
    try:
        yield store
    finally:
        scheduled_tasks.config.get_setting = orig_get
        scheduled_tasks.config.set_setting = orig_set
        scheduled_tasks.set_change_callback(orig_cb)


@contextmanager
def temporary_conversations():
    orig_dir = conversations.CONVERSATIONS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        conversations.CONVERSATIONS_DIR = Path(tmp)
        try:
            yield
        finally:
            conversations.CONVERSATIONS_DIR = orig_dir


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print(f"OK: {label}")


class FakeCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            content = (
                "<tool_call>\n"
                '{"tool":"create_scheduled_task",'
                '"title":"Ghost folder",'
                '"instruction":"Rappeler le dossier ghost sur mon bureau.",'
                '"schedule":{"type":"daily","time":"17:00"},'
                '"enabled":true,'
                '"source":"chat"}'
                "\n</tool_call>"
            )
        else:
            content = "C'est planifie."
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCompletions())


def test_agent_tool_wrappers_return_structured_results():
    with in_memory_cron_jobs() as store:
        ok_raw = agent.tool_create_scheduled_task(
            title="Daily task",
            instruction="Do X",
            schedule={"type": "daily", "time": "17:00"},
        )
        ok = json.loads(ok_raw)
        check("wrapper ok true", ok["ok"] is True)
        check("wrapper task id", ok["task_id"].startswith("cron_"))

        bad_raw = agent.tool_create_scheduled_task(
            title="Bad task",
            instruction="Do Y",
            schedule={"type": "weekly", "days_of_week": ["mon"], "time": "09:00"},
        )
        bad = json.loads(bad_raw)
        check("wrapper ok false", bad["ok"] is False)
        check("wrapper error code", "code" in bad)
        check("one job persisted", len(store["cron_jobs"]) == 1)


def test_run_turn_scheduled_task_dispatch_has_module_alias():
    with temporary_conversations(), in_memory_cron_jobs() as store:
        conv = conversations.create_conversation(title="Current chat")
        osia = agent.OSIAAgent(model="test-model", lang="fr")
        osia.provider = "ollama"
        osia.client = FakeClient()

        reply = osia._run_turn(
            "Rappelle-moi tous les jours a 17h le dossier ghost sur mon bureau.",
            history=[],
            current_conversation_id=conv["id"],
        )

        check("run turn final reply", "planifie" in reply.lower())
        check("run turn created job", len(store["cron_jobs"]) == 1)
        check("run turn schedule", store["cron_jobs"][0]["schedule"] == "at 17:00")
        check(
            "run turn default conversation",
            store["cron_jobs"][0]["conversation_id"] == conv["id"],
        )


def test_explicit_conversation_title_tool_arg():
    with temporary_conversations(), in_memory_cron_jobs() as store:
        conv = conversations.create_conversation(title="Pain au chocolat")
        raw = agent.tool_create_scheduled_task(
            title="Targeted task",
            instruction="Do X",
            schedule={"type": "daily", "time": "17:00"},
            conversation_title="Pain au chocolat",
        )
        result = json.loads(raw)
        check("explicit title ok", result["ok"] is True)
        check("explicit title linked", result["job"]["conversation_id"] == conv["id"])
        check("explicit title persisted", store["cron_jobs"][0]["conversation_id"] == conv["id"])

        bad_raw = agent.tool_create_scheduled_task(
            title="Missing target",
            instruction="Do Y",
            schedule={"type": "daily", "time": "17:00"},
            conversation_query="not a chat",
        )
        bad = json.loads(bad_raw)
        check("missing explicit target rejected", bad["ok"] is False)
        check("missing explicit target code", bad["code"] == "conversation_not_found")


def main():
    test_agent_tool_wrappers_return_structured_results()
    test_run_turn_scheduled_task_dispatch_has_module_alias()
    test_explicit_conversation_title_tool_arg()
    print("scheduled task agent tool tests passed")


if __name__ == "__main__":
    main()
