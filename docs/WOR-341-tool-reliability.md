# Tool schema and recovery investigation — WOR-341

Original investigation: 11 October 2026. Documentation consolidated on 16 November 2026. Product authority is defined by [AGENTS.md](../AGENTS.md), current ToolSpec/dispatch source and the Work Drive. Thinking presentation is consolidated in [Thinking V5](WOR-340-thinking-v5.md).

## Demonstrated incident

Read-only correlation of a saved Account/gpt-6-luna Steam/website request identified the first pre-dispatch rejection:

1. run_command received a command string and an integer timeout above its 300-second operation bound. Exact private command text and rejected value were omitted.
2. JSON Schema validation rejected $.timeout. The old nullable-union feedback exposed only anyOf, obscuring the useful maximum constraint.
3. The same model proposed computer_keypress instead. The old one-correction/same-tool gate ended the task before validating the alternative; it did not prove the alternative's arguments invalid.
4. Two desktop inspections and a screenshot had completed, but no correlated application launch, URI handoff or navigation dispatch established the original objective.
5. The stored error final was labelled success_verified. That historical label did not prove physical completion and was not retroactively rewritten.

The investigation corrected shared validation/recovery paths rather than adding a special Steam route or another planner.

## Distinct corrections

- Lossless generated tool declarations replaced lossy signatures that omitted bounds, nested/alternative schemas and optional fields. Shared JSON Schema definitions preserve the original independent schemas.
- Nullable validation failures expose safe matching minimum/maximum constraints, without rejected values or raw exception messages. Constraints are not silently clamped or coerced.
- Rejected proposals return factual feedback and the current schema to the selected model. The former one-correction/same-tool ceiling does not determine whether the user's objective is finished.
- Unsupported native proposals become same-model feedback through the canonical OSIA handoff. Native proposals do not silently gain direct PC execution.
- Existing application launch/status/URI capabilities were added to the Account native declaration list; they were already available textually. No application-name aliases or product-specific mappings were invented.
- The integrated Mistral provider was included in shared capability metadata.
- Direct dynamic read failures return sanitized field/type diagnostics and same-turn recoverability.
- Unknown outcomes use their actual typed state, not an automatic timeout label.

The current selected-model, unknown-outcome, tool-visibility and exact-turn guarantees belong to AGENTS.md. This historical report does not add independent replay, permission or semantic-routing rules.

## Receipt distinctions

Application process creation, exact ready-window evidence, URI handoff, committed navigation and requested page-content conditions establish different facts. A completed observation does not automatically prove the whole objective. The legacy navigate surface and typed navigate_browser surface retain distinct return contracts.

The old investigation exercised the real Windows launch function with injected resolution/spawn/window effects, a managed navigation operation against an in-memory page and same-model rejection/alternative/continuation fixtures. It did not physically launch Steam or visit the operator's requested website.

Later source changes and current readiness are recorded in the Work Drive; the October report is not proof that a current installed binary contains all later corrections.

## Dated inventory evidence

On 11 October the registry audit found 104 entries, of which 93 were available in its isolated environment and 11 were dormant or required a bound V4/vision turn. Every audited schema was valid and matched dispatcher properties/required arguments. Those counts and the then-current native frontier are historical, not a current tool catalogue.

No connected MCP tools were present in the isolated process. Registration/strict-validation/dispatch/unregistration fixtures passed, but configured credentials were not treated as proof of a live connector.

The original per-tool matrix distinguishes observed effects, composed harnesses and contract-only checks. Its complete rows and schema hash prefixes remain in the immutable [October audit snapshot](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-341-tool-reliability.md#per-tool-static-audit-matrix). Removed product modes and old tool counts must not be reintroduced from historical matrices.

## Verification record

Completed selections recorded then:

| Selection | Recorded result |
| --- | --- |
| Focused validation/recovery/schema/native bridge | 104 passed |
| Adjacent provider/parser/public summaries/Thinking/model authority/cancellation/ActionTruth | 187 passed |
| Added complete canonical rejection → alternate read → final fixture | 1 passed |
| Controlled Windows process truth | 54 passed; one older same-turn provider fixture excluded after interruption |

The investigation reported 346 completed passing checks in those final selections. Earlier failed/interrupted runs were excluded. An older additional run had 52 passes and four document cases demanding semantic duplicate caching removed by the completed architecture; that caching was not restored. The initial uncompressed prompt exceeded one context budget and was corrected before final delivery. One diagnostic fixture accidentally reached an SDK connection boundary because only streaming was stubbed; it was stopped and replaced with an explicit completion client. No successful live provider turn or physical action was established by that experiment.

Compilation/imports of the eight changed Python source/test files and whitespace checks passed. The nine source/test/report candidates had no credential signatures. The 244-file product hash comparison found only the three product files in the stated scope changed.

## Source scope and current references

The recorded product scope was osia/agent.py, osia/tool_contracts.py and osia/chatgpt_account/app_server_client.py. Their consumers include canonical prompt assembly, provider declarations, validation feedback, native handoff and typed outcome recording. The existing parser formats and concrete dispatchers were preserved.

Focused regression entry points include tests/test_tool_reliability_regression.py, tests/test_wor227_invalid_arguments.py, tests/test_wor178_tool_safety.py, tests/test_wor200_native_action_bridge.py and the controlled browser/application suites.

The stylistic prompt refinement also recorded operator approval of Thinking V5. Its later formatting investigation is retained separately in the [prompt verification audit](WOR-341-prompt-verification-audit.md); repeated style instructions and UI rules are not duplicated here.

Full original evidence, source/test file lists, historical tool matrix, limitations and scope inventories: [pre-consolidation Git snapshot](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-341-tool-reliability.md). Current commits, builds and operator acceptance are tracked in the dated Work Drive journal.
