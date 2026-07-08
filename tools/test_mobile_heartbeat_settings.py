#!/usr/bin/env python3
"""Focused mobile heartbeat-settings parity tests."""

from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SERVER_PATH = ROOT / "osia" / "server" / "server.py"
SERVER_SOURCE = SERVER_PATH.read_text(encoding="utf-8")
SERVER_TREE = ast.parse(SERVER_SOURCE)


def _mobile_allowed_settings() -> set[str]:
    for node in ast.walk(SERVER_TREE):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "allowed_settings"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Set):
            return {
                elt.value for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
    raise AssertionError("allowed_settings set not found")


def _function_source(name: str) -> str:
    for node in ast.walk(SERVER_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SERVER_SOURCE, node) or ""
    raise AssertionError(f"{name} function not found")


class MobileHeartbeatSettingsTests(unittest.TestCase):
    def test_mobile_can_write_heartbeat_checks_setting(self):
        self.assertIn("heartbeat_checks", _mobile_allowed_settings())

    def test_config_snapshot_keeps_heartbeat_checks_from_settings(self):
        source = _function_source("_build_config_snapshot")
        self.assertIn("load_settings()", source)
        self.assertNotIn('pop("heartbeat_checks"', source)
        self.assertNotIn("pop('heartbeat_checks'", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
