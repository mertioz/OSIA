# WOR-209 reasoning capability matrix

Audited 2026-07-30. `auto` is the canonical `Défaut` option and deliberately
omits a provider parameter. UI aliases are: `off` = Désactivé, `minimal` =
Minimal, `low` = Faible, `medium` = Moyen, `high` = Élevé, `xhigh` = Très
élevé, `max` = Max, and `ultra` = Ultra. A dash means that the selector is
hidden because no exact model-specific mapping is authoritative.

The executable source of truth is `osia.reasoning.MODEL_REASONING_CAPABILITIES`
plus ChatGPT App Server `model/list` metadata. PC UI, Mobile catalogue and the
provider adapter consume that source. Mobile must not infer options locally.

## Matrix

| Provider / model | Canonical UI option IDs | Native provider values | Provider parameter | Evidence |
|---|---|---|---|---|
| anthropic / claude-opus-4-8 | auto, off, low, medium, high, xhigh, max | omitted; disabled; adaptive + low/medium/high/xhigh/max | `thinking.type`; `output_config.effort` | [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort), [adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) |
| anthropic / claude-opus-4-7 | auto, off, low, medium, high, xhigh, max | omitted; disabled; adaptive + low/medium/high/xhigh/max | `thinking.type`; `output_config.effort` | Claude effort/adaptive-thinking docs |
| anthropic / claude-opus-4-6 | auto, off, low, medium, high, max | omitted; disabled; adaptive + low/medium/high/max | `thinking.type`; `output_config.effort` | Claude effort/adaptive-thinking docs |
| anthropic / claude-sonnet-4-6 | auto, off, low, medium, high, max | omitted; disabled; adaptive + low/medium/high/max | `thinking.type`; `output_config.effort` | Claude effort/adaptive-thinking docs |
| anthropic / claude-haiku-4-5-20251001 | — | — | — | Not in Claude's model-specific effort support table |
| openai / gpt-5.5 | auto, off, low, medium, high, xhigh | omitted; none/low/medium/high/xhigh | `reasoning_effort` | [GPT-5.5 model page](https://developers.openai.com/api/docs/models/gpt-5.5) |
| openai / gpt-5.4 | auto, off, low, medium, high, xhigh | omitted; none/low/medium/high/xhigh | `reasoning_effort` | [GPT-5.4 model page](https://developers.openai.com/api/docs/models/gpt-5.4) |
| openai / gpt-5.4-mini | auto, off, low, medium, high, xhigh | omitted; none/low/medium/high/xhigh | `reasoning_effort` | [GPT-5.4 mini model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini) |
| openai / gpt-4.1 | — | — | — | [GPT-4.1 is non-reasoning](https://developers.openai.com/api/docs/models/gpt-4.1) |
| openai / gpt-4.1-mini | — | — | — | Official OpenAI model page: non-reasoning family |
| openai / gpt-4.1-nano | — | — | — | Official OpenAI model page: non-reasoning family |
| deepseek / deepseek-v4-pro | auto, off, high, max | omitted; disabled; enabled + high/max | `thinking.type`; `reasoning_effort` | [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode) |
| deepseek / deepseek-v4-flash | auto, off, high, max | omitted; disabled; enabled + high/max | `thinking.type`; `reasoning_effort` | DeepSeek thinking-mode docs |
| google / gemini-3.5-flash | auto, minimal, low, medium, high | omitted; minimal/low/medium/high | `reasoning_effort` (OpenAI compatibility) | [Gemini thinking](https://ai.google.dev/gemini-api/docs/thinking), [OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai) |
| google / gemini-3.1-pro-preview | auto, low, medium, high | omitted; low/medium/high | `reasoning_effort` (OpenAI compatibility) | Gemini thinking/OpenAI-compatibility docs |
| google / gemini-3-flash-preview | auto, minimal, low, medium, high | omitted; minimal/low/medium/high | `reasoning_effort` (OpenAI compatibility) | Gemini thinking/OpenAI-compatibility docs |
| google / gemini-3.1-flash-lite | auto, minimal, low, medium, high | omitted; minimal/low/medium/high | `reasoning_effort` (OpenAI compatibility) | Gemini thinking/OpenAI-compatibility docs |
| google / gemini-2.5-flash | auto, off, minimal, low, medium, high | omitted; none/minimal/low/medium/high (compat maps to native budgets) | `reasoning_effort` (OpenAI compatibility) | Gemini OpenAI-compatibility mapping table |
| google / gemini-2.5-flash-lite | auto, off, minimal, low, medium, high | omitted; none/minimal/low/medium/high (compat maps to native budgets) | `reasoning_effort` (OpenAI compatibility) | Gemini OpenAI-compatibility mapping table |
| xai / grok-3 | — | — | — | [xAI retirement/redirect notice](https://docs.x.ai/developers/migration/may-15-retirement) |
| xai / grok-3-mini | — | — | — | Runtime/management metadata reports reasoning but current official model docs publish no exact effort enum |
| groq / llama-3.3-70b-versatile | — | — | — | [Groq reasoning model table](https://console.groq.com/docs/reasoning) |
| groq / llama-3.1-8b-instant | — | — | — | Groq reasoning model table |
| groq / qwen/qwen3-32b | — | — | — | Deprecated; no current exact reasoning-effort contract |
| groq / openai/gpt-oss-120b | auto, low, medium, high | omitted; low/medium/high | `reasoning_effort` | Groq reasoning model table |
| groq / openai/gpt-oss-20b | auto, low, medium, high | omitted; low/medium/high | `reasoning_effort` | Groq reasoning model table |
| groq / moonshotai/kimi-k2-instruct | — | — | — | Groq reasoning model table |
| groq / qwen/qwen3.6-27b (runtime-discovered) | auto, off | omitted; none | `reasoning_effort` | [Qwen 3.6 model page](https://console.groq.com/docs/model/qwen/qwen3.6-27b), Groq reasoning table |
| groq / allam-2-7b (runtime-discovered) | — | — | — | Groq runtime `/models`; absent from reasoning model table |
| mistral / mistral-large-2512 | — | — | — | [Mistral reasoning models](https://docs.mistral.ai/studio-api/conversations/reasoning) |
| mistral / mistral-medium-3-5 | auto, off, high | omitted; none/high | `reasoning_effort` | Mistral reasoning model page |
| mistral / mistral-small-2603 | — | — | — | Not listed for adjustable reasoning under this exact ID |
| mistral / codestral-2508 | — | — | — | Not listed for adjustable reasoning |
| chatgpt_account / gpt-5.6-sol | auto, low, medium, high, xhigh, max, ultra | omitted; low/medium/high/xhigh/max/ultra | App Server `effort` | Local ChatGPT App Server `model/list`; default `low` |
| chatgpt_account / gpt-5.6-terra | auto, low, medium, high, xhigh, max, ultra | omitted; low/medium/high/xhigh/max/ultra | App Server `effort` | Local ChatGPT App Server `model/list`; default `medium` |
| chatgpt_account / gpt-5.6-luna | auto, low, medium, high, xhigh, max | omitted; low/medium/high/xhigh/max | App Server `effort` | Local ChatGPT App Server `model/list`; default `medium` |
| chatgpt_account / gpt-5.5 | auto, low, medium, high, xhigh | omitted; low/medium/high/xhigh | App Server `effort` | Local ChatGPT App Server `model/list`; default `medium` |
| chatgpt_account / gpt-5.4 | auto, low, medium, high, xhigh | omitted; low/medium/high/xhigh | App Server `effort` | Local ChatGPT App Server `model/list`; default `medium` |
| chatgpt_account / gpt-5.4-mini | auto, low, medium, high, xhigh | omitted; low/medium/high/xhigh | App Server `effort` | Local ChatGPT App Server `model/list`; default `medium` |
| chatgpt_account / gpt-5.3-codex-spark | auto, low, medium, high, xhigh | omitted; low/medium/high/xhigh | App Server `effort` | Local ChatGPT App Server `model/list`; default `high` |
| ollama / gemma4:latest | — | — | — | Local `ollama show` reports `thinking`, but no exact model-specific effort enum; selector remains hidden |

## Configured runtime snapshot

The audited PC had API keys available for DeepSeek and Groq, a connected
ChatGPT Account, and local `gemma4:latest`. The effective configured inventory
therefore comprised both DeepSeek rows, the six hardcoded Groq rows plus the two
runtime-discovered Groq rows, all seven App Server rows, and the Ollama row.
Provider configuration and credentials were not changed.
