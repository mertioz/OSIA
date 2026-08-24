import io
import hashlib
import json
import types
import urllib.error

import pytest

from osia import (
    attachments,
    chat_routing,
    desktop_attachments,
    document_artifacts,
    document_sources,
)
from osia.agent import OSIAAgent
from osia.document_sources import DocumentSourceStore, owner_hash
from osia.server import server
from osia.turn_context import fixed_turn_context
from tests.wor187_fixtures import docx


def _routing_context(text, launcher):
    return {
        "launcher": launcher,
        "original_user_text": text,
        "provider_input": text,
        "history": [],
        "conversation_id": "conv_wor224_fixture",
        "request_id": "turn_wor224_fixture",
        "origin_ref": "mobile_fixture",
        "device_id": "device_fixture",
        "turn_context": fixed_turn_context("en", source="test"),
    }


def _install_round(agent, content):
    agent._stream_with_sentences = types.MethodType(
        lambda self, callback, *, on_first_token=None, guard=None: content,
        agent,
    )


@pytest.mark.parametrize(
    "provider,model",
    (
        ("chatgpt_account", "gpt-5.6-luna"),
        ("ollama", "fixture-compatible"),
    ),
)
def test_chat_handoff_snapshots_the_effective_turn_pair(provider, model):
    launches = []
    text = "Use Autonomous mode to audit, diagnose, implement, test, and review this project."
    agent = OSIAAgent(lang="en")
    agent.provider = provider
    agent.model = model
    agent._disable_hierarchy_for_turn = True
    agent._chat_autonomous_context = _routing_context(
        text,
        lambda request: launches.append(request) or {
            "status": "running",
            "provider_id": request["provider_id"],
            "model_id": request["model_id"],
        },
    )
    _install_round(
        agent,
        '<tool_call>{"tool":"start_autonomous_mission",'
        '"reason_code":"dependent_phases","confidence":0.99,'
        '"explicit_request":true}</tool_call>',
    )

    final = agent.run(text, history=[], on_sentence=lambda _text: None)

    assert "controls stay in this chat" in final
    assert len(launches) == 1
    assert (launches[0]["provider_id"], launches[0]["model_id"]) == (
        provider,
        model,
    )


def test_chat_launch_validates_inherited_pair_without_persisting_global(monkeypatch):
    request = {
        "objective": "Audit, diagnose, implement, test, and review the bounded fixture.",
        "source_refs": [],
        "request_id": "turn_wor224_server",
        "conversation_id": "conv_wor224_server",
        "origin_ref": "mobile_fixture",
        "device_id": "device_fixture",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-luna",
        "reason_code": "dependent_phases",
        "confidence": 0.99,
        "turn_context": fixed_turn_context("en", source="test"),
    }
    resolutions = []
    worker_calls = []

    def resolve(**kwargs):
        resolutions.append(kwargs)
        return {
            "provider_id": kwargs["provider_id"],
            "model_id": kwargs["model_id"],
            "fallback_occurred": False,
        }

    def worker(*args):
        worker_calls.append(args)
        server._autonomous["active"] = True
        server._autonomous["st"] = {
            "task_id": "task_wor224",
            "run_id": "run_wor224",
            "status": "in_progress",
            "run_state": "ready",
            "provider": request["provider_id"],
            "model": request["model_id"],
            "chat_route_request_id": request["request_id"],
        }

    original = dict(server._autonomous)
    try:
        monkeypatch.setattr(
            "osia.mobile_model_catalogue.resolve_current_autonomous_selection",
            resolve,
        )
        monkeypatch.setattr(server, "_acquire_single_runner", lambda: 224)
        monkeypatch.setattr(server, "_autonomous_start_worker", worker)
        with server._CHAT_ROUTE_REQUESTS_LOCK:
            server._CHAT_ROUTE_REQUESTS.clear()
        server._autonomous.update({"active": False, "st": None})

        first = server._launch_autonomous_from_chat(dict(request))
        replay = server._launch_autonomous_from_chat(dict(request))
    finally:
        server._autonomous.clear()
        server._autonomous.update(original)
        with server._CHAT_ROUTE_REQUESTS_LOCK:
            server._CHAT_ROUTE_REQUESTS.clear()

    assert first == replay
    assert first["status"] == "running"
    assert (first["provider_id"], first["model_id"]) == (
        "chatgpt_account",
        "gpt-5.6-luna",
    )
    assert len(worker_calls) == 1
    assert resolutions == [{
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-luna",
        "refresh_account": True,
        "strict": True,
        "persist": False,
    }]
    assert worker_calls[0][1] == "gpt-5.6-luna"
    assert worker_calls[0][9] == "chatgpt_account"


def test_model_and_transport_failures_have_distinct_localized_explanations():
    for language in ("en", "fr", "es", "de", "zh", "ja"):
        model_text = chat_routing.handoff_response_text(
            {"status": "failed", "error_code": "chat_model_unavailable"},
            language,
        )
        transport_text = chat_routing.handoff_response_text(
            {
                "status": "unavailable",
                "error_code": "runner_transport_unavailable",
            },
            language,
        )
        assert model_text.strip()
        assert transport_text.strip()
        assert model_text != transport_text


def test_desktop_loopback_preserves_pair_and_classifies_transport(monkeypatch):
    from osia import main

    captured = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return json.dumps({
                "status": "running",
                "provider_id": "chatgpt_account",
                "model_id": "gpt-5.6-luna",
            }).encode("utf-8")

    def open_ok(request, timeout):
        captured.append((json.loads(request.data.decode("utf-8")), timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", open_ok)
    request = {
        "objective": "Harmless loopback handoff",
        "source_refs": [],
        "request_id": "turn_wor224_loopback",
        "conversation_id": "conv_wor224_loopback",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-luna",
        "reason_code": "dependent_phases",
        "confidence": 0.99,
        "explicit_request": True,
        "turn_context": fixed_turn_context("en", source="test"),
    }
    result = main._launch_server_autonomous_from_chat(request)
    assert result["status"] == "running"
    assert captured[0][0]["provider_id"] == "chatgpt_account"
    assert captured[0][0]["model_id"] == "gpt-5.6-luna"

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("synthetic offline")
        ),
    )
    assert main._launch_server_autonomous_from_chat(request) == {
        "status": "unavailable",
        "error_code": "runner_transport_unavailable",
    }


def test_image_pdf_and_docx_refs_reach_the_run_and_remain_readable(
    tmp_path, monkeypatch,
):
    from PIL import Image
    from reportlab.pdfgen import canvas

    image_buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (12, 34, 56)).save(image_buffer, format="PNG")
    pdf_buffer = io.BytesIO()
    pdf_canvas = canvas.Canvas(pdf_buffer, pagesize=(72, 72))
    pdf_canvas.drawString(8, 36, "WOR-224")
    pdf_canvas.save()
    payloads = {
        "safe.png": image_buffer.getvalue(),
        "safe.pdf": pdf_buffer.getvalue(),
        "safe.docx": docx(),
    }
    store = DocumentSourceStore(tmp_path / "sources")
    permission_epoch = 224
    committed = []
    mime_by_name = {
        "safe.png": ("image", "image/png"),
        "safe.pdf": ("document", "application/pdf"),
        "safe.docx": (
            "document",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    }
    for logical_order, (name, payload) in enumerate(payloads.items()):
        path = tmp_path / name
        path.write_bytes(payload)
        category, mime = mime_by_name[name]
        committed.append(attachments.CommittedAttachment(
            attachment_id="att_" + f"{logical_order + 1:032x}",
            file_id="file_" + f"{logical_order + 1:032x}",
            logical_order=logical_order,
            display_name=name,
            path=path,
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            detected_category=category,
            detected_mime=mime,
        ))
    monkeypatch.setattr(document_sources, "get_source_store", lambda: store)
    provider_input = attachments.build_agent_input(
        "Read every attachment.",
        committed,
        owner_id="conv_wor224_docs",
        permission_epoch=permission_epoch,
    )
    refs = list(chat_routing.extract_source_refs(provider_input))
    assert len(refs) == 3
    captured = []

    class _PolicyStore:
        @staticmethod
        def load_state():
            return types.SimpleNamespace(policy_epoch=permission_epoch)

    def worker(*args):
        inventory = args[5]
        bound = store.bind_for_run(
            [item["source_ref"] for item in inventory],
            owner_id="conv_wor224_docs",
            run_owner_id="task_wor224_docs",
            run_id="run_wor224_docs",
            permission_epoch=permission_epoch,
        )
        captured.extend(bound)
        server._autonomous["active"] = True
        server._autonomous["st"] = {
            "task_id": "task_wor224_docs",
            "run_id": "run_wor224_docs",
            "status": "in_progress",
            "run_state": "ready",
            "provider": "chatgpt_account",
            "model": "gpt-5.6-luna",
            "chat_route_request_id": "turn_wor224_docs",
        }

    original = dict(server._autonomous)
    try:
        monkeypatch.setattr(
            "osia.mobile_model_catalogue.resolve_current_autonomous_selection",
            lambda **_kwargs: {
                "provider_id": "chatgpt_account",
                "model_id": "gpt-5.6-luna",
                "fallback_occurred": False,
            },
        )
        monkeypatch.setattr(
            "osia.action_policy.get_default_permission_policy",
            lambda: types.SimpleNamespace(store=_PolicyStore()),
        )
        monkeypatch.setattr(server, "_acquire_single_runner", lambda: 224)
        monkeypatch.setattr(server, "_autonomous_start_worker", worker)
        with server._CHAT_ROUTE_REQUESTS_LOCK:
            server._CHAT_ROUTE_REQUESTS.clear()
        server._autonomous.update({"active": False, "st": None})

        result = server._launch_autonomous_from_chat({
            "objective": "Read the image, PDF, and DOCX, then report their formats.",
            "source_refs": refs,
            "request_id": "turn_wor224_docs",
            "conversation_id": "conv_wor224_docs",
            "provider_id": "chatgpt_account",
            "model_id": "gpt-5.6-luna",
            "reason_code": "dependent_phases",
            "confidence": 0.99,
        })
    finally:
        server._autonomous.clear()
        server._autonomous.update(original)
        with server._CHAT_ROUTE_REQUESTS_LOCK:
            server._CHAT_ROUTE_REQUESTS.clear()

    assert result["status"] == "running"
    assert [item["source_ref"] for item in captured] == refs
    assert [item["type_label"] for item in captured] == ["png", "pdf", "docx"]
    with document_artifacts.artifact_access_scope("task_wor224_docs"):
        for ref, expected in zip(refs, payloads.values()):
            data, metadata = store.resolve(ref)
            assert data == expected
            assert metadata["bound_run_hash"] == owner_hash("run_wor224_docs")


def test_desktop_chat_source_is_stamped_for_autonomous_binding(
    tmp_path, monkeypatch,
):
    from reportlab.pdfgen import canvas

    path = tmp_path / "desktop-safe.pdf"
    pdf_canvas = canvas.Canvas(str(path), pagesize=(72, 72))
    pdf_canvas.drawString(8, 36, "WOR-224 desktop")
    pdf_canvas.save()
    store = DocumentSourceStore(tmp_path / "desktop-sources")
    monkeypatch.setattr(document_sources, "get_source_store", lambda: store)

    prepared = desktop_attachments.prepare_desktop_turn(
        "Use Autonomous mode to inspect this PDF.",
        [desktop_attachments.stage_attachment(path, 0)],
        provider="chatgpt_account",
        model="gpt-5.6-luna",
        owner_id="conv_wor224_desktop",
        permission_epoch=225,
    )
    refs = list(chat_routing.extract_source_refs(prepared.provider_content))

    assert len(refs) == 1
    inventory = store.inspect_for_binding(
        refs,
        owner_id="conv_wor224_desktop",
        permission_epoch=225,
    )
    assert inventory[0]["type_label"] == "pdf"


def test_mobile_projection_and_pc_panel_show_the_checkpoint_pair():
    state = {
        "task_id": "task_wor224_projection",
        "run_id": "run_wor224_projection",
        "status": "in_progress",
        "run_state": "running",
        "provider": "chatgpt_account",
        "model": "gpt-5.6-luna",
        "phases": [],
        "cursor": {"phase": 0, "task": 0},
    }
    mobile = server._autonomous_snapshot(state)
    assert (mobile["provider"], mobile["model"]) == (
        "chatgpt_account",
        "gpt-5.6-luna",
    )

    from PyQt6.QtWidgets import QApplication
    from osia.gui.main_popup import _AutonomousPanel

    app = QApplication.instance() or QApplication([])
    panel = _AutonomousPanel()
    try:
        panel.seed_from_state(state)
        assert panel._prog_model.isVisibleTo(panel)
        assert "chatgpt_account — gpt-5.6-luna" in panel._prog_model.text()
    finally:
        panel.deleteLater()
        app.processEvents()
