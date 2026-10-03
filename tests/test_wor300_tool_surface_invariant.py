"""WOR-300: complete, request-independent OSIA tool exposure.

The model-visible static surface is composed from genuine runtime
availability only.  Request wording, semantic classification, GoalContract,
ExecutionStrategy, domain and execution-frontier state never remove an
available capability; they remain execution-time gates.
"""

from __future__ import annotations

import os
import re
import sys

from osia import agent as agent_module
from osia import execution_strategy as es
from osia.chatgpt_account.app_server_client import _canonical_native_tools
from osia.computer_use_v4_integration import computer_use_v4_turn_scope
from osia.goal_contract_v2 import (
    goal_contract_v2_evaluation_scope,
    goal_contract_v2_from_model_proposal,
)
from osia.mcp_client import registry as mcp_registry
from osia.tool_contracts import (
    DORMANT_VISUAL_V3_TOOL_NAMES,
    effective_static_tool_specs,
    effective_tool_surface_scope,
    get_static_registry,
    static_tool_prompt_block,
)

CHAT_TOOLS = tuple(agent_module._active_chat_tool_names())
TRIVIAL = "Quelle est la capitale du Japon ?"
FILESYSTEM = (
    "Cree un fichier rapport.txt sur le Bureau avec le contenu bonjour, "
    "puis lis-le."
)
CODING = (
    "Cree-moi une application complete dans un nouveau dossier sur le Bureau"
)
DESKTOP = "Ouvre Chrome et clique sur le bouton Connexion"
COMPUTER_TOOLS = frozenset(
    name for name in CHAT_TOOLS if name.startswith("computer_")
)
FILESYSTEM_TOOLS = frozenset({
    "read_file", "write_file", "edit_file_lines", "create_directory",
    "delete_file", "delete_directory", "rename_file", "copy_file",
    "list_files", "search_files", "get_file_info", "render_pdf",
    "open_in_editor", "read_document", "read_document_artifact",
    "query_document",
})
SHELL_TOOLS = frozenset({"bash", "run_command"})
DESKTOP_TOOLS = frozenset({
    "inspect_desktop_surfaces", "activate_desktop_surface", "take_screenshot",
    "desktop_type", "desktop_press", "desktop_scroll",
})
SYSTEM_TOOLS = frozenset({
    "get_system_info", "list_running_processes", "kill_process", "set_volume",
    "get_clipboard", "set_clipboard",
})
MEMORY_SCHEDULER_TOOLS = frozenset({
    "save_memory", "create_scheduled_task", "list_scheduled_tasks",
    "update_scheduled_task", "delete_scheduled_task",
})
BROWSER_TOOLS = frozenset({
    "get_browser_state", "navigate_browser", "inspect_browser_page",
    "search_web", "open_browser", "navigate", "read_page",
})
GMAIL_TOOLS = frozenset({
    "gmail_list_labels", "gmail_list_latest", "gmail_search",
    "gmail_read_message", "gmail_create_draft", "gmail_send_draft",
})


def _v4_scope(provider: str = "openai", model: str = "gpt-5.4"):
    return computer_use_v4_turn_scope(
        provider,
        model,
        conversation_id="wor300-surface-test",
        turn_id="wor300-surface-test-turn",
    )


def _system_destructive_write_path() -> str:
    """Return the platform path the system-resource classifier protects."""

    if sys.platform == "win32":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        return os.path.join(root, "System32", "drivers", "etc", "hosts")
    if sys.platform == "darwin":
        return "/System/Library/CoreServices/SystemVersion.plist"
    return "/etc/hosts"


def _surface_for(request: str) -> set[str]:
    with es.execution_strategy_scope(request, CHAT_TOOLS):
        return {spec.name for spec in effective_static_tool_specs()}


def _runtime_available_chat_names() -> set[str]:
    """Availability-only expectation, independent of any strategy."""

    expected = set()
    for spec in get_static_registry().values():
        if spec.name.startswith("computer_"):
            continue  # no V4 vision-bound turn is active in the test runtime
        if spec.name in DORMANT_VISUAL_V3_TOOL_NAMES:
            continue  # dormant primitive: genuine runtime unavailability
        expected.add(spec.name)
    return expected


def test_trivial_and_complex_requests_expose_the_same_complete_set():
    trivial = _surface_for(TRIVIAL)
    filesystem = _surface_for(FILESYSTEM)
    coding = _surface_for(CODING)
    desktop = _surface_for(DESKTOP)

    assert trivial == filesystem == coding == desktop
    assert trivial == _runtime_available_chat_names()
    assert FILESYSTEM_TOOLS <= trivial
    assert SHELL_TOOLS <= trivial
    assert DESKTOP_TOOLS <= trivial
    assert SYSTEM_TOOLS <= trivial
    assert MEMORY_SCHEDULER_TOOLS <= trivial
    assert BROWSER_TOOLS <= trivial
    assert GMAIL_TOOLS <= trivial
    # V3 dormancy is genuine runtime unavailability, not relevance filtering.
    assert "request_visual_observation" not in trivial
    assert "desktop_click" not in trivial


def test_desktop_and_ui_tasks_keep_filesystem_and_shell_visible():
    for request in (DESKTOP, CODING, FILESYSTEM):
        names = _surface_for(request)
        assert FILESYSTEM_TOOLS <= names, request
        assert SHELL_TOOLS <= names, request


def test_text_only_questions_keep_desktop_and_browser_tools_visible():
    names = _surface_for(TRIVIAL)
    assert DESKTOP_TOOLS <= names
    assert BROWSER_TOOLS <= names
    assert SYSTEM_TOOLS <= names
    assert GMAIL_TOOLS <= names


def test_active_goal_contract_does_not_reduce_the_surface():
    proposal = {
        "goals": [{
            "goal_id": "build-app",
            "intent_family": "create_target",
            "semantic_target": "nouvelle application sur le Bureau",
            "required_application": "not_specified",
            "required_surface": "not_specified",
            "required_account": "not_specified",
            "desired_state": (
                "Une application complete existe dans un nouveau dossier "
                "sur le Bureau"
            ),
            "observable_postcondition": (
                "Le dossier et les fichiers de l'application existent"
            ),
            "explicit_constraints": [],
            "side_effect_class": "external_mutation",
            "depends_on": "",
            "conditional_mode": "",
            "branches": [],
            "completion_mode": "effect_event",
        }],
    }
    contract = goal_contract_v2_from_model_proposal(CODING, proposal)
    baseline = _surface_for(CODING)

    with goal_contract_v2_evaluation_scope(contract):
        with es.execution_strategy_scope(CODING, CHAT_TOOLS) as strategy:
            names = {spec.name for spec in effective_static_tool_specs()}

    assert strategy.mission.objective == CODING
    assert names == baseline
    assert FILESYSTEM_TOOLS <= names
    assert SHELL_TOOLS <= names


def test_active_execution_strategy_does_not_reduce_the_surface():
    baseline = _surface_for(TRIVIAL)
    for request in (TRIVIAL, FILESYSTEM, CODING, DESKTOP):
        strategy = es.build_execution_strategy(request, CHAT_TOOLS)
        with es.execution_strategy_scope(request, strategy=strategy):
            names = {spec.name for spec in effective_static_tool_specs()}
        assert names == baseline, request


def test_prompt_block_renders_exactly_the_complete_visible_surface():
    block = static_tool_prompt_block("en")
    rendered = {
        line.split("(", 1)[0][2:].strip()
        for line in block.splitlines()
        if line.startswith("- ") and "(" in line
    }
    assert rendered == {spec.name for spec in effective_static_tool_specs()}


def test_engine_surfaces_expose_the_same_complete_inventory():
    reference = None
    # WOR-303: there is exactly one execution surface plus the Scheduled
    # temporal trigger; no engine-specific tool policy remaps the inventory.
    for surface in ("", "scheduled"):
        with effective_tool_surface_scope(surface):
            names = {spec.name for spec in effective_static_tool_specs()}
        if reference is None:
            reference = names
        assert names == reference, surface
    assert reference == _runtime_available_chat_names()


def test_engine_surface_prompt_renders_the_complete_inventory():
    for surface in ("", "scheduled"):
        with effective_tool_surface_scope(surface):
            prompt = agent_module.get_system_prompt(
                "en", "fixture-model", "deepseek",
            )
        rendered = set(re.findall(
            r"^- ([a-z0-9_]+)\(", prompt, re.MULTILINE,
        ))
        assert rendered == _runtime_available_chat_names(), surface


def test_chatgpt_account_native_surface_is_request_independent():
    import sys

    requests = (TRIVIAL, FILESYSTEM, CODING, DESKTOP)
    strategies = {
        request: es.build_execution_strategy(request, CHAT_TOOLS)
        for request in requests
    }

    with _v4_scope("openai", "gpt-5.4"):
        with es.execution_strategy_scope(TRIVIAL):
            reference = tuple(
                item["name"] for item in _canonical_native_tools()
            )
        assert "write_file" in reference
        if sys.platform == "win32":
            assert COMPUTER_TOOLS <= set(reference)
        else:
            assert not (COMPUTER_TOOLS & set(reference))
        for request in requests:
            with es.execution_strategy_scope(
                request, strategy=strategies[request],
            ):
                names = tuple(
                    item["name"]
                    for item in _canonical_native_tools(strategies[request])
                )
            assert names == reference, request

    with es.execution_strategy_scope(TRIVIAL):
        without_v4 = tuple(item["name"] for item in _canonical_native_tools())
    assert not (COMPUTER_TOOLS & set(without_v4))
    for request in requests:
        with es.execution_strategy_scope(
            request, strategy=strategies[request],
        ):
            names = tuple(item["name"] for item in _canonical_native_tools())
        assert names == without_v4, request


def test_native_mutation_stays_visible_and_is_denied_before_dispatch(tmp_path):
    from osia.action_policy import guard_tool_call
    from osia.system_resources import classify_non_deletion_system_mutation

    strategy = es.build_execution_strategy(CODING, CHAT_TOOLS)
    with _v4_scope():
        native = {item["name"] for item in _canonical_native_tools(strategy)}
    assert "write_file" in native

    # Project file operations are not host-destruction primitives.
    ordinary = str(tmp_path / "wor300-native-ordinary.txt")
    decision = es.tool_preflight(
        "write_file",
        {"path": ordinary, "content": "fixture"},
        strategy=strategy,
    )
    assert decision is not None and decision.allowed is True

    system_path = _system_destructive_write_path()
    system_evidence = classify_non_deletion_system_mutation(
        "write_file", {"path": system_path, "content": "fixture"},
    )
    assert system_evidence is None or not system_evidence.blocks_destruction
    assert guard_tool_call(
        CODING, "write_file", {"path": system_path, "content": "fixture"},
    ) is None
    assert guard_tool_call(
        CODING, "write_file", {"path": ordinary, "content": "fixture"},
    ) is None


def test_all_connected_mcp_tools_are_listed_without_any_cap():
    server_id = "wor300_bulk"
    manager = _McpManager()
    registered = []
    try:
        registered = mcp_registry.register_server_tools(
            manager,
            server_id,
            [
                {
                    "name": f"bulk_{index:03d}",
                    "description": f"Bulk fixture tool {index}",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                    "annotations": {"readOnlyHint": True},
                }
                for index in range(73)
            ],
        )
        assert len(registered) == 73
        block = mcp_registry.mcp_tool_prompt_block()
        for name in registered:
            assert f"- {name}:" in block
        assert "more connected MCP tools" not in block
        prompt = agent_module.get_system_prompt(
            "en", "fixture-model", "deepseek",
        )
        for name in registered:
            assert f"- {name}:" in prompt
    finally:
        if registered:
            mcp_registry.unregister_server_tools(server_id)

    assert mcp_registry.mcp_tool_prompt_block() == ""


def test_computer_use_visibility_tracks_only_runtime_vision_availability():
    import sys

    baseline = _surface_for(TRIVIAL)
    assert not (COMPUTER_TOOLS & baseline)

    with _v4_scope():
        for request in (TRIVIAL, FILESYSTEM, CODING, DESKTOP):
            names = _surface_for(request)
            if sys.platform == "win32":
                assert COMPUTER_TOOLS <= names, request
            else:
                assert not (COMPUTER_TOOLS & names), request

    with _v4_scope("groq", "llama-3.3-70b-versatile"):
        for request in (TRIVIAL, CODING):
            names = _surface_for(request)
            assert not (COMPUTER_TOOLS & names), request


def test_computer_use_is_absent_on_non_windows_platforms(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    with _v4_scope():
        for request in (TRIVIAL, CODING, DESKTOP):
            names = _surface_for(request)
            assert not (COMPUTER_TOOLS & names), request


def test_visible_capability_is_still_guarded_before_dispatch(tmp_path):
    from osia.action_policy import guard_tool_call
    from osia.system_resources import classify_non_deletion_system_mutation

    strategy = es.build_execution_strategy(CODING, CHAT_TOOLS)
    with es.execution_strategy_scope(CODING, strategy=strategy):
        names = {spec.name for spec in effective_static_tool_specs()}
        assert FILESYSTEM_TOOLS <= names
        ordinary = str(tmp_path / "wor300-ordinary.txt")
        decision = es.tool_preflight(
            "write_file",
            {"path": ordinary, "content": "fixture"},
            strategy=strategy,
        )
    # Relevance never denies an ordinary write; a system-looking path on a
    # single-file write is not catastrophic host destruction.
    assert decision is not None and decision.allowed is True

    # Probe the classifier first: file path classification is not a host-wide
    # destruction decision.
    system_path = _system_destructive_write_path()
    system_evidence = classify_non_deletion_system_mutation(
        "write_file", {"path": system_path, "content": "fixture"},
    )
    ordinary_evidence = classify_non_deletion_system_mutation(
        "write_file", {"path": ordinary, "content": "fixture"},
    )
    assert system_evidence is None or not system_evidence.blocks_destruction
    assert ordinary_evidence is None or not ordinary_evidence.blocks_destruction
    assert guard_tool_call(
        CODING, "write_file", {"path": system_path, "content": "fixture"},
    ) is None
    assert guard_tool_call(
        CODING, "write_file", {"path": ordinary, "content": "fixture"},
    ) is None


def test_surface_composition_never_reads_permission_state():
    """WOR-301: no permission module or state exists in the composition path."""

    import importlib.util

    from osia import action_policy

    for name in ("permission_store", "permission_contract", "permission_policy"):
        assert importlib.util.find_spec(f"osia.{name}") is None, name
    for removed in (
        "PermissionPolicy", "PermissionStore", "PermissionMode",
        "build_permission_request", "evaluate_permission",
        "get_default_permission_policy", "set_default_permission_policy",
        "set_reinforced_deletion_confirmation_provider",
    ):
        assert not hasattr(action_policy, removed), removed
    assert _surface_for(TRIVIAL) == _runtime_available_chat_names()


def test_terminality_does_not_strip_available_capabilities_from_prompt():
    probe = object.__new__(agent_module.OSIAAgent)
    probe.lang = "en"
    probe.model = "fixture-model"
    probe.provider = "deepseek"
    probe.turn_context = None
    probe._terminality_controller = object()

    prompt = probe._system_prompt_for_current_turn()

    assert prompt == agent_module.get_system_prompt(
        "en", "fixture-model", "deepseek",
    )
    assert "- start_autonomous_mission(" not in prompt
    assert "- write_file(" in prompt
    assert "- bash(" in prompt


class _McpManager:
    def __init__(self):
        self.calls = []

    def call_tool(self, server_id, tool_name, arguments, *, timeout):
        self.calls.append((server_id, tool_name, arguments, timeout))
        return "ok"


def test_non_native_osia_capability_remains_text_callable_for_every_provider():
    text = (
        '<tool_call>{"tool":"write_file","path":"C:/fixture/a.txt",'
        '"content":"fixture"}</tool_call>'
    )
    for provider in ("deepseek", "groq", "chatgpt_account", "openai"):
        calls = agent_module._extract_tool_calls(text, provider=provider)
        assert calls and calls[0]["tool"] == "write_file", provider
    assert "write_file" in agent_module.TOOL_MAP
    assert "bash" in agent_module.TOOL_MAP


def test_connected_mcp_tools_stay_visible_and_disconnected_tools_leave():
    server_id = "wor300_fixture"
    manager = _McpManager()
    registered = []
    try:
        registered = mcp_registry.register_server_tools(manager, server_id, [{
            "name": "lookup",
            "description": "Lookup fixture state",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            "annotations": {"readOnlyHint": True},
        }])
        assert registered == ["mcp__wor300_fixture__lookup"]
        full = registered[0]

        for request in (TRIVIAL, CODING, DESKTOP):
            with es.execution_strategy_scope(request, CHAT_TOOLS):
                prompt = agent_module.get_system_prompt(
                    "en", "fixture-model", "deepseek",
                )
            assert f"- {full}:" in prompt, request
        assert full in agent_module.KNOWN_TOOL_NAMES
    finally:
        if registered:
            mcp_registry.unregister_server_tools(server_id)

    assert full not in mcp_registry.mcp_tool_prompt_block()
    assert full not in agent_module.KNOWN_TOOL_NAMES
