# Reasoning capabilities

Implementation reference checked on 16 November 2026. The supported options are
computed for an exact provider/model, rather than maintained in a second static
documentation matrix.

## Sources of truth

- [osia/reasoning.py](../osia/reasoning.py): `get_reasoning_capability`,
  `get_reasoning_options` and `build_reasoning_request` resolve the capability,
  saved choice and outgoing provider fields.
- [PC-owned model inventory](../osia/mobile_model_catalogue.py): the Desktop
  picker and Mobile projection use the same discovered metadata.
- [ChatGPT Account provider](../osia/chatgpt_account/provider.py): Codex
  `model/list` supplies the account-specific IDs, effort options and defaults.
  Its catalogue is distinct from OpenAI API discovery.

`auto` leaves the provider's reasoning parameter unspecified. Other UI IDs
(`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`) are offered
only when supported by that exact model. A catalogue entry or a configured key
is not proof of inference entitlement.

DeepSeek's current selectable ID and five-mode mapping are documented once in
[the DeepSeek reference](WOR-298-deepseek-flash.md). Discovery and transport
ownership are documented in [the provider reference](WOR-355-cloud-provider-audit.md).

## Verification

The relevant contracts are exercised by `tests/test_wor209_reasoning.py`,
`tests/test_wor298_deepseek_flash.py`, `tests/test_wor245_slash_commands.py` and
the provider-specific transport tests. A new test run has its own recorded
result; this document does not declare a permanent all-provider green state.

## Historical evidence

The 30 July 2026 matrix contained retired DeepSeek IDs and an account inventory
from that date. It has been removed from the current reference. Its complete
[dated matrix and observations](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/reasoning-capabilities.md) remain in Git history.
