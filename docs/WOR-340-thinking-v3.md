# WOR-340 — Thinking V3 multi-provider investigation

11 October 2026. Implementation and deterministic validation completed; operator testing remains pending. Linear remains **In Progress**. V2 changes and the Desktop crash correction remain in the working tree.

## Evidence and conclusion

Three independently demonstrated client defects explain why a working Account summary feed does not establish that the other Thinking paths work: canonical non-Account calls were non-streaming when final callbacks were suppressed; streaming discarded native tool-call deltas; typed Mistral content was consumed as if it were a string. Account also ignored explicit public message phases and delayed short summary updates behind a 160-character emission gate. These paths are corrected.

This is source inspection, official contract verification, SDK HTTP fixtures, ordered streaming fixtures, canonical Chat execution with mocked provider/tool responses, Qt tests and Flutter tests. No live provider request, credential use, physical UI operation, installed build, authentication change, installer rebuild, commit or publication occurred. The operator's report establishes that Account/GPT-6 Luna displays commentary in their live session; the other providers' live output remains unverified. Fixtures establish OSIA's handling of documented structures, not a promise that a model emits commentary on every request.

## 1. Working ChatGPT Account path

The existing App Server request opts into `summary: concise` when a consumer is bound. Exact native thread/turn-correlated `item/reasoning/summaryPartAdded` and `item/reasoning/summaryTextDelta` feed the public callback even during a non-streamed compatibility completion. `item/reasoning/textDelta` is explicitly ignored. The adapter callback carries the existing OSIA conversation/request/turn/run/provider/model identities; the agent checks them and the execution generation before recording the trace.

V3 forwards nonempty summary seeds and each changed sanitized delta at receipt, preserving spaces when subsequent deltas extend a seed. It also routes `agentMessage` items with explicit `phase: commentary` to that public callback, separately identified by native item ID. `phase: final_answer` and phase-absent legacy output keep their reply path. A completed public item delivered without deltas is emitted once at completion, without simulated earlier streaming. No authentication or account-selection code changed. [Official App Server item contract](https://learn.chatgpt.com/docs/app-server#items).

## 2–4. Factual provider support matrix

Registry: `osia/models.py`, nine provider IDs. Account uses the existing Codex adapter; all eight API/local paths use OpenAI-compatible **Chat Completions**, including Anthropic's implemented compatibility endpoint. This task does not add native Messages, Gemini generateContent, or Responses integrations. Reasoning controls in `osia/reasoning.py` select computation settings; they are not evidence of a public commentary channel.

| Provider | Public reasoning summary on implemented OSIA path | Ordinary pre/between-tool text and streaming | Handling and limitation |
| --- | --- | --- | --- |
| ChatGPT Account / Codex | Existing designated summary notifications; explicit public message phase also supported | Live summary/message callbacks | Working operator evidence plus fixtures. Correlated summaries and commentary retained; final phase remains final. |
| DeepSeek (`deepseek-flash`) | No designated public summary feed consumed; `reasoning_content` is CoT | Compatible `content` can accompany native/textual calls and stream | V3 forwards actual public tool-round content. Silent calls remain silent. [DeepSeek contract](https://api-docs.deepseek.com/guides/thinking_mode/). |
| OpenAI API | OSIA calls Chat Completions, not Responses summary events | `delta.content` and indexed `delta.tool_calls` | V3 retains fragmented native calls and public tool-round text. Do not infer Responses capabilities from this path. [Chat stream contract](https://developers.openai.com/api/reference/resources/chat). |
| Google Gemini | Current request does not opt into `include_thoughts`; no verified summary-event mapping in this adapter | Google documents compatible content streaming and function calls | Ordinary tool-round prose uses the shared corrected path. Native thought summaries and compatible opt-in exist in provider documentation, but their delivery is not enabled or live-verified here. No guessed `reasoning_content` mapping or reasoning-setting change. [Compatibility contract](https://ai.google.dev/gemini-api/docs/openai). |
| Groq | Parsed/raw reasoning is internal reasoning, not a public summary feed | Compatible text/native calls; OSIA also parses Groq XML/text calls | Hidden reasoning and `<think>` remain excluded; valid public residual text streams when tool context is established. [Groq reasoning contract](https://console.groq.com/docs/reasoning). |
| xAI | No designated summary feed consumed by OSIA's Chat Completions route | Compatible public content/tool calls | Private reasoning/encrypted reasoning is not a substitute for commentary. Actual public tool-round text uses V3. [xAI reasoning contract](https://docs.x.ai/developers/model-capabilities/text/reasoning). |
| Mistral | `type: thinking` contains reasoning traces, not public commentary | Content may be a string or typed chunk list; streamed text/tool calls | V3 selects only `type: text` chunks and preserves native calls. It never stringifies a thinking chunk or exposes its nested text. [Mistral chunk contract](https://docs.mistral.ai/studio/conversations/reasoning). |
| Ollama | No public summary feed consumed on current Chat Completions route | Streaming/tools depend on the installed model/server | Actual compatible tool-round content uses V3. Separate Responses summary support does not imply it is used here. [Ollama compatibility contract](https://docs.ollama.com/api/openai-compatibility). |
| Anthropic | Existing OpenAI-compatible route does not return Claude thinking | Compatible streaming/function calling is documented | Ordinary tool-round text uses V3. Native summarized thinking requires the native Claude API; no Claude account/integration migration in this task. [Anthropic compatibility contract](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk). |

**Live public commentary**: Account has observed operator evidence. All eight compatible paths can now transport actual tool-associated public content using their implemented contracts; exact models and live responses are unverified. A provider may emit calls without prose. **Final-only content** is a per-response outcome, not a hardcoded provider classification. Every provider's no-call response stays final, including a retrospective describing earlier actions. No request asks a model to invent commentary, and no additional request exists solely for commentary.

## 5–8. Competing hypotheses and demonstrated roots

**A — output differences: supported.** Account exposes explicitly designated summaries. DeepSeek reasoning content, Groq raw/parsed reasoning and Mistral thinking chunks have different semantics. A silent call or final-only answer cannot be repaired into commentary client-side. Public content returned alongside a call is distinct from private reasoning and can be shown.

**B — client buffering: demonstrated and corrected.** `_drive_terminal_turn` deliberately calls `run` without provisional final sentence callbacks. `_run_turn` formerly used `on_sentence is not None` as its sole switch for `stream=True`. Non-Account calls therefore assembled a whole response before `_public_progress_from_tool_response` ran. The existing Account summary callback avoided this delay. Additionally, `_stream_deltas` yielded only `delta.content`, dropping `delta.tool_calls`; a streamed response could lose both native execution and the evidence needed to classify its content as commentary. Account's nonempty seeds returned without emission and smaller follow-up deltas waited for 160 more characters or completion.

**C — retrospective generation: possible, response-dependent; no live conclusion invented.** An ordered fixture puts a model-authored retrospective after tool execution with no new calls: it remains the final answer and produces no Thinking entry. A model-authored observation between tool rounds is commentary only in its actual later round; it is not backdated to the earlier action. Existing same-model final synthesis remains a final-answer mechanism. The audited active Thinking display contains no template reconstruction from receipts and no secondary summarizer. This investigation cannot prove which of these outputs the operator's DeepSeek session actually received without sanitized live event evidence.

**Additional roots:** Mistral typed public text could cause string concatenation/regex handling to fail, while generic stringification would expose thinking chunks; normalize public text types instead. Codex `agentMessage.phase` was ignored, sending explicitly designated commentary into reply assembly; now public commentary and final text are separate.

V2 already corrected canonical summary eviction, per-native-round overwrite, partial Desktop/Mobile snapshot replacement and Desktop-to-Mobile forwarding. Its regression tests continue to pass; these are not newly claimed V3 defects.

## Exact pipeline and temporal evidence

| Boundary | Real events/category/provenance | Buffering, order and rejection |
| --- | --- | --- |
| Selected provider request | Same selected provider/model; existing reasoning kwargs; `stream=True` for a bound non-Account Thinking trace | Independent of final-answer callback presence. No token/turn/round ceiling, extra summarizer or provider switch introduced. |
| Compatible iteration | `choices[0].delta.content`; indexed function name/ID/argument fragments in `delta.tool_calls`; finish reason | Ignore private reasoning fields. Assemble native calls by index without dropping arguments or adding them to display text. Provider/proxy buffering remains outside OSIA. |
| Public normalization | String content, or Mistral `type: text` entries; native call context or canonically parsed textual call | Ambiguous ordinary text is held until a call proves intermediate semantics. Textual calls parse before stripping. Incomplete markup/JSON tails wait for a valid boundary; completed syntax is removed and later public prose also flows live. No-call content never enters Thinking. |
| Canonical agent | `_stream_deltas` → `PublicCommentaryStream` → `_emit_public_model_narration`; Account uses its public callback | Exact execution-generation checks remain. A unique response-round source records monotonic wording extensions as revisions; separate rounds append. End-of-response uses the same source, avoiding duplicate full paragraphs. |
| Trace and persistence | Existing `kind: public_reasoning_summary`, `display: execution.reasoning.summary`, event ID, sequence, revision, UTC receipt timestamp | Sanitizers reject credentials, internal protocol and trivial status labels. Canonical full public wording retained by V2 compaction rules. Sequence represents canonical receipt order; timestamps are not manufactured provider generation times. |
| Desktop projection | Existing callback → Qt signal → `MainPopup.update_chat_execution_activity` → `_ChatThinkingTrace` | Exact-turn merge protects richer snapshots. Only public summaries render. Empty active header opens immediately; manual collapse survives updates; canonical completion collapses once; stored wording reopens. |
| PC transport | Existing direct/full trace projection and Desktop localhost forwarder → accepted/trace/terminal WS events | Serial correlated delivery; terminal status follows trace. Existing LAN/WebRTC/Relay event contracts unchanged. Relay retains its intentional reduced frame/byte budgets. |
| Mobile projection | Execution trace envelope → exact-key accumulator → transient screen snapshot → `ExecutionTraceBlock` | No provider classifier/generator. Revisions merge, identical replay deduplicates, exact identities isolate turns. Partial final snapshot retains prior text. Existing flat 13px text, immediate/manual disclosure and canonical collapse preserved. |

Fixture timeline: first provider delta with ordinary prose → no commentary yet; first native call fragment → public commentary emitted **before** generator resumes/end-of-response; completed arguments → same parsed call; response end → actual tool dispatch/receipt; second commentary round → second tool; no-call final retrospective → final response; canonical terminal → collapse. The canonical fixture uses exactly two provider calls for one tool plus one final, with a callback absent as in `_drive_terminal_turn`. SDK HTTP fixtures use actual OpenAI SDK SSE parsing and documented DeepSeek/OpenAI-compatible fields. Account tests use documented item phases and summary notifications. These are simulated events, not captured live provider sessions.

Optional diagnostics: logger `osia.public_commentary` at DEBUG emits provider/model, canonical turn, response-round ID, monotonically ordered receipt boundary, monotonic elapsed milliseconds, public text length, tool-context flag, commentary-presence flag and pending/final/tool-round/empty-or-filtered classification. It never logs prose, reasoning, tool arguments, prompts or keys. `response_complete` is distinct from a commentary emission and from canonical completion. Existing trace terminal sequence and transport correlation provide the remaining ordering. A final-only model response and a provider-buffered chunk cannot be mislabeled as earlier live thinking.

Diagnostic outcomes remain separate: **A** absent/unrequested public commentary; **B** native deltas/type normalization previously lost in adapter; **C** canonical non-stream request previously delayed forwarding; **D/E/F** trace/Desktop/Mobile retention covered by passing V2 and V3 tests, with no new defect found; **G** a final retrospective stays final. No statement that DeepSeek itself is defective.

## 9–11. Changes and call sites

Desktop V3 runtime:

- `osia/public_commentary.py` (new): public content normalization, native delta assembly, per-round semantic boundary, sanitized temporal metadata.
- `osia/agent.py`: `_run_turn` streams for a bound compatible Thinking trace even without final callbacks; `_stream_deltas` normalizes/observes and preserves native calls; the outer universal parser still dispatches them. `_emit_public_model_narration` accepts a round source; `_emit_provider_public_reasoning_summary` accepts the optional validated native public item identity. `_live_public_progress_narration` handles complete/fragmented markup safely. Existing callers and callback setter interfaces remain compatible.
- `osia/chatgpt_account/app_server_client.py`: `_capture_public_reasoning_summary`/new publisher forward real seeds/deltas promptly; `_append_agent_text` and item notifications honor explicit public message phases. `_AccountCompletions` and `_AccountChatClient` already carry the callback payload; no changes needed there or in authentication.

Desktop tests: new `tests/test_wor340_provider_timelines.py`; one assertion/name/comment correction in `tests/test_wor247_chat_streaming.py`. The latter expected a fresh repeated safe read to be vetoed, directly contradicting completed WOR-303/WOR-305 and AGENTS.md. It now expects both model requests and each existing immediate technical retry; runtime dispatch logic did not change to satisfy that test.

Mobile V3: only `test/wor340_thinking_test.dart` gains nine provider-independent projection fixtures. No concrete Mobile runtime defect was identified, so its source files are unchanged from V2. Existing V2 test/source edits remain intact. New report: `docs/WOR-340-thinking-v3.md`; the V2 report remains historical.

## 12. Validation

- Final full focused Python run: **353 passed**, 18 existing FastAPI `on_event` deprecation warnings, 23.02 seconds. Includes V3 timelines, V2 Thinking/Qt, public progress, Account summary/native compatibility, provider parser/lifecycle, Groq, DeepSeek catalog/SDK fixtures, canonical streaming, PC↔Mobile parity, recovery, cancellation, durable ActionTruth/transport, startup and reasoning controls.
- A subsequent seed-whitespace preservation refinement was checked by **33 passing** directly affected timeline/Account summary/Account runtime tests. Changed Python files compile; all three touched runtime modules import.
- Flutter: **74 passed** in the nine focused Thinking/public progress/transport/recovery/reconnect suites; **flutter analyze --no-pub: no issues**. Dart formatting and both repositories' diff whitespace checks pass.
- Intermediate failures resolved: the stale Python anti-replay assertion above; two mistyped Mobile suite names corrected to actual existing paths; new Mobile test constructor/method references corrected. No outstanding test failure. No new runtime gate was added.
- Credential-signature scan of V3 candidate source/test/report files performed before delivery; no values printed. Nothing staged.

## 13–14. Limits and preservation

A client cannot make a provider emit public text it did not produce, recover wording previously discarded before V2, or prove server-side generation time from a buffered receipt. Unknown/absent message phases retain legacy final semantics. Google summary opt-in/output mapping and separate native/Responses summary APIs are not claimed to work through this Chat Completions adapter. Raw thinking is never substituted. Relay compact budgets can omit long entries from its wire projection; they do not erase canonical history or previously received client text. Live provider and real-device delivery/visual checks remain for the operator.

Chat dispatch, selected model, final answer ownership, key rotation, exact generation cancellation/stale rejection, conversation persistence and canonical lifecycle remain functional in focused tests. V2 immediate expansion/flat styling/manual collapse/completion reopening/crash fixes are retained. No installer, macOS port, browser launch/profile/window handling, account authentication, Claude account integration, TTS or platform behavior changed in V3.

## 15–16. Git and Linear

Desktop `main`, HEAD `509e17c80156fc366eaddad31b1205aece98b8e6`; Mobile `main`, HEAD `8c9a974386afbae3c8eaeac4dfed4139a74a54a3`. Both already contained dirty/untracked work. New V3 files are untracked; other V3 changes are unstaged. Nothing committed, pushed, published or installer-built. Other dirty paths remain intentionally uncommitted as found, including Desktop `.claude/settings.local.json` deletion/TTS/unrelated benchmarks and experiments, and Mobile README/V2 source/test work. No reset, clean or stash.

WOR-340 remains **In Progress pending operator testing**. Findings, limitations, changed paths and results are recorded in its V3 comment. The complete dirty-path inventories are appended below for review; the V3 scope is explicitly listed above.

## 17. Operator verification

1. Run source builds containing these uncommitted changes. For **ChatGPT Account / GPT-6 Luna**, send a safe tool task requiring two observations. Thinking should open immediately; real summaries or explicitly phased commentary should appear while the model is active. Manually collapse before the next update; it must stay closed. At canonical completion reopen the retained words; final answer paragraphs should remain in the reply.
2. Repeat with **DeepSeek / deepseek-flash**. Use a safe folder listing followed by reading a harmless fixture. If public `content` accompanies a call, it should appear once context is established and before response completion when the provider streams it. If only reasoning content or silent calls arrive, an empty active indicator is accurate. Do not expect private CoT. The final retrospective must remain in the reply.
3. Repeat with an **already configured OpenAI API or Groq model**, keeping provider/model selection fixed. Check native/textual calls execute, genuine prose arrives at its real boundary, and no generic Activity sentence appears. Do not create credentials to run this test. For a configured Mistral reasoning model also verify typed thinking is hidden and its text answer remains readable.
4. Send a greeting/final-only question on each: Thinking opens while active, then an empty completed panel disappears; the answer is not duplicated into Thinking.
5. Mirror each on Mobile over the existing LAN/WebRTC route; reconnect mid-turn and switch conversations. Received wording and order should match PC, replay should not duplicate it, stale turns should stay isolated, canonical completion should collapse, and manual reopening should work. Repeat over Relay within its compact-budget limits.
6. If commentary is absent, enable only the metadata logger described above and compare receipt/emission/completion order. Distinguish no public source, delayed provider receipt, valid public text emitted by OSIA, and final-only retrospective. Do not send raw hidden reasoning, credentials or private prompts in diagnostic evidence.

OSIA THINKING V3: MULTI-PROVIDER INVESTIGATION COMPLETE — VERIFIED FIXES IMPLEMENTED — AWAITING OPERATOR TEST


### Desktop final working tree inventory

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


### Mobile final working tree inventory

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
