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
