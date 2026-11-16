# Reasoning capabilities

Reasoning options are resolved for the exact provider and model. Product rules
are maintained in [AGENTS.md](../AGENTS.md) and the
[Work Drive rules index](https://drive.google.com/file/d/1uJqAH2Ch7g5Wc3DOav4FcEaGTEiIR10r/view).

## Resolve an option

[osia/reasoning.py](../osia/reasoning.py) owns three public entry points:

- `get_reasoning_capability(provider, model)` returns the model contract.
- `get_reasoning_options(provider, model)` returns the selectable options.
- `build_reasoning_request(provider, model, settings)` resolves the saved choice
  and outgoing provider fields.

`auto` leaves the reasoning parameter unspecified so the provider chooses its
default. The other UI IDs are `off`, `minimal`, `low`, `medium`, `high`, `xhigh`,
`max` and `ultra`; each is offered only when supported by that exact model.
ChatGPT Account rechecks an unsupported explicit choice against `model/list`
and returns an error if support cannot be verified or the choice remains
incompatible. DeepSeek rejects unsupported modes. Other API adapters can fall
back to `auto` for a legacy incompatible saved choice, with
`mapping_state=rejected`; the stored choice is migrated to the default when
the resolver owns the saved settings. This compatibility path is distinct
from validating a new explicit command selection.

## Shared metadata

[The PC-owned model inventory](../osia/mobile_model_catalogue.py) supplies the
Desktop picker and Mobile projection from the same discovered metadata.
[The ChatGPT Account provider](../osia/chatgpt_account/provider.py) uses Codex
`model/list` for the account-specific IDs, effort options and defaults. OpenAI
API discovery is a separate catalogue.

Catalogue visibility, configured credentials and supported option metadata are
distinct from actual request entitlement. [Provider discovery](providers.md)
explains availability, and [DeepSeek](deepseek.md) documents its exact five-mode
mapping.
