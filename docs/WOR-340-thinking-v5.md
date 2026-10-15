# WOR-340 V5 — automatic expansion for new active turns

The operator validated V4 and requested one correction: every new active turn
must start expanded before public commentary arrives. V5 changes four product
source lines across two UI files. Final operator validation is pending.

## Initial collapsed state and minimal correction

Desktop `_ChatThinkingTrace.__init__` defaulted `auto_expand` to False, and
`_ChatPage.set_thinking_trace` instantiated that default for all trace widgets.
V5 defaults it to True and explicitly passes `auto_expand=not historical` at
creation. New active turns open immediately; historical rows stay collapsed.

Mobile `_ExecutionTraceBlockState._updateDisclosure` set expansion to False on
the first active start, and `_ThinkingDisclosureState.initState` did the same
for the waiting control. Both initial assignments now set True. The base
disclosure memory and different-key reset still begin False for historical
projections, and the existing first-active guard opens only an unstarted,
uncompleted turn. Received snapshots do not repeatedly change disclosure state.

## Call sites and rendering

Desktop `begin_turn` creates the initial empty trace through `set_thinking_trace`.
Send/retry and `_apply_current_activity` use this same canonical binding path.
A replay for the same existing turn retains its widget and manual choice; a
distinct canonical turn creates fresh expanded disclosure. Completed history
uses `historical=True` and never starts expanded. No simulated click is used to
initialize production state.

V4's explicit visibility/height synchronization is unchanged: the empty turn
shows neutral dots without invented text, and its expanded disclosure makes
the existing QTextEdit visible with a positive height when text arrives. The
same document/widget is retained. Offscreen tests check actual visibility and
height, including queued GUI-thread delivery.

Mobile's Chat screen retains disclosure memory by exact PC trace key and shares
waiting-row memory with the received trace. The existing `didUpdateWidget`
rebuild and partial-snapshot merge render commentary automatically. Manual
collapse survives revisions and remounting into the answer slot. Different
turn keys reset first-start state and expand the new active turn. Transport and
Chat screen source were not edited in V5.

## Manual overrides and completion

Dots remain clickable and keyboard accessible. A manual collapse remains
collapsed through provider/tool updates and same-turn re-projection; reopening
shows all retained commentary. A subsequent distinct active turn starts
expanded regardless of that previous choice.

The existing canonical completion/cancellation/failure settlement is unchanged:
collapse once, stop dots, expose the recorded Thinking label when content
exists, and allow manual reopening. Final-answer chunks do not collapse the
trace. Active headers contain dots without the visible Thinking word.

## V5 files

Desktop product source:

- `osia/gui/main_popup.py` — two initialization/call-site lines.

Desktop focused tests:

- `tests/test_wor340_thinking_v4_ui.py` — automatic empty/live receipt checks,
  keyboard manual override, and nine provider-independent new-turn/replay cases.
- `tests/test_wor340_thinking.py` — remove clicks required only by V4's old default.
- `tests/test_wor311_public_progress.py` — expanded initial state.
- `tests/test_wor265_phase2a_pc_unified_chat_ui.py` — expanded initial state.

Mobile product source:

- `lib/widgets/execution_trace_block.dart` — first active and waiting starts only.

Mobile focused tests:

- `test/wor340_thinking_v4_ui_test.dart` — automatic rendering, manual keyboard
  collapse/reopen, and fresh-turn expansion with reused UI memory.
- `test/wor340_thinking_test.dart` — no-click projection for all nine provider
  identities, waiting/received transition, final collapse, history and next turn.
- `test/wor311_public_progress_test.dart` — no-click initial public narration.
- `test/wor254_transport_badge_test.dart` — no-click live/localized text and
  retained reduced-motion close/reopen checks.

Documentation: `docs/WOR-340-thinking-v5.md`. V1–V4 reports remain unchanged.
Test expectations were changed only where V5 explicitly supersedes the V4
collapsed default; existing animation, privacy and terminal assertions remain.

## Focused checks

- **84 Python tests passed** across V4/V5 UI, Thinking, public progress, unified
  PC Chat UI and cancellation Thinking files. There were 18 existing FastAPI
  `on_event` deprecation warnings.
- **48 Flutter tests passed** across Thinking UI, public progress, transport
  badge rendering and unified Chat projection files.
- Focused Flutter analysis of the UI source and four changed tests: **no issues
  found** (29 seconds).
- Both `git diff --check` runs passed; line-ending notices are Git warnings only.
- Credential-pattern scan across 11 V5 files: **zero matching locations**.
- `py_compile` and imports passed for the changed Python UI source and four
  affected Python test modules.
- Existing offscreen queued-thread, live rendering, flat styling, periodic seam,
  animation cleanup, reduced-motion, completion and history checks passed.
- Same-turn accepted/snapshot re-projection retains manual collapse; Mobile
  remounting retains the same exact-turn UI memory. No transport implementation
  was changed or live reconnect/provider session initiated.

## Preserved scope and Git

SHA-256 comparison against a beginning-of-V5 inventory of 244 product Python/Dart
source files found only the two disclosure files above changed. Agent, providers,
commentary extraction, reasoning settings, trace content, tool dispatch, Chat
screen, both dot painters, colors, text size, final-answer streaming, transport,
authentication and installer source remain as they were before V5.

Both repositories remain on `main` with unchanged HEADs:

- Desktop: `509e17c80156fc366eaddad31b1205aece98b8e6`
- Mobile: `8c9a974386afbae3c8eaeac4dfed4139a74a54a3`

Nothing staged, committed, pushed, published, installed or deployed. Prior
modified/deleted/untracked work is preserved. Temporary baseline copies and
test logs are outside the repositories in the OS temporary directory.
All V5 files stay uncommitted for operator review. Paths outside the V5 file
lists in the final inventories below remain earlier work, deliberately untouched.

## Linear and operator check

WOR-340 remains **In Progress** pending operator confirmation. The dedicated V5
comment records the explicit old initializers, four-line source correction,
manual/terminal behavior, preserved scope and focused results.

Send a safe request using any working provider. Confirm dots appear and genuine
commentary appears automatically without clicking. Collapse during work, wait
through another tool round, then reopen. Send another message and confirm it
starts expanded. Confirm final completion alone collapses and exposes the
Thinking label. Repeat on connected Mobile and after reconnect; completed
history should stay collapsed and no previous turn's text should appear.


## Final uncommitted inventories

### Desktop

```text
 D .claude/settings.local.json
 M osia/agent.py
 M osia/chatgpt_account/app_server_client.py
 M osia/execution_trace.py
 M osia/gui/main_popup.py
 M osia/gui/thinking_orbs.py
 M osia/i18n.py
 M osia/main.py
 M osia/relay_connector.py
 M osia/server/server.py
 M osia/voice/tts.py
 M tests/test_wor247_chat_streaming.py
 M tests/test_wor265_phase2a_pc_unified_chat_ui.py
 M tests/test_wor269_cancellation_thinking.py
 M tests/test_wor311_public_progress.py
?? .kilo/
?? .playwright-mcp/
?? benchmarks/wor289-phase2a/
?? benchmarks/wor292-systemic/
?? benchmarks/wor292-visual/
?? benchmarks/wor292/
?? benchmarks/wor294-293/
?? benchmarks/wor295-conditional-review/
?? benchmarks/wor295-judge-reliability/
?? benchmarks/wor295-prospective/
?? benchmarks/wor295-reference-v2/
?? benchmarks/wor295-reference-v3/
?? benchmarks/wor295-residual/
?? benchmarks/wor295-route-resumption/
?? benchmarks/wor295-semantic/
?? benchmarks/wor296-entailment/
?? benchmarks/wor296-reachability/
?? benchmarks/wor297/
?? config/semantic_v2_diagnostics/
?? docs/WOR-287-thinking-lifecycle.md
?? docs/WOR-305-architecture-audit.md
?? docs/WOR-305-phase2-report.md
?? docs/WOR-305-phase2-validation-plan.md
?? docs/WOR-305-report.md
?? docs/WOR-305-runtime-validation.md
?? docs/WOR-340-thinking-v3.md
?? docs/WOR-340-thinking-v4.md
?? docs/WOR-340-thinking-v5.md
?? docs/WOR-340-thinking.md
?? docs/wor285-mobile-evidence/
?? docs/wor298-live-evidence.json
?? docs/wor305-evidence/
?? docs/wor305-phase2-evidence/
?? docs/wor305-phase3a/
?? docs/wor305-phase3b/
?? docs/wor305-phase3c/
?? docs/wor305-phase4a/
?? docs/wor305-phase4a1/
?? docs/wor305-phase4b/
?? docs/wor305-phase4b1/
?? docs/wor305-phase4b2/
?? docs/wor305-phase4b3/
?? docs/wor305-phase4b4/
?? docs/wor305-phase4b5/
?? docs/wor305-phase4b6/
?? docs/wor305-phase4b7/
?? docs/wor305-phase4b8b/
?? docs/wor305-ws-claim-lifecycle/
?? osia/evidence_acquisition_v2.py
?? osia/execution_guidance.py
?? osia/goal_contract_v2.py
?? osia/goal_contract_v2_repair.py
?? osia/grounded_v4_claims.py
?? osia/production_semantic_authority_v2.py
?? osia/public_commentary.py
?? osia/semantic_completion_v2.py
?? osia/semantic_constraint_authority_v2.py
?? osia/semantic_execution_loop_v2.py
?? osia/semantic_v2_chat.py
?? osia/semantic_v2_input.py
?? osia/semantic_v2_model.py
?? osia/semantic_v2_permission_bridge.py
?? osia/utils/diagnostics.py
?? osia/visual_entailment_v2.py
?? osia/visual_target_continuity_v2.py
?? osia/wor266_capture_ab_probe.py
?? osia/wor266_live_probe.py
?? osia/wor289_physical_benchmark.py
?? osia/wor289_unified_probe.py
?? osia/wor289_v4_probe.py
?? osia/wor289_wgc_lifecycle_probe.py
?? osia/wor291_v2_probe.py
?? osia/wor292_semantic_loop_probe.py
?? osia/wor293_native_probe.py
?? tests/test_wor266_capture_ab_probe.py
?? tests/test_wor266_final_actuation_gates.py
?? tests/test_wor266_followup_continuity.py
?? tests/test_wor266_option_a_composed.py
?? tests/test_wor266_option_a_guards.py
?? tests/test_wor289_desktop_browser_scope.py
?? tests/test_wor289_goal_contract_generalization.py
?? tests/test_wor289_multi_action_checkpoint.py
?? tests/test_wor289_physical_benchmark.py
?? tests/test_wor290_execution_guidance.py
?? tests/test_wor291_goal_contract_v2.py
?? tests/test_wor292_constraint_authority.py
?? tests/test_wor292_live_cutin_readiness.py
?? tests/test_wor292_semantic_execution_loop_v2.py
?? tests/test_wor292_systemic_application_binding.py
?? tests/test_wor292_visual_continuation.py
?? tests/test_wor292_visual_protocol.py
?? tests/test_wor293_live_gate.py
?? tests/test_wor293_production_authority.py
?? tests/test_wor294_conditional_no_effect.py
?? tests/test_wor295_conditional_completion_review.py
?? tests/test_wor295_semantic_completion.py
?? tests/test_wor296_campaign_oracle.py
?? tests/test_wor296_evaluation_protocol.py
?? tests/test_wor296_evidence_material_budget.py
?? tests/test_wor296_frozen_simulation_boundary.py
?? tests/test_wor296_planning_policy_delivery.py
?? tests/test_wor296_safe_reachability.py
?? tests/test_wor296_visual_entailment.py
?? tests/test_wor297_surface_transitions.py
?? tests/test_wor299_hardening.py
?? tests/test_wor299_multieffect_contract.py
?? tests/test_wor299_turn_persistence.py
?? tests/test_wor299_verification_continuation.py
?? tests/test_wor305_phase3b_completion.py
?? tests/test_wor305_run_command_process_truth.py
?? tests/test_wor305_runtime_startup.py
?? tests/test_wor340_provider_timelines.py
?? tests/test_wor340_thinking.py
?? tests/test_wor340_thinking_v4_ui.py
?? tools/wor266_prompt_audit.py
?? tools/wor289_desktop_browser_escape_probe.py
?? tools/wor298_deepseek_probe.py
?? tools/wor302_groq_isolate.py
?? tools/wor302_live_probe.py
?? tools/wor303_live_probe.py
?? tools/wor305_phase2_tests.py
?? tools/wor305_phase3a_smoke.py
?? tools/wor305_runtime_probe.py
?? tools/wor305_ws_claim_smoke.py
```

### Mobile

```text
 M README.md
 M lib/l10n.dart
 M lib/screens/chat_screen.dart
 M lib/services/execution_trace.dart
 M lib/widgets/execution_trace_block.dart
 M lib/widgets/thinking_orbs.dart
 M test/wor254_transport_badge_test.dart
 M test/wor257_reflection_prose_test.dart
 M test/wor265_unified_chat_ui_test.dart
 M test/wor311_public_progress_test.dart
?? test/wor340_thinking_test.dart
?? test/wor340_thinking_v4_ui_test.dart
```

OSIA THINKING V5: AUTO-EXPAND FIX IMPLEMENTED — AWAITING OPERATOR VALIDATION
