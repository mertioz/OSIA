import asyncio
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from osia import lan_discovery, lan_gateway


class _FakeServiceInfo:
    instances = []

    def __init__(self, service_type, name, **kwargs):
        self.service_type = service_type
        self.name = name
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.instances.append(self)


class _FakeZeroconf:
    instances = []
    fail_registration = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.registered = []
        self.unregistered = []
        self.closed = 0
        self.instances.append(self)

    def register_service(self, service, *, allow_name_change):
        if self.fail_registration:
            raise RuntimeError("registration failed")
        self.registered.append((service, allow_name_change))

    def unregister_service(self, service):
        self.unregistered.append(service)

    def close(self):
        self.closed += 1


class LanDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeServiceInfo.instances.clear()
        _FakeZeroconf.instances.clear()
        _FakeZeroconf.fail_registration = False
        self._v4_only = object()
        self._zeroconf_module = SimpleNamespace(
            IPVersion=SimpleNamespace(V4Only=self._v4_only),
            ServiceInfo=_FakeServiceInfo,
            Zeroconf=_FakeZeroconf,
        )

    def test_advertisement_contains_only_public_routing_metadata(self):
        advertiser = lan_discovery.LanDiscoveryAdvertiser(
            "192.168.10.20", 8766, "0123456789abcdef",
        )
        with patch.dict(sys.modules, {"zeroconf": self._zeroconf_module}):
            advertiser.start()
            advertiser.start()

        self.assertEqual(len(_FakeServiceInfo.instances), 1)
        service = _FakeServiceInfo.instances[0]
        self.assertEqual(service.service_type, "_osia._tcp.local.")
        self.assertEqual(
            service.name,
            "OSIA-0123456789abcdef._osia._tcp.local.",
        )
        self.assertEqual(service.addresses, [socket.inet_aton("192.168.10.20")])
        self.assertEqual(service.port, 8766)
        self.assertEqual(
            service.properties,
            {
                b"protocol": b"1",
                b"product_version": b"1.0.0",
                b"instance": b"0123456789abcdef",
            },
        )
        self.assertEqual(
            set(service.properties),
            {b"protocol", b"product_version", b"instance"},
        )
        zeroconf = _FakeZeroconf.instances[0]
        self.assertEqual(
            zeroconf.kwargs,
            {"interfaces": ["192.168.10.20"], "ip_version": self._v4_only},
        )
        self.assertEqual(zeroconf.registered, [(service, False)])

        advertiser.close()
        advertiser.close()
        self.assertEqual(zeroconf.unregistered, [service])
        self.assertEqual(zeroconf.closed, 1)

    def test_registration_failure_closes_partial_zeroconf(self):
        advertiser = lan_discovery.LanDiscoveryAdvertiser(
            "10.1.2.3", 8766, "fedcba9876543210",
        )
        _FakeZeroconf.fail_registration = True
        with patch.dict(sys.modules, {"zeroconf": self._zeroconf_module}):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                advertiser.start()

        self.assertEqual(_FakeZeroconf.instances[0].closed, 1)
        self.assertIsNone(advertiser._zeroconf)
        self.assertIsNone(advertiser._service)

    def test_discovery_host_and_alias_validation_fail_closed(self):
        invalid_hosts = (
            "0.0.0.0",
            "127.0.0.1",
            "8.8.8.8",
            "169.254.1.1",
            "::1",
            "not-an-address",
        )
        for host in invalid_hosts:
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "private IPv4"):
                    lan_discovery.LanDiscoveryAdvertiser(
                        host, 8766, "0123456789abcdef",
                    )
        for alias in ("", "0123456789abcde", "0123456789ABCDEG"):
            with self.subTest(alias=alias):
                with self.assertRaisesRegex(ValueError, "instance alias"):
                    lan_discovery.LanDiscoveryAdvertiser(
                        "172.16.1.2", 8766, alias,
                    )

    def test_instance_alias_is_stable_random_and_contains_no_pairing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lan-direct-identity.json"
            first = lan_discovery.load_or_create_instance_alias(path)
            second = lan_discovery.load_or_create_instance_alias(path)
            payload = path.read_text(encoding="utf-8")

        self.assertRegex(first, r"^[a-f0-9]{16}$")
        self.assertEqual(second, first)
        self.assertIn('"version":1', payload)
        self.assertIn(f'"instance_alias":"{first}"', payload)
        lowered = payload.lower()
        for forbidden in ("pair", "pin", "proof", "token", "key", "secret"):
            self.assertNotIn(forbidden, lowered)

    async def test_gateway_readiness_follows_bind_and_advertisement_cleanup(self):
        events = []
        stop_event = asyncio.Event()

        class _FakeServerContext:
            async def __aenter__(self):
                events.append("wss-bound")
                return self

            async def __aexit__(self, *_args):
                events.append("wss-closed")

        class _FakeWebsockets:
            @staticmethod
            def serve(*_args, **_kwargs):
                return _FakeServerContext()

        class _FakeAdvertiser:
            def __init__(self, host, port, alias):
                self.args = (host, port, alias)
                events.append("advertiser-created")

            def start(self):
                events.append("advertisement-started")

            def close(self):
                events.append("advertisement-closed")

        class _FakeSSLContext:
            minimum_version = None

            def load_cert_chain(self, *_args):
                events.append("certificate-loaded")

        def ready():
            events.append("ready")
            stop_event.set()

        with (
            patch.dict(sys.modules, {"websockets": _FakeWebsockets}),
            patch.object(
                lan_discovery,
                "LanDiscoveryAdvertiser",
                _FakeAdvertiser,
            ),
            patch.object(
                lan_gateway,
                "lan_provisioning_snapshot",
                return_value={"instance_alias": "0123456789abcdef"},
            ),
            patch.object(
                lan_gateway.ssl,
                "SSLContext",
                return_value=_FakeSSLContext(),
            ),
        ):
            await lan_gateway._serve(
                "192.168.10.20",
                Path("certificate.pem"),
                Path("private-key.pem"),
                ready,
                stop_event,
            )

        self.assertEqual(
            events,
            [
                "certificate-loaded",
                "wss-bound",
                "advertiser-created",
                "advertisement-started",
                "ready",
                "advertisement-closed",
                "wss-closed",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
