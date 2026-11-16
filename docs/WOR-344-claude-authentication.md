# Claude API connection

Implementation reference checked on 16 November 2026. This is the single Claude
transport/authentication reference; the earlier provider-wide audit's claim
that native Messages was future work is no longer current.

## Implemented route

[osia/claude_messages.py](../osia/claude_messages.py) uses native `/v1/messages`
by default. `anthropic_transport=compatibility` retains the OpenAI-compatible
route. Both use the existing Console API-key configuration and optional
workspace ID. [osia/anthropic_api.py](../osia/anthropic_api.py) constructs the
headers; [key_chain](../osia/utils/key_chain.py) owns key rotation.

Native Messages supports system/history context, SSE text, native tool proposals
and correlated results, images, usage/error metadata, signed/redacted blocks
and provider-returned public Thinking summaries. OSIA executes tools through
its canonical Chat runtime. Transport adaptation creates no Agent SDK loop.

Messages requires a per-response `max_tokens`; the adapter currently supplies
16384 when no value is provided. A `max_tokens` stop is truncation, not objective
completion. This protocol field is not a global turn, tool or retry ceiling.

## Authentication and billing boundary

The implemented route is Console API/BYOK. Workspace selection identifies the
API billing context; it does not turn API requests into subscription-limit
traffic. Account eligibility and actual request access are separate from key
presence or catalogue visibility.

The 18 October 2026 authentication review established no OSIA-owned Console OAuth
client or approved claude.ai subscription-login route. It distinguished the
documented CLI-managed authentication from product-owned OAuth and recorded
that third-party product subscription access required provider approval. These
are the audit's qualified findings, not a newly verified entitlement statement.
The [original sources, unsent provider request and limitations](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-344-claude-authentication.md)
remain available for a future specifically authorized investigation.

## Historical corrections and evidence

The earlier WOR-343 work corrected five independent problems: retired process
callbacks/login waits (`c4d14d6f`), exact model/account freshness (`7f2d9707`),
Claude workspace headers (`07ce8b30`), observed logout state (`e1ed7eb9`) and
complete Claude model pagination (`6bda58fb`). The final combined harness then
reported 404 passes; it performed no authenticated provider inference. Its
[complete dated audit](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-343-provider-audit.md) preserves the full SHAs and failure
reproductions, rather than duplicating its superseded transport instructions.

WOR-344's subsequent native/compatibility harness reported 269 passes, including
same-model tool-result reinjection, cancellation and PC-side Mobile projection.
These are recorded historical results, not claims that every paid key, account,
frozen application or physical Mobile session was accepted.
