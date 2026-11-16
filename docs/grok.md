# Grok / xAI API

Grok / xAI (`xai`) and GroqCloud (`groq`) have separate identities, credentials,
endpoints and billing. Shared catalogue behavior is described in
[provider discovery](providers.md).

## Configure the route

OSIA uses the configured xAI API key with the Responses transport. Exact model
contracts, supported modalities and effort options are resolved by discovery and
the metadata in [osia/models.py](../osia/models.py) and
[osia/reasoning.py](../osia/reasoning.py).

The implemented connection is API-key based, rather than consumer Grok
subscription login. Credential presence and model visibility do not establish
request entitlement.

## Responses and reasoning state

[osia/responses_api.py](../osia/responses_api.py) handles streaming/completed
Responses, correlated function outputs and opaque encrypted context. OSIA's
canonical Chat runtime executes tools and owns cancellation and ActionTruth.
Opaque state from one provider/model is not forwarded to another route.

Public `summary_text` is distinct from private reasoning. Acceptance of
`reasoning_text.delta` as public text is enabled only by the exact xAI contract's
`public_reasoning_text` metadata; that exception is not generalized to other
providers or unknown models. An incomplete stream remains incomplete.

Model-specific configuration uses the shared
[reasoning capabilities](reasoning-capabilities.md). General product and runtime
rules are maintained in [AGENTS.md](../AGENTS.md) and the
[Work Drive rules index](https://drive.google.com/file/d/1uJqAH2Ch7g5Wc3DOav4FcEaGTEiIR10r/view).
