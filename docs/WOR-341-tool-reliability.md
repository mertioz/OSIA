# Natural Thinking commentary and canonical tool reliability — 11 October 2026

Implemented locally; WOR-341 remains In Progress pending operator validation.

## Part A — Thinking

The operator approved V5 before this task. The incident's saved public trace contains literal bold headings in `public_reasoning_summary` events from ChatGPT Account / `gpt-6-luna`. The characters were already present in provider-authored summary text; neither a local heading template nor Markdown rendering inserted them. The previous canonical Public progress updates paragraph requested concise factual updates but did not specify first-person sentences or exclude headings. The provider's native summary style is distinct from ordinary public commentary.

The sole style change adds guidance to that existing canonical paragraph: natural first-person sentences in the user's language, professional conversational tone, no artificial headings/bold titles/gerund-only labels or unnecessary lists, and no completion claims before execution evidence. No example sentence is hardcoded. There is no text rewrite, Markdown stripping, second AI, hidden-reasoning request, additional commentary call, or new subsystem. Legitimately supplied summaries retain their exact wording and provenance. This prompt guidance cannot guarantee that a provider changes the formatting of its native reasoning summaries.

All V5 UI, animation, disclosure, live text visibility, manual interaction, final collapse, transport, and Mobile source remain byte-identical to the task baseline. Hash comparison covered 244 product files: only the three product files listed below changed; 241 remained unchanged.

## Part B — recovered incident and first proven failure

The saved conversation dated 11 October, approximately 14:36–14:37 Europe/Paris, matches the operator's Steam + makg.pro request and the exact invalid-arguments final. Its route is `chatgpt_account:gpt-6-luna`. Evidence was read locally from conversation storage, correlated ToolAttempt records, the relaunch log, and the isolated App Server's SQLite log in read-only mode. Commands, frame/window contents, private arguments and credentials are omitted from this report.

1. The first rejected tool was **run_command**. Recovered shape: `{command: string, timeout: integer}`. The timeout was **above 300 seconds**. No command text or exact rejected value is reproduced.
2. `validate_tool_arguments` → `validate_arguments` → `Draft202012Validator` rejected `$.timeout`. Its canonical schema is nullable number with minimum 0.1 and maximum 300.0. The saved safe feedback only said `anyOf`, obscuring the actual bound.
3. The correction proposal changed tools to **computer_keypress**. The old recovery gate required exactly one call with the original tool name. It produced the terminal message before validating or dispatching the new proposal. The correction did not prove that computer_keypress arguments were invalid. Its complete argument shape is not recoverable from the retained sanitized records; no inferred key/frame values are reported.
4. Correlated receipts show two **inspect_desktop_surfaces** calls and one **computer_screenshot** completed. Those are read-only observations, not proof of Steam launch or URL navigation. There is no correlated open_application, open_uri, shell dispatch, navigate or navigate_browser receipt for this incident.
5. The rejected run_command had no effect. Ending the objective because the model selected another tool was premature: technically available app launch and browser tools remained. The legacy single-correction ceiling was shared across providers.
6. The persisted incident also labels the generated error final `success_verified`. That label does not establish the requested outcome. The automatic invalid-arguments terminal path is removed, and pre-dispatch rejections now enter the canonical typed evidence ledger rather than returning an invented terminal completion. The focused ledger harness proves rejection facts and later valid continuation; it does not retroactively change this stored conversation.

## Pipeline and corrections

The selected model receives `get_system_prompt`, generated capability declarations, and the current language contract. Textual/native calls enter the existing provider parsers before markup sanitation. Parsed arguments reach the authoritative ToolSpec validator, then the existing OSIA dispatcher, typed execution record, ToolAttempt/ActionTruth and same-model result reinjection. No second planner or routing authority was added.

- **Prompt contract loss:** the old static prompt retained required primitive signatures and up to three optional field names. It omitted bounds, nested schemas, other optional fields and alternative source contracts. The new prompt includes all available canonical input schemas with lossless shared JSON Schema `$defs` references. Tests expand each reference and prove exact equality to the original schema, validate the bundled schemas, and check that the prompt fits every supported provider's default context budget. Native provider declarations retain their independent exact schemas. The initial uncompressed implementation exceeded Ollama's budget and was corrected before delivery; no context-limit override was added. Final assembled English prompt measured 49,292 bytes / 24,655 estimated tokens in this workspace, excluding dynamic connector additions and subsequent conversation content. Context cost is higher than the previous lossy signatures; live provider behavior remains an operator check.
- **Unhelpful nullable errors:** matching branch constraints now report a safe `maximum`/`minimum` diagnostic, including the schema's bound, without the rejected value or exception message. Schema validation remains strict; no timeout clamp or argument coercion was added.
- **Recovery ceiling and same-tool lock:** each rejected proposal is returned as a factual recoverable result with its exact schema. The same selected model may issue another valid call, choose an alternative tool, or supply a truthful final assessed through the normal completion path. Successful or uncertain actions are never automatically replayed by recovery. Provider identity and exact-turn integrity checks remain.
- **Unsupported native proposals:** rejection becomes actionable same-model feedback with the available canonical surface, without the legacy one-reformulation objective ceiling. Native Codex effects remain intercepted; no direct native PC execution is enabled.
- **App native declarations:** added existing `open_application`, `open_uri`, `is_installed`, `is_running`, and `has_window` to the Account native declaration/handoff list. These were already textual OSIA capabilities; no Steam-specific mapping or new tool was created. The canonical contracts retain `name` for application launch, `app` for status checks and `uri` for deep links.
- **Mistral omission:** added the actually integrated Mistral provider to shared static/dynamic provider metadata. The nine supported providers now receive the same runtime-available canonical capability identities.
- **Direct dynamic validation:** Account read-only dynamic call failures now return sanitized field/type diagnostics, the current input schema and same-turn recoverability.
- **Unknown is not timeout:** final diagnostic metadata uses actual typed timeout evidence; a returned unknown effect is no longer automatically labelled `tool_timeout`. ActionTruth remains unknown, and distinct new model requests remain legal.

## App launch and website findings

`open_application(name)` already implements generic Windows resolution through App Paths, Start Menu/common install locations and packaged-app routes, with existing-window activation and process/window observation. `is_installed(app)`, `is_running(app)`, `has_window(app)`, structured desktop inspection and Computer Use V4 provide complementary observations. `run_command(command, timeout?)` remains a technically available alternative selected by the model. No application-name aliases or wrappers were silently invented.

`navigate_browser(url, state_ref?, wait_spec?)` uses the existing managed browser dispatcher and typed navigation receipt. `navigate(url)` remains its established legacy surface; their return contracts differ and they are not silently conflated. `makg.pro` normalizes to `https://makg.pro` in the existing browser implementation, and that explicit HTTPS URL validates against the canonical schema. `open_uri(uri)` is the existing OS URI route and reports fired-but-unverified outcomes where appropriate. Browser launch/profile isolation/single-window code was not changed.

The launcher harness exercises the real Windows launch function with mocked resolution, spawning and exact fixture-window activation. The navigation harness exercises the real managed navigation operation with an in-memory page whose URL changes and a real typed commit receipt. The multi-step harness exercises invalid shell rejection → alternative application dispatch → navigation dispatch → reinjection → one final answer across nine provider fixtures. None of these launches real Steam or visits makg.pro on the operator's desktop.

## Registry audit and capability evidence

Current static registry: **104 unique entries**, every implementation present and bound, every advertised property accepted by its dispatcher signature, every required dispatcher argument declared, and every original JSON Schema valid. **93** are available in the isolated audit process; the other **11** are dormant or require a bound V4/vision turn. The Account native surface has 16 declarations in that environment; other available capabilities remain callable through the complete textual surface. Full exact schemas are supplied for all 93 available textual tools.

No unimplemented static entry, orphan dispatch route or signature mismatch was found. `bash`/`run_command` are equivalent shell entry points; `navigate`/`navigate_browser` preserve distinct legacy/typed return surfaces. Dormant V3 tools remain registered for compatibility and unavailable to models. No stale alias was resurrected.

No dynamic MCP tools were registered in the isolated audit process. The existing MCP registration/strict-validation/dispatch/unregistration fixture passed. Actual running-process connector inventories and live connector effects are **NOT_TESTED**; configured credentials alone are not claimed as an active connection. No disconnected connector is added to the table.

Status meanings: VERIFIED_REAL is limited to observed physical read-only incident receipts; VERIFIED_HARNESS requires controlled execution/continuation evidence; CONTRACT_ONLY proves schema/signature integrity but not actual backend effects; UNAVAILABLE is scoped to this audit environment; NOT_TESTED has no execution claim; BROKEN would require a confirmed remaining implementation failure. The corrected shared contract/recovery defects are recorded above, without declaring every tool functional. The full per-tool table follows below.

## Validation and limits

- Final focused run: **104 passed**, 18 existing FastAPI deprecation warnings. Files: test_tool_reliability_regression, test_wor227_invalid_arguments, test_wor200_native_action_bridge, test_wor178_tool_registry, test_wor178_tool_safety, test_wor262_windows_app_resolution, test_wor269_cancellation_thinking.
- Adjacent run: **187 passed**, 18 existing warnings. It covers provider lifecycle/native/textual/DeepSeek/Groq formats, genuine public summaries, V5 Thinking, model authority, cancellation/supersession, system destruction guards, and controlled browser fixtures.
- An additional full canonical driver harness passes: pre-dispatch rejection → valid alternate read → one dispatch → one canonical final. This supplements the 104-test focused run. Windows process truth checks are recorded in the final verification note below.
- Python compilation/imports passed for every changed Python source and test. Desktop and Mobile whitespace checks pass. Candidate credential scan and final Git inventory are recorded below.
- Additional older controlled/document/smoke/streaming run: 52 passed, four document tests require semantic duplicate caching removed by WOR-305, and the cancellation test initially found the uncompressed prompt budget problem. The prompt problem is corrected and all eight cancellation tests pass in the final focused run. Document caching expectations were left untouched; no deleted caching/replay veto was restored.
- An additional broader canonical completion-fixture run and diagnostic driver experiments were interrupted after they stopped advancing. No passing result is claimed for those interrupted runs. The corrected focused full-driver and typed-rejection/continuation harnesses pass. The interrupted broad fixture cases were not used to justify a production turn limit or an unrelated runtime redesign. An additional Windows process suite's final same-turn provider fixture also stopped advancing and was excluded from the final process-only run; its outcome remains unverified.
- Stale tests changed only where they contradicted completed architecture: rejection is recoverable; unknown outcomes allow same-model continuation without automatic replay; semantic anti-replay fields are absent. The Account completion fixture now supports the streaming interface used by the current canonical runtime.
- No intentional live provider campaign, physical Steam launch, makg.pro navigation, operator browser control, installer build, Mobile publication, deployment, push, release, macOS work, staging or commit. One diagnostic driver fixture accidentally attempted the default SDK connection because only the streaming method was stubbed; the test was stopped at the transport connection boundary and replaced with an explicit completion client stub. No successful live provider turn or physical action was observed from that experiment. Provider-native generation quality and the actual installed application's Steam/website workflow remain **NOT_TESTED** by this task.

## Exact task file scope and call sites

Product:

1. `osia/agent.py`: SYSTEM_PROMPT_CORE/get_system_prompt callers; canonical `_run_turn` validation, result reinjection and native-rejection continuation; typed diagnostic finalization. Parser formats and dispatch implementations unchanged.
2. `osia/tool_contracts.py`: ALL_TEXT_PROVIDERS consumed by static/dynamic metadata and capability federation; `_validation_issue` used by both agent and direct validators; static_tool_prompt_block used by canonical prompt assembly; new private shared-schema builder. Native schema dictionaries and validation constraints unchanged.
3. `osia/chatgpt_account/app_server_client.py`: `_CANONICAL_NATIVE_TOOL_NAMES` consumed by declaration assembly and exact request mapping; `_resolve_dynamic_tool_call` error responses. Native actions still hand off to OSIA.

Tests:

4. `tests/test_tool_reliability_regression.py` (new).
5. `tests/test_wor227_invalid_arguments.py`.
6. `tests/test_wor178_tool_safety.py`.
7. `tests/test_wor200_native_action_bridge.py`.
8. `tests/test_wor186_browser.py`.

Report: `docs/WOR-341-tool-reliability.md` (new). No Mobile file changed in this task. Existing agent/bridge edits from V3 and all other prior dirty/untracked work were preserved. The temporarily investigated completion-test helper was restored to its original contents and has no task diff.

## Linear and operator checks

WOR-340 received only the prompt-style refinement and V5 approval record; it remains In Progress for that refinement's operator check. New WOR-341 is In Progress, related to WOR-178, WOR-200, WOR-201 and WOR-305. Historical permission/Autonomous/replay prescriptions in those old tickets are superseded by completed WOR-305 and AGENTS.md.

1. Run the source/build containing these edits, select ChatGPT Account / GPT-6 Luna, and send the original French request.
2. Check natural public commentary where the model supports it, without expecting OSIA to rewrite a supplied native summary. Confirm V5 expansion, interaction, animation, live updates and completion collapse still behave as approved.
3. Verify Steam actually launches or an existing exact window is activated, then verify the browser reaches exactly `https://makg.pro`. Check real receipts and one final response; preserve any earlier success if a later step fails.
4. If a pre-dispatch rejection occurs, check that feedback names the offending field/constraint and the same model can choose a new valid call or available alternative. Do not automatically replay successful/unknown physical effects.
5. Compare the same request with already configured DeepSeek (`deepseek-flash`) and another configured compatible provider, including Mistral if available. No new credentials or provider restrictions are required by this change.
6. Record any missing executable/browser dependency or runtime failure separately from a schema rejection. Keep WOR-341 In Progress until this physical check is reviewed.

App Server response/handoff behavior was checked against local source and the [official OpenAI dynamic tool call documentation](https://learn.chatgpt.com/docs/app-server#dynamic-tool-calls-experimental), accessed through the OpenAI Docs skill. That documentation establishes the protocol flow, not OSIA's physical execution success.

## Per-tool static audit matrix

Each row passed schema and signature checks; statuses describe the evidence boundary, not a promise of end-to-end effect. A schema hash prefix identifies the original independent ToolSpec schema. `available` reflects the isolated audit scope, not every future turn.

| Tool | Status | Available in audit | Dispatcher | Schema hash |
|---|---|---|---|---|
| declare_outcomes | CONTRACT_ONLY | yes | tool_declare_outcomes | 418d29c9ff09 |
| save_memory | CONTRACT_ONLY | yes | tool_save_memory | ccc6b0d2c720 |
| create_scheduled_task | CONTRACT_ONLY | yes | tool_create_scheduled_task | f0432e39c10f |
| list_scheduled_tasks | CONTRACT_ONLY | yes | tool_list_scheduled_tasks | aaa2353181fd |
| update_scheduled_task | CONTRACT_ONLY | yes | tool_update_scheduled_task | e376d45b6cb5 |
| delete_scheduled_task | CONTRACT_ONLY | yes | tool_delete_scheduled_task | 4501ba3f4309 |
| bash | CONTRACT_ONLY | yes | tool_bash | 0c8161dd26d4 |
| run_command | VERIFIED_HARNESS | yes | tool_run_command | 0c8161dd26d4 |
| open_application | VERIFIED_HARNESS | yes | tool_open_application | e1723bc0a03a |
| open_uri | CONTRACT_ONLY | yes | tool_open_uri | 71b846c101ba |
| take_screenshot | CONTRACT_ONLY | yes | tool_take_screenshot | aaa2353181fd |
| inspect_desktop_surfaces | VERIFIED_REAL | yes | tool_inspect_desktop_surfaces | 281c99f92d11 |
| request_visual_observation | UNAVAILABLE | no | tool_request_visual_observation | 0d8d16c652ee |
| read_file | CONTRACT_ONLY | yes | tool_read_file | b46d52eb71d3 |
| read_document | CONTRACT_ONLY | yes | tool_read_document | b6a181eb7ea3 |
| read_document_artifact | CONTRACT_ONLY | yes | tool_read_document_artifact | a99da43d4203 |
| query_document | CONTRACT_ONLY | yes | tool_query_document | c56311d8abe2 |
| write_file | CONTRACT_ONLY | yes | tool_write_file | 67718d8d7869 |
| render_pdf | CONTRACT_ONLY | yes | tool_render_pdf | 539ab3bd34cc |
| list_files | CONTRACT_ONLY | yes | tool_list_files | b46d52eb71d3 |
| delete_file | CONTRACT_ONLY | yes | tool_delete_file | b46d52eb71d3 |
| create_directory | CONTRACT_ONLY | yes | tool_create_directory | b46d52eb71d3 |
| delete_directory | CONTRACT_ONLY | yes | tool_delete_directory | d190e90b165b |
| rename_file | CONTRACT_ONLY | yes | tool_rename_file | b41671ba7ad2 |
| copy_file | CONTRACT_ONLY | yes | tool_copy_file | 8df9580432b9 |
| get_file_info | CONTRACT_ONLY | yes | tool_get_file_info | b46d52eb71d3 |
| search_files | CONTRACT_ONLY | yes | tool_search_files | d09d6c3c9090 |
| open_in_editor | CONTRACT_ONLY | yes | tool_open_in_editor | b46d52eb71d3 |
| edit_file_lines | CONTRACT_ONLY | yes | tool_edit_file_lines | 511427c75aad |
| get_system_info | CONTRACT_ONLY | yes | tool_get_system_info | aaa2353181fd |
| list_running_processes | CONTRACT_ONLY | yes | tool_list_running_processes | c330c8d6a576 |
| kill_process | CONTRACT_ONLY | yes | tool_kill_process | 2f69577d5676 |
| set_volume | CONTRACT_ONLY | yes | tool_set_volume | 3d52e2fa46c8 |
| get_clipboard | CONTRACT_ONLY | yes | tool_get_clipboard | aaa2353181fd |
| set_clipboard | CONTRACT_ONLY | yes | tool_set_clipboard | c65aa3599e1a |
| navigate | CONTRACT_ONLY | yes | tool_navigate | 2fd128251b7f |
| click | CONTRACT_ONLY | yes | tool_click | b7d6ec0e439f |
| fill | CONTRACT_ONLY | yes | tool_fill | 8aa1d057540b |
| inspect_form_fields | CONTRACT_ONLY | yes | tool_inspect_form_fields | 9abd07fb028c |
| fill_form_field | CONTRACT_ONLY | yes | tool_fill_form_field | 0c584be4fc8e |
| read_page | CONTRACT_ONLY | yes | tool_read_page | aaa2353181fd |
| get_current_url | CONTRACT_ONLY | yes | tool_get_current_url | aaa2353181fd |
| scroll | CONTRACT_ONLY | yes | tool_scroll | 13ed7bfe2a2d |
| go_back | CONTRACT_ONLY | yes | tool_go_back | aaa2353181fd |
| go_forward | CONTRACT_ONLY | yes | tool_go_forward | aaa2353181fd |
| open_new_tab | CONTRACT_ONLY | yes | tool_open_new_tab | 2fd128251b7f |
| find_and_click | CONTRACT_ONLY | yes | tool_find_and_click | 74fd1a54bfa8 |
| wait_for_load | CONTRACT_ONLY | yes | tool_wait_for_load | aaa2353181fd |
| get_page_text | CONTRACT_ONLY | yes | tool_get_page_text | aaa2353181fd |
| scroll_down | CONTRACT_ONLY | yes | tool_scroll_down | c3e3098165c9 |
| type_text | CONTRACT_ONLY | yes | tool_type_text | 7903e3e47930 |
| press_key | CONTRACT_ONLY | yes | tool_press_key | 6d54cdf88937 |
| open_browser | CONTRACT_ONLY | yes | tool_open_browser | daba4b549f0e |
| desktop_click | UNAVAILABLE | no | tool_desktop_click | fefc799927db |
| activate_desktop_surface | CONTRACT_ONLY | yes | tool_activate_desktop_surface | e2f41f78b0ff |
| desktop_type | CONTRACT_ONLY | yes | tool_desktop_type | e53e512ce6e7 |
| desktop_press | CONTRACT_ONLY | yes | tool_desktop_press | 6d54cdf88937 |
| desktop_scroll | CONTRACT_ONLY | yes | tool_desktop_scroll | 75f51f95908e |
| computer_screenshot | VERIFIED_REAL | no | tool_computer_screenshot | f9ad2d2edd38 |
| computer_zoom | UNAVAILABLE | no | tool_computer_zoom | 3988f423046e |
| computer_observation | UNAVAILABLE | no | tool_computer_observation | 1b7b54b5e2dc |
| computer_click | UNAVAILABLE | no | tool_computer_click | 7c0a8c0bc66e |
| computer_double_click | UNAVAILABLE | no | tool_computer_double_click | 7c0a8c0bc66e |
| computer_type | UNAVAILABLE | no | tool_computer_type | 375c811bb941 |
| computer_keypress | NOT_TESTED | no | tool_computer_keypress | e09b15bae4bf |
| computer_scroll | UNAVAILABLE | no | tool_computer_scroll | a9bd95449412 |
| computer_actions | UNAVAILABLE | no | tool_computer_actions | 775d2775f023 |
| is_running | CONTRACT_ONLY | yes | tool_is_running | c7896c879e93 |
| is_installed | CONTRACT_ONLY | yes | tool_is_installed | c7896c879e93 |
| has_window | CONTRACT_ONLY | yes | tool_has_window | c7896c879e93 |
| steam_game_state | CONTRACT_ONLY | yes | tool_steam_game_state | 31ceeb0e3880 |
| web_state | CONTRACT_ONLY | yes | tool_web_state | ce7ad25b8e22 |
| get_browser_state | CONTRACT_ONLY | yes | tool_get_browser_state | aaa2353181fd |
| navigate_browser | VERIFIED_HARNESS | yes | tool_navigate_browser | 0c022ef0ea4c |
| inspect_browser_page | CONTRACT_ONLY | yes | tool_inspect_browser_page | 10242f950138 |
| wait_for_browser_condition | CONTRACT_ONLY | yes | tool_wait_for_browser_condition | d371f7b64356 |
| activate_browser_element | CONTRACT_ONLY | yes | tool_activate_browser_element | 0ec25376bc38 |
| list_browser_tabs | CONTRACT_ONLY | yes | tool_list_browser_tabs | e1092371d8b2 |
| select_browser_tab | CONTRACT_ONLY | yes | tool_select_browser_tab | a58cef008fd9 |
| close_browser_tab | CONTRACT_ONLY | yes | tool_close_browser_tab | a58cef008fd9 |
| reload_browser_page | CONTRACT_ONLY | yes | tool_reload_browser_page | a58cef008fd9 |
| browser_back | CONTRACT_ONLY | yes | tool_browser_back | a58cef008fd9 |
| browser_forward | CONTRACT_ONLY | yes | tool_browser_forward | a58cef008fd9 |
| open_new_browser_tab | CONTRACT_ONLY | yes | tool_open_new_browser_tab | d4246bff3182 |
| return_to_browser_opener | CONTRACT_ONLY | yes | tool_return_to_browser_opener | a58cef008fd9 |
| search_web | CONTRACT_ONLY | yes | tool_search_web | 181927f7b3de |
| open_search_result | CONTRACT_ONLY | yes | tool_open_search_result | 3c7b8bd668f6 |
| paginate_browser | CONTRACT_ONLY | yes | tool_paginate_browser | deef30316294 |
| scroll_infinite | CONTRACT_ONLY | yes | tool_scroll_infinite | 0f1b1b33d818 |
| list_browser_dialogs | CONTRACT_ONLY | yes | tool_list_browser_dialogs | a58cef008fd9 |
| handle_browser_dialog | CONTRACT_ONLY | yes | tool_handle_browser_dialog | ad709b22509f |
| upload_browser_file | CONTRACT_ONLY | yes | tool_upload_browser_file | 72d014ff0c2f |
| download_browser_file | CONTRACT_ONLY | yes | tool_download_browser_file | e4bd336d9594 |
| gmail_list_labels | CONTRACT_ONLY | yes | tool_gmail_list_labels | aaa2353181fd |
| gmail_list_latest | CONTRACT_ONLY | yes | tool_gmail_list_latest | 6d5aceade42a |
| gmail_search | CONTRACT_ONLY | yes | tool_gmail_search | 97e2cd3f7b02 |
| gmail_read_message | CONTRACT_ONLY | yes | tool_gmail_read_message | faa7fccda2b7 |
| gmail_read_thread | CONTRACT_ONLY | yes | tool_gmail_read_thread | 9b1588dbee41 |
| gmail_create_draft | CONTRACT_ONLY | yes | tool_gmail_create_draft | 5a208ac60f57 |
| gmail_send_draft | CONTRACT_ONLY | yes | tool_gmail_send_draft | 534f3752d5d7 |
| gmail_mark_read | CONTRACT_ONLY | yes | tool_gmail_mark_read | faa7fccda2b7 |
| gmail_mark_unread | CONTRACT_ONLY | yes | tool_gmail_mark_unread | faa7fccda2b7 |
| gmail_apply_label | CONTRACT_ONLY | yes | tool_gmail_apply_label | 2b0d512ffc79 |
| gmail_remove_label | CONTRACT_ONLY | yes | tool_gmail_remove_label | 2b0d512ffc79 |

## Final Git inventory

Both repositories remain on main, with no staging, commit or push. All paths outside the nine-file task scope above are prior work and deliberately untouched.

### Desktop

HEAD `509e17c80156fc366eaddad31b1205aece98b8e6`.

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
 M osia/tool_contracts.py
 M osia/voice/tts.py
 M tests/test_wor178_tool_safety.py
 M tests/test_wor186_browser.py
 M tests/test_wor200_native_action_bridge.py
 M tests/test_wor227_invalid_arguments.py
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
?? docs/WOR-341-tool-reliability.md
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
?? tests/test_tool_reliability_regression.py
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

HEAD `8c9a974386afbae3c8eaeac4dfed4139a74a54a3`.

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

## Final verification note

Windows process truth: 54 passed, one older same-turn provider fixture deselected after its interrupted diagnostic run. These checks execute harmless controlled child processes, preserve zero/nonzero exit codes, separate stdout/stderr, and verify typed ToolAttempt/ActionTruth process observations. Combined clean completed checks: 104 focused + 187 adjacent + 1 added canonical-driver case + 54 process cases = 346 passing checks. This total does not include failed or interrupted older runs.

Compilation and imports passed for all eight changed Python files; both repositories pass git diff --check. Nine candidate source/test/report files have zero credential-signature locations. Both indexes are empty. The final hash check still finds only agent.py, tool_contracts.py and chatgpt_account/app_server_client.py changed among 244 baselined product files. All Mobile source and V5 UI files remain unchanged in this task. Temporary audit snapshots, matrices and diagnostics are outside the repository; no generated benchmark evidence is staged. All listed paths remain uncommitted as requested.

OSIA TOOL RELIABILITY: ROOT CAUSES CORRECTED — AWAITING OPERATOR VALIDATION
