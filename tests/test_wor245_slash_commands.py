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
    tool_allowed_for_directives,
    tool_is_current_web_evidence,
    apply_to_agent,
    validate_live,
)


def test_static_catalog_is_bilingual_bounded_and_byte_identical():
    entries = load_catalog()
    assert [entry["canonical_alias"] for entry in entries] == [
        "web", "model", "reasoning", "conversation", "tools", "status", "help",
    ]
    assert all(entry["name"]["en"] and entry["name"]["fr"] for entry in entries)
    pc = Path("assets/slash_commands/catalog.v1.json").read_bytes()
    mobile = Path("../osia-mobile/assets/slash_commands/catalog.v1.json").read_bytes()
    assert pc == mobile


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("/", ""),
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
    assert tool_allowed_for_directives("mcp__linear__get_issue", directives)
    assert not tool_allowed_for_directives("mcp__notion__search", directives)
    assert not tool_allowed_for_directives("gmail_search", directives)
    assert tool_allowed_for_directives("search_web", directives)


def test_connector_inventory_renders_only_the_selected_runtime(monkeypatch):
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
    block = registry.mcp_tool_prompt_block(server_id="linear")
    assert "mcp__linear__search" in block
    assert "mcp__notion__search" not in block


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
    import hashlib
    from osia.server import server

    identity = "hello\0" + SlashTurnDirectivesV1(
        capability_requirements=CapabilityRequirements(current_web=True),
    ).canonical_json()
    found = {
        "client_content_hash": hashlib.sha256(identity.encode()).hexdigest(),
        "turn_id": "turn-1",
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
        channel, "conv-1", "req-1", expected_user_content=identity,
    ))
    assert [frame["type"] for frame in channel.frames] == [
        "response", "turn_status",
    ]
    async def changed():
        return await server._send_persisted_turn_for_request(
            channel,
            "conv-1",
            "req-1",
            expected_user_content="hello\0changed-directives",
        )
    with pytest.raises(ValueError, match="different content"):
        asyncio.run(changed())
