# Provider discovery and transport

Product/runtime authority is maintained in [AGENTS.md](../AGENTS.md) and the
[Work Drive rules index](https://drive.google.com/file/d/1uJqAH2Ch7g5Wc3DOav4FcEaGTEiIR10r/view).
This guide identifies the executable sources for model availability and requests.

## One inventory

[osia/models.py](../osia/models.py) owns cloud discovery and documented fallback
metadata. A successful complete response replaces a provider's previous list,
including an empty response. Partial/corrupt pagination is not presented as a
complete inventory. Failed or stale discovery does not turn an unverified
static fallback into an available picker entry. `_DISCOVERY_TTL` declares the
cache duration.

Credential, workspace and endpoint changes invalidate discovery. Key rotation
uses [key_chain](../osia/utils/key_chain.py) for technical failures rather than
content errors. An existing explicit model selection is preserved. Catalogue
visibility and credential presence do not establish execution entitlement.

[osia/mobile_model_catalogue.py](../osia/mobile_model_catalogue.py) projects the
PC-owned identity, display name, source, availability, modalities and effort
options. Mobile does not maintain another authoritative provider/model list.

## Capability and transport locations

| Route | Implementation reference |
| --- | --- |
| OpenAI API exact contracts | [openai_model_contracts.py](../osia/openai_model_contracts.py), discovered IDs and capability intersection in models.py |
| ChatGPT Account | [chatgpt_account/provider.py](../osia/chatgpt_account/provider.py), managed authentication and Codex catalogue, independent of API-key discovery |
| Claude API | [Claude](claude.md), native Messages and optional compatibility adapter |
| Gemini / Groq / Mistral | Discovery in models.py, shared [model capabilities](../osia/model_capabilities.py) and [reasoning options](reasoning-capabilities.md) |
| DeepSeek | [Canonical model and images](deepseek.md) |
| xAI / Grok | [Grok](grok.md), separate from GroqCloud |
| Ollama | Local discovery in models.py |

## Requests and provider state

[osia/responses_api.py](../osia/responses_api.py) translates supported Responses
requests and events. Tools come from OSIA's registry; their execution and
factual result reinjection remain in the selected canonical route. Opaque
provider context belongs to its exact route. A route change uses factual textual
history rather than another model's opaque state. Interrupted streams and typed
errors are not success. Public provider summaries and private reasoning remain
distinct.

ChatGPT Account uses the context capacity advertised/configured by its Codex
runtime and native compaction. [app_server_client.py](../osia/chatgpt_account/app_server_client.py)
owns that protocol and runtime compatibility; a generic API model entry does
not set the account route's context window. Effective options and capacity
depend on the selected model, not on a second documentation catalogue.
