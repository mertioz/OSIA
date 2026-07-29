from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from osia import document_artifacts, document_reader
from osia.attachments import AttachmentBatchManager, AttachmentProtocolError
from osia.autonomous import authorization, orchestrator, planner, state
from osia.document_progress import document_progress_scope, emit_document_progress
from osia.document_sources import (
    DocumentSourceError,
    DocumentSourceStore,
    set_source_store_for_testing,
)
from osia.server import server
from tests.wor187_fixtures import docx, pptx, xlsx


class _PlanningAgent:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def reset(self):
        return None

    def run(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        return self.responses.pop(0)


def _plan_response(refs, *, invented=""):
    text = "Read and compare " + " ".join(refs)
    if invented:
        text += " " + invented
    return json.dumps({
        "phases": [{
            "name": "Documents",
            "tasks": [{
                "objective": text,
                "success_criterion": "Every selected source is compared",
            }],
        }],
    })


def _attachment_frame(kind, *, batch_id, request_id, sequence, **extra):
    return {
        "type": kind,
        "protocol_version": 1,
        "batch_id": batch_id,
        "conversation_id": "",
        "client_request_id": request_id,
        "request_id": request_id,
        "event_id": f"event_{sequence}_{secrets.token_hex(8)}",
        "sequence": sequence,
        **extra,
    }


def test_autonomous_attachment_transaction_is_not_a_chat_draft(tmp_path):
    now = [1_900_000_000.0]
    manager = AttachmentBatchManager(tmp_path / "batches", clock=lambda: now[0])
    payload = docx()
    digest = hashlib.sha256(payload).hexdigest()
    batch_id = "batch_autonomous_docs"
    request_id = "request_autonomous_docs"
    prepare = _attachment_frame(
        "attachment_batch_prepare",
        batch_id=batch_id,
        request_id=request_id,
        sequence=1,
        attachment_purpose="autonomous",
        source_device_role="mobile",
        file_count=1,
        aggregate_size=len(payload),
        created_at=datetime.fromtimestamp(now[0], timezone.utc).isoformat(),
        expiry=datetime.fromtimestamp(now[0] + 60, timezone.utc).isoformat(),
        files=[{
            "file_id": "file_document_one",
            "logical_order": 0,
            "display_name": "private-name.docx",
            "original_extension": "docx",
            "declared_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "detected_type_category": "document",
            "declared_byte_size": len(payload),
            "sha256": digest,
            "chunk_size": len(payload),
            "expected_chunk_count": 1,
        }],
    )
    accepted = manager.handle(prepare)[0]
    assert accepted["attachment_purpose"] == "autonomous"
    assert manager.handle(_attachment_frame(
        "attachment_file_begin", batch_id=batch_id, request_id=request_id,
        sequence=2, file_id="file_document_one", logical_order=0,
    )) == []
    assert manager.handle(_attachment_frame(
        "attachment_file_chunk", batch_id=batch_id, request_id=request_id,
        sequence=3, file_id="file_document_one", chunk_index=0,
        byte_offset=0, payload_length=len(payload), expected_chunk_count=1,
        data=base64.b64encode(payload).decode("ascii"),
    )) == []
    assert manager.handle(_attachment_frame(
        "attachment_file_complete", batch_id=batch_id, request_id=request_id,
        sequence=4, file_id="file_document_one",
        declared_byte_size=len(payload), sha256=digest,
    )) == []
    committed = manager.handle(_attachment_frame(
        "attachment_batch_commit", batch_id=batch_id, request_id=request_id,
        sequence=5, file_count=1, aggregate_size=len(payload),
    ))[0]
    assert committed["attachment_purpose"] == "autonomous"
    with pytest.raises(AttachmentProtocolError, match="attachment_purpose_mismatch"):
        manager.consume_for_turn(batch_id, "conversation_one", request_id)
    consumed = manager.consume_for_autonomous(batch_id, request_id)
    assert len(consumed) == 1 and consumed[0].path.read_bytes() == payload
    manager.cleanup_disconnected()


def test_exact_three_source_inventory_is_retained_and_invention_rejected():
    refs = [f"src_{index:032x}" for index in range(1, 4)]
    inventory = [
        {
            "source_ref": ref,
            "type_label": kind,
            "document_family": family,
            "state": "ready",
            "warnings": [],
        }
        for ref, kind, family in zip(
            refs,
            ("docx", "xlsx", "pptx"),
            ("document", "spreadsheet", "presentation"),
        )
    ]
    agent = _PlanningAgent([_plan_response(refs)])
    phases = planner.plan_objective(
        "Compare three selected documents", agent,
        run_id="run:three-documents", document_sources=inventory,
    )
    assert phases is not None
    assert all(ref in json.dumps(phases) for ref in refs)
    prompt = agent.prompts[0]
    assert all(ref in prompt for ref in refs)
    assert "private-name" not in prompt

    invented = "src_not_a_real_capability"
    bad_agent = _PlanningAgent([
        _plan_response(refs, invented=invented),
        _plan_response(refs, invented=invented),
    ])
    assert planner.plan_objective(
        "Compare them", bad_agent,
        run_id="run:invented-document", document_sources=inventory,
    ) is None
    substituted_agent = _PlanningAgent([
        _plan_response([refs[0], refs[1], refs[0]]),
        _plan_response([refs[0], refs[1], refs[0]]),
    ])
    assert planner.plan_objective(
        "Compare them", substituted_agent,
        run_id="run:substituted-document", document_sources=inventory,
    ) is None

    planned = state.new_task_state(
        "Compare selected inputs", "fixture", "fixture-model", phases,
        run_id="run:authorization-documents",
    )
    planned["document_sources"] = inventory
    summary, descriptors = authorization.derive_capability_proposal(planned)
    document_entries = [
        item for item in summary if item["operation_class"] == "read_document"
    ]
    assert len(document_entries) == 3
    assert len({item["target_scope_hash"] for item in document_entries}) == 3
    assert {
        descriptor["arguments"]["source_ref"]
        for descriptor in descriptors.values()
        if descriptor["tool_identity"] == "read_document"
    } == set(refs)


def test_source_binding_checks_owner_epoch_integrity_and_one_run(tmp_path):
    clock = [2_000_000_000.0]
    store = DocumentSourceStore(tmp_path / "sources", clock=lambda: clock[0])
    paths = []
    for name, payload in (("one.docx", docx()), ("two.xlsx", xlsx()), ("three.pptx", pptx())):
        path = tmp_path / name
        path.write_bytes(payload)
        paths.append(path)
    created = [
        store.create(
            path, owner_id="authenticated-device", source_kind="mobile_attachment",
            display_name=path.name, permission_epoch=7,
        )
        for path in paths
    ]
    refs = [item["source_ref"] for item in created]
    assert [item["type_label"] for item in store.inspect_for_binding(
        refs, owner_id="authenticated-device", permission_epoch=7,
    )] == ["docx", "xlsx", "pptx"]
    with pytest.raises(DocumentSourceError, match="duplicate_source"):
        store.inspect_for_binding(
            [refs[0], refs[0]], owner_id="authenticated-device", permission_epoch=7,
        )
    with pytest.raises(DocumentSourceError, match="unauthorized_source"):
        store.inspect_for_binding(
            refs, owner_id="another-device", permission_epoch=7,
        )
    with pytest.raises(DocumentSourceError, match="stale_permission_epoch"):
        store.inspect_for_binding(
            refs, owner_id="authenticated-device", permission_epoch=8,
        )

    failed_path = tmp_path / "failed.docx"
    failed_path.write_bytes(b"not-a-valid-document")
    failed = store.create(
        failed_path, owner_id="authenticated-device",
        source_kind="mobile_attachment", display_name="failed.docx",
        permission_epoch=7,
    )
    assert failed["state"] == "failed"
    with pytest.raises(DocumentSourceError, match="failed_source"):
        store.inspect_for_binding(
            [failed["source_ref"]], owner_id="authenticated-device",
            permission_epoch=7,
        )

    payload_path = store.root / refs[0] / "payload.bin"
    original = payload_path.read_bytes()
    payload_path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(DocumentSourceError, match="corrupted_source"):
        store.inspect_for_binding(
            refs, owner_id="authenticated-device", permission_epoch=7,
        )
    payload_path.write_bytes(original)
    bound = store.bind_for_run(
        refs, owner_id="authenticated-device", run_owner_id="task-owner",
        run_id="run:bound-once", permission_epoch=7,
    )
    assert [item["source_ref"] for item in bound] == refs
    with pytest.raises(DocumentSourceError, match="unauthorized_source|source_already_bound"):
        store.bind_for_run(
            refs, owner_id="authenticated-device", run_owner_id="task-owner-2",
            run_id="run:bound-twice", permission_epoch=7,
        )

    expiring = store.create(
        paths[0], owner_id="authenticated-device", source_kind="mobile_attachment",
        display_name=paths[0].name, permission_epoch=7,
    )
    clock[0] += 73 * 60 * 60
    with pytest.raises(DocumentSourceError, match="expired_source"):
        store.inspect_for_binding(
            [expiring["source_ref"]], owner_id="authenticated-device",
            permission_epoch=7,
        )


def test_bound_source_reuses_artifact_without_reparse(tmp_path):
    source_store = DocumentSourceStore(tmp_path / "sources")
    artifact_store = document_artifacts.ArtifactStore(tmp_path / "artifacts")
    prior_source = set_source_store_for_testing(source_store)
    prior_artifact = document_artifacts.set_store_for_testing(artifact_store)
    try:
        source_path = tmp_path / "resume.docx"
        source_path.write_bytes(docx())
        staged = source_store.create(
            source_path, owner_id="device", source_kind="mobile_attachment",
            display_name="resume.docx", permission_epoch=3,
        )
        source_store.bind_for_run(
            [staged["source_ref"]], owner_id="device", run_owner_id="run-owner",
            run_id="run:resume", permission_epoch=3,
        )
        with document_artifacts.artifact_access_scope("run-owner"):
            with patch("osia.document_reader._document_policy_epoch", return_value=3):
                with patch(
                    "osia.document_reader._run_worker",
                    wraps=document_reader._run_worker,
                ) as worker:
                    first = json.loads(document_reader.read_document(
                        source_ref=staged["source_ref"],
                    ))
                    second = json.loads(document_reader.read_document(
                        source_ref=staged["source_ref"],
                    ))
        assert first["ok"] is second["ok"] is True
        assert first["artifact_id"] == second["artifact_id"]
        assert worker.call_count == 1
    finally:
        set_source_store_for_testing(prior_source)
        document_artifacts.set_store_for_testing(prior_artifact)


def test_document_progress_is_monotonic_sanitized_and_terminal():
    emitted = []
    with document_progress_scope(
        "chat", "turn:document-progress", sink=emitted.append,
    ):
        emit_document_progress("private/path/report.docx", "identifying")
        emit_document_progress("private/path/report.docx", "identifying")
        emit_document_progress("private/path/report.docx", "extracting", document_type="DOCX")
        emit_document_progress("private/path/report.docx", "inspecting")
        emit_document_progress("private/path/report.docx", "ready", document_type="docx")
        emit_document_progress("private/path/report.docx", "failed", failure_category="missing_source")
    assert [item["phase"] for item in emitted] == [
        "identifying", "extracting", "ready",
    ]
    assert len({item["item_id"] for item in emitted}) == 1
    assert "private" not in json.dumps(emitted)

    scope_id = "turn:test-" + secrets.token_hex(8)
    item_id = "item_" + secrets.token_hex(12)
    base = {
        "scope_type": "chat", "scope_id": scope_id, "revision": 0,
        "item_id": item_id, "document_type": "../../secret.docx",
        "counts": {"completed": 1, "total": 3},
    }
    first = server._document_progress_frame({**base, "phase": "identifying"}, owner_device="device-a")
    assert first is not None and first["document_type"] == "document"
    assert set(first) == {
        "type", "schema_version", "event_id", "server_id", "session_id",
        "scope_type", "scope_id", "plan_revision", "sequence", "item_id",
        "document_type", "phase", "terminal", "failure_category", "counts",
        "snapshot",
    }
    assert server._document_progress_frame({**base, "phase": "identifying"}) is None
    ready = server._document_progress_frame({**base, "phase": "ready", "document_type": "docx"})
    assert ready is not None and ready["terminal"] is True
    assert server._document_progress_frame({**base, "phase": "failed"}) is None
    snapshot = server._document_progress_snapshot("chat", scope_id, owner_device="device-a")
    assert len(snapshot) == 1 and snapshot[0]["snapshot"] is True
    assert snapshot[0]["sequence"] > ready["sequence"]
    assert server._document_progress_snapshot("chat", scope_id, owner_device="device-b") == []
    server._retire_document_scope("chat", scope_id)
    assert server._document_progress_frame({
        **base, "item_id": "item_" + secrets.token_hex(12), "phase": "identifying",
    }) is None


def test_run_cannot_finish_before_all_document_sources_are_processed():
    phases = [{
        "name": "Documents",
        "tasks": [{
            "objective": "Compare explicit documents",
            "success_criterion": "The comparison is verified",
        }],
    }]
    run = state.new_task_state(
        "Compare explicit documents", "fixture", "fixture-model", phases,
        run_id="run:zero-of-one",
    )
    run["document_sources"] = [{
        "source_ref": "src_00000000000000000000000000000001",
        "type_label": "docx",
        "document_family": "document",
        "state": "ready",
        "warnings": [],
    }]
    run["phases"][0]["status"] = "done"
    run["phases"][0]["tasks"][0]["status"] = "done"
    run["cursor"] = {"phase": 1, "task": 0}
    run["run_state"] = "ready"
    events = []
    runner = orchestrator.Orchestrator(
        _PlanningAgent([]), progress_cb=events.append,
    )
    with patch.object(runner, "_document_sources_ready", return_value=False), patch.object(
        runner, "_safe_save", return_value=True,
    ):
        assert runner._step(run) is False
    assert run["status"] == "failed"
    assert run["run_state"] == "failed"
    assert events[-1]["event"] == "failed"


def test_empty_autonomous_inventory_does_not_inherit_chat_sources():
    response = _plan_response([])
    agent = _PlanningAgent([response])
    planned = planner.create_plan_state(
        "Summarize the objective only", "fixture", "fixture-model", agent,
        run_id="run:no-chat-inheritance", document_sources=[],
    )
    assert planned is not None
    assert planned["document_sources"] == []
    assert "ATTACHED DOCUMENT INVENTORY" not in agent.prompts[0]
