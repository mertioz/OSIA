#!/usr/bin/env python3
"""Standalone WebSocket test client for the autonomous-agent server (WOR-45 Phase 5).

Connects to the OSIA mobile server's /ws endpoint, authenticates with the mobile
token, then drives the autonomous engine end-to-end so you can validate the
SERVER side before the Flutter app exists. It prints every ``autonomous_progress``
frame the server broadcasts.

It uses the ``websockets`` library, which is already installed in the server's
venv (``uvicorn[standard]`` pulls it in). No other dependency.

──────────────────────────────────────────────────────────────────────────────
MANUAL TEST (end-to-end)
──────────────────────────────────────────────────────────────────────────────
1. Make sure a provider key is configured for the model you'll use (e.g. DeepSeek)
   in config/api_keys.json, and that model is one of OSIA's configured models.

2. Start the server (separate terminal), from the repo root:
       python -m osia.server.server
   (or however you normally launch it — run_server.py / systemd). It binds
   0.0.0.0:8765 and prints "[OSIA Server] Active model: ...".

3. In another terminal, from the repo root, run a trivial autonomous task:
       python tools/test_autonomous_ws.py \
           --objective "Create a folder named osia_p5_test and write a file hello.txt containing the word OK inside it" \
           --model deepseek-v4-pro

   You should see frames like:
       <-- autonomous_progress  event=running       0/3  phase='...'  status=in_progress
       <-- autonomous_progress  event=task_done     1/3  phase='...'  status=in_progress
       ...
       <-- autonomous_progress  event=done          3/3  phase='...'  status=done
   The client exits on a terminal event (done/failed/aborted/plan_failed) or
   on --timeout / Ctrl-C.

4. Other actions (open a SECOND client while a run is live to test these):
       python tools/test_autonomous_ws.py --action status      # render current progress
       python tools/test_autonomous_ws.py --action interrupt    # pause (resumable)
       python tools/test_autonomous_ws.py --action abort        # discard the run
       python tools/test_autonomous_ws.py --action resume       # resume a paused run from its cursor
   A second --action start while one is running should get event=busy.

5. Resume flow (WOR-45 server-side resume):
       # start a task, let a task or two complete, then in another terminal:
       python tools/test_autonomous_ws.py --action interrupt    # -> event=interrupted
       python tools/test_autonomous_ws.py --action status       # -> event=interrupted (was wrongly "running" before)
       python tools/test_autonomous_ws.py --action resume       # -> event=running, then re-runs from the cursor to done
   Resume re-acquires the single-runner lock (interrupt freed it); if another run
   holds it you get event=busy, and if nothing is resumable you get event=idle.

The token is read from <CONFIG_DIR>/mobile_token.txt (same resolution the server
uses) unless you pass --token. Override host/port with --host/--port.
"""

import argparse
import asyncio
import json
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path


def _import_websockets():
    """Lazy import so --help works even outside the server venv."""
    try:
        import websockets  # noqa: F401
        return websockets
    except ImportError:
        sys.exit(
            "This test needs the 'websockets' library (installed with "
            "uvicorn[standard]).\nRun it from the server's venv, or: pip install websockets"
        )


# Events that end an autonomous run — the client stops listening once one arrives.
_TERMINAL_EVENTS = {"done", "failed", "aborted", "plan_failed", "busy", "idle"}


def _resolve_token(explicit: "str | None") -> str:
    """Return the mobile token: the --token value if given, else read it from
    <CONFIG_DIR>/mobile_token.txt using the SAME resolution the server uses."""
    if explicit:
        return explicit.strip()
    # Prefer osia.config (authoritative); fall back to the two candidate dirs so
    # the script still works if run outside an importable repo checkout.
    candidates = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from osia.config import CONFIG_DIR  # type: ignore
        candidates.append(Path(CONFIG_DIR) / "mobile_token.txt")
    except Exception:
        pass
    repo = Path(__file__).resolve().parent.parent
    candidates.append(repo / "config" / "mobile_token.txt")
    candidates.append(Path.home() / ".osia" / "mobile_token.txt")
    for path in candidates:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    sys.exit(
        "Could not find mobile_token.txt. Pass --token <token> explicitly "
        f"(looked in: {', '.join(str(c) for c in candidates)})"
    )


def _fmt_progress(frame: dict) -> str:
    """One-line render of an autonomous_progress frame."""
    ev = frame.get("event", "?")
    done = frame.get("tasks_done")
    total = frame.get("tasks_total")
    phase = frame.get("phase_name", "")
    status = frame.get("status", "")
    counts = f"{done}/{total}" if total is not None else "-/-"
    extra = ""
    for k in ("task_id", "reason"):
        if frame.get(k):
            extra += f"  {k}={frame[k]!r}"
    return f"event={ev:<12} {counts:>5}  phase={phase!r:<24} status={status!r}{extra}"


@contextmanager
def _isolated_task_state(state_module):
    """Redirect only the mutable autonomous checkpoint to a temporary root."""
    original = state_module.TASK_STATE_FILE
    with tempfile.TemporaryDirectory(prefix="osia-wor125-autonomous-") as tmp:
        root = Path(tmp)
        checkpoint = root / "task_state.json"
        state_module.TASK_STATE_FILE = checkpoint
        try:
            yield root, checkpoint
        finally:
            state_module.TASK_STATE_FILE = original


def _run_isolation_self_test() -> int:
    """Deterministically prove isolation never redirects/copies user config."""
    class _FakeState:
        TASK_STATE_FILE = Path("original-task-state.json")

    original = _FakeState.TASK_STATE_FILE
    home = Path.home()
    root = None
    checks = {}
    with _isolated_task_state(_FakeState) as (temp_root, checkpoint):
        root = temp_root
        checks["checkpoint_redirected"] = checkpoint.parent == temp_root
        checks["home_unchanged"] = Path.home() == home
        checks["no_config_copied"] = not any(
            (temp_root / name).exists()
            for name in ("settings.json", "api_keys.json", "mobile_token.txt")
        )
        checkpoint.write_text("temporary", encoding="utf-8")
    checks["state_path_restored"] = _FakeState.TASK_STATE_FILE == original
    checks["temp_cleaned"] = root is not None and not root.exists()
    for name, passed in checks.items():
        print(f"AUTONOMOUS_ISOLATION_CHECK {name}={str(bool(passed)).lower()}")
    return 0 if checks and all(checks.values()) else 1


def _run_direct_isolated(args) -> int:
    """Exercise real planning while isolating only mutable autonomous state.

    Provider settings and credentials remain in their authoritative user config
    and are read in place; they are never copied into the temporary directory.
    The plan checkpoint lives under a TemporaryDirectory and is removed after a
    controlled cancellation immediately after the orchestrator starts.
    """
    repo = Path(__file__).resolve().parent.parent
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    from osia.agent import OSIAAgent
    from osia.autonomous import planner, state
    from osia.autonomous.orchestrator import Orchestrator
    from osia.config import load_settings
    from osia.models import provider_for_model

    objective = (args.objective or "").strip()
    model = (args.model or load_settings().get("selected_model") or "").strip()
    if not objective or not model:
        print("AUTONOMOUS_DIAGNOSTIC_FAILURE reason=invalid_request")
        return 2
    provider = provider_for_model(model)

    agent = OSIAAgent()
    agent.set_model(model, provider=provider)
    failures = []
    frames = []
    original_state_file = state.TASK_STATE_FILE
    temp_root = None
    checks = {}

    try:
        with _isolated_task_state(state) as (temp_root, checkpoint):

            planned = planner.create_plan_state(
                objective, provider, model, agent,
                failure_cb=failures.append,
            )
            if not isinstance(planned, dict):
                failure = failures[-1] if failures else None
                reason = failure.reason if failure else "unknown"
                status = (
                    f" status={failure.status_code}"
                    if failure and failure.status_code is not None else ""
                )
                print(
                    f"AUTONOMOUS_DIAGNOSTIC_FAILURE event=plan_failed "
                    f"reason={reason}{status}"
                )
                checks["checkpoint_absent_after_failure"] = not checkpoint.exists()
                return 1

            phases = planned.get("phases") or []
            tasks = [task for phase in phases for task in phase.get("tasks", [])]
            checks["valid_plan"] = bool(phases and tasks)
            state.save_task_state(planned)
            persisted = state.load_task_state()
            checks["run_id_correlated"] = (
                isinstance(persisted, dict)
                and persisted.get("task_id") == planned.get("task_id")
            )

            # Planning above used the real selected provider. Hold execution at
            # the agent boundary so starting the real orchestrator cannot invoke
            # tools or perform computer-control side effects before cancellation.
            release_execution = threading.Event()
            original_run = agent.run

            def _held_execution(*_args, **_kwargs):
                release_execution.wait(timeout=max(5.0, args.timeout))
                return "controlled cancellation"

            agent.run = _held_execution
            orch = Orchestrator(agent, progress_cb=lambda frame: frames.append(dict(frame)))
            try:
                orch.start(planned)
                first_thread = orch._thread
                orch.start(planned)
                checks["no_duplicate_run"] = orch._thread is first_thread
                checks["orchestrator_started"] = (
                    first_thread is not None and first_thread.is_alive()
                )
                planned["status"] = "aborted"
                orch.stop()
                release_execution.set()
                if first_thread is not None:
                    first_thread.join(timeout=max(5.0, args.timeout))
                checks["no_orphan_run"] = not any(
                    thread.name == "osia-orchestrator" and thread.is_alive()
                    for thread in threading.enumerate()
                )
            finally:
                release_execution.set()
                agent.run = original_run

            state.clear_task_state()
            checks["checkpoint_cleaned"] = not checkpoint.exists()
            checks["no_plan_failed"] = all(
                frame.get("event") != "plan_failed" for frame in frames
            )

            print(
                "AUTONOMOUS_DIAGNOSTIC_PLAN_CREATED "
                f"run_id={planned.get('task_id')} phases={len(phases)} "
                f"tasks={len(tasks)}"
            )
            print(
                "AUTONOMOUS_DIAGNOSTIC_EVENT event=running "
                f"phase_index=0 task_id={tasks[0].get('id')}"
            )
    finally:
        state.TASK_STATE_FILE = original_state_file

    checks["temp_cleaned"] = temp_root is not None and not temp_root.exists()
    for name, passed in checks.items():
        print(f"AUTONOMOUS_DIAGNOSTIC_CHECK {name}={str(bool(passed)).lower()}")
    if checks and all(checks.values()):
        print("AUTONOMOUS_DIAGNOSTIC_PASS marker=OSIA-WOR125-AUTONOMOUS-PASS")
        return 0
    print("AUTONOMOUS_DIAGNOSTIC_FAILURE reason=acceptance_check")
    return 1


async def _run(args) -> int:
    websockets = _import_websockets()
    token = _resolve_token(args.token)
    uri = f"ws://{args.host}:{args.port}/ws"
    print(f"[client] connecting to {uri}")
    async with websockets.connect(uri, max_size=None) as ws:
        # ── Auth ──
        await ws.send(json.dumps({"type": "auth", "token": token}))
        # Drain the post-auth burst (status/done + conversations) until we see
        # the auth ack, so it doesn't clutter the progress stream.
        authed = False
        while not authed:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") == "status" and msg.get("content") in ("done", "error"):
                if msg.get("content") == "error":
                    print("[client] AUTH FAILED (bad token?)")
                    return 2
                authed = True
                print("[client] authenticated\n")
        # Ignore any remaining burst frames briefly (conversations list, etc.).
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.4)
                msg = json.loads(raw)
                if msg.get("type") == "autonomous_progress":
                    # Unlikely this early, but don't lose it.
                    print(f"<-- {_fmt_progress(msg)}")
        except asyncio.TimeoutError:
            pass

        # ── Send the requested action ──
        if args.action == "start":
            payload = {"type": "autonomous_start",
                       "objective": args.objective, "model": args.model}
            print(f"--> autonomous_start objective={args.objective!r} model={args.model!r}")
        elif args.action == "interrupt":
            payload = {"type": "autonomous_interrupt"}
            print("--> autonomous_interrupt")
        elif args.action == "abort":
            payload = {"type": "autonomous_abort"}
            print("--> autonomous_abort")
        elif args.action == "resume":
            payload = {"type": "autonomous_resume"}
            print("--> autonomous_resume")
        else:  # status
            payload = {"type": "get_autonomous_status"}
            print("--> get_autonomous_status")
        await ws.send(json.dumps(payload))
        print()

        # ── Stream autonomous_progress frames until terminal / timeout ──
        deadline = asyncio.get_event_loop().time() + args.timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                print(f"\n[client] --timeout {args.timeout}s reached, exiting")
                return 0
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                print(f"\n[client] --timeout {args.timeout}s reached, exiting")
                return 0
            msg = json.loads(raw)
            if msg.get("type") == "autonomous_progress":
                print(f"<-- {_fmt_progress(msg)}")
                if msg.get("event") in _TERMINAL_EVENTS:
                    # For one-shot queries (status/interrupt/abort) and terminal
                    # run outcomes, one frame is enough — stop here.
                    if args.action != "start" or msg.get("event") != "running":
                        print(f"\n[client] terminal/▸ event '{msg.get('event')}' — done")
                        return 0
            else:
                # Surface any other frames (errors, etc.) without parsing them.
                print(f"<-- (other) type={msg.get('type')!r} {str(msg)[:120]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Drive the OSIA autonomous server over WebSocket.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--direct-isolated", action="store_true",
        help=("run real provider planning in-process with only the mutable "
              "autonomous checkpoint isolated; no WebSocket server required"),
    )
    ap.add_argument(
        "--self-test-isolation", action="store_true",
        help="run deterministic temporary-state isolation checks and exit",
    )
    ap.add_argument("--token", default=None, help="mobile token (default: read mobile_token.txt)")
    ap.add_argument("--action", choices=["start", "interrupt", "abort", "status", "resume"],
                    default="start")
    ap.add_argument("--objective",
                    default="Create a folder named osia_p5_test and write a file "
                            "hello.txt containing the word OK inside it",
                    help="objective for --action start")
    ap.add_argument("--model", default="", help="model id for --action start "
                    "(empty = server falls back to the configured selected_model)")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="seconds to listen before exiting (default 600)")
    args = ap.parse_args()
    try:
        if args.self_test_isolation:
            return _run_isolation_self_test()
        if args.direct_isolated:
            return _run_direct_isolated(args)
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[client] interrupted by user (Ctrl-C)")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
