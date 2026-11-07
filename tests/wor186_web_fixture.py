"""Reusable private loopback Web fixture for deterministic browser tests.

The servers bind only to 127.0.0.1 on ephemeral ports and own no user data.
All mutable state is bounded and deleted when the context closes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


DOWNLOAD_BYTES = b"OSIA WOR-186 synthetic download\n"
PRIVATE_SENTINEL = "WOR186_PRIVATE_SENTINEL_DO_NOT_LEAK"


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class Wor186WebFixture:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="osia-wor186-fixture-"))
        self.upload_file = self.root / "synthetic-upload.txt"
        self.upload_file.write_text("benign synthetic upload\n", encoding="utf-8")
        self._lock = threading.Lock()
        self._state = {
            "duplicate_count": 0,
            "upload_posts": 0,
            "requests": 0,
        }
        self._main = _LoopbackServer(("127.0.0.1", 0), self._handler(False))
        self._cross = _LoopbackServer(("127.0.0.1", 0), self._handler(True))
        self.base = f"http://127.0.0.1:{self._main.server_port}"
        self.cross_base = f"http://127.0.0.1:{self._cross.server_port}"
        self._threads = [
            threading.Thread(target=self._main.serve_forever, daemon=True),
            threading.Thread(target=self._cross.serve_forever, daemon=True),
        ]

    def __enter__(self):
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *_args):
        for server in (self._main, self._cross):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def duplicate_count(self) -> int:
        with self._lock:
            return int(self._state["duplicate_count"])

    @property
    def download_sha256(self) -> str:
        return hashlib.sha256(DOWNLOAD_BYTES).hexdigest()

    def _increment(self, key: str) -> int:
        with self._lock:
            self._state[key] = min(int(self._state.get(key, 0)) + 1, 10_000)
            return int(self._state[key])

    def _handler(self, cross_origin: bool):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def _send(self, body, *, status=HTTPStatus.OK, content_type="text/html; charset=utf-8", headers=None):
                encoded = body.encode("utf-8") if isinstance(body, str) else bytes(body)
                self.send_response(int(status))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self):
                parsed = urlsplit(self.path)
                if parsed.path == "/api/duplicate":
                    count = fixture._increment("duplicate_count")
                    self._send(
                        json.dumps({"count": count}),
                        content_type="application/json",
                    )
                    return
                if parsed.path == "/upload":
                    fixture._increment("upload_posts")
                    self._send("uploaded")
                    return
                self._send("not found", status=HTTPStatus.NOT_FOUND)

            def do_HEAD(self):
                parsed = urlsplit(self.path)
                if parsed.path not in {"/download", "/oversized"}:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                size = (
                    len(DOWNLOAD_BYTES)
                    if parsed.path == "/download"
                    else 2 * 1024 * 1024 * 1024 + 1
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", "attachment; filename=synthetic.bin")
                self.end_headers()

            def do_GET(self):
                fixture._increment("requests")
                parsed = urlsplit(self.path)
                path = parsed.path
                if cross_origin:
                    if path == "/frame-cross":
                        self._send("<main><h2>Cross-origin frame</h2></main>")
                    else:
                        self._send("not found", status=HTTPStatus.NOT_FOUND)
                    return
                if path == "/api/state":
                    with fixture._lock:
                        payload = dict(fixture._state)
                    self._send(json.dumps(payload), content_type="application/json")
                    return
                if path == "/search":
                    query = " ".join(parse_qs(parsed.query).get("q", [""])).strip()[:200]
                    self._send(f"""<!doctype html><html lang="en"><head><title>Controlled search</title></head>
                    <body><main><h1>Results</h1><p>{query}</p>
                    <a class="result" href="{fixture.base}/static"><h2>Static result</h2></a>
                    <p>Safe controlled summary</p></main></body></html>""")
                    return
                if path == "/static":
                    self._send("""<!doctype html><html lang="en"><head><title>Static fixture</title>
                    <meta name="description" content="Controlled metadata"><link rel="canonical" href="/static?ignored=1#fragment"></head>
                    <body><main><h1>Fixture heading</h1><p>Static controlled content.</p>
                    <a href="/extract?secret=QUERY_SENTINEL#frag">Extraction page</a></main></body></html>""")
                    return
                if path == "/spa":
                    self._send("""<!doctype html><title>SPA fixture</title><main><h1>SPA</h1><div id="dynamic">Waiting</div></main>
                    <script>setTimeout(()=>document.querySelector('#dynamic').textContent='Delayed dynamic content ready',300)</script>""")
                    return
                if path == "/extract":
                    self._send("""<!doctype html><html lang="fr"><head><title>Extraction fixture</title>
                    <meta name="description" content="Bounded extraction"><link rel="canonical" href="/extract"></head><body>
                    <main><h1>Primary heading</h1><article><h2>Article heading</h2><p>Article body.</p></article>
                    <a href="/static">Safe link</a><table><caption>Metrics</caption><tr><th>Name</th><th>Value</th></tr><tr><td>Alpha</td><td>1</td></tr></table>
                    <ol><li>First item</li><li>Second item</li></ol></main></body></html>""")
                    return
                if path == "/audit-browser":
                    self._send("""<!doctype html><title>Browser audit</title>
                    <main><table><tr><td>First table</td></tr></table>
                    <table><caption>Exact metrics</caption><tr><td>Second table</td></tr>
                    <tr><td>Extra row</td></tr></table><ul><li>Exact list</li></ul>
                    <div aria-label="Nested panel" style="height:100px;overflow-y:auto">
                      <p style="height:650px">Scrollable panel content</p>
                    </div>
                    <button id="replace" onclick="const previous=document.querySelector('#action');
                      const next=previous.cloneNode(true);
                      next.onclick=()=>document.querySelector('#status').textContent='Replacement clicked';
                      previous.replaceWith(next);
                      document.querySelector('#status').textContent='Replacement ready'">Replace action</button>
                    <button id="action" onclick="document.querySelector('#status').textContent='Original clicked'">Run action</button>
                    <p id="status">Ready</p></main>""")
                    return
                if path == "/menu":
                    self._send("""<!doctype html><title>Menu</title><button id="menu" aria-expanded="false"
                    onclick="this.setAttribute('aria-expanded','true');document.querySelector('#items').hidden=false">Menu</button>
                    <ul id="items" hidden><li>Choice one</li></ul><label>Native<select><option>One</option><option>Two</option></select></label>""")
                    return
                if path == "/page/1":
                    self._send('<main><h1>Page 1</h1><a rel="next" href="/page/2">Next page</a></main>')
                    return
                if path == "/page/2":
                    self._send("<main><h1>Page 2</h1><p>Pagination complete</p></main>")
                    return
                if path == "/infinite":
                    self._send("""<!doctype html><main id="items"><p>Item 1</p></main><script>
                    let n=1;addEventListener('scroll',()=>{if(n<3){n++;let p=document.createElement('p');p.textContent='Item '+n;items.appendChild(p)}})</script>""")
                    return
                if path == "/form":
                    self._send("""<!doctype html><form aria-label="Synthetic form"><label for="name">Name</label>
                    <input id="name" name="name"><label for="file">File</label><input id="file" name="file" type="file">
                    <button type="submit">Submit</button></form>""")
                    return
                if path == "/frame":
                    self._send('<main><h2>Same-origin frame</h2><button id="frame-button">Frame action</button></main>')
                    return
                if path == "/frames":
                    self._send(f"""<!doctype html><title>Frames</title><iframe src="/frame"></iframe>
                    <iframe src="{fixture.cross_base}/frame-cross"></iframe>""")
                    return
                if path == "/frame-transition":
                    self._send(f"""<!doctype html><title>Frame transition</title>
                    <iframe src="/frame"></iframe>
                    <button id="switch-frame"
                      onclick="document.querySelector('iframe').src='{fixture.cross_base}/frame-cross';this.textContent='Frame changed'">
                      Change frame
                    </button>""")
                    return
                if path == "/popup":
                    self._send("<main><h1>Popup content</h1></main>")
                    return
                if path == "/popup-delay":
                    self._send("""<!doctype html><button id="popup" onclick="setTimeout(()=>window.open('/popup'),300)">Open delayed popup</button>""")
                    return
                if path == "/dialogs":
                    self._send("""<!doctype html><button id="alert" onclick="alert('private dialog body')">Alert</button>
                    <button id="confirm" onclick="confirm('private confirm body')">Confirm</button>
                    <button id="prompt" onclick="prompt('private prompt body')">Prompt</button>""")
                    return
                if path == "/consent":
                    self._send("""<!doctype html><dialog open aria-label="Consent"><p>Local consent</p>
                    <button onclick="this.closest('dialog').close()">Accept</button></dialog>""")
                    return
                if path in {"/history/a", "/history/b"}:
                    other = "/history/b" if path.endswith("a") else "/history/a"
                    self._send(f'<main><h1>{path[-1].upper()}</h1><a href="{other}">Other history page</a></main>')
                    return
                if path == "/delayed-nav":
                    self._send("<script>setTimeout(()=>location.href='/static',300)</script><p>Delay</p>")
                    return
                if path == "/delayed-content":
                    self._send('<main id="late">Initial</main><script>setTimeout(()=>late.textContent="Late content ready",400)</script>')
                    return
                if path in {"/duplicate", "/ambiguous"}:
                    mutate = "this.textContent='Counted once'" if path == "/duplicate" else "void 0"
                    self._send(f"""<!doctype html><button id="once" onclick="fetch('/api/duplicate',{{method:'POST'}});{mutate}">One action</button>""")
                    return
                if path == "/download":
                    self._send(
                        DOWNLOAD_BYTES,
                        content_type="application/octet-stream",
                        headers={"Content-Disposition": "attachment; filename=synthetic.txt"},
                    )
                    return
                if path == "/download-page":
                    self._send('<a id="download" href="/download" download>Download</a>'
                               '<button type="button">No download</button>')
                    return
                if path == "/download-delay-page":
                    self._send('<a id="download" href="/download-delay" download>Delayed download</a>')
                    return
                if path == "/oversized-page":
                    self._send('<a id="download" href="/oversized" download>Oversized download</a>')
                    return
                if path == "/download-delay":
                    time.sleep(20)
                    self._send(DOWNLOAD_BYTES, content_type="application/octet-stream")
                    return
                if path == "/oversized":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(2 * 1024 * 1024 * 1024 + 1))
                    self.send_header("Content-Disposition", "attachment; filename=oversized.bin")
                    self.end_headers()
                    self.wfile.write(b"x")
                    return
                if path == "/login":
                    self._send('<form><input type="password" name="password"><button>Login</button></form>')
                    return
                if path == "/captcha":
                    self._send('<main><div id="captcha-box">Manual CAPTCHA required</div></main>')
                    return
                if path == "/2fa":
                    self._send('<form><input autocomplete="one-time-code" name="otp"></form>')
                    return
                if path == "/private":
                    self._send(f"<main>{PRIVATE_SENTINEL}</main>")
                    return
                self._send("not found", status=HTTPStatus.NOT_FOUND)

        return Handler


__all__ = ["DOWNLOAD_BYTES", "PRIVATE_SENTINEL", "Wor186WebFixture"]
