"""WOR-195/WOR-301 scheduled-task execution integrity.

Scheduled tasks execute with permanent Full Access: no authorization request,
no grant, no permit.  Retained here: canonical definition hashing, exact
revision/occurrence binding, disabled/blocked preflight, OUTCOME_UNKNOWN
durability, universal destruction barriers, sanitized mobile payloads.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta

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
    monkeypatch.setattr(scheduled_tasks.config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(scheduled_tasks.config, "_ensure_config_dir", lambda: None)
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


def test_universal_destruction_barrier_still_blocks(scheduled_runtime, tmp_path, monkeypatch):
    from osia import system_resources

    registry = system_resources.SystemResourceRegistry(
        platform_family="windows",
        windows=system_resources.WindowsRuntimeAdapter(
            storage_rows=[{"DiskNumber": 0, "PartitionNumber": 1, "IsSystem": True}],
        ),
    )
    monkeypatch.setattr(system_resources, "get_default_system_resource_registry", lambda: registry)
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


def test_cron_editor_revision_ignores_run_metadata_but_rejects_stale_definitions(
    scheduled_runtime,
):
    created = _create_sensitive()
    jobs = scheduled_tasks.scheduler_jobs_snapshot()
    revision = scheduled_tasks.cron_jobs_revision(jobs)
    execution = _execution(_current(created["id"]), "occurrence:test")
    assert scheduled_tasks.update_scheduled_occurrence_status(
        execution, last_status="running", next_run_at="2026-07-27T12:10:00",
    )
    assert scheduled_tasks.cron_jobs_revision(scheduled_tasks.scheduler_jobs_snapshot()) == revision

    jobs[0]["title"] = "Edited from the first editor"
    saved = scheduled_tasks.replace_scheduled_tasks_from_settings(
        jobs, expected_cron_jobs_revision=revision,
    )
    assert saved["ok"]
    assert saved["jobs"][0]["last_status"] == "running"
    assert saved["cron_jobs_revision"] != revision
    second = _create_sensitive(35)

    conflict = scheduled_tasks.replace_scheduled_tasks_from_settings(
        [], expected_cron_jobs_revision=revision,
    )
    assert conflict["code"] == scheduled_tasks.ERROR_SETTINGS_CONFLICT
    assert [job["id"] for job in conflict["jobs"]] == [created["id"], second["id"]]
    assert conflict["jobs"][0]["title"] == "Edited from the first editor"
    assert conflict["jobs"] == scheduled_tasks.scheduler_jobs_snapshot()
    assert conflict["cron_jobs_revision"] == scheduled_tasks.cron_jobs_revision(conflict["jobs"])


@pytest.fixture
def cron_workers(monkeypatch):
    workers = []
    real_thread = threading.Thread

    def spawn(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    def join():
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=5)
                assert not worker.is_alive()

    monkeypatch.setattr(cron.threading, "Thread", spawn)
    yield join
    join()


@pytest.mark.parametrize("stage", ["admission", "finalization"])
def test_job_write_failure_does_not_strand_the_scheduler(
    scheduled_runtime, cron_workers, monkeypatch, stage,
):
    first = _create_sensitive(25)
    second = _create_sensitive(35)
    now = datetime(2026, 7, 27, 12, 5)
    executed = []

    def run_task(task, *_args):
        executed.append(task)
        return {"status": "success_verified"}

    manager = cron.CronManager(run_task)
    if stage == "admission":
        save = scheduled_tasks.config.set_setting

        def fail_running_write(key, jobs):
            if any(job["id"] == first["id"] and job.get("last_status") == "running" for job in jobs):
                raise OSError("synthetic storage failure")
            save(key, jobs)

        target, name, failing = scheduled_tasks.config, "set_setting", fail_running_write
    else:
        update = scheduled_tasks.update_scheduled_occurrence_status

        def fail_final_write(execution, **fields):
            if fields.get("last_status") == "success_verified":
                raise RuntimeError("synthetic persistence failure")
            return update(execution, **fields)

        target, name, failing = scheduled_tasks, "update_scheduled_occurrence_status", fail_final_write

    due = {"next_run": now}
    with monkeypatch.context() as failure:
        failure.setattr(target, name, failing)
        launched = manager._fire(first["task"], first["schedule"], due, now, task_id=first["id"])
        assert launched is (stage == "finalization")
        cron_workers()
    if stage == "admission":
        assert due["next_run"] == now and not executed
        assert _current(first["id"])["execution_state"] != scheduled_tasks.RUNNING

    assert manager._fire(second["task"], second["schedule"], {}, now, task_id=second["id"])
    cron_workers()
    assert executed[-1] == second["task"]


def test_edited_schedule_is_not_launched_from_an_old_due_snapshot(
    scheduled_runtime, cron_workers, monkeypatch,
):
    created = _create_sensitive()
    due = datetime(2026, 7, 27, 12, 5)
    original = _current(created["id"])
    executed = []
    manager = cron.CronManager(
        lambda task, *_args: executed.append(task) or {"status": "success_verified"}
    )

    def stale_poll():
        updated = scheduled_tasks.update_scheduled_task(
            task_id=created["id"],
            schedule={"type": "interval", "every": 1, "unit": "hours"},
            now=due,
        )
        assert updated["ok"], updated
        return [original]

    with monkeypatch.context() as poll:
        poll.setattr(scheduled_tasks, "scheduler_jobs_snapshot", stale_poll)
        manager._check_jobs(due)
        cron_workers()
    assert executed == []
    manager._check_jobs(due + timedelta(hours=1))
    cron_workers()
    assert executed == [original["task"]]


def test_worker_start_failure_leaves_occurrence_due(
    scheduled_runtime, cron_workers, monkeypatch,
):
    job = _create_sensitive()
    due = datetime(2026, 7, 27, 12, 5)
    executed = []
    manager = cron.CronManager(
        lambda task, *_args: executed.append(task) or {"status": "success_verified"}
    )

    class UnstartableWorker:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start test worker")

    with monkeypatch.context() as failure:
        failure.setattr(cron.threading, "Thread", UnstartableWorker)
        manager._check_jobs(due)
    current = _current(job["id"])
    assert cron._parse_job_datetime(current["next_run_at"]) == due
    assert current["execution_state"] != scheduled_tasks.RUNNING
    assert executed == []
    manager._check_jobs(due + timedelta(seconds=1))
    cron_workers()
    assert executed == [job["task"]]


def test_desktop_and_mobile_job_creates_keep_both_rows(tmp_path, monkeypatch):
    config = scheduled_tasks.config
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", str(path))
    monkeypatch.setattr(config, "_ensure_config_dir", lambda: None)
    config.save_settings({"cron_jobs": []})
    script = """
import sys
from osia import config, scheduled_tasks

config.SETTINGS_FILE = sys.argv[1]
config._ensure_config_dir = lambda: None
print("ready", flush=True)
result = scheduled_tasks.create_scheduled_task(
    instruction="Return mobile status",
    schedule={"type": "interval", "every": 5, "unit": "minutes"},
    source="mobile_settings",
)
if not result["ok"]:
    raise RuntimeError(result)
"""
    writer = None
    try:
        with config.settings_transaction():
            writer = subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert writer.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                writer.wait(timeout=0.25)
            _create_sensitive()
        _, err = writer.communicate(timeout=10)
        assert writer.returncode == 0, err
        assert {job["task"] for job in scheduled_tasks.scheduler_jobs_snapshot()} == {
            "Set volume to 25", "Return mobile status",
        }
    finally:
        if writer is not None and writer.poll() is None:
            writer.kill()
            writer.communicate()


@pytest.mark.parametrize("edit_before_restart", [False, True])
def test_one_shot_is_not_relaunched_when_disabling_fails(
    scheduled_runtime, cron_workers, monkeypatch, edit_before_restart,
):
    now = datetime(2026, 7, 27, 12, 0)
    due = now + timedelta(minutes=5)
    result = scheduled_tasks.create_scheduled_task(
        title="One-shot result",
        instruction="Return status",
        schedule={"type": "once", "run_at": due.isoformat()},
        enabled=True,
        now=now,
    )
    assert result["ok"]
    runs = []

    def fail_disable(*_args, **_kwargs):
        raise OSError("synthetic storage failure")

    def run_task(task, *_args):
        runs.append(task)
        return {"status": "outcome_unknown"}

    manager = cron.CronManager(run_task)
    with monkeypatch.context() as failure:
        failure.setattr(scheduled_tasks, "disable_scheduled_task", fail_disable)
        manager._check_jobs(due)
        cron_workers()
        manager._check_jobs(due + timedelta(minutes=1))
        cron_workers()
    assert runs == ["Return status"]
    task_id = result["job"]["id"]
    recorded = _current(task_id)["occurrence_records"]
    assert [record["outcome"] for record in recorded] == ["outcome_unknown"]
    if edit_before_restart:
        edited = scheduled_tasks.update_scheduled_task(
            task_id=task_id, instruction="Return the edited status",
        )
        assert edited["ok"], edited

    restarted = cron.CronManager(run_task)
    restarted._check_jobs(due + timedelta(minutes=2))
    cron_workers()
    job = _current(task_id)
    assert job["enabled"] is False
    if edit_before_restart:
        assert runs == ["Return status", "Return the edited status"]
        assert len(job["occurrence_records"]) == 2
    else:
        assert runs == ["Return status"]
        assert job["occurrence_records"] == recorded


@pytest.mark.parametrize(("terminal", "outcome", "wire_status"), [
    ("success_verified", "success_verified", "completed"),
    ("blocked_verified", "blocked_verified", "failed"),
    ("hard_safety_block", "hard_safety_block", "failed"),
    ("user_cancelled", "user_cancelled", "failed"),
    ("completed", "completed_unverified", "failed"),
    (None, "outcome_unknown", "failed"),
])
def test_cron_notification_mirrors_the_terminal_result(
    scheduled_runtime, cron_workers, terminal, outcome, wire_status,
):
    from osia.server.server import _cron_notification_frame

    job = _create_sensitive()
    notices = []
    manager = cron.CronManager(
        lambda *_args: {"status": terminal},
        notify_task=lambda **payload: notices.append(_cron_notification_frame(payload)),
    )
    assert manager._fire(
        job["task"], job["schedule"], {}, datetime(2026, 7, 27, 12, 5),
        task_id=job["id"],
    )
    cron_workers()
    assert len(notices) == 1
    assert notices[0]["task_id"] == job["id"]
    assert notices[0]["outcome"] == outcome
    assert notices[0]["status"] == wire_status


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
