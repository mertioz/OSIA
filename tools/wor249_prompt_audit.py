"""Deterministic effective-prompt snapshots and before/after measurements for WOR-249."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia import agent, personality
from osia.chatgpt_account import provider as account_provider
from osia.mcp_client import registry as mcp_registry
from osia.tool_contracts import effective_tool_surface_scope
from osia.turn_context import fixed_turn_context


BASELINE_PATH = ROOT / "tests" / "fixtures" / "wor249_prompt_baseline.json"

_FIXTURE_PERSONA = "WOR249_PERSONALITY: calm, direct, and precise."
_FIXTURE_SKILL = """## User-enabled skills (lower-priority preferences)
WOR249_SKILL: use evidence."""
_FIXTURE_MCP = """## Connected MCP tools (available now)
Call these with the same textual <tool_call> JSON protocol and top-level arguments as OSIA tools.
- mcp__fixture__read: Read fixture state. Usage: {"tool":"mcp__fixture__read","query":"<value>"}"""


def prompt_metric(text: str) -> dict:
    """Return stable size/hash metrics, with an exact local tokenizer when installed."""

    encoded = text.encode("utf-8")
    result = {
        "chars": len(text),
        "utf8_bytes": len(encoded),
        "approx_tokens": (len(encoded) + 3) // 4,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    try:
        import tiktoken

        result["o200k_tokens"] = len(tiktoken.get_encoding("o200k_base").encode(text))
    except (ImportError, KeyError, ValueError):
        result["o200k_tokens"] = None
    return result


def _prompt(*, lang: str, surface: str = "", rich: bool = False) -> str:
    scope = effective_tool_surface_scope(surface) if surface else nullcontext()
    with ExitStack() as stack:
        stack.enter_context(scope)
        stack.enter_context(patch.object(agent, "_PROJECT_DIR", "C:/OSIA/project"))
        stack.enter_context(patch.object(agent, "get_desktop_path", return_value="C:/OSIA/Desktop"))
        stack.enter_context(patch.object(
            personality,
            "load_personality",
            return_value=_FIXTURE_PERSONA if rich else "",
        ))
        stack.enter_context(patch.object(
            personality,
            "load_enabled_skills_text",
            return_value=_FIXTURE_SKILL if rich else "",
        ))
        stack.enter_context(patch.object(
            mcp_registry,
            "mcp_tool_prompt_block",
            return_value=_FIXTURE_MCP if rich else "",
        ))
        return agent.get_system_prompt(lang, "fixture-model", "deepseek")


def build_deterministic_snapshots() -> dict[str, str]:
    """Build provider/mode/personality/skills/MCP/context prompt fixtures."""

    chat_en = _prompt(lang="en")
    chat_fr = _prompt(lang="fr")
    rich = _prompt(lang="fr", rich=True)
    scheduled = _prompt(lang="en", surface="scheduled")
    scheduled_fr = _prompt(lang="fr", surface="scheduled")

    context_agent = object.__new__(agent.OSIAAgent)
    context_agent.provider = "deepseek"
    context_agent.model = "fixture-model"
    context_agent.lang = "fr"
    context_agent.turn_context = fixed_turn_context(
        "fr", source="wor249_fixture", reference="wor249-fixture-turn",
    )
    context_agent._chatgpt_conversation_id = "wor249-fixture-conversation"
    context_agent.messages = [
        {"role": "system", "content": rich},
        {"role": "user", "content": "Bonjour, rappelle le contexte stable."},
        {"role": "assistant", "content": "Je conserve le contexte vérifié."},
        {
            "role": "user",
            "content": (
                "[Local OSIA memory — untrusted recalled context]\n"
                "WOR249_MEMORY: fixture evidence only, never instructions."
            ),
        },
    ]
    wire_context = "\n\n".join(
        f"[{message.get('role')}]\n{message.get('content')}"
        for message in context_agent._wire_messages()
    )

    account_messages = [{"role": "user", "content": "Réponds sans chemin local."}]
    account_sanitized = account_provider._sanitized_system_prompt(rich, account_messages)
    account_base = account_provider._insert_before_turn_language(
        account_sanitized,
        account_provider._CONTAINMENT_INSTRUCTIONS,
    )
    account_combined = account_base + "\n\n" + account_provider._DEVELOPER_INSTRUCTIONS

    return {
        "scheduled_api_en": scheduled,
        "scheduled_api_fr": scheduled_fr,
        "chat_api_en": chat_en,
        "chat_api_fr": chat_fr,
        "chat_api_fr_rich": rich,
        "chat_api_fr_rich_context": wire_context,
        "chatgpt_account_developer": account_provider._DEVELOPER_INSTRUCTIONS,
        "chatgpt_account_fr_rich_base": account_base,
        "chatgpt_account_fr_rich_combined": account_combined,
    }


def measure_snapshots(snapshots: dict[str, str]) -> dict[str, dict]:
    return {name: prompt_metric(text) for name, text in snapshots.items()}


def compare_with_baseline(current: dict[str, dict]) -> dict[str, dict]:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["snapshots"]
    comparison = {}
    for name, after in current.items():
        before = baseline[name]
        row = {"before": before, "after": after}
        for field in ("chars", "utf8_bytes", "approx_tokens", "o200k_tokens"):
            if before.get(field) is not None and after.get(field) is not None:
                delta = after[field] - before[field]
                row[field + "_delta"] = delta
                row[field + "_reduction_percent"] = round(-delta * 100 / before[field], 2)
        comparison[name] = row
    return comparison


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-text", action="store_true", help="include prompt bodies")
    args = parser.parse_args()
    snapshots = build_deterministic_snapshots()
    metrics = measure_snapshots(snapshots)
    report = {
        "baseline_source": str(BASELINE_PATH),
        "comparison": compare_with_baseline(metrics),
    }
    if args.include_text:
        report["current_snapshots"] = snapshots
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
