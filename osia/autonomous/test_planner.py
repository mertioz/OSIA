#!/usr/bin/env python3
"""Standalone tests for osia.autonomous.planner (WOR-45 Phase 2).

Mocks the agent entirely — NO network, NO real OSIAAgent — so it runs anywhere
with a plain ``python3 osia/autonomous/test_planner.py`` (or
``python3 -m osia.autonomous.test_planner``). Confirms:

  * clean raw JSON           → valid normalized phases
  * fenced ```json``` JSON    → fences stripped, parsed
  * malformed JSON           → ONE corrective retry → None (run() called twice)
  * shape validation rejects a task missing ``success_criterion``
  * the planner does NO disk I/O (state.save_task_state / _write_atomic unused)
  * the planner NEVER raises on bad model output (non-string, agent error)
  * create_plan_state builds a proper (unsaved) task_state dict

It also asserts the agent is only *driven* (reset + run), never mutated, and
that ids / statuses match the Phase-1 state schema.
"""

import os
import sys
import tempfile
from pathlib import Path

# Make the repo root importable when run as a bare script (…/osia/autonomous/
# → …/osia → <repo>).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from osia.autonomous import planner  # noqa: E402
from osia.autonomous import state  # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeAgent:
    """Minimal stand-in for OSIAAgent: returns canned strings from ``run()``.

    Records reset()/run() calls so tests can assert the planner only drives the
    agent through its public surface (reset + run) and retries exactly once.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.run_inputs = []
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def run(self, user_input, *args, **kwargs):
        self.run_inputs.append(user_input)
        if self._responses:
            return self._responses.pop(0)
        return ""  # exhausted → empty (unparseable) reply


class RaisingAgent:
    """Agent whose run() always raises — exercises the defensive wrap."""

    def __init__(self, error=None):
        self.run_inputs = []
        self.reset_calls = 0
        self.error = error or RuntimeError("simulated provider/network failure")

    def reset(self):
        self.reset_calls += 1

    def run(self, user_input, *args, **kwargs):
        self.run_inputs.append(user_input)
        raise self.error


class StatusError(RuntimeError):
    """Provider-style failure carrying only a safe HTTP status."""

    def __init__(self, status_code):
        super().__init__("simulated provider failure")
        self.status_code = status_code


# ── Canned model outputs ───────────────────────────────────────────────────────

CLEAN_JSON = (
    '{"phases":[{"name":"Phase 1","tasks":['
    '{"objective":"Tache A","success_criterion":"Le fichier a.txt existe"},'
    '{"objective":"Tache B","success_criterion":"La commande affiche OK"}]},'
    '{"name":"Phase 2","tasks":['
    '{"objective":"Tache C","success_criterion":"La page montre la confirmation"}]}]}'
)

FENCED_JSON = (
    "Voici le plan demandé :\n"
    "```json\n"
    '{"phases":[{"name":"Phase unique","tasks":['
    '{"objective":"Faire la chose","success_criterion":"La chose est faite et visible"}]}]}\n'
    "```\n"
    "J'espère que cela convient."
)

MALFORMED_JSON = '{"phases": [ {"name": "oops", "tasks": [ {"objective":'  # truncated

MISSING_CRITERION = (
    '{"phases":[{"name":"Phase 1","tasks":['
    '{"objective":"Tache sans critere"}]}]}'  # no success_criterion
)


# ── Tiny assert harness ─────────────────────────────────────────────────────────

_passed = 0


def check(label, cond):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        raise AssertionError(label)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_clean_json():
    print("test_clean_json")
    agent = FakeAgent([CLEAN_JSON])
    phases = planner.plan_objective("Objectif de test", agent)
    check("run() called exactly once (no retry)", len(agent.run_inputs) == 1)
    check("reset() called exactly once", agent.reset_calls == 1)
    check("phases is a non-empty list", isinstance(phases, list) and len(phases) == 2)
    check("phase 0 name preserved", phases[0]["name"] == "Phase 1")
    check("phase status defaults to pending",
          all(p["status"] == "pending" for p in phases))
    ids = [t["id"] for p in phases for t in p["tasks"]]
    check("task ids are <phase>.<task>", ids == ["0.0", "0.1", "1.0"])
    t = phases[0]["tasks"][0]
    check("task objective preserved", t["objective"] == "Tache A")
    check("task success_criterion preserved",
          t["success_criterion"] == "Le fichier a.txt existe")
    check("task fields normalized",
          t["status"] == "pending" and t["attempts"] == 0
          and t["distilled_summary"] == "" and t["last_error"] is None)
    # The normalized phases must survive new_task_state untouched (ids kept).
    st = state.new_task_state("Objectif de test", "anthropic", "claude-opus-4-8", phases)
    check("new_task_state keeps ids",
          [t["id"] for p in st["phases"] for t in p["tasks"]] == ["0.0", "0.1", "1.0"])


def test_fenced_json():
    print("test_fenced_json")
    agent = FakeAgent([FENCED_JSON])
    phases = planner.plan_objective("Faire quelque chose", agent)
    check("run() called once (fenced parsed on first try)", len(agent.run_inputs) == 1)
    check("fenced JSON parsed to one phase", isinstance(phases, list) and len(phases) == 1)
    check("fenced phase name parsed", phases[0]["name"] == "Phase unique")
    check("fenced task id is 0.0", phases[0]["tasks"][0]["id"] == "0.0")


def test_malformed_then_none():
    print("test_malformed_then_none")
    # Both the first run AND the corrective retry return malformed output.
    agent = FakeAgent([MALFORMED_JSON, MALFORMED_JSON])
    phases = planner.plan_objective("Objectif", agent)
    check("malformed → None", phases is None)
    check("retried exactly once (run() called twice)", len(agent.run_inputs) == 2)
    check("second call used the corrective prompt",
          "ONLY raw JSON" in agent.run_inputs[1])
    check("reset() still called only once (no reset on retry)", agent.reset_calls == 1)

    failures = []
    agent = FakeAgent([MALFORMED_JSON, MALFORMED_JSON])
    planner.plan_objective("Objectif", agent, failure_cb=failures.append)
    check("malformed output is reported as invalid_json",
          len(failures) == 1 and failures[0].reason == "invalid_json")


def test_shape_rejects_missing_criterion():
    print("test_shape_rejects_missing_criterion")
    # Direct validator check.
    import json as _json
    obj = _json.loads(MISSING_CRITERION)
    check("_validate_and_normalize rejects missing success_criterion",
          planner._validate_and_normalize(obj, 5, 5) is None)
    # End-to-end: parseable but shape-invalid → retry → None.
    agent = FakeAgent([MISSING_CRITERION, MISSING_CRITERION])
    phases = planner.plan_objective("Objectif", agent)
    check("missing success_criterion → None after retry", phases is None)
    check("retried exactly once", len(agent.run_inputs) == 2)
    failures = []
    agent = FakeAgent([MISSING_CRITERION, MISSING_CRITERION])
    planner.plan_objective("Objectif", agent, failure_cb=failures.append)
    check("incomplete output is reported as invalid_schema",
          len(failures) == 1 and failures[0].reason == "invalid_schema")


def test_more_shape_rejections():
    print("test_more_shape_rejections")
    import json as _json
    bad_cases = {
        "empty phases list": '{"phases": []}',
        "phases not a list": '{"phases": {"name": "x"}}',
        "phase missing name": '{"phases":[{"tasks":[{"objective":"o","success_criterion":"c"}]}]}',
        "phase empty name": '{"phases":[{"name":"  ","tasks":[{"objective":"o","success_criterion":"c"}]}]}',
        "empty tasks list": '{"phases":[{"name":"P","tasks":[]}]}',
        "task objective empty": '{"phases":[{"name":"P","tasks":[{"objective":"","success_criterion":"c"}]}]}',
        "criterion not string": '{"phases":[{"name":"P","tasks":[{"objective":"o","success_criterion":123}]}]}',
        "no phases key": '{"foo": "bar"}',
    }
    for label, raw in bad_cases.items():
        obj = _json.loads(raw)
        check(f"rejects: {label}", planner._validate_and_normalize(obj, 5, 5) is None)


def test_never_raises_on_bad_output():
    print("test_never_raises_on_bad_output")
    # run() returns a non-string → must not raise, must return None.
    agent = FakeAgent([12345, None])
    try:
        phases = planner.plan_objective("Obj", agent)
        check("non-string output → None (no raise)", phases is None)
    except Exception as exc:  # pragma: no cover
        check(f"non-string output raised {exc!r}", False)
    # run() itself raises → defensive wrap → None, no propagation.
    ragent = RaisingAgent()
    try:
        phases = planner.plan_objective("Obj", ragent)
        check("agent.run raising → None (no propagation)", phases is None)
        check("provider failure is not retried as malformed JSON",
              len(ragent.run_inputs) == 1)
    except Exception as exc:  # pragma: no cover
        check(f"agent.run exception propagated: {exc!r}", False)


def test_provider_failure_classification():
    print("test_provider_failure_classification")
    cases = [
        (StatusError(401), "provider_authentication", 401),
        (StatusError(403), "provider_authentication", 403),
        (StatusError(429), "provider_rate_limit", 429),
        (TimeoutError("simulated timeout"), "provider_timeout", None),
    ]
    for error, reason, status in cases:
        failures = []
        agent = RaisingAgent(error)
        phases = planner.plan_objective(
            "Objectif", agent, failure_cb=failures.append,
        )
        check(f"{reason}: planning fails closed", phases is None)
        check(f"{reason}: no JSON corrective retry", len(agent.run_inputs) == 1)
        check(
            f"{reason}: sanitized classification",
            len(failures) == 1
            and failures[0].reason == reason
            and failures[0].status_code == status,
        )


def test_failed_plan_leaves_no_checkpoint():
    print("test_failed_plan_leaves_no_checkpoint")
    original = state.TASK_STATE_FILE
    with tempfile.TemporaryDirectory(prefix="osia-planner-failure-") as tmp:
        checkpoint = Path(tmp) / "task_state.json"
        state.TASK_STATE_FILE = checkpoint
        try:
            failures = []
            planned = planner.create_plan_state(
                "Objectif", "deepseek", "deepseek-v4-pro",
                RaisingAgent(StatusError(401)),
                failure_cb=failures.append,
            )
            check("provider failure returns no partial plan", planned is None)
            check("provider failure writes no checkpoint", not checkpoint.exists())
            check("provider authentication reason retained",
                  failures and failures[0].reason == "provider_authentication")
        finally:
            state.TASK_STATE_FILE = original


def test_deeply_nested_no_crash():
    print("test_deeply_nested_no_crash")
    # A runaway model emitting JSON nested tens of thousands of levels deep makes
    # json.loads raise RecursionError (a RuntimeError, NOT a ValueError). The
    # planner must absorb it and return None, never propagate. (Even if a given
    # interpreter's json recursion ceiling is high enough to parse it, the shape
    # is wrong — key "a", not "phases" — so the result is still None either way;
    # the load-bearing assertion is "does NOT raise".)
    depth = 50000
    deep = '{"a":' * depth + '1' + '}' * depth
    agent = FakeAgent([deep, deep])
    try:
        phases = planner.plan_objective("Objectif reel", agent)
        check("deeply-nested JSON → None (no RecursionError escapes)", phases is None)
    except Exception as exc:  # pragma: no cover
        check(f"deeply-nested JSON raised {type(exc).__name__}", False)
    # The standalone extractor honors its own "never raises" docstring too.
    try:
        check("_extract_json_object swallows deep nesting",
              planner._extract_json_object(deep) is None)
    except Exception as exc:  # pragma: no cover
        check(f"_extract_json_object raised {type(exc).__name__}", False)


def test_blank_objective():
    print("test_blank_objective")
    agent = FakeAgent([CLEAN_JSON])
    check("blank objective → None", planner.plan_objective("   ", agent) is None)
    check("blank objective never calls the agent", len(agent.run_inputs) == 0)


def test_caps_truncate():
    print("test_caps_truncate")
    raw = ('{"phases":[{"name":"P","tasks":['
           '{"objective":"a","success_criterion":"ca"},'
           '{"objective":"b","success_criterion":"cb"},'
           '{"objective":"c","success_criterion":"cc"}]}]}')
    agent = FakeAgent([raw])
    phases = planner.plan_objective("Obj", agent, max_phases=1, max_tasks_per_phase=2)
    check("tasks truncated to cap", len(phases[0]["tasks"]) == 2)
    check("truncated ids contiguous",
          [t["id"] for t in phases[0]["tasks"]] == ["0.0", "0.1"])


def test_create_plan_state_no_disk_io():
    print("test_create_plan_state_no_disk_io")
    # Sabotage every disk-writing entry point: if the planner touches disk, fail.
    calls = []
    orig_save = state.save_task_state
    orig_write = state._write_atomic
    orig_clear = state.clear_task_state
    state.save_task_state = lambda *a, **k: calls.append("save")
    state._write_atomic = lambda *a, **k: calls.append("write")
    state.clear_task_state = lambda *a, **k: calls.append("clear")
    try:
        agent = FakeAgent([CLEAN_JSON])
        st = planner.create_plan_state("Mon objectif", "anthropic",
                                       "claude-opus-4-8", agent)
        check("create_plan_state returns a dict", isinstance(st, dict))
        check("state has schema_version", st.get("schema_version") == state.SCHEMA_VERSION)
        check("objective stored verbatim", st["objective"] == "Mon objectif")
        check("provider/model stored", st["provider"] == "anthropic"
              and st["model"] == "claude-opus-4-8")
        check("status starts in_progress", st["status"] == "in_progress")
        check("planned phases carried into state",
              len(st["phases"]) == 2 and st["phases"][0]["tasks"][0]["id"] == "0.0")
        check("NO disk I/O performed by planner", calls == [])
        # Failure path also writes nothing and returns None.
        bad_agent = FakeAgent([MALFORMED_JSON, MALFORMED_JSON])
        st2 = planner.create_plan_state("X", "anthropic", "m", bad_agent)
        check("create_plan_state → None on planning failure", st2 is None)
        check("still NO disk I/O on failure", calls == [])
    finally:
        state.save_task_state = orig_save
        state._write_atomic = orig_write
        state.clear_task_state = orig_clear


def main():
    tests = [
        test_clean_json,
        test_fenced_json,
        test_malformed_then_none,
        test_shape_rejects_missing_criterion,
        test_more_shape_rejections,
        test_never_raises_on_bad_output,
        test_provider_failure_classification,
        test_failed_plan_leaves_no_checkpoint,
        test_deeply_nested_no_crash,
        test_blank_objective,
        test_caps_truncate,
        test_create_plan_state_no_disk_io,
    ]
    for t in tests:
        t()
    print(f"\nALL TESTS PASSED ({_passed} checks across {len(tests)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
