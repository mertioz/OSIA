"""Deterministic production-default and one-time migration validation."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from osia import config
from osia.lan_discovery import load_or_create_instance_alias
from osia.lan_gateway import (
    _private_key_permissions_are_safe,
    ensure_lan_certificate,
)
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec


class ProductionDefaultsTests(unittest.TestCase):
    def test_tracked_defaults_are_direct_first_stun_only_and_machine_independent(self):
        defaults = config.DEFAULT_SETTINGS
        self.assertIs(defaults["lan_direct_v1"], True)
        self.assertIs(defaults["webrtc_direct_v1"], True)
        self.assertEqual(
            defaults["webrtc_stun_urls"],
            ["stun:stun.cloudflare.com:3478"],
        )
        self.assertEqual(
            defaults["transport_defaults_schema"],
            config.TRANSPORT_DEFAULTS_SCHEMA_VERSION,
        )
        serialized = json.dumps(defaults, sort_keys=True)
        self.assertNotIn("turn:", serialized)
        self.assertNotIn(":53", serialized)
        self.assertNotIn("lan_direct_host", defaults)
        self.assertNotIn("lan_certificate", serialized)
        self.assertNotIn("certificate_sha256", serialized)

    def test_settings_example_is_valid_safe_and_matches_supported_defaults(self):
        root = Path(__file__).resolve().parents[1]
        example = json.loads(
            (root / "config" / "settings.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(example, config.DEFAULT_SETTINGS)
        self.assertEqual(example["api_key_fallbacks"], {})
        self.assertEqual(example["mcp_servers"], [])
        self.assertIsNone(example["selected_model"])
        self.assertIsNone(example["selected_model_provider"])
        serialized = json.dumps(example, sort_keys=True)
        self.assertNotRegex(
            serialized,
            r"(?:[A-Za-z]:[\\/]Users[\\/]|/(?:home|Users)/[^/]+/)",
        )
        self.assertNotRegex(
            serialized,
            r"(?:10\.(?:\d{1,3}\.){2}\d{1,3}|"
            r"192\.168\.\d{1,3}\.\d{1,3}|"
            r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})",
        )

    def test_windows_installer_is_dependency_complete_and_private_firewall_only(self):
        root = Path(__file__).resolve().parents[1]
        requirements = (root / "requirements.txt").read_text(encoding="utf-8")
        for requirement in (
            "aiortc==1.15.0",
            "dnspython==2.8.0",
            "zeroconf==0.150.0",
        ):
            self.assertIn(requirement, requirements)
        install = (root / "install.bat").read_text(encoding="utf-8")
        self.assertIn('pip install -r "%PROJECT_DIR%requirements.txt"', install)
        self.assertIn("configure_lan_firewall.ps1", install)
        self.assertIn("install_windows_services", install)
        self.assertIn("-Action Install", install)
        self.assertNotIn("print('Mobile token:'", install)

        firewall = (root / "windows" / "configure_lan_firewall.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("-Profile Private", firewall)
        self.assertNotIn("-Profile Public", firewall)
        self.assertIn("-LocalPort 8766", firewall)
        self.assertIn("-RemoteAddress LocalSubnet", firewall)
        self.assertIn("-Program", firewall)
        self.assertIn("-Verb RunAs", firewall)

        uninstall = (root / "uninstall.bat").read_text(encoding="utf-8")
        self.assertIn("-Action Remove", uninstall)
        self.assertIn("disable_service('osia-server.service')", uninstall)
        self.assertIn("disable_service('osia-tunnel.service')", uninstall)
        self.assertNotIn("Remove-Item", uninstall)
        self.assertNotIn("rmdir", uninstall.lower())
        self.assertNotIn("relay_device.json", uninstall)

    def test_gateway_binds_only_selected_private_interface_and_advertises_after_bind(self):
        root = Path(__file__).resolve().parents[1]
        gateway = (root / "osia" / "lan_gateway.py").read_text(encoding="utf-8")
        self.assertIn("host = configured_lan_host()", gateway)
        self.assertIn("validate_private_lan_host", gateway)
        self.assertIn("websockets.serve(\n        handler,\n        host,\n        LAN_PORT", gateway)
        self.assertNotIn("websockets.serve(\n        handler,\n        \"0.0.0.0\"", gateway)
        bind_position = gateway.index("async with websockets.serve(")
        advertise_position = gateway.index("advertiser.start", bind_position)
        ready_position = gateway.index("ready()", advertise_position)
        self.assertLess(bind_position, advertise_position)
        self.assertLess(advertise_position, ready_position)


class TransportMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = self.root / "settings.json"
        self.api_keys = self.root / "api_keys.json"
        self.oauth_tokens = self.root / "oauth_tokens.json"
        self.mobile_token = self.root / "mobile_token.txt"
        self.patchers = (
            mock.patch.object(config, "CONFIG_DIR", str(self.root)),
            mock.patch.object(config, "SETTINGS_FILE", str(self.settings)),
            mock.patch.object(config, "API_KEYS_FILE", str(self.api_keys)),
            mock.patch.object(config, "OAUTH_TOKENS_FILE", str(self.oauth_tokens)),
            mock.patch.object(config, "MOBILE_TOKEN_FILE", str(self.mobile_token)),
        )
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(self._cleanup)
        self._invalidate_cache()

    def _cleanup(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self._invalidate_cache()
        self.temp.cleanup()

    @staticmethod
    def _invalidate_cache() -> None:
        with config._settings_cache_lock:
            config._settings_cache_signature = None
            config._settings_cache_data = {}

    def _write(self, value: object) -> None:
        self.settings.write_text(json.dumps(value), encoding="utf-8")
        self._invalidate_cache()

    def _read(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_brand_new_install_persists_production_defaults(self):
        config.ensure_config_files(create_token=False)
        self.assertEqual(self._read(), config.DEFAULT_SETTINGS)
        self.assertFalse(self.api_keys.exists())
        self.assertFalse(self.oauth_tokens.exists())
        self.assertFalse(self.mobile_token.exists())
        self.assertFalse(config.migrate_transport_defaults())

    def test_valid_existing_settings_are_not_overwritten(self):
        existing = {
            "transport_defaults_schema": config.TRANSPORT_DEFAULTS_SCHEMA_VERSION,
            "lan_direct_v1": False,
            "webrtc_direct_v1": False,
            "mcp_confirm_writes": True,
            "mcp_allow_unattended_writes": False,
            "custom_non_secret": "preserved",
        }
        self._write(existing)
        before = self.settings.read_bytes()
        config.ensure_config_files(create_token=False)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_clean_first_run_creates_only_required_runtime_files(self):
        output = io.StringIO()
        generated = "ab" * 10
        with mock.patch.object(config.secrets, "token_hex", return_value=generated):
            with redirect_stdout(output):
                config.ensure_config_files(create_token=True)
        self.assertEqual(self._read(), config.DEFAULT_SETTINGS)
        self.assertEqual(self.mobile_token.read_text(encoding="utf-8"), generated)
        self.assertNotIn(generated, output.getvalue())
        self.assertFalse(self.api_keys.exists())
        self.assertFalse(self.oauth_tokens.exists())
        if sys.platform != "win32":
            self.assertEqual(
                stat.S_IMODE(self.settings.stat().st_mode),
                stat.S_IRUSR | stat.S_IWUSR,
            )
            self.assertEqual(
                stat.S_IMODE(self.mobile_token.stat().st_mode),
                stat.S_IRUSR | stat.S_IWUSR,
            )

    def test_interrupted_mobile_token_write_preserves_existing_token(self):
        original = "cd" * 10
        self.mobile_token.write_text(original, encoding="utf-8")
        with mock.patch.object(
            config.os, "replace", side_effect=OSError("simulated interruption")
        ):
            with self.assertRaises(OSError):
                config.regenerate_mobile_token()
        self.assertEqual(self.mobile_token.read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.root.glob(".mobile_token.txt.*.tmp")), [])

    def test_old_install_with_missing_keys_is_migrated_once(self):
        self._write({"selected_model": "preserved-model"})
        self.assertTrue(config.migrate_transport_defaults())
        migrated = self._read()
        self.assertEqual(migrated["selected_model"], "preserved-model")
        self.assertIs(migrated["lan_direct_v1"], True)
        self.assertIs(migrated["webrtc_direct_v1"], True)
        self.assertEqual(
            migrated["webrtc_stun_urls"], [config.PRODUCTION_STUN_URL]
        )
        first_bytes = self.settings.read_bytes()
        self.assertFalse(config.migrate_transport_defaults())
        self.assertEqual(self.settings.read_bytes(), first_bytes)

    def test_old_materialized_false_defaults_are_activated(self):
        self._write({
            "lan_direct_v1": False,
            "webrtc_direct_v1": False,
            "webrtc_stun_urls": [],
            "relay_url": "wss://preserved.example/ws",
        })
        self.assertTrue(config.migrate_transport_defaults())
        migrated = self._read()
        self.assertIs(migrated["lan_direct_v1"], True)
        self.assertIs(migrated["webrtc_direct_v1"], True)
        self.assertEqual(migrated["relay_url"], "wss://preserved.example/ws")

    def test_already_migrated_explicit_opt_out_survives_restart(self):
        self._write({
            "transport_defaults_schema": config.TRANSPORT_DEFAULTS_SCHEMA_VERSION,
            "lan_direct_v1": False,
            "webrtc_direct_v1": False,
            "webrtc_stun_urls": [config.PRODUCTION_STUN_URL],
        })
        before = self.settings.read_bytes()
        self.assertFalse(config.migrate_transport_defaults())
        self.assertEqual(self.settings.read_bytes(), before)
        loaded = config.load_settings()
        self.assertIs(loaded["lan_direct_v1"], False)
        self.assertIs(loaded["webrtc_direct_v1"], False)

    def test_post_migration_opt_out_is_not_reenabled(self):
        self._write({"lan_direct_v1": False, "webrtc_direct_v1": False})
        self.assertTrue(config.migrate_transport_defaults())
        migrated = self._read()
        migrated["webrtc_direct_v1"] = False
        config.save_settings(migrated)
        self.assertFalse(config.migrate_transport_defaults())
        self.assertIs(config.load_settings()["webrtc_direct_v1"], False)

    def test_interrupted_atomic_write_preserves_old_file_then_retries(self):
        original = {
            "lan_direct_v1": False,
            "webrtc_direct_v1": False,
            "preserve": "yes",
        }
        self._write(original)
        real_replace = os.replace
        with mock.patch.object(
            config.os, "replace", side_effect=OSError("simulated interruption")
        ):
            with self.assertRaises(OSError):
                config.migrate_transport_defaults()
        self.assertEqual(self._read(), original)
        self.assertEqual(list(self.root.glob(".settings.json.*.tmp")), [])
        with mock.patch.object(config.os, "replace", side_effect=real_replace):
            self.assertTrue(config.migrate_transport_defaults())
        self.assertEqual(self._read()["preserve"], "yes")

    def test_invalid_settings_are_preserved_without_touching_secrets(self):
        invalid = b'{"truncated":'
        api_secret = b'{"provider":"secret-sentinel"}'
        token_secret = b"pairing-secret-sentinel"
        oauth_secret = b'{"oauth":"secret-sentinel"}'
        self.settings.write_bytes(invalid)
        self.api_keys.write_bytes(api_secret)
        self.mobile_token.write_bytes(token_secret)
        self.oauth_tokens.write_bytes(oauth_secret)
        self.assertFalse(config.migrate_transport_defaults())
        self.assertEqual(self.settings.read_bytes(), invalid)
        self.assertEqual(self.api_keys.read_bytes(), api_secret)
        self.assertEqual(self.mobile_token.read_bytes(), token_secret)
        self.assertEqual(self.oauth_tokens.read_bytes(), oauth_secret)

    def test_upgrade_preserves_pairing_identity_and_valid_certificate(self):
        relay_device = self.root / "relay_device.json"
        lan_identity = self.root / "lan_direct_identity.json"
        cert = self.root / "lan_direct_cert.pem"
        key = self.root / "lan_direct_key.pem"
        relay_device.write_bytes(b"paired-relay-sentinel")
        lan_identity.write_bytes(b"paired-lan-sentinel")
        ensure_lan_certificate(
            "192.168.50.10",
            cert_file=cert,
            key_file=key,
        )
        protected = (relay_device, lan_identity, cert, key)
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in protected
        }
        self._write({
            "lan_direct_v1": False,
            "webrtc_direct_v1": False,
            "webrtc_stun_urls": [],
        })

        self.assertTrue(config.migrate_transport_defaults())
        self.assertFalse(config.migrate_transport_defaults())
        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in protected
        }
        self.assertEqual(after, before)


class CleanFirstRunCertificateTests(unittest.TestCase):
    def test_isolated_first_run_creates_reusable_p256_exact_san_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cert = root / "lan_direct_cert.pem"
            key = root / "lan_direct_key.pem"
            identity = root / "lan_direct_identity.json"
            self.assertFalse(cert.exists())
            self.assertFalse(key.exists())
            self.assertFalse(identity.exists())

            _, _, first_pin = ensure_lan_certificate(
                "192.168.60.10",
                cert_file=cert,
                key_file=key,
            )
            alias = load_or_create_instance_alias(identity)
            first_cert = cert.read_bytes()
            first_key = key.read_bytes()
            parsed = x509.load_pem_x509_certificate(first_cert)
            public_key = parsed.public_key()
            self.assertIsInstance(public_key, ec.EllipticCurvePublicKey)
            self.assertIsInstance(public_key.curve, ec.SECP256R1)
            san = parsed.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            self.assertEqual(
                [str(address) for address in san.get_values_for_type(x509.IPAddress)],
                ["192.168.60.10"],
            )
            self.assertTrue(_private_key_permissions_are_safe(key))
            self.assertRegex(alias, r"^[0-9a-f]{16}$")

            _, _, second_pin = ensure_lan_certificate(
                "192.168.60.10",
                cert_file=cert,
                key_file=key,
            )
            self.assertEqual(second_pin, first_pin)
            self.assertEqual(cert.read_bytes(), first_cert)
            self.assertEqual(key.read_bytes(), first_key)
            self.assertEqual(load_or_create_instance_alias(identity), alias)


if __name__ == "__main__":
    unittest.main(verbosity=2)
