#!/usr/bin/env python3
"""Regression tests for repository secret-handling policy."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


def repository_kind() -> str:
    if (ROOT / "osia" / "config.py").is_file():
        return "pc"
    if (ROOT / "pubspec.yaml").is_file():
        return "mobile"
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    if package.get("name") == "osia-rendezvous":
        return "worker"
    if package.get("name") == "osia-site":
        return "site"
    raise AssertionError("unknown OSIA repository")


KIND = repository_kind()
IGNORED = {
    "pc": (
        ".env.local",
        ".wrangler/state/v3/account.json",
        "config/api_keys.json",
        "config/conversations/session.json",
        "config/cron.lock",
        "config/lan_direct_identity.json",
        "config/lan_direct_key.pem",
        "config/memory.sqlite-wal",
        "config/mobile_token.txt",
        "config/onboarding.json",
        "config/oauth_tokens.json",
        "config/relay_device.json",
        "config/settings.json",
        "config/task_state.json",
        "config/tool_attempts/.fingerprint.key",
        "config/turn_credentials.json",
        "config/turn_terminality/turn.json",
        "config/update-state.json",
        "preferences.txt",
       "runtime.log",
        "scripts/__pycache__/scan_secrets.cpython-312.pyc",
       "screenshots/user.png",
    ),
    "mobile": (
        ".env.local",
        "android/app/google-services.json",
        "android/app/release.jks",
        "android/key.properties",
        "android/local.properties",
        "build/app/outputs/flutter-apk/app.apk",
        "ios/ExportOptions.plist",
        "ios/Runner/GoogleService-Info.plist",
        "ios/Runner/profile.mobileprovision",
        "secure_storage_export.json",
        "test-evidence/device.log",
    ),
    "worker": (
        ".cloudflare/account.json",
        ".dev.vars",
        ".dev.vars.local",
        ".env.local",
        ".wrangler/state/v3/account.json",
        "state.sqlite",
        "test-evidence/finding.json",
    ),
    "site": (
        ".cloudflare/account.json",
        ".dev.vars",
        ".env.local",
        ".next/cache/webpack.bin",
        ".vercel/project.json",
        ".wrangler/state/v3/account.json",
        "out/index.html",
        "screenshots/local.png",
        "test-evidence/finding.json",
    ),
}
NOT_IGNORED = {
    "pc": ("config/settings.example.json", ".env.example", "requirements.txt"),
    "mobile": (".env.example", "pubspec.lock", "pubspec.yaml"),
    "worker": (".dev.vars.example", ".env.example", "package-lock.json"),
    "site": (".env.example", "package-lock.json", "wrangler.toml"),
}
EXPECTED_GITLEAKS_ALLOWLISTS = {
    "pc": 5,
    "mobile": 2,
    "worker": 0,
    "site": 0,
}
EXPECTED_OSIA_ALLOWLISTS = {
    "pc": {
        ("tests/test_wor151_close.py", "personal-local-path"),
        ("tests/test_wor182_autonomous_model_inventory.py", "high-entropy-credential"),
        ("tests/test_wor186_browser.py", "basic-auth-url"),
        ("tests/test_wor187_structured_documents.py", "personal-local-path"),
        ("tests/test_wor192_scope_normalization.py", "basic-auth-url"),
        ("tests/test_wor193_system_resources_macos.py", "personal-local-path"),
        ("tests/test_wor193_system_resources_windows.py", "personal-local-path"),
        ("tests/test_wor194_autonomous_protocol.py", "personal-local-path"),
        ("tests/test_wor201_document_tools.py", "personal-local-path"),
        ("tests/test_windows_packaging.py", "personal-local-path"),
        ("tools/test_gmail_write_safety.py", "high-entropy-credential"),
        ("tools/test_memory_store.py", "pem-private-key"),
        ("tools/test_mobile_model_catalogue.py", "high-entropy-credential"),
        ("tools/test_vision_inputs.py", "personal-local-path"),
    },
    "mobile": set(),
    "worker": set(),
    "site": set(),
}


def load_scanner():
    path = ROOT / "scripts" / "scan_secrets.py"
    spec = importlib.util.spec_from_file_location("osia_secret_scan_policy_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCAN = load_scanner()


class IgnorePolicyTests(unittest.TestCase):
    def _is_ignored(self, relative: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", "--", relative],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr.decode(errors="replace"))
        return result.returncode == 0

    def test_protected_runtime_paths_are_ignored(self):
        for relative in IGNORED[KIND]:
            with self.subTest(relative=relative):
                self.assertTrue(self._is_ignored(relative))

    def test_examples_and_required_manifests_remain_trackable(self):
        for relative in NOT_IGNORED[KIND]:
            with self.subTest(relative=relative):
                self.assertFalse(self._is_ignored(relative))


class ScannerPolicyTests(unittest.TestCase):
    def rules_for(self, text: str, path: str = "sample.txt") -> set[str]:
        return {
            finding.rule
            for finding in SCAN._scan_text(text, path, "policy-test", "", "")
        }

    def test_structured_and_generic_credentials_are_detected(self):
        structured = ("s" + "k" + "-") + ("Ab3dEf5Gh7Jk9Lm2" * 2)
        self.assertIn("api-compatible-key", self.rules_for(structured))

        generic = "Ab3dEf5Gh7Jk9Lm2Np4Qr6St"
        self.assertIn(
            "high-entropy-credential",
            self.rules_for(f'api_key = "{generic}"'),
        )

        private_marker = "-----BEGIN " + "PRIVATE KEY-----"
        self.assertIn("pem-private-key", self.rules_for(private_marker))

    def test_safe_placeholders_do_not_look_like_credentials(self):
        rules = self.rules_for('api_key = "<set-in-a-secret-store>"')
        self.assertNotIn("high-entropy-credential", rules)
        self.assertNotIn("api-compatible-key", rules)

    def test_tool_attempt_fingerprint_key_is_a_sensitive_store(self):
        self.assertIn(
            "tool-attempt-fingerprint-key",
            self.rules_for("", "config/tool_attempts/.fingerprint.key"),
        )

    def test_osia_allowlist_is_exact_and_keeps_structured_rules_active(self):
        self.assertEqual(SCAN.OSIA_ALLOWLIST, EXPECTED_OSIA_ALLOWLISTS[KIND])
        structured = ("s" + "k" + "-") + ("Ab3dEf5Gh7Jk9Lm2" * 2)
        findings = SCAN._scan_text(
            structured,
            "tools/test_memory_store.py",
            "policy-test",
            "",
            "",
        )
        filtered = {
            finding
            for finding in findings
            if (finding.path, finding.rule) not in SCAN.OSIA_ALLOWLIST
        }
        self.assertIn("api-compatible-key", {finding.rule for finding in filtered})

    def test_gitleaks_allowlists_are_path_and_rule_scoped(self):
        config = tomllib.loads(
            (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
        )
        self.assertIs(config["extend"]["useDefault"], True)
        allowlists = config.get("allowlists", [])
        self.assertEqual(len(allowlists), EXPECTED_GITLEAKS_ALLOWLISTS[KIND])
        for allowlist in allowlists:
            self.assertTrue(allowlist.get("targetRules"))
            self.assertTrue(allowlist.get("paths"))
            if allowlist.get("regexes"):
                self.assertEqual(allowlist.get("condition"), "AND")


class WorkflowPolicyTests(unittest.TestCase):
    def test_secret_scan_workflow_is_pinned_and_read_only(self):
        workflow = (
            ROOT / ".github" / "workflows" / "secret-scan.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertRegex(workflow, r"actions/checkout@[0-9a-f]{40}")
        self.assertNotRegex(workflow, r"permissions:[\s\S]*?\bwrite\b")
        self.assertIn('GITLEAKS_VERSION: "8.30.1"', workflow)
        self.assertLess(
            workflow.index("Run Gitleaks before repository code"),
            workflow.index("Test the repository security policy"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
