#!/usr/bin/env python3
"""Focused smoke tests for chat-created scheduled tasks."""

from __future__ import annotations

import copy
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia import conversations, scheduled_tasks
from osia import cron as cron_module
from osia.cron import CronManager, parse_schedule


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


@contextmanager
def in_memory_cron_runtime(initial=None):
    store = {"cron_jobs": copy.deepcopy(initial or [])}
    orig_get = cron_module.get_setting
    orig_set = cron_module.set_setting
    orig_scheduled_get = scheduled_tasks.config.get_setting
    orig_scheduled_set = scheduled_tasks.config.set_setting

    def fake_get(key, default=None):
        return copy.deepcopy(store.get(key, default))

    def fake_set(key, value):
        store[key] = copy.deepcopy(value)

    cron_module.get_setting = fake_get
    cron_module.set_setting = fake_set
    scheduled_tasks.config.get_setting = fake_get
    scheduled_tasks.config.set_setting = fake_set
    try:
        yield store
    finally:
        cron_module.get_setting = orig_get
        cron_module.set_setting = orig_set
        scheduled_tasks.config.get_setting = orig_scheduled_get
        scheduled_tasks.config.set_setting = orig_scheduled_set


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print(f"OK: {label}")


def test_create_daily_once_and_day_interval():
    now = datetime(2026, 7, 6, 12, 0)
    with in_memory_cron_jobs() as store:
        daily = scheduled_tasks.create_scheduled_task(
            title="Daily summary",
            instruction="Do X",
            schedule={"type": "daily", "time": "18:00"},
            now=now,
        )
        check("daily create ok", daily["ok"] is True)
        check("daily schedule", daily["job"]["schedule"] == "at 18:00")
        check("daily id", daily["task_id"].startswith("cron_"))

        once = scheduled_tasks.create_scheduled_task(
            title="One shot",
            instruction="Do Y",
            schedule={"type": "once", "run_at": "2026-07-15T09:00:00"},
            now=now,
        )
        check("once create ok", once["ok"] is True)
        check("once schedule", once["job"]["schedule"] == "once:2026-07-15T09:00")

        day_at = scheduled_tasks.create_scheduled_task(
            title="Every 3 days",
            instruction="Do Z",
            schedule={
                "type": "interval",
                "every": 3,
                "unit": "days",
                "time": "20:00",
            },
            now=now,
        )
        check("day interval at create ok", day_at["ok"] is True)
        check(
            "day interval at schedule",
            day_at["job"]["schedule"] == "every 3 days at 20:00",
        )
        check("three jobs persisted", len(store["cron_jobs"]) == 3)


def test_natural_schedule_parsing_and_next_run():
    now = datetime(2026, 7, 6, 15, 4)
    with in_memory_cron_jobs() as store:
        soon = scheduled_tasks.create_scheduled_task(
            title="Soon",
            instruction="Do soon",
            schedule="dans 2 minutes",
            now=now,
        )
        check("natural once ok", soon["ok"] is True)
        check("natural once schedule", soon["job"]["schedule"] == "once:2026-07-06T15:06")
        check("natural once next", soon["job"]["next_run_at"] == "2026-07-06T15:06")

        daily = scheduled_tasks.create_scheduled_task(
            title="Daily FR",
            instruction="Do daily",
            schedule="tous les jours à 15h06",
            now=now,
        )
        check("natural daily ok", daily["ok"] is True)
        check("natural daily schedule", daily["job"]["schedule"] == "at 15:06")
        check("natural daily next today", daily["job"]["next_run_at"] == "2026-07-06T15:06")

        day_at = scheduled_tasks.create_scheduled_task(
            title="Every 3 days FR",
            instruction="Do day interval",
            schedule="tous les 3 jours à 20h",
            now=now,
        )
        check("natural day interval ok", day_at["ok"] is True)
        check("natural day interval schedule", day_at["job"]["schedule"] == "every 3 days at 20:00")
        check("natural jobs persisted", len(store["cron_jobs"]) == 3)


def test_daily_next_run_ahead_and_passed():
    ahead = scheduled_tasks.next_run_at_for_schedule(
        "at 15:06",
        now=datetime(2026, 7, 6, 15, 4),
    )
    passed = scheduled_tasks.next_run_at_for_schedule(
        "at 15:06",
        now=datetime(2026, 7, 6, 15, 7),
    )
    check("daily next run ahead today", ahead == "2026-07-06T15:06")
    check("daily next run passed tomorrow", passed == "2026-07-07T15:06")


def test_interval_next_runs():
    now = datetime(2026, 7, 6, 12, 0)
    check(
        "every minutes next",
        scheduled_tasks.next_run_at_for_schedule("every 5 minutes", now=now)
        == "2026-07-06T12:05",
    )
    check(
        "every hours next",
        scheduled_tasks.next_run_at_for_schedule("every 2 hours", now=now)
        == "2026-07-06T14:00",
    )
    check(
        "every days next",
        scheduled_tasks.next_run_at_for_schedule("every 2 days", now=now)
        == "2026-07-08T12:00",
    )
    check(
        "every days at next today",
        scheduled_tasks.next_run_at_for_schedule("every 2 days at 20:00", now=now)
        == "2026-07-06T20:00",
    )


def test_rejections():
    now = datetime(2026, 7, 6, 12, 0)
    with in_memory_cron_jobs():
        past = scheduled_tasks.create_scheduled_task(
            title="Past",
            instruction="Do it",
            schedule={"type": "once", "run_at": "2026-07-01T09:00:00"},
            now=now,
        )
        check("past once rejected", past["ok"] is False)
        check("past code", past["code"] == scheduled_tasks.ERROR_VALIDATION)

        invalid = scheduled_tasks.create_scheduled_task(
            title="Bad interval",
            instruction="Do it",
            schedule={"type": "interval", "every": 0, "unit": "days"},
            now=now,
        )
        check("invalid recurrence rejected", invalid["ok"] is False)

        weekly = scheduled_tasks.create_scheduled_task(
            title="Weekly",
            instruction="Do it",
            schedule={"type": "weekly", "days_of_week": ["mon"], "time": "09:00"},
            now=now,
        )
        check("weekly unsupported", weekly["code"] == scheduled_tasks.ERROR_UNSUPPORTED)

        secret = scheduled_tasks.create_scheduled_task(
            title="Secret",
            instruction="Use api_key=sk-" + ("a" * 30),
            schedule={"type": "daily", "time": "18:00"},
            now=now,
        )
        check("secret rejected", secret["code"] == scheduled_tasks.ERROR_SECRET)

        tag = scheduled_tasks.create_scheduled_task(
            title="Tag",
            instruction="<tool_call>{\"tool\":\"bash\"}</tool_call>",
            schedule={"type": "daily", "time": "18:00"},
            now=now,
        )
        check("tool tag rejected", tag["code"] == scheduled_tasks.ERROR_VALIDATION)


def test_conversation_resolution_for_chat_tools():
    now = datetime(2026, 7, 6, 12, 0)
    with temporary_conversations(), in_memory_cron_jobs() as store:
        conv = conversations.create_conversation(title="Pain au chocolat")
        by_id = scheduled_tasks.create_scheduled_task(
            title="By id",
            instruction="Do id",
            schedule={"type": "daily", "time": "18:00"},
            conversation_id=conv["id"],
            now=now,
        )
        check("conversation id accepted", by_id["ok"] is True)
        check("conversation id stored", by_id["job"]["conversation_id"] == conv["id"])

        by_title = scheduled_tasks.create_scheduled_task(
            title="By title",
            instruction="Do title",
            schedule={"type": "daily", "time": "18:00"},
            conversation_title="Pain au chocolat",
            now=now,
        )
        check("conversation title accepted", by_title["ok"] is True)
        check("conversation title stored", by_title["job"]["conversation_id"] == conv["id"])

        by_query = scheduled_tasks.create_scheduled_task(
            title="By query",
            instruction="Do query",
            schedule={"type": "daily", "time": "18:00"},
            conversation_query="chat pain chocolat",
            now=now,
        )
        check("conversation query accepted", by_query["ok"] is True)
        check("conversation query stored", by_query["job"]["conversation_id"] == conv["id"])

        missing = scheduled_tasks.create_scheduled_task(
            title="Missing",
            instruction="Do missing",
            schedule={"type": "daily", "time": "18:00"},
            conversation_query="definitely absent",
            now=now,
        )
        check("missing conversation rejected", missing["ok"] is False)
        check(
            "missing conversation code",
            missing["code"] == scheduled_tasks.ERROR_CONVERSATION_NOT_FOUND,
        )

        conversations.create_conversation(title="Duplicate")
        conversations.create_conversation(title="Duplicate")
        ambiguous = scheduled_tasks.create_scheduled_task(
            title="Ambiguous",
            instruction="Do ambiguous",
            schedule={"type": "daily", "time": "18:00"},
            conversation_title="Duplicate",
            now=now,
        )
        check("ambiguous conversation rejected", ambiguous["ok"] is False)
        check(
            "ambiguous conversation code",
            ambiguous["code"] == scheduled_tasks.ERROR_CONVERSATION_AMBIGUOUS,
        )
        check("ambiguous candidates", len(ambiguous.get("candidates", [])) == 2)
        check("only successful jobs persisted", len(store["cron_jobs"]) == 3)


def test_list_update_delete_and_legacy_upgrade():
    legacy = [{
        "task": "Legacy task",
        "schedule": "every 2 hours",
        "enabled": True,
        "conversation_id": None,
        "custom": "keep-me",
    }]
    with in_memory_cron_jobs(legacy) as store:
        listed = scheduled_tasks.list_scheduled_tasks()
        check("list ok", listed["ok"] is True)
        check("legacy upgraded", listed["tasks"][0]["id"].startswith("cron_"))
        check("legacy metadata preserved", listed["tasks"][0]["custom"] == "keep-me")
        task_id = listed["tasks"][0]["id"]

        updated = scheduled_tasks.update_scheduled_task(
            task_id=task_id,
            title="Updated",
            instruction="Updated task",
            enabled=False,
        )
        check("update ok", updated["ok"] is True)
        check("stable id preserved", updated["job"]["id"] == task_id)
        check("unknown field preserved", updated["job"]["custom"] == "keep-me")
        check("enabled changed", updated["job"]["enabled"] is False)

        deleted = scheduled_tasks.delete_scheduled_task(task_id=task_id)
        check("delete ok", deleted["ok"] is True)
        check("deleted from store", store["cron_jobs"] == [])


def test_change_callbacks_for_chat_and_settings_paths():
    calls = []
    with in_memory_cron_jobs() as store:
        unsubscribe = scheduled_tasks.add_change_callback(lambda: calls.append("wake"))
        try:
            created = scheduled_tasks.create_scheduled_task(
                title="Wake chat",
                instruction="Do wake",
                schedule={"type": "daily", "time": "17:00"},
                now=datetime(2026, 7, 6, 12, 0),
            )
            check("chat path create ok", created["ok"] is True)
            check("chat path callback", len(calls) == 1)

            replaced = scheduled_tasks.replace_scheduled_tasks_from_settings(
                store["cron_jobs"],
                source_default="mobile_settings",
            )
            check("settings path replace ok", replaced["ok"] is True)
            check("settings path callback", len(calls) == 2)
        finally:
            unsubscribe()


def test_per_task_model_resolution():
    now = datetime(2026, 7, 6, 12, 0)
    with in_memory_cron_jobs():
        defaulted = scheduled_tasks.create_scheduled_task(
            title="Default model",
            instruction="Do model",
            schedule={"type": "daily", "time": "18:00"},
            active_model={"provider": "openai", "id": "gpt-5.5"},
            now=now,
        )
        check("default model create ok", defaulted["ok"] is True)
        check("default model stored", defaulted["job"]["model"]["id"] == "gpt-5.5")

        explicit = scheduled_tasks.create_scheduled_task(
            title="Explicit model",
            instruction="Do opus",
            schedule={"type": "daily", "time": "18:00"},
            model_query="opus 4.8",
            now=now,
        )
        check("explicit model create ok", explicit["ok"] is True)
        check("explicit model provider", explicit["job"]["model"]["provider"] == "anthropic")

        missing = scheduled_tasks.create_scheduled_task(
            title="Missing model",
            instruction="Do missing",
            schedule={"type": "daily", "time": "18:00"},
            model_query="definitely-not-a-real-model",
            now=now,
        )
        check("missing model rejected", missing["code"] == scheduled_tasks.ERROR_MODEL_NOT_FOUND)

        ambiguous = scheduled_tasks.create_scheduled_task(
            title="Ambiguous model",
            instruction="Do ambiguous",
            schedule={"type": "daily", "time": "18:00"},
            model_query="gpt",
            now=now,
        )
        check("ambiguous model rejected", ambiguous["code"] == scheduled_tasks.ERROR_MODEL_AMBIGUOUS)


def test_settings_model_metadata_round_trip():
    now = datetime(2026, 7, 6, 12, 0)
    active_model = {"provider": "openai", "id": "gpt-5.5", "label": "GPT-5.5"}
    with in_memory_cron_jobs():
        created = scheduled_tasks.replace_scheduled_tasks_from_settings(
            [{
                "task": "Settings task",
                "schedule": "at 18:00",
                "enabled": True,
                "conversation_id": "conv-a",
                "model": active_model,
                "next_run_at": "2026-07-06T18:00",
            }],
            source_default="settings",
        )
        check("settings model default write ok", created["ok"] is True)
        job = created["jobs"][0]
        check("settings model default stored", job["model"]["id"] == "gpt-5.5")
        check("settings conversation preserved", job["conversation_id"] == "conv-a")

        edited = dict(job)
        edited["task"] = "Settings task edited"
        preserved = scheduled_tasks.replace_scheduled_tasks_from_settings(
            [edited],
            source_default="settings",
        )
        check("settings edit preserve ok", preserved["ok"] is True)
        check("settings edit preserves model", preserved["jobs"][0]["model"]["id"] == "gpt-5.5")

        updated = dict(preserved["jobs"][0])
        updated["model"] = {
            "provider": "anthropic",
            "id": "claude-opus-4-8",
            "label": "Claude Opus 4.8",
        }
        changed = scheduled_tasks.replace_scheduled_tasks_from_settings(
            [updated],
            source_default="settings",
        )
        check("settings edit update model ok", changed["ok"] is True)
        check("settings edit updates model", changed["jobs"][0]["model"]["id"] == "claude-opus-4-8")

        legacy = scheduled_tasks.replace_scheduled_tasks_from_settings(
            [{
                "task": "Legacy no model",
                "schedule": "every 1 hours",
                "enabled": True,
                "next_run_at": "2026-07-06T13:00",
            }],
            source_default="settings",
        )
        check("settings legacy no model ok", legacy["ok"] is True)
        check("settings legacy no model preserved", "model" not in legacy["jobs"][0])

        bad = scheduled_tasks.create_scheduled_task(
            title="Bad model",
            instruction="Do bad model",
            schedule={"type": "daily", "time": "18:00"},
            model_query="not a real settings model",
            now=now,
        )
        check("invalid model selection rejected", bad["ok"] is False)
        check("invalid model selection code", bad["code"] == scheduled_tasks.ERROR_MODEL_NOT_FOUND)


def test_cron_parser_extension():
    spec = parse_schedule("every 3 days at 20:00")
    check("parser day interval kind", spec["kind"] == "day_interval_at")
    check("parser day interval days", spec["days"] == 3)
    check("parser day interval time", (spec["hour"], spec["minute"]) == (20, 0))


def test_cron_notification_callback_and_status():
    job = {
        "id": "cron_test_notification",
        "title": "Notify me",
        "task": "Return notification",
        "schedule": "every 1 minutes",
        "enabled": True,
    }
    notifications = []
    with in_memory_cron_runtime([job]) as store:
        manager = CronManager(
            lambda _task, _conv: {"status": "completed"},
            notify_task=lambda **payload: notifications.append(payload),
        )
        launched = manager._fire(
            "Return notification",
            "every 1 minutes",
            {},
            datetime(2026, 7, 6, 12, 0),
            job_index=0,
            signature=("Return notification", "every 1 minutes"),
            task_id="cron_test_notification",
            title="Notify me",
        )
        check("cron fire launched", launched is True)
        deadline = time.time() + 2.0
        while not notifications and time.time() < deadline:
            time.sleep(0.02)
        check("cron notification emitted", len(notifications) == 1)
        check("cron notification status", notifications[0]["status"] == "completed")
        check("cron job status updated", store["cron_jobs"][0]["last_status"] == "completed")


def test_cron_daily_next_run_prevents_poll_miss():
    fired = []
    created_at = datetime(2026, 7, 6, 15, 5)
    job = {
        "id": "cron_test_daily",
        "title": "Daily exact",
        "task": "Return exact",
        "schedule": "at 15:06",
        "enabled": True,
        "created_at": created_at.isoformat(timespec="minutes"),
        "next_run_at": "2026-07-06T15:06",
        "next_run_schedule": "at 15:06",
    }
    with in_memory_cron_runtime([job]) as store:
        manager = CronManager(lambda task, conv, model=None: fired.append((task, conv, model)))
        manager._check_jobs(datetime(2026, 7, 6, 15, 6, 10))
        deadline = time.time() + 2.0
        while not fired and time.time() < deadline:
            time.sleep(0.02)
        check("daily fires after first late poll", len(fired) == 1)
        check("daily next run tomorrow", store["cron_jobs"][0]["next_run_at"] == "2026-07-07T15:06")


def test_cron_passes_model_and_legacy_without_model_runs():
    fired = []
    with_model = {
        "id": "cron_test_model",
        "title": "With model",
        "task": "Return model",
        "schedule": "once:2026-07-06T12:00",
        "enabled": True,
        "next_run_at": "2026-07-06T12:00",
        "model": {"provider": "openai", "id": "gpt-5.5", "label": "GPT-5.5"},
    }
    with in_memory_cron_runtime([with_model]):
        manager = CronManager(lambda task, conv, model=None: fired.append((task, model)))
        manager._check_jobs(datetime(2026, 7, 6, 12, 0))
        deadline = time.time() + 2.0
        while len(fired) < 1 and time.time() < deadline:
            time.sleep(0.02)
        check("model task fired", fired[0][1]["id"] == "gpt-5.5")

    fired.clear()
    legacy = {
        "id": "cron_test_legacy",
        "title": "Legacy",
        "task": "Return legacy",
        "schedule": "once:2026-07-06T12:00",
        "enabled": True,
        "next_run_at": "2026-07-06T12:00",
    }
    with in_memory_cron_runtime([legacy]):
        manager = CronManager(lambda task, conv, model=None: fired.append((task, model)))
        manager._check_jobs(datetime(2026, 7, 6, 12, 0))
        deadline = time.time() + 2.0
        while len(fired) < 1 and time.time() < deadline:
            time.sleep(0.02)
        check("legacy task fired without model", fired[0][1] is None)


def main():
    test_create_daily_once_and_day_interval()
    test_natural_schedule_parsing_and_next_run()
    test_daily_next_run_ahead_and_passed()
    test_interval_next_runs()
    test_rejections()
    test_conversation_resolution_for_chat_tools()
    test_list_update_delete_and_legacy_upgrade()
    test_change_callbacks_for_chat_and_settings_paths()
    test_per_task_model_resolution()
    test_settings_model_metadata_round_trip()
    test_cron_parser_extension()
    test_cron_notification_callback_and_status()
    test_cron_daily_next_run_prevents_poll_miss()
    test_cron_passes_model_and_legacy_without_model_runs()
    print("scheduled task tests passed")


if __name__ == "__main__":
    main()
