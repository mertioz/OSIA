from __future__ import annotations

import math

import pytest
from jsonschema import Draft202012Validator

from osia import agent as agent_module
from osia.mcp_client import registry as mcp_registry
from osia.tool_contracts import (
    ExecutionOutcome,
    SideEffectClass,
    ToolRegistry,
    ToolRegistryError,
    ToolValidationError,
    assert_prompt_tool_examples_compatible,
    dynamic_tool_specs,
    get_static_registry,
    get_tool_spec,
    static_tool_prompt_block,
    validate_arguments,
)


def test_static_registry_is_complete_unique_and_provider_prompt_derived():
    registry = get_static_registry()
    executable = {
        name: dispatcher for name, dispatcher in agent_module.TOOL_MAP.items()
        if not name.startswith("mcp__")
    }
    assert len(executable) == 95
    assert len(registry.names()) == 95
    registry.assert_complete(executable)
    prompt = static_tool_prompt_block("en")
    for spec in registry.values():
        Draft202012Validator.check_schema(spec.input_schema)
        assert prompt.count(f"- {spec.name}:") == 1


def test_duplicate_registry_name_is_rejected():
    spec = get_static_registry().get("get_system_info")
    with pytest.raises(ToolRegistryError, match="duplicate tool name"):
        ToolRegistry((spec, spec))


def test_hard_coded_provider_examples_are_checked_against_registry():
    assert_prompt_tool_examples_compatible(
        '- read_file: Usage: {"tool":"read_file","path":"<path>"}'
    )
    with pytest.raises(ToolRegistryError, match="usage example drift"):
        assert_prompt_tool_examples_compatible(
            '- read_file: Usage: {"tool":"read_file","path":"<path>","extra":1}'
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": "10"},
        {"limit": True},
        {"limit": 0},
        {"limit": 201},
        {"limit": 10, "seeded_secret": "WOR178_SECRET"},
        {"limit": math.nan},
        {"limit": math.inf},
    ],
)
def test_invalid_values_fail_strict_validation(arguments):
    schema = get_static_registry().get("list_running_processes").input_schema
    with pytest.raises(ToolValidationError):
        validate_arguments(schema, arguments)


def test_required_enum_identifier_and_bounded_values_are_enforced():
    delete_schema = get_static_registry().get("delete_scheduled_task").input_schema
    with pytest.raises(ToolValidationError):
        validate_arguments(delete_schema, {})
    with pytest.raises(ToolValidationError):
        validate_arguments(delete_schema, {"task_id": "bad id with spaces"})

    browser_schema = get_static_registry().get("open_browser").input_schema
    with pytest.raises(ToolValidationError):
        validate_arguments(browser_schema, {"name": "unknown-browser"})
    assert validate_arguments(browser_schema, {"name": "brave"}) == {"name": "brave"}

    create_schema = get_static_registry().get("create_scheduled_task").input_schema
    with pytest.raises(ToolValidationError):
        validate_arguments(create_schema, {
            "title": None,
            "instruction": "fixture",
            "schedule": {"type": "once", "run_at": "2030-01-01T10:00:00Z"},
        })
    with pytest.raises(ToolValidationError):
        validate_arguments(create_schema, {
            "title": "fixture",
            "instruction": "fixture",
            "schedule": {"type": "interval", "every": 5},
        })


def test_invalid_call_is_rejected_before_dispatch_without_value_leak(monkeypatch):
    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "list_running_processes",
        lambda limit=50: calls.append(limit) or "unexpected",
    )
    record = agent_module._execute_tool_with_policy(
        "list_running_processes",
        {"limit": "WOR178_SECRET"},
        lang="en",
    )
    assert record.outcome == ExecutionOutcome.REJECTED_BEFORE_EXECUTION
    assert record.dispatched is False
    assert calls == []
    assert "WOR178_SECRET" not in str(record.result)


class _Manager:
    def __init__(self):
        self.calls = []

    def call_tool(self, server_id, tool_name, arguments, *, timeout):
        self.calls.append((server_id, tool_name, arguments, timeout))
        return "ok"


def test_dynamic_mcp_schema_is_registered_validated_and_removed(monkeypatch):
    server_id = "wor178_registry"
    manager = _Manager()
    tools = [{
        "name": "read_counter",
        "description": "third-party description",
        "inputSchema": {
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 3}},
            "required": ["count"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    }]
    try:
        names = mcp_registry.register_server_tools(manager, server_id, tools)
        assert names == ["mcp__wor178_registry__read_counter"]
        full = names[0]
        spec = get_tool_spec(full)
        assert spec is not None and spec.dynamic is True
        assert spec.side_effect == SideEffectClass.READ_ONLY

        wrapper = agent_module.TOOL_MAP[full]
        assert wrapper(count="2").startswith("ERROR:")
        assert wrapper(args={"count": 2}).startswith("ERROR:")
        assert manager.calls == []
        assert wrapper(count=2) == "ok"
        assert manager.calls[0][2] == {"count": 2}
    finally:
        mcp_registry.unregister_server_tools(server_id)

    assert get_tool_spec("mcp__wor178_registry__read_counter") is None
    assert all(spec.connector_id != server_id for spec in dynamic_tool_specs())
    assert "mcp__wor178_registry__read_counter" not in agent_module.KNOWN_TOOL_NAMES


def test_invalid_dynamic_mcp_schema_is_never_advertised_or_executable():
    server_id = "wor178_bad_schema"
    manager = _Manager()
    try:
        names = mcp_registry.register_server_tools(manager, server_id, [{
            "name": "bad",
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "not-a-json-schema-type"}},
            },
            "annotations": {"readOnlyHint": True},
        }])
        assert names == []
        assert mcp_registry.registered_names(server_id) == []
        assert get_tool_spec("mcp__wor178_bad_schema__bad") is None
    finally:
        mcp_registry.unregister_server_tools(server_id)


def test_duplicate_dynamic_mcp_name_is_rejected():
    server_id = "wor178_duplicate"
    descriptor = {
        "name": "same_name",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    }
    try:
        assert mcp_registry.register_server_tools(
            _Manager(), server_id, [descriptor, dict(descriptor)],
        ) == []
        assert mcp_registry.registered_names(server_id) == []
    finally:
        mcp_registry.unregister_server_tools(server_id)


def test_untrusted_mcp_annotations_cannot_lower_risk():
    assert mcp_registry.classify_tool("list_items", {}) is True
    assert mcp_registry.classify_tool(
        "list_items", {"readOnlyHint": "true"},
    ) is True
    assert mcp_registry.classify_tool(
        "list_items", {"readOnlyHint": True, "destructiveHint": True},
    ) is True
    assert mcp_registry.classify_tool(
        "list_items", {"readOnlyHint": True, "destructiveHint": False},
    ) is False
