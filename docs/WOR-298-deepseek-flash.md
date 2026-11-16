# DeepSeek model and image transport

Current implementation reference, checked on 16 November 2026. General Chat,
tool and language ownership is defined in [AGENTS.md](../AGENTS.md).

## Model identity and reasoning

[osia/deepseek.py](../osia/deepseek.py) owns the canonical selectable ID
`deepseek-flash` and compatibility migration. Retired IDs are historical input,
not additional selectable models. The PC inventory and Mobile projection share
this identity; the provider's wider public catalogue is a separate question.

[osia/reasoning.py](../osia/reasoning.py) supplies these exact mappings:

| UI ID | `thinking` | `reasoning_effort` |
| --- | --- | --- |
| `auto` | omitted | omitted |
| `off` | `{"type":"disabled"}` | omitted |
| `low` | `{"type":"enabled"}` | `low` |
| `high` | `{"type":"enabled"}` | `high` |
| `max` | `{"type":"enabled"}` | `max` |

The numeric encoder metadata is not an API/UI control. Other effort labels are
not silently approximated. General option resolution is described in
[reasoning capabilities](reasoning-capabilities.md).

## Images and compatibility

PC attachment staging and Mobile upload handling create the same canonical
image artifacts. The shared path is `OSIAAgent._wire_messages` →
`build_context` → `adapt_messages_for_provider` → the OpenAI-compatible image
adapter. [osia/image_inputs.py](../osia/image_inputs.py) owns MIME, geometry,
byte-envelope and provider limits; this page does not duplicate their constants.
Unsupported input is not silently converted into a text-only request.

Compatibility migration at configuration/model-selection boundaries preserves
the provider identity and other providers' selections. Retired Autonomous/V2
or hierarchy workflows described by the original implementation report are not
current product instructions.

## Historical validation

The 11 September 2026 audit recorded five real HTTP cases: text-only, PC image,
Mobile image, non-thinking image and low-reasoning image. All returned HTTP 200
with response model `deepseek-flash`; pixel checks established actual image
delivery. The retained run used 1,468 tokens. The non-DeepSeek comparison fixture
hash was `28dd92d417905c00a969c41c8581cacc859fa6bc2be7bf90cec09f4c9217f1b3`.

The [complete original report](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-298-deepseek-flash.md), including baseline failures,
changed files and test qualifications, remains historical evidence. It does not
establish today's live account access or reinstate the old V2 instructions.
