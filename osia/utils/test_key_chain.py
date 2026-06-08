#!/usr/bin/env python3
"""Smoke tests for :mod:`osia.utils.key_chain` — cooldown cache + per-task
sticky key, plus the legacy-equivalence guarantee.

Pure unit tests: no real API keys, no config, no network. ``_resolve_keys`` is
monkeypatched to a fixed 5-key chain and ``call_fn`` is faked, so the tests
exercise rotation/stickiness logic in isolation.

Run:  venv/bin/python -m osia.utils.test_key_chain
 or:  venv/bin/python osia/utils/test_key_chain.py
"""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

import osia.utils.key_chain as kc

CHAIN = ["k1", "k2", "k3", "k4", "k5"]


class FakeHTTPError(Exception):
    """Exception carrying a ``status_code`` like the OpenAI/requests errors that
    :func:`kc.error_status` reads, so it classifies as technical (429/5xx/auth)."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _install_chain(keys=CHAIN):
    kc._resolve_keys = lambda provider: list(keys)  # type: ignore[assignment]


def _reset():
    """Clear the process-wide cooldown cache between tests (sticky state is
    cleared automatically by leaving each ``task_scope``)."""
    with kc._cooldowns_lock:
        kc._key_cooldowns.clear()


def _caller(behavior):
    """Build ``(call_fn, attempts)``. ``behavior`` maps key -> ``"ok"`` or an int
    HTTP status to raise. ``attempts`` records every key tried, in order."""
    attempts = []

    def call_fn(key):
        attempts.append(key)
        outcome = behavior.get(key, "ok")
        if outcome == "ok":
            return f"result:{key}"
        raise FakeHTTPError(outcome)

    return call_fn, attempts


def _silent():
    """A log sink that captures lines for assertions instead of printing."""
    lines = []
    return (lambda m: lines.append(m)), lines


# ── individual tests ───────────────────────────────────────────────────────

def test_sticky_across_three_calls():
    """(1) Once key4 succeeds, the next calls in the same task go STRAIGHT to
    key4 — key1/2/3 are never re-tested."""
    _reset(); _install_chain()
    log, lines = _silent()
    with kc.task_scope():
        # Call A: k1,k2,k3 rate-limited, k4 works -> active becomes k4.
        call, att = _caller({"k1": 429, "k2": 429, "k3": 429, "k4": "ok"})
        assert kc.with_key_rotation("p", call, log=log) == "result:k4"
        assert att == ["k1", "k2", "k3", "k4"], att

        # Calls B and C: must jump straight to k4, no re-walk of k1..k3.
        for _ in range(2):
            call, att = _caller({"k4": "ok"})
            assert kc.with_key_rotation("p", call, log=log) == "result:k4"
            assert att == ["k4"], att

    # The sticky-start log line uses the documented format.
    assert any("task: starting from active key4" in l and "skipping key1-3" in l
               for l in lines), lines


def test_active_dies_advances_forward_not_restart():
    """(2) When the active key (k4) dies mid-task, the chain advances FORWARD to
    k5 (which becomes the new active key) — it does NOT restart at key1."""
    _reset(); _install_chain()
    log, lines = _silent()
    with kc.task_scope():
        # Establish active = k4.
        call, att = _caller({"k1": 429, "k2": 429, "k3": 429, "k4": "ok"})
        kc.with_key_rotation("p", call, log=log)

        # k4 now 429s; k5 works. k1/k2/k3 would also fail but must NOT be tried.
        call, att = _caller({"k1": 429, "k2": 429, "k3": 429, "k4": 429, "k5": "ok"})
        assert kc.with_key_rotation("p", call, log=log) == "result:k5"
        assert att == ["k4", "k5"], att  # forward from active, never key1
        assert "k1" not in att and "k2" not in att and "k3" not in att

        # k5 is the new active key: the next call goes straight to it.
        call, att = _caller({"k5": "ok"})
        assert kc.with_key_rotation("p", call, log=log) == "result:k5"
        assert att == ["k5"], att

    assert any("task: active key4" in l and "advancing to key5" in l
               for l in lines), lines


def test_new_task_starts_fresh_from_key1():
    """(3) A fresh task ignores the previous task's active key and starts from
    key1 again (cooldowns aside)."""
    _reset(); _install_chain()
    # Task A: drive active to k4.
    with kc.task_scope():
        call, _ = _caller({"k1": 429, "k2": 429, "k3": 429, "k4": "ok"})
        kc.with_key_rotation("p", call)
    _reset()  # clear cooldowns so task B is a clean slate (sticky already gone)

    # Task B: everything healthy -> must start at k1, NOT the old active k4.
    with kc.task_scope():
        call, att = _caller({"k1": "ok"})
        assert kc.with_key_rotation("p", call) == "result:k1"
        assert att == ["k1"], att

    # And with no scope at all: also starts at k1.
    call, att = _caller({"k1": "ok"})
    assert kc.with_key_rotation("p", call) == "result:k1"
    assert att == ["k1"], att


def test_sticky_and_cooldown_compose():
    """(4) Sticky start composes WITH the cooldown cache: the active key is only
    the starting point — if it is cooling, it is still skipped (jumped over) to
    the next live key forward."""
    _reset(); _install_chain()
    log, lines = _silent()
    with kc.task_scope():
        # active = k4, and k1/k2/k3 are now parked in cooldown (they 429'd).
        call, _ = _caller({"k1": 429, "k2": 429, "k3": 429, "k4": "ok"})
        kc.with_key_rotation("p", call, log=log)

        # Park the active key k4 too (a real technical failure), without yet
        # finding a new winner: k4 429s, then k5 works -> active advances to k5.
        call, att = _caller({"k4": 429, "k5": "ok"})
        assert kc.with_key_rotation("p", call, log=log) == "result:k5"
        assert att == ["k4", "k5"], att

        # Now make k5 the parked one and re-pin active to k4 to prove the
        # COOLING active key is jumped over (not retried) on the next call.
        kc._set_active("p", 3)          # active -> k4 (index 3)
        kc._park_key("p", 3, 429)       # k4 cooling
        call, att = _caller({"k5": "ok"})
        assert kc.with_key_rotation("p", call, log=log) == "result:k5"
        # Started at the active key's position but k4 was cooling, so jumped to k5.
        assert att == ["k5"], att
        assert "k4" not in att          # cooled active key not re-tested

    # Cooldown-skip log for the cooling active key was emitted.
    assert any("cooldown" in l and "key4" in l for l in lines), lines


def test_sticky_start_log_suppressed_when_active_cooling():
    """When the remembered active key is itself cooling, the chain silently jumps
    to the next live key — and must NOT print a 'starting from active keyN' line
    it would immediately contradict with that key's cooldown-skip line."""
    _reset(); _install_chain()
    with kc.task_scope():
        kc._set_active("p", 3)          # active -> k4
        kc._park_key("p", 3, 429)       # k4 cooling
        log, lines = _silent()
        call, att = _caller({"k5": "ok"})
        assert kc.with_key_rotation("p", call, log=log) == "result:k5"
        assert att == ["k5"], att
        assert not any("starting from active" in l for l in lines), lines
        assert any("cooldown" in l and "key4" in l for l in lines), lines


def test_legacy_equivalence_outside_scope():
    """No task_scope -> byte-for-byte legacy behavior: natural order, records
    nothing, content errors surface immediately, chain exhaustion re-raises."""
    _reset(); _install_chain()
    # Natural walk k1->k2->k3, k3 works. No stickiness leaks to the next call.
    call, att = _caller({"k1": 429, "k2": 429, "k3": "ok"})
    assert kc.with_key_rotation("p", call) == "result:k3"
    assert att == ["k1", "k2", "k3"], att
    # Next call STILL starts at k1 (nothing remembered outside a scope). Reset
    # the cooldown cache first — k1/k2 are parked from the walk above; that is
    # pre-existing cooldown behavior, separate from the sticky key under test.
    _reset()
    call, att = _caller({"k1": "ok"})
    assert kc.with_key_rotation("p", call) == "result:k1"
    assert att == ["k1"], att

    # Content error (400) is re-raised immediately, no rotation.
    _reset()
    call, att = _caller({"k1": 400})
    try:
        kc.with_key_rotation("p", call)
        assert False, "expected 400 to propagate"
    except FakeHTTPError as e:
        assert e.status_code == 400
    assert att == ["k1"], att  # never rotated past the first key

    # Whole chain technical-failing re-raises the LAST error (caller falls back).
    _reset()
    call, att = _caller({k: 429 for k in CHAIN})
    try:
        kc.with_key_rotation("p", call)
        assert False, "expected exhaustion to re-raise"
    except FakeHTTPError as e:
        assert e.status_code == 429
    assert att == CHAIN, att

    # Empty chain -> KeyChainEmpty.
    _install_chain([])
    try:
        kc.with_key_rotation("p", lambda k: "x")
        assert False, "expected KeyChainEmpty"
    except kc.KeyChainEmpty as e:
        assert e.provider == "p"
    _install_chain()


def test_iter_keys_reorder_and_isolation():
    """iter_keys rotates to the active key inside a scope, stays natural outside,
    and never records an active key itself."""
    _reset(); _install_chain()
    # Outside any scope: natural order.
    assert kc.iter_keys("p") == CHAIN

    with kc.task_scope():
        # No active recorded yet -> natural order; iter_keys must not record one.
        assert kc.iter_keys("p") == CHAIN
        assert kc._get_active("p") is None
        # A with_key_rotation success records active = k4.
        call, _ = _caller({"k1": 429, "k2": 429, "k3": 429, "k4": "ok"})
        kc.with_key_rotation("p", call)
        assert kc._get_active("p") == 3
        # iter_keys starts from active k4; k1-3 are cooling so they drop out
        # (sticky start + cooldown filtering compose).
        assert kc.iter_keys("p") == ["k4", "k5"]
        # With cooldowns cleared, the PURE wrapped reorder from the active key
        # is visible (active stays set — it lives in the scope, not the cache).
        _reset()
        assert kc.iter_keys("p") == ["k4", "k5", "k1", "k2", "k3"]

    # Scope exited -> back to natural order, active forgotten.
    assert kc.iter_keys("p") == CHAIN


def test_scopes_nest_and_isolate():
    """A nested task_scope is its own fresh sub-task; leaving it restores the
    outer task's active key."""
    _reset(); _install_chain()
    with kc.task_scope():
        call, _ = _caller({"k1": 429, "k2": "ok"})
        kc.with_key_rotation("p", call)
        assert kc._get_active("p") == 1          # outer active = k2
        with kc.task_scope():
            assert kc._get_active("p") is None    # nested starts fresh
            call, _ = _caller({"k1": 429, "k2": 429, "k3": 429, "k4": "ok"})
            kc.with_key_rotation("p", call)
            assert kc._get_active("p") == 3       # nested active = k4
        assert kc._get_active("p") == 1          # outer restored after nesting
    assert kc._get_active("p") is None           # all scopes gone


def test_record_active_key_resolves_index_and_seeds_start():
    """``record_active_key`` is the chat path's recorder: it maps a winning key
    STRING back to its raw-chain index and seeds the active key, so the next
    call (iter_keys / with_key_rotation) starts straight from it."""
    _reset(); _install_chain()
    with kc.task_scope():
        # The chat loop "won" on k3 -> publish it as active by string.
        kc.record_active_key("p", "k3")
        assert kc._get_active("p") == 2          # index of "k3"
        # A subsequent rotation now starts at k3 (k1/k2 are never tried).
        call, att = _caller({"k3": "ok"})
        assert kc.with_key_rotation("p", call) == "result:k3"
        assert att == ["k3"]
        # iter_keys reflects the same seeded start (k3 first, then forward).
        assert kc.iter_keys("p") == ["k3", "k4", "k5", "k1", "k2"]


def test_record_active_key_noops_outside_scope_and_for_unknown_key():
    """Recording is a strict no-op outside a task_scope, for an empty key, and
    for a key that isn't in the configured chain (no stable index to pin) —
    so legacy behavior and a shrunk/edited config never get a bogus active key."""
    _reset(); _install_chain()
    # Outside any scope: no state to write, and _get_active stays None.
    kc.record_active_key("p", "k3")
    assert kc._get_active("p") is None
    with kc.task_scope():
        kc.record_active_key("p", "")            # empty key
        assert kc._get_active("p") is None
        kc.record_active_key("p", "not-in-chain")  # unknown key
        assert kc._get_active("p") is None
        # A real key still records normally.
        kc.record_active_key("p", "k2")
        assert kc._get_active("p") == 1


def _run_in_worker(ex, fn, *, copy_ctx):
    """Mirror ``agent._execute_tool_once``'s dispatch: run ``fn`` on a pool
    thread, optionally inside a copy of the caller's context so a ContextVar
    scope set on this thread is visible (and its in-place mutation reflected
    back) in the worker. With ``copy_ctx=False`` it's a plain submit — the
    legacy behavior, where the scope does NOT cross the thread boundary."""
    if copy_ctx:
        ctx = contextvars.copy_context()
        return ex.submit(ctx.run, fn).result(timeout=5)
    return ex.submit(fn).result(timeout=5)


def test_copy_context_propagates_active_key_across_threads():
    """The desktop_click scenario: three vision calls run on the tool-executor
    thread reuse ONE active key instead of re-rotating from key1 each call —
    BUT only because the dispatch copies the context. Without the copy (legacy
    plain submit) the scope never crosses the thread boundary, so nothing is
    recorded and each call would restart at key1."""
    _install_chain()
    ex = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-tool")
    try:
        # ── With copy_context: scope crosses into the worker thread. ──
        _reset()
        with kc.task_scope():
            seen = []

            def vision_call():
                # k1 is rate-limited, k2 works — like a vision provider whose
                # primary key is throttled. with_key_rotation records the winner.
                call, att = _caller({"k1": 429, "k2": "ok"})
                out = kc.with_key_rotation("vis", call)
                seen.append(att)
                return out

            r1 = _run_in_worker(ex, vision_call, copy_ctx=True)
            # The key the WORKER recorded is visible from THIS (main) thread —
            # proves the in-place dict mutation crossed the thread boundary.
            assert kc._get_active("vis") == 1      # index of k2
            r2 = _run_in_worker(ex, vision_call, copy_ctx=True)
            r3 = _run_in_worker(ex, vision_call, copy_ctx=True)
            assert r1 == r2 == r3 == "result:k2"
            # Call 1 walked k1->k2; calls 2 and 3 start straight at k2. So k1 is
            # attempted exactly ONCE across all three calls, never re-rotated.
            all_attempts = [k for att in seen for k in att]
            assert all_attempts.count("k1") == 1, all_attempts
            assert all_attempts.count("k2") == 3, all_attempts

        # ── Without copy_context: scope does NOT propagate (legacy). ──
        _reset()
        with kc.task_scope():
            def vision_call_plain():
                call, _ = _caller({"k1": "ok"})
                return kc.with_key_rotation("vis", call)

            _run_in_worker(ex, vision_call_plain, copy_ctx=False)
            # Worker had no scope of its own, so _set_active no-op'd there and
            # the main thread's scope stayed empty — nothing was recorded.
            assert kc._get_active("vis") is None
    finally:
        ex.shutdown(wait=True)


TESTS = [
    test_sticky_across_three_calls,
    test_active_dies_advances_forward_not_restart,
    test_new_task_starts_fresh_from_key1,
    test_sticky_and_cooldown_compose,
    test_sticky_start_log_suppressed_when_active_cooling,
    test_legacy_equivalence_outside_scope,
    test_iter_keys_reorder_and_isolation,
    test_scopes_nest_and_isolate,
    test_record_active_key_resolves_index_and_seeds_start,
    test_record_active_key_noops_outside_scope_and_for_unknown_key,
    test_copy_context_propagates_active_key_across_threads,
]


def main() -> int:
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001 - test harness reports all failures
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    total = len(TESTS)
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
