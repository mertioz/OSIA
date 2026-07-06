"""Outbound OSIA public relay connector.

This process-side bridge keeps the existing local mobile server protocol intact:
it authenticates to ``ws://127.0.0.1:8765/ws`` as a local client, then forwards
JSON frames through the public relay. The relay never receives the raw mobile
token; PC and mobile authenticate to the relay with a token-derived proof.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from osia.config import CONFIG_DIR, load_mobile_token

DEFAULT_RELAY_URL = "wss://relay.osia.space/ws"
LOCAL_WS_URL = "ws://127.0.0.1:8765/ws"
PROTOCOL_VERSION = 1
MAX_RELAY_FRAME_BYTES = 64 * 1024

_DEVICE_FILE = Path(CONFIG_DIR) / "relay_device.json"
_started = False
_started_lock = threading.Lock()


def relay_pair_credentials(token: str) -> tuple[str, str]:
    """Return ``(pair_id, secret)`` from the mobile token without exposing it."""
    secret = hmac.new(
        token.encode("utf-8"),
        b"osia-relay-secret-v1",
        hashlib.sha256,
    ).hexdigest()
    pair_id = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return pair_id, secret


def _relay_enabled() -> bool:
    raw = os.environ.get("OSIA_RELAY_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _relay_url() -> str:
    return os.environ.get("OSIA_RELAY_URL", DEFAULT_RELAY_URL).strip() or DEFAULT_RELAY_URL


def _load_device_id() -> str:
    try:
        data = json.loads(_DEVICE_FILE.read_text(encoding="utf-8"))
        value = str(data.get("device_id") or "").strip()
        if value:
            return value
    except Exception:
        pass

    device_id = "pc-" + secrets.token_hex(16)
    try:
        _DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DEVICE_FILE.write_text(
            json.dumps({"device_id": device_id}, separators=(",", ":")),
            encoding="utf-8",
        )
        if os.name != "nt":
            try:
                _DEVICE_FILE.chmod(0o600)
            except Exception:
                pass
    except Exception as exc:
        print(f"[OSIA Relay] device id persistence skipped: {exc}")
    return device_id


def _session_id() -> str:
    return "pc-" + secrets.token_hex(16)


def _safe_frame_type(frame) -> str:
    if isinstance(frame, dict):
        return str(frame.get("type") or "")
    return ""


class RelayBridge:
    def __init__(self, token: str, relay_url: str) -> None:
        self.token = token
        self.relay_url = relay_url
        self.pair_id, self.secret = relay_pair_credentials(token)
        self.device_id = _load_device_id()
        self.session_id = _session_id()
        self.seq = 0
        self._relay_send_lock = asyncio.Lock()
        self._local_ws = None
        self._local_reader_task: asyncio.Task | None = None

    def _relay_endpoint(self) -> str:
        query = urlencode({
            "role": "pc",
            "pair_id": self.pair_id,
            "device_id": self.device_id,
            "session_id": self.session_id,
        })
        sep = "&" if "?" in self.relay_url else "?"
        return f"{self.relay_url}{sep}{query}"

    def _envelope(self, msg_type: str, payload: dict | None = None) -> dict:
        self.seq += 1
        return {
            "v": PROTOCOL_VERSION,
            "type": msg_type,
            "role": "pc",
            "pair_id": self.pair_id,
            "device_id": self.device_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "ack": 0,
            "payload": payload or {},
        }

    async def _send_relay(self, relay_ws, msg_type: str, payload: dict | None = None) -> None:
        encoded = json.dumps(
            self._envelope(msg_type, payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        async with self._relay_send_lock:
            await relay_ws.send(encoded)

    async def _connect_local(self, relay_ws) -> None:
        if self._local_ws is not None:
            return
        import websockets

        local_ws = await websockets.connect(
            LOCAL_WS_URL,
            max_size=MAX_RELAY_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
        )
        await local_ws.send(json.dumps({"type": "auth", "token": self.token}))
        self._local_ws = local_ws
        self._local_reader_task = asyncio.create_task(
            self._local_to_relay(local_ws, relay_ws)
        )
        print("[OSIA Relay] local OSIA websocket connected")

    async def _close_local(self) -> None:
        task = self._local_reader_task
        self._local_reader_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        local_ws = self._local_ws
        self._local_ws = None
        if local_ws is not None:
            try:
                await local_ws.close()
            except Exception:
                pass

    async def _local_to_relay(self, local_ws, relay_ws) -> None:
        try:
            async for raw in local_ws:
                if not isinstance(raw, str):
                    continue
                if len(raw) > MAX_RELAY_FRAME_BYTES:
                    print("[OSIA Relay] dropped oversized local frame")
                    continue
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._send_relay(relay_ws, "FRAME", {"frame": frame})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[OSIA Relay] local reader stopped: {exc}")
        finally:
            if self._local_ws is local_ws:
                self._local_ws = None

    async def _handle_relay_frame(self, relay_ws, msg: dict) -> None:
        msg_type = str(msg.get("type") or "")
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}

        if msg_type == "PAIR_CONFIRM":
            print("[OSIA Relay] relay pair confirmed")
            return
        if msg_type == "MOBILE_READY":
            await self._connect_local(relay_ws)
            await self._send_relay(relay_ws, "PC_READY", {})
            return
        if msg_type == "PING":
            await self._send_relay(relay_ws, "PONG", {"t": int(time.time() * 1000)})
            return
        if msg_type == "DISCONNECT":
            await self._close_local()
            return
        if msg_type == "ERROR":
            code = str(payload.get("code") or "")
            if code and code != "peer_unavailable":
                print(f"[OSIA Relay] relay error: {code}")
            return
        if msg_type == "BATCH":
            frames = payload.get("frames")
            if isinstance(frames, list):
                for frame in frames:
                    await self._forward_mobile_frame(frame)
            return
        if msg_type == "FRAME":
            await self._forward_mobile_frame(payload.get("frame"))

    async def _forward_mobile_frame(self, frame) -> None:
        if not isinstance(frame, dict):
            return
        if _safe_frame_type(frame) == "auth":
            return
        local_ws = self._local_ws
        if local_ws is None:
            return
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > MAX_RELAY_FRAME_BYTES:
            print("[OSIA Relay] dropped oversized mobile frame")
            return
        await local_ws.send(encoded)

    async def run_once(self) -> None:
        import websockets

        endpoint = self._relay_endpoint()
        async with websockets.connect(
            endpoint,
            max_size=MAX_RELAY_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
        ) as relay_ws:
            print("[OSIA Relay] connected to relay")
            await self._send_relay(relay_ws, "PAIR_START", {"secret": self.secret})
            await self._send_relay(relay_ws, "PC_READY", {})
            async for raw in relay_ws:
                if not isinstance(raw, str):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._handle_relay_frame(relay_ws, msg)

    async def run_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                await self.run_once()
                delay = 1.0
            except Exception as exc:
                print(f"[OSIA Relay] connector retry after error: {exc}")
                await self._close_local()
                await asyncio.sleep(delay)
                delay = min(delay * 1.7, 30.0)


def _thread_main() -> None:
    while _relay_enabled():
        token = load_mobile_token()
        if not token:
            print("[OSIA Relay] no mobile token yet; waiting")
            time.sleep(5)
            continue
        try:
            asyncio.run(RelayBridge(token, _relay_url()).run_forever())
        except Exception as exc:
            print(f"[OSIA Relay] connector loop crashed: {exc}")
            time.sleep(5)


def start_background_connector() -> None:
    """Start the relay connector once, if enabled. Best-effort and non-blocking."""
    global _started
    if not _relay_enabled():
        return
    with _started_lock:
        if _started:
            return
        _started = True
    threading.Thread(
        target=_thread_main,
        name="osia-relay-connector",
        daemon=True,
    ).start()
