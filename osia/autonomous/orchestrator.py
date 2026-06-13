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
import threading

from osia.autonomous import planner
# Module-qualified on purpose (like planner.py) so the persistence/mutator layer
# stays a single black box — and so a test can monkeypatch state.save_task_state.
from osia.autonomous import state

# ── Tunables (module constants) ──────────────────────────────────────────────
# Rolling context digest: keep it SMALL so it never balloons the subtask prompt.
DIGEST_MAX_CHARS = 2000       # hard char cap on the whole digest (FIFO-drop oldest)
DIGEST_MAX_LINES = 6          # K — keep at most this many digest lines
SUMMARY_MAX_CHARS = 300       # per-task distilled summary cap (cheap string trim)

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

    def __init__(self, agent, progress_cb=None):
        self.agent = agent
        self._progress_cb = progress_cb
        self._state = None
        self._stop = threading.Event()
        self._thread = None
        # One-shot: set by resume() so the loop re-checks an interrupted task's
        # success_criterion BEFORE re-executing it (crash-salvage). Consumed on
        # the first task examined, regardless of outcome.
        self._resume_recheck = False

    # ── Lifecycle (mirrors CronManager.start/stop) ───────────────────────────

    def start(self, state):
        """Begin executing a *fresh* plan on a background thread (idempotent)."""
        self._begin(state, resume=False)

    def resume(self, state):
        """Resume a persisted, partially-run plan — re-checks the interrupted
        task's success_criterion before re-executing it (idempotent)."""
        self._begin(state, resume=True)

    def _begin(self, st, resume):
        # Idempotent: never spawn a second loop over the same Orchestrator.
        if self._thread and self._thread.is_alive():
            return
        self._state = st
        self._resume_recheck = bool(resume)
        # We are (re-)entering execution ⇒ the run is in progress. Persist once
        # up front so the on-disk checkpoint matches what we are about to run.
        st["status"] = "in_progress"
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
            except Exception as exc:  # never let an unexpected error kill silently
                print(f"[ORCH] loop error: {exc}")
                try:
                    st["status"] = "failed"
                    self._append_log(st, "crashed", note=str(exc))
                    self._safe_save(st)
                except Exception:
                    pass
                self._emit({"event": "failed", "reason": f"orchestrator error: {exc}"})
                break
            if not keep_going:
                break

    def _step(self, st) -> bool:
        """Run ONE task-cycle. Returns whether the loop should continue."""
        cursor = st.get("cursor") or {"phase": 0, "task": 0}
        p = int(cursor.get("phase", 0) or 0)
        t = int(cursor.get("task", 0) or 0)
        phases = st.get("phases") or []

        # (A) Termination — the cursor has walked past the last phase.
        if p >= len(phases):
            st["status"] = "done"
            self._append_log(st, "done", note="all phases complete")
            self._safe_save(st)
            self._emit({"event": "done", "status": "done"})
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
                ok, reason = self._verify(task, task.get("distilled_summary", "") or "")
                if ok:
                    self._salvage(st, p, t, task, reason)
                    return True
            # Not salvageable ⇒ fall through and (re-)execute it normally.

        # (C) Begin an attempt — phase in_progress, bump attempts, CHECKPOINT A.
        phase["status"] = "in_progress"
        attempts = int(task.get("attempts", 0) or 0) + 1
        state.set_task_status(st, p, t, "in_progress", attempts=attempts)
        self._append_log(st, "attempt", ref=task.get("id"),
                         note=f"attempt {attempts}/{max_attempts}")
        self._safe_save(st)  # CHECKPOINT A

        # (D) Run the isolated subtask. run() can raise when fallbacks are
        # exhausted ⇒ treat as an empty result + recorded error (never crash).
        try:
            self.agent.reset()
            result = self.agent.run(self._compose(task, digest))
            if not isinstance(result, str):
                result = "" if result is None else str(result)
        except Exception as exc:
            result = ""
            state.set_task_status(st, p, t, "in_progress", attempts=attempts,
                                  last_error=f"agent.run raised: {exc}")

        # Honour a stop requested DURING the run — persist and exit promptly.
        if self._stop.is_set():
            self._safe_save(st)
            return False

        # (E) Verify doneness (fail-closed).
        ok, reason = self._verify(task, result)

        if ok:
            # (F1) Success — distil, mark done, grow+cap the digest, CHECKPOINT B.
            summary = self._distil(task, result)
            state.set_task_status(st, p, t, "done",
                                  distilled_summary=summary, last_error=None)
            st["context_digest"] = self._cap_digest(
                digest, f"[{task.get('id')}] {summary}")
            if t + 1 >= len(tasks):
                phase["status"] = "done"
            self._advance(st)
            self._append_log(st, "task_done", ref=task.get("id"), note=reason)
            self._safe_save(st)  # CHECKPOINT B
            self._emit({"event": "task_done", "task_id": task.get("id"),
                        "phase": p, "task": t, "reason": reason})
            return True

        # (F2) Failure.
        last_error = reason or task.get("last_error") or "success criterion not met"
        if attempts >= max_attempts:
            # Task is out of attempts — fail it and its phase. CHECKPOINT C.
            state.set_task_status(st, p, t, "failed", last_error=last_error)
            phase["status"] = "failed"
            self._append_log(st, "task_failed", ref=task.get("id"), note=last_error)
            self._safe_save(st)  # CHECKPOINT C
            self._emit({"event": "task_failed", "task_id": task.get("id"),
                        "reason": last_error})

            # Try to recover by re-planning the remaining work, if budget allows.
            if replan_count < max_replans and self._replan(st):
                return True  # loop continues at the freshly-appended phase
            # Out of attempts AND out of replans (or replanning failed) ⇒ stop.
            st["status"] = "failed"
            self._append_log(st, "failed", ref=task.get("id"),
                             note="attempts and replans exhausted")
            self._safe_save(st)
            self._emit({"event": "failed", "task_id": task.get("id"),
                        "reason": last_error})
            return False

        # Still has attempts left — retry. Cursor UNCHANGED; record the error.
        state.set_task_status(st, p, t, "in_progress", last_error=last_error)
        self._append_log(st, "retry", ref=task.get("id"), note=last_error)
        self._safe_save(st)
        return True

    # ── Subtask prompt + verification ────────────────────────────────────────

    def _compose(self, task, digest) -> str:
        """Build the isolated subtask prompt: objective + success_criterion +
        the (defensively-truncated) rolling context digest, each clearly
        labelled. The agent is told to do ONLY this step."""
        objective = (task.get("objective") or "").strip()
        criterion = (task.get("success_criterion") or "").strip()
        digest = digest or ""
        if len(digest) > DIGEST_MAX_CHARS:  # defensive — digest is already capped
            digest = digest[-DIGEST_MAX_CHARS:]
        parts = [
            "You are executing ONE step of a larger autonomous task. Do ONLY "
            "this step — do not attempt the rest of the overall objective.",
            "",
            f"STEP OBJECTIVE:\n{objective}",
            "",
            "SUCCESS CRITERION (you are done only when this is OBSERVABLY true):\n"
            f"{criterion}",
        ]
        if digest.strip():
            parts += [
                "",
                "CONTEXT SO FAR (results of previous steps — for reference only):\n"
                f"{digest}",
            ]
        parts += ["", "Carry out the step now, then stop."]
        return "\n".join(parts)

    def _verify(self, task, evidence):
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
        criterion = (task.get("success_criterion") or "").strip()
        if not criterion:
            # No observable criterion ⇒ we can't prove doneness ⇒ fail-closed.
            return False, "no success_criterion to verify against"

        verdict = self._file_precheck(criterion)
        if verdict is True:
            return True, "file criterion satisfied on disk"
        if verdict is False:
            return False, "required file missing or empty on disk"

        # Undecided ⇒ LLM judge by direct observation (evidence intentionally
        # NOT fed in: the whole point is to re-check, not to echo a self-report).
        return self._llm_judge(criterion)

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

    def _llm_judge(self, criterion):
        """Ask the agent to verify ``criterion`` by direct observation and parse
        a robust DONE / NOT_DONE verdict (ambiguous ⇒ NOT_DONE; run error ⇒
        NOT_DONE — fail-closed)."""
        prompt = (
            "Verify by DIRECT observation (take a screenshot, read the file, or "
            "re-open the page) whether the following is TRUE right now:\n"
            f"{criterion}\n\n"
            "Do NOT trust any earlier claim — check it yourself. Reply with a "
            "single word on the first line: DONE or NOT_DONE. Then one short "
            "line explaining why."
        )
        try:
            self.agent.reset()
            reply = self.agent.run(prompt)
        except Exception as exc:
            return False, f"verification run failed: {exc}"
        return self._parse_verdict(reply)

    @staticmethod
    def _parse_verdict(reply):
        """Robustly parse a DONE/NOT_DONE judge reply. Tests the NOT_DONE family
        FIRST (so ``NOT_DONE`` never reads as a bare ``DONE`` substring), accepts
        ``DONE`` only as a standalone word (so ``ABANDONED`` doesn't match), and
        treats anything ambiguous/empty as NOT_DONE."""
        text = (reply or "").strip()
        if not text:
            return False, "empty verification reply → NOT_DONE"
        up = text.upper()
        # Collapse separators so NOT-DONE / NOT DONE / NOT_DONE all normalise.
        norm = re.sub(r"[\s_\-]+", " ", up)
        if "NOT DONE" in norm or "NOTDONE" in up:
            return False, Orchestrator._short(text)
        if re.search(r"\bDONE\b", norm):
            return True, Orchestrator._short(text)
        return False, "ambiguous verdict → treated as NOT_DONE"

    # ── Summary / digest helpers (cheap string ops — no agent on the hot path) ─

    def _distil(self, task, result) -> str:
        """Distil a task result into a short summary line. Cheap string trim to
        ``SUMMARY_MAX_CHARS`` (kept string-based for now per the Phase-3 spec —
        no agent summarisation even for large output)."""
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
                    "reason": "verified satisfied on resume"})

    def _replan(self, st) -> bool:
        """Re-plan the REMAINING work after a task ran out of attempts.

        Builds a "remaining objective" (original goal + what's done so far + the
        failure), asks the planner for fresh phases, and on success appends them,
        moves the cursor to the first new phase and bumps ``replan_count`` — a
        single atomic checkpoint. Returns ``True`` if re-planned, ``False`` if the
        planner produced nothing (the caller then fails the run). The replan
        attempt is counted either way so ``max_replans`` is a hard cap."""
        budgets = st.setdefault("budgets", {})
        objective = st.get("objective", "") or ""
        digest = st.get("context_digest", "") or ""

        cursor = st.get("cursor") or {}
        p = int(cursor.get("phase", 0) or 0)
        t = int(cursor.get("task", 0) or 0)
        failed_obj, last_err = "", ""
        phases = st.get("phases") or []
        if 0 <= p < len(phases):
            tasks = phases[p].get("tasks") or []
            if 0 <= t < len(tasks):
                failed_obj = tasks[t].get("objective", "") or ""
                last_err = tasks[t].get("last_error", "") or ""

        remaining = (
            f"ORIGINAL OBJECTIVE:\n{objective}\n\n"
            f"COMPLETED SO FAR:\n{digest or '(nothing completed yet)'}\n\n"
            f"The step '{failed_obj}' FAILED ({last_err or 'no detail'}). Re-plan "
            "ONLY the remaining work still needed to achieve the original "
            "objective, taking that failure into account. Do not repeat steps "
            "already completed above."
        )
        try:
            new_phases = planner.plan_objective(remaining, self.agent)
        except Exception as exc:
            new_phases = None
            last_err = f"replan crashed: {exc}"

        # Count this replan attempt regardless of outcome (hard cap honoured).
        budgets["replan_count"] = int(budgets.get("replan_count", 0) or 0) + 1

        if not new_phases:
            self._append_log(st, "replan_failed", note="planner returned no plan")
            self._safe_save(st)
            return False

        first_new = len(st.get("phases") or [])
        st.setdefault("phases", []).extend(new_phases)
        st["cursor"] = {"phase": first_new, "task": 0}
        self._append_log(st, "replan", ref=str(first_new),
                         note=f"appended {len(new_phases)} new phase(s)")
        self._safe_save(st)  # single atomic checkpoint
        return True

    # ── Thin black-box wrappers over the state/persistence layer ──────────────
    # (state = the module; st = a task_state dict — never the two confused.)

    @staticmethod
    def _advance(st):
        state.advance_cursor(st)

    @staticmethod
    def _append_log(st, event, ref=None, note=None):
        state.append_log(st, event, ref=ref, note=note)

    @staticmethod
    def _safe_save(st):
        """Persist a checkpoint; a save failure is logged, never fatal to the
        loop (disk-full / permission error must not crash autonomous mode)."""
        try:
            state.save_task_state(st)
        except Exception as exc:
            print(f"[ORCH] checkpoint save failed: {exc}")

    def _emit(self, payload):
        """Fire ``progress_cb`` defensively — a callback error never kills the
        loop (Phase 4 GUI bridge runs here)."""
        cb = self._progress_cb
        if cb is None:
            return
        try:
            cb(dict(payload))
        except Exception as exc:
            print(f"[ORCH] progress_cb error: {exc}")
