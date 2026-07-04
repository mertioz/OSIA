#!/usr/bin/env python3
"""WOR-92 — Qt-free MCP connector catalog (single source of connector IDENTITY).

This module is the server-importable source of truth for each MCP connector's
*identity*: its ``id``, display ``name``, and i18n ``desc_key``. It deliberately
holds NO connection details — transport / url / auth / oauth_* live GUI-side in
``osia.gui.main_popup._MCP_CATALOG`` — so it stays import-light and Qt-free and
can be imported in the HEADLESS mobile server process, where the agent's system
prompt is built, without any PyQt dependency.

WHY it exists (WOR-92): ``osia.agent.get_system_prompt`` is the SINGLE prompt
builder shared by the desktop in-process ``Agent`` AND the mobile server's
``run(history=...)`` path. To make OSIA aware of its MCP connectors (which are
ACTIVE, and which CATALOG connectors it could offer to connect) on BOTH PC and
mobile, the prompt needs the connector list server-side. The catalog used to live
only in the Qt GUI, so the headless server could not see it. This module gives
both processes ONE identity source; descriptions resolve via ``osia.i18n`` (also
Qt-free) at prompt-build time.

The GUI's ``_MCP_CATALOG`` mirrors these ``id``/``name``/``desc_key`` values and
asserts at import that they match (see ``main_popup``), so the two cannot drift.
GUI-only fields (transport/url/auth/oauth_*) are intentionally kept out of this
module — they are not connector identity and would re-introduce duplication.

``suggestable`` marks a connector OSIA may proactively OFFER to connect ("I can
connect to X if you want"): True only for connectors that are connectable TODAY
(a working remote/stdio endpoint with zero-config auth) and not flagged
temporarily unavailable. It is a PROMPT concern ONLY — it does NOT gate the GUI's
own connect logic (``main_popup._mcp_connectable``), and an already-connected
connector is always surfaced regardless of this flag.
"""

from __future__ import annotations

# id / name / desc_key MIRROR osia.gui.main_popup._MCP_CATALOG (identity only).
# ``desc_key`` resolves through osia.i18n at prompt-build time.
#   suggestable=True  -> connectable now (open DCR / token / pre-seeded client),
#                        healthy -> OSIA may proactively offer it.
#   suggestable=False -> present in the GUI catalog but NOT offered, because:
#     * slack: WOR-99 — official remote endpoint exists, but Slack has no DCR and
#       requires a user-provided Slack app client_id/secret.
#     * google-drive / gmail-mcp-legacy: WOR-100/WOR-101 — connect, but Google's
#       MCP gateway is degraded (#229) -> flagged temporarily_unavailable in the
#       GUI. Visible Gmail uses native REST instead.
#     * asana / hubspot / figma: WOR-94 — no open RFC 7591 DCR / url left empty
#       -> not connectable in OSIA's OAuth substrate.
MCP_CATALOG_META = [
    {"id": "github",       "name": "GitHub",       "desc_key": "mcp.desc.github",    "suggestable": True},
    {"id": "slack",        "name": "Slack",        "desc_key": "mcp.desc.slack",     "suggestable": False},
    {"id": "notion",       "name": "Notion",       "desc_key": "mcp.desc.notion",    "suggestable": True},
    {"id": "linear",       "name": "Linear",       "desc_key": "mcp.desc.linear",    "suggestable": True},
    {"id": "google-drive", "name": "Google Drive", "desc_key": "mcp.desc.gdrive",    "suggestable": False},
    {"id": "gmail",        "name": "Gmail",        "desc_key": "mcp.desc.gmail",     "suggestable": False},
    {"id": "gmail-mcp-legacy", "name": "Gmail (legacy MCP)", "desc_key": "mcp.desc.gmail_legacy", "suggestable": False},
    {"id": "sentry",       "name": "Sentry",       "desc_key": "mcp.desc.sentry",    "suggestable": True},
    {"id": "stripe",       "name": "Stripe",       "desc_key": "mcp.desc.stripe",    "suggestable": True},
    {"id": "vercel",       "name": "Vercel",       "desc_key": "mcp.desc.vercel",    "suggestable": True},
    {"id": "supabase",     "name": "Supabase",     "desc_key": "mcp.desc.supabase",  "suggestable": True},
    {"id": "neon",         "name": "Neon",         "desc_key": "mcp.desc.neon",      "suggestable": True},
    {"id": "atlassian",    "name": "Atlassian",    "desc_key": "mcp.desc.atlassian", "suggestable": True},
    {"id": "canva",        "name": "Canva",        "desc_key": "mcp.desc.canva",     "suggestable": True},
    {"id": "paypal",       "name": "PayPal",       "desc_key": "mcp.desc.paypal",    "suggestable": True},
    {"id": "asana",        "name": "Asana",        "desc_key": "mcp.desc.asana",     "suggestable": False},
    {"id": "hubspot",      "name": "HubSpot",      "desc_key": "mcp.desc.hubspot",   "suggestable": False},
    {"id": "figma",        "name": "Figma",        "desc_key": "mcp.desc.figma",     "suggestable": False},
]


def get_catalog_meta() -> list:
    """Return a shallow copy of the connector identity catalog: a list of
    ``{"id","name","desc_key","suggestable"}`` dicts (copies, safe to mutate)."""
    return [dict(m) for m in MCP_CATALOG_META]


def catalog_by_id() -> dict:
    """``{id: {"id","name","desc_key","suggestable"}}`` for O(1) lookup."""
    return {m["id"]: dict(m) for m in MCP_CATALOG_META}
