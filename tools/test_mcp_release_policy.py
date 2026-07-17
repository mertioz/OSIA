"""Focused release-policy checks for OSIA's MCP SDK compatibility pin."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
import sys
import unittest

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MCP_VERSION = "1.28.0"


def _production_requirements() -> list[Requirement]:
    """Parse every effective declaration in the production requirements file."""
    parsed: list[Requirement] = []
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for raw_line in requirements.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        declaration = line.split(" #", 1)[0].rstrip()
        parsed.append(Requirement(declaration))
    return parsed


def _require_exact_supported_mcp(requirement: Requirement) -> None:
    """Reject every MCP declaration except the supported exact SDK pin."""
    if (
        canonicalize_name(requirement.name) != "mcp"
        or requirement.extras
        or requirement.marker is not None
        or requirement.url is not None
        or str(requirement.specifier) != f"=={EXPECTED_MCP_VERSION}"
    ):
        raise AssertionError(
            f"MCP must be pinned exactly to mcp=={EXPECTED_MCP_VERSION}"
        )


class MCPReleasePolicyTests(unittest.TestCase):
    def test_production_requirement_is_the_exact_supported_pin(self) -> None:
        mcp_requirements = [
            requirement
            for requirement in _production_requirements()
            if canonicalize_name(requirement.name) == "mcp"
        ]
        self.assertEqual(len(mcp_requirements), 1)
        _require_exact_supported_mcp(mcp_requirements[0])

    def test_policy_rejects_non_exact_or_unbounded_mcp_forms(self) -> None:
        for declaration in (
            "mcp>=1.28.0",
            "mcp~=1.28.0",
            "mcp==1.28.*",
            "mcp",
        ):
            with self.subTest(declaration=declaration):
                with self.assertRaises(AssertionError):
                    _require_exact_supported_mcp(Requirement(declaration))

    def test_installed_sdk_matches_and_oauth_compatibility_path_imports(self) -> None:
        self.assertEqual(version("mcp"), EXPECTED_MCP_VERSION)

        sys.path.insert(0, str(ROOT))
        try:
            from osia.mcp_client.oauth import (
                build_oauth_provider,
                force_oauth_login,
            )
        finally:
            sys.path.pop(0)

        class _Callback:
            async def redirect_handler(self, _url: str) -> None:
                return None

            async def callback_handler(self) -> tuple[str, str]:
                return "code", "state"

        provider = build_oauth_provider(
            "release-policy-smoke",
            "https://example.invalid/mcp",
            _Callback(),
        )
        self.assertTrue(callable(force_oauth_login))
        self.assertIsNotNone(getattr(provider, "context", None))
        for method_name in (
            "_initialize",
            "_perform_authorization",
            "_handle_token_response",
        ):
            self.assertTrue(callable(getattr(provider, method_name, None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
