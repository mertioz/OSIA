import json

import pytest

from osia import i18n
from osia.autonomous import diagnostics, planner, state
from osia.autonomous.orchestrator import Orchestrator


SENSITIVE_MARKER = "WOR173_PRIVATE_MARKER_DO_NOT_LEAK"


class StatusError(RuntimeError):
    def __init__(self, status_code):
        super().__init__(SENSITIVE_MARKER)
        self.status_code = status_code


class ScriptedAgent:
    def __init__(self, outputs, *, provider="fake_provider", model="fake-model"):
        self.outputs = list(outputs)
        self.provider = provider
        self.model = model

    def reset(self):
        return None

    def run(self, _message):
        value = self.outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _one_task_state(*, attempts=0, max_attempts=3, max_replans=2):
    st = state.new_task_state(
        "synthetic objective",
        "fake_provider",
        "fake-model",
        [{
            "name": "Synthetic phase",
            "tasks": [{
                "id": "0.0",
                "objective": "Perform a synthetic no-side-effect step",
                "success_criterion": "The synthetic screen shows READY",
                "attempts": attempts,
            }],
        }],
    )
    st["budgets"].update({
        "max_attempts_per_task": max_attempts,
        "max_replans": max_replans,
    })
    # Authorization is orthogonal to this diagnostic-focused test module.
    st["run_state"] = "ready"
    st["authorization_status"] = "authorized"
    st["authorization_source"] = "task_scoped"
    return st


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (StatusError(401), "planning_authentication_failure"),
        (StatusError(429), "planning_rate_limit"),
        (TimeoutError(SENSITIVE_MARKER), "planning_timeout"),
        (StatusError(500), "planning_provider_failure"),
    ],
)
def test_planner_provider_failures_are_typed(error, category):
    failures = []
    assert planner.plan_objective(
        "synthetic objective",
        ScriptedAgent([error]),
        failure_cb=failures.append,
    ) is None
    assert failures[-1].error_category.value == category
    assert SENSITIVE_MARKER not in repr(failures[-1])


@pytest.mark.parametrize(
    ("outputs", "reason_code"),
    [
        (["not json", "still not json"], "invalid_json"),
        ([
            '{"phases":[{"name":"P","tasks":[{"objective":"x"}]}]}',
            '{"phases":[{"name":"P","tasks":[{"objective":"x"}]}]}',
        ], "invalid_schema"),
    ],
)
def test_invalid_planning_output_after_correction_is_typed(outputs, reason_code):
    failures = []
    assert planner.plan_objective(
        "synthetic objective",
        ScriptedAgent(outputs),
        failure_cb=failures.append,
    ) is None
    assert failures[-1].reason == reason_code
    assert failures[-1].error_category is diagnostics.FailureCategory.INVALID_PLANNING_OUTPUT


def test_blank_and_reset_planning_failures_reach_typed_callback():
    blank_failures = []
    assert planner.plan_objective(
        "", ScriptedAgent([]), failure_cb=blank_failures.append,
    ) is None
    assert blank_failures[-1].error_category is (
        diagnostics.FailureCategory.INVALID_PLANNING_OUTPUT
    )

    class ResetFailureAgent(ScriptedAgent):
        def reset(self):
            raise StatusError(401)

    reset_failures = []
    assert planner.plan_objective(
        "synthetic objective",
        ResetFailureAgent([]),
        failure_cb=reset_failures.append,
    ) is None
    assert reset_failures[-1].error_category is (
        diagnostics.FailureCategory.PLANNING_AUTHENTICATION_FAILURE
    )


def test_task_exception_and_timeout_leaf_categories(monkeypatch):
    monkeypatch.setattr(state, "save_task_state", lambda _st: None)

    failed = _one_task_state(max_attempts=2)
    Orchestrator(
        ScriptedAgent([RuntimeError(SENSITIVE_MARKER), "NOT_DONE"]),
    )._step(failed)
    categories = [d["error_category"] for d in failed["diagnostic_history"]]
    assert "task_execution_failure" in categories
    assert failed["failure_diagnostic"]["error_category"] == "verification_not_completed"

    timed_out = _one_task_state(max_attempts=2)
    Orchestrator(
        ScriptedAgent([TimeoutError(SENSITIVE_MARKER), "NOT_DONE"]),
    )._step(timed_out)
    categories = [d["error_category"] for d in timed_out["diagnostic_history"]]
    assert "task_timeout" in categories


def test_verifier_exception_and_not_done_are_distinct(monkeypatch):
    monkeypatch.setattr(state, "save_task_state", lambda _st: None)

    verifier_error = _one_task_state(max_attempts=2)
    Orchestrator(
        ScriptedAgent(["synthetic result", RuntimeError(SENSITIVE_MARKER)]),
    )._step(verifier_error)
    assert verifier_error["failure_diagnostic"]["error_category"] == "verification_error"

    not_done = _one_task_state(max_attempts=2)
    Orchestrator(
        ScriptedAgent(["synthetic result", f"NOT_DONE {SENSITIVE_MARKER}"]),
    )._step(not_done)
    assert not_done["failure_diagnostic"]["error_category"] == (
        "verification_not_completed"
    )


def test_attempts_replan_failure_and_replans_exhausted(monkeypatch):
    monkeypatch.setattr(state, "save_task_state", lambda _st: None)

    attempts = _one_task_state(max_attempts=1, max_replans=2)
    events = []

    def failed_replan(_objective, _agent, failure_cb=None, **_kwargs):
        if failure_cb:
            failure_cb(planner.PlanFailure("invalid_schema"))
        return None

    monkeypatch.setattr(planner, "plan_objective", failed_replan)
    Orchestrator(
        ScriptedAgent(["result", "NOT_DONE"]), progress_cb=events.append,
    )._step(attempts)
    categories = [d["error_category"] for d in attempts["diagnostic_history"]]
    assert "task_attempts_exhausted" in categories
    assert "invalid_planning_output" in categories
    assert attempts["failure_diagnostic"]["error_category"] == "replan_failure"
    assert attempts["status"] == "failed"
    assert events[0]["reason"] == "task_attempts_exhausted"
    assert events[-1]["reason"] == "invalid_schema"
    assert SENSITIVE_MARKER not in json.dumps(events)

    exhausted = _one_task_state(max_attempts=1, max_replans=0)
    Orchestrator(ScriptedAgent(["result", "NOT_DONE"]))._step(exhausted)
    assert exhausted["failure_diagnostic"]["error_category"] == "replans_exhausted"


def test_invalid_resume_and_success_clear_only_current_diagnostic(monkeypatch):
    saved = []
    monkeypatch.setattr(state, "save_task_state", lambda st: saved.append(dict(st)))

    events = []
    invalid = state.new_task_state("", "fake_provider", "fake-model", [])
    invalid["run_state"] = "ready"
    invalid["authorization_status"] = "authorized"
    invalid["cursor"] = {"phase": 0, "task": 1}
    Orchestrator(ScriptedAgent([]), progress_cb=events.append).resume(invalid)
    assert invalid["status"] == "failed"
    assert invalid["failure_diagnostic"]["error_category"] == "invalid_resume_state"
    assert events[-1]["event"] == "failed"

    successful = _one_task_state(max_attempts=1)
    stale = diagnostics.make_diagnostic(
        successful,
        stage=diagnostics.AutonomousStage.CONTROL,
        category=diagnostics.FailureCategory.INTERRUPTED,
        reason_code="user_interrupted",
        resumable=True,
        checkpoint_present=True,
    )
    diagnostics.record_diagnostic(successful, stale)
    success_events = []
    Orchestrator(
        ScriptedAgent(["result", "DONE"]), progress_cb=success_events.append,
    )._step(successful)
    assert successful["failure_diagnostic"] is None
    assert successful["diagnostic_history"][-1]["error_category"] == "interrupted"
    assert success_events[-1]["event"] == "task_done"
    assert success_events[-1]["reason"] == "verification_completed"

    malformed_cursor = _one_task_state()
    malformed_cursor["cursor"] = {
        "phase": len(malformed_cursor["phases"]),
        "task": 1,
    }
    assert not state.resume_state_is_valid(malformed_cursor)


def test_interrupt_and_abort_remain_distinct_and_history_is_bounded():
    st = _one_task_state()
    for index in range(diagnostics.MAX_DIAGNOSTIC_HISTORY + 5):
        category = (
            diagnostics.FailureCategory.INTERRUPTED
            if index % 2 == 0
            else diagnostics.FailureCategory.ABORTED
        )
        diagnostic = diagnostics.make_diagnostic(
            st,
            stage=diagnostics.AutonomousStage.CONTROL,
            category=category,
            reason_code="user_interrupted" if index % 2 == 0 else "user_aborted",
            resumable=category is diagnostics.FailureCategory.INTERRUPTED,
            checkpoint_present=True,
        )
        diagnostics.record_diagnostic(st, diagnostic)
    assert len(st["diagnostic_history"]) == diagnostics.MAX_DIAGNOSTIC_HISTORY
    assert {d["error_category"] for d in st["diagnostic_history"]} == {
        "interrupted",
        "aborted",
    }


def test_malformed_persisted_diagnostic_fails_closed_without_free_form_text():
    raw = {
        "stage": SENSITIVE_MARKER.lower(),
        "error_category": SENSITIVE_MARKER.lower(),
        "reason_code": SENSITIVE_MARKER.lower(),
        "provider": "C:/Users/private",
        "model": "https://private.invalid/model",
        "resumable": "true",
        "checkpoint_present": "true",
    }
    clean = diagnostics.normalize_diagnostic(raw)
    assert clean["stage"] == "orchestrator"
    assert clean["error_category"] == "unknown_failure"
    assert clean["reason_code"] == "unknown_failure"
    assert clean["provider"] == ""
    assert clean["model"] == ""
    assert clean["resumable"] is False
    assert clean["checkpoint_present"] is False
    assert SENSITIVE_MARKER.lower() not in json.dumps(clean)


def test_old_and_new_checkpoints_load_compatibly(tmp_path, monkeypatch):
    checkpoint = tmp_path / "task_state.json"
    monkeypatch.setattr(state, "TASK_STATE_FILE", checkpoint)

    old = _one_task_state()
    old.pop("failure_diagnostic")
    old.pop("diagnostic_history")
    checkpoint.write_text(json.dumps(old), encoding="utf-8")
    loaded_old = state.load_task_state()
    assert loaded_old["failure_diagnostic"] is None
    assert loaded_old["diagnostic_history"] == []
    assert state.resume_state_is_valid(loaded_old)

    diagnostic = diagnostics.make_diagnostic(
        loaded_old,
        stage=diagnostics.AutonomousStage.CONTROL,
        category=diagnostics.FailureCategory.INTERRUPTED,
        reason_code="user_interrupted",
        resumable=True,
        checkpoint_present=True,
        transition_at="2026-07-25T12:00:00+00:00",
    )
    diagnostics.record_diagnostic(loaded_old, diagnostic)
    state.save_task_state(loaded_old)
    loaded_new = state.load_task_state()
    assert loaded_new["failure_diagnostic"]["error_category"] == "interrupted"
    assert loaded_new["diagnostic_history"][-1]["transition_at"] == (
        "2026-07-25T12:00:00+00:00"
    )

    old_failed = _one_task_state()
    old_failed["status"] = "failed"
    old_failed.pop("failure_diagnostic")
    old_failed.pop("diagnostic_history")
    checkpoint.write_text(json.dumps(old_failed), encoding="utf-8")
    loaded_failed = state.load_task_state()
    assert loaded_failed["failure_diagnostic"]["error_category"] == (
        "unknown_failure"
    )


def test_additive_server_frame_and_pc_localized_unknown_fallback(monkeypatch):
    original_checkpoint = state.TASK_STATE_FILE
    from osia.server import server
    state.TASK_STATE_FILE = original_checkpoint

    old = _one_task_state()
    old["failure_diagnostic"] = None
    frame = server._autonomous_frame({"event": "failed"}, old)
    assert frame["type"] == "autonomous_progress"
    assert frame["event"] == "failed"
    assert "error_category" not in frame

    diagnostic = diagnostics.make_diagnostic(
        old,
        stage=diagnostics.AutonomousStage.VERIFICATION,
        category=diagnostics.FailureCategory.VERIFICATION_ERROR,
        reason_code="verification_error",
        attempt=2,
        resumable=True,
        checkpoint_present=True,
        transition_at="2026-07-25T12:00:00+00:00",
    )
    diagnostics.record_diagnostic(old, diagnostic)
    frame = server._autonomous_frame({"event": "failed"}, old)
    assert frame["error_category"] == "verification_error"
    assert frame["attempt"] == 2
    assert frame["max_attempts"] == 3
    assert frame["replan_count"] == 0
    assert frame["max_replans"] == 2
    assert frame["provider"] == "fake_provider"
    assert frame["model"] == "fake-model"

    previous = i18n.get_language()
    try:
        from osia.gui.main_popup import _AutonomousPanel

        i18n.set_language("fr", persist=False)
        known = i18n.t(diagnostics.failure_message_key("verification_error"))
        unknown = i18n.t(diagnostics.failure_message_key("future_category"))
        assert "vérifier" in known
        assert "échoué" in unknown
        rendered = _AutonomousPanel._diagnostic_text({
            "error_category": "verification_error",
            "attempt": 2,
            "max_attempts": 3,
        })
        assert "vérifier" in rendered
        assert "2/3" in rendered
    finally:
        i18n.set_language(previous, persist=False)


def test_error_result_is_typed_without_persisting_provider_text(monkeypatch):
    monkeypatch.setattr(state, "save_task_state", lambda _st: None)
    st = _one_task_state(max_attempts=2)
    Orchestrator(
        ScriptedAgent([f"ERROR: {SENSITIVE_MARKER}", "NOT_DONE"]),
    )._step(st)
    assert "task_execution_failure" in {
        item["error_category"] for item in st["diagnostic_history"]
    }
    assert SENSITIVE_MARKER not in json.dumps(st)


def test_sensitive_marker_never_reaches_log_state_or_frame(
    capsys, tmp_path, monkeypatch,
):
    original_checkpoint = state.TASK_STATE_FILE
    from osia.server import server
    state.TASK_STATE_FILE = original_checkpoint

    checkpoint = tmp_path / "task_state.json"
    monkeypatch.setattr(state, "TASK_STATE_FILE", checkpoint)
    monkeypatch.setattr(server._astate, "TASK_STATE_FILE", checkpoint)

    st = _one_task_state(max_attempts=2)
    Orchestrator(
        ScriptedAgent([RuntimeError(SENSITIVE_MARKER), "NOT_DONE"]),
    )._step(st)
    state.save_task_state(st)
    frame = server._autonomous_frame({"event": "task_failed"}, st)

    captured = capsys.readouterr().out
    assert SENSITIVE_MARKER not in captured
    records = [
        json.loads(line.split("] ", 1)[1])
        for line in captured.splitlines()
        if line.startswith("[AUTONOMOUS_DIAGNOSTIC] ")
    ]
    assert records
    assert {
        "run_ref",
        "stage",
        "error_category",
        "attempt",
        "max_attempts",
        "replan_count",
        "max_replans",
        "provider",
        "model",
        "resumable",
        "checkpoint_present",
        "transition_at",
    }.issubset(records[-1])
    assert SENSITIVE_MARKER not in checkpoint.read_text(encoding="utf-8")
    assert SENSITIVE_MARKER not in json.dumps(frame)
