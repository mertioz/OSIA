"""Local temporary-profile integration coverage for the WOR-166 owner."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from osia.tools import browser


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/popup":
            body = b"<!doctype html><title>Popup</title><body>popup-ready</body>"
        else:
            body = b"""<!doctype html>
<title>Opener</title>
<button id="open" style="position:absolute;left:100px;top:100px;width:200px;height:60px">
Open popup
</button>
<script>
document.querySelector('#open').addEventListener('click', () => {
  const popup = window.open('about:blank', 'wor166-popup', 'width=500,height=500');
  setTimeout(() => { popup.location.href = '/popup'; }, 700);
});
</script>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_snapshot(owner, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = owner.snapshot()
        if predicate(value):
            return value
        time.sleep(0.02)
    raise AssertionError(f"owner snapshot condition timed out: {owner.snapshot()}")


def _external_cdp_click(port):
    script = r"""
const port = Number(process.argv[1]);
const targets = await fetch(`http://127.0.0.1:${port}/json`).then(r => r.json());
const target = targets.find(item => item.type === 'page' && item.url.includes('/opener'));
if (!target) throw new Error('loopback opener target not found');
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, {once: true});
  socket.addEventListener('error', reject, {once: true});
});
let next = 1;
const pending = new Map();
socket.addEventListener('message', event => {
  const message = JSON.parse(String(event.data));
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  }
});
function send(method, params) {
  return new Promise((resolve, reject) => {
    const id = next++;
    pending.set(id, message => message.error ? reject(new Error(method)) : resolve());
    socket.send(JSON.stringify({id, method, params}));
  });
}
await send('Input.dispatchMouseEvent', {type:'mouseMoved', x:150, y:130});
await send('Input.dispatchMouseEvent', {type:'mousePressed', x:150, y:130, button:'left', clickCount:1});
await send('Input.dispatchMouseEvent', {type:'mouseReleased', x:150, y:130, button:'left', clickCount:1});
socket.close();
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", script, str(port)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_temporary_brave_popup_dispatches_while_osia_is_idle(monkeypatch, tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node runtime unavailable for external public-CDP input")
    binary = browser.resolve_browser_name("brave")
    if not binary:
        pytest.skip("Brave is not installed")

    http_port = _free_port()
    cdp_port = _free_port()
    profile = tmp_path / "brave-profile"
    server = ThreadingHTTPServer(("127.0.0.1", http_port), _Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    owner = browser._AsyncBrowserOwner()
    monkeypatch.setattr(browser, "_owner", owner)
    monkeypatch.setattr(browser, "CDP_PORT", cdp_port)
    monkeypatch.setattr(browser, "CDP_URL", f"http://127.0.0.1:{cdp_port}")
    monkeypatch.setattr(browser, "OSIA_PROFILE_DIR", str(profile))
    monkeypatch.setattr(browser, "_detect_chromium_browser", lambda: binary)
    monkeypatch.setattr(browser, "_raise_browser_window", lambda _kind=None: None)
    opener_url = f"http://127.0.0.1:{http_port}/opener"
    popup_url = f"http://127.0.0.1:{http_port}/popup"
    try:
        result = browser.navigate(opener_url)
        assert isinstance(result, dict), result
        assert result["text"].startswith(f"Navigated to: {opener_url}")

        # This input comes from a separate public CDP client, equivalent to a
        # physical click. No browser wrapper is called until both event-driven
        # owner assertions below have succeeded.
        _external_cdp_click(cdp_port)
        blank = _wait_snapshot(
            owner,
            lambda value: value["registryCount"] == 2
            and value["activeUrl"] == "about:blank",
        )
        assert blank["threadAlive"]
        navigated = _wait_snapshot(
            owner, lambda value: value["activeUrl"] == popup_url
        )
        assert navigated["registryCount"] == 2

        assert browser.get_current_url() == f"Current URL: {popup_url}"
        assert browser.read_page() == "Page content:\npopup-ready"
    finally:
        browser.close_browser()
        leftovers = browser._processes_using_profile(str(profile))
        if leftovers:
            browser._terminate_processes(leftovers)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    assert owner.snapshot()["state"] == "STOPPED"
    assert not owner.snapshot()["threadAlive"]
    assert not browser._processes_using_profile(str(profile))
