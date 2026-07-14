#!/usr/bin/env python3
"""Focused regression tests for sanitized provider-failure handling."""

import contextlib
import io
from types import SimpleNamespace
import unittest

from osia.agent import OSIAAgent


class _QuotaError(Exception):
    status_code = 429
    body = {
        "error": {
            "code": "insufficient_quota",
            "message": "SENSITIVE_PROVIDER_RESPONSE",
        }
    }
    response = SimpleNamespace(headers={
        "Retry-After": "30",
        "Authorization": "SENSITIVE_HEADER",
    })


class _Completions:
    @staticmethod
    def create(**_kwargs):
        raise _QuotaError("provider rejected request")


class ProviderErrorPolicyTests(unittest.TestCase):
    def test_agent_429_log_contains_only_sanitized_policy_fields(self):
        agent = OSIAAgent(lang="en")
        agent.provider = "google"
        agent.model = "test-model"
        agent.client = SimpleNamespace(
            chat=SimpleNamespace(completions=_Completions())
        )
        agent._api_key_chain = ["not-a-real-key"]
        agent._api_key_pos = 0
        agent._api_key = agent._api_key_chain[0]
        output = io.StringIO()
        with self.assertRaises(_QuotaError):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                agent._run_turn("harmless", max_tool_rounds=0)
        logged = output.getvalue()
        self.assertIn("category=quota_exhausted", logged)
        self.assertNotIn("SENSITIVE_PROVIDER_RESPONSE", logged)
        self.assertNotIn("SENSITIVE_HEADER", logged)
        self.assertNotIn("Authorization", logged)


if __name__ == "__main__":
    unittest.main()
