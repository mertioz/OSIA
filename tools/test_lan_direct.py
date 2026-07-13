#!/usr/bin/env python3
"""WOR-114 Phase 1A LAN gateway security and lease tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.lan_gateway import (  # noqa: E402
    LAN_PROTOCOL_VERSION,
    LanGatewayLifecycle,
    LanGatewaySession,
    _load_and_validate_certificate_pair,
    _private_key_permissions_are_safe,
    ensure_lan_certificate,
    validate_private_lan_host,
)
from osia.relay_connector import RelayBridge  # noqa: E402
from osia.relay_crypto import (  # noqa: E402
    ALG_CHACHA20_POLY1305,
    E2E_VERSION,
    MOBILE_TO_PC,
    PC_TO_MOBILE,
    RelaySessionContext,
    b64url_decode,
    b64url_encode,
    decrypt_frame,
    derive_pair_secret,
    derive_session_keys,
    encrypt_frame,
    handshake_confirmation_tag,
    verify_handshake_confirmation,
    x25519_public_key,
)
from osia.transport_coordinator import (  # noqa: E402
    ApplicationTransportCoordinator,
    acquire_application_transport,
)
from osia.server.run_server import _launch_after_port_guard  # noqa: E402


_END = object()


def _lease_process(lock_path: str) -> None:
    """Acquire once and exit abruptly so the OS must recover stale ownership."""

    coordinator = ApplicationTransportCoordinator(Path(lock_path))
    lease = coordinator.try_acquire("lan", object())
    os._exit(0 if lease is not None else 3)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.close_reason = ""
        self.closed = False

    async def recv(self):
        item = await self.incoming.get()
        if item is _END:
            raise RuntimeError("socket closed")
        return item

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.close_code = code
        self.close_reason = reason
        await self.incoming.put(_END)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is _END:
            raise StopAsyncIteration
        return item


class _FakeLocalWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.incoming.put(_END)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is _END:
            raise StopAsyncIteration
        return item


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


class LanGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.token = "osia-lan-phase-1a-token"
        self.pair_id = hashlib.sha256(
            derive_pair_secret(self.token).hex().encode("utf-8")
        ).hexdigest()
        self.mobile_private_key = bytes.fromhex(
            "202122232425262728292a2b2c2d2e2f"
            "303132333435363738393a3b3c3d3e3f"
        )
        self.mobile_public_key = x25519_public_key(self.mobile_private_key)
        self.mobile_nonce_base = bytes.fromhex("b0b1b2b3")

    def _hello(self, *, pair_id: str | None = None, extra: bool = False) -> str:
        payload = {
            "e2e_version": E2E_VERSION,
            "alg": ALG_CHACHA20_POLY1305,
            "public_key": b64url_encode(self.mobile_public_key),
            "nonce_base": b64url_encode(self.mobile_nonce_base),
            "e2e_chunk_version": 1,
        }
        if extra:
            payload["unexpected"] = True
        return json.dumps(
            {
                "v": LAN_PROTOCOL_VERSION,
                "type": "E2E_HELLO",
                "role": "mobile",
                "pair_id": pair_id or self.pair_id,
                "payload": payload,
            }
        )

    async def _complete_handshake(self, websocket, session, *, tag: str | None = None):
        await websocket.incoming.put(self._hello())
        await _wait_for(lambda: len(websocket.sent) >= 1)
        pc_hello = json.loads(websocket.sent[0])
        payload = pc_hello["payload"]
        pc_public_key = b64url_decode(payload["public_key"])
        pc_nonce_base = b64url_decode(payload["nonce_base"])
        context = RelaySessionContext(
            pair_id=self.pair_id,
            pc_public_key=pc_public_key,
            mobile_public_key=self.mobile_public_key,
            pc_nonce_base=pc_nonce_base,
            mobile_nonce_base=self.mobile_nonce_base,
        )
        keys = derive_session_keys(
            token=self.token,
            local_private_key=self.mobile_private_key,
            peer_public_key=pc_public_key,
            context=context,
        )
        await websocket.incoming.put(
            json.dumps(
                {
                    "v": LAN_PROTOCOL_VERSION,
                    "type": "E2E_READY",
                    "role": "mobile",
                    "pair_id": self.pair_id,
                    "payload": {
                        "e2e_version": E2E_VERSION,
                        "alg": ALG_CHACHA20_POLY1305,
                        "kid": keys.kid,
                        "tag": tag
                        or handshake_confirmation_tag(
                            token=self.token,
                            context=context,
                            sender_role="mobile",
                        ),
                    },
                }
            )
        )
        return context, keys

    async def test_correct_identity_establishes_e2e_and_forwards_once(self):
        websocket = _FakeWebSocket()
        local = _FakeLocalWebSocket()
        coordinator = ApplicationTransportCoordinator()
        connected_tokens: list[str] = []

        async def connect_local(token: str):
            connected_tokens.append(token)
            return local

        session = LanGatewaySession(
            websocket,
            token=self.token,
            coordinator=coordinator,
            connect_local=connect_local,
        )
        task = asyncio.create_task(session.run())
        context, keys = await self._complete_handshake(websocket, session)
        await _wait_for(lambda: len(websocket.sent) >= 3)

        pc_ready = json.loads(websocket.sent[1])
        self.assertTrue(
            verify_handshake_confirmation(
                token=self.token,
                context=context,
                sender_role="pc",
                tag=pc_ready["payload"]["tag"],
            )
        )
        self.assertEqual(json.loads(websocket.sent[2])["type"], "PC_READY")
        self.assertEqual(connected_tokens, [self.token])
        self.assertEqual(coordinator.active_transport, "lan")

        frame = {"type": "text", "content": "hello", "client_request_id": "r1"}
        encrypted = encrypt_frame(
            frame,
            keys=keys,
            direction=MOBILE_TO_PC,
            pair_id=self.pair_id,
            role="mobile",
            priority="normal",
            final=False,
            seq=1,
        )
        envelope = {
            "v": LAN_PROTOCOL_VERSION,
            "type": "E2E_FRAME",
            "role": "mobile",
            "pair_id": self.pair_id,
            **encrypted,
        }
        await websocket.incoming.put(json.dumps(envelope))
        await _wait_for(lambda: len(local.sent) == 1)
        self.assertEqual(json.loads(local.sent[0]), frame)

        # The same authenticated sequence is a replay: it must not be forwarded twice.
        await websocket.incoming.put(json.dumps(envelope))
        await asyncio.wait_for(task, timeout=1.0)
        self.assertEqual(len(local.sent), 1)
        self.assertEqual(websocket.close_code, 1008)
        self.assertIsNone(coordinator.active_transport)

    async def test_real_wss_pin_and_e2e_handshake_succeed(self):
        import websockets

        with tempfile.TemporaryDirectory() as tmp:
            cert_file = Path(tmp) / "cert.pem"
            key_file = Path(tmp) / "key.pem"
            _, _, expected_pin = ensure_lan_certificate(
                "192.168.1.10", cert_file=cert_file, key_file=key_file
            )
            server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ssl.load_cert_chain(cert_file, key_file)
            local = _FakeLocalWebSocket()

            async def connect_local(_token: str):
                return local

            async def handler(websocket):
                await LanGatewaySession(
                    websocket,
                    token=self.token,
                    coordinator=ApplicationTransportCoordinator(),
                    connect_local=connect_local,
                ).run()

            async with websockets.serve(handler, "127.0.0.1", 0, ssl=server_ssl) as server:
                port = server.sockets[0].getsockname()[1]
                client_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                client_ssl.check_hostname = False
                client_ssl.verify_mode = ssl.CERT_NONE
                async with websockets.connect(
                    f"wss://127.0.0.1:{port}/lan", ssl=client_ssl
                ) as websocket:
                    ssl_object = websocket.transport.get_extra_info("ssl_object")
                    peer_der = ssl_object.getpeercert(binary_form=True)
                    self.assertEqual(hashlib.sha256(peer_der).hexdigest(), expected_pin)

                    await websocket.send(self._hello())
                    pc_hello = json.loads(await websocket.recv())
                    pc_public_key = b64url_decode(pc_hello["payload"]["public_key"])
                    pc_nonce_base = b64url_decode(pc_hello["payload"]["nonce_base"])
                    context = RelaySessionContext(
                        pair_id=self.pair_id,
                        pc_public_key=pc_public_key,
                        mobile_public_key=self.mobile_public_key,
                        pc_nonce_base=pc_nonce_base,
                        mobile_nonce_base=self.mobile_nonce_base,
                    )
                    keys = derive_session_keys(
                        token=self.token,
                        local_private_key=self.mobile_private_key,
                        peer_public_key=pc_public_key,
                        context=context,
                    )
                    await websocket.send(
                        json.dumps(
                            {
                                "v": LAN_PROTOCOL_VERSION,
                                "type": "E2E_READY",
                                "role": "mobile",
                                "pair_id": self.pair_id,
                                "payload": {
                                    "e2e_version": E2E_VERSION,
                                    "alg": ALG_CHACHA20_POLY1305,
                                    "kid": keys.kid,
                                    "tag": handshake_confirmation_tag(
                                        token=self.token,
                                        context=context,
                                        sender_role="mobile",
                                    ),
                                },
                            }
                        )
                    )
                    pc_ready = json.loads(await websocket.recv())
                    bridge_ready = json.loads(await websocket.recv())
                    self.assertTrue(
                        verify_handshake_confirmation(
                            token=self.token,
                            context=context,
                            sender_role="pc",
                            tag=pc_ready["payload"]["tag"],
                        )
                    )
                    self.assertEqual(bridge_ready["type"], "PC_READY")

                    frame = {
                        "type": "text",
                        "content": "wss-e2e",
                        "client_request_id": "wss-r1",
                    }
                    encrypted = encrypt_frame(
                        frame,
                        keys=keys,
                        direction=MOBILE_TO_PC,
                        pair_id=self.pair_id,
                        role="mobile",
                        priority="normal",
                        final=False,
                        seq=1,
                    )
                    await websocket.send(
                        json.dumps(
                            {
                                "v": LAN_PROTOCOL_VERSION,
                                "type": "E2E_FRAME",
                                "role": "mobile",
                                "pair_id": self.pair_id,
                                **encrypted,
                            }
                        )
                    )
                    await _wait_for(lambda: len(local.sent) == 1)
                    self.assertEqual(json.loads(local.sent[0]), frame)

    async def test_wrong_identity_and_malformed_hello_fail_closed(self):
        for hello in (
            self._hello(pair_id="0" * 64),
            self._hello(extra=True),
            "not-json",
        ):
            with self.subTest(hello=hello[:24]):
                websocket = _FakeWebSocket()
                local_called = False

                async def connect_local(_token: str):
                    nonlocal local_called
                    local_called = True
                    return _FakeLocalWebSocket()

                session = LanGatewaySession(
                    websocket,
                    token=self.token,
                    coordinator=ApplicationTransportCoordinator(),
                    connect_local=connect_local,
                )
                await websocket.incoming.put(hello)
                await asyncio.wait_for(session.run(), timeout=1.0)
                self.assertEqual(websocket.close_code, 1008)
                self.assertFalse(local_called)

    async def test_wrong_e2e_confirmation_fails_before_bridge(self):
        websocket = _FakeWebSocket()
        local_called = False

        async def connect_local(_token: str):
            nonlocal local_called
            local_called = True
            return _FakeLocalWebSocket()

        session = LanGatewaySession(
            websocket,
            token=self.token,
            coordinator=ApplicationTransportCoordinator(),
            connect_local=connect_local,
        )
        task = asyncio.create_task(session.run())
        await self._complete_handshake(websocket, session, tag=b64url_encode(b"x" * 32))
        await asyncio.wait_for(task, timeout=1.0)
        self.assertEqual(websocket.close_code, 1008)
        self.assertFalse(local_called)

    async def test_existing_relay_lease_prevents_duplicate_bridge(self):
        websocket = _FakeWebSocket()
        coordinator = ApplicationTransportCoordinator()
        relay_lease = coordinator.try_acquire("relay", object())
        self.assertIsNotNone(relay_lease)
        local_called = False

        async def connect_local(_token: str):
            nonlocal local_called
            local_called = True
            return _FakeLocalWebSocket()

        session = LanGatewaySession(
            websocket,
            token=self.token,
            coordinator=coordinator,
            connect_local=connect_local,
        )
        task = asyncio.create_task(session.run())
        await self._complete_handshake(websocket, session)
        await asyncio.wait_for(task, timeout=1.0)
        self.assertEqual(websocket.close_code, 1013)
        self.assertFalse(local_called)
        self.assertEqual(coordinator.active_transport, "relay")
        relay_lease.release()


class StartupOrderTests(unittest.TestCase):
    def test_failed_port_guard_starts_no_background_component(self):
        calls: list[str] = []

        launched = _launch_after_port_guard(
            port_guard=lambda: False,
            prepare=lambda: calls.append("prepare"),
            start_relay=lambda: calls.append("relay"),
            start_lan=lambda: calls.append("lan"),
            run_server=lambda: calls.append("server"),
        )

        self.assertFalse(launched)
        self.assertEqual(calls, [])

    def test_successful_guard_starts_each_component_exactly_once(self):
        calls: list[str] = []

        launched = _launch_after_port_guard(
            port_guard=lambda: calls.append("guard") or True,
            prepare=lambda: calls.append("prepare"),
            start_relay=lambda: calls.append("relay"),
            start_lan=lambda: calls.append("lan"),
            run_server=lambda: calls.append("server"),
        )

        self.assertTrue(launched)
        self.assertEqual(
            calls,
            ["guard", "prepare", "relay", "lan", "server"],
        )


class GatewayLifecycleTests(unittest.TestCase):
    def test_ready_start_is_idempotent_and_stop_joins(self):
        starts = 0

        async def serve(_host, _cert, _key, ready, stop_event):
            nonlocal starts
            starts += 1
            ready()
            await stop_event.wait()

        lifecycle = LanGatewayLifecycle(serve)
        self.assertTrue(lifecycle.start("192.168.1.10", Path("c"), Path("k")))
        self.assertTrue(lifecycle.started)
        self.assertTrue(lifecycle.start("192.168.1.10", Path("c"), Path("k")))
        self.assertEqual(starts, 1)
        self.assertTrue(lifecycle.stop(timeout=1.0))
        self.assertFalse(lifecycle.started)
        self.assertTrue(lifecycle.stop(timeout=1.0))

    def test_failed_bind_cleans_up_and_allows_retry(self):
        attempts = 0

        async def serve(_host, _cert, _key, ready, stop_event):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("bind failed")
            ready()
            await stop_event.wait()

        lifecycle = LanGatewayLifecycle(serve)
        self.assertFalse(
            lifecycle.start("192.168.1.10", Path("c"), Path("k"), timeout=1.0)
        )
        self.assertFalse(lifecycle.started)
        self.assertIsInstance(lifecycle.error, OSError)
        self.assertTrue(
            lifecycle.start("192.168.1.10", Path("c"), Path("k"), timeout=1.0)
        )
        self.assertEqual(attempts, 2)
        self.assertTrue(lifecycle.stop(timeout=1.0))

    def test_stop_is_safe_before_start(self):
        lifecycle = LanGatewayLifecycle()
        self.assertTrue(lifecycle.stop(timeout=0.1))


class RelayPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_stays_standby_until_encrypted_mobile_commit(self):
        relay_socket = _FakeWebSocket()
        with mock.patch.dict(os.environ, {"OSIA_LAN_DIRECT_V1": "1"}):
            bridge = RelayBridge("phase-a-token", "wss://relay.osia.space/ws")
        bridge.e2e_peer_ready = True
        bridge.e2e_keys = object()
        connect_calls = 0
        acknowledgements: list[dict] = []
        local_socket = _FakeLocalWebSocket()

        async def connect_local(_relay_socket, **_kwargs):
            nonlocal connect_calls
            connect_calls += 1
            bridge._local_ws = local_socket

        async def send_frame(_relay_socket, frame, **_kwargs):
            acknowledgements.append(frame)
            return True

        bridge._connect_local = connect_local
        bridge._send_e2e_frame = send_frame
        commit = {
            "type": "transport_commit",
            "transport": "relay",
            "attempt_id": "a" * 32,
        }
        await bridge._maybe_start_e2e_bridge(relay_socket)
        self.assertEqual(connect_calls, 0)
        ready = json.loads(relay_socket.sent[-1])
        self.assertEqual(ready["type"], "PC_READY")
        self.assertEqual(ready["payload"], {"e2e": True, "standby": True})

        await bridge._handle_transport_commit(relay_socket, commit)
        await bridge._handle_transport_commit(relay_socket, commit)

        self.assertEqual(connect_calls, 1)
        self.assertEqual(
            acknowledgements,
            [
                {
                    "type": "transport_committed",
                    "transport": "relay",
                    "attempt_id": "a" * 32,
                },
                {
                    "type": "transport_committed",
                    "transport": "relay",
                    "attempt_id": "a" * 32,
                },
            ],
        )
        await bridge._close_local()


class LanCertificateTests(unittest.TestCase):
    def test_flag_off_relay_leases_never_contend(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {"OSIA_LAN_DIRECT_V1": "0"}):
            first = acquire_application_transport("relay", object())
            second = acquire_application_transport("relay", object())
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertTrue(first.active)
        self.assertTrue(second.active)
        first.release()
        second.release()

    def test_certificate_pin_is_stable_and_matches_der(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            _, _, pin1 = ensure_lan_certificate(
                "192.168.1.10", cert_file=cert, key_file=key
            )
            _, _, pin2 = ensure_lan_certificate(
                "192.168.1.10", cert_file=cert, key_file=key
            )
            parsed = x509.load_pem_x509_certificate(cert.read_bytes())
            expected = hashlib.sha256(
                parsed.public_bytes(serialization.Encoding.DER)
            ).hexdigest()
            self.assertEqual(pin1, expected)
            self.assertEqual(pin2, expected)
            self.assertEqual(len(pin1), 64)

    def test_invalid_validity_and_wrong_san_regenerate_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            private_key = ec.generate_private_key(ec.SECP256R1())
            now = datetime.now(timezone.utc)
            expired = (
                x509.CertificateBuilder()
                .subject_name(
                    x509.Name(
                        [x509.NameAttribute(NameOID.COMMON_NAME, "OSIA LAN Direct")]
                    )
                )
                .issuer_name(
                    x509.Name(
                        [x509.NameAttribute(NameOID.COMMON_NAME, "OSIA LAN Direct")]
                    )
                )
                .public_key(private_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=2))
                .not_valid_after(now - timedelta(days=1))
                .add_extension(
                    x509.SubjectAlternativeName(
                        [x509.IPAddress(__import__("ipaddress").ip_address("192.168.1.9"))]
                    ),
                    critical=False,
                )
                .sign(private_key, hashes.SHA256())
            )
            cert.write_bytes(expired.public_bytes(serialization.Encoding.PEM))
            key.write_bytes(
                private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )

            ensure_lan_certificate(
                "192.168.1.10",
                cert_file=cert,
                key_file=key,
            )
            regenerated, _ = _load_and_validate_certificate_pair(
                "192.168.1.10",
                cert,
                key,
            )
            self.assertGreater(
                regenerated.not_valid_after_utc,
                datetime.now(timezone.utc),
            )

            mismatched_key = ec.generate_private_key(ec.SECP256R1())
            key.write_bytes(
                mismatched_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            ensure_lan_certificate(
                "192.168.1.10",
                cert_file=cert,
                key_file=key,
            )
            _load_and_validate_certificate_pair("192.168.1.10", cert, key)

    def test_atomic_replace_failure_restores_complete_prior_pair(self):
        import osia.lan_gateway as lan_gateway

        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            ensure_lan_certificate("192.168.1.10", cert_file=cert, key_file=key)
            old_cert = cert.read_bytes()
            old_key = key.read_bytes()
            real_replace = os.replace
            replace_calls = 0

            def fail_second_replace(source, destination):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError("simulated interrupted pair install")
                return real_replace(source, destination)

            with mock.patch.object(
                lan_gateway.os,
                "replace",
                side_effect=fail_second_replace,
            ):
                with self.assertRaises(OSError):
                    ensure_lan_certificate(
                        "192.168.1.11",
                        cert_file=cert,
                        key_file=key,
                    )

            self.assertEqual(cert.read_bytes(), old_cert)
            self.assertEqual(key.read_bytes(), old_key)
            _load_and_validate_certificate_pair("192.168.1.10", cert, key)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_private_key_permissions_are_restricted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            ensure_lan_certificate("192.168.1.10", cert_file=cert, key_file=key)
            self.assertTrue(_private_key_permissions_are_safe(key))

    def test_interprocess_lease_contends_and_recovers_after_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "application.lock"
            coordinator = ApplicationTransportCoordinator(lock_path)
            parent_lease = coordinator.try_acquire("lan", object())
            self.assertIsNotNone(parent_lease)

            context = multiprocessing.get_context("spawn")
            contender = context.Process(target=_lease_process, args=(str(lock_path),))
            contender.start()
            contender.join(10)
            self.assertEqual(contender.exitcode, 3)

            parent_lease.release()
            abrupt_owner = context.Process(
                target=_lease_process,
                args=(str(lock_path),),
            )
            abrupt_owner.start()
            abrupt_owner.join(10)
            self.assertEqual(abrupt_owner.exitcode, 0)

            recovered = coordinator.try_acquire("relay", object())
            self.assertIsNotNone(recovered)
            recovered.release()

    def test_only_rfc1918_ipv4_bind_addresses_are_allowed(self):
        for value in ("10.0.0.7", "172.16.2.3", "192.168.50.4"):
            self.assertEqual(validate_private_lan_host(value), value)
        for value in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "::1", "example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_private_lan_host(value)


if __name__ == "__main__":
    unittest.main()
