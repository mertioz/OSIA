import os
import threading
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from osia.gui.main_popup import _ChatPage
from osia.slash_commands import CapabilityRequirements, ModelOverride


_APP = QApplication.instance() or QApplication([])


def test_new_commands_use_the_desktop_palette_without_sending_literal_slashes(monkeypatch):
    from osia import personality
    from osia.gui import slash_palette

    monkeypatch.setattr(slash_palette, "connector_availability", lambda: ())
    monkeypatch.setattr(personality, "discover_skills", lambda: [
        SimpleNamespace(filename="brief.md", description="Make a brief", available=True),
    ])
    chat = _ChatPage()
    chat.show()
    special = QSignalSpy(chat.special_requested)
    typed = QSignalSpy(chat.submit_directive_draft)
    legacy = QSignalSpy(chat.send_text)
    try:
        chat._input.setPlainText("/")
        assert {"vision", "skill", "skillcreator"} <= {
            row.get("canonical_alias") for row in chat._slash_palette._rows
        }
        chat._input.setPlainText("/vision")
        chat._on_send()
        assert len(special) == 1
        assert not typed and not legacy

        chat._input.setPlainText("/skill")
        chat._on_send()
        assert chat._slash_palette._rows[0]["skill_name"] == "brief.md"
        chat._slash_palette._list.itemClicked.emit(chat._slash_palette._list.item(0))
        chat._input.setPlainText("Summarize this.")
        chat._on_send()
        assert typed[0][2]["skill_name"] == "brief.md"
        assert chat.accept_directive_submission()

        chat._input.setPlainText("/skillcreator Write a meeting skill.")
        chat._on_send()
        assert chat._input.toPlainText() == "Write a meeting skill."
        chat._on_send()
        assert typed[1][2]["skill_creator"] is True
        assert not legacy
    finally:
        chat.close()
        chat.deleteLater()


def test_palette_keeps_filtering_while_snapshot_worker_waits(monkeypatch):
    from osia.gui import slash_palette

    entered = threading.Event()
    release = threading.Event()
    loads = []

    def slow_connectors():
        loads.append(threading.get_ident())
        entered.set()
        release.wait(3)
        return ()

    monkeypatch.setattr(slash_palette, "connector_availability", slow_connectors)
    monkeypatch.setattr(slash_palette, "current_web_available", lambda _provider: False)
    chat = _ChatPage()
    chat.show()
    try:
        chat._input.setPlainText("/")
        assert entered.wait(1)
        for text in ("/v", "/vi", "/vis", "/vision"):
            chat._input.setPlainText(text)
            assert chat._slash_palette._rows[0]["canonical_alias"] == "vision"
        assert loads != [threading.get_ident()]
        assert len(loads) == 1
    finally:
        release.set()
        chat.close()
        chat.deleteLater()


def test_model_search_reaches_later_entries_and_applies_model_and_effort_together():
    chat = _ChatPage()
    chat.show()
    chat._input.setFocus()
    palette = chat._slash_palette
    model = {
        "model_id": "gpt-6.1-sol", "model_display_name": "GPT-6.1 Sol",
        "provider_id": "chatgpt_account", "provider_display_name": "ChatGPT",
        "reasoning_options": [{"id": level} for level in ("low", "xhigh", "max", "ultra")],
        "picker_visible": True, "available": True,
    }
    palette.set_model_inventory([
        {**model, "model_id": f"model-{index}", "model_display_name": f"Model {index}"}
        for index in range(40)
    ] + [model])
    try:
        chat._input.setPlainText("/model use this text")
        chat._on_send()
        palette._filter.setText("6.1 sol")
        assert [row["model_id"] for row in palette._rows] == [model["model_id"]]
        palette.activate_current()
        assert chat._directives.is_empty
        assert [row["reasoning_level"] for row in palette._rows] == [
            "auto", "low", "xhigh", "max", "ultra",
        ]
        palette._list.itemClicked.emit(palette._list.item(3))
        assert chat._input.toPlainText() == "use this text"
        assert chat._directives.model_override == ModelOverride("chatgpt_account", "gpt-6.1-sol")
        assert chat._directives.reasoning_override == "max"
        chat._remove_directive("model")
        assert chat._directives.is_empty
    finally:
        chat.close()
        chat.deleteLater()


def test_at_reference_preserves_text_on_both_sides_of_the_caret(monkeypatch):
    from osia.gui import slash_palette

    monkeypatch.setattr(slash_palette, "conversation_availability", lambda **_kwargs: ({
        "conversation_id": "conv-other", "display_title": "Projet", "display_project": "",
    },))
    chat = _ChatPage()
    chat.show()
    try:
        prefix = "Reprends 🙂 @pro"
        chat._input.setPlainText(prefix + " demain")
        cursor = chat._input.textCursor()
        cursor.setPosition(len(prefix.encode("utf-16-le")) // 2)
        chat._input.setTextCursor(cursor)
        for _ in range(20):
            if chat._slash_palette._rows[0].get("conversation_id"):
                break
            QTest.qWait(10)
        assert chat._slash_palette.activate_current()
        assert chat._input.toPlainText() == "Reprends 🙂  demain"
        assert chat._directives.conversation_refs[0].conversation_id == "conv-other"
    finally:
        chat.close()
        chat.deleteLater()


def test_modifier_selection_creates_chip_and_enter_emits_typed_draft_only():
    chat = _ChatPage()
    typed = QSignalSpy(chat.submit_directive_draft)
    legacy = QSignalSpy(chat.send_text)
    chat._input.setPlainText("/web latest release")
    chat._on_slash_selection({
        "id": "web.current",
        "canonical_alias": "web",
        "handler_contract": "set_current_web_requirement_v1",
    })
    assert chat._input.toPlainText() == "latest release"
    assert chat._directives.capability_requirements.current_web is True
    assert chat._directive_host.isVisible() is False  # parent is not shown
    assert chat._directive_layout.count() == 1
    chat._on_send()
    assert len(typed) == 1
    assert len(legacy) == 0
    assert typed[0][0] == "latest release"
    assert typed[0][2]["capability_requirements"] == {"current_web": True}
    # Draft stays intact until the host confirms persistence/dispatch.
    assert chat._input.toPlainText() == "latest release"
    assert chat.accept_directive_submission(())
    assert chat._input.toPlainText() == ""
    assert chat._directives.is_empty
    chat.deleteLater()


def test_informational_selection_is_immediate_and_never_emits_chat_turn():
    chat = _ChatPage()
    info = QSignalSpy(chat.informational_requested)
    typed = QSignalSpy(chat.submit_directive_draft)
    legacy = QSignalSpy(chat.send_text)
    chat._input.setPlainText("/help")
    chat._on_slash_selection({
        "id": "help.info",
        "canonical_alias": "help",
        "handler_contract": "show_available_help_v1",
    })
    assert len(info) == 1
    assert info[0][0] == "help.info"
    assert len(typed) == 0
    assert len(legacy) == 0
    chat.deleteLater()


def test_pc_composer_draft_retains_structured_chips_per_conversation():
    chat = _ChatPage()
    chat.set_draft_context("conv-a")
    chat._input.setPlainText("/web body")
    chat._on_slash_selection({
        "id": "web.current",
        "canonical_alias": "web",
        "handler_contract": "set_current_web_requirement_v1",
    })
    chat.set_draft_context("conv-b")
    assert chat._directives.is_empty
    chat.set_draft_context("conv-a")
    assert chat._directives.capability_requirements.current_web is True
    assert chat._input.toPlainText() == "body"
    chat.deleteLater()


def test_partial_filter_selection_and_model_auto_remove_the_override():
    chat = _ChatPage()
    chat._directives = chat._directives.__class__(
        model_override=ModelOverride("openai", "gpt-5.4")
    )
    chat._input.setPlainText("/mo keep this body")
    chat._on_slash_selection({
        "selection_type": "model",
        "remove_override": True,
        "name": "Automatic",
    })
    assert chat._directives.model_override is None
    assert chat._input.toPlainText() == "keep this body"
    chat.deleteLater()


def test_informational_command_must_be_used_alone():
    chat = _ChatPage()
    info = QSignalSpy(chat.informational_requested)
    chat._directives = chat._directives.__class__(
        capability_requirements=CapabilityRequirements(current_web=True)
    )
    chat._input.setPlainText("/status trailing")
    chat._on_slash_selection({
        "id": "status.info",
        "canonical_alias": "status",
        "handler_contract": "show_safe_status_v1",
    })
    assert len(info) == 0
    assert chat._input.toPlainText() == "/status trailing"
    chat.deleteLater()


def test_desktop_palette_enter_selects_escape_closes_and_tab_traverses():
    chat = _ChatPage()
    chat.show()
    _APP.processEvents()
    palette = chat._slash_palette
    assert chat._input.tabChangesFocus() is True
    selected = QSignalSpy(palette.item_selected)
    row = {
        "id": "web.current",
        "canonical_alias": "web",
        "name": "Current web",
        "description": "",
        "category": "commands",
        "enabled": True,
        "handler_contract": "set_current_web_requirement_v1",
    }
    palette._show_rows([row], "Commands")
    assert palette._list.item(0).text() == "Commands"
    assert not bool(
        palette._list.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
    )
    assert palette._list.currentItem().data(Qt.ItemDataRole.UserRole) == row
    tab = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier)
    assert palette.handle_key(tab) is False
    enter = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier
    )
    assert palette.handle_key(enter) is True
    assert len(selected) == 1
    palette._show_rows([row], "Commands")
    escape = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    )
    assert palette.handle_key(escape) is True
    assert palette.isHidden()
    chat.close()
    chat.deleteLater()
