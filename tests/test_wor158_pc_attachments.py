"""Deterministic PC attachment-draft and atomic-composer regressions."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["OSIA_REDUCED_MOTION"] = "1"

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from osia.attachments import MAX_ATTACHMENT_BYTES
from osia.desktop_attachments import (
    DesktopAttachmentError,
    prepare_desktop_turn,
    stage_attachment,
)
from osia.gui.main_popup import MainPopup
from osia import main as osia_main


PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-png"
GIF = b"GIF89a" + b"synthetic-gif"
_QT_APP = QApplication.instance() or QApplication([])


def _write(path: Path, data: bytes = b"synthetic") -> Path:
    path.write_bytes(data)
    return path


def _popup() -> MainPopup:
    popup = MainPopup()
    popup._stack.setCurrentIndex(popup.PAGE_CHAT)
    popup.show()
    _QT_APP.processEvents()
    return popup


def _stage_in_chat(popup: MainPopup, paths: list[Path]) -> None:
    with patch(
        "osia.gui.main_popup.QFileDialog.getOpenFileNames",
        return_value=([str(path) for path in paths], ""),
    ):
        QTest.mouseClick(popup._chat._file_btn, Qt.MouseButton.LeftButton)
    _QT_APP.processEvents()


def _close(popup: MainPopup) -> None:
    popup.close()
    popup.deleteLater()
    _QT_APP.processEvents()


def test_selecting_one_file_stages_without_send(tmp_path):
    popup = _popup()
    try:
        sends = QSignalSpy(popup.send_text)
        drafts = QSignalSpy(popup.submit_draft)
        legacy_picks = QSignalSpy(popup.pick_file)
        _stage_in_chat(popup, [_write(tmp_path / "one.txt")])
        assert len(sends) == len(drafts) == len(legacy_picks) == 0
        assert len(popup._chat.pending_attachments()) == 1
        assert popup._chat._attachment_layout.count() == 1
    finally:
        _close(popup)


def test_multiple_files_are_ordered_and_duplicates_are_intentional(tmp_path):
    popup = _popup()
    try:
        first = _write(tmp_path / "first.txt")
        second = _write(tmp_path / "second.txt")
        _stage_in_chat(popup, [first, second, first])
        pending = popup._chat.pending_attachments()
        assert [item.display_name for item in pending] == [
            "first.txt", "second.txt", "first.txt",
        ]
        assert [item.logical_order for item in pending] == [0, 1, 2]
        assert len({item.draft_id for item in pending}) == 3
    finally:
        _close(popup)


def test_text_before_and_after_selection_is_preserved(tmp_path):
    popup = _popup()
    try:
        popup._chat._input.setText("before")
        _stage_in_chat(popup, [_write(tmp_path / "draft.txt")])
        assert popup._chat._input.text() == "before"
        popup._chat._input.setText("after")
        assert popup._chat._input.text() == "after"
        assert len(popup._chat.pending_attachments()) == 1
    finally:
        _close(popup)


def test_text_and_multiple_attachments_emit_one_atomic_draft(tmp_path):
    popup = _popup()
    try:
        draft_spy = QSignalSpy(popup.submit_draft)
        text_spy = QSignalSpy(popup.send_text)
        _stage_in_chat(
            popup,
            [_write(tmp_path / "one.txt"), _write(tmp_path / "two.txt")],
        )
        popup._chat._input.setText("one request")
        QTest.mouseClick(popup._chat._send_btn, Qt.MouseButton.LeftButton)
        assert len(draft_spy) == 1
        assert len(text_spy) == 0
        assert draft_spy[0][0] == "one request"
        assert [item.display_name for item in draft_spy[0][1]] == [
            "one.txt", "two.txt",
        ]
        # The UI is cleared only by the host's explicit acceptance callback.
        assert popup._chat._input.text() == "one request"
        assert len(popup._chat.pending_attachments()) == 2
    finally:
        _close(popup)


def test_attachments_only_send_and_acceptance_clear_once(tmp_path):
    popup = _popup()
    try:
        draft_spy = QSignalSpy(popup.submit_draft)
        _stage_in_chat(popup, [_write(tmp_path / "only.txt")])
        assert popup._chat._send_btn.isEnabled()
        QTest.mouseClick(popup._chat._send_btn, Qt.MouseButton.LeftButton)
        ids = tuple(item.draft_id for item in draft_spy[0][1])
        assert draft_spy[0][0] == ""
        assert popup.accept_draft_submission(ids)
        assert not popup._chat.pending_attachments()
        assert popup._chat._input.text() == ""
        assert not popup.accept_draft_submission(ids)
        QTest.mouseClick(popup._chat._send_btn, Qt.MouseButton.LeftButton)
        assert len(draft_spy) == 1
    finally:
        _close(popup)


def test_remove_deletes_only_the_selected_item(tmp_path):
    popup = _popup()
    try:
        _stage_in_chat(
            popup,
            [_write(tmp_path / "one.txt"), _write(tmp_path / "two.txt")],
        )
        first_chip = popup._chat._attachment_layout.itemAt(0).widget()
        remove = first_chip.findChild(QPushButton, "attachmentRemove")
        QTest.mouseClick(remove, Qt.MouseButton.LeftButton)
        assert [item.display_name for item in popup._chat.pending_attachments()] == [
            "two.txt",
        ]
    finally:
        _close(popup)


def test_preflight_failure_marks_and_keeps_complete_draft(tmp_path):
    popup = _popup()
    try:
        _stage_in_chat(popup, [_write(tmp_path / "image.png", PNG)])
        popup._chat._input.setText("keep me")
        item = popup._chat.pending_attachments()[0]
        error = DesktopAttachmentError(
            "unsupported_model",
            attachment_id=item.draft_id,
            display_name=item.display_name,
        )
        popup.show_attachment_error(error)
        assert popup._chat._input.text() == "keep me"
        assert popup._chat.pending_attachments()[0].validation_state == "error"
        assert popup._chat._attachment_layout.count() == 1
    finally:
        _close(popup)


def test_missing_and_changed_sources_reject_before_provider(tmp_path):
    missing_path = _write(tmp_path / "missing.txt")
    missing = stage_attachment(missing_path, 0)
    missing_path.unlink()
    with pytest.raises(DesktopAttachmentError, match="missing_file"):
        prepare_desktop_turn("read", [missing], provider="anthropic", model="x")

    changed_path = _write(tmp_path / "changed.txt")
    changed = stage_attachment(changed_path, 0)
    changed_path.write_bytes(b"different-size")
    with pytest.raises(DesktopAttachmentError, match="changed_file"):
        prepare_desktop_turn("read", [changed], provider="anthropic", model="x")


@pytest.mark.parametrize(
    ("provider", "model", "data", "code"),
    [
        ("anthropic", "claude-opus-4-8", PNG, "unsupported_provider"),
        ("openai", "unknown-model", PNG, "unsupported_model"),
        (
            "groq",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            GIF,
            "unsupported_mime",
        ),
    ],
)
def test_provider_model_and_mime_rejections(tmp_path, provider, model, data, code):
    item = stage_attachment(_write(tmp_path / "image.bin", data), 0)
    with pytest.raises(DesktopAttachmentError, match=code):
        prepare_desktop_turn("inspect", [item], provider=provider, model=model)


def test_image_reaches_wor157_multimodal_block_once(tmp_path):
    item = stage_attachment(_write(tmp_path / "image.png", PNG), 0)
    prepared = prepare_desktop_turn(
        "inspect",
        [item],
        provider="openai",
        model="gpt-5.5",
    )
    assert isinstance(prepared.provider_content, list)
    assert [part["type"] for part in prepared.provider_content] == [
        "text", "image_url",
    ]
    assert prepared.provider_content[1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert prepared.attachment_ids == (item.draft_id,)


def test_generic_file_uses_existing_type_file_path_without_eager_read(tmp_path):
    source = _write(tmp_path / "document.xyz", b"generic-data")
    item = stage_attachment(source, 0)
    prepared = prepare_desktop_turn(
        "summarize", [item], provider="anthropic", model="claude-opus-4-8",
    )
    assert isinstance(prepared.provider_content, str)
    assert "type:file" in prepared.provider_content
    assert str(source.resolve()) in prepared.provider_content
    assert str(source.parent) not in prepared.display_text
    assert "document.xyz" in prepared.display_text


def test_visible_ui_and_tooltips_never_contain_private_directory(tmp_path):
    popup = _popup()
    try:
        source = _write(tmp_path / "visible-name.txt")
        _stage_in_chat(popup, [source])
        chip = popup._chat._attachment_layout.itemAt(0).widget()
        visible = "\n".join(
            label.text() for label in chip.findChildren(QLabel)
        )
        tooltips = "\n".join(
            widget.toolTip() for widget in chip.findChildren(QPushButton)
        )
        assert "visible-name.txt" in visible
        assert str(tmp_path) not in visible
        assert str(tmp_path) not in tooltips
    finally:
        _close(popup)


def test_staging_and_preflight_do_not_log_paths_or_base64(tmp_path):
    source = _write(tmp_path / "quiet.png", PNG)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        item = stage_attachment(source, 0)
        prepare_desktop_turn(
            "inspect", [item], provider="openai", model="gpt-5.5",
        )
    logged = output.getvalue()
    assert str(tmp_path) not in logged
    assert "base64" not in logged


def test_persisted_attachment_turn_uses_only_path_free_display_text(tmp_path):
    source = _write(tmp_path / "private-document.txt")
    item = stage_attachment(source, 0)
    prepared = prepare_desktop_turn(
        "summarize", [item], provider="anthropic", model="claude-opus-4-8",
    )
    agent = type("Agent", (), {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": prepared.provider_content},
            {"role": "assistant", "content": "done"},
        ],
    })()
    persisted = []
    with patch.object(
        osia_main.conversations,
        "add_message",
        side_effect=lambda conv, role, content: persisted.append((conv, role, content)),
    ):
        osia_main._persist_agent_delta(
            "conversation",
            agent,
            0,
            user_display_text=prepared.display_text,
        )
    assert persisted[0] == ("conversation", "user", prepared.display_text)
    assert str(tmp_path) not in persisted[0][2]
    assert "base64" not in persisted[0][2]


def test_conversation_switch_restores_separate_text_and_attachments(tmp_path):
    popup = _popup()
    try:
        popup.refresh_conversations("conversation-a")
        popup._chat._input.setText("draft a")
        _stage_in_chat(popup, [_write(tmp_path / "a.txt")])
        popup.refresh_conversations("conversation-b")
        assert popup._chat._input.text() == ""
        assert not popup._chat.pending_attachments()
        popup._chat._input.setText("draft b")
        popup.refresh_conversations("conversation-a")
        assert popup._chat._input.text() == "draft a"
        assert [item.display_name for item in popup._chat.pending_attachments()] == [
            "a.txt",
        ]
        popup.refresh_conversations("conversation-b")
        assert popup._chat._input.text() == "draft b"
        assert not popup._chat.pending_attachments()
    finally:
        _close(popup)


def test_unbound_first_message_draft_moves_to_lazy_conversation(tmp_path):
    popup = _popup()
    try:
        popup._chat._input.setText("first message")
        _stage_in_chat(popup, [_write(tmp_path / "first.txt")])
        ids = tuple(
            item.draft_id for item in popup._chat.pending_attachments()
        )
        # Conversation creation refreshes the active context first.
        popup.refresh_conversations("new-conversation")
        assert not popup._chat.pending_attachments()
        popup.bind_unbound_draft("new-conversation")
        assert tuple(
            item.draft_id for item in popup._chat.pending_attachments()
        ) == ids
        assert popup._chat._input.text() == "first message"
    finally:
        _close(popup)


def test_close_reopen_and_fresh_process_widget_never_send_implicitly(tmp_path):
    popup = _popup()
    sends = QSignalSpy(popup.submit_draft)
    _stage_in_chat(popup, [_write(tmp_path / "close.txt")])
    popup._hide_animated()
    _QT_APP.processEvents()
    popup._show_animated()
    _QT_APP.processEvents()
    assert len(sends) == 0
    assert len(popup._chat.pending_attachments()) == 1
    _close(popup)

    restarted = _popup()
    try:
        assert not restarted._chat.pending_attachments()
        assert restarted._chat._input.text() == ""
    finally:
        _close(restarted)


def test_execution_failure_after_acceptance_does_not_reintroduce_draft(tmp_path):
    popup = _popup()
    try:
        _stage_in_chat(popup, [_write(tmp_path / "once.txt")])
        item = popup._chat.pending_attachments()[0]
        assert popup.accept_draft_submission((item.draft_id,))
        popup.show_attachment_error(
            DesktopAttachmentError(
                "unreadable_file",
                attachment_id=item.draft_id,
                display_name=item.display_name,
            )
        )
        assert not popup._chat.pending_attachments()
        assert popup._chat._input.text() == ""
    finally:
        _close(popup)


def test_sparse_file_over_20_mib_stages_with_bounded_selection_memory(tmp_path):
    source = tmp_path / "large.bin"
    with source.open("wb") as handle:
        handle.write(b"not-an-image")
        handle.truncate((20 * 1024 * 1024) + 1)
    tracemalloc.start()
    item = stage_attachment(source, 0)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert item.size_bytes > 20 * 1024 * 1024
    assert item.attachment_type == "file"
    assert peak < 2 * 1024 * 1024


def test_provider_size_rejection_occurs_before_large_image_read(tmp_path):
    source = tmp_path / "large.png"
    with source.open("wb") as handle:
        handle.write(PNG)
        handle.truncate(16 * 1024 * 1024)
    item = stage_attachment(source, 0)
    with patch.object(Path, "read_bytes", side_effect=AssertionError("eager read")):
        with pytest.raises(DesktopAttachmentError, match="provider_size_limit"):
            prepare_desktop_turn(
                "inspect",
                [item],
                provider="groq",
                model="meta-llama/llama-4-scout-17b-16e-instruct",
            )


def test_mobile_20_mib_policy_is_unchanged():
    assert MAX_ATTACHMENT_BYTES == 20 * 1024 * 1024


def test_enter_sends_once_and_shift_enter_inserts_newline(tmp_path):
    popup = _popup()
    try:
        sends = QSignalSpy(popup.submit_draft)
        _stage_in_chat(popup, [_write(tmp_path / "keyboard.txt")])
        popup._chat._input.setFocus()
        QTest.keyClicks(popup._chat._input, "line one")
        QTest.keyClick(
            popup._chat._input,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.ShiftModifier,
        )
        QTest.keyClicks(popup._chat._input, "line two")
        assert popup._chat._input.text() == "line one\nline two"
        QTest.keyClick(popup._chat._input, Qt.Key.Key_Return)
        assert len(sends) == 1
        assert sends[0][0] == "line one\nline two"
    finally:
        _close(popup)


def test_attachment_controls_have_accessible_names_and_input_regains_focus(tmp_path):
    popup = _popup()
    try:
        _stage_in_chat(popup, [_write(tmp_path / "accessible.txt")])
        chip = popup._chat._attachment_layout.itemAt(0).widget()
        remove = chip.findChild(QPushButton, "attachmentRemove")
        assert chip.accessibleName()
        assert "accessible.txt" in remove.accessibleName()
        QTest.mouseClick(remove, Qt.MouseButton.LeftButton)
        assert popup._chat._input.hasFocus()
    finally:
        _close(popup)


def test_source_and_frozen_collection_include_desktop_draft_module():
    root = Path(__file__).resolve().parent.parent
    spec = (root / "packaging" / "windows" / "osia.spec").read_text("utf-8")
    main = (root / "osia" / "main.py").read_text("utf-8")
    assert 'collect_submodules(\n    "osia"' in spec
    assert "from osia.desktop_attachments import" in main
    assert "popup.submit_draft.connect(handle_submit_draft)" in main


def test_no_pc_product_cap_or_raw_draft_serialization_contract():
    module = Path(sys.modules["osia.desktop_attachments"].__file__).read_text("utf-8")
    assert "MAX_ATTACHMENT_BYTES" not in module
    assert "read_bytes()" not in module
    assert "json.dump" not in module
    assert "base64.b64encode" in module  # send-time provider preparation only
