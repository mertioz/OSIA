"""Claude Console API authentication shared by chat and model discovery.

This is API-key transport only. It never reads Claude/Claude Code sessions or
introduces an agent runtime. Keys remain owned by osia.utils.key_chain.
"""


def request_headers(api_key: str) -> dict[str, str]:
    from osia.config import get_setting

    headers = {"x-api-key": api_key}
    workspace = get_setting("anthropic_workspace_id", "")
    if isinstance(workspace, str) and workspace.strip():
        headers["anthropic-workspace-id"] = workspace.strip()
    return headers
