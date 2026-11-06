import json
from pathlib import Path

import pytest

from osia.slash_commands import (
    CapabilityRequirements,
    ConnectorHint,
    ConversationRef,
    ModelOverride,
    SlashDirectiveError,
    SlashTurnDirectivesV1,
    consume_selected_alias,
    consume_palette_prefix,
    escaped_literal_text,
    connector_availability,
    load_catalog,
    informational_payload,
    safe_availability_overlay,
    slash_palette_query,
    tool_is_current_web_evidence,
    apply_to_agent,
    validate_live,
)


def test_static_catalog_is_bilingual_bounded_and_byte_identical():
    entries = load_catalog()
    assert [entry["canonical_alias"] for entry in entries] == [
        "web", "model", "reasoning", "conversation", "tools", "status", "help",
        "special", "skill", "skillcreator",
    ]
    assert all(entry["name"]["en"] and entry["name"]["fr"] for entry in entries)
    pc = Path("assets/slash_commands/catalog.v1.json").read_bytes()
    mobile = Path("../osia-mobile/assets/slash_commands/catalog.v1.json").read_bytes()
    assert pc == mobile


def test_special_is_conversation_bound_and_rejects_visual_calls(tmp_path, monkeypatch):
    from osia import agent, conversations
    from osia.chatgpt_account.app_server_client import _canonical_native_tools
    from osia.computer_use_v4_integration import (
        computer_use_v4_test_scope,
        computer_use_v4_tools_available,
        computer_use_v4_turn_scope,
    )
    from osia.slash_commands import chat_command_scope, validate_special_image_input
    from osia.tool_contracts import effective_static_tool_specs

    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path)
    first = conversations.create_conversation()["id"]
    second = conversations.create_conversation()["id"]
    assert conversations.set_conversation_special_mode(first)["special_mode"] is True
    assert conversations.conversation_special_mode(first)
    assert not conversations.conversation_special_mode(second)
    with pytest.raises(SlashDirectiveError, match="restore vision"):
        validate_special_image_input(first, has_image=True)
    validate_special_image_input(second, has_image=True)

    assert conversations.claim_background_execution(first, "active-turn")
    with pytest.raises(ValueError, match="conversation_busy"):
        conversations.set_conversation_special_mode(first, False)
    assert conversations.release_background_execution(first, "active-turn")

    def unexpected_capture():
        pytest.fail("Special mode dispatched a desktop capture")

    monkeypatch.setitem(agent.TOOL_MAP, "take_screenshot", unexpected_capture)
    with (
        chat_command_scope(first, None),
        computer_use_v4_test_scope(vision_capable=True),
        computer_use_v4_turn_scope("chatgpt_account", "gpt-6.1-sol", conversation_id=first, turn_id="t1"),
    ):
        assert not computer_use_v4_tools_available()
        tools = {spec.name for spec in effective_static_tool_specs()}
        native_tools = {spec["name"] for spec in _canonical_native_tools()}
        assert not any(name.startswith("computer_") for name in tools | native_tools)
        assert not {"take_screenshot", "save_screenshot", "desktop_scroll"} & tools
        assert {"desktop_type", "desktop_press", "inspect_desktop_surfaces", "get_browser_state"} <= tools
        result = agent._execute_tool_with_policy("take_screenshot", {})
        assert not result.dispatched
        assert result.reason_code == "chat_command_capability_unavailable"
        assert "The user disabled vision and mouse input" in agent.get_system_prompt("en")

    with chat_command_scope(second, None):
        assert "save_screenshot" in {spec.name for spec in effective_static_tool_specs()}
    assert conversations.set_conversation_special_mode(first, False)["special_mode"] is False
    validate_special_image_input(first, has_image=True)


def test_skill_creator_saves_only_new_skills_and_selects_one_for_the_turn(tmp_path, monkeypatch):
    from osia import agent, conversations, personality
    from osia.chatgpt_account.app_server_client import _canonical_native_tools
    from osia.slash_commands import chat_command_scope
    from osia.tool_contracts import effective_static_tool_specs

    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(personality, "SKILLS_DIR", str(tmp_path / "skills"))
    enabled = {}
    monkeypatch.setattr(personality, "set_skill_enabled_state", enabled.__setitem__)
    monkeypatch.setattr(personality, "get_setting", lambda key, default=None: (
        enabled if key == "skills_enabled" else default
    ))
    monkeypatch.setattr(personality, "notify_config_changed", lambda _key: None)

    arguments = {"name": "meeting-notes", "description": "Summarize meeting notes.",
                 "instructions": "Présente les décisions et les actions.",
                 "required_tools": ["read_document"]}
    assert agent.tool_create_chat_skill(**arguments)["receipt"]["dispatch_state"] == "not_dispatched"
    conv_id = conversations.create_conversation()["id"]
    create = SlashTurnDirectivesV1.from_wire({"schema_version": 1, "skill_creator": True})
    assert create.to_wire()["skill_creator"] is True
    with pytest.raises(SlashDirectiveError, match="Describe the skill"):
        create.require_sendable(body="", has_attachment=True)
    with chat_command_scope(conv_id, create):
        assert "create_chat_skill" in {spec.name for spec in effective_static_tool_specs()}
        assert "create_chat_skill" in {spec["name"] for spec in _canonical_native_tools()}
        assert "[User selected /skillcreator for this turn.]" in agent.get_system_prompt("fr")
        saved = agent._execute_tool_with_policy("create_chat_skill", arguments)
        assert saved.dispatched and not saved.is_error
        assert saved.result["receipt"]["reason_code"] == "SKILL_CREATED"
        duplicate = agent.tool_create_chat_skill(**{**arguments, "instructions": "Overwrite it."})
        assert duplicate["receipt"]["outcome"] == "confirmed_failure_before_effect"
        invalid = agent.tool_create_chat_skill(**{**arguments, "name": "../outside"})
        assert invalid["receipt"]["dispatch_state"] == "not_dispatched"

    selected = SlashTurnDirectivesV1.from_wire({"schema_version": 1, "skill_name": "meeting-notes.md"})
    validate_live(selected, current_conversation_id=conv_id)
    assert not personality.is_skill_enabled("meeting-notes.md")
    with chat_command_scope(conv_id, selected):
        prompt = agent.get_system_prompt("fr")
        assert "User-selected OSIA skill for this turn" in prompt
        assert arguments["instructions"] in prompt
        assert "Required OSIA tools: read_document" in prompt
        assert "- create_chat_skill(" not in prompt
    assert arguments["instructions"] not in agent.get_system_prompt("fr")
    with pytest.raises(SlashDirectiveError, match="unavailable"):
        validate_live(SlashTurnDirectivesV1(skill_name="missing.md"), current_conversation_id=conv_id)


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("/", ""),
        ("/ ", ""),
        ("/ special", "special"),
        ("   /web", "web"),
        ("/linear do it", "linear"),
        ("//web", None),
        ("https://osia.space/path", None),
        ("C:/Users/test", None),
        ("2/3", None),
        ("hello /web", None),
        ("/UPPER", None),
        ("/1", None),
    ],
)
def test_palette_grammar_only_matches_initial_ascii_directive_prefix(text, query):
    assert slash_palette_query(text) == query


def test_literal_escape_and_selected_alias_preserve_body():
    assert escaped_literal_text("  //web is prose") == "  /web is prose"
    assert consume_selected_alias("  /web /linear find it", "web") == (
        "  /linear find it"
    )
    assert consume_selected_alias("https://x/web", "web") == "https://x/web"
    assert consume_palette_prefix("  /we /linear find it") == (
        "  /linear find it"
    )
    assert consume_palette_prefix("/ skillcreator a new brief") == "a new brief"


def test_connector_and_conversation_title_collision_remain_distinct(monkeypatch):
    import osia.slash_commands as slash

    monkeypatch.setattr(slash, "model_availability", lambda **_kwargs: ())
    monkeypatch.setattr(slash, "current_web_available", lambda *_args: False)
    monkeypatch.setattr(
        slash,
        "connector_availability",
        lambda: (
            slash.ConnectorAvailability(
                "linear", "linear", "Linear", "Connector",
                "connected", True, True,
            ),
        ),
    )
    monkeypatch.setattr(
        slash,
        "conversation_availability",
        lambda **_kwargs: ({
            "conversation_id": "conv-linear",
            "display_title": "Linear",
            "display_project": "Delivery",
        },),
    )
    rows = slash.palette_items(filter_text="linear")
    assert {(row["category"], row["id"]) for row in rows} == {
        ("connectors", "connector.linear"),
        ("conversations", "conversation.conv-linear"),
    }


def test_help_is_registry_derived_and_includes_argument_hints(monkeypatch):
    import osia.slash_commands as slash

    monkeypatch.setattr(slash, "connector_availability", lambda: ())
    monkeypatch.setattr(
        slash,
        "palette_items",
        lambda **_kwargs: ({
            "canonical_alias": "model",
            "description": "Choose one exact model for this turn",
            "argument_hint": "model",
            "enabled": True,
            "category": "commands",
        },),
    )
    payload = informational_payload("help.info")
    assert payload["commands"] == [{
        "alias": "/model",
        "description": "Choose one exact model for this turn",
        "argument_hint": "model",
    }]


def test_safe_overlay_and_status_bound_connector_rows(monkeypatch):
    import osia.slash_commands as slash

    connectors = tuple(
        slash.ConnectorAvailability(
            f"connector-{index}", f"connector-{index}", f"Connector {index}",
            "Connector", "connected", True, True,
        )
        for index in range(30)
    )
    monkeypatch.setattr(slash, "connector_availability", lambda: connectors)
    monkeypatch.setattr(
        "osia.capability_federation.federated_capabilities", lambda _provider: (),
    )
    overlay = safe_availability_overlay()
    status = informational_payload("status.info")
    assert len(overlay["connectors"]) == 24
    assert status["connector_count"] == 30
    assert len(status["connectors"]) == 8


def test_wire_contract_is_strict_canonical_and_bounded():
    directives = SlashTurnDirectivesV1(
        connector_hints=(ConnectorHint("linear"),),
        capability_requirements=CapabilityRequirements(current_web=True),
        conversation_refs=(ConversationRef("conv-1", "Title", "Project"),),
        model_override=ModelOverride("openai", "gpt-5.4"),
    )
    directives._validate_bounds()
    replayed = SlashTurnDirectivesV1.from_wire(directives.to_wire())
    assert replayed == directives
    assert json.loads(replayed.canonical_json()) == directives.to_wire()
    with pytest.raises(SlashDirectiveError, match="Unknown typed metadata") as error:
        SlashTurnDirectivesV1.from_wire({
            **directives.to_wire(),
            "commands": ["/web"],
        })
    assert error.value.code == "unknown_directive_metadata"


def test_duplicates_and_fifth_modifier_fail_closed():
    with pytest.raises(SlashDirectiveError) as duplicate:
        SlashTurnDirectivesV1.from_wire({
            "schema_version": 1,
            "connector_hints": [
                {"connector_id": "linear"},
                {"connector_id": "gmail-rest"},
            ],
            "capability_requirements": {"current_web": False},
            "conversation_refs": [],
        })
    assert duplicate.value.code == "directive_limit_exceeded"
    fifth = SlashTurnDirectivesV1(
        connector_hints=(ConnectorHint("linear"),),
        capability_requirements=CapabilityRequirements(current_web=True),
        conversation_refs=(ConversationRef("conv-1"),),
        model_override=ModelOverride("openai", "gpt"),
        reasoning_override="high",
    )
    with pytest.raises(SlashDirectiveError) as too_many:
        fifth._validate_bounds()
    assert too_many.value.code == "directive_limit_exceeded"


def test_modifier_requires_body_or_attachment_and_connector_is_structural():
    directives = SlashTurnDirectivesV1(
        connector_hints=(ConnectorHint("linear"),),
    )
    with pytest.raises(SlashDirectiveError) as empty:
        directives.require_sendable(body="", has_attachment=False)
    assert empty.value.code == "directive_body_required"
    directives.require_sendable(body="", has_attachment=True)
    # WOR-301: connector hints never scope visibility or dispatch.
    from osia import slash_commands as _slash_module
    assert not hasattr(_slash_module, "tool_allowed_for_directives")


def test_connector_inventory_always_renders_every_connected_runtime(monkeypatch):
    from osia.mcp_client import registry

    schema = {"type": "object", "properties": {}}
    monkeypatch.setattr(registry, "_tool_meta", {
        "mcp__linear__search": {
            "server": "linear", "description": "Search Linear",
            "inputSchema": schema,
        },
        "mcp__notion__search": {
            "server": "notion", "description": "Search Notion",
            "inputSchema": schema,
        },
    })
    # WOR-300/WOR-301: the model-visible inventory is complete and the
    # slash connector hint never scopes visibility or dispatch.
    block = registry.mcp_tool_prompt_block()
    assert "mcp__linear__search" in block
    assert "mcp__notion__search" in block


def test_exact_model_application_uses_model_then_provider_and_is_turn_scoped():
    calls = []

    class Agent:
        _disable_hierarchy_for_turn = False

        def set_model(self, model_id, provider=None):
            calls.append((model_id, provider))

    agent = Agent()
    directives = SlashTurnDirectivesV1(
        model_override=ModelOverride("openai", "gpt-5.4"),
        reasoning_override="high",
    )
    apply_to_agent(agent, directives)
    assert calls == [("gpt-5.4", "openai")]
    assert agent._disable_hierarchy_for_turn is True
    assert agent._reasoning_override_for_turn == "high"
    assert agent._slash_turn_directives is directives


def test_exact_model_rejects_an_incompatible_image_attachment(monkeypatch):
    monkeypatch.setattr(
        "osia.slash_commands.model_availability",
        lambda **_kwargs: ({
            "providerId": "openai",
            "id": "text-only",
            "inputModalities": ["text"],
        },),
    )
    directives = SlashTurnDirectivesV1(
        model_override=ModelOverride("openai", "text-only"),
    )
    with pytest.raises(SlashDirectiveError) as incompatible:
        validate_live(directives, requires_image_attachment=True)
    assert incompatible.value.code == "model_attachment_incompatible"


def test_explicit_reasoning_self_reference_and_deleted_reference_fail_closed(
    monkeypatch,
):
    monkeypatch.setattr(
        "osia.slash_commands.model_availability",
        lambda **_kwargs: ({
            "providerId": "openai",
            "id": "gpt-test",
            "reasoning": {"options": [{"id": "low"}]},
        },),
    )
    with pytest.raises(SlashDirectiveError) as unsupported:
        validate_live(SlashTurnDirectivesV1(
            model_override=ModelOverride("openai", "gpt-test"),
            reasoning_override="high",
        ))
    assert unsupported.value.code == "unsupported_reasoning"

    self_ref = SlashTurnDirectivesV1(
        conversation_refs=(ConversationRef("conv-current"),),
    )
    with pytest.raises(SlashDirectiveError) as current:
        validate_live(self_ref, current_conversation_id="conv-current")
    assert current.value.code == "conversation_self_reference"
    monkeypatch.setattr("osia.conversations.get_conversation", lambda _identity: None)
    with pytest.raises(SlashDirectiveError) as deleted:
        validate_live(SlashTurnDirectivesV1(
            conversation_refs=(ConversationRef("conv-deleted"),),
        ))
    assert deleted.value.code == "conversation_not_found"


def test_connector_hint_does_not_change_permission_and_web_proof_is_typed():
    class Agent:
        permission_mode = "Standard"
        _disable_hierarchy_for_turn = False

        def set_model(self, *_args, **_kwargs):
            raise AssertionError("connector-only directive must not select a model")

    agent = Agent()
    apply_to_agent(agent, SlashTurnDirectivesV1(
        connector_hints=(ConnectorHint("linear"),),
    ))
    assert agent.permission_mode == "Standard"
    assert tool_is_current_web_evidence("search_web")
    assert tool_is_current_web_evidence("inspect_browser_page")
    assert not tool_is_current_web_evidence("read_file")


def test_web_requirement_uses_the_active_provider_without_exact_override(
    monkeypatch,
):
    providers = []
    monkeypatch.setattr(
        "osia.config.load_settings",
        lambda: {"selected_model_provider": "chatgpt_account"},
    )
    monkeypatch.setattr(
        "osia.slash_commands.current_web_available",
        lambda provider: providers.append(provider) or True,
    )
    validate_live(SlashTurnDirectivesV1(
        capability_requirements=CapabilityRequirements(current_web=True),
    ))
    assert providers == ["chatgpt_account"]


def test_configured_disconnected_gmail_is_visible_but_disabled(monkeypatch):
    monkeypatch.setattr("osia.config.load_settings", lambda: {"mcp_servers": []})
    monkeypatch.setattr(
        "osia.integrations.google_oauth.oauth_client_configured",
        lambda key: key == "gmail-rest",
    )
    monkeypatch.setattr(
        "osia.tools.gmail.gmail_connection_status",
        lambda validate=False: {"connected": False, "status": "disconnected"},
    )

    class Manager:
        @staticmethod
        def servers():
            return {}

    monkeypatch.setattr("osia.mcp_client.manager.get_manager", Manager)
    gmail = next(
        item for item in connector_availability()
        if item.connector_id == "gmail-rest"
    )
    assert gmail.configured is True
    assert gmail.usable is False
    assert gmail.state == "disconnected"


def test_configured_connected_linear_is_selectable_from_live_tools(monkeypatch):
    monkeypatch.setattr(
        "osia.config.load_settings",
        lambda: {"mcp_servers": [{
            "id": "linear", "name": "Linear", "enabled": True,
        }]},
    )
    monkeypatch.setattr(
        "osia.integrations.google_oauth.oauth_client_configured", lambda _key: False,
    )
    monkeypatch.setattr(
        "osia.tools.gmail.gmail_connection_status",
        lambda validate=False: {"connected": False},
    )

    class Manager:
        @staticmethod
        def servers():
            return {"linear": {"status": "connected", "tools": 1}}

        @staticmethod
        def list_tools(server_id):
            assert server_id == "linear"
            return [{"name": "mcp__linear__search"}]

    monkeypatch.setattr("osia.mcp_client.manager.get_manager", Manager)
    linear = next(
        item for item in connector_availability()
        if item.connector_id == "linear"
    )
    assert linear.alias == "linear"
    assert linear.usable is True
    assert linear.tool_names == ("mcp__linear__search",)


def test_legacy_gmail_never_owns_the_native_alias(monkeypatch):
    monkeypatch.setattr(
        "osia.config.load_settings",
        lambda: {"mcp_servers": [{
            "id": "gmail",
            "name": "Legacy Gmail",
            "url": "https://gmailmcp.googleapis.com/mcp",
            "enabled": True,
        }]},
    )
    monkeypatch.setattr(
        "osia.integrations.google_oauth.oauth_client_configured",
        lambda _key: False,
    )
    monkeypatch.setattr(
        "osia.tools.gmail.gmail_connection_status",
        lambda validate=False: {"connected": False},
    )

    class Manager:
        @staticmethod
        def servers():
            return {"gmail": {"status": "connected", "tools": 2}}

    monkeypatch.setattr("osia.mcp_client.manager.get_manager", Manager)
    assert all(
        item.alias != "gmail" for item in connector_availability()
    )


def test_explicit_conversation_context_reads_only_selected_identity(monkeypatch):
    from osia import conversations
    from osia.conversation_context import (
        FOLDER_CONTEXT_MAX_CHARS,
        FOLDER_CONTEXT_MAX_EXCERPTS,
        retrieve_explicit_conversation_context,
    )

    calls = []
    selected = {
        "id": "selected",
        "messages": [
            {"role": "user", "content": "The release marker is MARKER_SELECTED."},
            {"role": "assistant", "content": "Confirmed MARKER_SELECTED."},
        ],
    }

    def get_conversation(identity):
        calls.append(identity)
        return selected if identity == "selected" else None

    monkeypatch.setattr(conversations, "get_conversation", get_conversation)
    context = retrieve_explicit_conversation_context(
        "selected", "What is the release marker?",
    )
    assert calls == ["selected"]
    assert context.conversation_ids == ("selected",)
    assert context.excerpt_count <= FOLDER_CONTEXT_MAX_EXCERPTS
    assert len(context.prompt) <= FOLDER_CONTEXT_MAX_CHARS + 300
    assert "MARKER_SELECTED" in context.prompt
    assert "untrusted" in context.prompt.lower()


def test_persisted_replay_fingerprint_rejects_changed_directives(monkeypatch):
    import asyncio
    from osia.server import server

    def identity(directives):
        return server._turn_input_identity(json.dumps({
            "attachment_batch_id": "",
            "text": "hello",
            "turn_directives": directives.canonical_json(),
        }, sort_keys=True, separators=(",", ":")), kind="text")

    selected = identity(SlashTurnDirectivesV1(skill_name="brief.md"))
    found = {
        **selected,
        "conversation_id": "conv-1",
        "request_id": "req-1",
        "turn_id": "slash-replay-turn",
        "run_id": "slash-replay-run",
        "provider_id": "deepseek",
        "model_id": "deepseek-flash",
        "effective_reasoning": "auto",
        "user_message_id": "message-user",
        "assistant_message_id": "message-assistant",
        "response_event_id": "event-response",
        "response_sequence": 1,
        "terminal_event_id": "event-terminal",
        "terminal_sequence": 2,
        "assistant_content": "done",
        "terminal_status": "done",
    }
    monkeypatch.setattr(
        server.conversations, "find_correlated_turn", lambda *_args: found,
    )

    class Channel:
        def __init__(self):
            self.frames = []

        async def send_json(self, frame):
            self.frames.append(frame)

    channel = Channel()
    assert asyncio.run(server._send_persisted_turn_for_request(
        channel, "conv-1", "req-1", expected_identity=selected,
    ))
    assert [frame["type"] for frame in channel.frames] == [
        "response", "turn_status",
    ]
    with pytest.raises(ValueError, match="different content"):
        asyncio.run(server._send_persisted_turn_for_request(
            channel, "conv-1", "req-1",
            expected_identity=identity(SlashTurnDirectivesV1(skill_creator=True)),
        ))
