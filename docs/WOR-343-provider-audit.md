# WOR-343 provider audit

Audit date: 2026-10-18. Desktop baseline: `origin/main` at `4d5f2e6e` (WOR-342).
Mobile baseline: `origin/main` at `b47da71`. Work is isolated in
`codex/wor343-providers`; the original dirty PC/Mobile checkouts are preserved.

Evidence labels: HARNESS = deterministic fixtures; CONTRACT_ONLY = source and
official documentation; LIVE = authenticated execution; BLOCKED = external
authorization/input needed; NOT_TESTED = no execution evidence. A catalog entry
or successful authentication does not prove inference entitlement.

## Before implementation

| Capability | Classification | Evidence |
| --- | --- | --- |
| ChatGPT managed browser/device login | UNVERIFIED / CONTRACT_ONLY | `AppServerClient.begin_login`, `begin_device_code_login`, `wait_for_login` use documented account RPCs. No operator sign-in performed. |
| ChatGPT persistence/renewal | UNVERIFIED / CONTRACT_ONLY | Isolated OSIA `CODEX_HOME`; managed Codex auth owns storage/refresh. OSIA does not read another app's credentials. |
| ChatGPT process lifecycle | BROKEN | `_reader_loop` dispatches valid messages without checking process ownership; `_wait_loop` counts obsolete process crashes. Logout drops login waiters without waking them; process failure does not end login waits. |
| Model discovery/selection | INCOMPLETE | Paginated `model/list` filters hidden/non-text entries. `selected_model` silently chooses a different default when a saved selection disappears. No inference entitlement guarantee. |
| Account switching/quota | INCOMPLETE | Provider logout clears threads/models; client clears quota on successful login/logout. `account/updated` currently does not invalidate quota, and cached status can retain disconnected models. |
| New SIWC plan-sharing OAuth | INCOMPLETE / BLOCKED for paid distribution | No app-owned dynamic client registration, stable `ext_agent_host_id`, scope/ID-token validation, token refresh or Responses provider configuration exists. This is a separate route from managed Codex auth. No OSIA partner approval evidence. |
| Canonical tools/model authority | WORKING / HARNESS | Baseline provider boundary/model authority regressions pass. Dynamic tool requests route through canonical OSIA dispatch; raw Codex physical tools are rejected; provider reroutes fail explicitly. |
| Streaming/Thinking | WORKING / HARNESS | Existing native message/summary events, correlation and provider timeline fixtures. Raw reasoning text is ignored. |
| Claude Console API/BYOK | UNVERIFIED / CONTRACT_ONLY | `OSIAAgent.set_model` uses the officially documented OpenAI SDK compatibility endpoint on `api.anthropic.com/v1`; API keys resolve through `key_chain.iter_keys`. No authenticated call performed. |
| Claude API workspace credentials | BROKEN for multi-workspace identity keys | Chat client, key rotation and model discovery do not supply the now-required `anthropic-workspace-id` header. Single-workspace keys do not require it. |
| Claude account/Agent SDK | UNSUPPORTED for OSIA absent approval / BLOCKED | No existing account integration. Product developer/SDK documentation requires prior approval for third-party claude.ai login/rate limits. SDK embeds Claude Code's agent loop, not a documented standalone inference transport. |
| Claude Thinking through compatibility API | INCOMPLETE / CONTRACT_ONLY | Thinking can be enabled but Anthropic documents that this route does not return Claude Thinking. Do not synthesize it. Native Messages migration is separate work. |
| Claude Live API | INCOMPLETE | `osia/voice/live_api.py` explicitly remains a stub; unrelated to account connectivity. |
| PC/Mobile account projection | WORKING / CONTRACT_ONLY pending regression run | `mobile_model_catalogue.account_snapshot` allowlists account/model/quota fields; server sends the PC snapshot. Mobile owns no provider credentials or inference runtime. |
| Windows frozen runtime | UNVERIFIED / CONTRACT_ONLY | Package stages Codex 0.144.1; client separately accepts reviewed 0.156.1. Runtime resource paths, supervised hidden process policy and isolated environment are present. No new build/application launch authorized. |

Baseline HARNESS: 46 passed (runtime compatibility, provider boundary, model
authority, provider timelines and WOR-342 Qt crash checks).

## Authorization and supported routes

OpenAI's [App Server documentation](https://developers.openai.com/codex/app-server)
documents managed ChatGPT auth, automatic refresh and account RPCs. Preserve
this existing path; its protocol support is not proof of OSIA commercial rights
or actual model access.

[SIWC plan usage](https://developers.openai.com/siwc/token-sharing-open-source)
defines an additional app-owned OAuth route for OSS/local apps, requiring a
stable opaque host ID and client registration. Paid/hosted apps are directed to
the [interest form](https://openai.com/form/sign-in-with-chatgpt-interest/).
Eligibility/deployment classification and any necessary OpenAI approval remain
external dependencies. No token migration or Codex impersonation is permitted.
Its [App Server configuration](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)
uses an app-owned OAuth access token in the child environment, Responses API,
and restart/resume on renewal. The
[preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
must be rechecked before implementing that separate route.

Anthropic's [October subscription notice](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
describes continued SDK/third-party subscription usage and API credits. This
does not establish OSIA distribution approval. The
[developer login guidance](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account)
directs products built for others to Console API keys/supported cloud providers;
the [SDK overview](https://code.claude.com/docs/en/agent-sdk/overview) requires prior
approval to offer claude.ai login/rate limits. OSIA therefore uses Claude API,
with separate API billing/credits and no promise that Pro/Max includes access.

[API authentication](https://platform.claude.com/docs/en/manage-claude/authentication)
also documents WIF for configured workloads and App Attest for registered Apple
apps. Neither is a Windows subscription login nor a reason to move inference
or secrets into Mobile. The existing
[OpenAI compatibility route](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)
supports chat/tools/streaming, with documented feature limits.

## Validation boundaries

No paid/live inference, browser login, credential inspection, external app
launch, release, TestFlight build, version change or deployment is performed.
Operator validation must cover first login, decline/cancel, expired/revoked
session refresh, account switching, actual model entitlement, 401/403/429,
Windows packaged restart/Stop and physical Desktop/Mobile reconnect.
Mocks establish code behavior only. Keep WOR-343 In Progress until acceptance.

## Correction 1 — process/login lifecycle

Retired stdout/stderr/exit callbacks are ignored. Logout first cancels a pending
managed login. Process/protocol failure wakes login waiters with a typed reason;
shutdown produces cancellation. A new login remains possible afterwards.
Callers retain the same public signatures and account RPCs.

HARNESS: new regressions reproduced **9 failures / 1 pass before** the fix;
afterwards **54 passed** across the new lifecycle tests, runtime compatibility,
provider-native boundaries, public reasoning summaries, cancellation and turn
supersession. `py_compile`, touched-module imports and `git diff --check` passed.
Existing FastAPI startup deprecation warnings remain unrelated.

## Correction 2 — selected model and account freshness

An explicit saved model stays exact even when absent from the current catalog;
new selections still validate against discovery, and first use may initialize
the reported default. Actual provider execution, not the catalog, determines
access. The Settings probe no longer writes a replacement selection. The old
test requiring silent replacement contradicted the operator's selected-model
contract and was updated to require no write.

Account updates invalidate quota and model caches and give conversation state a
new account generation. Late model/quota responses from the old account are
discarded. Disconnected status exposes no cached model list. After successful
login, old threads/alternate processes are released. Runtime startup happens
before thread reuse decisions, so a restarted process receives full canonical
history with the same model rather than a stale unloaded thread ID.

HARNESS: six new state defects reproduced before correction. **73 passed**
afterwards, including dynamic/active native handoffs, model authority, public
summaries, Mobile parity, Thinking, WOR-342 Qt stability and quota/login fixtures.
`py_compile`, imports, exact diff review and credential-pattern scan passed.
No WebSocket schema or Mobile source changes.

## Correction 3 — Claude API workspace authentication

Added the optional PC-owned `anthropic_workspace_id` setting and a shared
`osia.anthropic_api.request_headers` helper. Existing `key_chain.iter_keys`
resolution remains authoritative. Chat client creation, key rotation, endpoint
rebuild and model discovery preserve the Console workspace header. Existing
single-workspace keys send their original headers when the setting is empty.

The PC API card/dropdown says **Claude API**, links to the current Console key
page and explains Console billing/credits (including eligible plan credits).
It provides an optional workspace field; key-presence indication does not claim
authenticated access. The credential store is unchanged; workspace IDs are
non-secret settings, and no raw credentials are added to Mobile projection.
The save path persists workspace selection before key-change listeners rebuild
clients. No account-login button or Claude SDK agent was introduced.

HARNESS: three failing workspace/UI cases reproduced before implementation.
**162 passed** afterwards, covering the Claude HTTP fixtures, 401/403/429/503,
key rotation, provider parsers, Thinking, reasoning mappings, DeepSeek/non-
DeepSeek invariants, key-chain rules and WOR-342 regressions. One fixture-backed
Claude streamed native tool proposal passed through the real canonical
`read_file` tool on a temporary file, then its factual contents were reinjected
into the same selected Claude model. This is not LIVE provider evidence.
Compile/import/diff checks passed. Offscreen card inspection used an explicitly
loaded system font; it does not establish packaged application rendering.

The existing OpenAI-compatible Claude route remains supported, with its
documented Thinking-return and structured-output limitations. Native Messages
transport is not claimed to exist. Operator must validate a real Console key
(single-/multi-workspace), permissions, credits and actual model access.

## Correction 4 — truthful desktop disconnect status

The desktop disconnect worker previously emitted `connected=False` even when
logout raised. It now rereads the provider's factual state; a still-connected
account remains connected and shows a retryable disconnect failure. The local
UI-only failure marker is removed before storing/projecting account status.
Errors never include exception bodies or credentials. Mobile already rereads
actual state and requires no source change.

HARNESS: the false-disconnect case failed before the fix; **55 passed** after,
covering all WOR-343 cases, Mobile parity, Thinking V5 disclosure/UI, WOR-342
crash regressions and connect/disconnect/restart selection preservation.
Compile/import/diff/credential checks passed.

## Correction 5 — complete Claude model discovery

The previous discovery code fetched only the first page of Claude's official
Models API (default 20 entries). It now follows `has_more` / `last_id` using
`after_id`, preserving exact model IDs, the same supplied key and workspace.
The existing request site and discovery callers/signatures are retained. Other
providers keep their original single-request behavior. A failed later page or
missing/repeated cursor returns discovery failure, rather than presenting a
partial list as complete. Cursor validation protects this one operation; it
does not impose any Chat/tool iteration limit.

Source contract: [Claude List Models](https://platform.claude.com/docs/en/api/models/list).
HARNESS: all five pagination cases failed before correction; **74 passed**
afterwards across Claude discovery/authentication, reasoning, Mobile parity and
DeepSeek/non-DeepSeek invariants. Compile/import/diff/credential checks passed.

## Final operator report

| Requested item | Result and evidence |
| --- | --- |
| 1. ChatGPT Account capabilities | Existing managed browser/device login, account read/logout, quota, model discovery, streams and canonical tool adaptation retained. HARNESS verified; authenticated access UNVERIFIED. |
| 2. Claude capabilities | Console API/BYOK with optional workspace, model discovery, compatible chat/streaming/tool proposals. No Claude Account or Agent SDK runtime. |
| 3. Official methods/conditions | Managed Codex RPCs and Claude Console API documented above. SIWC app-owned registration is a distinct route; paid/hosted use needs OpenAI's separate eligibility/partner process. Claude product subscription login needs Anthropic approval. |
| 4. Exact defects | Retired process callbacks, orphaned login waits, silent model replacement, stale account quota/models/thread reuse, restart before stale thread reuse, missing Claude workspace headers, false desktop logout status, first-page-only Claude discovery. Correction sections identify methods and evidence. |
| 5. ChatGPT corrections | Corrections 1, 2 and 4. Existing compliant managed auth preserved; no token extraction or new OAuth workaround. |
| 6. Claude corrections | Corrections 3 and 5. Shared workspace headers, API settings/labels/billing, official key link and complete pagination. |
| 7. Subscription status | ChatGPT managed path exists and matches documented RPCs, but no LIVE sign-in/inference was performed. Separate SIWC plan-sharing route is not implemented. Claude subscription login for OSIA is BLOCKED absent prior approval and a supported transport compatible with the canonical engine. |
| 8. API-key status | Existing OpenAI API route unchanged. Claude API route improved and HARNESS validated; real Console authentication, balance and billing remain NOT_TESTED. No implicit switch from subscription to API billing. |
| 9. Catalog/entitlement | Exact IDs retained; explicit saved ChatGPT model never silently replaced. New selections validate against discovery. Catalog presence, stored credentials and a green key-presence indicator do not prove request entitlement. Only successful authorized execution proves that request. |
| 10. Tool integration | Real canonical read_file on a temporary fixture file, with mocked Claude HTTP/SSE and same-model result reinjection. ChatGPT native dynamic handoff, raw-native-effect rejection, model authority and tool-surface regressions pass. No LIVE model-driven physical effects claimed. |
| 11. PC/Mobile parity | PC remains owner of accounts, workspace, model execution and tools. Existing sanitized WebSocket schemas retained. PC-side Mobile projection/lifecycle/reconnect regressions pass. No Mobile files, build numbers or TestFlight releases changed; no Flutter rebuild/test or physical iOS reconnect performed. |
| 12. Authentication/security | Managed login/device/cancel/success/timeout/failure boundaries, isolation, logout, account freshness and secret-free snapshots covered by fixtures. API keys still use existing store/key_chain. No credentials copied from other apps, logged or committed. Existing Windows credential-store protection is unchanged; no new encryption/ACL claim. |
| 13. Tests | Final combined HARNESS: **404 passed, 18 existing FastAPI deprecation warnings, 43.33 s**. All 14 changed Python files py_compile successfully; all 8 touched production modules import. Diff checks and reviewed candidate credential-pattern scans pass. |
| 14. Windows package | Source tests ran on Windows/Python 3.12. Frozen paths with spaces/Unicode, Qt DLL hook, build environment isolation, package source inclusion and managed-runtime smoke requirements pass as HARNESS/CONTRACT_ONLY. Actual frozen executable startup/login/streaming/Stop is NOT_TESTED; no package build or app launch performed. |
| 15. Commits/remote SHAs | Five source corrections listed below, each pushed immediately and exact remote HEAD independently verified. Documentation-only finalization follows them in Git history. No force push, reset, clean, broad staging or release. |
| 16. External blockers | No evidence of OSIA OpenAI paid/hosted SIWC approval; no app-owned SIWC registration, persisted host ID or app-owned token lifecycle. No evidence of Anthropic product subscription-login approval or a documented standalone subscription inference transport that avoids a second agent loop. |
| 17. Operator sign-in tests | Real ChatGPT login/decline/expired-refresh/revocation/switch/logout, entitlement/quota and cancellation; real Claude Console key (single-/multi-workspace), revoked/expired key, 401/403/429/credit exhaustion; restart and physical PC/Mobile reconnect. These require operator interaction and authorization for any billed requests/app launch. |
| 18. Remaining work | Operator source acceptance, then separately authorized frozen-package validation. Confirm OSIA deployment/commercial eligibility before any SIWC expansion. Obtain Anthropic approval before adding subscription product login. A native Claude Messages transport for full native Thinking/structured outputs remains a separately scoped enhancement, not an existing capability. |
| 19. Linear | WOR-343 remains **In Progress**. Audit and every pushed correction have their own comments containing evidence, full SHA and remaining LIVE limits; final acceptance summary is posted after documentation push. |
| 20. Recommended next step | Operator validates the corrected source checkout at C:\\Users\\vertu\\osia-worktrees\\wor343-providers with a real managed ChatGPT account and an explicitly configured Claude Console API key/workspace. Confirm exact selected model, one safe read-tool turn, Stop, restart and Mobile reconnect before ticket acceptance. |

### Source correction commits

| Correction | Verified OSIA origin/main SHA |
| --- | --- |
| 1 — retired process/login lifecycle | `c4d14d6f54873f8b21d9da5f1a1743f5572fd7d1` |
| 2 — exact model/account/restart context | `7f2d9707196758e5c6ecf1dda0e96ac606288b1c` |
| 3 — Claude workspace API credentials | `07ce8b3071c9239037458c1d262d7ceb254d104e` |
| 4 — observed logout projection | `e1ed7eb958dbe8f6de2152dbc203fc2766d0905b` |
| 5 — Claude model pagination | `6bda58fb0b9d8d650f144ca539b6bfcfa80829af` |

### Authentication evidence boundaries

| Scenario | ChatGPT | Claude API |
| --- | --- | --- |
| Initial login / success / cancel | HARNESS official managed RPC fixtures | HARNESS SDK HTTP + key/workspace configuration |
| Failed authentication | HARNESS login/error/cancel boundaries; actual refusal NOT_TESTED | HARNESS 401/403 status preserved |
| Expired/revoked credentials / refresh | CONTRACT_ONLY: managed Codex owns renewal; real expiry/revocation NOT_TESTED | HARNESS 401 preserved; manual Console key replacement, no invented refresh token |
| Logout / account switch | HARNESS cancellation, observed state and account-generation freshness | HARNESS key rotation/workspace persistence; no subscription session |
| Rate limits / unavailability | HARNESS quota snapshots/process failure; LIVE 429/entitlement NOT_TESTED | HARNESS 429/503 fixtures and existing shared key-chain regressions |
| App restart / frozen package | HARNESS process generation/context and frozen resource contracts; actual app NOT_TESTED | HARNESS client reconstruction/header preservation; actual app NOT_TESTED |
| PC/Mobile reconnect / Stop | HARNESS canonical WebSocket/cancellation/parity regressions | Same PC canonical engine; physical Mobile/provider session NOT_TESTED |

### Changed files and callers

- `osia/chatgpt_account/app_server_client.py`: process readers/wait loop,
  login failure/wait/logout, account notifications and quota refresh.
- `osia/chatgpt_account/provider.py`: status/model cache, selected model,
  successful login, `_prepare` process/account conversation binding.
- `osia/anthropic_api.py`: shared header construction; consumed by
  `OSIAAgent.set_model`, `_activate_next_api_key` and `fetch_remote_models`.
  `_activate_fallback_endpoint` preserves the constructed headers.
- `osia/agent.py`: the two existing Claude client-construction callers;
  canonical execution semantics unchanged.
- `osia/models.py`: existing remote-discovery request and pagination;
  `discover_all_remote_models`, async discovery and UI/catalog callers retain
  their signatures.
- `osia/config.py`: optional non-secret workspace setting.
- `osia/gui/main_popup.py`: Claude API card/settings, initial-selection-only
  persistence and observed disconnect projection.
- `osia/i18n.py`: English/French API/workspace and disconnect-failure copy,
  with the existing English fallback for other languages.
- Five new `tests/test_wor343_*.py` files, one corrected legacy selection
  assertion in `tools/test_chatgpt_account_integration.py`, and this audit.

### Final regression coverage

The combined 404-test command included all new WOR-343 tests, provider/runtime
compatibility and model-authority suites, WOR-300/301 tool-surface freedom,
WOR-302 native/textual/DSML parser and continuation, ActionTruth/cancellation,
WOR-305 WebSocket claims/supersession, WOR-308 Mobile projection, WOR-340
timelines/Thinking/disclosure, WOR-342 Qt stability, WOR-298 non-DeepSeek
snapshot invariants, reasoning/key-chain tests, selected managed account
auth/quota/settings fixtures, and seven Windows packaging/resource contracts.
This is a focused acceptance regression set, not the entire repository suite.

### Preserved local work

The isolated correction checkout is clean after reviewed commits. The original
PC checkout stays at `509e17c8`; its 22 pre-existing tracked changes and 128
untracked entries remain outside this task's staging/commits. This includes the
pre-existing `.claude/settings.local.json` deletion; changes in `osia/agent.py`,
`osia/chatgpt_account/app_server_client.py`, `osia/execution_trace.py`,
`osia/gui/main_popup.py`, `osia/gui/thinking_orbs.py`, `osia/i18n.py`,
`osia/main.py`, `osia/relay_connector.py`, `osia/server/server.py`,
`osia/tool_contracts.py`, `osia/turn_context.py`, `osia/voice/tts.py`; and the
existing WOR-178/186/200/227/247/249/265/269/311 test edits. Untracked tool state,
benchmarks, diagnostic data and older source/test/docs evidence were excluded
because they predate and are unrelated to these corrections.

The original Mobile checkout stays at `8c9a974`, with its 10 tracked edits
(`README.md`, `lib/l10n.dart`, `lib/screens/chat_screen.dart`,
`lib/services/execution_trace.dart`, `lib/widgets/execution_trace_block.dart`,
`lib/widgets/thinking_orbs.dart`, and WOR-254/257/265/311 tests) and three
untracked tests preserved. Its pushed accepted baseline remains `b47da71`.
Neither original checkout was reset, stashed, overwritten or relaunched. The
running/installed OSIA application has not been updated by these source pushes.
