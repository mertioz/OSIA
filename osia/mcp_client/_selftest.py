#!/usr/bin/env python3
"""Standalone self-test for the OSIA MCP client core (WOR-44 Phase 1).

NOT wired into the app. Run it directly from the repo root:

    # Linux / Mac (venv active, `mcp` installed):
    python -m osia.mcp._selftest

    # Windows (venv active):
    python -m osia.mcp._selftest

Two modes, auto-selected:

  * REAL mode (preferred) — used when a Node toolchain is on PATH
    (``npx`` on Linux/Mac, ``npx.cmd`` on Windows). It launches the reference
    Filesystem MCP server over stdio against a throwaway temp dir, lists its
    tools, reads a file through a tool, forces a tool error, then disconnects
    and shuts the manager down. The exact server command:

        Linux/Mac:  npx     -y @modelcontextprotocol/server-filesystem <tmpdir>
        Windows:    npx.cmd -y @modelcontextprotocol/server-filesystem <tmpdir>

    The FIRST run may download the npm package (a one-time network fetch);
    subsequent runs are offline.

  * MOCK mode (fallback) — used when no Node toolchain is found. It exercises
    the loop-thread bridge and the CallToolResult→str / ERROR conversion
    against an in-memory fake session, proving the bridge without any real
    server or network.

Exit code: 0 on success, 1 if a check fails.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

# Allow running both as a module (`python -m osia.mcp._selftest`) and as a
# script from arbitrary CWDs.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from osia.mcp.manager import (  # noqa: E402
    MCPManager,
    _ServerConn,
    _calltoolresult_to_text,
    STATUS_CONNECTED,
)


# ── Fakes for MOCK mode ──────────────────────────────────────────────────────

class _FakeText:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeImage:
    type = "image"
    mimeType = "image/png"
    data = "QUJD"  # base64 "ABC"


class _FakeResult:
    def __init__(self, content, is_error=False, structured=None):
        self.content = content
        self.isError = is_error
        self.structuredContent = structured


class _FakeSession:
    """Stands in for an mcp.ClientSession. Its async ``call_tool`` is driven on
    the manager's dedicated loop via run_coroutine_threadsafe, exactly like a
    real session."""

    async def call_tool(self, name, arguments):
        if name == "boom":
            raise RuntimeError("simulated transport failure")
        if name == "slow":
            await asyncio.sleep(5)
            return _FakeResult([_FakeText("done late")])
        if name == "err_tool":
            return _FakeResult([_FakeText("bad arguments")], is_error=True)
        if name == "image_tool":
            return _FakeResult([_FakeImage()])
        return _FakeResult([_FakeText(f"echo: {sorted(arguments.items())}")])


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def run_mock() -> bool:
    print("== MOCK mode ==")
    ok = True

    # 1) Pure conversion checks (no loop needed).
    ok &= _check(
        "text result → text",
        _calltoolresult_to_text(_FakeResult([_FakeText("hi")]), "s", "t") == "hi",
    )
    ev = _calltoolresult_to_text(_FakeResult([_FakeText("oops")], is_error=True), "s", "t")
    ok &= _check("isError → ERROR: string", ev.startswith("ERROR: mcp/s/t:"), ev)
    empty = _calltoolresult_to_text(_FakeResult([]), "s", "t")
    ok &= _check("empty success → (no output)", empty == "(no output)", empty)
    img = _calltoolresult_to_text(_FakeResult([_FakeImage()]), "s", "t")
    ok &= _check("image block → placeholder", img.startswith("[image content:"), img)
    structured = _calltoolresult_to_text(
        _FakeResult([], structured={"k": "v"}), "s", "t"
    )
    ok &= _check("structuredContent fallback", '"k": "v"' in structured, structured)

    # 2) The loop-thread ↔ sync-call bridge with a fake connected server.
    mgr = MCPManager()
    try:
        mgr._ensure_loop()
        conn = _ServerConn(server_id="fake", cfg={"transport": "stdio"})
        conn.status = STATUS_CONNECTED
        conn.session = _FakeSession()
        conn.tools = [{"name": "echo", "description": "echo", "inputSchema": {}}]
        with mgr._lock:
            mgr._servers["fake"] = conn

        r_echo = mgr.call_tool("fake", "echo", {"x": 1})
        ok &= _check("bridge: success returns string", r_echo == "echo: [('x', 1)]", r_echo)

        r_err = mgr.call_tool("fake", "err_tool", {})
        ok &= _check("bridge: isError → ERROR:", r_err.startswith("ERROR: mcp/fake/err_tool:"), r_err)

        r_boom = mgr.call_tool("fake", "boom", {})
        ok &= _check("bridge: exception → ERROR:", r_boom.startswith("ERROR: mcp/fake/boom:"), r_boom)

        r_slow = mgr.call_tool("fake", "slow", {}, timeout=1.0)
        ok &= _check("bridge: timeout → ERROR:", "timed out" in r_slow, r_slow)

        r_missing = mgr.call_tool("ghost", "x", {})
        ok &= _check("unregistered server → ERROR:", r_missing.startswith("ERROR: mcp/ghost/x:"), r_missing)

        r_img = mgr.call_tool("fake", "image_tool", {})
        ok &= _check("bridge: image → placeholder", r_img.startswith("[image content:"), r_img)
    finally:
        mgr.shutdown()

    # 3) Clean shutdown: loop thread is gone, no dangling thread.
    ok &= _check("shutdown clears loop", mgr._loop is None)
    ok &= _check(
        "shutdown joins thread",
        mgr._thread is None or not mgr._thread.is_alive(),
    )

    # 4) call_tool after shutdown still returns an ERROR: string, never raises.
    r_after = mgr.call_tool("fake", "echo", {})
    ok &= _check("call after shutdown → ERROR:", r_after.startswith("ERROR: mcp/fake/echo:"), r_after)

    return ok


def run_real() -> bool:
    cmd = "npx.cmd" if sys.platform == "win32" else "npx"
    print(f"== REAL mode (using `{cmd}`) ==")
    ok = True

    tmpdir = tempfile.mkdtemp(prefix="osia-mcp-selftest-")
    sample = os.path.join(tmpdir, "hello.txt")
    with open(sample, "w", encoding="utf-8") as f:
        f.write("Hello from the OSIA MCP self-test.\n")

    mgr = MCPManager()
    try:
        cfg = {
            "id": "fs",
            "name": "Filesystem",
            "transport": "stdio",
            "command": cmd,
            "args": ["-y", "@modelcontextprotocol/server-filesystem", tmpdir],
            "env": None,
            "auth_type": "none",
            "enabled": True,
        }
        # Generous timeout: first run may download the npm package.
        status = mgr.connect(cfg, connect_timeout=120.0)
        print(f"  connect status: {status}  error={mgr.error('fs')!r}")
        ok &= _check("connected", status == STATUS_CONNECTED)
        if status != STATUS_CONNECTED:
            return ok  # nothing else to exercise

        tools = mgr.list_tools("fs")
        names = {t["name"] for t in tools}
        print(f"  tools ({len(tools)}): {sorted(names)}")
        ok &= _check("discovered tools", len(tools) > 0)

        if "list_directory" in names:
            out = mgr.call_tool("fs", "list_directory", {"path": tmpdir})
            print(f"  list_directory → {out!r}")
            ok &= _check("list_directory ok", not out.startswith("ERROR:"))

        read_name = (
            "read_text_file" if "read_text_file" in names
            else ("read_file" if "read_file" in names else None)
        )
        if read_name:
            out = mgr.call_tool("fs", read_name, {"path": sample})
            print(f"  {read_name} → {out!r}")
            ok &= _check("read file ok", "Hello from the OSIA MCP self-test." in out)

            forced = mgr.call_tool(
                "fs", read_name, {"path": os.path.join(tmpdir, "does-not-exist.txt")}
            )
            print(f"  forced-error read → {forced!r}")
            ok &= _check("missing file → ERROR:", forced.startswith("ERROR: mcp/fs/"), forced)
        else:
            print("  (no read_file/read_text_file tool exposed — skipping read checks)")

        bad = mgr.call_tool("fs", "this_tool_does_not_exist", {})
        print(f"  unknown tool → {bad!r}")
        ok &= _check("unknown tool → ERROR:", bad.startswith("ERROR: mcp/fs/"), bad)

        st = mgr.disconnect("fs")
        print(f"  disconnect status: {st}")
    finally:
        mgr.shutdown()
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    ok &= _check("shutdown clears loop", mgr._loop is None)
    return ok


def main() -> int:
    has_node = bool(shutil.which("npx.cmd") or shutil.which("npx"))
    if has_node:
        try:
            ok = run_real()
        except Exception as e:  # noqa: BLE001
            print(f"  real-mode error ({type(e).__name__}: {e}); falling back to MOCK")
            ok = run_mock()
    else:
        print("(no npx/node toolchain on PATH)")
        ok = run_mock()

    print(f"\nself-test {'PASSED' if ok else 'FAILED'}.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
