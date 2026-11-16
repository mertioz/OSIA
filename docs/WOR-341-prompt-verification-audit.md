# Prompt and proportionate verification audit — WOR-341

Original audit: 11 October 2026. Consolidated on 16 November 2026. Historical measurements below describe that audit's source and fixtures. Current instructions are in [AGENTS.md](../AGENTS.md), the canonical source prompt and the Work Drive; they are not defined by an old report's delivery status.

## Distinct findings

The bold heading delimiters reported by the operator already existed in provider-authored public Account summaries. The old clients displayed that public wording literally. A narrow display helper was added on Desktop and Mobile; its current behavior is consolidated in [Thinking V5](WOR-340-thinking-v5.md). Raw canonical wording and provenance were not rewritten.

Execution-efficiency evidence was observational, not a controlled speed comparison. The three retained Steam/website runs below did not demonstrate duplicated invocations of the same observation tool. A read tool's success receipt established completion of the read, not necessarily the user's physical objective. An unknown URI handoff gave a legitimate reason for more evidence.

The demonstrated prompt defects were legacy retry/reconciliation prescriptions conflicting with the selected-model architecture and repeated completion prose in result feedback. The audit removed those repetitions at their existing source/reinjection call sites; it did not change tool dispatch or demonstrate a measured post-change speedup.

## Canonical assembly references

The original audit inspected nine provider identities and actual Account/compatible wire assembly through these existing boundaries:

1. agent.SYSTEM_PROMPT_CORE, get_system_prompt and _system_prompt_for_current_turn supply the shared core.
2. tool_contracts.static_tool_prompt_block supplies exact canonical schemas with shared JSON Schema definitions; personality, skills and connected tools are supplied through their existing sources.
3. _wire_messages performs the current provider/context adaptation. The selected provider and model remain the authority under AGENTS.md.
4. ChatGPT Account provider._prepare and AppServerClient.start_or_resume_conversation apply the actual current native contract.
5. The established tool-result reinjection path returns factual results and argument-validation feedback to the same model.

The old descriptions of an injected authoritative response-language contract are removed: current TurnContext carries UI localization/observability, and the selected model owns response language. The old instruction to recreate an Account thread whenever the system/schema fingerprint changes is also removed. Current provider.py distinguishes runtime/thread reuse, persisted history and application_context. The source, not this historical assembly description, defines the supported lifecycle.

The audit retained technical target freshness before visual actuation. It did not prescribe an independent screenshot or re-observation after every operation. The current canonical prompt and AGENTS.md define evidence handling; there is no second verification recipe in this document.

## Recorded execution comparison

These uncontrolled sessions had different starting states and are not provider benchmarks. Repeated argument fingerprints identify candidates for review, not proof that repeated work was unnecessary after intervening state changes.

| Session | Attempts | Observation tools | Same-tool/argument candidates | Public entries / characters | First dispatch / receipt | Last receipt / trace terminal |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Luna, 16:31–16:33 local | 6 | 2 | 1 | 6 / 466 | 6.479 s / 6.980 s | 82.046 s / 86.793 s |
| Luna, 16:24–16:25 local | 6 | 2 | 1 | 6 / 544 | 8.152 s / 8.527 s | 89.526 s / 89.677 s |
| DeepSeek Flash, 16:22–16:23 local | 5 | 1 | 1 | 1 / 76 | 4.453 s / 5.424 s | 55.093 s / 57.311 s |

The first Luna sequence reopened an application around intervening browser work. The second contained an outcome_unknown URI handoff. DeepSeek repeated navigation with the same fingerprint after intervening application work. None demonstrated a repeated call to the same observation tool. First dispatch/receipt and trace terminal are ledger timestamps, not independent physical-success measurements or provider generation times.

The audit also exercised equivalent simple success, uncertain outcome, recoverable failure and multi-step scenarios in isolated fixtures. No comparable third-provider physical run was found in the inspected records. No private prompts, hidden reasoning, credentials or tool-argument values were exported.

## Prompt and presentation changes recorded then

The old prompt repeated completion conditions in both the objective paragraph and every result batch, and prescribed reconciliation as a prerequisite to a fresh model request. These conflicting prescriptions were replaced by factual uncertainty and selected-model continuation; this report does not reproduce the obsolete instructions as an alternative policy.

Result-feedback translations were updated together in the six then-supported locales. English result feedback decreased from 389 to 239 characters; isolated French system snapshots decreased by 132 characters. Exact schemas were preserved. These are prompt-size measurements, not execution-speed results.

The presentation fallback handled complete simple whole-line bold wrappers only. It retained inline Markdown, links, code, escapes, incomplete streamed text, stored wording and final replies. It did not turn a heading into an invented first-person claim. A public summary mechanism may retain its own wording despite style guidance; raw private reasoning is not a substitute.

The source changes were limited to the canonical prompt/result text and the two display helpers. The audit found no basis for redesigning launch/navigation receipts or for globally declaring uncertain launch branches verified.

## Dated verification and limits

- 154 focused Python checks plus 25 further checks: 179 passed.
- 33 Mobile checks passed; targeted Flutter analysis was clean.
- Five changed Python source/test files compiled and imported; whitespace checks passed.
- Initial fixture/constructor mistakes were corrected. Interrupted fixtures were not counted as successes.
- One historical combined WOR-249 prompt aggregate was explicitly excluded because it compared against an older lossy prompt and an obsolete duplicate-action phrase. No replay veto or schema weakening was restored for it.

The recorded whole-product hash comparison covered 193 Desktop Python and 51 Mobile Dart files; only four product files changed in that audit. This is a dated scope proof, not the current working-tree inventory.

At this later audit, the operator had reported Thinking V5 and tool recovery working. The report's then-current pending checks concerned the new formatting/verification observations; they must not be presented as a current unpublished V5 task.

## Historical source and related records

- [Complete original audit, prompt deltas, exact measurements and limitations](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-341-prompt-verification-audit.md).
- [Distinct schema-validation and recovery investigation](WOR-341-tool-reliability.md).
- [Consolidated Thinking reference](WOR-340-thinking-v5.md).

Current delivery, installer generations and operator results belong in the dated Work Drive journal. The old Linear status, dirty-tree lists and uncommitted delivery assertions are not current operating instructions.
