# Tool contracts and chat commands

The current registry and dispatcher define available tools. This guide maps their implementation and typed data; architectural authority remains in [AGENTS.md](../AGENTS.md).

## Contract map

| Concern | Implementation |
| --- | --- |
| Static ToolSpec schemas, bindings and technical availability | [tool_contracts.py](../osia/tool_contracts.py) |
| Static dispatchers and canonical agent loop | [agent.py](../osia/agent.py) |
| Provider-visible projection | [capability_federation.py](../osia/capability_federation.py) |
| Dynamic connector registration | [mcp_client/registry.py](../osia/mcp_client/registry.py) |
| Attempt ownership and observed dispatch/outcomes | [tool_attempts.py](../osia/tool_attempts.py) |
| Explicit slash directives and conversation settings | [slash_commands.py](../osia/slash_commands.py) |
| Provider-specific adaptation | [provider guides](providers.md) |

ToolSpec binds a name and dispatcher to its input_schema, preconditions, effect/idempotency metadata, local operation timeout/retry contract, result policy and observability. get_static_registry and get_tool_spec expose these definitions; validate_tool_arguments validates the actual call against the canonical schema.

The same schemas feed the generated textual catalogue and supported native declarations. Dynamic MCP entries use the existing registry/registration lifecycle. Availability depends on actual platform/provider/connector support and explicit user settings, not a second semantic task classifier. No fixed documentation table substitutes for the live registry.

## Call and result flow

    provider native/textual call → parse original payload → schema validation
    → canonical dispatch → typed execution record and factual result reinjection

The parser supports the current native and textual formats, including DeepSeek DSML and Groq XML/text. Internal reasoning/protocol markup is stripped only after tool parsing/execution. Schema rejections identify the technical issue and current contract so the selected model can choose its next call.

ToolExecutionRecord distinguishes result, is_error, typed outcome, attempts, dispatched, timed_out, contract, attempt identity and reason_code. Process tools additionally carry their typed process facts. An error-looking string in stdout is data; a spawned process or completed observation is not automatically proof of the whole requested objective.

ToolAttempt/ActionTruth attach exact ownership and observed outcomes to dispatched work. Unknown effects remain unknown. AGENTS.md defines fresh model requests, cancellation, stale callbacks and critical system-path protection; these rules are not duplicated in a separate per-tool matrix.

## Browser and desktop tools

Managed browser tools work with tab/document identity, structured page state, element references and form operations. A click on a page selector is not an OS-pointer movement. Fresh references and typed results bind the requested operation to the observed page.

Native desktop inspection, capture, UIA, key input and physical pointer tools have separate contracts. A screenshot is observation, not proof of an action. /mousecontrol applies to native pointer capabilities while preserving keyboard, vision and structured web work. The [Thinking/activity guide](thinking.md) explains the independent display indicators.

[desktop_scene.py](../osia/desktop_scene.py), [computer_use_v4_integration.py](../osia/computer_use_v4_integration.py) and [tools/desktop_control.py](../osia/tools/desktop_control.py) own the current target/surface and native boundaries. [tools/browser.py](../osia/tools/browser.py) and [browser_forms.py](../osia/tools/browser_forms.py) own managed web interaction.

## Slash commands

Typing / in the chat composer opens the command palette. A command selection is a local action or typed next-message directive, not an unstructured prompt prefix. // escapes a literal leading slash. The catalogue is [assets/slash_commands/catalog.v1.json](../assets/slash_commands/catalog.v1.json); Desktop and Mobile share its wire identities.

| Command | Effect |
| --- | --- |
| /web | Requests a relevant search attempt for the next turn; absent/unhelpful results do not prohibit a normal answer |
| /model | Selects an exact provider/model and supported reasoning option for the next turn without rewriting global selection |
| /status | Shows the model, conversation settings and connector status through the local information path |
| /help | Lists the commands available on the current surface |
| /mousecontrol | Toggles native physical pointer control for the current conversation; keyboard, vision and structured web tools remain available |
| /compact | Requests compaction through the existing context path; ChatGPT Account uses Codex's native thread compaction |
| /skill | Selects a saved skill for the next message |
| /skillcreator | Carries an explicit skill-creation request to the existing creation tool |

Modifiers require their supported message/attachment body. Model/reasoning choices are validated against the exact current model metadata. Mouse-control preference belongs to its canonical conversation and is not changed during accepted work using that preference.

The palette/composer implementation is [slash_palette.py](../osia/gui/slash_palette.py) and [main_popup.py](../osia/gui/main_popup.py). PC handlers in [main.py](../osia/main.py) and [server.py](../osia/server/server.py) use the same typed definitions and canonical conversation state.
