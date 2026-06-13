#!/usr/bin/env python3
"""OSIA autonomous orchestrator / execution loop — PLACEHOLDER (WOR-45, later phase).

Intentionally empty for Phase 1 (harness/state layer only). The execution loop —
walking the cursor through ``phases``/``tasks``, running each task through the
agent, checkpointing via :func:`osia.autonomous.state.save_task_state`, and
honouring the attempt/replan budgets — lands in a later phase.

Do NOT add execution logic here yet. In particular this phase must NOT touch the
agent loop, ``_extract_tool_calls``, ``_AntiLoopGuard`` or the WebSocket
protocol (see CLAUDE.md).
"""
