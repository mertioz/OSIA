"""Canonical Chat execution helpers (WOR-303).

Unified Chat is the single conversational and execution runtime.  A request
stays the same canonical Chat objective whether it needs zero tools or dozens
of provider/tool rounds; complexity and duration never select another agent
mode.  Scheduled Tasks remain a separate temporal trigger system that executes
through this same Chat runtime.

This module keeps only the presentation helpers shared by the canonical Chat
runtime.  There is no Autonomous classification, no handoff contract, and no
second execution launcher here.
"""

from __future__ import annotations

from typing import Any


def execution_prompt(language: str = "en") -> str:
    """Return the unified Chat execution policy for provider turns."""

    del language
    return (
        "## Unified Chat execution\n"
        "Use this Chat as the single execution surface. Answer informational "
        "questions directly. For requests requiring tools, computer actions, or "
        "multiple dependent actions, execute the next safe action with the "
        "available tools, observe its result, and then choose the next action. "
        "Keep the SAME objective until it is actually complete and verified, "
        "however many provider/tool rounds that requires. Do not generate a "
        "mandatory task list and do not launch a second execution mode. "
        "Future or recurring requests still use only the Scheduled Tasks tools. "
        "If the essential objective is missing, ask one clarifying question. "
        "Never reveal private reasoning."
    )


def project_chat_tool_attempt_status(status: object) -> str:
    """Project internal attempt state as non-terminal Chat presentation."""

    normalized = str(status or "")
    if normalized == "blocked_unknown_effect":
        # Legacy status label only: this is presentation for an uncertain attempt,
        # not a dispatch veto. Fresh model-requested calls remain allowed.
        return "verifying"
    return {
        "action_starting": "action_starting",
        "action_in_progress": "action_in_progress",
        "waiting_tool_result": "waiting_tool_result",
        "verifying_tool_result": "verifying",
        "resuming_tool_turn": "resuming",
        "cancellation_requested": "cancellation_requested",
        "cancelled_proven": "cancelled_proven",
    }.get(normalized, "action_in_progress")


__all__ = [
    "execution_prompt",
    "project_chat_tool_attempt_status",
]
