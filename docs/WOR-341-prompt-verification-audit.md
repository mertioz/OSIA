# Natural Thinking format and proportionate verification audit

11 October 2026. Source implementation only. The operator reports that Thinking V5 and WOR-341 tool recovery work. Keep WOR-340 and WOR-341 In Progress pending the new operator test.

## Findings and scope

The literal bold delimiters originate in genuinely public Account summaries. The canonical natural-commentary guidance reaches Account baseInstructions; OSIA does not insert these headings. Both clients previously displayed public text literally. A narrow Thinking-only presentation correction now removes the outer delimiters of a simple, complete, whole-line `**heading**`. It preserves the words, language, raw canonical text, event identity, provenance, transport and final answer. It does not translate headings or turn them into invented first-person claims.

The execution-efficiency hypothesis is only partly supported. Recorded Luna runs contain more summary text and six actual tool attempts, versus five in a recent DeepSeek run. Both providers repeat actions. No duplicate invocation of the same observation tool appears in these three runs. An unknown URI outcome in one Luna run gives a legitimate reason for additional evidence. Neither summary count nor a successful read receipt proves that the original physical objective succeeded.

Demonstrated prompt defects are conflicting legacy retry/reconciliation instructions and unnecessarily repeated completion prose in result feedback. They are corrected without changing tool execution. No measurement establishes a post-change speed improvement or a causal relationship between these old instructions and Luna's choices.

## Effective instructions, assembly and history

Inspected the complete current nine-provider prompt assembly, generated deterministic full snapshots outside Git, and tested actual Account `_prepare` and compatible wire assembly. Providers: Account, DeepSeek, OpenAI, Anthropic, Google, Groq, xAI, Mistral and Ollama. All use the same canonical English core; the current-turn language instruction is separate and last. This is current source/fixture evidence, not capture of every historical upstream request or a guarantee about a running installed build.

The assembly/call sites are:

1. `agent.SYSTEM_PROMPT_CORE` → `get_system_prompt` → `_system_prompt_for_current_turn` → `run`/`_wire_messages`. Core sections cover objective completion, optional public updates, capability inventory, textual tool format and factual protections.
2. `tool_contracts.replace_static_tool_prompt_section` → `static_tool_prompt_block`: full effective registry, compact capability descriptions, output/side-effect/precondition metadata and exact JSON Schemas with shared definitions. The isolated audit exposes 93 available static tools. Technically unavailable/turn-bound tools are not fabricated. Existing native declarations and DSML/XML parsers are unchanged.
3. Optional local personality and enabled skill blocks, then `_with_mcp_tool_block`; language preference remains authoritative. At audit time, the configured personality was 176 characters and contained no English verify/verification/inspect/heading/plan terms; enabled skills and connected-MCP blocks were empty in this process. Private text is not reproduced. This does not establish the live operator process's connector inventory or rule out every non-English synonym.
4. `chat_routing.execution_prompt`: “execute the next safe action with the available tools, observe its result, and then choose the next action”; “Do not generate a mandatory task list”. This is consumption of returned evidence, not a requirement for an independent observer after every tool. Scheduled occurrences use the same engine and their separate scope context.
5. Account `provider._prepare` sanitizes unneeded project/desktop paths and inserts `_CONTAINMENT_INSTRUCTIONS` before the language contract. Relevant exact addition: “A capability exposed as a native dynamic tool is invoked only through that native call, never duplicated as an XML/text tool call.” Developer instruction: “Act as OSIA's provider. Use any applicable hosted or OSIA capability and report only observed outcomes.” `app_server_client.start_or_resume_conversation` sends baseInstructions/developerInstructions/dynamicTools to thread/start. System/schema fingerprint changes rebuild the isolated thread; unchanged messages reuse it. No extra formatting request is added.
6. Compatible providers receive the canonical system message through `_wire_messages`, context construction and existing message adaptation. No separate provider verification recipe was found. Existing reasoning settings and provider-native tool formats are unchanged.
7. Canonical turns may add `outcome_contract.completion_instruction`: choose tools freely, declarations/envelopes optional, decide completion from results and observed state, failures are repair feedback, unknown is not success. This repeats some factual principles but does not command repeated checks; left unchanged. Learning/artifact context is conditional. Legacy strategy/unfinished-operation feedback exists for recovery; new canonical Chat turns have no strategy authority. No evidence establishes that the latest Steam scenarios activated those legacy paths.
8. Each result batch is reinjected by `agent.run` using `turn_context.localized_text('tools_complete_instruction', language)`. This was a second detailed restatement of all completion conditions. It now directs the model to use fresh existing evidence and choose further work only when needed. Current-schema rejection feedback and WOR-341 same-model recovery remain unchanged.
9. Capability-specific visual guidance still requires a fresh target-bound frame before visual actuation. Screenshot/action receipts distinguish pixels from an independent postcondition. These technical freshness guarantees are retained; they are not a global requirement to capture a screenshot after a direct launch or navigation receipt.

Reviewed Linear WOR-249, WOR-286, WOR-305, WOR-311, WOR-340 and WOR-341. WOR-249 supports one canonical core and avoiding duplicated instructions; its old Autonomous/permission/replay requirements are superseded by completed WOR-305. WOR-286 protects sufficient initial reasoning: no reasoning-effort reduction, artificial sleep, fast-start instruction or global objective limit was added. WOR-311 establishes legitimate model-authored public commentary. Approved V5 behavior is preserved.

## Before and after

Canonical objective paragraph previously contained:

> reassess the whole objective after each tool result or observation
>
> Preserve verified progress, never repeat a successful mutation, and reconcile an uncertain effect before retrying.

The first phrase duplicated result feedback. The second conflicts with WOR-305: unknown effects are facts, not a dispatch veto or mandatory reconciliation prerequisite.

The corrected paragraph includes:

> For a simple action, use an appropriate available tool and consume its factual result. Reuse fresh, authoritative receipts and observations that already establish the requested outcome; do not independently reconfirm the same unchanged postcondition without a concrete reason.
>
> Observe further when evidence is ambiguous, contradictory, stale, or insufficient for a required postcondition, or when the user requests additional verification.
>
> For complex work, reason as needed, iterate, and recover from genuine failures until the deliverable is usable and its relevant checks are satisfied. Unknown effects remain uncertain; the selected model decides whether to observe, retry, or choose another action from the facts.

The legacy protection bullet also prescribed “do it once and observe”, “Retry only with positive evidence of no effect” and reconciliation for uncertain effects. It now preserves the factual invariant:

> An unchanged screenshot is inconclusive: it proves neither failure nor success. Choose any further action from the requested objective and available evidence, preserving target freshness and cancellation. An uncertain effect is not proof of success or a prohibition on a fresh model-requested call.

Previous English result feedback started “The current tool batch finished. Reassess the full objective against the evidence” and restated every step/deliverable/constraint/final-state condition. Corrected feedback:

> Use these factual results and fresh existing evidence. Choose another tool if required work or evidence remains missing; otherwise give the truthful final answer. Report uncertainty or a real limitation without claiming unobserved success.

All six existing locales are updated together: en/fr/zh/ja/de/es. The same call site and actual results remain in place. English feedback shrinks from 389 to 239 characters. Isolated French system snapshots shrink by 132 characters for each provider. After sizes range from 48,600–48,612 characters for API adapters and 48,887 for Account baseInstructions; exact schemas remain intact. These are prompt-size measurements, not execution-speed measurements.

Public guidance already supplied by WOR-341 remains:

> Write natural first-person sentences in the user's language, conversationally and professionally; avoid headings, bold titles, gerund-only labels, and unnecessary lists.
>
> Keep it concise, do not narrate every tool action

There is no instruction to plan every trivial action publicly, inspect an unchanged environment repeatedly, independently verify every invocation, or narrate every verification. Adding more repetitions of the same style demand would not demonstrate control over Account summary wording.

## Public-summary source and presentation

Pipeline: Account `item/reasoning/summaryPartAdded` and `summaryTextDelta`, or explicitly public message phase `commentary` → correlated provider callback → canonical public_reasoning_summary event → persisted trace/PC protocol → Desktop `_ChatThinkingTrace` or Mobile `ExecutionTraceBlock`.

The recent recorded public summaries contain the operator-reported bold titles. No OSIA heading template produces them. OSIA's canonical guidance reaches the Account request in the fixture, so the observed wording is inconsistent with that guidance rather than proof that the guidance is absent. The upstream mechanism's internal authoring process is not exposed by the retained evidence.

[Official App Server documentation](https://learn.chatgpt.com/docs/app-server) separates readable summary events from raw reasoning, and documents summary verbosity settings. The inspected bridge/protocol provides no verified way to force arbitrary first-person wording, language or title-free summary formatting. Guidance is supported for ordinary public assistant commentary; consistent obedience by provider-authored reasoning summaries remains unverified. No hidden reasoning content is read or displayed.

Desktop `_thinking_display_text` and Mobile `formatThinkingPublicText` handle only complete simple whole-line bold wrappers. They preserve inline bold, links, code spans, HTML, escapes, nested Markdown, indented code and fenced blocks. A streamed unfinished wrapper remains literal until a complete supported line arrives. Words such as “Opening Steam” remain exactly those words; the presentation fallback does not manufacture “I opened Steam”. It never runs on canonical storage, sanitizer, transport, model reinjection or final answers.

The existing widgets/documents remain in place. Expansion, manual collapse, subsequent-turn expansion, animation/orbs, font, transparency, live updates, terminal collapse and reopen are unchanged. Mobile changes only because the same minimal presentation fallback is required on both clients.

## Recorded execution comparison

Read-only correlation uses stored conversations/public traces plus ToolAttempt/ActionTruth in the configured store. App Server logs are read-only and queried only for safe markers. No raw prompts, private reasoning, tool arguments or credentials are exported. The three recent operator records request Steam and a website; model tool availability/history and physical starting state were not controlled, so this is an observational comparison.

| Recorded run | Actual attempts | Observation tools | Same-tool/same-argument repeat candidates | Public entries / characters | First dispatch / first receipt | Last receipt / trace terminal |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Luna, 16:31–16:33 local | 6 | 2 | 1 | 6 / 466 | 6.479s / 6.980s | 82.046s / 86.793s |
| Luna, 16:24–16:25 local | 6 | 2 | 1 | 6 / 544 | 8.152s / 8.527s | 89.526s / 89.677s |
| DeepSeek Flash, 16:22–16:23 local | 5 | 1 | 1 | 1 / 76 | 4.453s / 5.424s | 55.093s / 57.311s |

First dispatch means the recorded ToolAttempt start, not an observed GUI effect. Last receipt and canonical terminal are not independent measurements of physical objective completion. Public entries can contain multiple headings/sections; they are not tool counts or token-update counts.

Luna first sequence: `open_application → navigate → open_application → open_browser → is_running → inspect_desktop_surfaces`. Both app calls have the same argument fingerprint. The first result's Account-log continuation includes the existing “ready and focused” marker. Later app reopening/focus work is a candidate for unnecessary repetition, but the target/focus changed during intervening browser work, so identical arguments alone do not prove unchanged-state redundancy.

Luna second sequence: `open_application → navigate → open_application → open_uri → is_running → inspect_desktop_surfaces`. The URI records outcome_unknown/effect_possible. Further observation can legitimately resolve uncertainty. There is no second call to either observation tool. These tools establish different facts: process presence and available surfaces. The ledger's success_verified on a read means the observation completed, not necessarily that its Boolean value was true or the complete user request succeeded.

DeepSeek sequence: `open_application → navigate → is_running → open_application → navigate`. The app arguments differ; the two navigation calls have the same fingerprint. The first navigation already has a success_verified receipt. This is a repeated-action candidate, not evidence that DeepSeek never repeats work. Again, intervening actions matter.

No pre-dispatch validation rejection is present in these three retained trace timelines; no schema-repair event is demonstrated. Physical error/recovery counts cannot be reconstructed perfectly from terminal attempt status alone. Luna's unknown URI is one uncertain action, not a proven failed physical action. No repeated invocation of the same observation tool is demonstrated. Public wording repeats verification more often than independent inspections occur. No canonical layer is observed demanding the exact repeated launch/navigation calls.

No comparable recorded third-provider physical run was found in the recent Steam/site records inspected. OpenAI, Account and DeepSeek equivalent scenarios use isolated deterministic fixtures with comparable tools. These fixtures test that the same model receives factual results, completes directly when adequate, observes legitimately when uncertain, recovers from a failed operation, and handles distinct multi-step postconditions. They do not measure live provider judgment, latency or success rates. No paid live calls or physical application/mouse/keyboard manipulation were performed in this task.

## Receipt audit

No receipt code is changed. `open_application` already distinguishes a focused existing window, ready/focused newly opened window, a running process without a confirmed ready window, process-spawn-only uncertainty and actual failure. Process creation is never upgraded to window-readiness proof. Some legacy launch strings are recorded conservatively as success_unverified because the registry does not assert verified success for every branch. The actual string is still reinjected; the conservative ledger label does not hide the observed window text from the model. Globally marking this tool verified would falsely certify its uncertain branches.

`open_uri` explicitly distinguishes handoff/attempt from confirmed effects. `navigate_browser` uses typed dispatch/outcome receipts with target/document identity, navigation-commit and before/after URL evidence; uncertain dispatch and local wait failures remain distinguishable. The recorded scenario uses legacy `navigate`, whose contract differs from the typed navigation tool. A committed navigation establishes navigation, not every possible requested page-content condition. Existing valid observations and failure recovery remain necessary when evidence does not establish the requested effect.

No demonstrated missing-result defect justifies redesigning launch/navigation receipts in this request. The confirmed weak feedback is duplicated prompt prose, which is corrected at its existing reinjection call site.

## Changed files and checks

Task changes:

| File | Change |
| --- | --- |
| Desktop `osia/agent.py` | Canonical objective/uncertainty prompt only |
| Desktop `osia/turn_context.py` | Six existing result-feedback translations |
| Desktop `osia/gui/main_popup.py` | Thinking-only presentation helper and its detail-widget call |
| Desktop `tests/test_prompt_verification_audit.py` | Prompt/Account delivery, presentation/raw preservation and controlled four-scenario comparisons |
| Desktop `tests/test_wor249_prompt_core.py` | Replace stale retry-veto assertions with current factual uncertainty/model-authority assertions |
| Mobile `lib/widgets/execution_trace_block.dart` | Matching Thinking-only presentation helper and display call |
| Mobile `test/prompt_verification_presentation_test.dart` | Formatting, raw snapshot/provenance and V5 lifecycle fixtures |
| Desktop `docs/WOR-341-prompt-verification-audit.md` | This report |

Final completed Python selection: **154 passed**, 18 existing FastAPI warnings, covering new audit, WOR-341 reliability, WOR-227 invalid arguments, WOR-200 native bridge, WOR-340 V5 UI/storage, WOR-311 public progress and WOR-269 cancellation. Additional focused prompt/model-authority/provider-summary/outcome-contract selection: **25 passed, 1 deselected**. Total **179 passing Python checks**.

Mobile focused selection: **33 passed** across new presentation, WOR-340 Thinking/V5, WOR-311 public progress and WOR-254 execution trace. Targeted Flutter analysis: no issues. Dart formatting changes only the new helper/test. All five changed Python source/test files compile and import. Both repo whitespace checks pass, with existing line-ending-normalization warnings.

Failed/interrupted checks are not counted as successes: the initial new full-driver fixture stopped advancing with incomplete mock receipt envelopes; it was interrupted. The final scenario tests use the shared Chat loop with explicit fixture outputs and schema_version receipts; existing WOR-341 canonical-driver coverage passes in the 154 selection. The next fixture run had three unknown-scenario failures because the test used `name` instead of the real `has_window(app)` contract; corrected, with all final scenarios passing. The initial Mobile test used an incorrect affordance key; corrected to the existing toggle, without product changes. The default system Python lacks pytest; verification uses the repository venv.

One historical WOR-249 combined prompt-audit gate remains failing and is explicitly deselected in the final focused run. It compares size against the much older lossy prompt and its WOR-266 helper still demands an old duplicate-action phrase. The prior WOR-341 exact-schema delivery already invalidated that size baseline. Actual current safety/model-authority checks pass; no replay veto or schema weakening was reintroduced to make this historical aggregate green. This is an unresolved test-maintenance limitation, not a completed full-suite claim.

## Git, preservation and operator test

Both repositories remain on main with unchanged HEADs: Desktop `509e17c80156fc366eaddad31b1205aece98b8e6`, Mobile `8c9a974386afbae3c8eaeac4dfed4139a74a54a3`. Both indexes are empty. Nothing committed, pushed, built, installed, published or deployed.

Start/end source hashes cover 193 Desktop Python files and 51 Mobile Dart files. Exactly the four listed product files change; 240 remain byte-identical. An AST comparison proves all of agent.py except SYSTEM_PROMPT_CORE is semantically identical to the task-start baseline. Tool dispatch, Account bridge, parsers, ToolAttempt/ActionTruth, server/protocol, browser ownership/launch, orbs and animation sources are unchanged in this task. Existing dirty/untracked source, tests, reports, deleted local settings, benchmarks and machine-local directories are preserved. They remain uncommitted as pre-existing work, not candidates added by this request. Task files also remain uncommitted by instruction.

Baseline/final exact Git inventories, source hashes, nine full deterministic prompt snapshots and sanitized recorded metrics are saved outside the repo in the tempfile-resolved `osia-prompt-verification-audit` directory. User-owned customization text is omitted from these prompt snapshots. These are local audit evidence, not staged generated artifacts.

Linear WOR-341 is updated with execution findings and WOR-249/WOR-286 design context. WOR-340 is updated only for the minimal presentation fix and preservation of approved V5. Both stay In Progress. Source changes await operator testing in an app running this edited source.

Manual test:

1. Use Account/gpt-6-luna and the original French Steam/site request. Confirm simple public headings display without `**` on Desktop and Mobile, and raw stored events retain the original text/provenance. Wording may still be an English heading; this fallback does not claim control over Account authoring.
2. Confirm every new turn expands before commentary, manual collapse survives live updates, completion collapses, and reopening shows all received commentary. Check original font/transparency/orbs/continuous animation and unchanged final answer.
3. Count actual ToolAttempts separately from summary entries. Inspect their facts: ready window versus process-only, navigation commit versus requested content, and unknown/error recovery. Check the physical app and website; do not equate terminal labels with that observation.
4. Compare DeepSeek Flash and one already configured other provider with equivalent prompts, available tools and documented starting state. Record first useful effect, factual completion, actual repeated checks and narration. Only then assess whether live behavior improved.
5. Exercise one uncertain launch/navigation outcome, one real recoverable error, and a multi-step task with distinct required outcomes. Confirm sufficient observations, selected-model continuation and no false-success final.

OSIA PROMPT/VERIFICATION AUDIT: DEMONSTRATED FIXES IMPLEMENTED — AWAITING OPERATOR TEST
