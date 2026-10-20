# Cloud provider audit — 2026-10-20

Scope: WOR-355, primary Desktop and Mobile repositories. No paid inference was
authorized or performed. Published availability, account discovery, synthetic
transport tests and physical acceptance are different evidence levels.

## Provider findings and official sources

| Provider | Official contract and models | OSIA implementation / limits |
|---|---|---|
| OpenAI API | [Models](https://developers.openai.com/api/docs/models), [list](https://developers.openai.com/api/reference/resources/models/methods/list), [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [Astra](https://developers.openai.com/api/docs/models/gpt-6-astra), [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | Bearer API key. Add documented `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna` and `gpt-5.6-sol/terra/luna`. Preserve existing GPT-5.5, GPT-5.4 and GPT-4.1 entries when discovery lists them. GPT-6.1 tools require Responses; GPT-6 Sol/Luna chat-completions tools require reasoning none. Use Responses for all new entries. Text/image input, text output, streaming/functions, 1,050,000 context. Astra/6.1 efforts low/medium/high/xhigh/max; Sol/Luna and 5.6 additionally none. |
| ChatGPT Account | [App Server model/list](https://learn.chatgpt.com/docs/app-server#list-models-modellist) | Separate managed authentication and catalogue, unchanged native runtime. Account-specific returned IDs, modalities and effort options remain authoritative; no API-key catalogue substitutes for it. RPC pagination retained. Discovery can reflect a bundled/cache catalogue rather than all current public API releases. |
| Anthropic | [Models](https://platform.claude.com/docs/en/models/overview), [Models API](https://platform.claude.com/docs/en/api/models/list) | Native Messages, x-api-key, anthropic-version, optional workspace header. Current documented IDs: `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`; old valid IDs remain eligible when listed. Text/image, text output, tools/streaming, 1M context. Discovery supplies image_input, thinking types, effort options and input limit. Never offer disabled thinking if provider metadata excludes it. Keep signed/redacted native blocks and public summarized thinking behavior. |
| Gemini | [Models](https://ai.google.dev/gemini-api/docs/models), [list](https://ai.google.dev/api/models), [release notes](https://ai.google.dev/gemini-api/docs/changelog), [Argon](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon), [3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash) | Add `gemini-4-argon` (GA October 13), `gemini-3.8-flash` (September). Discovery uses x-goog-api-key and nextPageToken, filters generateContent and excludes media-only services. Inference retains official OpenAI-compatible endpoint. Low/medium/high thinking; minimal is invalid on 3.8. Image input, streaming/functions, 1,048,576 context. Providers also accept audio/video/PDF natively; those modalities are not newly enabled in OSIA by this ticket. |
| DeepSeek | [Models API](https://api-docs.deepseek.com/api/list-models/), [thinking](https://api-docs.deepseek.com/guides/thinking_mode), [API overview](https://api-docs.deepseek.com/) | Enable authenticated discovery. Sole selectable ID remains `deepseek-flash`; preserve off/low/high/max, vision, native streaming and tool/context handling already covered by WOR-298/303. Retired aliases and invented future IDs cannot reappear. |
| GroqCloud | [Models](https://console.groq.com/docs/models), [deprecations](https://console.groq.com/docs/deprecations), [reasoning](https://console.groq.com/docs/reasoning) | Separate from Grok. Add `qwen/qwen3.8-27b`: text/image, functions/streaming, 131,072 context, none/low/medium/high. GPT-OSS 20B/120B retain low/medium/high and 131,072 context. Ordinary-tier Llama 8B/70B retired August 16; enterprise exceptions remain discoverable. Qwen 3.6 ordinary tiers retired September 14. Compound retired September 21. STT/TTS/safety/Compound entries are excluded from Chat. Enterprise MiniMax/Muse remain discoverable without assuming account access. No private reasoning is promoted into public Thinking. |
| Mistral | [Models](https://docs.mistral.ai/models), [Models API](https://docs.mistral.ai/api/endpoint/models), [Large 4](https://docs.mistral.ai/models/mistral-large-4-0) | Enable Bearer-key discovery; completion_chat distinguishes chat from OCR/embedding/FIM. Use returned vision/function_calling/max_context_length. Add public-preview `mistral-large-4` (October 6), multimodal, tools/streaming, 1M context. Retain usable Large 3/Medium 3.5/Small 4 entries. Unknown efforts use provider default rather than fabricated settings. |
| xAI | Already present as a legacy entry; official Responses integration and account-login research belong to WOR-356. | Not GroqCloud; same canonical execution authority. |
| Ollama | Existing local provider, outside cloud refresh. | Local discovery/profile behavior unchanged. |

All links were retrieved on 2026-10-20. Models above were documented by this
date; no release after that date is intentionally added. Rate limits, regional
access, credits and special model eligibility remain account/provider-specific.
No current price is used as an execution or routing decision.

## Discovery and availability

Desktop owns one in-memory inventory. Successful complete discovery replaces
that provider's list, including a successful empty list. Errors do not resurrect
static entries. Partial/corrupt pagination is rejected. Historical static
records remain explicit unverified fallbacks, disabled in the PC/Mobile picker.
Snapshots expire after 15 minutes; Desktop refreshes every 10 minutes. Existing
selected configuration is preserved; expiration does not terminate a running
turn. Credential/workspace/fallback changes invalidate old discoveries and
in-flight callbacks. Only technical errors rotate keys through the shared key
chain; content errors such as 404 do not rotate.

Some list endpoints do not report endpoint/tool capabilities. OpenAI and xAI
therefore intersect returned IDs with documented exact contracts; unrecognized
IDs cannot silently become usable chat models. This is a capability limitation,
not evidence that an unknown model is unavailable at the provider. Future
Responses-only contracts may require another documented metadata entry. Claude,
Mistral and Google preserve the metadata their endpoints actually expose.

Mobile receives exact provider/model identity, availability and source fields,
and renders the PC display name. It maintains no new independent model list.
Secrets and raw provider responses are never serialized in the catalogue.

## Runtime and validation

ResponsesClient translates requests/stream items only. It advertises OSIA tools;
OSIAAgent dispatches them, correlates receipts, then reinjects function outputs
and opaque reasoning into the original route. Provider/model switches use
textual factual history instead of another route's ciphertext. EOF/errors are
not completion. Only summary_text events enter public Thinking; raw reasoning
deltas are ignored. ChatGPT Account and native Claude retain their transports.

Real, free discovery on this machine: DeepSeek returned `deepseek-flash`; Groq
returned `allam-2-7b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`,
`openai/gpt-oss-120b`. Managed ChatGPT returned GPT-6 Astra/Sol/Luna,
GPT-5.6 Sol/Terra/Luna and Daybreak Blue, with their own effort metadata.
GPT-6.1 Sol is documented publicly but was absent from this account catalogue;
OSIA does not inject it into ChatGPT Account.

No API keys were configured for OpenAI API, Claude, Gemini, Mistral or xAI.
Their wire contracts are tested using the real OpenAI SDK with synthetic HTTP
responses. Canonical tests actually read a temporary local file through OSIA and
check native result reinjection in both streaming and nonstreaming modes.
This establishes implementation behavior, not live inference success.

Validation commands/results and synchronized commit SHAs are recorded in the
Linear delivery comment. Historical non-DeepSeek regression tests now preserve
their established contracts while allowing WOR-355's intentional additions.

## Before WOR-357

Configure missing provider API keys on Desktop and, with explicit paid-call
authorization, validate a text/image/tool/stream/Stop turn for each provider.
Revalidate Thinking on a new private Android test build and on iOS. Preserve
pairing, credentials, conversations and existing updater ordering; no version,
public build, store submission or Linux packaging is part of this delivery.
