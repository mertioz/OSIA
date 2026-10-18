# Claude connection for the Windows candidate

Rechecked 18 October 2026. This supersedes the previous audit's statement that
native Messages is future work. ChatGPT Account already works in the operator's
source checkout; authenticated source acceptance is not a release prerequisite.

## Implemented connection

OSIA uses the native `/v1/messages` API by default for Claude API. The existing
Console key card opens the official key page and accepts a workspace ID. Keys
continue through `key_chain.iter_keys`, including technical-failure rotation.
Console workspace selection determines API billing. Eligible Max/Team monthly
API credits can fund API requests when linked through Anthropic's Console;
this does not turn a BYOK request into subscription-limit traffic.

Native transport supports exact model IDs, system/history context, SSE text,
provider-returned public Thinking summaries, signatures and redacted blocks,
native tool proposals and correlated tool results, images, usage and rate-limit
metadata, typed API errors and the canonical stream cancellation mechanism.
Only OSIA dispatch executes tools. No Agent SDK or second execution loop exists.
The optional `anthropic_transport=compatibility` setting retains the previous
OpenAI-compatible transport. Switching transport never changes billing/account.
Messages requires a per-response `max_tokens`; the adapter supplies 16384 and
reports `max_tokens` stop as truncation, never objective completion. No global
turn/tool/retry ceiling is introduced. Usage/rate metadata is available on the
PC transport; no new Mobile wire schema or quota UI is claimed.

## Official authentication boundaries

* [Console CLI authentication](https://platform.claude.com/docs/en/cli-sdks-libraries/cli/authentication)
  documents `ant auth login`, browser or pasted-code OAuth, workspace binding,
  named profiles and CLI-owned token refresh/storage. It documents local CLI
  development/scripting; it does not establish a public OAuth client
  registration grant for OSIA. We do not reuse its client ID, credentials,
  private store or Claude Code tokens. A CLI integration would also require a
  reviewed redistributable Windows runtime and supported inference interface.
* [Developer login guidance](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account)
  directs applications built for others to Console API authentication.
* [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
  requires prior Anthropic approval for third-party products offering claude.ai
  login or rate limits. This is the precise approval boundary for OSIA.
* [Plan notice](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
  says eligible SDK/third-party subscription usage continues. It does not grant
  OSIA its own OAuth client or supersede the developer approval condition.
* [API credits](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)
  are distinct from subscription allowance. Provider account eligibility,
  linking, balances and actual entitlement require operator verification.

No unauthorized subscription-login button is shipped. Console OAuth for an
OSIA-owned client and subscription access remain externally unconfirmed;
this limitation does not block native API inference in the candidate.

## Operator-ready request to Anthropic (not sent)

Subject: OSIA desktop — approved Console OAuth and Claude subscription access

We develop OSIA (https://osia.space), a Windows desktop assistant with a Mobile
client. The desktop owns model execution and the user's local credentials.
We currently integrate Claude through the native Messages API with user-supplied
Console API keys and optional workspace selection. All tool execution stays
inside OSIA's canonical Chat engine with cancellation and factual receipts.

We request confirmation of OSIA's eligibility to offer an application-owned
Console browser/device OAuth sign-in and, separately, claude.ai Pro/Max/Team
subscription access. Please provide the application/partner approval process,
OAuth client registration, permitted scopes, PKCE/redirect requirements,
refresh/revoke rules, Windows distribution requirements, branding requirements,
workspace-selection contract and explicit API-credit/subscription billing rules.

Our integration needs a supported native inference transport with streaming,
public Thinking summaries and tools, while retaining OSIA's existing execution
engine. We will not impersonate Claude Code, copy another application's tokens,
or silently switch a user from subscription allowance to paid API usage.
Please confirm whether this architecture is supported and which approval is
required before enabling a sign-in button in a distributed OSIA build.

## Validation

269 focused regressions pass, including native and compatibility HTTP fixtures,
actual canonical temporary-file reads and native result reinjection with the
same model, public Thinking, typed errors, account lifecycle, cancellation,
ActionTruth, DeepSeek and PC-side Mobile projection. No real paid requests or
authenticated Claude login were performed. Frozen/package validation is tracked
separately in WOR-344; both tickets remain open for operator acceptance.
