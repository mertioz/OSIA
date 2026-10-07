#!/usr/bin/env python3
"""Focused WOR-77 reasoning-settings tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
import threading
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
        self.assertEqual(config.normalize_reasoning_level("warp"), "warp")
        self.assertEqual(
            config.normalize_reasoning_settings(
                {
                    "default": "warp",
                    "providers": {"Google": "LOW", "": "high", 12: "medium"},
                    "models": {
                        "deepseek-flash": "MAX",
                        "gemini-3.5-flash": "LOUD",
                    },
                }
            ),
            {
                "default": "warp",
                "providers": {"google": "low"},
                "models": {
                    "deepseek-flash": "max",
                    "gemini-3.5-flash": "loud",
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
            "providers": {"google": "medium", "anthropic": "high"},
            "models": {
                "gemini-3.5-flash": "high",
                "claude-opus-4-8": "max",
            },
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
                "anthropic", "claude-opus-4-8", settings
            ),
            "max",
        )
        self.assertEqual(
            reasoning.resolve_reasoning_option_id(
                "anthropic", "claude-sonnet-4-6", settings
            ),
            "high",
        )

    def test_model_specific_options(self):
        deepseek_ids = option_ids("deepseek", "deepseek-flash")
        self.assertEqual(deepseek_ids, ["auto", "off", "low", "high", "max"])
        self.assertNotIn("normal", deepseek_ids)
        self.assertNotIn("medium", deepseek_ids)
        deepseek_labels = [
            option["label"]
            for option in reasoning.get_reasoning_options("deepseek", "deepseek-flash")
        ]
        self.assertIn("Non-thinking", deepseek_labels)
        self.assertIn("Max", deepseek_labels)
        self.assertEqual(option_ids("deepseek", "deepseek-v4-pro"), [])

        self.assertEqual(
            option_ids("google", "gemini-3.5-flash"),
            ["auto", "minimal", "low", "medium", "high"],
        )
        self.assertEqual(
            option_ids("google", "gemini-2.5-flash"),
            ["auto", "off", "minimal", "low", "medium", "high"],
        )
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
                "deepseek", "deepseek-flash", {"models": {"deepseek-flash": "auto"}}
            ),
            {},
        )
        with self.assertRaisesRegex(ValueError, "unsupported DeepSeek reasoning"):
            reasoning.build_reasoning_kwargs(
                "deepseek", "deepseek-flash", {"models": {"deepseek-flash": "normal"}}
            )
        self.assertEqual(option_ids("deepseek", "deepseek-v4-pro"), [])
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
                "deepseek", "deepseek-flash", {"models": {"deepseek-flash": "off"}}
            ),
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )
        self.assertEqual(
            reasoning.build_reasoning_kwargs(
                "deepseek",
                "deepseek-flash",
                {"models": {"deepseek-flash": "high"}},
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
                "deepseek-flash",
                {"models": {"deepseek-flash": "max"}},
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
                "mistral", "mistral-medium-3-5", {"default": "high"}
            ),
            {"reasoning_effort": "high"},
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
                "deepseek-flash",
                {"models": {"deepseek-flash": "max"}},
            ),
            "Max",
        )
        self.assertIsNone(
            reasoning.get_selected_reasoning_label(
                "deepseek",
                "deepseek-flash",
                {"models": {"deepseek-flash": "auto"}},
            )
        )
        self.assertIsNone(
            reasoning.get_selected_reasoning_label(
                "deepseek",
                "deepseek-flash",
                {"models": {"deepseek-flash": "medium"}},
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
                    "models": {"deepseek-flash": "MAX"},
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
            personality,
            "resolve_personality",
            lambda: personality.PersonalitySelection(
                "default", "", "test", "missing", ""
            ),
        ), patched(
            personality, "discover_skills", lambda: []
        ):
            cfg = server_mod._build_config_snapshot()

        self.assertNotIn("island_x", cfg)
        self.assertNotIn("island_y", cfg)
        self.assertEqual(
            cfg["reasoning"],
            {
                "default": "high",
                "providers": {"google": "wrong"},
                "models": {"deepseek-flash": "max"},
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
            agent.turn_context = None
            agent._chatgpt_conversation_id = None
            agent._tool_attempt_metadata = None
            agent._slash_turn_directives = None
            agent._execution_generation_local = threading.local()
            agent.lang = "en"
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
        self.assertEqual(captured["messages"][0]["role"], "user")
        self.assertTrue(captured["messages"][0]["content"].startswith("hello"))
        self.assertIn("[TURN RESPONSE LANGUAGE]", captured["messages"][0]["content"])
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
