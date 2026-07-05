#!/usr/bin/env python3
"""Focused WOR-77 reasoning-settings tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import osia.config as config
import osia.reasoning as reasoning
from osia import personality
from osia.server import server as server_mod


@contextmanager
def patched(obj, name: str, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def option_ids(provider: str, model: str) -> list[str]:
    return [opt["id"] for opt in reasoning.get_reasoning_options(provider, model)]


def claude_adaptive_kwargs(effort: str) -> dict:
    return {
        "extra_body": {
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": effort},
        }
    }


class ReasoningSettingsTests(unittest.TestCase):
    def test_default_config_normalizes_to_stable_schema(self):
        self.assertEqual(
            config.normalize_reasoning_settings(None),
            {"default": "auto", "providers": {}, "models": {}},
        )
        with patched(
            config,
            "_read_settings_file",
            lambda: {"reasoning": {"providers": {"Google": "Medium"}}},
        ):
            settings = config.load_settings()
        self.assertEqual(
            settings["reasoning"],
            {
                "default": "auto",
                "providers": {"google": "medium"},
                "models": {},
            },
        )

    def test_invalid_and_partial_configs_normalize_safely(self):
        self.assertEqual(config.normalize_reasoning_level("HIGH"), "high")
        self.assertEqual(config.normalize_reasoning_level("MAX"), "max")
        self.assertEqual(config.normalize_reasoning_level("none"), "off")
        self.assertEqual(config.normalize_reasoning_level("warp"), "auto")
        self.assertEqual(
            config.normalize_reasoning_settings(
                {
                    "default": "warp",
                    "providers": {"Google": "LOW", "": "high", 12: "medium"},
                    "models": {
                        "deepseek-v4-pro": "MAX",
                        "gemini-3.5-flash": "LOUD",
                    },
                }
            ),
            {
                "default": "auto",
                "providers": {"google": "low"},
                "models": {
                    "deepseek-v4-pro": "max",
                    "gemini-3.5-flash": "auto",
                },
            },
        )
        self.assertEqual(
            config.normalize_reasoning_settings(
                {"models": {"gemini-3.5-flash": "minimal"}}
            ),
            {
                "default": "auto",
                "providers": {},
                "models": {"gemini-3.5-flash": "minimal"},
            },
        )

    def test_resolution_order_model_provider_default(self):
        settings = {
            "default": "low",
            "providers": {"google": "medium"},
            "models": {"gemini-3.5-flash": "high"},
        }
        self.assertEqual(
            reasoning.resolve_reasoning_option_id(
                "google", "gemini-3.5-flash", settings
            ),
            "high",
        )
        self.assertEqual(
            reasoning.resolve_reasoning_option_id(
                "google", "gemini-3.1-pro-preview", settings
            ),
            "medium",
        )
        self.assertEqual(
            reasoning.resolve_reasoning_option_id(
                "mistral", "mistral-medium-3-5", settings
            ),
            "low",
        )

    def test_model_specific_options(self):
        deepseek_ids = option_ids("deepseek", "deepseek-v4-pro")
        self.assertEqual(deepseek_ids, ["auto", "off", "normal", "max"])
        self.assertNotIn("low", deepseek_ids)
        self.assertNotIn("medium", deepseek_ids)
        self.assertNotIn("high", deepseek_ids)
        deepseek_labels = [
            opt["label"]
            for opt in reasoning.get_reasoning_options("deepseek", "deepseek-v4-pro")
        ]
        self.assertIn("Normal", deepseek_labels)
        self.assertIn("Max", deepseek_labels)

        self.assertEqual(
            option_ids("google", "gemini-3.5-flash"),
            ["auto", "minimal", "low", "medium", "high"],
        )
        self.assertEqual(
            option_ids("google", "gemini-2.5-flash"),
            ["auto", "off", "minimal", "low", "medium", "high"],
        )
        self.assertEqual(
            option_ids("groq", "openai/gpt-oss-120b"),
            ["auto", "low", "medium", "high"],
        )
        self.assertEqual(
            option_ids("mistral", "mistral-medium-3-5"),
            ["auto", "off", "minimal", "low", "medium", "high", "xhigh"],
        )
        openai_reasoning_ids = [
            "auto",
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        ]
        self.assertEqual(option_ids("openai", "gpt-5.5"), openai_reasoning_ids)
        self.assertEqual(option_ids("openai", "gpt-5.4"), openai_reasoning_ids)
        self.assertEqual(option_ids("openai", "gpt-5.4-mini"), openai_reasoning_ids)
        self.assertEqual(option_ids("openai", "gpt-4.1"), [])
        self.assertEqual(
            option_ids("anthropic", "claude-opus-4-8"),
            ["auto", "off", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(
            option_ids("anthropic", "claude-sonnet-4-6"),
            ["auto", "off", "low", "medium", "high", "max"],
        )
        self.assertNotIn("xhigh", option_ids("anthropic", "claude-sonnet-4-6"))
        self.assertEqual(option_ids("anthropic", "claude-haiku-4-5-20251001"), [])
        self.assertEqual(option_ids("xai", "grok-3"), [])

    def test_auto_invalid_and_unsupported_models_return_no_kwargs(self):
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "google", "gemini-3.5-flash", {"models": {"gemini-3.5-flash": "auto"}}
            ),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "deepseek", "deepseek-v4-pro", {"models": {"deepseek-v4-pro": "low"}}
            ),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs("openai", "gpt-5.5", {"default": "auto"}),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "openai", "gpt-4.1", {"default": "high"}
            ),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "anthropic", "claude-opus-4-8", {"default": "auto"}
            ),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "anthropic", "claude-haiku-4-5-20251001", {"default": "high"}
            ),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs("xai", "grok-3", {"default": "high"}),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs("ollama", "llama3", {"default": "high"}),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "groq", "llama-3.3-70b-versatile", {"default": "high"}
            ),
            {},
        )

    def test_verified_provider_request_kwargs(self):
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "google",
                "gemini-3.5-flash",
                {"models": {"gemini-3.5-flash": "minimal"}},
            ),
            {"reasoning_effort": "minimal"},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "google", "gemini-3.5-flash", {"default": "high"}
            ),
            {"reasoning_effort": "high"},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "google", "gemini-2.5-flash", {"default": "off"}
            ),
            {"reasoning_effort": "none"},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "google", "gemini-3.5-flash", {"default": "off"}
            ),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "deepseek", "deepseek-v4-pro", {"models": {"deepseek-v4-pro": "off"}}
            ),
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "deepseek",
                "deepseek-v4-pro",
                {"models": {"deepseek-v4-pro": "normal"}},
            ),
            {
                "extra_body": {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "high",
                }
            },
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "deepseek",
                "deepseek-v4-flash",
                {"models": {"deepseek-v4-flash": "max"}},
            ),
            {
                "extra_body": {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "max",
                }
            },
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "groq", "openai/gpt-oss-120b", {"default": "medium"}
            ),
            {"reasoning_effort": "medium"},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "groq", "openai/gpt-oss-20b", {"default": "off"}
            ),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "mistral", "mistral-medium-3-5", {"default": "off"}
            ),
            {"reasoning_effort": "none"},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "mistral", "mistral-medium-3-5", {"default": "xhigh"}
            ),
            {"reasoning_effort": "xhigh"},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "mistral", "mistral-large-2512", {"default": "high"}
            ),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "openai", "gpt-5.5", {"models": {"gpt-5.5": "off"}}
            ),
            {"reasoning_effort": "none"},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "openai", "gpt-5.5", {"models": {"gpt-5.5": "high"}}
            ),
            {"reasoning_effort": "high"},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "openai", "gpt-5.5", {"models": {"gpt-5.5": "xhigh"}}
            ),
            {"reasoning_effort": "xhigh"},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "anthropic",
                "claude-opus-4-8",
                {"models": {"claude-opus-4-8": "max"}},
            ),
            claude_adaptive_kwargs("max"),
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "anthropic",
                "claude-opus-4-8",
                {"models": {"claude-opus-4-8": "off"}},
            ),
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "anthropic",
                "claude-sonnet-4-6",
                {"models": {"claude-sonnet-4-6": "max"}},
            ),
            claude_adaptive_kwargs("max"),
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "anthropic",
                "claude-sonnet-4-6",
                {"models": {"claude-sonnet-4-6": "xhigh"}},
            ),
            {},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "anthropic",
                "claude-haiku-4-5-20251001",
                {"models": {"claude-haiku-4-5-20251001": "max"}},
            ),
            {},
        )

    def test_selected_reasoning_label(self):
        self.assertEqual(
            reasoning.get_selected_reasoning_label(
                "deepseek",
                "deepseek-v4-pro",
                {"models": {"deepseek-v4-pro": "max"}},
            ),
            "Max",
        )
        self.assertIsNone(
            reasoning.get_selected_reasoning_label(
                "deepseek",
                "deepseek-v4-pro",
                {"models": {"deepseek-v4-pro": "auto"}},
            )
        )
        self.assertIsNone(
            reasoning.get_selected_reasoning_label(
                "deepseek",
                "deepseek-v4-pro",
                {"models": {"deepseek-v4-pro": "medium"}},
            )
        )

    def test_server_snapshot_and_mobile_payload_normalize_reasoning(self):
        self.assertEqual(
            server_mod._normalize_mobile_reasoning_setting(["bad"]),
            {"default": "auto", "providers": {}, "models": {}},
        )

        def fake_load_settings():
            return {
                "reasoning": {
                    "default": "HIGH",
                    "providers": {"Google": "wrong"},
                    "models": {"deepseek-v4-pro": "MAX"},
                },
                "mcp_servers": [],
                "island_x": 10,
                "island_y": 20,
            }

        def fake_get_setting(key, default=None):
            if key in ("api_key_fallbacks",):
                return {}
            if key == "mcp_servers":
                return []
            return default

        with patched(config, "load_settings", fake_load_settings), patched(
            config, "load_api_keys", lambda: {}
        ), patched(config, "get_setting", fake_get_setting), patched(
            config, "get_mcp_token", lambda _sid: ""
        ), patched(
            server_mod, "_gmail_rest_config_snapshot", lambda: {}
        ), patched(
            personality, "load_personality", lambda: ""
        ), patched(
            personality, "list_skills", lambda: []
        ):
            cfg = server_mod._build_config_snapshot()

        self.assertNotIn("island_x", cfg)
        self.assertNotIn("island_y", cfg)
        self.assertEqual(
            cfg["reasoning"],
            {
                "default": "high",
                "providers": {"google": "auto"},
                "models": {"deepseek-v4-pro": "max"},
            },
        )

    def test_agent_stream_chat_request_merges_reasoning_kwargs(self):
        import osia.agent as agent_mod

        def capture_stream_kwargs(provider: str, model: str, settings: dict) -> dict:
            captured: dict = {}

            class FakeCompletions:
                def create(self, **kwargs):
                    captured.update(kwargs)
                    return []

            class FakeChat:
                completions = FakeCompletions()

            class FakeClient:
                chat = FakeChat()

            agent = object.__new__(agent_mod.OSIAAgent)
            agent.client = FakeClient()
            agent.model = model
            agent.provider = provider
            agent.messages = [{"role": "user", "content": "hello"}]

            with patched(reasoning, "get_reasoning_settings", lambda: settings):
                self.assertEqual(list(agent._stream_deltas()), [])

            return captured

        captured = capture_stream_kwargs(
            "google",
            "gemini-3.5-flash",
            {
                "default": "auto",
                "providers": {},
                "models": {"gemini-3.5-flash": "high"},
            },
        )
        self.assertEqual(captured["model"], "gemini-3.5-flash")
        self.assertEqual(captured["messages"], [{"role": "user", "content": "hello"}])
        self.assertTrue(captured["stream"])
        self.assertEqual(captured["reasoning_effort"], "high")

        captured = capture_stream_kwargs(
            "openai",
            "gpt-5.5",
            {"default": "auto", "providers": {}, "models": {"gpt-5.5": "high"}},
        )
        self.assertEqual(captured["reasoning_effort"], "high")

        captured = capture_stream_kwargs(
            "openai",
            "gpt-5.5",
            {"default": "auto", "providers": {}, "models": {"gpt-5.5": "auto"}},
        )
        self.assertNotIn("reasoning_effort", captured)
        self.assertNotIn("extra_body", captured)

        captured = capture_stream_kwargs(
            "anthropic",
            "claude-opus-4-8",
            {
                "default": "auto",
                "providers": {},
                "models": {"claude-opus-4-8": "max"},
            },
        )
        self.assertEqual(
            captured["extra_body"],
            claude_adaptive_kwargs("max")["extra_body"],
        )

        captured = capture_stream_kwargs(
            "anthropic",
            "claude-opus-4-8",
            {
                "default": "auto",
                "providers": {},
                "models": {"claude-opus-4-8": "off"},
            },
        )
        self.assertEqual(captured["extra_body"], {"thinking": {"type": "disabled"}})

        captured = capture_stream_kwargs(
            "anthropic",
            "claude-haiku-4-5-20251001",
            {
                "default": "auto",
                "providers": {},
                "models": {"claude-haiku-4-5-20251001": "max"},
            },
        )
        self.assertNotIn("reasoning_effort", captured)
        self.assertNotIn("extra_body", captured)


if __name__ == "__main__":
    unittest.main(verbosity=2)
