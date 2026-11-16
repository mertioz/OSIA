# Claude API connection

## Configure the route

The Desktop Claude API card uses a Console API key and an optional workspace
ID. Workspace selection identifies the API billing context; subscription
allowance is not used as an API credential.

[osia/claude_messages.py](../osia/claude_messages.py) uses native `/v1/messages`
by default. `anthropic_transport=compatibility` selects the OpenAI-compatible
route without changing credentials or billing. [osia/anthropic_api.py](../osia/anthropic_api.py)
constructs headers, and [key_chain](../osia/utils/key_chain.py) owns technical
key rotation.

## Native Messages

The adapter supports system/history context, SSE text, native tool proposals
and correlated results, images, usage/error metadata, signed/redacted blocks
and provider-returned public Thinking summaries. OSIA's canonical Chat runtime
executes tools; the transport adapter does not create an Agent SDK loop.

Messages requires a per-response `max_tokens`; the adapter supplies 16384 when
no value is provided. A `max_tokens` stop is reported as truncation, not objective
completion. This protocol field is not a global turn, tool or retry ceiling.

## Account boundaries

The implemented OSIA connection is Console API/BYOK. A Claude subscription or
the presence of a Console key does not establish request entitlement. OSIA does
not expose a consumer subscription-login transport or reuse another
application's stored tokens.

The PC owns provider configuration and execution; Mobile receives the sanitized
projection described in [provider discovery](providers.md). Model-specific
options use [reasoning capabilities](reasoning-capabilities.md). General runtime
and credential rules are maintained in [AGENTS.md](../AGENTS.md) and the
[Work Drive rules index](https://drive.google.com/file/d/1uJqAH2Ch7g5Wc3DOav4FcEaGTEiIR10r/view).
