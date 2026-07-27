"""WOR-175 deterministic Autonomous document-reference propagation."""

from __future__ import annotations

import json
import re
import secrets

import pytest

from osia import document_artifacts, document_reader
from osia.autonomous import diagnostics, orchestrator, planner, state


_TAIL_TEXT = secrets.token_hex(20)
_DOCUMENT_TEXT = secrets.token_hex(220) + _TAIL_TEXT


def _metadata():
    return {
        "document_type": "pdf",
        "page_count": 2,
        "page_methods": ["native", "ocr"],
        "confidence": 0.93,
        "warnings": [],
    }


def _artifact_id(prompt: str) -> str:
    match = re.search(r"doc_[0-9a-f]{32}", prompt)
    assert match, prompt
    return match.group(0)


class ArtifactAgent:
    provider = "test"
    model = "test-model"
    last_run_diagnostic = None

    def __init__(self):
        self.execution_reads = 0
        self.verifier_reads = 0
        self.prompts = []

    def reset(self):
        return None

    def run(self, prompt):
        self.prompts.append(prompt)
        if prompt.startswith("Verify by DIRECT observation"):
            artifact_id = _artifact_id(prompt)
            result = json.loads(document_reader.read_document_artifact(
                artifact_id, offset=300, chunk_chars=800,
            ))
            self.verifier_reads += 1
            return "DONE\nVerified locally." if _TAIL_TEXT in result["content"] else "NOT_DONE"
        if "create the local artifact" in prompt:
            ref = document_artifacts.get_store().create(_DOCUMENT_TEXT, _metadata())
            document_artifacts.capture_reference(ref)
            return _DOCUMENT_TEXT
        artifact_id = _artifact_id(prompt)
        result = json.loads(document_reader.read_document_artifact(
            artifact_id, offset=300, chunk_chars=800,
        ))
        self.execution_reads += 1
        return "later task observed the bounded artifact" if _TAIL_TEXT in result["content"] else "ERROR: missing tail"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    checkpoint = tmp_path / "task_state.json"
    store = document_artifacts.ArtifactStore(tmp_path / "artifacts")
    previous = document_artifacts.set_store_for_testing(store)
    monkeypatch.setattr(state, "TASK_STATE_FILE", checkpoint)
    try:
        yield checkpoint, store
    finally:
        document_artifacts.set_store_for_testing(previous)


def _workflow_state():
    st = state.new_task_state("document workflow", "test", "test-model", [
        {
            "name": "documents",
            "tasks": [
                {
                    "id": "create",
                    "objective": "create the local artifact",
                    "success_criterion": "the retained artifact contains the required tail token",
                },
                {
                    "id": "consume",
                    "objective": "use the retained local artifact in a later task",
                    "success_criterion": "the same artifact still contains the required tail token",
                },
            ],
        },
    ])
    # Authorization is covered separately; these tests isolate artifact flow.
    st["run_state"] = "ready"
    st["authorization_status"] = "authorized"
    st["authorization_source"] = "task_scoped"
    return st


def test_task_to_task_verifier_tail_propagation_and_terminal_cleanup(isolated):
    checkpoint, store = isolated
    st = _workflow_state()
    agent = ArtifactAgent()
    events = []
    runner = orchestrator.Orchestrator(agent, events.append)
    runner._state = st

    assert runner._step(st) is True
    ref = st["artifacts"][0]
    artifact_id = ref["artifact_id"]
    assert st["phases"][0]["tasks"][0]["artifact_ids"] == [artifact_id]
    with document_artifacts.artifact_access_scope(st["task_id"], [artifact_id]):
        assert store.inspect(artifact_id)["content_length"] > 300
    first_checkpoint = checkpoint.read_text("utf-8")
    assert _TAIL_TEXT not in first_checkpoint
    assert _DOCUMENT_TEXT not in first_checkpoint
    assert agent.verifier_reads == 1

    assert runner._step(st) is True
    assert agent.execution_reads == 1
    assert agent.verifier_reads == 2
    assert st["phases"][0]["tasks"][1]["artifact_ids"] == [artifact_id]
    assert _TAIL_TEXT not in json.dumps(st)
    assert all(_TAIL_TEXT not in json.dumps(event) for event in events)

    assert runner._step(st) is False
    assert st["status"] == "done"
    assert st["artifacts"] == []
    assert not (store.root / artifact_id).exists()


def test_interruption_restart_resume_preserves_authorized_access(isolated):
    checkpoint, store = isolated
    st = _workflow_state()
    task = st["phases"][0]["tasks"][0]
    with document_artifacts.artifact_access_scope(st["task_id"]):
        ref = store.create(_DOCUMENT_TEXT, _metadata())
    st["artifacts"] = [ref]
    task["artifact_ids"] = [ref["artifact_id"]]
    task["status"] = "in_progress"
    task["attempts"] = 1
    state.save_task_state(st)

    restarted = document_artifacts.ArtifactStore(store.root)
    previous = document_artifacts.set_store_for_testing(restarted)
    try:
        loaded = state.load_task_state()
        runner = orchestrator.Orchestrator(ArtifactAgent())
        runner._state = loaded
        assert runner._artifact_failure(loaded) is None
        with runner._artifact_scope(loaded, task):
            chunk = json.loads(document_reader.read_document_artifact(
                ref["artifact_id"], offset=300, chunk_chars=800,
            ))
        assert _TAIL_TEXT in chunk["content"]
    finally:
        document_artifacts.set_store_for_testing(previous)


def test_background_resume_salvages_then_completes_and_cleans(isolated):
    _, store = isolated
    st = _workflow_state()
    task = st["phases"][0]["tasks"][0]
    with document_artifacts.artifact_access_scope(st["task_id"]):
        ref = store.create(_DOCUMENT_TEXT, _metadata())
    st["artifacts"] = [ref]
    task["artifact_ids"] = [ref["artifact_id"]]
    task["status"] = "in_progress"
    task["attempts"] = 1
    state.save_task_state(st)
    loaded = state.load_task_state()
    agent = ArtifactAgent()
    runner = orchestrator.Orchestrator(agent)
    runner.resume(loaded)
    runner._thread.join(timeout=10)
    assert not runner._thread.is_alive()
    assert loaded["status"] == "done"
    assert agent.verifier_reads >= 2
    assert agent.execution_reads == 1
    assert loaded["artifacts"] == []
    assert not (store.root / ref["artifact_id"]).exists()


def test_retry_and_replan_retain_reference_without_payload_in_prompt(
    isolated, monkeypatch,
):
    checkpoint, store = isolated
    st = _workflow_state()
    task = st["phases"][0]["tasks"][0]
    with document_artifacts.artifact_access_scope(st["task_id"]):
        ref = store.create(_DOCUMENT_TEXT, _metadata())
    st["artifacts"] = [ref]
    task["artifact_ids"] = [ref["artifact_id"]]
    task["status"] = "in_progress"
    task["attempts"] = 1
    agent = ArtifactAgent()
    runner = orchestrator.Orchestrator(agent)
    runner._state = st
    runner._active_task = task

    state.set_task_status(st, 0, 0, "in_progress", last_error="verification_not_completed")
    state.save_task_state(st)
    assert state.load_task_state()["artifacts"][0]["artifact_id"] == ref["artifact_id"]

    captured = []
    monkeypatch.setattr(
        planner,
        "plan_objective",
        lambda objective, agent, failure_cb=None, **_kwargs: (
            captured.append(objective)
            or [{"name": "replacement", "status": "pending", "tasks": []}]
        ),
    )
    assert runner._replan(st) is True
    assert st["artifacts"][0]["artifact_id"] == ref["artifact_id"]
    assert ref["artifact_id"] in captured[0]
    assert _TAIL_TEXT not in captured[0]
    assert len(captured[0]) < orchestrator.DIGEST_MAX_CHARS * 2
    assert checkpoint.exists()


def test_expired_missing_corrupt_and_unauthorized_are_typed(isolated):
    _, store = isolated
    st = _workflow_state()
    task = st["phases"][0]["tasks"][0]
    with document_artifacts.artifact_access_scope(st["task_id"]):
        ref = store.create(_DOCUMENT_TEXT, _metadata())
    st["artifacts"] = [ref]
    task["artifact_ids"] = [ref["artifact_id"]]
    runner = orchestrator.Orchestrator(ArtifactAgent())
    runner._state = st
    assert runner._artifact_failure(st, task) is None

    payload = store.root / ref["artifact_id"] / "payload.txt"
    payload.write_text(secrets.token_hex(24), "utf-8")
    assert runner._artifact_failure(st, task) == "document_artifact_corrupted"
    payload.unlink()
    assert runner._artifact_failure(st, task) == "document_artifact_missing"

    other = document_artifacts.ArtifactStore(store.root.parent / "other")
    with document_artifacts.artifact_access_scope("different-owner"):
        other_ref = other.create(secrets.token_hex(24), _metadata())
    st["artifacts"] = [other_ref]
    task["artifact_ids"] = [other_ref["artifact_id"]]
    previous = document_artifacts.set_store_for_testing(other)
    try:
        assert runner._artifact_failure(st, task) == "document_artifact_unauthorized"
    finally:
        document_artifacts.set_store_for_testing(previous)

    now = [10.0]
    expiring = document_artifacts.ArtifactStore(store.root.parent / "expiry", clock=lambda: now[0])
    previous = document_artifacts.set_store_for_testing(expiring)
    try:
        with document_artifacts.artifact_access_scope(st["task_id"]):
            expired_ref = expiring.create(secrets.token_hex(24), _metadata())
        st["artifacts"] = [expired_ref]
        task["artifact_ids"] = [expired_ref["artifact_id"]]
        now[0] += document_artifacts.ARTIFACT_TTL_SECONDS + 1
        assert runner._artifact_failure(st, task) == "document_artifact_expired"
        outcome = runner._verify_outcome(task, "")
        assert outcome.category == diagnostics.FailureCategory.VERIFICATION_ERROR
        assert outcome.reason_code == "document_artifact_expired"
    finally:
        document_artifacts.set_store_for_testing(previous)


def test_startup_cleanup_preserves_referenced_and_removes_orphan(
    isolated, monkeypatch,
):
    _, store = isolated
    st = _workflow_state()
    with document_artifacts.artifact_access_scope(st["task_id"]):
        kept = store.create(secrets.token_hex(24), _metadata())
        orphan = store.create(secrets.token_hex(24), _metadata())
    st["artifacts"] = [kept]
    store._live_ids.clear()
    store.cleanup(remove_unreferenced=True)
    assert (store.root / orphan["artifact_id"]).exists()
    monkeypatch.setattr(store, "_creator_alive", lambda metadata: False)
    store.cleanup(
        document_artifacts.referenced_ids(st), remove_unreferenced=True,
    )
    assert (store.root / kept["artifact_id"]).exists()
    assert not (store.root / orphan["artifact_id"]).exists()


def test_diagnostics_and_progress_fingerprints_remain_bounded():
    from osia import agent

    st = _workflow_state()
    diagnostic = diagnostics.make_diagnostic(
        st,
        stage=diagnostics.AutonomousStage.RESUME,
        category=diagnostics.FailureCategory.VERIFICATION_ERROR,
        reason_code="document_artifact_missing",
    )
    encoded = json.dumps(diagnostic)
    assert diagnostic["reason_code"] == "document_artifact_missing"
    assert _TAIL_TEXT not in encoded

    guard = agent._AntiLoopGuard(lang="en")
    fingerprint = agent._document_progress_fingerprint({"content_hash": "b" * 64})
    assert guard._record_useful_progress(fingerprint) is True
    assert guard._record_useful_progress(fingerprint) is False
    assert guard.useful_progress_count == 1

    document_failure_agent = ArtifactAgent()
    document_failure_agent.last_run_document_error = "document_extraction_failure"
    failure = orchestrator.Orchestrator(document_failure_agent)._agent_run_failure()
    assert failure == (
        diagnostics.FailureCategory.TASK_EXECUTION_FAILURE,
        "document_extraction_failure",
    )
