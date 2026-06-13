#!/usr/bin/env python3
"""OSIA autonomous planner — objective → phases/tasks (WOR-45 Phase 2).

The planner turns a single top-level *objective* into the ``phases``/``tasks``
structure consumed by :func:`osia.autonomous.state.new_task_state`. It does so
with **ONE** LLM planning run (plus, at most, ONE corrective retry), reusing a
ready :class:`osia.agent.OSIAAgent` as a black box — it only ever calls
``agent.reset()`` and ``agent.run(...)``; it never touches the agent loop, the
tool-call parser, ``_AntiLoopGuard`` or any protected internals (see CLAUDE.md).

This is **Phase 2 — the planner ONLY**. There is no execution loop here (that is
Phase 3). The module is also **pure**: no disk I/O, no GUI, no network of its
own. Saving the resulting state is :mod:`osia.autonomous.state`'s job — the
caller decides when to persist.

Two public entry points:

* :func:`plan_objective` — objective + ready agent → a validated, normalized
  ``phases`` list (state schema) or ``None`` on failure (never raises).
* :func:`create_plan_state` — convenience wrapper that feeds the planned phases
  into :func:`osia.autonomous.state.new_task_state` and returns the (unsaved)
  state dict, or ``None``.

Design notes
------------
* **Anti "marked-complete-without-testing".** Every task carries a
  plain-language ``success_criterion`` — an *observable* test of doneness — so a
  later execution phase can verify a task instead of trusting a self-reported
  "done". The planning prompt makes this mandatory.
* **Robust JSON extraction.** Models wrap JSON in prose / markdown fences despite
  instructions, so we strip fences, brace-match the first complete ``{...}``
  object (string-aware), and validate the exact shape. On any parse/shape
  failure we retry the run ONCE with a corrective "raw JSON only" message; if it
  still fails we return ``None`` — the planner never raises on bad model output.
* **Language-aware (trivially).** The instructions are in English (the model
  follows them reliably), but the prompt tells the model to write the phase
  names, task objectives and success criteria in the SAME language as the
  objective — so the *plan* speaks the user's language without us doing any
  language detection.
"""

import json
import re

from osia.autonomous import state

# ── Bounded autonomy defaults ────────────────────────────────────────────────
# A SMALL plan is the goal (research: long, sprawling plans drift). These are
# hard caps: the prompt asks the model to stay under them AND we defensively
# truncate anything beyond them so a runaway plan can never blow up the harness.
DEFAULT_MAX_PHASES = 5
DEFAULT_MAX_TASKS_PER_PHASE = 5

# The exact JSON shape the model must return — reused verbatim in both the
# initial prompt and the corrective retry so the contract is stated identically.
_SCHEMA_HINT = (
    '{"phases":[{"name":"...","tasks":'
    '[{"objective":"...","success_criterion":"..."}]}]}'
)


# ── Prompt construction ──────────────────────────────────────────────────────

def _build_planning_prompt(objective: str, max_phases: int,
                           max_tasks_per_phase: int) -> str:
    """Build the strict planning prompt for ``objective``.

    Instructs the model to decompose the objective into a SMALL number of
    phases, each with a SMALL number of concrete, independently-verifiable
    tasks, each carrying an observable ``success_criterion``, and to return ONLY
    raw JSON in the exact :data:`_SCHEMA_HINT` shape.
    """
    return (
        "You are a planning module. Decompose the OBJECTIVE below into a short, "
        "concrete, executable plan.\n\n"
        f"OBJECTIVE:\n{objective}\n\n"
        "Rules:\n"
        f"- Use a SMALL number of phases (at most {max_phases}). Each phase is a "
        "milestone toward the objective.\n"
        f"- Each phase has a SMALL number of tasks (at most "
        f"{max_tasks_per_phase}). Each task must be concrete and independently "
        "verifiable — something that can be done and checked on its own.\n"
        "- For EACH task, write a `success_criterion`: a short, plain-language, "
        "OBSERVABLE test that proves the task is actually done (e.g. \"the file "
        "report.txt exists and is non-empty\", \"the page shows the order "
        "confirmation number\", \"the command prints OK\"). It MUST be checkable "
        "by inspection — never \"the task is marked complete\".\n"
        "- Write `name`, `objective` and `success_criterion` in the SAME "
        "LANGUAGE as the OBJECTIVE above.\n\n"
        "Return ONLY raw JSON — no prose, no explanation, no markdown code "
        "fences. Output EXACTLY this shape and nothing else:\n"
        f"{_SCHEMA_HINT}\n"
    )


_CORRECTIVE_PROMPT = (
    "Your previous response could not be parsed as the required plan. Return "
    "ONLY raw JSON — no prose, no markdown code fences — in EXACTLY this schema "
    "and nothing else:\n"
    f"{_SCHEMA_HINT}\n"
    'Every phase needs a non-empty "name" and a non-empty "tasks" list; every '
    'task needs a non-empty "objective" and a non-empty "success_criterion".'
)


# ── Robust JSON extraction ───────────────────────────────────────────────────

def _strip_code_fences(text: str) -> str:
    """Remove a leading ```/```lang fence line and a trailing ``` fence.

    Belt-and-suspenders: :func:`_find_brace_span` already ignores anything
    outside the outer braces, but stripping fences first keeps pathological
    inputs simple and mirrors the documented extraction steps.
    """
    t = text.strip()
    t = re.sub(r"^```[^\n]*\n", "", t)
    t = re.sub(r"\n```\s*$", "", t)
    return t


def _find_brace_span(s: str):
    """Return the first complete, brace-balanced ``{...}`` substring of ``s``.

    Scans from the first ``{`` tracking brace depth while respecting JSON string
    literals (so braces inside strings don't unbalance the count), and returns
    the slice when depth returns to zero — i.e. the first ``{`` up to its
    matching ``}``. Returns ``None`` if there is no first ``{`` or it never
    closes (truncated output).
    """
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def _extract_json_object(text):
    """Extract and parse the first JSON object from raw model output.

    Strips markdown fences, then tries (a) the string-aware brace-matched span
    and (b) a first-``{``-to-last-``}`` fallback, parsing each with
    :func:`json.loads`. Returns the parsed ``dict`` or ``None`` — never raises,
    whatever the model produced (non-string, prose, truncated JSON, …).
    """
    if not isinstance(text, str):
        return None
    s = _strip_code_fences(text)
    candidates = []
    span = _find_brace_span(s)
    if span:
        candidates.append(span)
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(s[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError, RecursionError):
            # RecursionError (a RuntimeError, NOT a ValueError/TypeError) is what
            # json.loads raises on JSON nested thousands of levels deep — exactly
            # the runaway model output this extractor must absorb. Caught so the
            # function honors its "never raises" contract; the stack has fully
            # unwound here, so trying the next candidate is safe.
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ── Shape validation + normalization into the state schema ───────────────────

def _nonempty_str(value) -> bool:
    """True iff ``value`` is a non-empty (after stripping) string."""
    return isinstance(value, str) and bool(value.strip())


def _validate_and_normalize(obj, max_phases: int, max_tasks_per_phase: int):
    """Validate the planning dict's shape and normalize it to the state schema.

    Required shape: ``obj["phases"]`` is a non-empty list; each phase is a dict
    with a non-empty ``name`` and a non-empty ``tasks`` list; each task is a dict
    with non-empty ``objective`` and ``success_criterion`` strings. On ANY shape
    violation, returns ``None`` (the caller retries / gives up gracefully).

    On success, returns a ``phases`` list ready for
    :func:`osia.autonomous.state.new_task_state`: task ids are
    ``"<phase_idx>.<task_idx>"``, every ``status`` is ``"pending"``, with
    ``attempts=0``, ``distilled_summary=""`` and ``last_error=None``. Phases and
    tasks beyond the caps are defensively truncated.
    """
    if not isinstance(obj, dict):
        return None
    phases = obj.get("phases")
    if not isinstance(phases, list) or not phases:
        return None

    out_phases = []
    for p_idx, phase in enumerate(phases):
        if max_phases and p_idx >= max_phases:
            break
        if not isinstance(phase, dict) or not _nonempty_str(phase.get("name")):
            return None
        tasks = phase.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return None

        out_tasks = []
        for t_idx, task in enumerate(tasks):
            if max_tasks_per_phase and t_idx >= max_tasks_per_phase:
                break
            if not isinstance(task, dict):
                return None
            objective = task.get("objective")
            criterion = task.get("success_criterion")
            if not _nonempty_str(objective) or not _nonempty_str(criterion):
                return None
            out_tasks.append({
                "id": f"{p_idx}.{t_idx}",
                "objective": objective.strip(),
                "success_criterion": criterion.strip(),
                "status": "pending",
                "attempts": 0,
                "distilled_summary": "",
                "last_error": None,
            })
        if not out_tasks:  # defensive: never emit a task-less phase
            return None
        out_phases.append({
            "name": phase["name"].strip(),
            "status": "pending",
            "tasks": out_tasks,
        })

    if not out_phases:
        return None
    return out_phases


# ── One planning attempt ─────────────────────────────────────────────────────

def _attempt(agent, message, max_phases, max_tasks_per_phase):
    """Run ONE planning message through the agent and parse/validate the reply.

    Returns the normalized ``phases`` list, or ``None`` if the agent errored or
    its output failed extraction/validation. The whole attempt — ``agent.run``
    plus extraction and validation — is wrapped in ``except Exception`` so a
    transient provider error OR any pathological model output (e.g. JSON nested
    deep enough to make ``json.loads`` raise ``RecursionError``) yields ``None``
    instead of crashing the caller. This is the module-level enforcement of the
    "never raises on bad model output" contract.
    """
    try:
        raw = agent.run(message)
        obj = _extract_json_object(raw)
        if obj is None:
            return None
        return _validate_and_normalize(obj, max_phases, max_tasks_per_phase)
    except Exception:
        return None


# ── Public API ───────────────────────────────────────────────────────────────

def plan_objective(objective: str, agent, *,
                   max_phases: int = DEFAULT_MAX_PHASES,
                   max_tasks_per_phase: int = DEFAULT_MAX_TASKS_PER_PHASE):
    """Decompose ``objective`` into a validated, normalized ``phases`` list.

    ``agent`` is a ready :class:`osia.agent.OSIAAgent` (already model-set by the
    caller). This does ONE planning run — ``agent.reset()`` then
    ``agent.run(planning_prompt)`` — extracts and validates the JSON plan, and on
    any parse/shape failure retries ONCE with a corrective "raw JSON only"
    message (keeping the failed turn in context so the model can self-correct).

    Returns the ``phases`` list ready for
    :func:`osia.autonomous.state.new_task_state`, or ``None`` if planning failed
    after the retry (blank objective, agent error, or unparseable/invalid
    output). **Never raises** on bad model output.
    """
    if not _nonempty_str(objective):
        return None
    objective = objective.strip()
    prompt = _build_planning_prompt(objective, max_phases, max_tasks_per_phase)

    # First attempt: fresh context (reset clears history except the system
    # prompt). The agent is the caller's; reset() is part of its public contract.
    agent.reset()
    phases = _attempt(agent, prompt, max_phases, max_tasks_per_phase)
    if phases is not None:
        return phases

    # Corrective retry — NO reset, so the model sees its own bad output plus a
    # restated exact-schema demand. Exactly one retry; its result (possibly
    # None) is final.
    return _attempt(agent, _CORRECTIVE_PROMPT, max_phases, max_tasks_per_phase)


def create_plan_state(objective: str, provider: str, model: str, agent, *,
                      max_phases: int = DEFAULT_MAX_PHASES,
                      max_tasks_per_phase: int = DEFAULT_MAX_TASKS_PER_PHASE):
    """Plan ``objective`` and build a fresh (UNSAVED) ``task_state`` dict.

    Thin convenience over :func:`plan_objective` +
    :func:`osia.autonomous.state.new_task_state`. Returns the state dict, or
    ``None`` if planning failed. Performs **no disk I/O** — the planner never
    saves; persisting the returned state (via
    :func:`osia.autonomous.state.save_task_state`) is the caller's decision.
    """
    phases = plan_objective(objective, agent, max_phases=max_phases,
                            max_tasks_per_phase=max_tasks_per_phase)
    if phases is None:
        return None
    return state.new_task_state(objective, provider, model, phases)
