"""WOR-301 final invariant: unrestricted tool execution and one barrier.

Authoritative product rule under test:

* if a capability is technically available in the current runtime, OSIA must be
  able to use it for any request, at any time, in every mode and from every
  backend execution path;
* availability and model choice are separate: the model decides usefulness, and
  no OSIA request/mode/semantic/strategy/relevance layer may reject a call;
* the only product authorization barrier is
  ``ERROR: BLOCK_SYSTEM_DESTRUCTION`` for directly evidenced catastrophic host
  destruction;
* factual ToolAttempt/ActionTruth records, stale target/frame freshness,
  exact-turn cancellation, and invalid-argument/state checks remain intact.
  Historical outcomes do not veto a fresh model-requested dispatch.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from osia.action_policy import (
    ExecutionType,
    classify_action_request,
    execution_context_scope,
    guard_tool_call,
    make_execution_context,
    system_mutation_barrier,
)
from osia.execution_strategy import (
    build_execution_strategy,
    preflight_refusal_is_dispatch_blocking,
    tool_preflight,
)
from osia.tool_contracts import (
    effective_tool_surface_scope,
    get_static_registry,
    static_tool_inventory_specs,
    tool_is_runtime_available,
)

# WOR-303: one canonical Chat execution surface plus the Scheduled temporal
# trigger.  No engine-specific surface may remap the tool inventory.
_SURFACES = (
    "",
    "scheduled",
)

_RELEVANCE_REFUSAL_PREFIXES = (
    "explicit_negative_constraint_forbids",
    "higher_strategy_still_applicable",
)
_RELEVANCE_REFUSAL_CODES = frozenset({
    "required_application_changed",
    "required_surface_changed",
    "changed_required_surface",
    "earlier_action_node_pending",
    "read_only_visual_mission_tool_not_applicable",
    "global_capture_hidden_for_exact_visual_read",
    "semantic_batch_incomplete_semantic_goal",
    "mechanism_not_applicable",
    "attempted_effect_not_applicable_to_semantic_node",
    "attempted_effect_target_mismatch",
    "visual_mouse_intent_not_applicable",
    "foreground_transition_intent_not_applicable",
    "foreground_transition_effect_policy_blocked",
    "stronger_exact_uia_frontier_available",
    "stronger_exact_keyboard_frontier_available",
    "uia_exhaustion_evidence_required",
    "uia_candidate_retired_before_effect",
    "goal_effect_pending",
    "prerequisite_effect_pending",
    "canonical_visual_read_already_observed",
    "structured_uia_evidence_sufficient",
    "keyboard_frontier_exhausted",
    "keyboard_frontier_not_exhausted",
    "uia_frontier_exhausted_keyboard_ready",
})


# ---------------------------------------------------------------------------
# 1. Mode independence: no execution-surface eligibility filter.
# ---------------------------------------------------------------------------


def test_tool_inventory_is_identical_in_every_mode():
    """Availability is the only contract: no surface changes the inventory."""

    expected = {spec.name for spec in static_tool_inventory_specs()}
    assert expected
    for surface in _SURFACES:
        with effective_tool_surface_scope(surface):
            assert {spec.name for spec in static_tool_inventory_specs()} == expected


def test_mode_eligibility_vocabulary_is_gone():
    """WOR-301: no eligibility field, gate or surface filter survives."""

    from osia import action_policy, slash_commands
    from osia import tool_contracts

    assert not hasattr(tool_contracts, "tool_is_eligible_in_current_surface")
    assert not hasattr(action_policy, "autonomous_permission_gate")
    assert not hasattr(action_policy, "scheduled_permission_gate")
    assert not hasattr(slash_commands, "tool_allowed_for_directives")
    for spec in get_static_registry().values():
        assert not hasattr(spec, "chat_eligible"), spec.name
        assert not hasattr(spec, "autonomous_eligible"), spec.name
        assert not hasattr(spec, "scheduled_eligible"), spec.name


def test_only_guard_tool_call_guards_an_available_tool():
    """Every representative capability dispatches through the integrity gate."""

    # WOR-303: one canonical Chat execution type plus Scheduled.
    chat = make_execution_context(execution_type=ExecutionType.CHAT)
    scheduled = make_execution_context(execution_type=ExecutionType.SCHEDULED)
    cases = (
        ("bash", {"command": "echo hello"}),
        ("read_document", {"path": "C:/Temp/report.pdf"}),
        ("gmail_send_message", {"to": "a@example.test", "subject": "s"}),
        ("mcp__server__tool", {"value": 1}),
        ("save_memory", {"text": "note"}),
        ("computer_screenshot", {}),
    )
    for execution_context in (None, chat, scheduled):
        scope = (
            execution_context_scope(execution_context)
            if execution_context is not None
            else execution_context_scope(make_execution_context())
        )
        with scope:
            for tool, arguments in cases:
                assert guard_tool_call("do the thing", tool, dict(arguments)) is None, tool


# ---------------------------------------------------------------------------
# 2. Preflight: no request/semantic/strategy/frontier relevance refusal.
# ---------------------------------------------------------------------------

_TOOL_CATALOGUE = tuple(
    spec.name for spec in get_static_registry().values()
)

_PREFLIGHT_MATRIX = (
    (
        "Open Steam and go to my Library.",
        "write_file",
        {"path": "C:/Temp/wor301-report.txt", "content": "hello"},
    ),
    (
        "Open Steam and go to my Library.",
        "run_command",
        {"command": "echo hello"},
    ),
    ("Open Steam and go to my Library.", "get_clipboard", {}),
    ("Open Steam and go to my Library.", "save_memory", {"text": "note"}),
    (
        "Open Steam and go to my Library.",
        "navigate_browser",
        {"url": "https://example.test/"},
    ),
    (
        "Search the web for the best espresso machine.",
        "write_file",
        {"path": "C:/Temp/wor301-web.txt", "content": "x"},
    ),
    (
        "Create a python script in C:/Temp named calc.py.",
        "navigate_browser",
        {"url": "https://example.test/"},
    ),
    ("What is the capital of France?", "run_command", {"command": "echo hi"}),
    ("What is the capital of France?", "list_files", {"path": "C:/Temp"}),
    (
        "Read the document C:/Temp/report.pdf.",
        "kill_process",
        {"process_name": "notepad"},
    ),
    ("Open Steam and go to my Library.", "desktop_press", {"key": "enter"}),
    ("What is the capital of France?", "list_running_processes", {}),
    (
        "Apply for the support job and submit the form.",
        "create_directory",
        {"path": "C:/Temp/wor301-dir"},
    ),
)


@pytest.mark.parametrize(("objective", "tool", "arguments"), _PREFLIGHT_MATRIX)
def test_preflight_never_refuses_for_relevance(objective, tool, arguments):
    strategy = build_execution_strategy(objective, _TOOL_CATALOGUE)
    decision = tool_preflight(tool, dict(arguments), strategy=strategy)
    reason = "" if decision is None else str(decision.reason_code or "")
    for prefix in _RELEVANCE_REFUSAL_PREFIXES:
        assert not reason.startswith(prefix), (objective, tool, reason)
    assert reason not in _RELEVANCE_REFUSAL_CODES, (objective, tool, reason)
    if decision is not None and not decision.allowed:
        assert preflight_refusal_is_dispatch_blocking(reason), (
            objective, tool, reason,
        )


def test_goal_contract_negative_constraints_no_longer_block_dispatch():
    strategy = build_execution_strategy(
        "Open Steam at my Library and do not use the browser.",
        _TOOL_CATALOGUE,
    )
    decision = tool_preflight(
        "navigate_browser", {"url": "https://example.test/"}, strategy=strategy,
    )
    assert decision is None or decision.allowed


def test_dispatchable_capability_set_is_request_and_mode_independent():
    """The final WOR-301 invariant, stated exactly.

    For one platform/dependency/connector set, the available, callable and
    dispatchable capability set must not change with the request wording, the
    task type, the semantic domain, the GoalContract/ExecutionStrategy, the
    current node, the mode, the backend or the frontier.
    """

    requests = (
        "Open Steam and go to my Library.",
        "Search the web for the best espresso machine and write a summary file.",
        "Create a python script in C:/Temp named calc.py.",
        "What is the capital of France?",
        "Apply for the support job and submit the form.",
        "Do not use the browser: read the local report instead.",
    )
    expected = {
        spec.name for spec in static_tool_inventory_specs()
    }
    assert expected

    for surface in _SURFACES:
        with effective_tool_surface_scope(surface):
            available = {
                spec.name for spec in static_tool_inventory_specs()
            }
        assert available == expected, surface

    for objective in requests:
        strategy = build_execution_strategy(objective, tuple(expected))
        for spec in static_tool_inventory_specs():
            decision = tool_preflight(spec.name, {}, strategy=strategy)
            if decision is not None and not decision.allowed:
                assert preflight_refusal_is_dispatch_blocking(
                    decision.reason_code
                ), (objective, spec.name, decision.reason_code)


# ---------------------------------------------------------------------------
# 3. Real dispatch: irrelevant tools execute across unrelated requests.
# ---------------------------------------------------------------------------


class _RepeatingCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        message = SimpleNamespace(content=self.responses[index], tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_RepeatingCompletions(responses))


def _tool_call(tool, **kwargs):
    payload = {"tool": tool, **kwargs}
    return "<tool_call>\n" + json.dumps(payload) + "\n</tool_call>"


def _run_agent_turn(monkeypatch, request, responses):
    from osia.agent import OSIAAgent

    agent = OSIAAgent(lang="en")
    agent.provider = "ollama"
    agent.model = "wor301-model"
    agent.client = _FakeClient(responses)
    monkeypatch.setattr(agent, "_resolve_route", lambda _text: ("medium", []))
    return agent, agent.run(request, history=[])


_DISPATCH_MATRIX = (
    (
        "What is the capital of France?",
        "run_command",
        {"command": "echo wor301"},
        "shell during a trivial informational request",
    ),
    (
        "Open Steam and go to my Library.",
        "write_file",
        {"path": "C:/Temp/wor301-desktop.txt", "content": "note"},
        "filesystem tool during a Desktop-classified objective",
    ),
    (
        "Create a python script in C:/Temp named calc.py.",
        "navigate_browser",
        {"url": "https://example.test/"},
        "browser tool during a coding objective",
    ),
)


@pytest.mark.parametrize(
    ("objective", "tool", "arguments", "label"), _DISPATCH_MATRIX,
)
def test_irrelevant_available_tool_dispatches(
    monkeypatch, objective, tool, arguments, label,
):
    from osia import agent as agent_module

    executed = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        tool,
        lambda **kwargs: executed.append(kwargs) or "tool finished",
    )
    _agent, _result = _run_agent_turn(
        monkeypatch,
        objective,
        [_tool_call(tool, **arguments), "Done."],
    )
    assert executed == [arguments], label


# ---------------------------------------------------------------------------
# 4. Harmful-request classification is a label, never an authorization layer.
# ---------------------------------------------------------------------------


def test_harmful_request_text_is_labelled_but_never_blocks_dispatch():
    decision = classify_action_request("Threaten Contact Alpha in a message.")
    assert decision.action_class.value == "E"
    for tool, arguments in (
        ("find_and_click", {"text": "Send"}),
        ("run_command", {"command": "echo hello"}),
        ("write_file", {"path": "notes.txt", "content": "x"}),
    ):
        assert guard_tool_call(
            "Threaten Contact Alpha in a message.", tool, dict(arguments),
        ) is None


# ---------------------------------------------------------------------------
# 5. The single authorization barrier: system destruction only.
# ---------------------------------------------------------------------------


def _osia_project_root() -> str:
    import osia

    return str(Path(osia.__file__).resolve().parent.parent)


def test_ordinary_destruction_progresses_without_any_user_authorization(tmp_path):
    """DELETION_PREFLIGHT_REQUIRED is integrity-only (no confirmation, no grant).

    The bound plan, single-use consume and verified removal are exercised
    end-to-end by the WOR-193 deletion suites; this asserts that the guard
    prepares the exact plan and proceeds with no user interaction.
    """

    from osia.mcp_client import registry as mcp_registry

    prompts = []
    mcp_registry.set_confirmation_provider(
        lambda origin, tool, summary: prompts.append((origin, tool, summary)) or False
    )

    target = tmp_path / "generated"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "file.txt").write_text("x", encoding="utf-8")

    assert guard_tool_call(
        "clean up the generated folder", "delete_directory",
        {"path": str(target), "recursive": True},
    ) is None
    # A non-recursive request on a non-empty directory is an invalid state,
    # reported as a typed reason, never as a permission request.
    error = guard_tool_call(
        "clean up the generated folder", "delete_directory",
        {"path": str(target), "recursive": False},
    )
    assert error == "ERROR: DELETION_PREFLIGHT_REQUIRED (non_recursive_directory_not_empty)"
    assert prompts == []


def test_ordinary_destruction_is_never_pre_emptively_blocked(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    ordinary_file = tmp_path / "notes.txt"
    ordinary_file.write_text("hello", encoding="utf-8")

    assert guard_tool_call(
        "clean up the temporary folder", "delete_directory",
        {"path": str(empty_dir), "recursive": False},
    ) is None
    assert guard_tool_call(
        "delete the note", "delete_file", {"path": str(ordinary_file)},
    ) is None
    assert guard_tool_call(
        "remove a generated folder", "delete_directory",
        {"path": str(tmp_path / "generated"), "recursive": True},
    ) in {None, "ERROR: DELETION_PREFLIGHT_REQUIRED (deletion_root_missing)"}
    assert system_mutation_barrier(
        "delete_directory", {"path": str(empty_dir)},
    ) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system roots")
def test_only_exact_recursive_windows_os_roots_are_catastrophic():
    from osia.system_resources import DestructiveOperation, SystemResourceRegistry

    registry = SystemResourceRegistry(platform_family="windows")
    windows_root = registry.windows.windows_directory
    system_root = registry.windows.system_directory
    for path in {windows_root, system_root}:
        evidence = registry.classify_path(
            path, operation=DestructiveOperation.RECURSIVE_DELETE,
        )
        assert evidence.blocks_destruction, path
    child = registry.classify_path(
        os.path.join(system_root, "ordinary-child.dll"),
        operation=DestructiveOperation.DELETE,
    )
    assert not child.blocks_destruction


def test_osia_project_and_data_directories_are_not_host_destruction(tmp_path):
    from osia.system_resources import DestructiveOperation, SystemResourceRegistry

    osia_data_dir = str(Path.home() / ".osia")
    osia_root = _osia_project_root()
    registry = SystemResourceRegistry()
    for path in (osia_data_dir, osia_root):
        assert not registry.classify_path(
            path, operation=DestructiveOperation.RECURSIVE_DELETE,
        ).blocks_destruction, path
    # Ordinary project and user-data writes stay available.
    assert guard_tool_call(
        "write a log", "write_file",
        {
            "path": os.path.join(osia_data_dir, "scratch", "log.txt"),
            "content": "hello",
        },
    ) is None

def test_system_file_write_is_not_a_host_destruction_primitive():
    if sys.platform != "win32":
        pytest.skip("Windows system file")
    path = r"C:\Windows\System32\drivers\etc\hosts"
    assert guard_tool_call(
        "update hosts", "write_file", {"path": path, "content": "x"},
    ) is None
    assert system_mutation_barrier(
        "write_file", {"path": path, "content": "x"},
    ) is None


def test_reparse_traversal_for_a_single_file_is_not_host_destruction(tmp_path):
    """A system-looking single-file target is not a catastrophic host wipe."""

    if sys.platform != "win32":
        pytest.skip("Windows junction semantics")
    junction = tmp_path / "junction-to-system"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), r"C:\Windows\System32"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if created.returncode != 0:
        pytest.skip(f"junction unavailable: {created.stderr[-160:]}")

    through = junction / "drivers" / "etc" / "hosts"
    # This policy inspection does not write the file.  A single-file edit is
    # an ordinary tool operation even when its resolved path is in the OS tree.
    assert guard_tool_call(
        "update hosts", "write_file", {"path": str(through), "content": "x"},
    ) is None
    # Removing the junction itself removes only the link: ordinary cleanup.
    assert guard_tool_call(
        "remove the link", "delete_directory",
        {"path": str(junction), "recursive": False},
    ) != "ERROR: BLOCK_SYSTEM_DESTRUCTION"


# ---------------------------------------------------------------------------
# 7. Contextual routing/recursion restrictions are gone (WOR-301 closure).
# ---------------------------------------------------------------------------


def test_connector_hint_never_refuses_a_connected_connector_tool(monkeypatch):
    from osia import agent as agent_module
    from osia.agent import OSIAAgent
    from osia.slash_commands import ConnectorHint, SlashTurnDirectivesV1

    directives = SlashTurnDirectivesV1(
        connector_hints=(ConnectorHint("linear"),),
    )
    executed = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "mcp__notion__search",
        lambda **kwargs: executed.append(kwargs) or "notion result",
    )
    agent = OSIAAgent(lang="en")
    agent.provider = "ollama"
    agent.model = "wor301-model"
    agent.client = _FakeClient([
        '<tool_call>{"tool":"mcp__notion__search","query":"release notes"}'
        "</tool_call>",
        "Done.",
    ])
    agent._slash_turn_directives = directives

    agent.run("Search Notion for the release notes.", history=[])

    # A connector-hint mismatch never refuses the call any more.
    assert executed and executed[0].get("query") == "release notes"


def test_no_autonomous_routing_tool_exists_and_no_chat_tool_refusal():
    from osia import agent as agent_module

    # WOR-303: the Autonomous handoff tool is gone and every remaining
    # technically available tool stays visible and callable from Chat.
    available = set(agent_module._active_chat_tool_names())
    assert "start_autonomous_mission" not in available
    assert available <= set(agent_module.TOOL_MAP)
    assert "start_autonomous_mission" not in agent_module.TOOL_MAP
    source = Path(agent_module.__file__).read_text(encoding="utf-8")
    assert "tool_not_available_in_chat" not in source
    assert "slash_directives_chat_only" not in source


def test_nested_scheduled_tool_dispatch_is_not_mode_refused(monkeypatch):
    from osia import agent as agent_module
    from osia.agent import OSIAAgent

    executed = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "schedule_task",
        lambda **kwargs: executed.append(kwargs) or "scheduled",
    )
    token = agent_module._SCHEDULED_EXECUTION_CONTEXT.set({})
    try:
        agent = OSIAAgent(lang="en")
        agent.provider = "ollama"
        agent.model = "wor301-model"
        agent.client = _FakeClient([
            '<tool_call>{"tool":"schedule_task","text":"standup reminder",'
            '"run_at":"2026-09-17T09:00:00"}</tool_call>',
            "Done.",
        ])
        agent.run("Remind me tomorrow morning.", history=[])
    finally:
        agent_module._SCHEDULED_EXECUTION_CONTEXT.reset(token)

    assert executed, "nested scheduled dispatch was refused"


# ---------------------------------------------------------------------------
# 8. The historical permission modules stay gone.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    (
        "osia.permission_store",
        "osia.permission_contract",
        "osia.gui.permissions_center",
        "osia.gui.mcp_confirm",
        "osia.gui.deletion_confirm",
    ),
)
def test_permission_modules_are_not_importable(module):
    assert importlib.util.find_spec(module) is None
