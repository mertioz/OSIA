# Provider discovery and transport reference

Implementation reference checked on 16 November 2026. Product/runtime authority
and verification workflow are defined in [AGENTS.md](../AGENTS.md); this page
documents the implementation locations and provider-specific boundaries.

## One inventory

[osia/models.py](../osia/models.py) owns cloud discovery and documented fallback
metadata. A successful complete response replaces a provider's previous list,
including a successful empty response. A partial/corrupt pagination response is
not presented as a complete inventory. Failed/stale discovery does not turn an
unverified static fallback into an available picker entry. The current cache
duration is declared by `_DISCOVERY_TTL` in that module.

Credential, workspace and endpoint changes invalidate discovery. Key rotation
uses [key_chain](../osia/utils/key_chain.py) for technical failures rather than
content errors. An existing explicit model selection is preserved; catalogue
presence and credential presence do not establish execution entitlement.

[osia/mobile_model_catalogue.py](../osia/mobile_model_catalogue.py) projects the
PC-owned model identity, display name, source, availability, modalities and
effort options. Mobile does not maintain another authoritative catalogue.

## Capability and transport locations

| Route | Implementation reference |
| --- | --- |
| OpenAI API exact contracts | [openai_model_contracts.py](../osia/openai_model_contracts.py), discovered IDs and capability intersection in models.py |
| ChatGPT Account | [chatgpt_account/provider.py](../osia/chatgpt_account/provider.py), managed App Server catalogue and authentication, independent of the API-key catalogue |
| Claude API | [Claude reference](WOR-344-claude-authentication.md), native Messages and optional compatibility adapter |
| Gemini / Groq / Mistral | Provider discovery in models.py, shared [model capabilities](../osia/model_capabilities.py) and [reasoning options](reasoning-capabilities.md) |
| DeepSeek | [Canonical model and image reference](WOR-298-deepseek-flash.md) |
| xAI / Grok | [Grok reference](WOR-356-grok-api-audit.md), separate from GroqCloud |
| Ollama | Local discovery in models.py; not a cloud account route |

[osia/responses_api.py](../osia/responses_api.py) translates supported Responses
requests/events. Tools are advertised from OSIA's registry; their execution and
factual result reinjection stay in the selected canonical route. Opaque
provider context is retained for its exact route, while a route change uses
factual textual history. Interrupted streams and typed errors are not success.
Public provider summaries and private reasoning remain distinct.

## Historical evidence

The 20 October 2026 audit recorded free DeepSeek, Groq and managed ChatGPT model
discovery. Other provider requests used real SDKs with synthetic HTTP results;
no paid inference was performed. Its old model-addition checklist is replaced
by the executable inventory above. [The dated matrix, exact discovered IDs and
test limits](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-355-cloud-provider-audit.md) remain available without becoming another current
catalogue or promise of account access.

The combined WOR-354/355/356 delivery recorded 271 Python passes, a subsequent
35-case focused pass, 512 Flutter passes and one skipped opt-in relay diagnostic.
Its Android fixture built but wireless ADB disconnected during installation;
no physical acceptance was claimed. The unique Mobile Thinking-touch evidence
is [the original Mobile report](https://github.com/mertioz/osia-mobile/blob/ff96862917efd40fa8009f59a8840ca6cbdb749e/docs/WOR-354-thinking-touch.md).
The [complete delivery and source-commit table](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-354-356-delivery.md) preserve that
history; its old “next release” instructions are not current gates.
