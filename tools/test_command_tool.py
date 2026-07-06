#!/usr/bin/env python3
"""Focused tests for OSIA's shell command tool."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import osia.agent as agent


def _run_ok(command: str) -> str:
    result = agent.tool_run_command(command)
    if result.startswith("ERROR"):
        raise AssertionError(result)
    return result.strip()


class CommandTimeoutTests(unittest.TestCase):
    def test_default_timeout_is_300_and_executor_allows_it(self):
        self.assertEqual(agent.DEFAULT_BASH_TIMEOUT_SECONDS, 300)
        self.assertEqual(agent._command_timeout_seconds(), 300)
        self.assertEqual(
            agent._tool_timeout_seconds("run_command", {"command": "echo ok"}),
            300 + agent.COMMAND_EXECUTOR_GRACE_SECONDS,
        )
        self.assertEqual(
            agent._tool_timeout_seconds("bash", {"command": "echo ok"}),
            300 + agent.COMMAND_EXECUTOR_GRACE_SECONDS,
        )
        self.assertEqual(
            agent._tool_timeout_seconds("read_file", {"path": "README.md"}),
            agent.TOOL_TIMEOUT_SECONDS,
        )

    def test_explicit_shorter_timeout_still_works(self):
        command = "Start-Sleep -Seconds 2; Write-Output done"
        if sys.platform != "win32":
            quoted = repr("import time; time.sleep(2); print('done')")
            command = f"{sys.executable!r} -c {quoted}"

        started = time.monotonic()
        result = agent.tool_run_command(command, timeout=1)
        elapsed = time.monotonic() - started

        self.assertIn("timed out after 1s", result)
        self.assertLess(elapsed, 5)
        self.assertEqual(agent._tool_timeout_seconds(
            "run_command",
            {"command": command, "timeout": 1},
        ), 1 + agent.COMMAND_EXECUTOR_GRACE_SECONDS)

    def test_shell_timeout_result_is_not_retried(self):
        calls = []

        def fake_timeout(command: str, timeout=None) -> str:
            calls.append((command, timeout))
            return "ERROR: command timed out after 1s"

        old = agent.TOOL_MAP["run_command"]
        agent.TOOL_MAP["run_command"] = fake_timeout
        try:
            result, is_error = agent._execute_tool_with_retry(
                "run_command",
                {"command": "fake", "timeout": 1},
            )
        finally:
            agent.TOOL_MAP["run_command"] = old

        self.assertEqual(calls, [("fake", 1)])
        self.assertTrue(is_error)
        self.assertIn("timed out after 1s", result)


@unittest.skipUnless(
    sys.platform == "win32" and shutil.which("powershell.exe"),
    "Windows PowerShell tests require powershell.exe",
)
class WindowsPowerShellCommandTests(unittest.TestCase):
    def test_env_userprofile_survives(self):
        self.assertEqual(_run_ok("Write-Output $env:USERPROFILE"), os.environ["USERPROFILE"])

    def test_braced_variable_and_literal_dollar_survive(self):
        result = _run_ok(
            "${OSIA_PS_VAR} = 'brace-ok'\n"
            "Write-Output ${OSIA_PS_VAR}\n"
            "Write-Output '$literal-dollar'"
        )
        self.assertEqual(result.splitlines(), ["brace-ok", "$literal-dollar"])

    def test_hashtable_survives(self):
        result = _run_ok(
            '$h = @{ Name = "Test"; Value = 1 }\n'
            'Write-Output "$($h.Name):$($h.Value)"'
        )
        self.assertEqual(result, "Test:1")

    def test_script_block_braces_survive(self):
        result = _run_ok('& { param($x) Write-Output "block:$x" } "ok"')
        self.assertEqual(result, "block:ok")

    def test_multiline_powershell_works(self):
        result = _run_ok(
            "$first = 'multi'\n"
            "$second = 'line'\n"
            'Write-Output "$first-$second"'
        )
        self.assertEqual(result, "multi-line")

    def test_simple_cmd_command_still_works(self):
        self.assertEqual(_run_ok("cmd /c echo osia-cmd-ok"), "osia-cmd-ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
