from __future__ import annotations

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from osia import cron, scheduled_tasks
from osia.action_policy import (
    DecisionType,
    ExecutionType,
    PermissionMode,
    PermissionPolicy,
    PermitError,
    build_permission_request,
    execution_context_scope,
    set_default_permission_policy,
    trusted_deletion_classification,
)
from osia.permission_store import (
    PermissionStore,
    UnavailableSecureStorageBackend,
    WindowsDPAPISecureStorageBackend,
)
from osia.tool_contracts import ExecutionOutcome, ToolExecutionRecord


@pytest.fixture
def scheduled_runtime(tmp_path, monkeypatch):
    backend = WindowsDPAPISecureStorageBackend()
    if not backend.availability.available:
        pytest.skip("protected recurring-grant persistence requires DPAPI")
    values = {"cron_jobs": []}

    def fake_get(key, default=None):
        return copy.deepcopy(values.get(key, default))

    def fake_set(key, value):
        values[key] = copy.deepcopy(value)

    monkeypatch.setattr(scheduled_tasks.config, "get_setting", fake_get)
    monkeypatch.setattr(scheduled_tasks.config, "set_setting", fake_set)
    monkeypatch.setattr(cron, "get_setting", fake_get)
    monkeypatch.setattr(cron, "set_setting", fake_set)
    scheduled_tasks._PENDING_AUTHORIZATIONS.clear()
    scheduled_tasks._RESPONDED_AUTHORIZATIONS.clear()
    store = PermissionStore(tmp_path / "permissions.dat", backend=backend)
    policy = PermissionPolicy(store)
    set_default_permission_policy(policy)
    try:
        yield values, policy
    finally:
        scheduled_tasks._PENDING_AUTHORIZATIONS.clear()
        scheduled_tasks._RESPONDED_AUTHORIZATIONS.clear()
        set_default_permission_policy(None)


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


def _authorize(task_id: str, *, origin: str = "desktop", **kwargs) -> dict:
    begun = scheduled_tasks.begin_scheduled_task_authorization(
        task_id, origin_ref=origin, **kwargs,
    )
    assert begun["ok"] is True
    request = dict(begun["request"])
    request["action"] = "approve"
    result = scheduled_tasks.respond_scheduled_task_authorization(
        request, authenticated_origin=origin,
    )
    assert result["ok"] is True
    return request


def _execution(job: dict, occurrence: str) -> dict:
    return {
        "source": "scheduled_task",
        "task_id": job["id"],
        "task_revision": job["task_revision"],
        "definition_hash": job["definition_hash"],
        "authorization_revision": job["authorization_revision"],
        "policy_epoch": job["policy_epoch"],
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


def test_authorize_before_enable_exact_recurring_and_two_occurrences(
    scheduled_runtime,
):
    _values, policy = scheduled_runtime
    created = _create_sensitive()
    assert created["enabled"] is False
    assert created["authorization_status"] == scheduled_tasks.AUTHORIZATION_REQUIRED
    assert scheduled_tasks.preflight_scheduled_task(created["id"])["ok"] is False

    request = _authorize(created["id"])
    job = _current(created["id"])
    assert job["enabled"] is True
    assert job["authorization_status"] == scheduled_tasks.AUTHORIZED
    assert job["grant_refs"]
    assert request["definition_hash"] == job["definition_hash"]

    for number in (1, 2):
        execution = _execution(job, f"occurrence:{number}")
        context = scheduled_tasks.execution_context_for_scheduled_occurrence(execution)
        with execution_context_scope(context):
            permission_request = build_permission_request(
                "set_volume", {"level": 25}, policy=policy,
            )
        decision = policy.evaluate(permission_request)
        assert decision.decision_type == DecisionType.ALLOW
        permit = policy.mint_permit(permission_request, decision)
        assert permit is not None
        assert policy.consume_permit(permit, permission_request).used is True


def test_edit_revision_invalidation_and_occurrence_id_is_not_permission(
    scheduled_runtime,
):
    _values, _policy = scheduled_runtime
    created = _create_sensitive()
    _authorize(created["id"])
    before = _current(created["id"])
    stale = _execution(before, "occurrence:old")
    edited = scheduled_tasks.update_scheduled_task(
        task_id=created["id"], instruction="Set volume to 30", source="settings",
    )["job"]
    assert edited["task_revision"] == before["task_revision"] + 1
    assert edited["definition_hash"] != before["definition_hash"]
    assert edited["grant_refs"] == []
    assert edited["authorization_status"] == scheduled_tasks.AUTHORIZATION_EDITED
    assert edited["enabled"] is False
    stale["occurrence_id"] = "occurrence:fresh-id-cannot-authorize"
    with pytest.raises(scheduled_tasks.ScheduledTaskError):
        scheduled_tasks.execution_context_for_scheduled_occurrence(stale)


def test_stale_duplicate_wrong_task_hash_and_origin_responses_are_rejected(
    scheduled_runtime,
):
    _values, _policy = scheduled_runtime
    first = _create_sensitive(25)
    second = _create_sensitive(30)
    begun = scheduled_tasks.begin_scheduled_task_authorization(
        first["id"], origin_ref="mobile:fixture",
    )
    request = begun["request"]
    for field, value in (
        ("task_id", second["id"]),
        ("task_revision", request["task_revision"] + 1),
        ("definition_hash", "f" * 64),
        ("origin_ref", "mobile:wrong"),
    ):
        response = {**request, field: value, "action": "approve"}
        rejected = scheduled_tasks.respond_scheduled_task_authorization(
            response, authenticated_origin="mobile:fixture",
        )
        assert rejected["ok"] is False
        assert rejected["code"] == scheduled_tasks.ERROR_AUTHORIZATION_STALE

    accepted = scheduled_tasks.respond_scheduled_task_authorization(
        {**request, "action": "approve"},
        authenticated_origin="mobile:fixture",
    )
    assert accepted["ok"] is True
    duplicate = scheduled_tasks.respond_scheduled_task_authorization(
        {**request, "action": "approve"},
        authenticated_origin="mobile:fixture",
    )
    assert duplicate["ok"] is False
    assert duplicate["code"] == scheduled_tasks.ERROR_AUTHORIZATION_STALE

    third = _create_sensitive(40)
    concurrent_request = scheduled_tasks.begin_scheduled_task_authorization(
        third["id"], origin_ref="desktop:race",
    )["request"]
    response = {**concurrent_request, "action": "approve"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _index: scheduled_tasks.respond_scheduled_task_authorization(
                response, authenticated_origin="desktop:race",
            ),
            range(2),
        ))
    assert sorted(result["ok"] for result in results) == [False, True]


def test_expiration_revocation_and_policy_epoch_invalidate_before_dispatch(
    scheduled_runtime, monkeypatch,
):
    _values, policy = scheduled_runtime
    created = _create_sensitive()
    request = _authorize(created["id"], ttl_seconds=60)
    job = _current(created["id"])
    context = scheduled_tasks.execution_context_for_scheduled_occurrence(
        _execution(job, "occurrence:permit"),
    )
    with execution_context_scope(context):
        permission_request = build_permission_request(
            "set_volume", {"level": 25}, policy=policy,
        )
    decision = policy.evaluate(permission_request)
    permit = policy.mint_permit(permission_request, decision)
    assert permit is not None
    scheduled_tasks.update_scheduled_task(
        task_id=created["id"], instruction="Set volume to 26", source="settings",
    )
    with pytest.raises(PermitError):
        policy.consume_permit(permit, permission_request)
    assert scheduled_tasks.preflight_scheduled_task(created["id"])["ok"] is False

    created = _create_sensitive(30)
    request = _authorize(created["id"], ttl_seconds=60)
    monkeypatch.setattr(time, "time", lambda: float(request["expiration"]) + 1.0)
    expired = scheduled_tasks.preflight_scheduled_task(created["id"])
    assert expired["ok"] is False
    assert expired["authorization_status"] == scheduled_tasks.AUTHORIZATION_EXPIRED


def test_revocation_is_idempotent_and_scheduler_does_not_consume_retry(
    scheduled_runtime,
):
    values, _policy = scheduled_runtime
    created = _create_sensitive()
    _authorize(created["id"])
    authorized = _current(created["id"])
    one = scheduled_tasks.revoke_scheduled_task_grant(
        created["id"], authorized["grant_refs"][0],
    )
    assert one["ok"] is True and one["changed"] is True
    assert scheduled_tasks.preflight_scheduled_task(created["id"])["ok"] is False

    created = _create_sensitive(30)
    _authorize(created["id"])
    first = scheduled_tasks.revoke_scheduled_task_authorization(created["id"])
    second = scheduled_tasks.revoke_scheduled_task_authorization(created["id"])
    assert first["ok"] is True and first["changed"] is True
    assert second["ok"] is True and second["changed"] is False
    timing = {"next_run": datetime(2026, 7, 27, 12, 5)}
    launched = cron.CronManager(lambda *_args, **_kwargs: None)._fire(
        "Set volume to 25", "every 5 minutes", timing,
        datetime(2026, 7, 27, 12, 5), task_id=created["id"], job_index=0,
    )
    assert launched is False
    assert timing == {"next_run": datetime(2026, 7, 27, 12, 5)}
    stored = next(job for job in values["cron_jobs"] if job["id"] == created["id"])
    assert stored["last_status"] == "blocked_authorization"


def test_session_authority_disappears_after_restart(tmp_path, monkeypatch):
    values = {"cron_jobs": []}
    monkeypatch.setattr(
        scheduled_tasks.config, "get_setting",
        lambda key, default=None: copy.deepcopy(values.get(key, default)),
    )
    monkeypatch.setattr(
        scheduled_tasks.config, "set_setting",
        lambda key, value: values.__setitem__(key, copy.deepcopy(value)),
    )
    scheduled_tasks._PENDING_AUTHORIZATIONS.clear()
    scheduled_tasks._RESPONDED_AUTHORIZATIONS.clear()
    first_store = PermissionStore(
        tmp_path / "unavailable.dat", backend=UnavailableSecureStorageBackend(),
    )
    set_default_permission_policy(PermissionPolicy(first_store))
    try:
        created = _create_sensitive()
        _authorize(created["id"])
        assert first_store.path.exists() is False
        assert scheduled_tasks.preflight_scheduled_task(created["id"])["ok"] is True

        restarted_store = PermissionStore(
            first_store.path, backend=UnavailableSecureStorageBackend(),
        )
        set_default_permission_policy(PermissionPolicy(restarted_store))
        blocked = scheduled_tasks.preflight_scheduled_task(created["id"])
        assert blocked["ok"] is False
        assert blocked["authorization_status"] in {
            scheduled_tasks.AUTHORIZATION_REQUIRED,
            scheduled_tasks.AUTHORIZATION_REVOKED,
        }
    finally:
        scheduled_tasks._PENDING_AUTHORIZATIONS.clear()
        scheduled_tasks._RESPONDED_AUTHORIZATIONS.clear()
        set_default_permission_policy(None)


def test_legacy_read_only_and_sensitive_migration_and_unknown_schema_fail_closed(
    scheduled_runtime,
):
    values, _policy = scheduled_runtime
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
    assert jobs["cron_legacy_read"]["authorization_status"] == scheduled_tasks.AUTHORIZED
    assert jobs["cron_legacy_write"]["enabled"] is False
    assert jobs["cron_legacy_write"]["task"] == "Set volume to 25"
    assert jobs["cron_unknown_schema"]["enabled"] is False
    assert jobs["cron_unknown_schema"]["schema_version"] == 999
    assert jobs["cron_corrupt_schema"]["enabled"] is False
    assert jobs["cron_corrupt_schema"]["schema_version"] == "bad"


def test_outcome_unknown_is_durable_and_blocks_later_occurrence(
    scheduled_runtime,
):
    _values, policy = scheduled_runtime
    created = _create_sensitive()
    _authorize(created["id"])
    job = _current(created["id"])
    execution = _execution(job, "occurrence:unknown")
    context = scheduled_tasks.execution_context_for_scheduled_occurrence(execution)
    with execution_context_scope(context):
        request = build_permission_request("set_volume", {"level": 25}, policy=policy)
    record = ToolExecutionRecord(
        "ERROR: outcome unknown", True, ExecutionOutcome.OUTCOME_UNKNOWN,
        1, True, True, request.effective_contract,
    )
    scheduled_tasks.record_scheduled_tool_execution(
        execution, "set_volume", {"level": 25}, record,
    )
    blocked = scheduled_tasks.preflight_scheduled_task(created["id"])
    assert blocked["ok"] is False
    assert blocked["authorization_status"] == scheduled_tasks.BLOCKED_OUTCOME_UNKNOWN
    later = {**execution, "occurrence_id": "occurrence:later"}
    with pytest.raises(scheduled_tasks.ScheduledTaskError):
        scheduled_tasks.execution_context_for_scheduled_occurrence(later)


def test_full_access_keeps_universal_destruction_barriers(
    scheduled_runtime, tmp_path,
):
    _values, policy = scheduled_runtime
    policy.store.set_permission_mode(PermissionMode.FULL_ACCESS)
    target = tmp_path / "synthetic-delete-target"
    target.mkdir()
    created = scheduled_tasks.create_scheduled_task(
        title="Synthetic delete",
        instruction=f"Delete directory {target}",
        schedule={"type": "daily", "time": "12:00"},
        enabled=True,
        source="settings",
    )["job"]
    assert created["enabled"] is True and created["grant_refs"] == []
    context = scheduled_tasks.execution_context_for_scheduled_occurrence(
        _execution(created, "occurrence:barrier"),
    )
    with execution_context_scope(context):
        ordinary = build_permission_request(
            "delete_directory", {"path": str(target)}, policy=policy,
        )
    ordinary_decision = policy.evaluate(ordinary)
    assert ordinary_decision.decision_type == DecisionType.ALLOW
    pending_permit = policy.mint_permit(ordinary, ordinary_decision)
    assert pending_permit is not None
    for system, colossal, expected in (
        (True, False, DecisionType.BLOCK_SYSTEM_DESTRUCTION),
        (False, True, DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION),
    ):
        with execution_context_scope(context):
            request = build_permission_request(
                "delete_directory", {"path": str(target)}, policy=policy,
                deletion_classification=trusted_deletion_classification(
                    system_destruction=system, colossal_deletion=colossal,
                ),
            )
        assert policy.evaluate(request).decision_type == expected
    scheduled_tasks.update_scheduled_task(
        task_id=created["id"],
        instruction=f"Delete directory {target} after review",
        source="settings",
    )
    with pytest.raises(PermitError):
        policy.consume_permit(pending_permit, ordinary)


def test_mobile_payload_and_notification_are_sanitized(scheduled_runtime):
    _values, _policy = scheduled_runtime
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


def test_mobile_dynamic_target_requires_pc_local_review(scheduled_runtime):
    created = scheduled_tasks.create_scheduled_task(
        title="Private synthetic target",
        instruction="Delete the private file named WOR195_PRIVATE.txt",
        schedule={"type": "daily", "time": "12:00"},
        enabled=True,
        source="mobile_settings",
    )["job"]

    result = scheduled_tasks.begin_scheduled_task_authorization(
        created["id"],
        origin_ref="mobile_test",
        mobile=True,
    )
    assert not result["ok"]
    assert result["code"] == scheduled_tasks.ERROR_PC_REVIEW_REQUIRED
    assert "request" not in result
