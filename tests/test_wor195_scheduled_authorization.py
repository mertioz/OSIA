"""WOR-195/WOR-301 scheduled-task execution integrity.

Scheduled tasks execute with permanent Full Access: no authorization request,
no grant, no permit.  Retained here: canonical definition hashing, exact
revision/occurrence binding, disabled/blocked preflight, OUTCOME_UNKNOWN
durability, universal destruction barriers, sanitized mobile payloads.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest

from osia import action_policy, cron, scheduled_tasks
from osia.action_policy import (
    ExecutionType,
    execution_context_scope,
)
from osia.tool_contracts import (
    ExecutionOutcome,
    ToolExecutionRecord,
    effective_contract,
    get_tool_spec,
)


@pytest.fixture
def scheduled_runtime(tmp_path, monkeypatch):
    values = {"cron_jobs": []}

    def fake_get(key, default=None):
        return copy.deepcopy(values.get(key, default))

    def fake_set(key, value):
        values[key] = copy.deepcopy(value)

    monkeypatch.setattr(scheduled_tasks.config, "get_setting", fake_get)
    monkeypatch.setattr(scheduled_tasks.config, "set_setting", fake_set)
    monkeypatch.setattr(cron, "get_setting", fake_get)
    monkeypatch.setattr(cron, "set_setting", fake_set)
    yield values


def _create_sensitive(level: int = 25) -> dict:
    result = scheduled_tasks.create_scheduled_task(
        title="Synthetic volume",
        instruction=f"Set volume to {level}",
        schedule={"type": "interval", "every": 5, "unit": "minutes"},
        enabled=True,
        source="settings",
        now=datetime(2026, 7, 27, 12, 0),
    )
    assert result["ok"] is True
    return result["job"]


def _execution(job: dict, occurrence: str) -> dict:
    return {
        "source": "scheduled_task",
        "task_id": job["id"],
        "task_revision": job["task_revision"],
        "definition_hash": job["definition_hash"],
        "authorization_revision": job["authorization_revision"],
        "policy_epoch": job.get("policy_epoch", 0),
        "occurrence_id": occurrence,
    }


def _current(task_id: str) -> dict:
    return scheduled_tasks.preflight_scheduled_task(task_id).get("job") or next(
        item for item in scheduled_tasks.scheduler_jobs_snapshot()
        if item["id"] == task_id
    )


def test_canonical_definition_hash_excludes_cosmetic_and_volatile_fields():
    base = {
        "task": "  Set   volume to 25  ",
        "schedule": "every 5 minutes",
        "timezone": "Europe/Paris",
        "model": {"provider": "openai", "id": "fixture"},
        "capability_constraints": [{
            "capability_id": "cap", "tool_category": "native",
            "effect_class": "reversible", "safe_category": "safe_local_setting",
            "call_binding": "a" * 64,
        }],
        "target_binding": "b" * 64,
        "conversation_id": "conversation:fixture",
        "desired_enabled": True,
        "execution_mode": "agent",
        "title": "Cosmetic A",
        "created_at": "yesterday",
        "updated_at": "now",
    }
    changed_cosmetic = {**base, "title": "Cosmetic B", "updated_at": "later"}
    assert scheduled_tasks.task_definition_hash(base) == (
        scheduled_tasks.task_definition_hash(changed_cosmetic)
    )
    for field, value in {
        "task": "Set volume to 30",
        "schedule": "every 6 minutes",
        "timezone": "UTC",
        "model": {"provider": "openai", "id": "other"},
        "target_binding": "c" * 64,
        "desired_enabled": False,
        "execution_mode": "read_only",
    }.items():
        assert scheduled_tasks.task_definition_hash({**base, field: value}) != (
            scheduled_tasks.task_definition_hash(base)
        )


def test_enabled_task_preflights_and_executes_without_grants(scheduled_runtime):
    _values = scheduled_runtime
    created = _create_sensitive()
    assert created["enabled"] is True
    assert created["desired_enabled"] is True
    assert "grant_refs" not in created
    assert "authorization_status" not in created

    job = _current(created["id"])
    assert scheduled_tasks.preflight_scheduled_task(created["id"])["ok"] is True

    for number in (1, 2):
        execution = _execution(job, f"occurrence:{number}")
        context = scheduled_tasks.execution_context_for_scheduled_occurrence(execution)
        assert context.execution_type is ExecutionType.SCHEDULED
        with execution_context_scope(context):
            # WOR-301: no mode gate exists; the only barrier is integrity.
            assert not hasattr(action_policy, "scheduled_permission_gate")
            assert action_policy.guard_tool_call(
                f"Set volume to 25", "set_volume", {"level": 25},
            ) is None


def test_edit_bumps_revisions_and_stale_occurrence_is_rejected(scheduled_runtime):
    _values = scheduled_runtime
    created = _create_sensitive()
    before = _current(created["id"])
    stale = _execution(before, "occurrence:old")
    edited = scheduled_tasks.update_scheduled_task(
        task_id=created["id"], instruction="Set volume to 30", source="settings",
    )["job"]
    assert edited["task_revision"] == before["task_revision"] + 1
    assert edited["definition_hash"] != before["definition_hash"]
    assert edited["enabled"] is True

    stale["occurrence_id"] = "occurrence:fresh-id-cannot-revive"
    with pytest.raises(scheduled_tasks.ScheduledTaskError):
        scheduled_tasks.execution_context_for_scheduled_occurrence(stale)

    # The edited revision itself remains exactly executable.
    fresh = _execution(edited, "occurrence:fresh")
    scheduled_tasks.execution_context_for_scheduled_occurrence(fresh)


def test_finishing_an_old_occurrence_cannot_disable_or_overwrite_an_edit(scheduled_runtime):
    created = _create_sensitive()
    old = _execution(_current(created["id"]), "occurrence:old")
    assert scheduled_tasks.mark_scheduled_task_running(old)
    result = scheduled_tasks.update_scheduled_task(
        task_id=created["id"], instruction="Set volume to 35",
    )
    assert result["ok"]
    scheduled_tasks.update_scheduled_occurrence_status(old, last_status="old-status")
    scheduled_tasks.mark_scheduled_task_finished(old)
    assert scheduled_tasks.disable_scheduled_task(created["id"], execution=old)["ok"] is False
    updated = _current(created["id"])
    assert updated["task"] == "Set volume to 35"
    assert updated["enabled"] and updated["last_status"] != "old-status"


def test_disable_stops_the_scheduler_without_consuming_the_schedule(
    scheduled_runtime,
):
    values = scheduled_runtime
    created = _create_sensitive()
    disabled = scheduled_tasks.disable_scheduled_task(created["id"])
    assert disabled["ok"] is True and disabled["changed"] is True
    assert disabled["task"]["enabled"] is False

    blocked = scheduled_tasks.preflight_scheduled_task(created["id"])
    assert blocked["ok"] is False
    assert blocked["execution_state"] == scheduled_tasks.DISABLED

    timing = {"next_run": datetime(2026, 7, 27, 12, 5)}
    launched = cron.CronManager(lambda *_args, **_kwargs: None)._fire(
        "Set volume to 25", "every 5 minutes", timing,
        datetime(2026, 7, 27, 12, 5), task_id=created["id"], job_index=0,
    )
    assert launched is False
    stored = next(job for job in values["cron_jobs"] if job["id"] == created["id"])
    assert stored["last_status"] == "blocked_authorization"

    # Re-enabling through an edit makes the exact new revision runnable again.
    reenabled = scheduled_tasks.update_scheduled_task(
        task_id=created["id"], enabled=True, source="settings",
    )["job"]
    assert reenabled["enabled"] is True
    assert scheduled_tasks.preflight_scheduled_task(created["id"])["ok"] is True


def test_legacy_migration_and_unknown_schema_fail_closed(scheduled_runtime):
    values = scheduled_runtime
    values["cron_jobs"] = [
        {"id": "cron_legacy_read", "title": "Read", "task": "Return status",
         "schedule": "every 5 minutes", "enabled": True},
        {"id": "cron_legacy_write", "title": "Write", "task": "Set volume to 25",
         "schedule": "every 5 minutes", "enabled": True},
        {"id": "cron_unknown_schema", "title": "Unknown", "task": "Return intact",
         "schedule": "every 5 minutes", "enabled": True, "schema_version": 999},
        {"id": "cron_corrupt_schema", "title": "Corrupt", "task": "Return intact",
         "schedule": "every 5 minutes", "enabled": True, "schema_version": "bad"},
    ]
    jobs = {job["id"]: job for job in scheduled_tasks.scheduler_jobs_snapshot()}
    assert jobs["cron_legacy_read"]["enabled"] is True
    assert jobs["cron_legacy_read"]["schema_version"] == (
        scheduled_tasks.SCHEDULED_TASK_SCHEMA_VERSION
    )
    assert "grant_refs" not in jobs["cron_legacy_read"]
    assert jobs["cron_legacy_write"]["enabled"] is True
    assert jobs["cron_legacy_write"]["task"] == "Set volume to 25"
    assert jobs["cron_unknown_schema"]["enabled"] is False
    assert jobs["cron_unknown_schema"]["schema_version"] == 999
    assert jobs["cron_corrupt_schema"]["enabled"] is False
    assert jobs["cron_corrupt_schema"]["schema_version"] == "bad"


def test_outcome_unknown_stays_factual_and_allows_current_and_later_occurrences(
    scheduled_runtime,
):
    _values = scheduled_runtime
    created = _create_sensitive()
    job = _current(created["id"])
    execution = _execution(job, "occurrence:unknown")
    context = scheduled_tasks.execution_context_for_scheduled_occurrence(execution)
    assert context.execution_type is ExecutionType.SCHEDULED
    record = ToolExecutionRecord(
        "ERROR: outcome unknown", True, ExecutionOutcome.OUTCOME_UNKNOWN,
        1, True, True,
        effective_contract(get_tool_spec("set_volume"), {"level": 25}),
    )
    scheduled_tasks.record_scheduled_tool_execution(
        execution, "set_volume", {"level": 25}, record,
    )
    current = scheduled_tasks.preflight_scheduled_task(created["id"])
    assert current["ok"] is True
    assert current["job"]["execution_records"][-1]["outcome"] == "outcome_unknown"
    assert scheduled_tasks.scheduled_execution_block_state(
        execution,
    ) == ""
    scheduled_tasks.record_scheduled_occurrence(execution, outcome="failed_before_dispatch")
    assert _current(created["id"])["occurrence_records"][-1]["outcome"] == "outcome_unknown"
    later = {**execution, "occurrence_id": "occurrence:later"}
    later_context = scheduled_tasks.execution_context_for_scheduled_occurrence(later)
    assert later_context.execution_id != context.execution_id


@pytest.mark.parametrize("desired_enabled", [True, False])
def test_legacy_ambiguity_barrier_preserves_user_enablement(scheduled_runtime, desired_enabled):
    _create_sensitive()
    job = scheduled_runtime["cron_jobs"][0]
    job.update(
        enabled=False,
        desired_enabled=desired_enabled,
        ambiguity_barriers=["legacy-marker"],
        execution_records=[{"outcome": "outcome_unknown"}],
    )
    restored = scheduled_tasks.scheduler_jobs_snapshot()[0]
    assert restored["enabled"] is desired_enabled
    assert "ambiguity_barriers" not in restored
    assert restored["execution_records"] == [{"outcome": "outcome_unknown"}]


def test_universal_destruction_barrier_still_blocks(scheduled_runtime, tmp_path):
    _values = scheduled_runtime
    target = tmp_path / "synthetic-delete-target"
    target.mkdir()
    created = scheduled_tasks.create_scheduled_task(
        title="Synthetic delete",
        instruction=f"Delete directory {target}",
        schedule={"type": "daily", "time": "12:00"},
        enabled=True,
        source="settings",
    )["job"]
    assert created["enabled"] is True and "grant_refs" not in created
    context = scheduled_tasks.execution_context_for_scheduled_occurrence(
        _execution(created, "occurrence:barrier"),
    )
    with execution_context_scope(context):
        # An ordinary non-system deletion dispatches with no authorization.
        assert action_policy.guard_tool_call(
            f"Delete directory {target}",
            "delete_directory",
            {"path": str(target)},
        ) is None
        # The absolute system-destruction barrier is the only refusal.
        assert action_policy.system_mutation_barrier(
            "run_command",
            {"command": "Remove-Partition -DiskNumber 0 -PartitionNumber 1"},
        ) == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
        assert not hasattr(action_policy, "scheduled_permission_gate")


def test_mobile_payload_and_notification_are_sanitized(scheduled_runtime):
    _values = scheduled_runtime
    sentinel = "PRIVATE_FILENAME_RECIPIENT_URL"
    created = scheduled_tasks.create_scheduled_task(
        title=sentinel,
        instruction=f"Set volume to 25 {sentinel}",
        schedule={"type": "daily", "time": "12:00"},
        enabled=True,
        source="settings",
    )["job"]
    mobile = scheduled_tasks.sanitized_mobile_jobs()
    serialized = json.dumps(mobile)
    assert sentinel not in serialized
    assert created["task"] not in serialized
    for forbidden in ("grant_refs", "scope_hash", "call_binding", "arguments"):
        assert forbidden not in serialized
    assert mobile[0]["task_redacted"] is True

    tampered = {
        **created,
        "schedule": sentinel,
        "timezone": sentinel,
        "model": {"provider": sentinel, "id": f"https://{sentinel}"},
        "capability_constraints": [{
            "safe_category": sentinel,
            "tool_category": sentinel,
        }],
    }
    assert sentinel not in json.dumps(
        scheduled_tasks.sanitized_mobile_jobs([tampered])
    )

    from osia.server.server import _cron_notification_frame

    frame = _cron_notification_frame({
        "task_id": created["id"], "title": sentinel,
        "status": "failed", "safe_category": "safe_local_setting",
    })
    assert sentinel not in json.dumps(frame)
    assert frame["title"] == "Scheduled task"
    assert frame["safe_category"] == "safe_local_setting"


def test_windows_volume_success_is_verified(monkeypatch):
    from osia import agent
    from osia.tool_contracts import (
        ExecutionOutcome,
        effective_contract,
        get_tool_spec,
    )

    monkeypatch.setattr(agent.sys, "platform", "win32")
    monkeypatch.setattr(agent, "_set_windows_volume_percent", lambda pct: pct)

    spec = get_tool_spec("set_volume")
    assert spec is not None and spec.verified_success
    record = agent._execute_tool_once_record(
        "set_volume",
        {"level": 31},
        timeout_s=5,
        contract=effective_contract(spec, {"level": 31}),
    )
    assert record.result == "Volume set to 31%"
    assert record.outcome is ExecutionOutcome.SUCCESS_VERIFIED
