"""Local registration of user-selected MCP servers, shared by Settings and Chat."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from osia import config
from osia.mcp_client.catalog import MCP_CATALOG_META


class InvalidServer(ValueError):
    """Translation key for invalid public connection parameters."""


_CREDENTIAL_PARAMETER = re.compile(
    r"(?:^|-|_)(?:token|password|passwd|secret|api[-_]?key|authorization|signature|sig|key)$", re.I
)


def server_config(name, transport, *, url="", command="", args=None, auth_type="none", token_env=""):
    name = str(name or "").strip()
    if not name or len(name) > 120 or not name.isprintable():
        raise InvalidServer("mcp.err_name")
    if transport not in ("https", "stdio") or auth_type not in ("none", "token", "oauth"):
        raise InvalidServer("mcp.err_transport")
    token_env = (token_env or "").strip()
    args = [] if args is None else args
    if not isinstance(args, list) or any(not isinstance(arg, str) or "\0" in arg for arg in args):
        raise InvalidServer("mcp.err_command")
    if transport == "https":
        url = (url or "").strip()
        try:
            endpoint = urlsplit(url)
            _ = endpoint.port
            valid = (endpoint.scheme == "https" and endpoint.hostname and
                     endpoint.username is None and endpoint.password is None and
                     not endpoint.fragment and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in url) and
                     not any(_CREDENTIAL_PARAMETER.search(key)
                             for key, _ in parse_qsl(endpoint.query, keep_blank_values=True)))
        except ValueError:
            valid = False
        if not valid or command or args or token_env:
            raise InvalidServer("mcp.err_url")
    else:
        command = (command or "").strip()
        if not command or not command.isprintable() or url or auth_type == "oauth":
            raise InvalidServer("mcp.err_command")
        if any(_CREDENTIAL_PARAMETER.search(arg.partition("=")[0]) for arg in args
               if arg.startswith("-") or "=" in arg):
            raise InvalidServer("mcp.err_inline_secret")
        if ((auth_type == "token" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env))
                or (token_env and auth_type != "token")):
            raise InvalidServer("mcp.err_token_env")
    sid = re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-.").lower() or "custom"
    return {"id": sid, "name": name, "transport": "streamable_http" if transport == "https" else "stdio",
            "command": command or None, "args": args, "url": url or None, "env": {}, "auth_type": auth_type,
            "enabled": False, "token_env": token_env or None, "custom": True}


def save_custom_server(server, token=""):
    """Reserve the namespace and persist without starting a process or contacting a URL."""
    with config.settings_transaction():
        saved = config.get_setting("mcp_servers", []) or []
        taken = {e["id"] for e in MCP_CATALOG_META}
        taken.update(e.get("id") for e in saved if isinstance(e, dict))
        sid = base = server["id"]
        index = 2
        while sid in taken:
            sid = f"{base}-{index}"
            index += 1
        if token:
            config.set_mcp_token(sid, token)
        try:
            config.set_setting("mcp_servers", [*saved, {**server, "id": sid}])
        except Exception:
            if token:
                config.delete_mcp_token(sid)
            raise
    return sid


def tool_add_mcp_server(name, transport, url="", command="", args=None, auth_type="none", token_env=""):
    """Register a connector requested in Chat. Credentials are entered in Settings."""
    from osia import i18n
    try:
        cfg = server_config(name, transport, url=url, command=command, args=args,
                            auth_type=auth_type, token_env=token_env)
        sid = save_custom_server(cfg)
    except InvalidServer as exc:
        return {"text": i18n.t(str(exc)), "receipt": {"schema_version": 1, "dispatch_state": "not_dispatched",
                "outcome": "confirmed_failure_before_effect", "reason_code": "MCP_CONFIG_INVALID"}}
    except (ValueError, OSError):
        return {"text": "MCP registration could not be verified.", "receipt": {"schema_version": 1,
                "dispatch_state": "effect_unknown", "outcome": "outcome_unknown", "reason_code": "MCP_SAVE_FAILED"}}
    return {"text": f"Saved connector {sid}. Select Connect in Settings → MCP / Connectors to start it.",
            "receipt": {"schema_version": 1, "dispatch_state": "effect_confirmed",
                        "outcome": "confirmed_success", "reason_code": "MCP_SERVER_SAVED"}}
