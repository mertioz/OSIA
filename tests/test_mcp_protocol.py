import asyncio
from types import SimpleNamespace

import httpx
import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from osia.agent import _execute_tool_with_policy
from osia.mcp_client.manager import (
    MCPManager,
    STATUS_CONNECTED,
    STATUS_FAILED,
    _list_session_tools,
    _ServerConn,
)
from osia.mcp_client.registry import register_server_tools, unregister_server_tools
from osia.tool_contracts import ExecutionOutcome


def test_mcp_discovers_and_registers_tools_beyond_first_page():
    schema = {"type": "object", "properties": {}}

    class PaginatedSession:
        async def list_tools(self, *, params=None):
            if params is None:
                return ListToolsResult(tools=[Tool(name="first", inputSchema=schema)], nextCursor="page-two")
            assert params.cursor == "page-two"
            return ListToolsResult(tools=[Tool(name="second", inputSchema=schema)])

    tools = asyncio.run(_list_session_tools(PaginatedSession()))
    assert [tool["name"] for tool in tools] == ["first", "second"]
    try:
        assert register_server_tools(MCPManager(), "audit_paginated", tools) == [
            "mcp__audit_paginated__first",
            "mcp__audit_paginated__second",
        ]
    finally:
        unregister_server_tools("audit_paginated")


def test_mcp_rejects_repeated_pagination_cursor():
    class RepeatingSession:
        async def list_tools(self, *, params=None):
            return ListToolsResult(tools=[], nextCursor="page-two")

    with pytest.raises(RuntimeError, match="repeated pagination cursor"):
        asyncio.run(_list_session_tools(RepeatingSession()))


@pytest.fixture
def connector():
    manager = MCPManager()
    server_id = "audit_connector"
    auth_value = "example-fixture-authorization-value"
    session = SimpleNamespace()
    conn = _ServerConn(
        server_id=server_id,
        cfg={"headers": {"Authorization": "Bearer " + auth_value}},
        status=STATUS_CONNECTED,
        session=session,
    )
    assert manager._ensure_loop() is not None
    manager._servers[server_id] = conn
    register_server_tools(manager, server_id, [
        {"name": "lookup", "annotations": {"readOnlyHint": True}},
        {"name": "modify", "annotations": {"readOnlyHint": False}},
    ])
    try:
        yield manager, session, server_id, auth_value
    finally:
        unregister_server_tools(server_id)
        manager.shutdown()


def test_mcp_error_word_in_success_is_data(connector):
    manager, session, server_id, _ = connector
    payload = "ERROR: 403 forbidden in an incident report"

    async def call_tool(_name, _arguments):
        return CallToolResult(content=[TextContent(type="text", text=payload)], isError=False)

    session.call_tool = call_tool
    for tool, expected in (
        ("lookup", ExecutionOutcome.SUCCESS_VERIFIED),
        ("modify", ExecutionOutcome.SUCCESS_UNVERIFIED),
    ):
        record = _execute_tool_with_policy(f"mcp__{server_id}__{tool}", {})
        assert record.outcome is expected
        assert not record.is_error
        assert record.result == payload
    assert manager.status(server_id) == STATUS_CONNECTED


def test_mcp_resource_permission_error_preserves_connection_and_redacts(connector):
    manager, session, server_id, auth_value = connector

    async def call_tool(_name, _arguments):
        return CallToolResult(content=[TextContent(
            type="text", text=f"HTTP 403: forbidden for this resource ({auth_value})",
        )], isError=True)

    session.call_tool = call_tool
    record = _execute_tool_with_policy(f"mcp__{server_id}__modify", {})
    assert record.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert record.is_error
    assert "forbidden for this resource" in record.result
    assert auth_value not in record.result
    assert manager.status(server_id) == STATUS_CONNECTED


def test_mcp_transport_401_demotes_without_leaking_exception(connector):
    manager, session, server_id, auth_value = connector

    async def call_tool(_name, _arguments):
        request = httpx.Request("POST", "https://example.test/mcp")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError(auth_value, request=request, response=response)

    session.call_tool = call_tool
    record = _execute_tool_with_policy(f"mcp__{server_id}__modify", {})
    assert record.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert auth_value not in record.result
    assert manager.status(server_id) == STATUS_FAILED
