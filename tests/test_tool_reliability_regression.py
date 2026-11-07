"""Canonical contract and continuation harnesses; no physical desktop effects."""
import inspect
import json

import pytest
from jsonschema import Draft202012Validator

from osia import agent as runtime
from osia.capability_federation import federated_capabilities
from osia.chatgpt_account.app_server_client import _canonical_native_tools
from osia.tool_contracts import (
    ALL_TEXT_PROVIDERS, BROWSER_FAMILY_TOOL_NAMES, ToolValidationError, effective_static_tool_specs,
    get_static_registry, static_tool_prompt_block, validate_tool_arguments,
    validation_error_details,
)
from test_wor227_invalid_arguments import _install_rounds
from tests.test_wor305_phase3a_no_replay import dispatch


def test_whole_registry_signatures_and_advertised_schemas():
    registry = get_static_registry()
    registry.assert_complete(runtime.TOOL_MAP)
    for spec in registry.values():
        Draft202012Validator.check_schema(spec.input_schema)
        signature = inspect.signature(spec.dispatcher)
        parameters = signature.parameters
        if not any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
            assert set(spec.input_schema.get("properties", {})) <= set(parameters), spec.name
        required = set(spec.input_schema.get("required", []))
        for name, parameter in parameters.items():
            if (parameter.default is parameter.empty and parameter.kind in
                    {parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY}):
                assert name in required, spec.name
    prompt = static_tool_prompt_block()
    definitions = json.loads(prompt.split("against this block. ", 1)[1])["$defs"]
    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].rsplit("/", 1)[1]])
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list): return [expand(v) for v in value]
        return value
    for spec in effective_static_tool_specs():
        line = next(line for line in prompt.splitlines() if line.startswith(f"- {spec.name}("))
        schema = json.loads(line.split("; input_schema=", 1)[1])
        assert expand(schema) == spec.input_schema
        Draft202012Validator.check_schema({**schema, "$defs": definitions})


@pytest.mark.parametrize("provider", ALL_TEXT_PROVIDERS)
def test_provider_capability_contract_parity(provider):
    available = {s.name for s in effective_static_tool_specs()}
    projected = {c.identity for c in federated_capabilities(provider) if c.source == "osia"}
    assert projected == available
    prompt = runtime.get_system_prompt("fr")
    assert "natural first-person sentences" in prompt
    assert prompt.count("natural first-person sentences") == 1
    assert '"minimum":0.1' in prompt and '"maximum":300.0' in prompt
    from osia.conversation_context import build_context
    build_context([{"role": "system", "content": prompt}, {"role": "user", "content": "Fixture request"}],
                  provider=provider, model="deepseek-flash" if provider == "deepseek" else "fixture")


def test_nullable_timeout_feedback_reports_actual_bound_without_values():
    spec = get_static_registry().get("run_command")
    with pytest.raises(ToolValidationError) as raised:
        validate_tool_arguments(spec, {"command": "PRIVATE_COMMAND_SENTINEL", "timeout": 999})
    details = validation_error_details(raised.value)
    assert details["issues"] == [{"path": "$.timeout", "code": "maximum", "expected": {"maximum": 300.0}}]
    assert details["dispatch_state"] == "not_dispatched"
    assert "PRIVATE_COMMAND_SENTINEL" not in json.dumps(details)
    assert "999" not in json.dumps(details)


@pytest.mark.parametrize("provider", ALL_TEXT_PROVIDERS)
def test_invalid_shell_then_alternative_app_and_url_continue_once(monkeypatch, provider):
    dispatches = []
    monkeypatch.setitem(runtime.TOOL_MAP, "run_command", lambda **kw: pytest.fail("invalid call dispatched"))
    def application(name):
        dispatches.append(("application", name))
        return {"ok": True, "receipt": {"schema_version": 1, "dispatch_state": "effect_confirmed", "outcome": "confirmed_success"}}
    def navigate(url, state_ref=None, wait_spec=None):
        dispatches.append(("url", url))
        return {"ok": True, "receipt": {"schema_version": 1, "dispatch_state": "effect_confirmed", "outcome": "confirmed_success"}}
    monkeypatch.setitem(runtime.TOOL_MAP, "open_application", application)
    monkeypatch.setitem(runtime.TOOL_MAP, "navigate_browser", navigate)
    a = runtime.OSIAAgent(model="fixture", lang="en")
    a.provider = provider
    a.model = "deepseek-flash" if provider == "deepseek" else "fixture"
    observations = []
    final = "The fixture app and requested website opened."
    _install_rounds(a, [
        '<tool_call>{"tool":"run_command","command":"PRIVATE_COMMAND_SENTINEL","timeout":999}</tool_call>',
        '<tool_call>{"tool":"open_application","name":"FixtureApp"}</tool_call>',
        '<tool_call>{"tool":"navigate_browser","url":"https://makg.pro"}</tool_call>',
        final, final,
    ], observations)
    result = a.run("Open the fixture application, then https://makg.pro.", history=[], on_sentence=lambda _: None)
    assert result == final
    assert dispatches == [("application", "FixtureApp"), ("url", "https://makg.pro")]
    feedback = observations[1]["messages"].rsplit("<tool_result>", 1)[-1]
    assert "PRIVATE_COMMAND_SENTINEL" not in feedback
    assert "Current tool JSON Schema" in feedback and "maximum" in feedback
    assert "confirmed_success" in observations[3]["messages"]
    assert {(o["provider"], o["model"]) for o in observations} == {(provider, a.model)}


def test_account_app_tools_have_exact_native_contracts():
    tools = {t["name"]: t for t in _canonical_native_tools()}
    for name in ("open_application", "open_uri", "is_installed", "is_running", "has_window", "steam_game_state"):
        assert tools[name]["inputSchema"] == get_static_registry().get(name).input_schema


def test_account_managed_browser_tools_stay_available_with_mouse_disabled(monkeypatch):
    from osia.slash_commands import chat_command_scope

    monkeypatch.setattr("osia.conversations.conversation_special_mode", lambda _: True)
    with chat_command_scope("browser-fixture", None):
        tools = {tool["name"]: tool for tool in _canonical_native_tools()}
        assert BROWSER_FAMILY_TOOL_NAMES <= tools.keys()
        for name in BROWSER_FAMILY_TOOL_NAMES:
            assert tools[name]["inputSchema"] == get_static_registry().get(name).input_schema


@pytest.mark.parametrize(("downloaded", "progress"), [
    ("0", "0.0%"), ("9999", "99.9%"), ("10000", "100.0%"), ("invalid", None),
])
def test_account_can_read_steam_download_progress(monkeypatch, tmp_path, downloaded, progress):
    from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
    from osia.tool_attempts import ToolAttemptStore
    from osia.tools import steam

    monkeypatch.setattr(steam, "steam_path", lambda: str(tmp_path))
    monkeypatch.setattr(steam, "_game_running", lambda _: False)
    apps = tmp_path / "steamapps"
    apps.mkdir()
    (apps / "appmanifest_264710.acf").write_text(
        '"AppState" { "appid" "264710" "name" "Fixture Game" '
        f'"StateFlags" "1026" "BytesToDownload" "10000" "BytesDownloaded" "{downloaded}" }}',
        encoding="utf-8",
    )
    store = ToolAttemptStore(tmp_path / "attempts")
    client = AppServerClient(base_dir=tmp_path / "account", tool_attempt_store=store)
    sent = []
    client._send = sent.append
    attempt = _TurnAttempt(thread_id="fixture-thread", turn_id="fixture-turn", model_id="gpt-6-luna")
    client._attempts[attempt.thread_id] = attempt
    client._handle_server_request({"id": 42, "method": "item/tool/call", "params": {
        "threadId": attempt.thread_id, "turnId": attempt.turn_id, "callId": "steam-state",
        "tool": "steam_game_state", "arguments": {"name": "264710"},
    }})
    kind, payload = attempt.events.get_nowait()
    assert kind == "dynamic_tool_call"
    client._resolve_dynamic_tool_call(attempt, payload)

    assert sent[0]["id"] == 42
    assert sent[0]["result"]["success"] is True
    result = json.dumps(sent[0]["result"])
    assert "installed: no" in result
    if progress is None:
        assert "Steam manifest download" not in result
    else:
        assert progress in result
    assert attempt.native_tool_call is None


def test_account_dynamic_rejection_supplies_precise_same_turn_feedback(monkeypatch):
    from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
    sent = []
    client = AppServerClient()
    client._send = sent.append
    attempt = _TurnAttempt(thread_id="fixture-thread", turn_id="fixture-turn")
    client._resolve_dynamic_tool_call(attempt, {
        "request_id": 42, "params": {}, "call": {
            "call_id": "fixture-call", "tool": "inspect_desktop_surfaces",
            "arguments": {"target": 987654321},
        },
    })
    response = json.dumps(sent)
    assert "INVALID_TOOL_ARGUMENTS" in response
    assert "input_schema" in response and "$.target" in response
    assert "retryable_in_same_model_turn" in response
    assert "987654321" not in response


def test_bare_domain_normalization_retains_requested_host():
    from osia.tools.browser import _normalize_url
    assert _normalize_url("makg.pro") == "https://makg.pro"
    validate_tool_arguments(get_static_registry().get("navigate_browser"), {"url": "https://makg.pro"})


def test_windows_launcher_resolves_spawns_and_confirms_only_fixture_window(monkeypatch):
    from types import SimpleNamespace
    from osia.tools import desktop_control as dc
    import osia.desktop_scene as scene
    launched = []
    monkeypatch.setattr(scene, "focus_existing_application_window", lambda _: ("unavailable", ""))
    monkeypatch.setattr(runtime, "_lookup_memory_app_route", lambda _: None)
    monkeypatch.setattr(runtime, "_remember_verified_app_route", lambda *_: None)
    monkeypatch.setattr(dc, "find_app_window", lambda _: None)
    monkeypatch.setattr(dc, "resolve_windows_app", lambda _: ("fixture.exe", []))
    monkeypatch.setattr(dc, "windows_app_candidates", lambda *_: {"fixture"})
    monkeypatch.setattr(dc, "find_app_window_multi", lambda _: (42, "fixture") if launched else None)
    monkeypatch.setattr(dc, "foreground_snapshot", lambda: (1, "initial"))
    monkeypatch.setattr(dc, "is_app_running", lambda _: False)
    monkeypatch.setattr(dc, "focus_app_window", lambda *a, **kw: ("ready", "fixture"))
    def spawn(args, **kw):
        launched.append(args)
        return SimpleNamespace(poll=lambda: 0)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    result = runtime.tool_open_application("FixtureApp")
    assert launched == [["fixture.exe"]]
    assert "ready and focused" in result


def test_managed_navigation_executes_and_records_observed_fixture_commit():
    import asyncio
    from osia.tools.browser import _AsyncBrowserOwner
    owner = _AsyncBrowserOwner()
    class Page:
        url = "https://fixture.invalid/initial"
        main_frame = object()
        def is_closed(self): return False
        def on(self, *_): pass
        async def goto(self, url, **_): self.url = url
        async def bring_to_front(self): pass
        async def wait_for_load_state(self, *_args, **_kwargs): pass
    page, context = Page(), object()
    owner._context = context
    owner._active_page = page
    owner._state_tracker.start_context(context)
    before = owner._state_tracker.register_page(page)
    async def resolve(*_, **__): return page, before
    owner._resolve_state_page = resolve
    owner._publish = lambda: None
    result = asyncio.run(owner._op_navigate_safe("makg.pro"))
    assert page.url == "https://makg.pro"
    assert result["ok"] is True
    assert result["receipt"]["outcome"] == "confirmed_success"


def test_canonical_rejections_remain_facts_and_allow_completion(dispatch, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from osia.turn_terminality import TurnTerminalityController
    invoked = []
    monkeypatch.setitem(runtime.TOOL_MAP, "get_system_info", lambda: invoked.append(1) or "Fixture system: ready")
    a = runtime.OSIAAgent(model="fixture", lang="en")
    a._terminality_controller = TurnTerminalityController(
        turn_id="fixture-recovery", objective="Read fixture system information",
        execution_required=True, storage_dir=tmp_path / "terminality",
    )
    a._learning_engine = SimpleNamespace(ingest_terminal_attempts=lambda: None, context_for_request=lambda _: "")
    provider_calls = []
    _install_rounds(a, [
        '<tool_call>{"tool":"run_command","command":"PRIVATE_COMMAND_SENTINEL","timeout":999}</tool_call>',
        '<tool_call>{"tool":"run_command","command":"PRIVATE_COMMAND_SENTINEL","timeout":999}</tool_call>',
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        "The fixture system is ready.",
    ], provider_calls)
    final = a.run("Read the fixture system information.", history=[], on_sentence=lambda _: None)
    assert final == "The fixture system is ready."
    assert invoked == [1] and len(provider_calls) == 4
    state = a._terminality_controller.snapshot()
    rejected = [e for e in state["typed_evidence"] if e.get("outcome") == "rejected_before_execution"]
    # Identical value-free facts may share an evidence token; neither rejection
    # was dispatched, and both reached the same model as separate feedback.
    assert rejected
    assert all(e.get("dispatch_state") != "effect_confirmed" for e in rejected)


def test_canonical_driver_recovers_rejection_then_emits_one_final(dispatch, monkeypatch):
    from types import SimpleNamespace
    a = runtime.OSIAAgent(model="fixture", lang="en")
    a._learning_engine = SimpleNamespace(ingest_terminal_attempts=lambda: None, context_for_request=lambda _: "")
    invoked = []
    monkeypatch.setitem(runtime.TOOL_MAP, "get_system_info", lambda: invoked.append(1) or "Fixture system: ready")
    observations, finals = [], []
    responses = iter([
        '<tool_call>{"tool":"run_command","command":"PRIVATE_COMMAND_SENTINEL","timeout":999}</tool_call>',
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        "The fixture system is ready.",
        "The fixture system is ready.",
    ])
    def create(**kwargs):
        observations.append(kwargs)
        try: content = next(responses)
        except StopIteration: pytest.fail("Unexpected extra fixture provider round")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])
    a.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    a.configure_tool_attempts(conversation_id="fixture", turn_id="fixture-driver", request_id="fixture", final_response_message_id="fixture-final")
    result = a.run_terminal_turn("Read the fixture system information.", turn_id="fixture-driver", history=[], on_sentence=finals.append)
    assert result == "The fixture system is ready."
    assert finals == [result] and invoked == [1] and len(observations) == 4
