#!/usr/bin/env python3
"""OSIA MCP (Model Context Protocol) client package — WOR-44.

Phase 1: a self-contained MCP *client* core. ``MCPManager`` connects to MCP
servers (stdio transport in Phase 1), discovers their tools, and calls a tool —
returning the result as a plain string or an ``ERROR: ...`` string, mirroring
the native-tool contract in ``osia.agent``.

This package is deliberately STANDALONE: it imports neither ``osia.agent`` nor
``osia.server`` and owns its own asyncio event loop in a daemon thread, so it
works identically inside the desktop GUI process and the mobile server process.
Agent/UI wiring is intentionally NOT here (later phases).
"""

from .manager import (  # noqa: F401
    MCPManager,
    get_manager,
    set_status_listener,
    STATUS_CONNECTED,
    STATUS_CONNECTING,
    STATUS_DISCONNECTED,
    STATUS_FAILED,
    SUPPORTED_TRANSPORTS,
    KNOWN_TRANSPORTS,
)

__all__ = [
    "MCPManager",
    "get_manager",
    "set_status_listener",
    "STATUS_CONNECTED",
    "STATUS_CONNECTING",
    "STATUS_DISCONNECTED",
    "STATUS_FAILED",
    "SUPPORTED_TRANSPORTS",
    "KNOWN_TRANSPORTS",
]
