#!/usr/bin/env python3
"""OSIA autonomous task harness (WOR-45).

This package holds the *autonomous task* machinery — the long-running,
multi-step "do this whole objective on your own" mode. It is built in phases:

* **Phase 1 (this code) — harness/state layer ONLY.** ``state.py`` owns the
  persistent, crash-safe ``task_state.json`` (atomic checkpoint + resume
  detection). No planner and no execution loop yet.
* Later phases add the planner (``planner.py``) and the execution/orchestration
  loop (``orchestrator.py``) — both are intentionally empty placeholders for now.

Nothing here touches the agent loop, the tool-call parser, ``_AntiLoopGuard``,
the WebSocket protocol, or ``key_chain`` (see CLAUDE.md). The config/data dir is
always resolved via ``osia.config.CONFIG_DIR`` — never recomputed.
"""
