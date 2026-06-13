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
   A second --action start while one is running should get event=busy.

The token is read from <CONFIG_DIR>/mobile_token.txt (same resolution the server
uses) unless you pass --token. Override host/port with --host/--port.
"""

import argparse
import asyncio
import json
import sys
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
    ap.add_argument("--token", default=None, help="mobile token (default: read mobile_token.txt)")
    ap.add_argument("--action", choices=["start", "interrupt", "abort", "status"],
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
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[client] interrupted by user (Ctrl-C)")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
