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
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
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
_SUCCESS_MARKER = "AUTONOMOUS_DIAGNOSTIC_SUCCESS"
_FAILURE_MARKER = "AUTONOMOUS_DIAGNOSTIC_FAILURE"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PRODUCTION_TASK_STATE = _REPO_ROOT / "config" / "task_state_mobile.json"
_LEGACY_TEST_PROJECT = _REPO_ROOT / "osia_p5_test"


def _ascii_text(value) -> str:
    """Render dynamic server text losslessly without console-only Unicode."""
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def _fingerprint(path: Path):
    """Return a content-only fingerprint without exposing file contents."""
    if not path.exists():
        return None
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


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
    ev = _ascii_text(frame.get("event", "?"))
    done = frame.get("tasks_done")
    total = frame.get("tasks_total")
    phase = _ascii_text(frame.get("phase_name", ""))
    status = _ascii_text(frame.get("status", ""))
    counts = f"{done}/{total}" if total is not None else "-/-"
    extra = ""
    for k in ("task_id", "reason"):
        if frame.get(k):
            extra += f"  {k}={_ascii_text(frame[k])!r}"
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
                    print(f"{_FAILURE_MARKER} reason=authentication")
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
            print("--> autonomous_start "
                  f"objective={_ascii_text(args.objective)!r} "
                  f"model={_ascii_text(args.model)!r}")
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
                print(f"{_FAILURE_MARKER} reason=timeout")
                return 4
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                print(f"\n[client] --timeout {args.timeout}s reached, exiting")
                print(f"{_FAILURE_MARKER} reason=timeout")
                return 4
            msg = json.loads(raw)
            if msg.get("type") == "autonomous_progress":
                print(f"<-- {_fmt_progress(msg)}")
                if msg.get("event") in _TERMINAL_EVENTS:
                    # For one-shot queries (status/interrupt/abort) and terminal
                    # run outcomes, one frame is enough — stop here.
                    if args.action != "start" or msg.get("event") != "running":
                        event = _ascii_text(msg.get("event"))
                        print(f"\n[client] terminal -> event '{event}' - done")
                        if args.action == "start" and event != "done":
                            print(f"{_FAILURE_MARKER} event={event}")
                            return 3
                        print(f"{_SUCCESS_MARKER} event={event}")
                        return 0
            else:
                # Surface any other frames (errors, etc.) without parsing them.
                msg_type = _ascii_text(msg.get("type"))
                rendered = _ascii_text(msg)[:120]
                print(f"<-- (other) type={msg_type!r} {rendered}")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_accepting(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _wait_for_isolated_server(proc: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            raise RuntimeError(f"isolated server exited before ready (code {code})")
        if _port_accepting(port):
            return
        time.sleep(0.05)
    raise RuntimeError("isolated server did not become ready")


def _stop_isolated_server(proc: subprocess.Popen, stop_file: Path, port: int) -> None:
    stop_file.write_text("stop\n", encoding="ascii")
    try:
        code = proc.wait(timeout=30.0)
    except subprocess.TimeoutExpired as exc:
        proc.terminate()
        proc.wait(timeout=10.0)
        raise RuntimeError("isolated server did not shut down cleanly") from exc
    if code != 0:
        raise RuntimeError(f"isolated server cleanup failed (code {code})")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _port_accepting(port):
            return
        time.sleep(0.05)
    raise RuntimeError("isolated server listener remained open")


def _run_isolated(args) -> int:
    """Run the live WebSocket scenario against test-only temporary state."""
    state_before = _fingerprint(_PRODUCTION_TASK_STATE)
    if _LEGACY_TEST_PROJECT.exists():
        raise RuntimeError("legacy test project already exists; refusing to overwrite it")

    result = 1
    with tempfile.TemporaryDirectory(prefix="osia-autonomous-diagnostic-") as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        token_file = root / "mobile_token.txt"
        token_file.write_text(secrets.token_hex(20), encoding="ascii")
        stop_file = root / "stop"
        port = _free_loopback_port()
        server_log = root / "server.log"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_isolated-server",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--_root", str(root),
            "--_token-file", str(token_file),
            "--_stop-file", str(stop_file),
        ]
        env = os.environ.copy()
        # The child writes only to a UTF-8 temporary log, never to the console.
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = None
        cleanup_error = None
        with server_log.open("w", encoding="utf-8") as log:
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=workspace,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
                _wait_for_isolated_server(proc, port)
                if args.test_inject_failure:
                    raise RuntimeError("intentional test-only failure after server startup")
                args.host = "127.0.0.1"
                args.port = port
                args.token = token_file.read_text(encoding="ascii")
                if args.objective is None:
                    args.objective = (
                        "Create a folder named osia_p5_test in the current working "
                        "directory and write a file hello.txt containing the word OK "
                        "inside it"
                    )
                result = asyncio.run(_run(args))
                if result == 0 and args.verify_default_fixture:
                    fixture = workspace / "osia_p5_test" / "hello.txt"
                    if not fixture.is_file() or fixture.read_text(
                            encoding="utf-8").strip() != "OK":
                        print(f"{_FAILURE_MARKER} reason=fixture_verification")
                        result = 5
            finally:
                if proc is not None:
                    try:
                        _stop_isolated_server(proc, stop_file, port)
                    except Exception as exc:
                        cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error

    if _fingerprint(_PRODUCTION_TASK_STATE) != state_before:
        raise RuntimeError("production autonomous task state changed")
    if _LEGACY_TEST_PROJECT.exists():
        raise RuntimeError("repository test project was created")
    return result


def _run_isolated_server(args) -> int:
    """Hidden child mode: host the production app with test-only path patches."""
    root = Path(args._root).resolve()
    token_file = Path(args._token_file).resolve()
    stop_file = Path(args._stop_file).resolve()
    workspace = root / "workspace"
    if not workspace.is_dir() or root not in token_file.parents or root not in stop_file.parents:
        return 2

    sys.path.insert(0, str(_REPO_ROOT))
    import uvicorn
    from osia import agent as agent_module
    from osia import conversations
    from osia.autonomous import state as autonomous_state
    from osia.server import server as server_module

    app = server_module.app
    startup_handlers = list(app.router.on_startup)
    originals = {
        "task_state": autonomous_state.TASK_STATE_FILE,
        "lock_file": server_module._AUTONOMOUS_LOCK_FILE,
        "config_dir": server_module.CONFIG_DIR,
        "token_file": server_module.TOKEN_FILE,
        "mobile_token": server_module.MOBILE_TOKEN,
        "upload_dir": server_module.UPLOAD_DIR,
        "conversations_dir": conversations.CONVERSATIONS_DIR,
        "project_dir": agent_module._PROJECT_DIR,
    }
    autonomous_state.TASK_STATE_FILE = root / "task_state_mobile.json"
    server_module._AUTONOMOUS_LOCK_FILE = root / "autonomous.lock"
    server_module.CONFIG_DIR = root / "config"
    server_module.TOKEN_FILE = token_file
    server_module.MOBILE_TOKEN = token_file.read_text(encoding="ascii").strip()
    server_module.UPLOAD_DIR = root / "uploads"
    conversations.CONVERSATIONS_DIR = root / "conversations"
    agent_module._PROJECT_DIR = workspace
    app.router.on_startup.clear()

    async def serve() -> int:
        config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
            access_log=False,
        )
        instance = uvicorn.Server(config)

        async def watch_stop_file() -> None:
            while not stop_file.exists():
                await asyncio.sleep(0.05)
            instance.should_exit = True

        watcher = asyncio.create_task(watch_stop_file())
        try:
            await instance.serve()
        finally:
            instance.should_exit = True
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        pending = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return 6
        return 0

    try:
        return asyncio.run(serve())
    finally:
        autonomous_state.TASK_STATE_FILE = originals["task_state"]
        server_module._AUTONOMOUS_LOCK_FILE = originals["lock_file"]
        server_module.CONFIG_DIR = originals["config_dir"]
        server_module.TOKEN_FILE = originals["token_file"]
        server_module.MOBILE_TOKEN = originals["mobile_token"]
        server_module.UPLOAD_DIR = originals["upload_dir"]
        conversations.CONVERSATIONS_DIR = originals["conversations_dir"]
        agent_module._PROJECT_DIR = originals["project_dir"]
        app.router.on_startup[:] = startup_handlers


def main() -> int:
    ap = argparse.ArgumentParser(description="Drive the OSIA autonomous server over WebSocket.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=None, help="mobile token (default: read mobile_token.txt)")
    ap.add_argument("--action", choices=["start", "interrupt", "abort", "status", "resume"],
                    default="start")
    ap.add_argument("--objective",
                    default=None,
                    help="objective for --action start (default: isolated file fixture)")
    ap.add_argument("--model", default="", help="model id for --action start "
                    "(empty = server falls back to the configured selected_model)")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="seconds to listen before exiting (default 600)")
    ap.add_argument("--live-server", action="store_true",
                    help="use the already-running server instead of isolated test state")
    ap.add_argument("--test-inject-failure", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--_isolated-server", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--_root", help=argparse.SUPPRESS)
    ap.add_argument("--_token-file", help=argparse.SUPPRESS)
    ap.add_argument("--_stop-file", help=argparse.SUPPRESS)
    args = ap.parse_args()
    args.verify_default_fixture = args.objective is None
    try:
        if args._isolated_server:
            return _run_isolated_server(args)
        if args.test_inject_failure and (args.live_server or args.action != "start"):
            raise RuntimeError("test failure injection requires isolated start mode")
        if args.action == "start" and not args.live_server:
            return _run_isolated(args)
        if args.action == "start" and args.objective is None:
            args.objective = (
                "Create a folder named osia_p5_test and write a file hello.txt "
                "containing the word OK inside it"
            )
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[client] interrupted by user (Ctrl-C)")
        return 130
    except Exception as exc:
        reason = _ascii_text(type(exc).__name__)
        print(f"{_FAILURE_MARKER} reason={reason}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
