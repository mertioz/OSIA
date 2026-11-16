# DeepSeek model and image transport

## Model identity

[osia/deepseek.py](../osia/deepseek.py) owns the canonical selectable ID
`deepseek-flash` and compatibility migration. The PC inventory and Mobile
projection share that identity. Compatibility input does not create another
selectable model or change another provider's selection.

## Reasoning

[osia/reasoning.py](../osia/reasoning.py) supplies these exact mappings:

| UI ID | `thinking` | `reasoning_effort` |
| --- | --- | --- |
| `auto` | omitted | omitted |
| `off` | `{"type":"disabled"}` | omitted |
| `low` | `{"type":"enabled"}` | `low` |
| `high` | `{"type":"enabled"}` | `high` |
| `max` | `{"type":"enabled"}` | `max` |

Numeric encoder metadata is not an API/UI control. Option lookup and unsupported
choices use the shared [reasoning capability path](reasoning-capabilities.md).

## Images

PC attachment staging and Mobile upload handling create the same canonical
image artifacts. The request path is `OSIAAgent._wire_messages` →
`build_context` → `adapt_messages_for_provider` → the OpenAI-compatible image
adapter.

[osia/image_inputs.py](../osia/image_inputs.py) owns the accepted MIME formats,
geometry, byte envelope and provider limits. Unsupported input is not silently
converted into a text-only request. [Provider discovery](providers.md) supplies
availability; product/runtime authority is maintained in
[AGENTS.md](../AGENTS.md).
