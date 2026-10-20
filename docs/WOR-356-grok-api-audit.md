# Grok / xAI delivery — 2026-10-20

Grok / xAI (`xai`) and GroqCloud (`groq`) are independent providers. Their keys,
endpoints, model identities and configuration entries remain separate.

## Verified official contracts

Sources retrieved 2026-10-20:

- [Grok 4.7 guide](https://docs.x.ai/developers/grok-4-7): `grok-4.7` is the
  current public flagship, released September 21. Text/image input, text output,
  500,000-token context, low/medium/high(default)/xhigh effort; reasoning cannot
  be disabled. Its Fast variant is not available on the public API and is not
  added to OSIA's catalogue. Image/video generation and voice use separate APIs;
  OSIA's new Grok Chat integration does not claim those generation services.
- [Models API](https://docs.x.ai/developers/rest-api-reference/inference/models):
  `/v1/models` lists identities; `/v1/language-models` adds modalities, aliases
  and model-specific effort metadata. OSIA intersects these lists, excluding
  media-only entries. If language metadata fails, exact documented contracts
  remain usable when `/models` confirms them. Discovery is not proof of a paid
  inference request or of sufficient credits/endpoint ACLs.
- [Responses reference](https://docs.x.ai/developers/rest-api-reference/inference/responses)
  and [function calling](https://docs.x.ai/developers/tools/function-calling):
  use Bearer API key at `https://api.x.ai/v1`, OpenAI SDK Responses, SSE and
  custom function schemas. OSIA advertises its tools and returns correlated
  function_call_output receipts; xAI server-side search/execution is not enabled.
- [Reasoning](https://docs.x.ai/developers/model-capabilities/text/reasoning):
  preserve encrypted_content privately and return reasoning items unchanged on
  subsequent requests. Public summary_text is rendered in Thinking. For the
  specifically documented Grok 4.7 contract, reasoning_text.delta also carries
  a public summary; this does not authorize exposing that event from other
  providers or unknown models. No encrypted content is rendered or decrypted.
- [Retirement](https://docs.x.ai/developers/migration/may-15-retirement):
  `grok-3` and several older Grok 4/Fast/Code slugs redirect to other models after
  retirement. They are excluded from new selections rather than falsely claiming
  the original identity is still served. Stored configuration is not rewritten.
  Existing `grok-3-mini` remains eligible only when listed, with its text-only
  legacy chat contract; the old blanket vision claim is corrected.
- [Rate limits](https://docs.x.ai/developers/rate-limits) and
  [errors](https://docs.x.ai/developers/debugging): limits depend on team/model
  and account tier. Existing typed HTTP/SDK error handling and shared technical
  key rotation apply, including 401/403/429/5xx; content errors do not rotate.
  A broken/unfinished stream remains a transport failure, never success.

## Account-login investigation

The [official account FAQ](https://docs.x.ai/developers/faq/accounts) states
that Grok and the API can share an account but their billing is separate.
The [management authentication reference](https://docs.x.ai/developers/rest-api-reference/management/auth)
documents team-bound API keys and endpoint/model ACLs, not a delegated consumer
subscription grant for third-party applications.

No supported third-party Grok subscription-login method or entitlement transfer
was established by the reviewed public documentation. This is a documented
research limitation, not a claim that a private partner program cannot exist.
OSIA offers API-key configuration and a localized explanatory notice on Desktop
and Mobile. It offers no account-login button, cookie extraction, token reuse,
impersonation or unofficial endpoint.

Operator next step, if subscription access is wanted: ask xAI at the documented
support@x.ai or sales@x.ai channel for third-party delegated authentication,
client registration, approved scopes, entitlement/billing terms and permission
to integrate OSIA. No request has been sent on the operator's behalf. Claude
Account authorization in WOR-343 is independent and unchanged.

## Implementation and evidence boundary

The existing OSIAAgent still owns selection, tool dispatch, ActionTruth,
cancellation and turn state. The shared ResponsesClient performs protocol
translation only and is reapplied when the key or endpoint changes. Streaming
and nonstreaming tool results return to the same exact `xai:grok-4.7` route.
Cross-route changes fall back to factual textual history instead of forwarding
another model's opaque Responses context. EOF, incomplete and typed error
behavior uses the canonical engine's existing lifecycle.

The Desktop Grok card uses the existing password field, API-key storage owner
and official Console link. GroqCloud remains a separate card. The Mobile
catalogue receives PC-owned identity, name, source, modalities, effort options
and availability; no provider secret enters the snapshot. API key entry follows
the existing transient editor-to-PC mechanism, with no new credential store.

Tests use the real SDK and synthetic HTTP responses. The canonical tests execute
an actual read_file against a temporary fixture, then assert the correlated
native receipt and encrypted context return only to the selected provider/model
in streaming and nonstreaming modes. Other contracts cover image translation,
reasoning settings, public/private summary separation, discovery/media filtering,
auth headers, UI key routing, errors, receipt identity and stream closing.

No xAI key is configured on this machine. Neither live xAI discovery nor live
Grok inference has been verified. The implementation is contract-tested; the
operator must create a Console key with appropriate model/endpoint access and
API credits, then authorize paid smoke calls before LIVE_INFERENCE_VERIFIED can
be claimed. No public build, version transition, store submission or Linux
packaging has been performed.
