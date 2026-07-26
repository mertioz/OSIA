#!/usr/bin/env python3
"""Provider/agent/GUI contract tests for the managed account layer."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from osia.agent import OSIAAgent
from osia.chatgpt_account.app_server_client import AppServerError, TurnResult
from osia.chatgpt_account.provider import (
    AccountProviderError,
    ChatGPTAccountProvider,
    _sanitized_system_prompt, _text_content,
)


class _ProviderClient:
    def __init__(self):
        self.generation = 1
        self.workspace_dir = Path(tempfile.gettempdir()) / "osia-provider-test"
        self.started = []
        self.turns = []

    def start_or_resume_conversation(self, **kwargs):
        self.started.append(kwargs)
        return f"thread-{len(self.started)}"

    def run_turn(self, **kwargs):
        self.turns.append(kwargs)
        text = "account reply"
        if kwargs.get("on_delta"):
            kwargs["on_delta"](text)
        return TurnResult(text, kwargs["thread_id"], "turn-1", "completed", True)

    def cancel_turn(self, **_kwargs):
        pass

    def shutdown(self):
        pass


class ProviderAdapterTests(unittest.TestCase):
    def test_conversation_continuity_sends_only_new_user_content(self):
        client = _ProviderClient()
        provider = ChatGPTAccountProvider(client=client)
        first = [
            {"role": "system", "content": "OSIA system"},
            {"role": "user", "content": "hello"},
        ]
        provider.complete(conversation_id="c1", model="m1", messages=first)
        second = first + [
            {"role": "assistant", "content": "account reply"},
            {"role": "user", "content": "next"},
        ]
        provider.complete(conversation_id="c1", model="m1", messages=second)
        self.assertEqual(len(client.started), 1)
        self.assertEqual(client.turns[0]["text"], "hello")
        self.assertEqual(client.turns[1]["text"], "next")

    def test_history_mismatch_recreates_only_account_thread(self):
        client = _ProviderClient()
        provider = ChatGPTAccountProvider(client=client)
        provider.complete(
            conversation_id="c1", model="m1",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "one"}],
        )
        provider.complete(
            conversation_id="c1", model="m1",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "different"}],
        )
        self.assertEqual(len(client.started), 2)

    def test_local_image_parts_are_never_attached(self):
        content = [
            {"type": "text", "text": "safe text"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}},
        ]
        self.assertEqual(_text_content(content), "safe text")

    def test_local_paths_are_disclosed_only_when_request_requires_them(self):
        system = (
            "Your working directory is C:\\private\\osia/.\n"
            "Use the exact absolute path given in the [DESKTOP PATH: ...] line.\n"
            "[DESKTOP PATH: C:\\Users\\me\\Desktop]"
        )
        ordinary = _sanitized_system_prompt(
            system, [{"role": "user", "content": "hello"}]
        )
        self.assertNotIn("C:\\private\\osia", ordinary)
        self.assertNotIn("C:\\Users\\me\\Desktop", ordinary)
        required = _sanitized_system_prompt(
            system, [{"role": "user", "content": "list my desktop"}]
        )
        self.assertNotIn("C:\\private\\osia", required)
        self.assertIn("C:\\Users\\me\\Desktop", required)

    def test_rejected_request_diagnostics_keep_safe_turn_context(self):
        client = _ProviderClient()

        def reject(**_kwargs):
            raise AppServerError(
                "ERROR: safe retry message",
                category="request_rejected",
                method="thread/start",
                server_code=-32600,
            )

        client.start_or_resume_conversation = reject
        provider = ChatGPTAccountProvider(client=client)
        with self.assertRaises(AccountProviderError) as caught:
            provider.complete(
                conversation_id="c1",
                model="account-model",
                messages=[{"role": "user", "content": "hello"}],
                effort="high",
            )
        error = caught.exception
        self.assertEqual(error.method, "thread/start")
        self.assertEqual(error.server_code, -32600)
        self.assertEqual(error.selected_model, "account-model")
        self.assertEqual(error.reasoning_level, "high")
        self.assertFalse(error.output_started)

    def test_native_action_refusal_survives_account_provider_normalization(self):
        from osia import i18n

        client = _ProviderClient()
        with patch("osia.i18n.get_language", return_value="fr"):
            refusal = i18n.t("chatgpt_account.native_action_refused")

        def reject(**_kwargs):
            raise AppServerError(
                refusal,
                output_started=True,
                native_action=True,
                controlled_refusal=True,
                allow_fallback=False,
                category="native_item:fileChange",
            )

        client.run_turn = reject
        provider = ChatGPTAccountProvider(client=client)
        with self.assertRaises(AccountProviderError) as caught:
            provider.complete(
                conversation_id="c1",
                model="account-model",
                messages=[{"role": "user", "content": "hello"}],
            )
        error = caught.exception
        self.assertEqual(str(error), refusal)
        self.assertIn("Aucune action n’a été effectuée", str(error))
        self.assertTrue(error.output_started)
        self.assertTrue(error.native_action)
        self.assertTrue(error.controlled_refusal)
        self.assertFalse(error.allow_fallback)


class _FakeCompletions:
    def __init__(self, *, text=None, error=None, calls=None):
        self.text = text
        self.error = error
        self.calls = calls if calls is not None else []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        message = SimpleNamespace(content=self.text, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _SequenceCompletions:
    def __init__(self, outcomes, calls):
        self.outcomes = list(outcomes)
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        message = SimpleNamespace(content=outcome, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeOpenAIClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


class _FakeAccountProvider:
    def __init__(self, account_client):
        self.account_client = account_client

    def chat_client(self, conversation_id=None):
        self.account_client.conversation_id = conversation_id
        self.account_client.set_conversation_id = lambda value: setattr(
            self.account_client, "conversation_id", value
        )
        return self.account_client


class _RecordingCompletions:
    def __init__(self, provider, outcomes, attempts):
        self.provider = provider
        self.outcomes = list(outcomes)
        self.attempts = attempts

    def create(self, **_kwargs):
        self.attempts.append(self.provider)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        message = SimpleNamespace(content=outcome, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class AgentRoutingEqualityTests(unittest.TestCase):
    @staticmethod
    def _account_error(message="ERROR: unavailable", *, allow=True, output=False):
        return AccountProviderError(AppServerError(
            message, allow_fallback=allow, output_started=output
        ))

    def _run_route(
        self,
        *,
        selected_provider,
        selected_model,
        hierarchy=None,
        outcomes=None,
        tool_map=None,
    ):
        import osia.agent as agent_module
        from osia.models import PROVIDERS

        hierarchy = list(hierarchy or [])
        outcomes = {key: list(value) for key, value in (outcomes or {}).items()}
        attempts = []
        clients = {
            provider: _FakeOpenAIClient(_RecordingCompletions(
                provider,
                outcomes.get(provider, [f"{provider} reply"]),
                attempts,
            ))
            for provider in PROVIDERS
        }
        account_provider = _FakeAccountProvider(clients["chatgpt_account"])

        by_url = {
            config["base_url"]: provider
            for provider, config in PROVIDERS.items()
            if provider != "chatgpt_account"
        }

        def openai_factory(*_args, **kwargs):
            return clients[by_url.get(str(kwargs.get("base_url")), "ollama")]

        settings = {
            "ai_hierarchy_enabled": bool(hierarchy),
            "ai_hierarchy": {"heavy": [], "medium": hierarchy},
        }

        def get_setting(key, default=None):
            return copy.deepcopy(settings.get(key, default))

        patches = [
            patch("osia.agent.OpenAI", side_effect=openai_factory),
            patch("osia.config.get_setting", side_effect=get_setting),
            patch("osia.task_router.classify_task", return_value="medium"),
            patch(
                "osia.chatgpt_account.provider.get_account_provider",
                return_value=account_provider,
            ),
            patch.object(OSIAAgent, "_provider_key_chain", return_value=["test-key"]),
        ]
        if tool_map:
            patches.append(patch.dict(agent_module.TOOL_MAP, tool_map))
        started = [item.start() for item in patches]
        try:
            agent = OSIAAgent(model="manual-model", lang="en")
            agent.set_model(selected_model, provider=selected_provider)
            try:
                result = agent.run(
                    "hello", history=[], current_conversation_id="conv-1"
                )
                error = None
            except Exception as exc:
                result, error = None, exc
        finally:
            for item in reversed(patches):
                item.stop()
        return result, error, attempts, agent, settings

    def test_manual_deepseek_is_not_intercepted_by_connected_account(self):
        result, error, attempts, agent, _settings = self._run_route(
            selected_provider="deepseek", selected_model="deepseek-v4-pro"
        )
        self.assertIsNone(error)
        self.assertEqual(result, "deepseek reply")
        self.assertEqual(attempts, ["deepseek"])
        self.assertEqual(agent.provider, "deepseek")

    def test_manual_openai_is_not_intercepted_by_connected_account(self):
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="openai", selected_model="gpt-5.4"
        )
        self.assertIsNone(error)
        self.assertEqual(result, "openai reply")
        self.assertEqual(attempts, ["openai"])

    def test_manual_chatgpt_model_uses_account_provider(self):
        result, error, attempts, agent, _settings = self._run_route(
            selected_provider="chatgpt_account", selected_model="account-model"
        )
        self.assertIsNone(error)
        self.assertEqual(result, "chatgpt_account reply")
        self.assertEqual(attempts, ["chatgpt_account"])
        self.assertEqual(agent.provider, "chatgpt_account")

    def test_saved_provider_identity_wins_over_ambiguous_model_id(self):
        account_client = _FakeOpenAIClient(
            _FakeCompletions(text="account reply", calls=[])
        )
        account_provider = _FakeAccountProvider(account_client)
        api_client = _FakeOpenAIClient(_FakeCompletions(text="api reply", calls=[]))

        for saved_provider in ("chatgpt_account", "openai"):
            with self.subTest(saved_provider=saved_provider):
                with (
                    patch("osia.agent.OpenAI", return_value=api_client),
                    patch(
                        "osia.agent.OSIAAgent._provider_key_chain",
                        return_value=["key"],
                    ),
                    patch(
                        "osia.config.get_setting",
                        side_effect=lambda key, default=None: {
                            "selected_model": "gpt-shared-id",
                            "selected_model_provider": saved_provider,
                        }.get(key, default),
                    ),
                    patch(
                        "osia.chatgpt_account.provider.get_account_provider",
                        return_value=account_provider,
                    ),
                ):
                    agent = OSIAAgent(model="manual-model", lang="en")
                    agent.set_model("gpt-shared-id")
                    self.assertEqual(agent.provider, saved_provider)

    def test_disconnected_account_does_not_change_non_account_route(self):
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="deepseek", selected_model="deepseek-v4-pro"
        )
        self.assertIsNone(error)
        self.assertEqual(result, "deepseek reply")
        self.assertNotIn("chatgpt_account", attempts)

    def test_hierarchy_deepseek_chatgpt_openai_preserves_exact_order(self):
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="ollama",
            selected_model="manual-model",
            hierarchy=[
                "deepseek:deepseek-v4-pro",
                "chatgpt_account:account-model",
                "openai:gpt-5.4",
            ],
            outcomes={
                "deepseek": [RuntimeError("connection error")],
                "chatgpt_account": [self._account_error()],
                "openai": ["openai success"],
            },
        )
        self.assertIsNone(error)
        self.assertEqual(result, "openai success")
        self.assertEqual(attempts, ["deepseek", "chatgpt_account", "openai"])

    def test_hierarchy_openai_chatgpt_groq_preserves_exact_order(self):
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="ollama",
            selected_model="manual-model",
            hierarchy=[
                "openai:gpt-5.4",
                "chatgpt_account:account-model",
                "groq:llama-3.3-70b-versatile",
            ],
            outcomes={
                "openai": [RuntimeError("connection error")],
                "chatgpt_account": [self._account_error()],
                "groq": ["groq success"],
            },
        )
        self.assertIsNone(error)
        self.assertEqual(result, "groq success")
        self.assertEqual(attempts, ["openai", "chatgpt_account", "groq"])

    def test_chatgpt_is_first_only_when_saved_first(self):
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="ollama",
            selected_model="manual-model",
            hierarchy=[
                "chatgpt_account:account-model",
                "deepseek:deepseek-v4-pro",
            ],
        )
        self.assertIsNone(error)
        self.assertEqual(result, "chatgpt_account reply")
        self.assertEqual(attempts, ["chatgpt_account"])

    def test_hierarchy_without_chatgpt_never_attempts_account(self):
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="openai",
            selected_model="gpt-5.4",
            hierarchy=["deepseek:deepseek-v4-pro", "openai:gpt-5.4"],
            outcomes={"deepseek": [RuntimeError("connection error")]},
        )
        self.assertIsNone(error)
        self.assertEqual(result, "openai reply")
        self.assertEqual(attempts, ["deepseek", "openai"])

    def test_pre_output_account_failure_uses_next_configured_provider(self):
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="openai",
            selected_model="gpt-5.4",
            hierarchy=[
                "chatgpt_account:account-model",
                "deepseek:deepseek-v4-pro",
            ],
            outcomes={"chatgpt_account": [self._account_error()]},
        )
        self.assertIsNone(error)
        self.assertEqual(result, "deepseek reply")
        self.assertEqual(attempts, ["chatgpt_account", "deepseek"])

    def test_partial_account_failure_is_never_replayed(self):
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="openai",
            selected_model="gpt-5.4",
            hierarchy=[
                "chatgpt_account:account-model",
                "deepseek:deepseek-v4-pro",
            ],
            outcomes={
                "chatgpt_account": [self._account_error(
                    "ERROR: partial", allow=False, output=True
                )]
            },
        )
        self.assertIsNone(result)
        self.assertIsInstance(error, AccountProviderError)
        self.assertEqual(attempts, ["chatgpt_account"])

    def test_native_action_failure_is_never_replayed_before_output(self):
        from osia import i18n

        with patch("osia.i18n.get_language", return_value="en"):
            refusal = i18n.t("chatgpt_account.native_action_refused")
        native_error = AccountProviderError(AppServerError(
            refusal,
            native_action=True,
            controlled_refusal=True,
            allow_fallback=False,
            category="native_item:commandExecution",
        ))
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="openai",
            selected_model="gpt-5.4",
            hierarchy=[
                "chatgpt_account:account-model",
                "deepseek:deepseek-v4-pro",
            ],
            outcomes={"chatgpt_account": [native_error]},
        )
        self.assertIsNone(result)
        self.assertIs(error, native_error)
        self.assertEqual(str(error), refusal)
        self.assertTrue(error.native_action)
        self.assertTrue(error.controlled_refusal)
        self.assertFalse(error.allow_fallback)
        self.assertEqual(attempts, ["chatgpt_account"])

    def test_openai_and_deepseek_tool_paths_still_reach_osia_dispatcher(self):
        for provider, model in (
            ("openai", "gpt-5.4"),
            ("deepseek", "deepseek-v4-pro"),
        ):
            with self.subTest(provider=provider):
                tool_calls = []

                def fake_tool(value):
                    tool_calls.append(value)
                    return "tool ok"

                result, error, attempts, _agent, _settings = self._run_route(
                    selected_provider=provider,
                    selected_model=model,
                    outcomes={provider: [
                        '<tool_call>{"tool":"account_test_tool","value":"ok"}</tool_call>',
                        f"{provider} final",
                    ]},
                    tool_map={"account_test_tool": fake_tool},
                )
                self.assertIsNone(error)
                self.assertEqual(result, f"{provider} final")
                self.assertEqual(tool_calls, ["ok"])
                self.assertEqual(attempts, [provider, provider])

    def test_no_provider_replay_after_textual_tool_call(self):
        tool_calls = []

        def fake_tool(value):
            tool_calls.append(value)
            return "tool ok"

        late_error = self._account_error("ERROR: reinjection failed")
        result, error, attempts, _agent, _settings = self._run_route(
            selected_provider="openai",
            selected_model="gpt-5.4",
            hierarchy=[
                "chatgpt_account:account-model",
                "deepseek:deepseek-v4-pro",
            ],
            outcomes={
                "chatgpt_account": [
                    '<tool_call>{"tool":"account_test_tool","value":"ok"}</tool_call>',
                    late_error,
                ]
            },
            tool_map={"account_test_tool": fake_tool},
        )
        self.assertIsNone(error)
        self.assertIn("did not replay the turn", result)
        self.assertEqual(
            _agent.last_run_diagnostic.get("reason_code"),
            "task_execution_failure",
        )
        self.assertEqual(tool_calls, ["ok"])
        self.assertEqual(attempts, ["chatgpt_account", "chatgpt_account"])

    def test_no_account_first_or_pinned_routing_helper_remains(self):
        root = Path(__file__).resolve().parents[1]
        agent_source = (root / "osia" / "agent.py").read_text(encoding="utf-8")
        provider_source = (
            root / "osia" / "chatgpt_account" / "provider.py"
        ).read_text(encoding="utf-8")
        gui_source = (
            root / "osia" / "gui" / "main_popup.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("enable_chatgpt_account", agent_source)
        self.assertNotIn("preferred_route", provider_source)
        self.assertNotIn("_chatgpt_hierarchy_pinned", gui_source)


class ProtectedPipelineRegressionTests(unittest.TestCase):
    def test_generic_model_change_clears_stale_selected_provider(self):
        from osia import config

        saved = []
        settings = {
            "selected_model": "account-model",
            "selected_model_provider": "chatgpt_account",
        }
        with (
            patch.object(config, "load_settings", return_value=copy.deepcopy(settings)),
            patch.object(config, "save_settings", side_effect=saved.append),
        ):
            config.set_setting("selected_model", "deepseek-v4-pro")
        self.assertEqual(saved[-1]["selected_model"], "deepseek-v4-pro")
        self.assertIsNone(saved[-1]["selected_model_provider"])

    def test_groq_xml_and_deepseek_native_tool_calls_still_parse(self):
        import osia.agent as agent_module
        xml = '<tool_call>{"tool":"read_file","path":"x"}</tool_call>'
        groq = agent_module._extract_tool_calls(xml, provider="groq")
        self.assertEqual(groq, [{"tool": "read_file", "path": "x"}])

        native_call = SimpleNamespace(
            function=SimpleNamespace(name="read_file", arguments='{"path":"y"}')
        )
        message = SimpleNamespace(tool_calls=[native_call])
        deepseek = agent_module._extract_tool_calls("", message=message, provider="deepseek")
        self.assertEqual(deepseek, [{"tool": "read_file", "path": "y"}])

    def test_parse_before_strip_contract_remains_observable(self):
        import osia.agent as agent_module
        raw = '<tool_call>{"tool":"read_file","path":"x"}</tool_call>'
        self.assertTrue(agent_module._extract_tool_calls(raw, provider="groq"))
        self.assertFalse(agent_module._extract_tool_calls(
            agent_module.strip_raw_tool_tags(raw), provider="groq"
        ))


class GuiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _build_panel(self, *, connect_handler=None):
        from osia.config import DEFAULT_SETTINGS
        from osia.gui import main_popup
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["ai_hierarchy"] = {
            "heavy": ["openai:a", "anthropic:b"],
            "medium": ["groq:c"],
        }
        patches = [
            patch.object(main_popup, "load_settings", return_value=copy.deepcopy(settings)),
            patch.object(main_popup, "load_api_keys", return_value={}),
            patch.object(main_popup, "autostart_enabled", return_value=False),
            patch.object(
                main_popup._SettingsPanel, "_probe_chatgpt_account",
                lambda _self: None,
            ),
            patch.object(main_popup, "set_setting"),
        ]
        if connect_handler is not None:
            patches.append(patch.object(
                main_popup._SettingsPanel, "_on_chatgpt_connect",
                connect_handler,
            ))
        started = [item.start() for item in patches]
        try:
            panel = main_popup._SettingsPanel()
        finally:
            for item in reversed(patches):
                item.stop()
        return panel, started[4], settings

    @staticmethod
    def _select(panel, provider):
        for index in range(panel._ai_provider_combo.count()):
            if panel._ai_provider_combo.itemData(index) == provider:
                panel._ai_provider_combo.setCurrentIndex(index)
                return
        raise AssertionError(f"missing settings provider entry: {provider}")

    def test_chatgpt_is_first_distinct_dropdown_entry_below_selector(self):
        from osia.gui import main_popup
        panel, _save_setting, _settings = self._build_panel()
        entries = [
            (panel._ai_provider_combo.itemData(index),
             panel._ai_provider_combo.itemText(index))
            for index in range(panel._ai_provider_combo.count())
        ]
        self.assertEqual(entries, [
            ("chatgpt_account", "ChatGPT"),
            ("anthropic", "Anthropic"),
            ("openai", "OpenAI"),
            ("google", "Google"),
            ("deepseek", "DeepSeek"),
            ("xai", "xAI"),
            ("groq", "Groq"),
            ("mistral", "Mistral"),
            ("ollama", "Local (Ollama)"),
        ])
        root_layout = panel._chatgpt_card.parentWidget().layout()
        card_index = root_layout.indexOf(panel._chatgpt_card)
        selector_index = next(
            index for index in range(root_layout.count())
            if root_layout.itemAt(index).layout() is not None
            and root_layout.itemAt(index).layout().indexOf(panel._ai_provider_combo) >= 0
        )
        self.assertLess(selector_index, card_index)
        self.assertFalse(hasattr(panel, "_chatgpt_disclosure"))
        permanent_text = [
            label.text() for label in panel._chatgpt_card.findChildren(main_popup.QLabel)
        ]
        self.assertNotIn(main_popup.i18n.t("chatgpt_account.disclosure"), permanent_text)
        self.assertFalse(any("always first" in text.lower() for text in permanent_text))
        panel.deleteLater()

    def test_compact_states_and_information_tooltip(self):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QFocusEvent
        from osia.gui import main_popup
        panel, _save_setting, _settings = self._build_panel()
        self._select(panel, "chatgpt_account")
        panel._apply_chatgpt_status({
            "available": False, "runtimeAvailable": True,
            "connected": False, "models": [], "error": "disconnected",
        })
        self.assertFalse(panel._chatgpt_connect_btn.isHidden())
        self.assertTrue(panel._chatgpt_status.isHidden())
        self.assertFalse(hasattr(panel, "_chatgpt_model_row"))
        self.assertFalse(hasattr(panel, "_chatgpt_model_combo"))
        self.assertFalse(hasattr(panel, "_chatgpt_reasoning_row"))
        self.assertFalse(hasattr(panel, "_chatgpt_reasoning_combo"))
        self.assertTrue(panel._chatgpt_disconnect_btn.isHidden())
        self.assertFalse(panel._chatgpt_info_btn.isHidden())
        tooltip = panel._chatgpt_info_btn.toolTip()
        for expected in (
            "cloud provider", "processed by OpenAI", "does not automatically attach",
            "privacy guarantees do not apply", "like other providers",
            "separately available",
        ):
            self.assertIn(expected.lower(), tooltip.lower())
        self.assertNotIn("before api", tooltip.lower())
        self.assertNotIn("always first", tooltip.lower())
        self.assertGreaterEqual(tooltip.count("\n"), 2)
        self.assertEqual(panel._chatgpt_info_btn.accessibleDescription(), tooltip)
        with patch.object(main_popup.QToolTip, "showText") as show_tooltip:
            panel._chatgpt_info_btn.focusInEvent(
                QFocusEvent(QEvent.Type.FocusIn)
            )
            self.assertEqual(show_tooltip.call_args.args[1], tooltip)

        with patch.object(main_popup.threading, "Thread") as thread:
            panel._on_chatgpt_connect()
            panel._on_chatgpt_connect()
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
        self.assertFalse(panel._chatgpt_cancel_btn.isHidden())
        self.assertTrue(panel._chatgpt_connect_btn.isHidden())
        self.assertIn("ChatGPT", panel._chatgpt_status.text())
        panel._chatgpt_action_busy = False

        panel._apply_chatgpt_status({
            "available": True, "runtimeAvailable": True, "connected": True,
            "account": {"type": "chatgpt", "email": "safe@example.test",
                        "planType": "plus"},
            "models": [{"id": "m1", "displayName": "Model One"}],
            "selectedModel": "m1", "error": "",
            "quotaWindows": [
                {"label": "5 h", "remainingPercent": 100},
                {"label": "Weekly", "remainingPercent": 79},
            ],
        })
        self.assertFalse(panel._chatgpt_status.isHidden())
        self.assertEqual(panel._chatgpt_account_detail.text(), "safe@example.test · Plus")
        self.assertEqual(
            panel._chatgpt_usage.text(),
            "5 h: 100% remaining · Weekly: 79% remaining",
        )
        self.assertNotIn("Usage", panel._chatgpt_usage.text())
        self.assertFalse(panel._chatgpt_usage.isHidden())
        self.assertFalse(panel._chatgpt_disconnect_btn.isHidden())
        self.assertFalse(panel._chatgpt_info_btn.isHidden())

        panel._apply_chatgpt_status({
            "available": False, "runtimeAvailable": True, "connected": False,
            "models": [], "error": '{"jsonrpc":"2.0","token":"SECRET"}',
        })
        self.assertFalse(panel._chatgpt_retry_btn.isHidden())
        self.assertNotIn("jsonrpc", panel._chatgpt_status.text())
        self.assertNotIn("SECRET", panel._chatgpt_status.text())
        self.assertFalse(panel._chatgpt_info_btn.isHidden())
        panel.deleteLater()

    def test_retry_resets_stale_login_opens_browser_once_and_redacts_errors(self):
        from osia.chatgpt_account.app_server_client import AppServerError
        from osia.chatgpt_account import provider as provider_module
        from osia.gui import main_popup

        panel, _save_setting, _settings = self._build_panel()
        self._select(panel, "chatgpt_account")
        observed = {"reset": 0, "begin": 0, "wait": 0, "waiting_text": ""}

        class FakeProvider:
            def reset_login(self):
                observed["reset"] += 1

            def begin_login(self):
                observed["begin"] += 1
                return {
                    "authUrl": "https://chatgpt.com/auth/test-only",
                    "loginId": "login-test",
                }

            def wait_for_login(self, _login_id, timeout):
                observed["wait"] += 1
                observed["waiting_text"] = panel._chatgpt_status.text()
                self.assert_timeout = timeout
                raise AppServerError(
                    "ERROR: SECRET session detail",
                    category="login_timeout",
                )

            def cancel_login(self):
                raise AssertionError("browser opened; cancellation is not expected")

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        fake_provider = FakeProvider()
        panel._chatgpt_status.setText("previous failure")
        panel._set_chatgpt_ui_state("error")
        with (
            patch.object(provider_module, "get_account_provider", return_value=fake_provider),
            patch.object(main_popup.webbrowser, "open", return_value=True) as browser,
            patch.object(main_popup.threading, "Thread", ImmediateThread),
        ):
            panel._on_chatgpt_retry()

        self.assertEqual(observed["reset"], 1)
        self.assertEqual(observed["begin"], 1)
        self.assertEqual(observed["wait"], 1)
        self.assertEqual(fake_provider.assert_timeout, 300.0)
        self.assertIn("Waiting for validation", observed["waiting_text"])
        browser.assert_called_once_with("https://chatgpt.com/auth/test-only", new=2)
        self.assertEqual(panel._chatgpt_status.text(), "Connection failed")
        self.assertNotIn("SECRET", panel._chatgpt_status.text())
        panel.deleteLater()

    def test_browser_launch_failure_is_visible_and_retry_is_single_flight(self):
        from osia.chatgpt_account import provider as provider_module
        from osia.gui import main_popup

        panel, _save_setting, _settings = self._build_panel()
        self._select(panel, "chatgpt_account")
        cancelled = []

        class FakeProvider:
            def reset_login(self):
                pass

            def begin_login(self):
                return {
                    "authUrl": "https://chatgpt.com/auth/test-only",
                    "loginId": "login-test",
                }

            def cancel_login(self):
                cancelled.append(True)

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with (
            patch.object(provider_module, "get_account_provider", return_value=FakeProvider()),
            patch.object(main_popup.webbrowser, "open", return_value=False),
            patch.object(main_popup.threading, "Thread", ImmediateThread),
        ):
            panel._on_chatgpt_retry()
        self.assertEqual(cancelled, [True])
        self.assertIn("could not open", panel._chatgpt_status.text().lower())

        panel._chatgpt_action_busy = False
        panel._set_chatgpt_ui_state("error")
        with patch.object(main_popup.threading, "Thread") as thread:
            panel._on_chatgpt_retry()
            panel._on_chatgpt_retry()
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
        panel.deleteLater()

    def test_switching_keeps_openai_unchanged_and_connect_signal_single(self):
        from unittest.mock import MagicMock
        handler = MagicMock()
        panel, save_setting, _settings = self._build_panel(connect_handler=handler)
        for _ in range(4):
            self._select(panel, "openai")
            self.assertTrue(panel._chatgpt_card.isHidden())
            self.assertFalse(panel._provider_cards["openai"].isHidden())
            self.assertEqual(
                panel._provider_cards["openai"]._key_input.echoMode(),
                panel._provider_cards["openai"]._key_input.EchoMode.Password,
            )
            self._select(panel, "chatgpt_account")
            self.assertFalse(panel._chatgpt_card.isHidden())
            self.assertTrue(panel._provider_cards["openai"].isHidden())
        account_cards = [
            frame for frame in panel.findChildren(type(panel._chatgpt_card))
            if frame.property("settingsEntry") == "chatgpt_account"
        ]
        self.assertEqual(len(account_cards), 1)
        panel._set_chatgpt_ui_state("disconnected")
        panel._chatgpt_connect_btn.click()
        self.app.processEvents()
        handler.assert_called_once()
        save_setting.assert_not_called()
        panel.deleteLater()

    def test_account_is_a_normal_freely_positioned_hierarchy_entry(self):
        from osia.gui import main_popup
        panel, save_setting, _settings = self._build_panel()
        with patch.object(main_popup, "set_setting", save_setting):
            before = copy.deepcopy(panel._hier_values)
            panel._apply_chatgpt_status({
                "available": True,
                "runtimeAvailable": True,
                "connected": True,
                "account": {"type": "chatgpt", "email": None, "planType": "plus"},
                "models": [{"id": "m1", "displayName": "M1"}],
                "selectedModel": "m1",
                "error": "",
            })
            self.assertFalse(hasattr(panel, "_chatgpt_hierarchy_pinned"))
            self.assertEqual(panel._hier_values, before)
            save_setting.assert_not_called()
            values = [value for _label, value in panel._hier_model_choices()]
            self.assertIn("chatgpt_account:m1", values)

            panel._hier_values["heavy"] = [
                "openai:a", "chatgpt_account:m1", "anthropic:b",
            ]
            panel._render_hier_category("heavy")
            self.assertEqual(
                [combo.currentData() for combo in panel._hier_rows["heavy"]],
                ["openai:a", "chatgpt_account:m1", "anthropic:b"],
            )
            panel._on_hier_move("heavy", 1, -1)
            self.assertEqual(
                panel._hier_values["heavy"],
                ["chatgpt_account:m1", "openai:a", "anthropic:b"],
            )
            save_setting.assert_called_once_with(
                "ai_hierarchy",
                {
                    "heavy": ["chatgpt_account:m1", "openai:a", "anthropic:b"],
                    "medium": ["groq:c"],
                },
            )
            self.assertEqual(main_popup.POPUP_W, 440)
            self.assertEqual(main_popup.POPUP_W_WIDE, 640)
            self.assertIn("openai", panel._provider_cards)
            panel._chatgpt_action_busy = True
            panel._set_chatgpt_ui_state("connecting")
            self.assertFalse(panel._chatgpt_cancel_btn.isHidden())
            panel._chatgpt_action_busy = False
            panel._set_chatgpt_ui_state("disconnected")
            self.assertFalse(panel._chatgpt_connect_btn.isHidden())
            panel._chatgpt_status.setText("safe error")
            panel._set_chatgpt_ui_state("error")
            self.assertFalse(panel._chatgpt_retry_btn.isHidden())
            panel.deleteLater()

    def test_connect_disconnect_and_restart_preserve_exact_hierarchy(self):
        panel, save_setting, settings = self._build_panel()
        expected = copy.deepcopy(panel._hier_values)
        connected = {
            "available": True, "runtimeAvailable": True, "connected": True,
            "account": {"type": "chatgpt", "planType": "plus"},
            "models": [{"id": "m1", "displayName": "M1"}],
            "selectedModel": "m1", "error": "",
        }
        panel._apply_chatgpt_status(connected)
        self.assertEqual(panel._hier_values, expected)
        panel._apply_chatgpt_status({
            "available": False, "runtimeAvailable": True, "connected": False,
            "models": [], "error": "disconnected",
        })
        self.assertEqual(panel._hier_values, expected)
        save_setting.assert_not_called()
        panel.deleteLater()

        restarted, restarted_save, _ = self._build_panel()
        self.assertEqual(restarted._hier_values, {
            "heavy": settings["ai_hierarchy"]["heavy"],
            "medium": settings["ai_hierarchy"]["medium"],
        })
        restarted_save.assert_not_called()
        restarted.deleteLater()

    def test_priority_wording_is_absent_in_all_languages(self):
        from osia import i18n
        for language in ("en", "fr", "zh", "ja", "de", "es"):
            tooltip = i18n.TRANSLATIONS["chatgpt_account.info_tooltip"][language]
            lowered = tooltip.lower()
            self.assertNotIn("always first", lowered)
            self.assertNotIn("before api", lowered)
        self.assertNotIn("chatgpt_account.priority", i18n.TRANSLATIONS)
        self.assertNotIn("chatgpt_account.always_first", i18n.TRANSLATIONS)
        self.assertNotIn("chatgpt_account.always_first_sub", i18n.TRANSLATIONS)

    @staticmethod
    def _model_rows(panel):
        from osia.gui import main_popup
        return [
            panel._host_layout.itemAt(index).widget()
            for index in range(panel._host_layout.count() - 1)
            if isinstance(
                panel._host_layout.itemAt(index).widget(), main_popup._ModelRow
            )
        ]

    @staticmethod
    def _grid_cards(panel):
        grid_host = panel._host_layout.itemAt(0).widget()
        grid = grid_host.layout()
        return [
            grid.itemAt(index).widget()
            for index in range(grid.count())
        ]

    @classmethod
    def _grid_providers(cls, panel):
        return [card._provider for card in cls._grid_cards(panel)]

    def test_models_page_omits_disconnected_and_unusable_account_without_gap(self):
        from osia.gui import main_popup
        api_models = [
            {"id": "a", "provider": "anthropic", "label": "A"},
            {"id": "o", "provider": "openai", "label": "O"},
        ]
        disconnected = {
            "available": False, "connected": False, "models": [],
            "error": "disconnected",
        }
        unusable = {
            "available": True, "connected": True, "error": "",
            "models": [
                {"id": "hidden", "hidden": True, "inputModalities": ["text"]},
                {"id": "image", "inputModalities": ["image"]},
            ],
        }
        terminal_error = {
            "available": True, "connected": True,
            "models": [{"id": "stale", "inputModalities": ["text"]}],
            "error": "runtime_missing",
        }
        with (
            patch.object(
                main_popup, "load_api_keys",
                return_value={"anthropic": "key", "openai": "key"},
            ),
            patch.object(main_popup, "get_all_models", return_value=api_models),
        ):
            panel = main_popup._ModelPanel()
            for status in (disconnected, unusable, terminal_error):
                panel.set_account_status(status)
                providers = self._grid_providers(panel)
                self.assertEqual(providers, ["anthropic", "openai"])
                self.assertNotIn("chatgpt_account", providers)
                grid = panel._host_layout.itemAt(0).widget().layout()
                self.assertEqual(
                    [(grid.getItemPosition(i)[0], grid.getItemPosition(i)[1])
                     for i in range(grid.count())],
                    [(0, 0), (0, 1)],
                )
                visible_text = "\n".join(
                    label.text() for label in panel.findChildren(main_popup.QLabel)
                )
                self.assertNotIn(
                    main_popup.i18n.t("models.account_connect_required"),
                    visible_text,
                )
            panel.deleteLater()

    def test_models_page_account_card_uses_compact_singular_and_plural_count(self):
        from osia.gui import main_popup
        api_models = [{"id": "api", "provider": "openai", "label": "API"}]
        with (
            patch.object(main_popup, "load_api_keys", return_value={"openai": "key"}),
            patch.object(main_popup, "get_all_models", return_value=api_models),
        ):
            panel = main_popup._ModelPanel()
            for model_ids, expected in ((["one"], "1 model"),
                                        (["one", "two"], "2 models")):
                panel.set_account_status({
                    "available": True, "connected": True, "error": "",
                    "models": [
                        {"id": model_id, "displayName": model_id.title(),
                         "inputModalities": ["text"]}
                        for model_id in model_ids
                    ],
                    "selectedModel": model_ids[0],
                })
                cards = self._grid_cards(panel)
                account_cards = [
                    card for card in cards if card._provider == "chatgpt_account"
                ]
                self.assertEqual(len(account_cards), 1)
                account_card = account_cards[0]
                labels = {
                    label.objectName(): label.text()
                    for label in account_card.findChildren(main_popup.QLabel)
                    if label.objectName()
                }
                self.assertEqual(labels["provGridName"], "ChatGPT")
                self.assertEqual(labels["provGridCount"], expected)
                api_card = next(card for card in cards if card._provider == "openai")
                self.assertEqual(
                    account_card.layout().contentsMargins(),
                    api_card.layout().contentsMargins(),
                )
                self.assertEqual(
                    account_card.sizeHint().height(), api_card.sizeHint().height()
                )
            panel.deleteLater()

    def test_models_page_account_population_selection_and_api_isolation(self):
        from unittest.mock import MagicMock
        from osia.gui import main_popup
        api_models = [{
            "id": "api-model", "provider": "openai", "label": "API Model",
        }]
        status = {
            "available": True, "connected": True, "error": "",
            "selectedModel": "account-b",
            "models": [
                {"id": "account-a", "displayName": "Account A",
                 "inputModalities": ["text"],
                 "supportedReasoningEfforts": [{"id": "low"}]},
                {"id": "account-b", "displayName": "Account B",
                 "inputModalities": ["text"],
                 "supportedReasoningEfforts": []},
                {"id": "hidden", "displayName": "Hidden", "hidden": True,
                 "inputModalities": ["text"]},
                {"id": "image-only", "displayName": "Image",
                 "inputModalities": ["image"]},
            ],
        }
        with (
            patch.object(main_popup, "load_api_keys", return_value={"openai": "key"}),
            patch.object(main_popup, "get_all_models", return_value=api_models),
        ):
            panel = main_popup._ModelPanel()
            panel.set_current("api-model", "openai")
            panel.set_account_status(status)
            providers = self._grid_providers(panel)
            self.assertEqual(providers[:2], ["chatgpt_account", "openai"])

            panel._show_provider("chatgpt_account")
            rows = self._model_rows(panel)
            self.assertEqual([row._mid for row in rows], ["account-a", "account-b"])
            self.assertFalse(any(row.property("active") for row in rows))

            account_provider = MagicMock()
            api_change = MagicMock()
            panel.model_changed.connect(api_change)
            with patch(
                "osia.chatgpt_account.provider.get_account_provider",
                return_value=account_provider,
            ):
                next(row for row in rows if row._mid == "account-a").selected.emit(
                    "account-a"
                )
            account_provider.set_selected_model.assert_called_once_with("account-a")
            api_change.assert_called_once_with("account-a", "chatgpt_account")
            self.assertEqual(panel._current_model, "api-model")
            self.assertEqual(panel._current_provider, "openai")
            self.assertEqual(panel._account_status["selectedModel"], "account-a")
            panel.deleteLater()

    def test_models_page_logout_reconnect_and_status_signal_refresh(self):
        from unittest.mock import MagicMock
        from osia.gui import main_popup
        api_models = [{
            "id": "api-model", "provider": "openai", "label": "API Model",
        }]
        with (
            patch.object(main_popup, "load_api_keys", return_value={"openai": "key"}),
            patch.object(main_popup, "get_all_models", return_value=api_models),
        ):
            models = main_popup._ModelPanel()
            models.set_current("api-model", "openai")
            api_change = MagicMock()
            models.model_changed.connect(api_change)
            settings, _save_setting, _settings = self._build_panel()
            settings.chatgpt_status_changed.connect(models.set_account_status)

            connected = {
                "available": True, "runtimeAvailable": True, "connected": True,
                "account": {"email": None, "planType": "plus"},
                "models": [{"id": "m1", "displayName": "M1",
                            "inputModalities": ["text"]}],
                "selectedModel": "m1", "error": "",
            }
            settings._apply_chatgpt_status(connected)
            models._show_provider("chatgpt_account")
            self.assertEqual([row._mid for row in self._model_rows(models)], ["m1"])
            account_rows = self._model_rows(models)

            settings._apply_chatgpt_status({
                "available": False, "runtimeAvailable": True,
                "connected": False,
                "models": [{"id": "stale", "inputModalities": ["text"]}],
                "error": "disconnected",
            })
            self.app.processEvents()
            self.assertTrue(all(row.isHidden() for row in account_rows))
            self.assertEqual(self._model_rows(models), [])
            self.assertIsNone(models._view)
            self.assertEqual(self._grid_providers(models), ["openai"])
            self.assertEqual(models._current_model, "api-model")
            api_change.assert_not_called()

            settings._apply_chatgpt_status(connected)
            settings._apply_chatgpt_status(connected)
            self.app.processEvents()
            self.assertEqual(
                self._grid_providers(models).count("chatgpt_account"), 1
            )
            models._show_provider("chatgpt_account")
            self.assertEqual([row._mid for row in self._model_rows(models)], ["m1"])
            models.deleteLater()
            settings.deleteLater()

    def test_account_reasoning_is_namespaced_and_restored(self):
        from osia.gui import main_popup
        status = {
            "available": True, "connected": True, "selectedModel": "reasoning",
            "models": [
                {"id": "reasoning", "displayName": "Reasoning",
                 "inputModalities": ["text"],
                 "supportedReasoningEfforts": [{"id": "low"}, {"id": "high"}]},
                {"id": "plain", "displayName": "Plain",
                 "inputModalities": ["text"],
                 "supportedReasoningEfforts": []},
            ],
        }
        initial = {
            "default": "auto",
            "providers": {"openai": "medium", "deepseek": "high"},
            "models": {"api-model": "low", "chatgpt_account:reasoning": "low"},
        }
        saved = []
        with (
            patch.object(main_popup, "load_api_keys", return_value={}),
            patch.object(main_popup, "get_all_models", return_value=[]),
            patch.object(main_popup, "get_reasoning_settings", return_value=copy.deepcopy(initial)),
            patch.object(main_popup, "set_reasoning_settings", side_effect=saved.append),
        ):
            panel = main_popup._ModelPanel()
            panel.set_account_status(status)
            panel._show_provider("chatgpt_account")
            rows = {row._mid: row for row in self._model_rows(panel)}
            self.assertIsNotNone(rows["reasoning"]._reasoning_btn)
            self.assertIsNone(rows["plain"]._reasoning_btn)
            self.assertEqual(rows["reasoning"]._explicit_reasoning_option_id(), "low")
            rows["reasoning"]._set_reasoning_option("high")

        self.assertEqual(saved[-1]["models"]["chatgpt_account:reasoning"], "high")
        self.assertEqual(saved[-1]["models"]["api-model"], "low")
        self.assertEqual(saved[-1]["providers"], initial["providers"])
        restored = copy.deepcopy(saved[-1])
        with patch.object(main_popup, "get_reasoning_settings", return_value=restored):
            row = main_popup._ModelRow(
                {"id": "reasoning", "provider": "chatgpt_account",
                 "label": "Reasoning"},
                is_active=True,
                reasoning_options=[
                    {"id": "auto", "label_key": "reasoning.default", "default": True},
                    {"id": "high", "label_key": "reasoning.high", "default": False},
                ],
            )
            self.assertEqual(row._explicit_reasoning_option_id(), "high")
            self.assertFalse(row._reasoning_chip.isHidden())
            row.deleteLater()
        panel.deleteLater()

    def test_missing_saved_account_model_persists_deterministic_replacement(self):
        from unittest.mock import MagicMock
        from osia.gui import main_popup
        provider = MagicMock()
        provider.status.return_value = {
            "available": True, "connected": True,
            "models": [{"id": "default"}], "selectedModel": "default",
        }
        provider.rate_limits.return_value = {}
        with patch.object(main_popup, "get_setting", return_value="missing"):
            status = main_popup._SettingsPanel._chatgpt_status_with_usage(provider)
        self.assertEqual(status["selectedModel"], "default")
        provider.set_selected_model.assert_called_once_with("default")

    def test_models_page_refresh_listener_stays_single_across_visits(self):
        from osia.config import DEFAULT_SETTINGS
        from osia.gui import main_popup
        with (
            patch.object(
                main_popup, "load_settings",
                return_value=copy.deepcopy(DEFAULT_SETTINGS),
            ),
            patch.object(main_popup, "load_api_keys", return_value={}),
            patch.object(main_popup, "autostart_enabled", return_value=False),
            patch.object(main_popup, "set_setting"),
            patch.object(
                main_popup._SettingsPanel, "_probe_chatgpt_account",
                lambda _self: None,
            ),
        ):
            popup = main_popup.MainPopup()
            panel = popup._models
            self.assertEqual(panel.receivers(panel.account_refresh_requested), 1)
            for _ in range(3):
                popup._on_nav_clicked(popup.PAGE_MODELS)
                popup.show_chat()
            self.assertEqual(panel.receivers(panel.account_refresh_requested), 1)
            popup._settings._apply_chatgpt_status({
                "available": False, "runtimeAvailable": True,
                "connected": False, "models": [], "error": "disconnected",
            })
            self.assertEqual(panel._account_status["error"], "disconnected")
            popup.deleteLater()

    def test_openai_api_registry_remains_separate(self):
        from osia.models import API_PROVIDERS, PROVIDERS
        self.assertIn("openai", API_PROVIDERS)
        self.assertNotIn("chatgpt_account", API_PROVIDERS)
        self.assertEqual(PROVIDERS["openai"]["name"], "OpenAI")
        self.assertEqual(PROVIDERS["openai"]["base_url"], "https://api.openai.com/v1")


if __name__ == "__main__":
    unittest.main()
