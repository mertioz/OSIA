"""Additive PC/mobile protocol privacy and canonical-state checks."""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from osia.autonomous import state
from osia.server import server


def test_mobile_snapshot_is_sanitized_and_canonical_across_reconnect():
    private_path = r"C:\Users\private\secret-plan.txt"
    private_body = "SYNTHETIC_PRIVATE_BODY_194"
    st = state.new_task_state(
        f"Write {private_path} with {private_body} and notify private@example.com at https://private.test/a",
        "fixture-provider",
        "fixture-model",
        [{
            "name": f"Prepare secret-plan with {private_body}",
            "tasks": [{
                "objective": f"Write secret-plan.txt with {private_body}",
                "success_criterion": "The private file exists",
            }],
        }],
        run_id="run:protocol",
    )
    st.update({
        "run_state": "authorization_pending",
        "authorization_status": "pending",
        "capability_summary": [{
            "capability_id": "cap_one",
            "label": "local_files:write",
            "tool_category": "filesystem",
            "effect_class": "persistent_local",
            "target_scope_hash": "a" * 64,
            "target_precision": "exact",
            "delegable": True,
            "can_approve_mobile": False,
        }],
        "authorization_request": {
            "protocol_version": 1,
            "request_id": "auth_one",
            "run_id": "run:protocol",
            "plan_revision": 1,
            "authorization_revision": 0,
            "expected_policy_epoch": 0,
            "origin_ref": "mobile_origin",
            "issued_at": 1.0,
            "expires_at": 9999999999.0,
            "responded": False,
        },
    })

    first = server._autonomous_snapshot(st)
    reconnect = server._autonomous_snapshot(st)
    wire = json.dumps(first)
    assert first == reconnect
    assert first["authorization_request"]["request_id"] == "auth_one"
    assert first["run_id"] == "run:protocol"
    assert "secret-plan.txt" not in wire
    assert "private@example.com" not in wire
    assert "https://private.test" not in wire
    assert private_body not in wire
    assert first["objective"] == ""
    assert first["phase_name"] == ""
    assert first["plan_summary"] == [{"name": "Phase 1", "tasks": ["Task 1.1"]}]
    assert "target_scope_hash" not in wire
    assert first["capability_summary"][0]["can_approve_mobile"] is False
    st["_runtime_pc_capability_details"] = {"cap_one": private_path}
    pc = server._autonomous_pc_snapshot(st)
    assert pc["_server_mobile_run"] is True
    assert pc["_runtime_pc_capability_details"]["cap_one"] == private_path


def test_old_client_observation_cannot_turn_pending_state_into_execution():
    st = state.new_task_state(
        "Read safe information",
        "fixture-provider",
        "fixture-model",
        [{"name": "Inspect", "tasks": ["Read safe information"]}],
        run_id="run:old-client",
    )
    st["run_state"] = "authorization_pending"
    st["authorization_status"] = "pending"
    before = json.dumps(st, sort_keys=True)
    snapshot = server._autonomous_snapshot(st)
    assert snapshot["run_state"] == "authorization_pending"
    assert st["run_state"] == "authorization_pending"
    assert json.dumps(st, sort_keys=True) == before


def test_loopback_pc_authorize_controls_canonical_server_run(monkeypatch):
    st = {
        "run_id": "run:physical",
        "authorization_request": {"origin_ref": "mobile:bound"},
    }
    calls = []

    class FakeOrchestrator:
        def start(self, value):
            calls.append(("start", value["run_id"]))

    monkeypatch.setitem(server._autonomous, "st", st)
    monkeypatch.setattr(
        server._aauthorization,
        "response_for_current_request",
        lambda value, *, action, origin_ref: {
            "action": action,
            "origin_ref": origin_ref,
        },
    )
    monkeypatch.setattr(
        server._aauthorization,
        "authorize_and_start",
        lambda value, response, *, origin_ref: calls.append(
            ("authorize", response["action"], origin_ref)
        ),
    )
    monkeypatch.setattr(server._astate, "save_task_state", lambda value: None)
    monkeypatch.setattr(
        server, "_ensure_autonomous_engine", lambda: (object(), FakeOrchestrator())
    )
    monkeypatch.setattr(server, "_autonomous_frame", lambda payload, value: payload)
    monkeypatch.setattr(server, "_broadcast_autonomous", lambda frame: None)
    monkeypatch.setattr(
        server,
        "_autonomous_pc_snapshot",
        lambda value: {"run_id": value["run_id"], "_server_mobile_run": True},
    )

    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {
            "type": "http.request",
            "body": b'{"action":"authorize"}',
            "more_body": False,
        }

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/internal/autonomous_authorization_action",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8765),
        "scheme": "http",
    }, receive)
    result = asyncio.run(server.internal_autonomous_authorization_action(request))
    assert result["ok"] is True
    assert result["event"] == "running"
    assert calls == [
        ("authorize", "approve", "desktop"),
        ("start", "run:physical"),
    ]
    assert st["authorization_request"]["origin_ref"] == "desktop"


def test_loopback_pc_recovers_persisted_mobile_checkpoint(monkeypatch):
    persisted = {
        "run_id": "run:reconnected",
        "run_state": "authorization_pending",
    }
    monkeypatch.setitem(server._autonomous, "st", None)
    monkeypatch.setattr(server._astate, "load_task_state", lambda: persisted)

    recovered = server._current_autonomous_state()

    assert recovered is persisted
    assert server._autonomous["st"] is persisted
