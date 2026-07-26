from __future__ import annotations

import copy
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from osia import mobile_model_catalogue as catalogue


def _raw(provider: str, model_id: str, **extra) -> dict:
    return {
        "provider": provider,
        "id": model_id,
        "label": extra.pop("label", model_id),
        **extra,
    }


def _account(*, connected: bool = True, available: bool = True,
             error: str = "", models=None) -> dict:
    return {
        "runtimeAvailable": True,
        "connected": connected,
        "available": available,
        "error": error,
        "models": list(models or []),
    }


class CanonicalInventoryTests(unittest.TestCase):
    def test_api_configuration_matrix_and_local_detection(self):
        raw = [
            _raw("openai", "shared"),
            _raw("deepseek", "shared"),
            _raw("ollama", "local:latest"),
        ]
        self.assertEqual(catalogue.build_model_inventory(raw, set(), None)[0][
            "provider_id"
        ], "ollama")
        one = catalogue.build_model_inventory(raw, {"openai"}, None)
        self.assertEqual(
            [(item["provider_id"], item["model_id"]) for item in one],
            [("openai", "shared"), ("ollama", "local:latest")],
        )
        several = catalogue.build_model_inventory(
            raw, {"openai", "deepseek"}, None
        )
        self.assertEqual(len(several), 3)

    def test_fallback_only_key_uses_key_chain_predicate(self):
        with patch.object(
            catalogue, "has_key", side_effect=lambda provider: provider == "groq"
        ):
            configured = catalogue.configured_api_providers({})
        self.assertEqual(configured, {"groq"})

    def test_account_visibility_rules(self):
        model = {
            "id": "account-model",
            "displayName": "Account Model",
            "inputModalities": ["text"],
            "supportedReasoningEfforts": [{"id": "high"}],
        }
        visible = catalogue.build_model_inventory(
            [], set(), _account(models=[model])
        )
        self.assertEqual(
            [(item["provider_id"], item["model_id"]) for item in visible],
            [("chatgpt_account", "account-model")],
        )
        for status in (
            _account(connected=False, available=False, error="disconnected", models=[model]),
            _account(available=False, models=[model]),
            _account(error="provider_error", models=[model]),
            _account(models=[]),
            {**_account(models=[model]), "runtimeAvailable": False},
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    catalogue.build_model_inventory([], set(), status), []
                )

    def test_mixed_account_dedup_and_cross_provider_duplicate_ids(self):
        account_model = {"id": "shared", "inputModalities": ["text"]}
        inventory = catalogue.build_model_inventory(
            [
                _raw("openai", "shared"),
                _raw("openai", "shared", label="duplicate"),
                _raw("deepseek", "shared"),
            ],
            {"openai", "deepseek"},
            _account(models=[account_model, dict(account_model)]),
        )
        self.assertEqual(
            [(item["provider_id"], item["model_id"]) for item in inventory],
            [
                ("chatgpt_account", "shared"),
                ("openai", "shared"),
                ("deepseek", "shared"),
            ],
        )
        self.assertEqual(len({item["stable_key"] for item in inventory}), 3)

    def test_inventory_whitelists_fields_and_never_leaks_credentials(self):
        inventory = catalogue.build_model_inventory(
            [{
                **_raw("openai", "gpt"),
                "api_key": "must-not-leak",
                "accessToken": "must-not-leak",
                "workspace_dir": "C:/private/path",
                "account": {"raw": "must-not-leak"},
            }],
            {"openai"},
            None,
        )
        encoded = json.dumps(inventory).lower()
        for forbidden in (
            "must-not-leak", "api_key", "accesstoken", "workspace_dir",
            "c:/private/path", '"account"',
        ):
            self.assertNotIn(forbidden, encoded)


class AutonomousSelectionTests(unittest.TestCase):
    def setUp(self):
        self.inventory = catalogue.build_model_inventory(
            [
                _raw("openai", "shared"),
                _raw("deepseek", "shared"),
                _raw("groq", "unique"),
            ],
            {"openai", "deepseek", "groq"},
            None,
        )

    def test_exact_saved_pair_and_main_seed_are_preserved(self):
        exact = catalogue.resolve_autonomous_selection(
            self.inventory,
            autonomous_provider="deepseek",
            autonomous_model="shared",
        )
        self.assertEqual(
            (exact["provider_id"], exact["model_id"]),
            ("deepseek", "shared"),
        )
        self.assertFalse(exact["fallback_occurred"])
        seeded = catalogue.resolve_autonomous_selection(
            self.inventory,
            main_provider="openai",
            main_model="shared",
        )
        self.assertEqual(seeded["reason"], "seeded_from_main")
        self.assertEqual(seeded["provider_id"], "openai")

    def test_legacy_bare_migration_is_unique_only(self):
        unique = catalogue.resolve_autonomous_selection(
            self.inventory, autonomous_model="unique"
        )
        self.assertEqual(unique["provider_id"], "groq")
        self.assertEqual(unique["reason"], "legacy_model_migrated")
        ambiguous = catalogue.resolve_autonomous_selection(
            self.inventory, autonomous_model="shared"
        )
        self.assertTrue(ambiguous["fallback_occurred"])
        self.assertEqual(ambiguous["reason"], "legacy_model_ambiguous")
        unknown = catalogue.resolve_autonomous_selection(
            self.inventory, autonomous_model="missing"
        )
        self.assertTrue(unknown["fallback_occurred"])
        self.assertEqual(unknown["reason"], "legacy_model_unavailable")

    def test_wire_identity_supports_model_ids_containing_colons(self):
        inventory = catalogue.build_model_inventory(
            [_raw("ollama", "family:latest")], set(), None
        )
        exact = catalogue.resolve_autonomous_selection(
            inventory, wire_value="ollama:family:latest"
        )
        self.assertEqual(exact["provider_id"], "ollama")
        self.assertEqual(exact["model_id"], "family:latest")

    def test_stale_fallback_is_explicit_and_empty_inventory_fails_truthfully(self):
        stale = catalogue.resolve_autonomous_selection(
            self.inventory,
            autonomous_provider="openai",
            autonomous_model="removed",
        )
        self.assertTrue(stale["fallback_occurred"])
        self.assertEqual(stale["reason"], "saved_selection_unavailable")
        empty = catalogue.resolve_autonomous_selection(
            [], autonomous_provider="openai", autonomous_model="removed"
        )
        self.assertIsNone(empty["entry"])
        self.assertEqual(empty["reason"], "no_models_available")

    def test_runtime_application_passes_exact_provider_and_model(self):
        class Agent:
            def __init__(self):
                self.calls = []

            def set_model(self, model, provider=None):
                self.calls.append((provider, model))

        agent = Agent()
        catalogue.apply_autonomous_agent_model(agent, "deepseek", "shared")
        self.assertEqual(agent.calls, [("deepseek", "shared")])

    def test_runtime_resolution_persists_only_autonomous_pair(self):
        writes = []
        with (
            patch.object(
                catalogue,
                "build_catalogue",
                return_value={"inventory": self.inventory},
            ),
            patch.object(
                catalogue,
                "load_settings",
                return_value={
                    "selected_model_provider": "openai",
                    "selected_model": "shared",
                },
            ),
            patch.object(
                catalogue,
                "set_setting",
                side_effect=lambda key, value: writes.append((key, value)),
            ),
        ):
            resolved = catalogue.resolve_current_autonomous_selection(
                provider_id="deepseek", model_id="shared"
            )
        self.assertEqual(resolved["provider_id"], "deepseek")
        self.assertEqual(writes, [
            ("autonomous_model_provider", "deepseek"),
            ("autonomous_model", "shared"),
        ])

    def test_runtime_legacy_model_does_not_inherit_saved_provider(self):
        with (
            patch.object(
                catalogue,
                "build_catalogue",
                return_value={"inventory": self.inventory},
            ),
            patch.object(
                catalogue,
                "load_settings",
                return_value={
                    "autonomous_model_provider": "deepseek",
                    "autonomous_model": "shared",
                },
            ),
            patch.object(catalogue, "set_setting"),
        ):
            resolved = catalogue.resolve_current_autonomous_selection(
                provider_id="", model_id="unique"
            )
        self.assertEqual(resolved["provider_id"], "groq")
        self.assertEqual(resolved["model_id"], "unique")
        self.assertEqual(resolved["reason"], "legacy_model_migrated")


class PcSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_standard_and_autonomous_panels_receive_identical_pairs(self):
        from osia.gui.main_popup import _AutonomousPanel, _ModelPanel

        inventory = catalogue.build_model_inventory(
            [_raw("openai", "same"), _raw("deepseek", "same")],
            {"openai", "deepseek"},
            None,
        )
        resolution = catalogue.resolve_autonomous_selection(
            inventory,
            autonomous_provider="deepseek",
            autonomous_model="same",
        )
        standard = _ModelPanel()
        autonomous = _AutonomousPanel()
        standard.set_inventory(inventory)
        autonomous.set_inventory(inventory, resolution)
        standard_pairs = {
            (entry["provider_id"], entry["model_id"])
            for entry in standard._inventory
        }
        autonomous_pairs = {
            tuple(autonomous._model_combo.itemData(index))
            for index in range(autonomous._model_combo.count())
        }
        self.assertEqual(standard_pairs, autonomous_pairs)
        grouped = {}
        for entry in inventory:
            grouped.setdefault(entry["provider_id"], []).append(entry)
        serialized_pairs = {
            (model["providerId"], model["id"])
            for provider_id in catalogue.PROVIDER_ORDER
            if provider_id in grouped
            for model in catalogue._provider_entry(
                provider_id, grouped[provider_id]
            )["models"]
        }
        self.assertEqual(standard_pairs, serialized_pairs)
        standard.deleteLater()
        autonomous.deleteLater()

    def test_start_emits_exact_pair_and_empty_inventory_disables_start(self):
        from osia.gui.main_popup import _AutonomousPanel

        panel = _AutonomousPanel()
        panel.set_inventory([], catalogue.resolve_autonomous_selection([]))
        self.assertFalse(panel._start_btn.isEnabled())

        inventory = catalogue.build_model_inventory(
            [_raw("openai", "same"), _raw("deepseek", "same")],
            {"openai", "deepseek"},
            None,
        )
        resolution = catalogue.resolve_autonomous_selection(
            inventory,
            autonomous_provider="deepseek",
            autonomous_model="same",
        )
        panel.set_inventory(inventory, resolution)
        panel._objective_edit.setPlainText("Harmless objective")
        emitted = []
        panel.start_requested.connect(
            lambda objective, provider, model: emitted.append(
                (objective, provider, model)
            )
        )
        panel._on_start()
        self.assertEqual(
            emitted,
            [("Harmless objective", "deepseek", "same")],
        )
        panel.deleteLater()

    def test_key_and_account_changes_refresh_both_pc_selectors(self):
        from osia.config import DEFAULT_SETTINGS
        from osia.gui import main_popup

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        key_state = {}
        raw_models = [_raw("openai", "api-model")]
        account_model = {
            "id": "account-model",
            "displayName": "Account Model",
            "inputModalities": ["text"],
        }
        with (
            patch.object(main_popup, "load_settings", return_value=settings),
            patch.object(
                main_popup, "load_api_keys", side_effect=lambda: dict(key_state)
            ),
            patch.object(main_popup, "get_all_models", return_value=raw_models),
            patch.object(
                main_popup,
                "configured_api_providers",
                side_effect=lambda keys: {
                    provider for provider, value in (keys or {}).items() if value
                },
            ),
            patch.object(main_popup, "autostart_enabled", return_value=False),
            patch.object(main_popup, "set_setting"),
            patch.object(
                main_popup._SettingsPanel,
                "_probe_chatgpt_account",
                lambda _self, force=False: None,
            ),
        ):
            popup = main_popup.MainPopup()
            key_state["openai"] = "configured"
            popup._on_api_keys_saved({"openai": "configured"})
            self.assertEqual(
                {
                    (entry["provider_id"], entry["model_id"])
                    for entry in popup._models._inventory
                },
                {("openai", "api-model")},
            )
            self.assertEqual(
                popup._models._inventory,
                popup._autonomous._inventory,
            )

            popup._on_chatgpt_status_changed(
                _account(models=[account_model])
            )
            self.assertEqual(
                {
                    (entry["provider_id"], entry["model_id"])
                    for entry in popup._autonomous._inventory
                },
                {
                    ("chatgpt_account", "account-model"),
                    ("openai", "api-model"),
                },
            )
            popup._on_chatgpt_status_changed(
                _account(
                    connected=False,
                    available=False,
                    error="disconnected",
                    models=[account_model],
                )
            )
            self.assertEqual(
                {
                    (entry["provider_id"], entry["model_id"])
                    for entry in popup._models._inventory
                },
                {("openai", "api-model")},
            )
            key_state.clear()
            popup._on_api_keys_saved({})
            self.assertEqual(popup._models._inventory, [])
            self.assertEqual(popup._autonomous._inventory, [])
            popup.deleteLater()


class MobileServerAutonomousRuntimeTests(unittest.TestCase):
    class Agent:
        def __init__(self):
            self.calls = []
            self.provider = ""
            self.model = ""

        def set_turn_context(self, _context):
            pass

        def set_model(self, model, provider=None):
            self.provider = provider
            self.model = model
            self.calls.append((provider, model))

    class Orchestrator:
        def __init__(self):
            self.started = []
            self.resumed = []

        def start(self, state):
            self.started.append(state)

        def resume(self, state):
            self.resumed.append(state)

    def _resolution(self, provider="deepseek", model="shared"):
        return {
            "entry": {},
            "provider_id": provider,
            "model_id": model,
            "stable_key": f"{provider}:{model}",
            "fallback_occurred": False,
            "reason": "exact_selection",
        }

    def test_mobile_start_worker_uses_exact_wire_provider(self):
        from osia.server import server

        agent = self.Agent()
        orchestrator = self.Orchestrator()
        state = {
            "task_id": "task-start",
            "status": "in_progress",
            "provider": "deepseek",
            "model": "shared",
            "phases": [],
            "cursor": {"phase": 0, "task": 0},
        }
        original = dict(server._autonomous)
        server._autonomous.update({
            "active": True,
            "gen": 182,
            "interrupting": False,
            "st": None,
        })
        try:
            with (
                patch.object(
                    server,
                    "_ensure_autonomous_engine",
                    return_value=(agent, orchestrator),
                ),
                patch.object(server, "_orch_loop_alive", return_value=False),
                patch.object(
                    catalogue,
                    "resolve_current_autonomous_selection",
                    return_value=self._resolution(),
                ),
                patch.object(
                    server._aplanner, "create_plan_state", return_value=state
                ) as create_plan,
                patch.object(server._astate, "save_task_state"),
                patch.object(server, "_write_autonomous_lock"),
                patch.object(server, "_broadcast_autonomous"),
            ):
                server._autonomous_start_worker(
                    "Harmless objective", "deepseek:shared", 182, "en"
                )
        finally:
            server._autonomous.clear()
            server._autonomous.update(original)
        self.assertEqual(agent.calls, [("deepseek", "shared")])
        self.assertEqual(create_plan.call_args.args[1:3], ("deepseek", "shared"))
        self.assertEqual(orchestrator.started, [state])

    def test_mobile_resume_worker_uses_task_state_exact_pair(self):
        from osia.server import server

        agent = self.Agent()
        orchestrator = self.Orchestrator()
        state = {
            "task_id": "task-resume",
            "status": "in_progress",
            "provider": "openai",
            "model": "shared",
            "language": "en",
            "phases": [],
            "cursor": {"phase": 0, "task": 0},
        }
        original = dict(server._autonomous)
        server._autonomous.update({
            "active": True,
            "gen": 183,
            "interrupting": False,
            "st": None,
        })
        try:
            with (
                patch.object(
                    server,
                    "_ensure_autonomous_engine",
                    return_value=(agent, orchestrator),
                ),
                patch.object(server, "_orch_loop_alive", return_value=False),
                patch.object(server._astate, "load_task_state", return_value=state),
                patch.object(server._astate, "resume_state_is_valid", return_value=True),
                patch.object(server._astate, "save_task_state"),
                patch.object(
                    catalogue,
                    "resolve_current_autonomous_selection",
                    return_value=self._resolution("openai", "shared"),
                ),
                patch.object(server, "_write_autonomous_lock"),
                patch.object(server, "_broadcast_autonomous"),
            ):
                server._autonomous_resume_worker(183)
        finally:
            server._autonomous.clear()
            server._autonomous.update(original)
        self.assertEqual(agent.calls, [("openai", "shared")])
        self.assertEqual(orchestrator.resumed, [state])
        self.assertEqual((state["provider"], state["model"]), ("openai", "shared"))


if __name__ == "__main__":
    unittest.main()
