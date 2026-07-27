#!/usr/bin/env python3
"""Standalone tests for osia.autonomous.orchestrator (WOR-45 Phase 3).

Mocks the agent entirely — NO network, NO real OSIAAgent — and mocks
``state.save_task_state`` so NO disk I/O happens (each test counts checkpoints
instead). Runs anywhere with a plain ``python3 osia/autonomous/test_orchestrator.py``
(or ``python3 -m osia.autonomous.test_orchestrator``).

Scenarios:
  * a 2-phase / 3-task plan driven to full completion (every verify DONE)
  * a task that fails max_attempts, then succeeds after a replan
  * a task that exhausts the replan budget → status "failed"
  * stop() requested DURING a task → loop exits, nothing corrupted
  * resume() with the one-shot re-check salvaging an already-satisfied task
    (NOT re-executed)
  * the deterministic file pre-check (decisive accept / reject on a real temp
    file) plus the robust DONE/NOT_DONE verdict parser

Every test bounds the background thread with a join timeout, so a stuck loop
fails loudly instead of hanging. The orchestrator is only ever driven through
its public surface (start/resume/stop) + private thread join; the agent, the
state mutators and the planner are used as black boxes (their source is never
modified).
"""

import os
import sys
import tempfile
import threading

# Make the repo root importable when run as a bare script (…/osia/autonomous/
# → …/osia → <repo>).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from osia.autonomous import orchestrator  # noqa: E402
from osia.autonomous import state  # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────────────────

class ScriptedAgent:
    """Prompt-aware stand-in for OSIAAgent.

    Routes ``run()`` by inspecting the prompt the orchestrator built:
      * planner prompts (contain "ONLY raw JSON")     → ``plan``
      * verification prompts (contain "DONE or NOT_DONE") → ``judge``
      * everything else (subtask execution)           → ``exec_result``

    Each of ``exec_result`` / ``judge`` / ``plan`` may be a plain string or a
    zero-arg callable (so a test can drive a stateful sequence). Records run/reset
    calls for assertions.
    """

    def __init__(self, *, exec_result="step output", judge="DONE", plan=""):
        self.exec_result = exec_result
        self.judge = judge
        self.plan = plan
        self.run_inputs = []
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def run(self, user_input, *args, **kwargs):
        self.run_inputs.append(user_input)
        text = user_input or ""
        if "ONLY raw JSON" in text:            # planner (initial or corrective)
            return _resolve(self.plan)
        if "DONE or NOT_DONE" in text:         # LLM judge
            return _resolve(self.judge)
        return _resolve(self.exec_result, text)  # subtask execution

    # convenience for assertions
    @property
    def exec_inputs(self):
        return [t for t in self.run_inputs if "STEP OBJECTIVE" in (t or "")]

    @property
    def judge_inputs(self):
        return [t for t in self.run_inputs if "DONE or NOT_DONE" in (t or "")]


def _resolve(value, *args):
    """A plain value, or a callable invoked with the given args (callables
    accept ``*args`` so passing the prompt or nothing both work)."""
    return value(*args) if callable(value) else value


def seq(items, default="DONE"):
    """A stateful callable that pops the next scripted item, then ``default``."""
    box = list(items)

    def _next(*_a):
        return box.pop(0) if box else default
    return _next


VALID_PLAN = (
    '{"phases":[{"name":"Recovery","tasks":['
    '{"objective":"Alternative step","success_criterion":'
    '"the alternative path shows success"}]}]}'
)


# ── save_task_state mock (count checkpoints, zero disk I/O) ───────────────────

class SaveCounter:
    """Context manager that swaps state.save_task_state for a counter."""

    def __init__(self):
        self.count = 0
        self._orig = None

    def __enter__(self):
        self._orig = state.save_task_state
        state.save_task_state = lambda *a, **k: self._bump()
        return self

    def _bump(self):
        self.count += 1

    def __exit__(self, *exc):
        state.save_task_state = self._orig
        return False


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


def run_to_completion(orch, label, timeout=5.0):
    """Join the orchestrator's worker thread, asserting it actually finished."""
    t = orch._thread
    if t is not None:
        t.join(timeout=timeout)
        check(f"{label}: loop thread terminated (no hang)",
              not t.is_alive())


def two_phase_plan():
    """phase0: 2 tasks, phase1: 1 task — non-file criteria (→ LLM judge)."""
    phases = [
        {"name": "P0", "status": "pending", "tasks": [
            {"id": "0.0", "objective": "do A",
             "success_criterion": "A is visibly complete", "status": "pending",
             "attempts": 0, "distilled_summary": "", "last_error": None},
            {"id": "0.1", "objective": "do B",
             "success_criterion": "B is visibly complete", "status": "pending",
             "attempts": 0, "distilled_summary": "", "last_error": None},
        ]},
        {"name": "P1", "status": "pending", "tasks": [
            {"id": "1.0", "objective": "do C",
             "success_criterion": "C is visibly complete", "status": "pending",
             "attempts": 0, "distilled_summary": "", "last_error": None},
        ]},
    ]
    return _authorized_test_state(
        state.new_task_state(
            "overall goal", "anthropic", "claude-opus-4-8", phases,
        )
    )


def one_task_plan(criterion="the work is visibly complete"):
    phases = [
        {"name": "P0", "status": "pending", "tasks": [
            {"id": "0.0", "objective": "do the thing",
             "success_criterion": criterion, "status": "pending",
             "attempts": 0, "distilled_summary": "", "last_error": None},
        ]},
    ]
    return _authorized_test_state(
        state.new_task_state(
            "overall goal", "anthropic", "claude-opus-4-8", phases,
        )
    )


def _authorized_test_state(st):
    """Model a completed review; authorization itself has focused tests."""
    st["run_state"] = "ready"
    st["authorization_status"] = "authorized"
    st["authorization_source"] = "task_scoped"
    return st


# ── Tests ────────────────────────────────────────────────────────────────────

def test_full_completion():
    print("test_full_completion")
    events = []
    agent = ScriptedAgent(exec_result="did it", judge="DONE")
    orch = orchestrator.Orchestrator(agent, progress_cb=events.append)
    st = two_phase_plan()
    with SaveCounter() as saves:
        orch.start(st)
        run_to_completion(orch, "full")
    check("status done", st["status"] == "done")
    check("cursor walked past the last phase", st["cursor"] == {"phase": 2, "task": 0})
    all_tasks = [t for p in st["phases"] for t in p["tasks"]]
    check("every task done", all(t["status"] == "done" for t in all_tasks))
    check("every task attempted exactly once",
          all(t["attempts"] == 1 for t in all_tasks))
    check("both phases done", all(p["status"] == "done" for p in st["phases"]))
    # Checkpoints: begin(1) + per task [CHECKPOINT A + CHECKPOINT B]=2*3 + done(1) = 8
    check(f"checkpoint at each transition (saw {saves.count})", saves.count == 8)
    check("emitted one task_done per task",
          [e["event"] for e in events].count("task_done") == 3)
    check("emitted final done", events[-1]["event"] == "done")
    check("digest accumulated all three summaries",
          all(tid in st["context_digest"] for tid in ("0.0", "0.1", "1.0")))
    # The agent was only driven through reset()+run() — never mutated otherwise.
    check("agent reset before every execution and every verify",
          agent.reset_calls == len(agent.run_inputs))


def test_fail_then_replan_success():
    print("test_fail_then_replan_success")
    # Task A fails its 2 attempts (judge NOT_DONE x2), replan yields task B which
    # the judge then passes (DONE).
    agent = ScriptedAgent(exec_result="tried",
                          judge=seq(["NOT_DONE", "NOT_DONE", "DONE"]),
                          plan=VALID_PLAN)
    events = []
    orch = orchestrator.Orchestrator(agent, progress_cb=events.append)
    st = one_task_plan()
    st["budgets"]["max_attempts_per_task"] = 2
    st["budgets"]["max_replans"] = 1
    with SaveCounter() as saves:
        orch.start(st)
        run_to_completion(orch, "replan")
        check("replan stops for fresh authorization review",
              st["run_state"] == "authorization_pending")
        _authorized_test_state(st)
        orch.start(st)
        run_to_completion(orch, "authorized replan")
    check("overall status done (objective met via replan)", st["status"] == "done")
    a = st["phases"][0]["tasks"][0]
    check("failed task capped at max_attempts (2)", a["attempts"] == 2)
    check("failed task marked failed", a["status"] == "failed")
    check("original phase marked failed", st["phases"][0]["status"] == "failed")
    check("a recovery phase was appended", len(st["phases"]) == 2)
    b = st["phases"][1]["tasks"][0]
    check("recovery task done", b["status"] == "done")
    check("replan_count incremented to 1", st["budgets"]["replan_count"] == 1)
    check("cursor past both phases", st["cursor"] == {"phase": 2, "task": 0})
    evs = [e["event"] for e in events]
    check("emitted task_failed for the original task", "task_failed" in evs)
    check("emitted final done", evs[-1] == "done")
    check("checkpoints were written (>0)", saves.count > 0)


def test_exhaust_replans_failed():
    print("test_exhaust_replans_failed")
    # Judge ALWAYS NOT_DONE; planner ALWAYS returns a plan. With max_attempts=1
    # and max_replans=1: task A fails → replan(1) → task B fails → no budget → FAIL.
    agent = ScriptedAgent(exec_result="tried", judge="NOT_DONE", plan=VALID_PLAN)
    events = []
    orch = orchestrator.Orchestrator(agent, progress_cb=events.append)
    st = one_task_plan()
    st["budgets"]["max_attempts_per_task"] = 1
    st["budgets"]["max_replans"] = 1
    with SaveCounter() as saves:
        orch.start(st)
        run_to_completion(orch, "exhaust")
        check("replan stops for fresh authorization review",
              st["run_state"] == "authorization_pending")
        _authorized_test_state(st)
        orch.start(st)
        run_to_completion(orch, "authorized exhaust")
    check("overall status failed", st["status"] == "failed")
    check("replan_count capped at max_replans (1)",
          st["budgets"]["replan_count"] == 1)
    check("exactly one recovery phase appended", len(st["phases"]) == 2)
    check("both tasks marked failed",
          st["phases"][0]["tasks"][0]["status"] == "failed"
          and st["phases"][1]["tasks"][0]["status"] == "failed")
    evs = [e["event"] for e in events]
    check("emitted final failed", evs[-1] == "failed")
    check("checkpoints were written (>0)", saves.count > 0)


def test_stop_mid_task():
    print("test_stop_mid_task")
    agent = ScriptedAgent(judge="DONE")
    orch = orchestrator.Orchestrator(agent)

    # Simulate stop() landing WHILE the first task's execution runs: the exec
    # callback sets the stop event, then the loop must persist and bail out
    # before verifying / advancing.
    def exec_then_stop(*_a):
        orch._stop.set()
        return "partial"
    agent.exec_result = exec_then_stop

    st = two_phase_plan()
    with SaveCounter() as saves:
        orch.start(st)
        run_to_completion(orch, "stop")
    a = st["phases"][0]["tasks"][0]
    check("stopped task left in_progress (not done/failed)",
          a["status"] == "in_progress")
    check("attempt was recorded before the stop", a["attempts"] == 1)
    check("cursor did NOT advance past the stopped task",
          st["cursor"] == {"phase": 0, "task": 0})
    check("overall status still in_progress", st["status"] == "in_progress")
    check("no verify ran after stop (judge never called)",
          len(agent.judge_inputs) == 0)
    check("checkpoint A + stop-save written (>=2)", saves.count >= 2)


def test_resume_salvage():
    print("test_resume_salvage")
    # A task interrupted mid-run (in_progress, attempts=1). On resume the one-shot
    # re-check verifies it DONE and salvages it WITHOUT re-executing.
    agent = ScriptedAgent(judge="DONE")  # exec would be wrong to call here
    events = []
    orch = orchestrator.Orchestrator(agent, progress_cb=events.append)
    st = one_task_plan(criterion="the page shows the confirmation")  # → LLM judge
    task = st["phases"][0]["tasks"][0]
    task["status"] = "in_progress"
    task["attempts"] = 1
    st["phases"][0]["status"] = "in_progress"
    with SaveCounter() as saves:
        orch.resume(st)
        run_to_completion(orch, "resume")
    check("salvaged task marked done", task["status"] == "done")
    check("salvage did NOT bump attempts beyond the recorded 1", task["attempts"] == 1)
    check("task was NEVER re-executed (no STEP OBJECTIVE prompt sent)",
          len(agent.exec_inputs) == 0)
    check("exactly one verify (the salvage re-check) ran",
          len(agent.judge_inputs) == 1)
    check("overall status done", st["status"] == "done")
    check("cursor advanced past the salvaged task", st["cursor"] == {"phase": 1, "task": 0})
    evs = [e["event"] for e in events]
    check("emitted a salvaged event", "salvaged" in evs)
    check("emitted final done", evs[-1] == "done")
    check("checkpoints were written (>0)", saves.count > 0)


def test_resume_recheck_not_satisfied_reexecutes():
    print("test_resume_recheck_not_satisfied_reexecutes")
    # On resume the interrupted task's re-check FAILS (NOT_DONE), so it must be
    # re-executed normally and (this time) pass.
    agent = ScriptedAgent(exec_result="redo",
                          judge=seq(["NOT_DONE", "DONE"]))
    orch = orchestrator.Orchestrator(agent)
    st = one_task_plan(criterion="the page shows the confirmation")
    task = st["phases"][0]["tasks"][0]
    task["status"] = "in_progress"
    task["attempts"] = 1
    with SaveCounter():
        orch.resume(st)
        run_to_completion(orch, "resume-reexec")
    check("task ultimately done", task["status"] == "done")
    check("task was re-executed after a failed re-check (attempts → 2)",
          task["attempts"] == 2)
    check("the subtask WAS executed once on the re-run",
          len(agent.exec_inputs) == 1)
    check("overall status done", st["status"] == "done")


def test_file_precheck_decisive():
    print("test_file_precheck_decisive")
    # An agent whose JUDGE would WRONGLY say DONE — proving the deterministic file
    # check overrides the (untrusted) LLM verdict on a missing file.
    agent = ScriptedAgent(judge="DONE")
    orch = orchestrator.Orchestrator(agent)

    fd, path = tempfile.mkstemp(prefix="osia_orch_", suffix=".txt")
    try:
        os.write(fd, b"content")
        os.close(fd)
        crit_exists = f'the file "{path}" exists and is non-empty'
        ok, _ = orch._verify({"success_criterion": crit_exists}, "ignored")
        check("present non-empty file → decisive ACCEPT (no judge needed)", ok is True)
        check("decisive accept did NOT call the agent", len(agent.run_inputs) == 0)

        # Empty file but criterion demands non-empty → decisive REJECT.
        open(path, "w").close()
        ok2, _ = orch._verify({"success_criterion": crit_exists}, "ignored")
        check("present but empty file → decisive REJECT", ok2 is False)
        check("decisive reject still did NOT call the agent", len(agent.run_inputs) == 0)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    # Missing file → decisive REJECT, overriding the judge's bogus DONE.
    missing = os.path.join(tempfile.gettempdir(), "osia_orch_definitely_absent_xyz.txt")
    if os.path.exists(missing):  # pragma: no cover
        os.unlink(missing)
    ok3, _ = orch._verify(
        {"success_criterion": f'the file {missing} exists'}, "ignored")
    check("missing file → decisive REJECT despite judge='DONE'", ok3 is False)
    check("missing-file reject did NOT call the agent", len(agent.run_inputs) == 0)


def test_file_precheck_defers_ambiguous():
    print("test_file_precheck_defers_ambiguous")
    # Regression for the adversarial-review findings: the deterministic layer must
    # NOT decisively reject on relative filenames (cwd ≠ agent cwd) or on path-ish
    # junk tokens (version numbers / decimals / domains) — it defers those to the
    # judge instead, so a genuinely-completed task never burns its whole budget.
    agent = ScriptedAgent(judge="DONE")
    orch = orchestrator.Orchestrator(agent)
    fp = orch._file_precheck
    extract = orchestrator.Orchestrator._extract_paths

    check("relative file criterion → defer (None), NOT reject",
          fp("the file report.txt exists and is non-empty") is None)
    check("version-number token → defer (None), NOT reject",
          fp("a file was generated; the report version is 2.3.1") is None)
    check("domain token → defer (None), NOT reject",
          fp("the page at example.com is created and shows the confirmation") is None)
    check("no agent call made while deferring (precheck is pure)",
          len(agent.run_inputs) == 0)

    # _extract_paths no longer misclassifies pure version tokens, but still keeps
    # real filenames.
    toks = extract("the report version is 2.3.1 and report.csv was created")
    check("_extract_paths drops pure version tokens", "2.3.1" not in toks)
    check("_extract_paths keeps the real filename", "report.csv" in toks)

    # An ABSOLUTE present file still yields a decisive accept even with junk tokens.
    fd, path = tempfile.mkstemp(prefix="osia_orch_abs_", suffix=".txt")
    try:
        os.write(fd, b"x")
        os.close(fd)
        crit = f'the file "{path}" was created (build 1.2)'
        check("absolute present file → decisive ACCEPT despite junk token",
              fp(crit) is True)
        check("absolute-decisive path still made no agent call",
              len(agent.run_inputs) == 0)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_verdict_parser():
    print("test_verdict_parser")
    pv = orchestrator.Orchestrator._parse_verdict
    check("DONE → True", pv("DONE\nbecause it worked")[0] is True)
    check("done lowercase → True", pv("done")[0] is True)
    check("NOT_DONE → False", pv("NOT_DONE\nstill missing")[0] is False)
    check("NOT DONE (space) → False", pv("NOT DONE")[0] is False)
    check("not-done (hyphen) → False", pv("not-done, nope")[0] is False)
    check("NOTDONE (glued) → False", pv("NOTDONE")[0] is False)
    check("ABANDONED does NOT read as DONE → False (ambiguous)",
          pv("ABANDONED the attempt")[0] is False)
    check("empty reply → False", pv("")[0] is False)
    check("ambiguous prose → False", pv("maybe, I am not sure")[0] is False)
    # NOT_DONE must win even if 'DONE' also appears later in the text.
    check("NOT_DONE wins over a later DONE → False",
          pv("NOT_DONE — the file is not DONE yet")[0] is False)


def test_idempotent_start():
    print("test_idempotent_start")
    # A blocking exec keeps the first thread alive so a second start() is a no-op.
    gate = threading.Event()
    agent = ScriptedAgent(judge="DONE")

    def blocking_exec(*_a):
        gate.wait(2.0)
        return "ok"
    agent.exec_result = blocking_exec

    orch = orchestrator.Orchestrator(agent)
    st = two_phase_plan()
    with SaveCounter():
        orch.start(st)
        first = orch._thread
        orch.start(st)  # must NOT spawn a second loop
        check("second start() did not replace the running thread",
              orch._thread is first)
        gate.set()
        run_to_completion(orch, "idempotent")
    check("plan still completes after the no-op second start", st["status"] == "done")


def main():
    tests = [
        test_full_completion,
        test_fail_then_replan_success,
        test_exhaust_replans_failed,
        test_stop_mid_task,
        test_resume_salvage,
        test_resume_recheck_not_satisfied_reexecutes,
        test_file_precheck_decisive,
        test_file_precheck_defers_ambiguous,
        test_verdict_parser,
        test_idempotent_start,
    ]
    for t in tests:
        t()
    print(f"\nALL TESTS PASSED ({_passed} checks across {len(tests)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
