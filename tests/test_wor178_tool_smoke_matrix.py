from __future__ import annotations

import json

from osia import agent as agent_module
from osia.mcp_client import registry as mcp_registry
from tools.tool_reliability_matrix import build_reports


class _Manager:
    def call_tool(self, *_args, **_kwargs):
        raise AssertionError("matrix generation must never execute a tool")


def test_matrix_is_complete_dynamic_deterministic_and_content_free(tmp_path):
    server_id = "wor178_matrix"
    secret = "WOR178_MATRIX_SECRET"
    connected_tool = {
        "name": "read_status",
        "description": f"provider payload {secret}",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    }
    try:
        names = mcp_registry.register_server_tools(
            _Manager(), server_id, [connected_tool],
        )
        assert names == ["mcp__wor178_matrix__read_status"]
        configured = [
            {"id": server_id, "private": secret},
            {"id": "wor178_offline", "private": secret},
        ]
        json_path, markdown_path, rows = build_reports(
            tmp_path, configured_connectors=configured,
        )
        _json_path_2, _markdown_path_2, rows_again = build_reports(
            tmp_path, configured_connectors=configured,
        )
    finally:
        mcp_registry.unregister_server_tools(server_id)

    assert rows == rows_again
    static_names = {
        name for name in agent_module.TOOL_MAP if not name.startswith("mcp__")
    }
    row_names = {row["tool_name"] for row in rows}
    assert static_names <= row_names
    assert len(static_names) == 68
    assert "mcp__wor178_matrix__read_status" in row_names
    assert "connector::wor178_offline" in row_names
    offline = next(row for row in rows if row["tool_name"] == "connector::wor178_offline")
    assert offline["currently_executable"] is False
    assert offline["current_evidence_classification"] == "unavailable_in_environment"
    assert "connector::wor178_matrix" not in row_names

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["tool_execution_performed"] is False
    assert len(payload["rows"]) == len(rows)
    rendered = json_path.read_text(encoding="utf-8") + markdown_path.read_text(encoding="utf-8")
    assert secret not in rendered
    assert str(tmp_path) not in rendered
    assert set(payload["evidence_totals"]) <= {
        "proved",
        "unavailable_in_environment",
        "not_safely_testable",
        "failed",
        "real_validation_required",
        "metadata_only",
    }
