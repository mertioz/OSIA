"""Privacy and bounded-behavior tests for the production STUN health probe."""

from __future__ import annotations

from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from osia import stun_health


class _FakeSocket:
    def __init__(self, response: bytes | None, *, timeout: bool = False) -> None:
        self.response = response
        self.raise_timeout = timeout
        self.request = b""
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendto(self, request: bytes, _address: tuple) -> int:
        self.request = request
        return len(request)

    def recvfrom(self, _size: int):
        if self.raise_timeout:
            raise socket.timeout
        return self.response, ("discarded", 0)


class StunHealthTests(unittest.TestCase):
    def test_binding_probe_is_credential_free_and_never_allocates_turn(self):
        transaction_id = b"t" * 12
        response = struct.pack("!HHI12s", 0x0101, 0, 0x2112A442, transaction_id)
        fake = _FakeSocket(response)
        with (
            mock.patch.object(
                stun_health,
                "_resolve_numeric_addresses",
                return_value=[(socket.AF_INET, ("redacted", 3478))],
            ),
            mock.patch.object(stun_health.secrets, "token_bytes", return_value=transaction_id),
            mock.patch.object(stun_health.socket, "socket", return_value=fake),
        ):
            result = stun_health.classify_stun_url(
                "stun:stun.cloudflare.com:3478", timeout=1.0
            )
        message_type, body_length, cookie, request_id = struct.unpack(
            "!HHI12s", fake.request
        )
        self.assertEqual(message_type, 0x0001)
        self.assertNotEqual(message_type, 0x0003)
        self.assertEqual(body_length, 0)
        self.assertEqual(cookie, 0x2112A442)
        self.assertEqual(request_id, transaction_id)
        self.assertEqual(len(fake.request), 20)
        self.assertTrue(fake.closed)
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.address_family, "ipv4")
        self.assertEqual(result.candidate_type, "srflx")
        self.assertEqual(result.protocol, "udp")

    def test_health_categories_are_sanitized_and_bounded(self):
        with mock.patch.object(
            stun_health, "_resolve_numeric_addresses", return_value=[]
        ):
            dns = stun_health.classify_stun_url(
                "stun:stun.cloudflare.com:3478", timeout=1.0
            )
        self.assertEqual(dns.status, "dns_unavailable")

        with mock.patch.object(
            stun_health,
            "_resolve_numeric_addresses",
            side_effect=TimeoutError,
        ):
            timed_out = stun_health.classify_stun_url(
                "stun:stun.cloudflare.com:3478", timeout=1.0
            )
        self.assertEqual(timed_out.status, "timeout")

        fake = _FakeSocket(None, timeout=True)
        with (
            mock.patch.object(
                stun_health,
                "_resolve_numeric_addresses",
                return_value=[(socket.AF_INET, ("redacted", 3478))],
            ),
            mock.patch.object(stun_health.socket, "socket", return_value=fake),
        ):
            unavailable = stun_health.classify_stun_url(
                "stun:stun.cloudflare.com:3478", timeout=1.0
            )
        self.assertEqual(unavailable.status, "timeout")
        self.assertTrue(fake.closed)
        self.assertEqual(unavailable.address_family, "none")
        self.assertNotIn("redacted", repr(unavailable))

        malformed = stun_health.classify_stun_url("turn:example.test:3478")
        self.assertEqual(malformed.status, "unavailable")

    def test_startup_probe_is_singleton_and_non_blocking(self):
        starts: list[str] = []

        class FakeThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self):
                starts.append(self.name)

        with stun_health._probe_lock:
            stun_health._probe_started = False
            stun_health._latest_result = None
        with mock.patch.object(stun_health.threading, "Thread", FakeThread):
            self.assertTrue(
                stun_health.start_stun_health_probe(
                    ["stun:stun.cloudflare.com:3478"]
                )
            )
            self.assertFalse(
                stun_health.start_stun_health_probe(
                    ["stun:stun.cloudflare.com:3478"]
                )
            )
        self.assertEqual(starts, ["osia-stun-health"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
