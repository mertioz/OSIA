# Tool contracts and validation sources

Current reference, consolidated on 16 November 2026. The former generated
matrix was a 30 July snapshot with 143 entries, including one live Linear
connector inventory. It is not the current runtime catalogue.

## Executable contract map

| Concern | Source |
| --- | --- |
| Static schemas, dispatcher bindings, typed results and runtime availability | [osia/tool_contracts.py](../osia/tool_contracts.py) |
| Static dispatcher implementations | [osia/agent.py](../osia/agent.py) |
| Provider-visible capability projection | [osia/capability_federation.py](../osia/capability_federation.py) |
| Dynamic connector registry | [osia/mcp_client/registry.py](../osia/mcp_client/registry.py) |
| Actual dispatch/outcome records | [osia/tool_attempts.py](../osia/tool_attempts.py) |
| Slash command schemas and explicit conversation settings | [osia/slash_commands.py](../osia/slash_commands.py) |

The selected provider/model's actual runtime inventory and the exact call's
typed receipt supply the operational facts. Architectural rules are maintained
in [AGENTS.md](../AGENTS.md); they are not repeated per tool in another table.

## Verification references

- `tests/test_tool_reliability_regression.py`: registry/schema/dispatcher
  consistency and provider contract paths.
- `tests/test_wor302_provider_tool_lifecycle.py`: supported parser/native tool
  lifecycles.
- `tests/test_wor305_outcome_contract.py` and
  `tests/test_wor269_durable_action_truth.py`: factual effects and outcomes.
- `tests/test_wor245_slash_commands.py` and
  `tests/test_wor245_slash_palette_ui.py`: explicit command behavior.
- `tests/test_wor308_mobile_parity.py`: canonical PC/Mobile projection.

A named suite is a verification entry point, not a claim that every external
action or provider is live-tested. Results, baselines, credentials/physical-test
limits and the exact delivered commit belong to the dated validation report.

## Historical matrix

The original matrix recorded 43 VERIFIED_REAL, 59 VERIFIED_HARNESS,
22 CONTRACT_ONLY and 19 LEGACY_ALIAS entries. It included unique evidence for
local shell exit codes, Windows app-resolution decoding and document-artifact
follow-ups, plus explicit exclusions for real Gmail mutations. The old table
mixed factual per-attempt outcomes with superseded Autonomous, permission and
retry/turn restrictions. Those old policy interpretations have been removed;
the source still owns typed attempt states, and AGENTS.md distinguishes an
attempt's outcome from objective completion or a fresh model-requested call.

The [complete dated matrix](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/tool-e2e-matrix.md) retains its original schemas, proof
qualifications, fixes and status counts in Git history, without asserting that
they are today's inventory or validation status.
