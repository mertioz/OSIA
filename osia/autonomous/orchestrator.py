#!/usr/bin/env python3
"""OSIA autonomous orchestrator / execution loop (WOR-45 Phase 3).

This is the **engine only** — the background loop that walks the cursor through
the ``phases``/``tasks`` of a planned ``task_state`` (see
:mod:`osia.autonomous.state`), runs each task through a *dedicated* agent,
**verifies** doneness against the task's ``success_criterion`` (fail-closed, so a
self-reported "done" is never trusted), checkpoints after every transition, and
honours the per-task attempt budget and the overall replan budget. Bounded
autonomy: every loop path either advances the cursor, increments an attempt
(capped at ``max_attempts_per_task``) or increments a replan (capped at
``max_replans``) — so it always terminates.

It mirrors the :class:`osia.cron.CronManager` daemon pattern: a single
``daemon=True`` background thread bracketed by :meth:`start`/:meth:`resume` and
:meth:`stop`, driven by a :class:`threading.Event`.

Scope (Phase 3 — engine ONLY):

* NO ``main.py`` wiring, NO ``--autonomous`` entry point, NO GUI/island feedback
  (those are Phase 4 — surfaced here only through the optional ``progress_cb``).
* The agent (:meth:`OSIAAgent.run` / :meth:`OSIAAgent.reset`), the
  :mod:`~osia.autonomous.state` mutators and the
  :mod:`~osia.autonomous.planner` are used strictly as **black boxes**. This
  module never touches the agent loop, the tool-call parser, ``_AntiLoopGuard``,
  ``run()``/``_run_turn`` internals, the WebSocket protocol, ``key_chain``,
  ``CONFIG_DIR`` resolution or any other protected contract (see CLAUDE.md).
* No Qt import: ``progress_cb`` is a plain ``Callable[[dict], None]`` (default
  no-op) and every call to it is wrapped so a GUI error can never kill the loop.

Naming note: the *module* :mod:`osia.autonomous.state` is imported as ``state``
(as in :mod:`~osia.autonomous.planner`); a *task_state dict* is always the local
``st`` — so ``state.set_task_status(st, ...)`` never shadows one with the other.
"""

import os
import re
import inspect
import json
import threading
from contextlib import contextmanager
from typing import NamedTuple

from osia import document_artifacts, document_sources
from osia.document_progress import document_progress_scope
from osia.autonomous import authorization, diagnostics, planner
# Module-qualified on purpose (like planner.py) so the persistence/mutator layer
# stays a single black box — and so a test can monkeypatch state.save_task_state.
from osia.autonomous import state
from osia import i18n
from osia.turn_context import fixed_turn_context, normalize_locale

# ── Tunables (module constants) ──────────────────────────────────────────────
# Rolling context digest: keep it SMALL so it never balloons the subtask prompt.
DIGEST_MAX_CHARS = 2000       # hard char cap on the whole digest (FIFO-drop oldest)
DIGEST_MAX_LINES = 6          # K — keep at most this many digest lines
SUMMARY_MAX_CHARS = 300       # per-task distilled summary cap (cheap string trim)
ARTIFACT_CONTEXT_MAX_CHARS = 4096
DOCUMENT_FINAL_MAX_CHARS = 8000

# ── Verifier keyword heuristics (deterministic pre-check, layer 1) ───────────
# Stems/words that mark a criterion as a *file-existence* claim. Stems (e.g.
# "enregistr") catch conjugations. English + French (the planner writes the
# criterion in the objective's language).
_FILE_KEYWORDS = (
    "file", "exists", "exist", "created", "saved", "written", "generated",
    "fichier", "existe", "créé", "cree", "enregistr", "sauvegard", "écrit",
    "ecrit", "généré", "genere",
)
# Presence ⇒ the file must also be non-empty (getsize > 0). "vide" covers
# "non vide" (non-empty) and bare "vide" (empty) — either way we demand content.
_NONEMPTY_KEYWORDS = (
    "non-empty", "nonempty", "non empty", "not empty",
    "non vide", "non-vide", "pas vide", "vide",
)
# Presence ⇒ the criterion also asserts a NON-file observation we cannot confirm
# from disk alone ⇒ never decisively *accept*; defer to the LLM judge instead.
_NONFILE_OBSERVABLE_KEYWORDS = (
    "show", "shows", "display", "print", "prints", "page", "screen", "écran",
    "ecran", "confirmation", "message", "says", "contains", "contient",
    "matches", "output", "console", "affich", "montre", "réponse", "reponse",
)


class _VerificationOutcome(NamedTuple):
    ok: bool
    category: diagnostics.FailureCategory | None
    reason_code: str


_AGENT_RUN_FAILURE_CATEGORIES = frozenset({
    diagnostics.FailureCategory.STAGNATION_TIMEOUT,
    diagnostics.FailureCategory.HARD_TOTAL_CEILING,
    diagnostics.FailureCategory.REPEATED_IDENTICAL_CALL,
    diagnostics.FailureCategory.REPEATED_CYCLE,
    diagnostics.FailureCategory.CONSECUTIVE_TOOL_ERRORS,
    diagnostics.FailureCategory.PROVIDER_TIMEOUT,
    diagnostics.FailureCategory.TOOL_TIMEOUT,
})


class Orchestrator:
    """Background execution engine for one autonomous ``task_state``.

    Parameters
    ----------
    agent:
        A **dedicated** :class:`osia.agent.OSIAAgent` (already model-set by the
        caller). Used only through its public surface — ``agent.reset()`` and
        ``agent.run(str) -> str`` — for both task execution and verification.
        Because the loop ``reset()``s before every run, this agent should NOT be
        shared with the interactive chat.
    progress_cb:
        Optional ``Callable[[dict], None]`` invoked with small event dicts
        (``{"event": "task_done"|"task_failed"|"failed"|"done"|"salvaged", ...}``)
        as the run progresses. Defaults to a no-op. NO Qt here — Phase 4 wires a
        thread-safe GUI bridge on top. Any exception it raises is swallowed.
    """

    def __init__(self, agent, progress_cb=None, *, origin_ref="desktop",
                 document_progress_cb=None):
        self.agent = agent
        self._progress_cb = progress_cb
        self._document_progress_cb = document_progress_cb
        self._state = None
        self._stop = threading.Event()
        self._thread = None
        # One-shot: set by resume() so the loop re-checks an interrupted task's
        # success_criterion BEFORE re-executing it (crash-salvage). Consumed on
        # the first task examined, regardless of outcome.
        self._resume_recheck = False
        self._resume_scope_extension = False
        self._resume_scope_tool = ""
        self._active_task = None
        self._origin_ref = str(origin_ref or "desktop")

    def _agent_run(self, message: str, *, verification: bool = False):
        """Run one autonomous provider turn under the checkpoint language."""

        st = self._state if isinstance(self._state, dict) else {}
        language = normalize_locale(st.get("language")) or i18n.DEFAULT_LANG
        from osia.action_policy import (
            ExecutionType,
            execution_context_scope,
            get_default_permission_policy,
            make_execution_context,
        )

        context = fixed_turn_context(
            language,
            source="autonomous_state",
            reference=str(st.get("run_id") or st.get("task_id") or "autonomous"),
            mode=str(st.get("language_mode") or "single-language"),
            allowed_languages=st.get("allowed_languages") or (),
            source_language=st.get("source_language"),
            target_language=st.get("target_language"),
        )
        policy = get_default_permission_policy()
        permission_state = policy.store.load_state()
        execution_context = make_execution_context(
            execution_type=ExecutionType.AUTONOMOUS,
            interactive=False,
            run_id=str(st.get("run_id") or ""),
            plan_revision=str(st.get("plan_revision") or 0),
            task_revision=str(st.get("authorization_revision") or 0),
            policy_epoch=permission_state.policy_epoch,
            store=policy.store,
        )
        origin_ref = str(st.get("authorization_origin_ref") or self._origin_ref)
        from osia.tool_contracts import effective_tool_surface_scope

        web_surface = self._uses_autonomous_web_surface(st)
        surface = (
            "autonomous_web_verify" if web_surface and verification
            else "autonomous_web" if web_surface
            else "autonomous"
        )
        with authorization.run_authorization_scope(
            st,
            save=state.save_task_state,
            emit=self._emit,
            origin_ref=origin_ref,
        ), execution_context_scope(execution_context), effective_tool_surface_scope(
            surface
        ), document_progress_scope(
            "autonomous",
            str(st.get("run_id") or st.get("task_id") or "autonomous"),
            revision=int(st.get("plan_revision", 0) or 0),
            sink=self._document_progress_cb,
        ):
            if hasattr(self.agent, "set_turn_context"):
                self.agent.set_turn_context(context)
                return self.agent.run(message, turn_context=context)
            return self.agent.run(message)

    @staticmethod
    def _uses_autonomous_web_surface(st: dict) -> bool:
        """Classify a Web run from its trusted plan metadata and objective."""

        for item in st.get("capability_summary") or []:
            if not isinstance(item, dict):
                continue
            if (
                item.get("tool_category") == "browser"
                or item.get("operation_class") == "browser_target"
                or item.get("target_category") == "browser_target"
            ):
                return True
        text = " ".join((
            str(st.get("_runtime_objective", st.get("objective", "")) or ""),
            *(str(task.get("_runtime_objective", task.get("objective", "")) or "")
              for phase in st.get("phases") or [] if isinstance(phase, dict)
              for task in phase.get("tasks") or [] if isinstance(task, dict)),
        )).lower()
        return bool(
            re.search(r"https?://", text)
            or any(marker in text for marker in (
                " browser", " web ", " website", " site ", " webpage",
                "page web", "navigateur", "popup", "iframe",
            ))
        )

    def _agent_run_failure(self):
        """Read only the agent's allow-listed typed run diagnostic."""
        document_error = str(
            getattr(self.agent, "last_run_document_error", None) or ""
        )
        if document_error in {
            "document_extraction_failure", "file_identity_mismatch",
            "unsupported_format", "unsupported_legacy_format",
            "encrypted_document", "drm_protected", "protected_document",
            "corrupted_document", "active_content_detected",
            "external_reference_detected", "resource_limit_exceeded",
        }:
            return (
                diagnostics.FailureCategory.TASK_EXECUTION_FAILURE,
                document_error,
            )
        raw = getattr(self.agent, "last_run_diagnostic", None)
        if not isinstance(raw, dict):
            return None
        try:
            category = diagnostics.FailureCategory(raw.get("error_category"))
        except (TypeError, ValueError):
            return None
        if category not in _AGENT_RUN_FAILURE_CATEGORIES:
            return None
        reason_code = str(raw.get("reason_code") or category.value)
        if reason_code != category.value:
            reason_code = category.value
        return category, reason_code

    # ── Lifecycle (mirrors CronManager.start/stop) ───────────────────────────

    def start(self, state):
        """Begin executing a *fresh* plan on a background thread (idempotent)."""
        self._begin(state, resume=False)

    def resume(self, state):
        """Resume a persisted, partially-run plan — re-checks the interrupted
        task's success_criterion before re-executing it (idempotent)."""
        self._begin(state, resume=True)

    def _validate_browser_resume(self, st) -> str | None:
        checkpoint = state.normalize_browser_checkpoint(
            st.get("browser_checkpoint")
        )
        stored = checkpoint.get("browser_state")
        if stored is None:
            return None
        pending = checkpoint.get("pending_operation") or {}
        if pending.get("kind") == "download":
            return "download_attention_required"
        if pending.get("kind") == "upload":
            return "browser_state_stale"
        try:
            from osia.tools.browser import get_browser_state

            observed = get_browser_state()
            data = observed.get("data") if isinstance(observed, dict) else None
            live = data.get("state") if isinstance(data, dict) else None
        except Exception:
            return "unsupported_browser_behavior"
        if not isinstance(observed, dict) or observed.get("ok") is not True:
            return "unsupported_browser_behavior"
        if live != stored:
            checkpoint["browser_state"] = live
            st["browser_checkpoint"] = checkpoint
            return "browser_state_stale"
        return None

    def _begin(self, st, resume):
        # Idempotent: never spawn a second loop over the same Orchestrator.
        if self._thread and self._thread.is_alive():
            return
        if not isinstance(st, dict) or not state.execution_state_is_valid(st):
            invalid = st if isinstance(st, dict) else state.new_task_state("", "", "", [])
            self._state = invalid
            invalid["status"] = "failed"
            invalid["run_state"] = "failed"
            diagnostic = self._record_failure(
                invalid,
                diagnostics.AutonomousStage.RESUME if resume else diagnostics.AutonomousStage.ORCHESTRATOR,
                diagnostics.FailureCategory.INVALID_RESUME_STATE,
                reason_code="invalid_authorization_state",
                resumable=False,
                checkpoint_present=isinstance(st, dict),
            )
            self._safe_save(invalid)
            self._emit({"event": "failed", **diagnostic})
            return
        if resume and st.get("run_state") == "running":
            # A persisted running checkpoint means the process stopped between
            # checkpoints.  Resume re-enters through the same ready gate; every
            # dispatch still revalidates the protected epoch/grant.
            st["run_state"] = "ready"
        if st.get("run_state") != "ready":
            self._state = st
            self._safe_save(st)
            self._emit({
                "event": (
                    "authorization_pending"
                    if st.get("run_state") == "authorization_pending"
                    else st.get("run_state")
                ),
                **authorization.pc_projection(st),
            })
            return
        if resume and not state.resume_state_is_valid(st):
            invalid = st if isinstance(st, dict) else state.new_task_state("", "", "", [])
            self._state = invalid
            invalid["status"] = "failed"
            invalid["run_state"] = "failed"
            diagnostic = self._record_failure(
                invalid,
                diagnostics.AutonomousStage.RESUME,
                diagnostics.FailureCategory.INVALID_RESUME_STATE,
                reason_code="invalid_resume_state",
                resumable=False,
                checkpoint_present=isinstance(st, dict),
            )
            self._safe_save(invalid)
            self._emit({"event": "failed", **diagnostic})
            return
        self._state = st
        st["language"] = (
            normalize_locale(st.get("language")) or i18n.DEFAULT_LANG
        )
        artifact_failure = self._artifact_failure(st)
        if resume and artifact_failure is not None:
            st["status"] = "failed"
            diagnostic = self._record_failure(
                st,
                diagnostics.AutonomousStage.RESUME,
                diagnostics.FailureCategory.VERIFICATION_ERROR,
                reason_code=artifact_failure,
                resumable=False,
                checkpoint_present=True,
            )
            self._safe_save(st)
            self._emit({"event": "failed", **diagnostic})
            return
        browser_resume_reason = self._validate_browser_resume(st) if resume else None
        if browser_resume_reason is not None:
            state.suspend_browser(st, browser_resume_reason)
            self._safe_save(st)
            self._emit({
                "event": browser_resume_reason,
                **authorization.pc_projection(st),
            })
            return
        current_task = None
        cursor = st.get("cursor") or {}
        try:
            current_task = (st.get("phases") or [])[int(
                cursor.get("phase", 0) or 0
            )]["tasks"][int(cursor.get("task", 0) or 0)]
        except (IndexError, KeyError, TypeError, ValueError):
            current_task = None
        self._resume_scope_extension = bool(
            resume
            and isinstance(current_task, dict)
            and current_task.get("last_error") == "scope_extension_required"
            and len(getattr(self.agent, "messages", []) or []) > 1
        )
        self._resume_scope_tool = ""
        if self._resume_scope_extension:
            for record in reversed(st.get("operation_records") or []):
                if (
                    isinstance(record, dict)
                    and record.get("status") == "suspended_scope_extension"
                ):
                    candidate = str(record.get("tool_identity") or "")
                    if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", candidate):
                        self._resume_scope_tool = candidate
                    break
        self._resume_recheck = bool(resume and not self._resume_scope_extension)
        # We are (re-)entering execution ⇒ the run is in progress. Persist once
        # up front so the on-disk checkpoint matches what we are about to run.
        st["status"] = "in_progress"
        st["run_state"] = "running"
        diagnostics.clear_current(st)
        self._safe_save(st)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="osia-orchestrator", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        """Signal the loop to stop and wait briefly for the thread to exit."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    # ── Main loop ────────────────────────────────────────────────────────────

    def _run_loop(self):
        """Drive the plan one task-cycle at a time until terminal or stopped.

        Each :meth:`_step` returns ``True`` to keep going or ``False`` to stop
        (terminal status reached, or :meth:`stop` requested). An unexpected
        exception is contained: the run is marked ``failed``, checkpointed and
        emitted — it never escapes the daemon thread (CronManager pattern).
        """
        st = self._state
        if st is None:
            return
        while not self._stop.is_set():
            try:
                keep_going = self._step(st)
            except Exception:  # never let an unexpected error kill silently
                try:
                    st["status"] = "failed"
                    diagnostic = self._record_failure(
                        st,
                        diagnostics.AutonomousStage.ORCHESTRATOR,
                        diagnostics.FailureCategory.ORCHESTRATOR_FAILURE,
                        reason_code="orchestrator_failure",
                        resumable=False,
                        checkpoint_present=True,
                    )
                    self._append_log(st, "crashed", note="orchestrator_failure")
                    self._safe_save(st)
                except Exception:
                    diagnostic = {}
                self._emit({
                    "event": "failed",
                    "reason": "orchestrator_failure",
                    **diagnostic,
                })
                break
            if not keep_going:
                break

    def _approved_scope_resume_call(self, approved_tool: str) -> dict | None:
        """Return the private exact call retained at pre-dispatch suspension."""

        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", approved_tool or ""):
            return None
        for message in reversed(getattr(self.agent, "messages", []) or []):
            if not isinstance(message, dict):
                continue
            call = message.get("_autonomous_resume_call")
            if (
                isinstance(call, dict)
                and call.get("tool") == approved_tool
            ):
                return dict(call)
        return None

    def _step(self, st) -> bool:
        """Run ONE task-cycle. Returns whether the loop should continue."""
        cursor = st.get("cursor") or {"phase": 0, "task": 0}
        p = int(cursor.get("phase", 0) or 0)
        t = int(cursor.get("task", 0) or 0)
        phases = st.get("phases") or []

        # (A) Termination — the cursor has walked past the last phase.
        if p >= len(phases):
            remaining_requirements = state.checklist_remaining(st)
            if remaining_requirements:
                budgets = st.get("budgets") or {}
                replan_count = int(budgets.get("replan_count", 0) or 0)
                max_replans = int(
                    budgets.get("max_replans", state.DEFAULT_MAX_REPLANS)
                )
                if replan_count < max_replans and self._replan(st):
                    return st.get("run_state") == "ready"
                st["status"] = "failed"
                st["run_state"] = "failed"
                diagnostic = self._record_failure(
                    st,
                    diagnostics.AutonomousStage.VERIFICATION,
                    diagnostics.FailureCategory.VERIFICATION_NOT_COMPLETED,
                    reason_code="required_criteria_incomplete",
                    resumable=False,
                    checkpoint_present=True,
                )
                self._append_log(
                    st, "failed", note="required_criteria_incomplete",
                )
                self._safe_save(st)
                self._emit({
                    "event": "failed",
                    "reason": "required_criteria_incomplete",
                    **diagnostic,
                })
                return False
            if (
                state.normalize_document_sources(st.get("document_sources"))
                and not self._document_sources_ready(st)
            ):
                st["status"] = "failed"
                st["run_state"] = "failed"
                diagnostic = self._record_failure(
                    st,
                    diagnostics.AutonomousStage.VERIFICATION,
                    diagnostics.FailureCategory.VERIFICATION_NOT_COMPLETED,
                    reason_code="verification_not_completed",
                    resumable=False,
                    checkpoint_present=True,
                )
                self._append_log(
                    st, "failed", note="document_sources_incomplete",
                )
                self._safe_save(st)
                self._emit({
                    "event": "failed",
                    "reason": "verification_not_completed",
                    **diagnostic,
                })
                return False
            final_answer = ""
            if document_artifacts.normalize_references(st.get("artifacts")):
                final_answer = self._consolidate_document_final(st)
                if not final_answer:
                    st["status"] = "failed"
                    st["run_state"] = "failed"
                    diagnostic = self._record_failure(
                        st,
                        diagnostics.AutonomousStage.TASK_EXECUTION,
                        diagnostics.FailureCategory.TASK_EXECUTION_FAILURE,
                        reason_code="task_execution_failure",
                        resumable=False,
                        checkpoint_present=True,
                    )
                    self._append_log(
                        st, "failed", note="document_finalization_failure",
                    )
                    self._safe_save(st)
                    self._emit({
                        "event": "failed",
                        "reason": "document_finalization_failure",
                        **diagnostic,
                    })
                    return False
            st["status"] = "done"
            st["run_state"] = "completed"
            diagnostics.clear_current(st)
            self._append_log(st, "done", note="all phases complete")
            self._safe_save(st)
            payload = {"event": "done", "status": "done"}
            if final_answer:
                payload["final_answer"] = final_answer
            self._emit(payload)
            return False

        phase = phases[p]
        tasks = phase.get("tasks") or []

        # Defensive: cursor past this phase's tasks (e.g. an empty phase) — close
        # the phase and roll to the next one. Never re-runs anything.
        if t >= len(tasks):
            if phase.get("status") not in ("done", "failed"):
                phase["status"] = "done"
            self._advance(st)
            self._safe_save(st)
            return True

        task = tasks[t]
        self._active_task = task

        # Defensive: stale cursor parked on an already-done task ⇒ just advance
        # (normal flow never lands here; this absorbs a corrupt/odd resume state).
        if task.get("status") == "done":
            self._resume_recheck = False
            if t + 1 >= len(tasks) and phase.get("status") != "failed":
                phase["status"] = "done"
            self._advance(st)
            self._safe_save(st)
            return True

        digest = st.get("context_digest", "") or ""
        budgets = st.get("budgets") or {}
        max_attempts = int(budgets.get("max_attempts_per_task",
                                       state.DEFAULT_MAX_ATTEMPTS_PER_TASK))
        max_replans = int(budgets.get("max_replans", state.DEFAULT_MAX_REPLANS))
        replan_count = int(budgets.get("replan_count", 0) or 0)

        # (B) One-shot resume re-check — salvage an interrupted, already-satisfied
        # task instead of re-running it. Consumed on the first task regardless.
        if self._resume_recheck:
            self._resume_recheck = False
            if task.get("status") == "in_progress" and int(task.get("attempts", 0) or 0) > 0:
                verification = self._verify_outcome(
                    task, task.get("distilled_summary", "") or "",
                )
                if verification.ok:
                    self._salvage(st, p, t, task, verification.reason_code)
                    return True
                self._record_failure(
                    st,
                    diagnostics.AutonomousStage.VERIFICATION,
                    verification.category,
                    reason_code=verification.reason_code,
                    attempt=task.get("attempts", 0),
                    resumable=True,
                    checkpoint_present=True,
                )
            # Not salvageable ⇒ fall through and (re-)execute it normally.

        # (C) Begin an attempt — phase in_progress, bump attempts, CHECKPOINT A.
        # A replan or scope-extension resume can arrive after every explicitly
        # named operation for this stable requirement already produced a
        # dispatcher-verified receipt. Re-verify read-only before asking the
        # model to execute again; this is the durable no-replay path.
        required_operations = self._required_browser_operations(task)
        if required_operations and required_operations.issubset(set(
            self._verified_operations_for_task(task)
        )):
            verification = self._verify_outcome(
                task, task.get("distilled_summary", "") or "",
            )
            if verification.ok:
                self._resume_scope_extension = False
                self._salvage(
                    st, p, t, task, "verified_operation_receipt_reuse",
                )
                return True

        phase["status"] = "in_progress"
        attempts = int(task.get("attempts", 0) or 0) + 1
        state.set_task_status(st, p, t, "in_progress", attempts=attempts)
        self._append_log(st, "attempt", ref=task.get("id"),
                         note=f"attempt {attempts}/{max_attempts}")
        self._safe_save(st)  # CHECKPOINT A

        # (D) Run the isolated subtask. run() can raise when fallbacks are
        # exhausted ⇒ treat as an empty result + recorded error (never crash).
        agent_failure = None
        previous_exact_resume_call = getattr(
            self.agent, "_autonomous_resume_exact_call", None,
        )
        try:
            continue_approved_scope = self._resume_scope_extension
            self._resume_scope_extension = False
            if continue_approved_scope:
                approved_tool = self._resume_scope_tool
                self._resume_scope_tool = ""
                exact_resume_call = self._approved_scope_resume_call(
                    approved_tool,
                )
                if exact_resume_call is not None:
                    self.agent._autonomous_resume_exact_call = exact_resume_call
                tool_instruction = (
                    f"The approved operation is {approved_tool}. "
                    if approved_tool else
                    "Identify the one operation that was rejected before "
                    "dispatch. "
                )
                execution_prompt = (
                    "The exact scope extension requested in this same step is "
                    "now approved. Continue the interrupted step from this "
                    "conversation. " + tool_instruction +
                    "The immediately preceding assistant message may contain "
                    "other batched calls; ignore all of them. Reissue only the "
                    "approved operation with exactly the same arguments as "
                    "your sole next tool call. The earlier rejection occurred before "
                    "dispatch, so do not inspect, refresh, navigate, or obtain "
                    "replacement opaque references first; do not broaden or "
                    "replace the approved call."
                )
            else:
                self.agent.reset()
                execution_prompt = self._compose(task, digest)
            with self._artifact_scope(st, task):
                result = self._agent_run(execution_prompt)
            if not isinstance(result, str):
                result = "" if result is None else str(result)
            agent_failure = self._agent_run_failure()
        except Exception as exc:
            result = ""
            agent_failure = self._agent_run_failure()
            if agent_failure is not None:
                category, reason_code = agent_failure
            else:
                is_timeout = (
                    isinstance(exc, TimeoutError)
                    or "timeout" in type(exc).__name__.lower()
                )
                category = (
                    diagnostics.FailureCategory.TASK_TIMEOUT
                    if is_timeout
                    else diagnostics.FailureCategory.TASK_EXECUTION_FAILURE
                )
                reason_code = (
                    "task_timeout" if is_timeout else "task_execution_failure"
                )
            state.set_task_status(st, p, t, "in_progress", attempts=attempts,
                                  last_error=reason_code)
            self._record_failure(
                st,
                diagnostics.AutonomousStage.TASK_EXECUTION,
                category,
                reason_code=reason_code,
                attempt=attempts,
                resumable=True,
                checkpoint_present=True,
            )
        finally:
            self.agent._autonomous_resume_exact_call = (
                previous_exact_resume_call
            )

        barrier = st.get("run_state")
        if barrier in {
            "suspended_scope_extension",
            "suspended_authorization_revoked",
            "blocked_outcome_unknown",
            "suspended_browser_intervention",
            "suspended_browser_state",
        }:
            if barrier != "blocked_outcome_unknown":
                # Authorization failed before dispatch and is not a task retry.
                attempts = max(0, attempts - 1)
                state.set_task_status(
                    st, p, t, "in_progress", attempts=attempts,
                    last_error=(
                        "scope_extension_required"
                        if barrier == "suspended_scope_extension"
                        else (
                            state.normalize_browser_checkpoint(
                                st.get("browser_checkpoint")
                            ).get("manual_intervention")
                            if barrier.startswith("suspended_browser")
                            else "authorization_revoked"
                        )
                    ),
                )
            else:
                state.set_task_status(
                    st, p, t, "in_progress", attempts=attempts,
                    last_error="outcome_unknown",
                )
            self._safe_save(st)
            self._emit({"event": barrier, **authorization.pc_projection(st)})
            return False

        # ``ERROR:`` is the agent's protected deterministic failure contract.
        # Recording it is observability-only; verification still runs exactly as
        # before, so this does not alter execution, retry or replan behavior.
        if (agent_failure is None and isinstance(result, str)
                and result.lstrip().startswith("ERROR:")):
            self._record_failure(
                st,
                diagnostics.AutonomousStage.TASK_EXECUTION,
                diagnostics.FailureCategory.TASK_EXECUTION_FAILURE,
                reason_code="task_error_result",
                attempt=attempts,
                resumable=True,
                checkpoint_present=True,
            )

        # Honour a stop requested DURING the run — persist and exit promptly.
        if self._stop.is_set():
            self._safe_save(st)
            return False

        # (E) Verify doneness (fail-closed).
        verification = self._verify_outcome(task, result)

        if verification.ok:
            # (F1) Success — distil, mark done, grow+cap the digest, CHECKPOINT B.
            summary = self._distil(task, result)
            state.set_task_status(st, p, t, "done",
                                  distilled_summary=summary, last_error=None)
            diagnostics.clear_current(st)
            st["context_digest"] = self._cap_digest(
                digest, f"[{task.get('id')}] {summary}")
            if t + 1 >= len(tasks):
                phase["status"] = "done"
            self._advance(st)
            self._append_log(
                st, "task_done", ref=task.get("id"),
                note=verification.reason_code,
            )
            self._safe_save(st)  # CHECKPOINT B
            self._emit({"event": "task_done", "task_id": task.get("id"),
                        "phase": p, "task": t,
                        "reason": verification.reason_code})
            return True

        # (F2) Failure.
        if agent_failure is not None:
            failure_category, last_error = agent_failure
            failure_stage = diagnostics.AutonomousStage.TASK_EXECUTION
        else:
            failure_category = verification.category
            last_error = verification.reason_code
            failure_stage = diagnostics.AutonomousStage.VERIFICATION
        self._record_failure(
            st,
            failure_stage,
            failure_category,
            reason_code=last_error,
            attempt=attempts,
            resumable=True,
            checkpoint_present=True,
        )
        if attempts >= max_attempts:
            # Task is out of attempts — fail it and its phase. CHECKPOINT C.
            state.set_task_status(st, p, t, "failed", last_error=last_error)
            phase["status"] = "failed"
            diagnostic = self._record_failure(
                st,
                diagnostics.AutonomousStage.RETRY,
                diagnostics.FailureCategory.TASK_ATTEMPTS_EXHAUSTED,
                reason_code="task_attempts_exhausted",
                attempt=attempts,
                resumable=True,
                checkpoint_present=True,
            )
            if agent_failure is not None:
                # Keep the typed leaf cause current for PC/mobile while retaining
                # task_attempts_exhausted in bounded history.
                diagnostic = self._record_failure(
                    st,
                    diagnostics.AutonomousStage.TASK_EXECUTION,
                    failure_category,
                    reason_code=last_error,
                    attempt=attempts,
                    resumable=True,
                    checkpoint_present=True,
                )
            self._append_log(
                st, "task_failed", ref=task.get("id"),
                note="task_attempts_exhausted",
            )
            self._safe_save(st)  # CHECKPOINT C
            self._emit({"event": "task_failed", "task_id": task.get("id"),
                        "reason": "task_attempts_exhausted", **diagnostic})

            # Try to recover by re-planning the remaining work, if budget allows.
            if replan_count < max_replans:
                if self._replan(st):
                    # Replanning always creates a new plan revision and review
                    # request unless protected Full Access is already active.
                    # In that isolated mode the new tail may continue directly.
                    return st.get("run_state") == "ready"
            else:
                self._record_failure(
                    st,
                    diagnostics.AutonomousStage.REPLAN,
                    diagnostics.FailureCategory.REPLANS_EXHAUSTED,
                    reason_code="replans_exhausted",
                    attempt=attempts,
                    resumable=False,
                    checkpoint_present=True,
                )
            if agent_failure is not None:
                # Replan failure/exhaustion remains in history; the terminal
                # projection keeps the concrete leaf cause that stopped the turn.
                self._record_failure(
                    st,
                    diagnostics.AutonomousStage.TASK_EXECUTION,
                    failure_category,
                    reason_code=last_error,
                    attempt=attempts,
                    resumable=False,
                    checkpoint_present=True,
                )
            # Out of attempts AND out of replans (or replanning failed) ⇒ stop.
            st["status"] = "failed"
            st["run_state"] = "failed"
            self._append_log(st, "failed", ref=task.get("id"),
                             note=(st.get("failure_diagnostic") or {}).get(
                                 "reason_code", "unknown_failure",
                             ))
            self._safe_save(st)
            terminal_reason = (st.get("failure_diagnostic") or {}).get(
                "reason_code", "unknown_failure",
            )
            self._emit({"event": "failed", "task_id": task.get("id"),
                        "reason": terminal_reason,
                        **diagnostics.projection(st)})
            return False

        # Still has attempts left — retry. Cursor UNCHANGED; record the error.
        state.set_task_status(st, p, t, "in_progress", last_error=last_error)
        self._append_log(st, "retry", ref=task.get("id"), note=last_error)
        self._safe_save(st)
        return True

    # ── Subtask prompt + verification ────────────────────────────────────────

    @contextmanager
    def _artifact_scope(self, st, task=None):
        """Expose only this run's retained document capabilities to the agent."""

        if not isinstance(st, dict):
            yield
            return
        references = document_artifacts.normalize_references(
            st.get("artifacts")
        )
        artifact_ids = [ref["artifact_id"] for ref in references]

        def _capture(reference):
            self._capture_artifact(st, task, reference)

        with document_artifacts.artifact_access_scope(
            str(st.get("task_id") or ""),
            artifact_ids,
            _capture,
        ):
            yield

    def _capture_artifact(self, st, task, reference) -> None:
        """Checkpoint a safe reference immediately after create/read succeeds."""

        clean = document_artifacts.sanitize_reference(reference)
        if clean is None or not isinstance(st, dict):
            return
        existing = document_artifacts.normalize_references(st.get("artifacts"))
        artifact_id = clean["artifact_id"]
        changed = all(ref["artifact_id"] != artifact_id for ref in existing)
        st["artifacts"] = document_artifacts.normalize_references(
            existing + [clean]
        )
        if isinstance(task, dict):
            ids = [
                str(value) for value in (task.get("artifact_ids") or [])
                if str(value).startswith("doc_")
            ]
            if artifact_id not in ids:
                ids.append(artifact_id)
                changed = True
            task["artifact_ids"] = ids[:document_artifacts.MAX_ARTIFACT_REFS]
        if changed:
            self._safe_save(st)

    def _document_sources_ready(self, st: dict) -> bool:
        """Require one valid retained artifact for every explicit run source."""

        inventory = state.normalize_document_sources(st.get("document_sources"))
        try:
            linked = document_sources.get_source_store().linked_artifacts(
                [item["source_ref"] for item in inventory],
                run_owner_id=str(st.get("task_id") or ""),
                run_id=str(st.get("run_id") or ""),
                policy_epoch=int(st.get("policy_epoch", 0) or 0),
            )
            references = document_artifacts.normalize_references(
                st.get("artifacts")
            )
            known = {item["artifact_id"] for item in references}
            if len(linked) != len(inventory) or any(
                artifact_id not in known for artifact_id in linked
            ):
                return False
            with self._artifact_scope(st):
                for artifact_id in linked:
                    document_artifacts.get_store().inspect(artifact_id)
            return True
        except (
            document_sources.DocumentSourceError,
            document_artifacts.ArtifactStoreError,
        ):
            return False

    @staticmethod
    def _artifact_context(st, task=None) -> str:
        """Render bounded non-sensitive metadata for execution and verifier."""

        if not isinstance(st, dict):
            return ""
        references = document_artifacts.normalize_references(st.get("artifacts"))
        lines = []
        for ref in references:
            lines.append(
                f"{ref['artifact_id']} type={ref.get('format_label', ref.get('document_type'))} "
                f"family={ref.get('document_family', 'document')} "
                f"pages={ref.get('page_count', 0)} sections={ref.get('section_count', 0)} "
                f"tables={ref.get('table_count', 0)} sheets={ref.get('sheet_count', 0)} "
                f"slides={ref.get('slide_count', 0)} chunks={ref.get('chunk_count', 0)} "
                f"queries={','.join(ref.get('supported_queries') or [])}"
            )
        return "\n".join(lines)[:ARTIFACT_CONTEXT_MAX_CHARS]

    @staticmethod
    def _artifact_failure(st, task=None) -> str | None:
        """Validate retained payload availability using stable reason codes."""

        if not isinstance(st, dict):
            return None
        raw_references = st.get("artifacts")
        references = document_artifacts.normalize_references(raw_references)
        if isinstance(raw_references, list) and raw_references and not references:
            return "document_artifact_invalid"
        if not references:
            return None
        artifact_ids = [ref["artifact_id"] for ref in references]
        mapping = {
            "missing_artifact": "document_artifact_missing",
            "expired_artifact": "document_artifact_expired",
            "corrupted_artifact": "document_artifact_corrupted",
            "unreadable_artifact": "document_artifact_unreadable",
            "unauthorized_artifact": "document_artifact_unauthorized",
            "invalid_artifact_id": "document_artifact_invalid",
        }
        try:
            with document_artifacts.artifact_access_scope(
                str(st.get("task_id") or ""), artifact_ids,
            ):
                for artifact_id in artifact_ids:
                    document_artifacts.get_store().inspect(artifact_id)
        except document_artifacts.ArtifactStoreError as exc:
            return mapping.get(exc.category, "document_artifact_corrupted")
        return None

    def _consolidate_document_final(self, st: dict) -> str:
        """Produce one transient, bounded document answer at terminal time.

        The answer is deliberately never written into the Autonomous
        checkpoint.  It is emitted only on the canonical terminal event; the
        retained document payload remains in the private artifact store.
        """

        references = document_artifacts.normalize_references(st.get("artifacts"))
        if not references:
            return ""
        objective = state.sanitize_autonomous_text(
            st.get("objective", ""),
        )[:2_000]
        criteria = [
            state.sanitize_autonomous_text(item.get("criterion", ""))[:500]
            for item in st.get("checklist") or []
            if isinstance(item, dict) and item.get("criterion")
        ][:25]
        prompt = (
            "Produce the single consolidated final answer for this completed "
            "document task. Use query_document with targeted operations on the "
            "opaque retained artifacts before answering; do not read sequentially "
            "through the whole document and do not re-run extraction. Answer the "
            "user's objective directly, report unsupported or uncertain facts "
            "truthfully, and never reveal a local path, private filename, storage "
            "key, or content hash. Return only the final answer text.\n\n"
            f"OBJECTIVE:\n{objective}\n\n"
            "VERIFIED CRITERIA:\n"
            + "\n".join(f"- {criterion}" for criterion in criteria)
            + "\n\nOPAQUE ARTIFACTS:\n"
            + self._artifact_context(st)
        )
        try:
            self.agent.reset()
            with self._artifact_scope(st):
                result = self._agent_run(prompt)
        except Exception:
            return ""
        if not isinstance(result, str) or not result.strip():
            return ""
        if result.lstrip().startswith("ERROR:"):
            return ""
        activity = getattr(self.agent, "last_run_document_activity", None)
        requires_targeted_activity = any(
            int(reference.get("schema_version", 1) or 1) >= 2
            for reference in references
        )
        if (
            requires_targeted_activity and activity is not True
        ) or (
            not requires_targeted_activity and activity is False
        ):
            return ""
        return state.sanitize_autonomous_text(result).strip()[:DOCUMENT_FINAL_MAX_CHARS]

    def _compose(self, task, digest) -> str:
        """Build the isolated subtask prompt: objective + success_criterion +
        the (defensively-truncated) rolling context digest, each clearly
        labelled. The agent is told to do ONLY this step."""
        objective = (
            task.get("_runtime_objective", task.get("objective")) or ""
        ).strip()
        criterion = (
            task.get(
                "_runtime_success_criterion", task.get("success_criterion"),
            ) or ""
        ).strip()
        digest = digest or ""
        if len(digest) > DIGEST_MAX_CHARS:  # defensive — digest is already capped
            digest = digest[-DIGEST_MAX_CHARS:]
        parts = [
            "When this step uses the Web, use only the authoritative WOR-186 "
            "browser tools that return opaque state and element references: "
            "navigate_browser, search_web, open_search_result, "
            "inspect_browser_page, activate_browser_element, the browser tab/"
            "history/popup tools, paginate_browser, scroll_infinite, "
            "upload_browser_file, and download_browser_file. Semantic forms "
            "may use inspect_form_fields and fill_form_field. Legacy selector-"
            "based browser tools such as navigate, read_page, click, fill, "
            "find_and_click, type_text and press_key are not eligible in "
            "Autonomous. Never invent a reference; carry forward only a "
            "reference returned by the current document.",
            "When a step requires a structured search, call search_web and use "
            "its returned result_ref with open_search_result. Inspecting or "
            "generically activating a pre-existing link does not execute the "
            "structured-search step.",
            "You are executing ONE step of a larger autonomous task. Do ONLY "
            "this step — do not attempt the rest of the overall objective.",
            "",
            f"STEP OBJECTIVE:\n{objective}",
            "",
            "SUCCESS CRITERION (you are done only when this is OBSERVABLY true):\n"
            f"{criterion}",
        ]
        checklist = [
            item for item in (self._state or {}).get("checklist") or []
            if isinstance(item, dict)
        ]
        if checklist:
            lines = []
            for item in checklist[:64]:
                status = str(item.get("status") or "pending")
                criterion_text = " ".join(
                    str(item.get("criterion") or "").split()
                )[:180]
                lines.append(
                    f"{item.get('requirement_id')} {status}: {criterion_text}"
                )
            parts += [
                "",
                "RUN CHECKLIST (a terminal answer is forbidden while any item "
                "is not verified):\n" + "\n".join(lines),
            ]
        checkpoint = state.normalize_browser_checkpoint(
            (self._state or {}).get("browser_checkpoint")
        )
        browser_state = checkpoint.get("browser_state")
        if browser_state is not None:
            parts += [
                "",
                "CURRENT AUTHORITATIVE BROWSER STATE (re-observe if a tool "
                "reports it stale):\n" + json.dumps(
                    browser_state, sort_keys=True, separators=(",", ":"),
                ),
            ]
        reference_handoffs = checkpoint.get("reference_handoffs") or []
        if reference_handoffs:
            parts += [
                "",
                "SAFE OPAQUE REFERENCES FROM THE CURRENT DOCUMENT (use only "
                "with the matching current state; re-observe if stale):\n"
                + json.dumps(
                    reference_handoffs, sort_keys=True, separators=(",", ":"),
                ),
            ]
        completed_operations = self._verified_operations_for_task(task)
        if completed_operations:
            parts += [
                "",
                "VERIFIED OPERATIONS FOR THIS STEP (do not replay them): "
                + ", ".join(completed_operations[:32]),
            ]
        if self._uses_autonomous_web_surface(self._state or {}):
            from osia.tool_contracts import AUTONOMOUS_WEB_TOOL_NAMES

            parts += [
                "",
                "AVAILABLE STRUCTURED WEB TOOLS: "
                + ", ".join(sorted(AUTONOMOUS_WEB_TOOL_NAMES)),
            ]
        if digest.strip():
            parts += [
                "",
                "CONTEXT SO FAR (results of previous steps — for reference only):\n"
                f"{digest}",
            ]
        source_inventory = state.normalize_document_sources(
            (self._state or {}).get("document_sources")
        )
        if source_inventory:
            parts += [
                "",
                "EXPLICIT DOCUMENT INPUTS (opaque references and safe metadata "
                "only):\n" + "\n".join(
                    f"{item['source_ref']} type={item['type_label']} "
                    f"family={item['document_family']} state={item['state']}"
                    for item in source_inventory
                ),
                "Use read_document on every source_ref required by this step, "
                "then use targeted query_document operations on the returned "
                "artifact. Never substitute or omit a selected source.",
            ]
        artifact_context = self._artifact_context(self._state, task)
        if artifact_context:
            parts += [
                "",
                "LOCAL DOCUMENT ARTIFACTS (opaque metadata only; use "
                "targeted query_document operations; read_document_artifact "
                "is compatibility-only):\n"
                f"{artifact_context}",
            ]
        parts += ["", "Carry out the step now, then stop."]
        return "\n".join(parts)

    def _verify(self, task, evidence):
        """Compatibility wrapper returning the historical ``(ok, reason)`` pair."""
        outcome = self._verify_outcome(task, evidence)
        return outcome.ok, outcome.reason_code

    def _verify_outcome(self, task, evidence) -> _VerificationOutcome:
        """Return ``(ok: bool, reason: str)`` — fail-closed, two layers.

        Layer 1 (deterministic): a purely objective file-existence criterion
        naming an **absolute** path is decided on disk — a missing/empty required
        file is a decisive *reject*, a present (non-empty) file a decisive
        *accept* (only when the criterion is purely about that file). Relative /
        ambiguous criteria are NOT decided here (see :meth:`_file_precheck`).
        Layer 2 (LLM judge): everything else is verified by DIRECT observation —
        the judge is told NOT to trust the prior ``evidence`` but to re-check
        (screenshot / read file / re-open). Ambiguity ⇒ NOT_DONE.
        """
        criterion = (
            task.get(
                "_runtime_success_criterion", task.get("success_criterion"),
            ) or ""
        ).strip()
        artifact_failure = self._artifact_failure(self._state, task)
        if artifact_failure is not None:
            return _VerificationOutcome(
                False, diagnostics.FailureCategory.VERIFICATION_ERROR,
                artifact_failure,
            )
        if not criterion:
            # No observable criterion ⇒ we can't prove doneness ⇒ fail-closed.
            return _VerificationOutcome(
                False, diagnostics.FailureCategory.VERIFICATION_ERROR,
                "missing_success_criterion",
            )

        if self._uses_autonomous_web_surface(self._state or {}):
            required_operations = self._required_browser_operations(task)
            verified_operations = set(
                self._verified_operations_for_active_task()
            )
            if not required_operations.issubset(verified_operations):
                return _VerificationOutcome(
                    False,
                    diagnostics.FailureCategory.VERIFICATION_NOT_COMPLETED,
                    "required_browser_operation_missing",
                )

        verdict = self._file_precheck(criterion)
        if verdict is True:
            return _VerificationOutcome(True, None, "file_criterion_satisfied")
        if verdict is False:
            return _VerificationOutcome(
                False, diagnostics.FailureCategory.VERIFICATION_NOT_COMPLETED,
                "file_criterion_not_met",
            )

        # Undecided ⇒ LLM judge by direct observation (evidence intentionally
        # NOT fed in: the whole point is to re-check, not to echo a self-report).
        return self._llm_judge_outcome(criterion, evidence)

    def _required_browser_operations(self, task) -> set[str]:
        if not self._uses_autonomous_web_surface(self._state or {}):
            return set()
        from osia.tool_contracts import AUTONOMOUS_WEB_TOOL_NAMES

        current_task = task if isinstance(task, dict) else {}
        criterion = str(current_task.get(
            "_runtime_success_criterion",
            current_task.get("success_criterion", ""),
        ) or "")
        return {
            name for name in AUTONOMOUS_WEB_TOOL_NAMES
            if re.search(
                rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])",
                criterion,
            )
        }

    def _file_precheck(self, criterion):
        """Tri-state deterministic check: ``True`` (accept) / ``False`` (reject)
        / ``None`` (undecided ⇒ defer to the judge).

        Only **absolute** path tokens are decided on disk. The orchestrator's
        current working directory is NOT the agent's working dir, so a missing
        *relative* token is ambiguous — and loose tokens (version numbers like
        ``2.3.1``, decimals, domains like ``example.com``) look path-ish but are
        not files. Deciding on those would false-reject a genuinely-completed
        task and then burn its whole attempt/replan budget. So relative/ambiguous
        criteria are deferred to the LLM judge, which re-observes via the agent
        (whose own file tools resolve relative paths correctly). The deterministic
        layer stays DECISIVE only where it is reliable: cwd-independent absolute
        paths."""
        low = criterion.lower()
        if not any(kw in low for kw in _FILE_KEYWORDS):
            return None  # not a file-existence claim
        # Reliable only for absolute paths (cwd-independent); defer the rest.
        abs_paths = [p for p in self._extract_paths(criterion) if os.path.isabs(p)]
        if not abs_paths:
            return None  # no absolute path to check on disk ⇒ defer to the judge
        requires_nonempty = any(kw in low for kw in _NONEMPTY_KEYWORDS)
        for pth in abs_paths:
            try:
                if not os.path.exists(pth):
                    return False  # a required absolute file is missing ⇒ reject
                if requires_nonempty and os.path.isfile(pth) and os.path.getsize(pth) == 0:
                    return False  # required non-empty but empty ⇒ decisive reject
            except OSError:
                return None  # can't stat ⇒ don't decide on disk; defer to judge
        # Every required absolute path is present (and non-empty when demanded).
        # Only decisively ACCEPT a PURE file criterion — if it also asserts a
        # non-file observation we can't confirm from disk, defer to the judge.
        if any(kw in low for kw in _NONFILE_OBSERVABLE_KEYWORDS):
            return None
        return True

    @staticmethod
    def _extract_paths(criterion):
        """Pull plausible path/filename tokens out of a criterion string.

        Picks (a) anything inside quotes/backticks and (b) bare tokens that look
        like a filename (have an extension) or a path (contain a separator).
        Trailing sentence punctuation and surrounding quotes are trimmed without
        mangling leading ``./`` or ``/`` (so a relative path stays relative)."""
        cands = []
        for q in re.findall(r"[\"'`]([^\"'`]+)[\"'`]", criterion):
            cands.append(q)
        for tok in re.findall(r"[^\s\"'`]+", criterion):
            if re.search(r"\.[A-Za-z0-9]{1,8}$", tok) or "/" in tok or "\\" in tok:
                cands.append(tok)

        out, seen = [], set()
        for c in cands:
            c = c.strip().strip("\"'`")        # surrounding quotes/backticks
            c = c.rstrip(").,;:!? ")            # trailing sentence punctuation
            c = c.lstrip("(")                  # leading paren only (keep ./, /)
            if not c or c in seen:
                continue
            # Skip pure version/decimal tokens (2.3.1, 1.2, v2.0): they match the
            # extension regex below but are never file paths.
            if re.fullmatch(r"v?\d+(?:\.\d+)+", c):
                continue
            if re.search(r"\.[A-Za-z0-9]{1,8}$", c) or "/" in c or "\\" in c:
                seen.add(c)
                out.append(c)
        return out

    def _verified_operation_records_for_task(self, task) -> list[dict]:
        st = self._state if isinstance(self._state, dict) else {}
        current_task = task if isinstance(task, dict) else {}
        task_id = str(current_task.get("id") or "")
        durable_task_ids = {task_id, f"task:{task_id}"}
        requirement_id = str(current_task.get("requirement_id") or "")
        records = []
        for record in st.get("operation_records") or []:
            if (
                isinstance(record, dict)
                and record.get("status") == "success_verified"
                and (
                    record.get("task_id") in durable_task_ids
                    or (
                        requirement_id
                        and record.get("requirement_id") == requirement_id
                    )
                )
            ):
                records.append(record)
        return records

    def _verified_operations_for_task(self, task) -> list[str]:
        operations = []
        for record in self._verified_operation_records_for_task(task):
            name = str(record.get("tool_identity") or "")[:80]
            if name and name not in operations:
                operations.append(name)
        return operations

    def _verified_operations_for_active_task(self) -> list[str]:
        return self._verified_operations_for_task(self._active_task)

    def _browser_verification_context(self) -> str:
        """Return bounded receipt-derived evidence for the active task."""

        st = self._state if isinstance(self._state, dict) else {}
        operations = self._verified_operations_for_active_task()
        operation_receipts = []
        for record in self._verified_operation_records_for_task(
            self._active_task
        ):
            evidence = record.get("browser_evidence")
            if not isinstance(evidence, dict):
                continue
            operation_receipts.append({
                "operation": str(record.get("tool_identity") or "")[:80],
                "typed_evidence": evidence,
            })
            if len(operation_receipts) >= 32:
                break
        checkpoint = state.normalize_browser_checkpoint(
            st.get("browser_checkpoint")
        )
        receipt = checkpoint.get("last_action_receipt") or {}
        safe_receipt = None
        if isinstance(receipt, dict):
            safe_receipt = {
                "before_state": receipt.get("before_state"),
                "after_state": receipt.get("after_state"),
                "dispatch_state": receipt.get("dispatch_state"),
                "outcome": receipt.get("outcome"),
                "evidence": [
                    {
                        key: value
                        for key, value in item.items()
                        if value is None or isinstance(value, (bool, int, float))
                    }
                    for item in (receipt.get("evidence") or [])[:16]
                    if isinstance(item, dict)
                ],
            }
        checklist = [
            {
                "requirement_id": str(item.get("requirement_id") or ""),
                "status": str(item.get("status") or "pending"),
                "evidence_ref": str(item.get("evidence_ref") or "")[:80],
            }
            for item in (st.get("checklist") or [])[:64]
            if isinstance(item, dict)
        ]
        context = {
            "current_browser_state": checkpoint.get("browser_state"),
            "verified_operations_for_current_task": operations[:32],
            "verified_operation_receipts_for_current_task": operation_receipts,
            "last_safe_action_receipt": safe_receipt,
            "safe_private_artifact_summary": checkpoint.get(
                "last_artifact_summary"
            ),
            "current_document_reference_handoffs": checkpoint.get(
                "reference_handoffs"
            ) or [],
            "requirement_statuses": checklist,
        }
        return json.dumps(context, sort_keys=True, separators=(",", ":"))

    def _llm_judge_outcome(self, criterion, evidence="") -> _VerificationOutcome:
        """Ask the agent to verify ``criterion`` by direct observation and parse
        a robust DONE / NOT_DONE verdict (ambiguous ⇒ NOT_DONE; run error ⇒
        NOT_DONE — fail-closed)."""
        candidate = state.sanitize_autonomous_text(evidence)[:2_000].strip()
        response_criterion = bool(
            candidate
            and re.search(
                r"\b(?:answer|reply|response|summary|synthesi[sz]|"
                r"r[ée]ponse|r[ée]sum[ée]|synth[èe]se|texte final)\b",
                str(criterion or ""),
                flags=re.IGNORECASE,
            )
        )
        if response_criterion:
            prompt = (
                "Verify whether the CANDIDATE TASK OUTPUT itself satisfies the "
                "following response-content criterion:\n"
                f"{criterion}\n\n"
                "For requirements about the candidate's content, format, or "
                "language, inspect the candidate directly; do not require a "
                "screenshot or external page. Any claimed external state or "
                "side effect still requires independent direct evidence. Reply "
                "with a single word on the first line: DONE or NOT_DONE. Then "
                "one short line explaining why."
            )
        else:
            prompt = (
                "Verify by DIRECT observation (take a screenshot, read the file, or "
                "re-open the page) whether the following is TRUE right now:\n"
                f"{criterion}\n\n"
                "Do NOT trust any earlier claim — check it yourself. Reply with a "
                "single word on the first line: DONE or NOT_DONE. Then one short "
                "line explaining why."
            )
        if self._uses_autonomous_web_surface(self._state or {}):
            prompt += (
                "\n\nSAFE VERIFICATION CONTEXT contains dispatcher-verified "
                "operation names and typed receipts. Treat those as direct "
                "evidence and never replay them. If more evidence is needed, "
                "use read-only structured observation only:\n"
                + self._browser_verification_context()
            )
        if candidate:
            prompt += (
                "\n\nCANDIDATE TASK OUTPUT (untrusted prose; evaluate it "
                "against the independent evidence above):\n" + candidate
            )
        verified_operations = self._verified_operations_for_active_task()
        if verified_operations:
            prompt += (
                "\n\nDISPATCHER-VERIFIED CURRENT-TASK OPERATIONS (names only):\n"
                + json.dumps(verified_operations[:32], ensure_ascii=True)
            )
        artifact_context = self._artifact_context(self._state, self._active_task)
        if artifact_context:
            prompt += (
                "\n\nAvailable local document artifacts (opaque metadata; "
                "use targeted query_document operations; read_document_artifact "
                "remains available for v1 compatibility):\n"
                f"{artifact_context}"
            )
        try:
            self.agent.reset()
            with self._artifact_scope(self._state, self._active_task):
                reply = self._agent_run(prompt, verification=True)
        except Exception:
            return _VerificationOutcome(
                False, diagnostics.FailureCategory.VERIFICATION_ERROR,
                "verification_error",
            )
        ok, reason_code = self._parse_verdict(reply)
        return _VerificationOutcome(
            ok,
            None if ok else diagnostics.FailureCategory.VERIFICATION_NOT_COMPLETED,
            reason_code,
        )

    @staticmethod
    def _parse_verdict(reply):
        """Robustly parse a DONE/NOT_DONE judge reply. Tests the NOT_DONE family
        FIRST (so ``NOT_DONE`` never reads as a bare ``DONE`` substring), accepts
        ``DONE`` only as a standalone word (so ``ABANDONED`` doesn't match), and
        treats anything ambiguous/empty as NOT_DONE."""
        text = (reply or "").strip()
        if not text:
            return False, "empty_verification_reply"
        up = text.upper()
        # Collapse separators so NOT-DONE / NOT DONE / NOT_DONE all normalise.
        norm = re.sub(r"[\s_\-]+", " ", up)
        if "NOT DONE" in norm or "NOTDONE" in up:
            return False, "verification_not_completed"
        if re.search(r"\bDONE\b", norm):
            return True, "verification_completed"
        return False, "ambiguous_verification_reply"

    # ── Summary / digest helpers (cheap string ops — no agent on the hot path) ─

    def _distil(self, task, result) -> str:
        """Distil a task result into a short summary line. Cheap string trim to
        ``SUMMARY_MAX_CHARS`` (kept string-based for now per the Phase-3 spec —
        no agent summarisation even for large output)."""
        artifact_ids = task.get("artifact_ids") if isinstance(task, dict) else []
        if artifact_ids:
            text = "Local document artifact retained: " + self._artifact_context(
                self._state, task,
            )
        else:
            text = " ".join((result or "").split())
        if len(text) <= SUMMARY_MAX_CHARS:
            return text
        return text[:SUMMARY_MAX_CHARS].rstrip() + "…"

    @staticmethod
    def _cap_digest(digest, new_line) -> str:
        """Append ``new_line`` to the rolling digest, keeping the last
        ``DIGEST_MAX_LINES`` lines and a hard ``DIGEST_MAX_CHARS`` ceiling
        (FIFO-drop oldest lines, then truncate a lone oversized line)."""
        lines = [ln for ln in (digest or "").splitlines() if ln.strip()]
        lines.append(" ".join((new_line or "").split()))
        if len(lines) > DIGEST_MAX_LINES:
            lines = lines[-DIGEST_MAX_LINES:]
        capped = "\n".join(lines)
        while len(capped) > DIGEST_MAX_CHARS and len(lines) > 1:
            lines.pop(0)
            capped = "\n".join(lines)
        if len(capped) > DIGEST_MAX_CHARS:  # single huge line — hard truncate
            capped = capped[-DIGEST_MAX_CHARS:]
        return capped

    @staticmethod
    def _short(text, limit=200) -> str:
        """One-line, length-capped form of ``text`` for log/reason fields."""
        return " ".join((text or "").split())[:limit]

    # ── Salvage / replan ─────────────────────────────────────────────────────

    def _salvage(self, st, p, t, task, reason):
        """Mark an interrupted-but-already-satisfied task done WITHOUT re-running
        it (resume crash-salvage), grow the digest, advance and checkpoint."""
        summary = (task.get("distilled_summary") or "").strip() \
            or "completed (verified on resume)"
        state.set_task_status(st, p, t, "done", last_error=None)
        diagnostics.clear_current(st)
        digest = st.get("context_digest", "") or ""
        st["context_digest"] = self._cap_digest(digest, f"[{task.get('id')}] {summary}")
        phases = st.get("phases") or []
        if 0 <= p < len(phases):
            tasks = phases[p].get("tasks") or []
            if t + 1 >= len(tasks) and phases[p].get("status") != "failed":
                phases[p]["status"] = "done"
        self._advance(st)
        self._append_log(st, "salvaged", ref=task.get("id"), note=reason)
        self._safe_save(st)
        self._emit({"event": "salvaged", "task_id": task.get("id"),
                    "reason": "verification_completed"})

    def _replan(self, st) -> bool:
        """Re-plan the REMAINING work after a task ran out of attempts.

        Builds a "remaining objective" (original goal + what's done so far + the
        failure), asks the planner for fresh phases, and on success appends them,
        moves the cursor to the first new phase and bumps ``replan_count`` — a
        single atomic checkpoint. Returns ``True`` if re-planned, ``False`` if the
        planner produced nothing (the caller then fails the run). The replan
        attempt is counted either way so ``max_replans`` is a hard cap."""
        budgets = st.setdefault("budgets", {})
        objective = st.get("_runtime_objective", st.get("objective", "")) or ""
        digest = st.get("context_digest", "") or ""

        cursor = st.get("cursor") or {}
        p = int(cursor.get("phase", 0) or 0)
        t = int(cursor.get("task", 0) or 0)
        failed_obj, last_err = "", ""
        phases = st.get("phases") or []
        if 0 <= p < len(phases):
            tasks = phases[p].get("tasks") or []
            if 0 <= t < len(tasks):
                failed_obj = tasks[t].get(
                    "_runtime_objective", tasks[t].get("objective", ""),
                ) or ""
                last_err = tasks[t].get("last_error", "") or ""

        remaining = (
            f"ORIGINAL OBJECTIVE:\n{objective}\n\n"
            f"COMPLETED SO FAR:\n{digest or '(nothing completed yet)'}\n\n"
            f"The step '{failed_obj}' FAILED ({last_err or 'no detail'}). Re-plan "
            "ONLY the remaining work still needed to achieve the original "
            "objective, taking that failure into account. Do not repeat steps "
            "already completed above."
        )
        artifact_context = self._artifact_context(st)
        if artifact_context:
            remaining += (
                "\n\nRETAINED LOCAL DOCUMENT ARTIFACTS (opaque metadata only; "
                "use targeted query_document operations if a later step needs "
                f"content):\n{artifact_context}"
            )
        pending_sources = state.normalize_document_sources(
            st.get("document_sources")
        )
        if pending_sources:
            remaining += (
                "\n\nEXPLICIT DOCUMENT INPUTS (retain every exact opaque "
                "source_ref; never substitute):\n"
                + "\n".join(
                    f"{item['source_ref']} type={item['type_label']} "
                    f"family={item['document_family']} state={item['state']}"
                    for item in pending_sources
                )
            )
        planning_failures = []
        pending_requirements = [
            {
                "requirement_id": str(item.get("requirement_id") or ""),
                "text": str(
                    item.get("_runtime_criterion", item.get("criterion", ""))
                    or ""
                ),
            }
            for item in state.checklist_remaining(st)
        ]
        try:
            planning_kwargs = {"failure_cb": planning_failures.append}
            if "language" in inspect.signature(planner.plan_objective).parameters:
                planning_kwargs["language"] = st.get("language")
            if "turn_context" in inspect.signature(planner.plan_objective).parameters:
                planning_kwargs["turn_context"] = fixed_turn_context(
                    st.get("language"),
                    source="autonomous_replan",
                    reference=str(st.get("run_id") or st.get("task_id") or "autonomous"),
                    mode=str(st.get("language_mode") or "single-language"),
                    allowed_languages=st.get("allowed_languages") or (),
                    source_language=st.get("source_language"),
                    target_language=st.get("target_language"),
                )
            if pending_requirements:
                planning_kwargs["requirements"] = pending_requirements
            if pending_sources:
                planning_kwargs["document_sources"] = pending_sources
            with self._artifact_scope(st, self._active_task):
                new_phases = planner.plan_objective(
                    remaining,
                    self.agent,
                    run_id=str(st.get("run_id") or ""),
                    plan_revision=int(st.get("plan_revision", 0)) + 1,
                    **planning_kwargs,
                )
        except Exception:
            new_phases = None

        # Count this replan attempt regardless of outcome (hard cap honoured).
        budgets["replan_count"] = int(budgets.get("replan_count", 0) or 0) + 1

        if not new_phases:
            if planning_failures:
                failure = planning_failures[-1]
                self._record_failure(
                    st,
                    diagnostics.AutonomousStage.REPLAN,
                    failure.error_category,
                    reason_code=failure.reason,
                    resumable=True,
                    checkpoint_present=True,
                )
                reason_code = failure.reason
            else:
                reason_code = "replan_failure"
            self._record_failure(
                st,
                diagnostics.AutonomousStage.REPLAN,
                diagnostics.FailureCategory.REPLAN_FAILURE,
                reason_code=reason_code,
                resumable=False,
                checkpoint_present=True,
            )
            self._append_log(st, "replan_failed", note=reason_code)
            self._safe_save(st)
            return False

        first_new = len(st.get("phases") or [])
        st.setdefault("phases", []).extend(new_phases)
        pending_ids = {
            item["requirement_id"] for item in pending_requirements
        }
        for item in st.get("checklist") or []:
            if (
                isinstance(item, dict)
                and item.get("requirement_id") in pending_ids
            ):
                item.update({
                    "status": "pending",
                    "evidence_ref": "",
                    "failure_reason": "",
                    "retry_eligible": True,
                })
        st["cursor"] = {"phase": first_new, "task": 0}
        authorization.prepare_replan(
            st,
            origin_ref=str(st.get("authorization_origin_ref") or self._origin_ref),
        )
        diagnostics.clear_current(st)
        self._append_log(st, "replan", ref=str(first_new),
                         note=f"appended {len(new_phases)} new phase(s)")
        self._safe_save(st)  # single atomic checkpoint
        self._emit({
            "event": (
                "running" if st.get("run_state") == "ready"
                else "autonomous_plan_ready"
            ),
            **authorization.pc_projection(st),
        })
        return True

    # ── Thin black-box wrappers over the state/persistence layer ──────────────
    # (state = the module; st = a task_state dict — never the two confused.)

    @staticmethod
    def _advance(st):
        state.advance_cursor(st)

    @staticmethod
    def _append_log(st, event, ref=None, note=None):
        state.append_log(st, event, ref=ref, note=note)

    def _safe_save(self, st):
        """Persist a checkpoint; a save failure is logged, never fatal to the
        loop (disk-full / permission error must not crash autonomous mode)."""
        try:
            state.save_task_state(st)
        except Exception:
            diagnostics.set_checkpoint_present(st, False)
            diagnostic = diagnostics.make_diagnostic(
                st,
                stage=diagnostics.AutonomousStage.PERSISTENCE,
                category=diagnostics.FailureCategory.CHECKPOINT_FAILURE,
                reason_code="checkpoint_failure",
                resumable=False,
                checkpoint_present=False,
            )
            diagnostics.emit_diagnostic_log(diagnostic)

    def _emit(self, payload):
        """Fire ``progress_cb`` defensively — a callback error never kills the
        loop (Phase 4 GUI bridge runs here)."""
        cb = self._progress_cb
        if cb is None:
            return
        try:
            cb(dict(payload))
        except Exception:
            diagnostic = diagnostics.make_diagnostic(
                self._state,
                stage=diagnostics.AutonomousStage.ORCHESTRATOR,
                category=diagnostics.FailureCategory.ORCHESTRATOR_FAILURE,
                reason_code="progress_callback_failure",
                resumable=bool(
                    isinstance(self._state, dict)
                    and self._state.get("status") == "in_progress"
                ),
                checkpoint_present=isinstance(self._state, dict),
            )
            diagnostics.emit_diagnostic_log(diagnostic)

    def _record_failure(self, st, stage, category, *, reason_code,
                        attempt=None, resumable=False,
                        checkpoint_present=True) -> dict:
        """Record one failure using current agent/provider metadata only."""
        provider, model = diagnostics.actual_provider_model(
            self.agent,
            st.get("provider", "") if isinstance(st, dict) else "",
            st.get("model", "") if isinstance(st, dict) else "",
        )
        diagnostic = diagnostics.make_diagnostic(
            st,
            stage=stage,
            category=category or diagnostics.FailureCategory.UNKNOWN_FAILURE,
            reason_code=reason_code,
            attempt=attempt,
            provider=provider,
            model=model,
            resumable=resumable,
            checkpoint_present=checkpoint_present,
        )
        return diagnostics.record_diagnostic(st, diagnostic)
