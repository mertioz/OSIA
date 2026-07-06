#!/usr/bin/env python3
"""Focused smoke tests for chat-created scheduled tasks."""

from __future__ import annotations

import copy
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia import scheduled_tasks
from osia.cron import parse_schedule


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


def test_cron_parser_extension():
    spec = parse_schedule("every 3 days at 20:00")
    check("parser day interval kind", spec["kind"] == "day_interval_at")
    check("parser day interval days", spec["days"] == 3)
    check("parser day interval time", (spec["hour"], spec["minute"]) == (20, 0))


def main():
    test_create_daily_once_and_day_interval()
    test_rejections()
    test_list_update_delete_and_legacy_upgrade()
    test_cron_parser_extension()
    print("scheduled task tests passed")


if __name__ == "__main__":
    main()
