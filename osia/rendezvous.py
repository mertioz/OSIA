#!/usr/bin/env python3
"""WOR-64 — Cloudflare quick-tunnel URL publisher (rendezvous).

OSIA's mobile server is exposed through a Cloudflare *quick* tunnel whose public
``https://<random>.trycloudflare.com`` hostname changes every time the tunnel
restarts. So the phone can find the PC without the user copy-pasting a fresh URL
each time, a tiny daemon thread tails cloudflared's logfile, extracts the current
trycloudflare URL, and publishes ``{proof, url}`` to the rendezvous Worker. The
phone later asks the Worker for the URL keyed by the same shared secret.

The published ``proof`` is ``HMAC_SHA256(key=token, msg="osia-rdv-v1")`` (see
:func:`osia.config.rendezvous_proof`); the Worker derives the lookup key from it
itself, so the raw pairing token never leaves the machine — the PC sends only
``proof`` + ``url``.

Cross-platform and stdlib-only (``urllib``) so it can be reused by the Linux
tunnel later. EVERYTHING here is strictly **best-effort**: a missing token, an
unreadable/malformed log, or an unreachable Worker is a silent no-op that never
raises and never blocks the caller. The publisher runs on a ``daemon=True``
thread, so it can never hold up process exit, and it touches NONE of the
server-side WOR-47 watchdog state — the tunnel and the server are separate
processes, and this only ever runs in the tunnel process.
"""
import os
import re
import json
import time
import threading
import urllib.request

from osia.config import load_mobile_token, rendezvous_proof

# Default rendezvous Worker (POST /publish accepts JSON {proof, url}).
WORKER_URL = "https://osia-rendezvous.aymengherbi06.workers.dev"

# A trycloudflare quick-tunnel hostname as printed in cloudflared's logfile, e.g.
# ``https://random-words-1234.trycloudflare.com``.
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_POLL_SEC = 1.0          # how often we re-scan the appended log bytes
_KEEPALIVE_SEC = 300.0   # re-POST the (unchanged) URL this often. The Worker's
                         # entry TTL is 600s, so a 300s keepalive refreshes it
                         # with margin (~288 writes/day, well under the 1000/day
                         # free quota); genuine URL changes POST immediately.
_POST_TIMEOUT = 10.0

# Guard so start_publisher() only ever launches one daemon thread even if called
# more than once.
_started = False
_started_lock = threading.Lock()


def _publish(worker_url: str, proof: str, url: str) -> None:
    """POST ``{proof, url}`` to the Worker's ``/publish`` endpoint via stdlib."""
    data = json.dumps({"proof": proof, "url": url}).encode("utf-8")
    req = urllib.request.Request(
        worker_url.rstrip("/") + "/publish",
        data=data,
        # A User-Agent is required: Cloudflare's edge bot-management 403s the
        # default ``Python-urllib`` client before the request ever reaches the
        # Worker. A clean custom UA is enough to pass (verified live).
        headers={
            "Content-Type": "application/json",
            "User-Agent": "osia-rendezvous/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=_POST_TIMEOUT) as resp:
        resp.read()  # drain the body so the connection closes cleanly


def _run(logfile_path: str, worker_url: str) -> None:
    """Tail ``logfile_path``, publishing each new trycloudflare URL + keepalive.

    Offset tracking: remember how many bytes of the log we have already scanned
    and only read what has been *appended* since (opened in binary so the offset
    is an unambiguous byte count). If the file has shrunk — cloudflared truncated
    or rotated it — reset the offset to 0 and rescan from the top. Only consume
    up to the last complete line so a URL split across two reads is picked up on
    the next tick instead of being skipped. The whole body is wrapped so any
    error (missing file, decode glitch, network failure) just sleeps and retries
    on the next tick rather than killing the daemon.
    """
    offset = 0
    last_published_url = None
    last_post_ts = 0.0
    while True:
        try:
            try:
                size = os.path.getsize(logfile_path)
            except OSError:
                size = 0
            if size < offset:        # truncated/rotated → rescan from the start
                offset = 0
            url = None
            if size > offset:
                with open(logfile_path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                # Consume only up to the last newline; keep any trailing partial
                # line for the next read so a URL written across two ticks isn't
                # split and missed.
                nl = chunk.rfind(b"\n")
                if nl != -1:
                    consumed = chunk[:nl + 1]
                    offset += len(consumed)
                    matches = _URL_RE.findall(
                        consumed.decode("utf-8", errors="replace"))
                    if matches:
                        url = matches[-1]  # most-recent URL in the new bytes wins
            # Decide whether to POST: on a genuine URL change, always; otherwise
            # only every _KEEPALIVE_SEC to refresh the Worker's TTL. Gating the
            # non-keepalive POSTs on an actual change keeps us under the quota.
            target = url or last_published_url
            if target:
                now = time.time()
                changed = url is not None and url != last_published_url
                keepalive_due = (now - last_post_ts) >= _KEEPALIVE_SEC
                if changed or keepalive_due:
                    token = load_mobile_token()
                    if token:
                        _publish(worker_url, rendezvous_proof(token), target)
                        last_published_url = target
                        last_post_ts = now
        except Exception:
            # Best-effort: never let a bad tick kill the daemon.
            pass
        time.sleep(_POLL_SEC)


def start_publisher(logfile_path, worker_url: str = WORKER_URL) -> None:
    """Start the single rendezvous publisher daemon thread (idempotent).

    Spawns one ``daemon=True`` thread that tails ``logfile_path`` (cloudflared's
    quick-tunnel logfile) and publishes ``{proof, url}`` to ``worker_url`` on
    every URL change plus a periodic keepalive. Best-effort and non-blocking:
    returns immediately, swallows any error, and never raises — a publisher that
    fails to start must never affect the tunnel supervisor.
    """
    global _started
    try:
        with _started_lock:
            if _started:
                return
            _started = True
        threading.Thread(
            target=_run,
            args=(str(logfile_path), worker_url),
            name="osia-rendezvous-publisher",
            daemon=True,
        ).start()
    except Exception:
        pass
