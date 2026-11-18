from types import SimpleNamespace

from osia.gui.chat_status_view import status_lines


def account_status():
    return {
        "command_id": "status.info", "connection": "account",
        "model": {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol"},
        "context": {"used": 1200, "maximum": 828400, "estimated": False},
        "quota": {"state": "available", "windows": [{"label": "5h", "remaining_percent": 0}]},
        "usage": None,
        "connectors": [{"name": "PRIVATE_CONNECTOR"}], "reasoning": "PRIVATE_REASONING",
    }


def test_account_view_only_renders_model_context_and_remaining_quota():
    lines = status_lines(account_status(), "fr")
    assert len(lines) == 3
    assert "GPT-6.1 Sol" in lines[0]
    assert "1 200 / 828 400" in lines[1] and "≈" not in lines[1]
    assert "0 %" in lines[2]
    assert "PRIVATE" not in " ".join(lines)
    assert "Coût" not in " ".join(lines)


def test_api_view_labels_estimates_and_never_turns_unknown_prices_into_zero():
    payload = account_status()
    payload.update(connection="api", quota=None, usage={
        "total_tokens": 42, "cost_usd": None,
        "models": [{"model": {"label": "Model A"}, "total_tokens": 22, "cost_usd": .000001},
                   {"model": {"label": "Model B"}, "total_tokens": 20, "cost_usd": None}],
    })
    text = "\n".join(status_lines(payload, "fr"))
    assert "estimation" in text and "42" in text
    assert "Coût estimé : indisponible" in text
    assert "Model A" in text and "0.000001 USD" in text
    assert "Model B" in text and "0 USD" not in text
    assert "Quota" not in text


def test_status_dialog_is_plain_text_without_old_cards(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget
    from osia.gui.main_popup import MainPopup

    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    captured = []
    def inspect(dialog):
        labels = dialog.findChildren(QLabel)
        captured.extend(label.text() for label in labels)
        assert all(label.textFormat() == Qt.TextFormat.PlainText for label in labels)
        assert not dialog.findChildren(QWidget, "slashStatusCard")
        return 0
    monkeypatch.setattr(QDialog, "exec", inspect)
    try:
        MainPopup.show_slash_information(parent, account_status())
        assert len(captured) == 3
    finally:
        parent.close()


def test_current_intro_guide_teaches_both_slash_commands_and_past_chat_references():
    from osia.gui.guide_slideshow import _SLIDES

    for language in ("en", "fr"):
        text = " ".join(title + " " + body for title, body in _SLIDES[language])
        assert "/" in text and "@" in text
        assert "context" in text
