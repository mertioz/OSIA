from __future__ import annotations

import json

from osia.autonomous import state


def test_old_active_state_migrates_without_authority(tmp_path, monkeypatch):
    path = tmp_path / "task_state.json"
    monkeypatch.setattr(state, "TASK_STATE_FILE", path)
    legacy = {
        "schema_version": 1,
        "task_id": "legacy:run",
        "objective": "inspect",
        "status": "in_progress",
        "provider": "fixture",
        "model": "fixture-model",
        "cursor": {"phase": 0, "task": 0},
        "phases": [{
            "name": "P", "status": "pending",
            "tasks": [{
                "id": "t0", "objective": "inspect",
                "success_criterion": "done", "status": "pending",
                "attempts": 0,
            }],
        }],
        "budgets": {"max_attempts_per_task": 2, "max_replans": 1},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = state.load_task_state()
    assert loaded["schema_version"] == state.SCHEMA_VERSION
    assert loaded["run_state"] == "authorization_pending"
    assert loaded["grant_refs"] == []
    assert loaded["authorization_source"] == "none"


def test_unknown_schema_and_corrupt_grant_refs_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "task_state.json"
    monkeypatch.setattr(state, "TASK_STATE_FILE", path)
    path.write_text(json.dumps({
        "schema_version": 999,
        "status": "in_progress",
        "grant_refs": ["grant:invented"],
    }), encoding="utf-8")
    loaded = state.load_task_state()
    assert loaded["authorization_status"] == "corrupt"
    assert loaded["run_state"] == "failed"
    assert loaded["status"] == "failed"
    assert loaded["grant_refs"] == []

    st = state.new_task_state(
        "x", "p", "m", [{"name": "P", "tasks": ["x"]}],
        run_id="run:valid",
    )
    st["grant_refs"] = ["raw path is invalid"]
    assert state.execution_state_is_valid(st) is False


def test_replan_revision_preserves_non_replayable_records():
    st = state.new_task_state(
        "x", "p", "m", [{"name": "P", "tasks": ["x"]}],
        run_id="run:records",
    )
    record = {
        "operation_id": "op:one",
        "task_id": "task:t0",
        "phase_id": "phase:0",
        "plan_revision": 1,
        "tool_identity": "write_file",
        "normalized_call_hash": "a" * 64,
        "private_scope_hash": "b" * 64,
        "grant_ref": "grant:one",
        "authorization_source": "grant",
        "policy_epoch": 0,
        "status": "partial_effect_verified",
        "dispatch_attempt_count": 1,
        "started_at": "now",
        "completed_at": "now",
        "execution_outcome": "partial_effect_verified",
        "safe_reason_code": "partial_effect_verified",
    }
    state.upsert_operation_record(st, record)
    state.add_ambiguity_barrier(st, record)
    st["plan_revision"] += 1
    assert state.operation_record(st, "op:one")["status"] == (
        "partial_effect_verified"
    )
    assert len(st["ambiguity_barriers"]) == 1
