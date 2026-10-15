# WOR-340 — Thinking V2 correction

Date: 11 October 2026. Status: implementation corrected; **In Progress pending operator validation**.

This report supersedes the earlier WOR-340 implementation report. The first correction was incomplete: it still generated coarse Activity sentences, opened only after useful content, kept Desktop's tinted text panel, and did not repair commentary retention across provider rounds and partial snapshots.

## Confirmed causes and evidence

1. **Expansion:** Desktop `_ChatThinkingTrace.apply_trace` opened only inside `if detail and not self._content_seen`; `_set_expanded` refused an empty detail. Mobile `_updateDisclosure` required `_hasContent`; its waiting `ThinkingDisclosure` always showed a closed chevron. Mobile send/retry paths also set `_thinking = false` while awaiting a reply. These directly contradicted the new immediate-expansion requirement.
2. **Generic narration:** Desktop `_safe_payload` mapped action/result keys through `_DISPLAY_KEYS`, `chat.thinking.*` translations, and an Activity prefix. Mobile `formatExecutionTraceSummary` translated the same coarse events through `executionTraceText`; unused older summary/completed-action/recovery generators and translations also remained in l10n.dart. None of this text was selected-model commentary.
3. **Lost wording across provider rounds:** `ExecutionTraceLedger.record_public_reasoning_summary` used one `provider_public_reasoning_summary` dedupe slot for the entire canonical Chat turn. `record` replaced the existing event at that slot. `OSIAAgent._emit_provider_public_reasoning_summary` checked native thread/turn IDs for presence but discarded them before recording. Consequently, a later native provider turn overwrote earlier public summary wording. The regression test reproduces this with two native turns inside one canonical turn.
4. **Lost wording across snapshots:** Desktop `apply_trace` assigned the incoming safe binding, source payload and text wholesale. A snapshot with a higher maximum sequence could omit the commentary event, pass freshness checks, then execute `setPlainText` with only Activity lines. MainPopup's saved projection did the same, including when the conversation was hidden. Mobile's accumulator already merged partial envelopes, but rejected its accumulated view after 24 events/12 KiB; history and screen/widget snapshots could also replace richer live content.
5. **Retention bounds:** the public sanitizer truncated summaries to 1,600 characters; narration rejected more than 500 characters and stopped after eight summary events. Canonical compaction/validation imposed 24-event/12-KiB limits, and the account adapter cut public-summary input at 8,192 characters/32 parts. These could discard legitimate public wording.
6. **Background:** Desktop `QTextEdit#chatThinkingDetail` explicitly used a tinted rgba background and left border. Flutter's current expanded content was already Padding/SelectionArea/Text; it needed no decorative replacement.
7. **Desktop-origin Mobile gap:** `main.py`'s `on_execution_trace` emitted only to the Qt bridge. Only the server-origin agent callback called `_broadcast_execution_trace`. Desktop-origin live commentary therefore lacked a forwarding path to Mobile.

Read-only inspection of existing local conversation trace metadata found 13 traces and eight public-summary events, all from chatgpt_account / gpt-6-luna. A tool-heavy saved turn had 18 events and one summary event at revision nine. No private conversation bodies, credentials or hidden reasoning were printed. Saved records contain the final summary revision, not earlier wording; they cannot reconstruct the operator's precise disappearing-text sequence. The source-confirmed loss paths above were reproduced by focused fixtures and corrected. Previously discarded text cannot be recovered from those final-only records.

## Generator removal

Desktop's Thinking action/result display map and automatic Activity formatting were removed. Its optimistic Stop tap no longer appends a Cancelling Activity sentence. All obsolete `chat.thinking.*` narration translations were removed in every supported language, including French; only title, expand, collapse and Stop control labels remain.

This removes the origins of “Using the selected capability”, “The observation reached the selected model”, “Running the selected command”, “The action completed”, “The observed result needs attention”, and the old verification/strategy/waiting/terminal sentence variants.

Mobile's action/result-to-prose formatter branch was removed. The legacy executionTraceText, summary-clause/summary, completed-action, recovered-action, blocked-action and natural-text generators, plus their multilingual narration tables, were deleted. Coarse goal/action/result/terminal keys remain structured facts; no client converts them into Thinking sentences.

Structured execution receipts, ToolAttempt, ActionTruth and the ledger's factual events remain internally available. No new model call, summarizer, planner, provider-specific fallback, router or semantic gate was introduced. No raw tool arguments/results were added to public output.

## Retention and presentation

- Provider summaries use native thread plus native turn identity for their dedupe slot. Revisions of an aggregate extend that entry; separate native turns append distinct entries. Shrinking aggregates cannot erase wording, and a distinct statement does not overwrite an already displayed statement.
- Canonical snapshots retain every sanitized public commentary entry. Diagnostic facts keep their existing compact projection budget. Transport limits are separate from canonical storage: Relay retains six incremental trace frames plus one terminal frame, eight terminal snapshot events and 4 KiB terminal size. Its old 12-KiB incremental size bound is enforced at the Relay boundary. LAN/WebRTC can receive the complete public trace.
- Desktop widgets and MainPopup merge validated snapshots by exact conversation/request/turn/run and provider/model/reasoning identity. Missing entries do not remove received wording; stale revisions and mismatched bindings remain rejected. Generic events cannot overwrite a summary event.
- Mobile merges snapshots in its widget disclosure memory and an in-memory screen projection cache, including history hydration with missing/incomplete traces. The cache contains only PC-authored validated events and creates no durable Mobile store or execution authority. Its accumulator retains received commentary beyond the former canonical size limits.
- New Desktop turns start expanded even with no text. Empty active rows remain clickable, so manual collapse before the first commentary is respected. Each new turn has independent defaults.
- Mobile send/retry paths show the waiting row immediately. The row starts expanded, permits manual collapse, and transfers that disclosure choice into the canonical trace row when its identity arrives.
- Answer chunks only mark streaming; they do not collapse Thinking. Canonical terminal/final/failure or committed cancellation collapses once. Duplicate completion does not undo manual reopening. Empty completed rows disappear. Received commentary stays reopenable after cancellation.
- Desktop text background is transparent, with no border, card, glass, gradient or shadow; its 10px detail size and existing font/spacing are preserved. Flutter retains plain 13px text at 1.38 line height without a decorated container. Existing accent-aware orbs and chevrons remain; unrelated bubbles are unchanged.
- Existing Qt signal delivery remains queued through the current bridge. The crash repair's `set_active` method remains intact.

## Desktop-origin forwarding

A single background FIFO sender forwards only validated existing execution_trace frames over the established localhost process bridge. The new **internal HTTP route** `/internal/execution_trace` validates loopback origin, allowed public fields and the PC's persisted exact execution binding. It announces the existing turn_accepted event once per delivered turn, broadcasts the existing execution_trace feed, then emits the existing terminal turn_status after a terminal trace.

There are no new PC-to-Mobile WebSocket message types or fields, no new pairing flow, no transport switching changes, and no independent execution runtime. The FIFO sender never blocks Qt or provider execution. Delivery failures remain optional projection failures. Existing history recovery remains available. Mocked endpoint/sender tests verify order, terminal delivery, public validation, binding rejection and nonlocal rejection; Mobile tests consume Desktop acceptance without a local send and retain commentary through a compact terminal snapshot.

## Changed files and call sites

Desktop files changed in this V2 pass:

- `osia/agent.py`: provider public-summary callback now carries native source identity into the ledger.
- `osia/execution_trace.py`: append/retention rules, canonical compaction/validation, public wire-frame validator; called by the agent, persistence, widgets, Relay and forwarding route.
- `osia/chatgpt_account/app_server_client.py`: retain designated public summary parts/input without artificial truncation; the existing provider callback path is unchanged.
- `osia/gui/main_popup.py`: widget/MainPopup snapshot merges, immediate empty expansion, terminal/manual handling, cancellation retention and flat detail CSS.
- `osia/i18n.py`: delete all Thinking narration translations.
- `osia/main.py`: Desktop trace callback connects to an ordered optional Mobile sender.
- `osia/server/server.py`: loopback route forwards the same canonical events after binding validation.
- `osia/relay_connector.py`: keep the existing incremental frame size bound at the transport boundary.
- `tests/test_wor340_thinking.py`: new V2 runtime, retention, UI, provider and forwarding regressions.
- `tests/test_wor311_public_progress.py`, `tests/test_wor265_phase2a_pc_unified_chat_ui.py`: replace assertions that explicitly conflict with V2; retain crash/stream/correlation checks.
- `docs/WOR-340-thinking.md`: this report.

Mobile files changed in this V2 pass:

- `lib/widgets/execution_trace_block.dart`: real-public-text-only rendering, immediate disclosure, waiting-row manual override, snapshot retention and completion behavior.
- `lib/screens/chat_screen.dart`: immediate send/retry indicator, exact-turn history/live retention and cancellation projection.
- `lib/services/execution_trace.dart`: canonical retention beyond transport limits and validated snapshot merges; all existing event parsers and accumulator callers remain compatible.
- `lib/l10n.dart`: remove multilingual narration generators and tables.
- `test/wor340_thinking_test.dart`: V2 widget/retention/flat-style/long-history/Desktop-origin transport regressions.
- `test/wor254_transport_badge_test.dart`, `test/wor257_reflection_prose_test.dart`, `test/wor265_unified_chat_ui_test.dart`: replace stale synthesized-prose expectations with real commentary or empty tool-only traces.

## Verification

Final focused Python run: **293 passed**, 18 existing FastAPI on_event deprecation warnings, 30.61 seconds. Covered Thinking V2, public progress, designated account summaries, trace persistence/Relay, Qt Chat lifecycle, cancellation, DeepSeek catalog/parser, native/text provider tool lifecycle, Groq XML/text, canonical streaming, Mobile parity, ActionTruth, durable transport, recovery edges and repaired startup.

Final Mobile run: **65 passed**, covering Thinking V2, trace/privacy/transport badges, removed narration, unified Chat, public progress, history/recovery, durable transport and global reconnect. **flutter analyze --no-pub: no issues found**.

Every changed Python source/test compiles with py_compile. All eight touched Python runtime modules import successfully. Both repositories' git diff --check pass. A credential-signature scan of all 19 V2 source/test candidate files found no credential signatures; no credentials were displayed or added. No staging, commit, push, publication, installer rebuild, store deployment, live provider request or physical mouse/keyboard automation was performed.

## Provider limitations and operator validation

ChatGPT Account uses its existing designated public-summary feed. Compatible selected providers can contribute public tool-round commentary when actually returned; this change adds no provider-specific summary feed to OpenAI API, DeepSeek, Gemini, Groq, xAI, Mistral or Ollama. Anthropic integration remains unchanged. Raw hidden reasoning is excluded for every provider. A provider with no legitimate public commentary has only the neutral active indicator and empty expanded content; there is no generated narration.

Relay intentionally exposes less trace content within its unchanged budget. It cannot transmit every long entry; previously received Mobile commentary is retained, and the PC canonical trace keeps the complete sanitized content. Previously discarded historical wording cannot be reconstructed.

Operator steps using builds/source containing these edits:

1. Send a fresh message on Desktop and Mobile. Confirm the chevron opens immediately, before commentary, with neutral accent-aware animation.
2. Collapse while waiting, then let commentary/tool events arrive. Confirm it stays collapsed. Open it and verify original selected-model wording remains through multiple tool rounds and the final answer.
3. Confirm answer streaming and tool results do not collapse Thinking; final completion/failure/committed cancellation does. Reopen completed/cancelled nonempty content; verify no Activity templates appear.
4. Inspect normal and any light/dark/custom theme: the detail has no background/border/box and retains readable font size. Empty completed traces should disappear.
5. Start another turn and switch conversations. Verify no older trace appears under it. Test Stop and stale callbacks, then reopen historical commentary.
6. Test Desktop-origin commentary on Mobile over LAN/WebRTC and Relay; reconnect and hydrate history. Confirm exact-turn isolation, no lost already-received commentary, unchanged pairing/transport and unchanged final answers. Relay's reduced-detail behavior is expected.
7. Check each real provider available to the operator. Absence of commentary is acceptable; fabricated substitutes are not.

Linear WOR-340 remains In Progress awaiting this operator verification. Installed binaries were not rebuilt, and no claim of real-device visual or live-provider validation is made.

## Git state

Desktop remains on main at 509e17c80156fc366eaddad31b1205aece98b8e6; Mobile remains on main at 8c9a974386afbae3c8eaeac4dfed4139a74a54a3. Nothing is staged or committed. V2 changes remain uncommitted for operator review, together with the earlier requested crash/Thinking changes.

Pre-existing unrelated Desktop modifications to `.claude/settings.local.json` (deleted) and `osia/voice/tts.py`, and existing test/untracked work were preserved. Mobile's pre-existing README.md and test/wor311_public_progress_test.dart changes were preserved. No reset, clean, stash, forced addition or push was used. Generated test logs stayed in the system temporary directory. The full remaining uncommitted path list follows; paths outside the V2 list above were intentionally left as found.

### Desktop remaining uncommitted paths

```text
 D .claude/settings.local.json
 M osia/agent.py
 M osia/chatgpt_account/app_server_client.py
 M osia/execution_trace.py
 M osia/gui/main_popup.py
 M osia/i18n.py
 M osia/main.py
 M osia/relay_connector.py
 M osia/server/server.py
 M osia/voice/tts.py
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
?? tests/test_wor340_thinking.py
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

### Mobile remaining uncommitted paths

```text
 M README.md
 M lib/l10n.dart
 M lib/screens/chat_screen.dart
 M lib/services/execution_trace.dart
 M lib/widgets/execution_trace_block.dart
 M test/wor254_transport_badge_test.dart
 M test/wor257_reflection_prose_test.dart
 M test/wor265_unified_chat_ui_test.dart
 M test/wor311_public_progress_test.dart
?? test/wor340_thinking_test.dart
```

OSIA THINKING V2: CORRECTED — AWAITING OPERATOR VALIDATION
