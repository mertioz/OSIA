"""Public research stays in the selected model's canonical tool loop."""

from __future__ import annotations

from email.message import Message
import io
import json
import socket
from urllib.parse import parse_qs, urlsplit

import pytest

from osia import agent as runtime
from osia.capability_federation import federated_capabilities
from osia.chatgpt_account.app_server_client import (
    AppServerClient, _TurnAttempt, _canonical_native_tools,
)
from osia.slash_commands import tool_is_current_web_evidence
from osia.tool_attempts import ToolAttemptStore
from osia.tool_contracts import ALL_TEXT_PROVIDERS, ExecutionOutcome
from osia.tools import public_web


_RESULT_URL = "https://docs.example.com/application/install"
_RSS = f"""
<rss version="2.0"><channel>
  <item><title>Official setup guide</title><link>{_RESULT_URL}</link>
    <description>The app supports installation from its built-in library.</description></item>
  <item><title>Private address</title><link>http://127.0.0.1/</link></item>
</channel></rss>
""".encode()


def _search_fixture(monkeypatch):
    requests = []

    def request(url, accept, limit, deadline):
        requests.append(url)
        return public_web._Response("application/rss+xml", body=_RSS)

    monkeypatch.setattr(public_web, "_request_once", request)
    monkeypatch.setattr(runtime, "_get_browser_tools", lambda: pytest.fail("Public research opened the browser"))
    return requests


def test_every_provider_keeps_public_research_and_browser_actions():
    for provider in ALL_TEXT_PROVIDERS:
        available = {
            item.identity: item for item in federated_capabilities(provider)
            if item.available
        }
        assert available["search_public_web"].equivalence_class == "web_search"
        assert available["read_public_web_page"].side_effect_class == "read_only"
        assert {"open_browser", "navigate_browser", "search_web"} <= available.keys()
    assert {"search_public_web", "read_public_web_page"} <= {
        spec["name"] for spec in _canonical_native_tools()
    }


def test_search_returns_sources_through_canonical_dispatch(monkeypatch):
    requests = _search_fixture(monkeypatch)
    record = runtime._execute_tool_with_policy(
        "search_public_web", {"query": "  official\n application   setup "},
        allow_retry=False,
    )
    assert record.outcome is ExecutionOutcome.SUCCESS_VERIFIED
    assert record.result["results"] == [{
        "title": "Official setup guide", "url": _RESULT_URL,
        "snippet": "The app supports installation from its built-in library.",
    }]
    assert parse_qs(urlsplit(requests[0]).query)["q"] == ["official application setup"]
    assert tool_is_current_web_evidence("search_public_web")
    assert tool_is_current_web_evidence("read_public_web_page")


@pytest.mark.parametrize(("provider", "model"), [
    ("deepseek", "deepseek-flash"),
    ("openrouter", "fixture/model"),
])
def test_research_informs_the_following_local_action(monkeypatch, provider, model):
    from test_wor227_invalid_arguments import _install_rounds

    _search_fixture(monkeypatch)
    opened = []

    def open_application(name):
        opened.append(name)
        return {
            "ok": True,
            "text": "The application opened.",
            "receipt": {"schema_version": 1, "dispatch_state": "effect_confirmed", "outcome": "confirmed_success"},
        }

    monkeypatch.setitem(runtime.TOOL_MAP, "open_application", open_application)
    agent = runtime.OSIAAgent(model="fixture", lang="en")
    agent.provider, agent.model = provider, model
    observations = []
    final = "I checked the public setup guide and opened the app."
    _install_rounds(agent, [
        '<tool_call>{"tool":"search_public_web","query":"Fixture Editor official setup"}</tool_call>',
        '<tool_call>{"tool":"open_application","name":"Fixture Editor"}</tool_call>',
        final,
        final,
    ], observations)

    assert agent.run("Check the public setup guide, then open Fixture Editor.", history=[], on_sentence=lambda _: None) == final
    assert _RESULT_URL in observations[1]["messages"]
    assert opened == ["Fixture Editor"]
    assert {(round_["provider"], round_["model"]) for round_ in observations} == {(provider, model)}
    assert agent.last_slash_capability_receipt["identity"] == "search_public_web"


def test_account_research_returns_to_the_same_native_turn(monkeypatch, tmp_path):
    requests = _search_fixture(monkeypatch)
    client = AppServerClient(
        base_dir=tmp_path / "account",
        tool_attempt_store=ToolAttemptStore(tmp_path / "attempts"),
    )
    sent = []
    client._send = sent.append
    attempt = _TurnAttempt(thread_id="research-thread", turn_id="research-turn", model_id="gpt-6-luna")
    client._attempts[attempt.thread_id] = attempt
    client._handle_server_request({"id": 21, "method": "item/tool/call", "params": {
        "threadId": attempt.thread_id, "turnId": attempt.turn_id,
        "callId": "public-search-1", "tool": "search_public_web",
        "arguments": {"query": "official application setup"},
    }})
    kind, payload = attempt.events.get_nowait()
    assert kind == "dynamic_tool_call"
    client._resolve_dynamic_tool_call(attempt, payload)

    assert len(requests) == 1 and len(sent) == 1
    assert sent[0]["id"] == 21 and sent[0]["result"]["success"] is True
    assert _RESULT_URL in json.dumps(sent[0]["result"])
    assert attempt.native_tool_call is None and not attempt.native_action
    assert any(
        event.phase == "completed" and event.outcome == "success_verified"
        and event.tool_name == "search_public_web"
        for event in attempt.capability_events
    )


def test_unavailable_search_uses_only_the_other_http_source(monkeypatch):
    urls = []

    def request(url, accept, limit, deadline):
        urls.append(urlsplit(url).hostname)
        if len(urls) == 1:
            raise OSError("offline")
        body = f'<a class="result__a" href="{_RESULT_URL}">Setup</a>'
        return public_web._Response("text/html", body=body.encode())

    monkeypatch.setattr(public_web, "_request_once", request)
    result = public_web.search_public_web("application setup")
    assert urls == ["www.bing.com", "html.duckduckgo.com"]
    assert result["source"] == "DuckDuckGo" and _RESULT_URL in result["text"]


def test_public_read_validates_redirects_before_sending_another_request(monkeypatch):
    visited = []

    def request(url, accept, limit, deadline):
        visited.append(url)
        return public_web._Response(redirect="http://169.254.169.254/latest")

    monkeypatch.setattr(public_web, "_request_once", request)
    result = public_web.read_public_web_page(_RESULT_URL)
    assert not result["ok"] and result["error"]["code"] == "public_url_required"
    assert visited == [_RESULT_URL]


def test_hostname_with_mixed_public_and_private_dns_answers_never_connects(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.2", 443)),
    ])
    monkeypatch.setattr(socket, "create_connection", lambda *args: pytest.fail("Attempted unsafe connection"))
    result = public_web.read_public_web_page(_RESULT_URL)
    assert not result["ok"] and result["error"]["code"] == "public_address_required"


def test_page_reader_connects_to_checked_ip_with_original_tls_and_host(monkeypatch):
    observed = []
    raw_socket = object()

    class Response(io.BytesIO):
        status = 200
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        length = None

        def getheader(self, name, default=None):
            return self.headers.get(name, default)

    class Connection:
        def __init__(self, host, port):
            self.sock = None

        def request(self, method, target, *, headers):
            observed.append((method, target, headers["Host"]))
            assert "Cookie" not in headers and "Authorization" not in headers

        def getresponse(self):
            return Response(b"<head><title>Setup</title></head><main><script>hidden_code()</script><p>Install from the application library.</p></main>")

        def close(self):
            pass

    class TLS:
        def wrap_socket(self, socket_, *, server_hostname):
            assert socket_ is raw_socket
            observed.append(server_hostname)
            return self

        def settimeout(self, timeout):
            assert timeout > 0

    def connect(address, timeout):
        observed.append(address)
        return raw_socket

    monkeypatch.setattr(public_web, "_resolve_public_addresses", lambda host, port: ("8.8.8.8",))
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(public_web, "_tls_context", TLS)
    monkeypatch.setattr(public_web.http.client, "HTTPConnection", Connection)
    result = public_web.read_public_web_page(_RESULT_URL)
    assert observed == [("8.8.8.8", 443), "docs.example.com", ("GET", "/application/install", "docs.example.com")]
    assert result["ok"] and "Install from the application library." in result["text"]
    assert "hidden_code" not in result["text"]
