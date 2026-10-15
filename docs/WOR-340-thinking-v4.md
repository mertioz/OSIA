# WOR-340 V4 — Thinking UI rendering and control

Implemented in the Desktop and Mobile working trees. Operator visual validation
is pending. V3 provider functionality was confirmed by the operator before V4.
This change is confined to UI source, focused tests, and this report.

## 1. Exact Desktop rendering bug

`_ChatThinkingTrace.__init__` explicitly hides `_detail` and gives it a maximum
height of zero. Opening an empty trace also leaves the text widget hidden.
When the first public commentary arrives, `apply_trace` updates the existing
QTextDocument and increases `maximumHeight` if `_expanded` is true, but the V3
path never calls `show()` or `setVisible(True)`. Logical expansion and positive
height do not undo explicit Qt widget hiding.

An offscreen reproduction against the V3 working tree returned:

```text
expanded=True, text_received=True, explicitly_hidden=True, maximum_height=180
after_reopen_hidden=False
```

This establishes a widget visibility defect, rather than a missing provider
notification, absent commentary, stale binding, or delayed repaint. The text was
already in the QTextDocument before the click.

## 2. Why closing and reopening worked

The existing `_set_expanded` click path calls `_detail.show()` when text exists.
The normal live-update path only changed height. That difference explains the
operator's observed recovery after closing and reopening.

## 3. Immediate live rendering

`apply_trace` now projects explicit visibility from the received text and chosen
disclosure state, along with the existing height constraint. It stops an obsolete
height animation when content/visibility changes so animation values cannot
keep a newly received document collapsed. Unchanged receipts do not recreate
the text widget or document. Existing append-only insertion, revision rejection,
partial snapshot merge, scrolling, sanitization and exact-turn binding remain.

The offscreen regression opens an empty trace, receives a first summary and a
revision, checks visible positive-height content immediately after queued UI
delivery, retains the same document object, and tests several source entries,
duplicate receipts, manual collapse, reopening, and stale-turn rejection.

Mobile already rebuilds received trace content through `didUpdateWidget` and its
retained canonical snapshot. No missing live notification was demonstrated.
Focused widget checks verify first-click and first-receipt rendering. They did
expose a separate reduced-motion layout error: a zero-duration `AnimatedSize`
could invalidate itself in `performLayout` when opening the trace at text scale
3. Reduced motion now bypasses AnimatedSize and lays out the content directly.
The previously failing large-text case now passes.

## 4. Active dots interaction

New turns start compact and collapsed. Only the six-dot control is visible in
the Thinking header during active work. Clicking it opens/closes the received
text, including opening before text exists. Later receipts do not override the
choice. Desktop uses a native checkable QPushButton for the dots, keyboard focus
and Space activation, native button semantics, an accessible Thinking name and
expand/collapse description reflecting the disclosure state, and a painted
focus indicator. The separate Stop control retains its existing dispatch.

Mobile uses the same disclosure control for waiting and received-trace states,
with InkWell keyboard interaction and focus color, explicit Semantics button,
expanded state, accessible label and tap action, and a minimum 54×48 target.
Disclosure memory is keyed to the exact PC trace; a different key resets even
an accidentally reused memory object. No new Mobile commentary source exists.

## 5. Terminal Thinking label

Canonical completion collapses once, stops/hides dots, and exposes the recorded
Thinking label and chevron if displayable text exists. Desktop renders
`Thinking ▸` in English; Mobile uses the localized label and existing chevron
icon. Clicking the final control reopens the retained text. Duplicate terminal
receipts preserve a manual reopening. Empty completed traces disappear.

Draft final-answer chunks and tool/provider rounds do not settle the control.
The existing failure/cancellation terminal paths retain received text under a
neutral Thinking label; no success claim or fabricated completion text is added.
Desktop keeps a pending cancellation control in the unsettled visual state.
Mobile's `_thinkingTraceActive` no longer treats `cancellationRequested` alone
as terminal; Stop dispatch and cancellation state handling remain unchanged.

## 6. Seamless animation

V3 reduced time to an 1800 ms cycle, then multiplied phase by fractional
frequencies (including 0.82, 1.06 and 1.21). Those waveforms had different
positions/scales when the cycle wrapped from 2π to zero, causing the snap.

Both painters now use one 1800 ms periodic phase with six evenly spaced offsets:

```text
angle_i = phase + i·2π/6
x = center_x + 15.4·cos(angle_i)
y = center_y + 3.7·sin(angle_i)
radius = 3.1 + 0.4·sin(angle_i)
opacity derives continuously from y, with no zero-opacity interval
```

Position, radius, opacity input and velocity match at both cycle ends. Focused
numeric tests compare the endpoints and finite-difference velocities. Motion
is a restrained elliptical orbit, with the existing six-dot footprint and
accent-aware depth shading. Widgets/controllers are stable across receipts;
there is no per-cycle destruction, pause, blank frame or restart workaround.

Desktop retains the parent-owned QTimer and elapsed clock, stops the timer on
hide/inactivity/reduced motion, and retains the existing 250 ms exposure check
when clipped by scrolling rather than repainting offscreen. Mobile retains its
single controller and scroll/lifecycle/TickerMode visibility gating, stopping
for inactive/background/hidden/reduced-motion states and disposing with the
widget. Reduced motion displays static dots while active.

## 7. Desktop files changed for V4

| File | Change |
| --- | --- |
| `osia/gui/main_popup.py` | Explicit live visibility; compact default; active/final control selection and disclosure accessibility |
| `osia/gui/thinking_orbs.py` | Native button/focus and true periodic geometry |
| `tests/test_wor340_thinking_v4_ui.py` | Five new offscreen rendering, keyboard, seam, cleanup and queued GUI-thread checks |
| `tests/test_wor340_thinking.py` | Existing lifecycle checks now explicitly open the compact default |
| `tests/test_wor311_public_progress.py` | Replace prior automatic-expansion assumption with dots activation |
| `tests/test_wor265_phase2a_pc_unified_chat_ui.py` | Compact default, hidden active label and explicit activation |
| `docs/WOR-340-thinking-v4.md` | Investigation, verification and inventories |

## 8. Mobile files changed for V4

| File | Change |
| --- | --- |
| `lib/widgets/execution_trace_block.dart` | Shared accessible dots control; compact default; exact-key disclosure reset; direct reduced-motion layout |
| `lib/widgets/thinking_orbs.dart` | Matching true periodic geometry; existing controller lifecycle preserved |
| `lib/screens/chat_screen.dart` | UI activity projection waits for committed cancellation rather than the Stop request |
| `test/wor340_thinking_v4_ui_test.dart` | Five new live-control, keyboard, exact-turn, seam and lifecycle checks |
| `test/wor340_thinking_test.dart` | Explicit user activation for V4 default; previous retention/provider parity coverage preserved |
| `test/wor311_public_progress_test.dart` | Explicit opening before checking live prose |
| `test/wor265_unified_chat_ui_test.dart` | Waiting control has dots and no visible label |
| `test/wor254_transport_badge_test.dart` | Explicit opening, localized accessible active labels/final visible label, large-text reduced-motion checks |

Only earlier auto-expansion/active-label expectations conflicting with the V4
request were changed. No obsolete runtime behavior was restored to satisfy tests.

## 9. Focused verification

- Python: **98 passed**, 18 existing FastAPI `on_event` deprecation warnings.
  Six files: V4 UI, V2/V3 Thinking, public progress, unified PC Chat UI,
  cancellation Thinking, and multi-provider timeline fixtures.
- `py_compile` for both changed Python source files and all four affected Python
  tests passed. Imports of the source modules and affected test modules passed.
- Queued QThread signal receipt was applied on QApplication's GUI thread.
  Tests run in Qt offscreen mode; no physical mouse/keyboard interactions.
- Desktop and Mobile seam tests verify endpoint/velocity continuity; cleanup
  tests cover hiding/terminal or inactive/reduced-motion/background conditions.
- Flat transparent content styles and existing font size are unchanged and
  covered by the existing theme/style assertions.

Mobile: **79 passed** across ten focused UI, canonical trace, transport/recovery
and reconnect test files. Final `flutter analyze --no-pub`: **no issues found**
(57.3 seconds). Both repositories passed `git diff --check`; existing line-ending
normalization notices are Git warnings, not whitespace failures. Credential
pattern scan across all 15 V4 candidate files: **zero matching locations**.
Initial failures from V2 automatic-expansion assertions were
updated to the explicitly requested V4 UX. The actual reduced-motion layout
failure was fixed in source and its existing regression retained.

## 10. Preserved working pipeline

No V4 edits to agent/provider/tool execution, reasoning parameters, public
summary generation, authentication, ActionTruth, trace storage or transport.
SHA-256 comparison against the beginning of V4 confirmed byte-identical:
`osia/agent.py`, `osia/public_commentary.py`,
`osia/chatgpt_account/app_server_client.py`, `osia/execution_trace.py`,
`osia/server/server.py`, and `osia/relay_connector.py`.
These files remain dirty from earlier work, not from V4.
LAN/WebRTC/Relay messages and traffic limits were unchanged. V3 multi-provider
timeline fixtures passed unchanged. No live provider request was made for V4.
No browser launch, installer, release, authentication or macOS work was performed.

## 11. Git state

Both repositories remain on `main`, with unchanged HEADs:

- Desktop: `509e17c80156fc366eaddad31b1205aece98b8e6`
- Mobile: `8c9a974386afbae3c8eaeac4dfed4139a74a54a3`

All V4 changes remain uncommitted for operator review. Nothing was staged,
committed, pushed, published or deployed. Existing modified/deleted/untracked
paths were preserved. Full final inventories below identify remaining paths;
anything outside the V4 tables above was intentionally left as pre-existing work.
Local test output and baseline copies are in the OS temporary directory only.

## 12. Linear

WOR-340 remains **In Progress** pending operator visual validation. V4 findings,
changes, scope and verification are recorded in a dedicated issue comment.

## 13. Operator visual verification

1. Use the local Desktop application with an already working configured provider.
   Start a safe task that produces genuine commentary over several tool rounds.
   Confirm dots alone appear, trace starts collapsed, and Stop remains usable.
2. Open dots before the first commentary arrives. Check that received text
   appears without another click. Also open after text has arrived; first click
   must reveal the available text. Watch several live revisions/tool rounds.
3. Close the trace and check that new updates keep it closed. Reopen it and
   verify original text, order and spacing, with no duplicate entries.
4. Watch dots through several 1.8-second cycles for a seamless orbit with no
   snap, blank frame or simultaneous reset. Use Tab/Space on Desktop and a
   Mobile hardware keyboard where available to check activation and focus.
5. During final-answer streaming, confirm there is still no visible Thinking
   header. On canonical completion, confirm dots disappear, trace collapses and
   the compact Thinking label reopens the correct recorded text.
6. Try a turn without public commentary, a cancellation, and a terminal failure.
   Empty completed traces must leave no useless row; legitimate recorded text
   stays reopenable without success claims. Check another conversation and a
   new turn for text/disclosure isolation.
7. On connected Mobile repeat opening before/after text, toggling during tool
   rounds, terminal reopening and reconnect. Verify the 48-pixel tap target,
   flat content, French/English headers, reduced motion and large system text.
   No new transport setting or separate source is required.

Automated tests establish rendering/state behavior and numeric continuity;
actual perceived animation, OS focus/screen-reader presentation, and connected
operator sessions remain the final manual validation gate.


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

The V4 file tables identify the paths changed in this turn. All other listed
paths are preserved earlier work; they were neither staged nor overwritten.

OSIA THINKING V4: UI POLISH IMPLEMENTED — AWAITING OPERATOR VALIDATION
