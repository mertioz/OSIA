# Grok / xAI API reference

Implementation reference checked on 16 November 2026. Grok / xAI (`xai`) and
GroqCloud (`groq`) have separate identities, credentials, endpoints and billing.
Shared inventory behavior is documented once in
[the provider reference](WOR-355-cloud-provider-audit.md).

## Implemented route

OSIA uses the configured xAI API key with the Responses transport. Exact model
contracts, supported modalities and effort options are defined by discovery and
the documented metadata in [osia/models.py](../osia/models.py) and
[osia/reasoning.py](../osia/reasoning.py), not by a second model list here.

[osia/responses_api.py](../osia/responses_api.py) handles streaming and completed
Responses, correlated function outputs and opaque encrypted context. OSIA's Chat
runtime executes tools and owns cancellation and ActionTruth. Context from one
provider/model is not forwarded as another route's opaque state.

Public `summary_text` is distinct from private reasoning. Acceptance of
`reasoning_text.delta` as public text is enabled only by the exact xAI contract's
`public_reasoning_text` metadata; the adapter does not generalize that exception
to other providers or unknown models. An incomplete stream remains incomplete.

## Account and historical evidence

The 20 October 2026 review found no established third-party Grok subscription
login or entitlement transfer for OSIA. It recorded API-key configuration and an
explanatory notice instead of consumer-login/token reuse. Provider-account
identity and API billing were distinguished. No request to xAI was sent.

That audit used synthetic HTTP with the real SDK and canonical temporary-file
tool/result reinjection. It had no configured xAI key and verified no live xAI
inference. The [original contract sources, authentication research and test
qualifications](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-356-grok-api-audit.md) remain dated evidence. The old Grok 4.7-only model
snapshot is not the current selectable catalogue or proof of account access.
