"""Focused WOR-171 desktop QR-pairing UI regressions."""

from __future__ import annotations

import types

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton

from osia.gui import main_popup


_QT_APP = QApplication.instance() or QApplication([])
_TEST_TOKEN = "0123456789abcdefabcd"


class _DormantThread:
    def __init__(self, *args, target=None, **_kwargs):
        self.target = target if target is not None else (args[0] if args else None)

    def start(self) -> None:
        return None


@pytest.fixture()
def settings_panel(monkeypatch):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    monkeypatch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    monkeypatch.setattr(main_popup, "load_mobile_token", lambda: _TEST_TOKEN)
    panel = main_popup._SettingsPanel()
    panel.resize(640, 560)
    panel.show()
    _QT_APP.processEvents()
    yield panel
    panel.close()
    panel.deleteLater()
    _QT_APP.processEvents()


def test_qr_encoder_receives_exact_active_token(monkeypatch):
    encoded = []

    class _FakeQr:
        def __init__(self, **_kwargs):
            pass

        def add_data(self, payload, *, optimize):
            encoded.append((payload, optimize))

        def make(self, *, fit):
            assert fit is True

        def get_matrix(self):
            return [
                [True, False, True, False, True],
                [False, True, False, True, False],
                [True, False, True, False, True],
                [False, True, False, True, False],
                [True, False, True, False, True],
            ]

    monkeypatch.setattr(main_popup.qrcode, "QRCode", _FakeQr)

    pixmap = main_popup._pairing_qr_pixmap(_TEST_TOKEN, target_size=50)

    assert encoded == [(_TEST_TOKEN, 0)]
    assert not pixmap.isNull()


def test_qr_action_is_present_and_uses_current_token(settings_panel, monkeypatch):
    opened = []

    class _FakeDialog:
        def __init__(self, parent, token, anchor=None):
            opened.append((parent, token, anchor))

        def exec(self):
            return 0

    monkeypatch.setattr(main_popup, "_PairingQrDialog", _FakeDialog)

    assert settings_panel._mobile_show_btn.text() == main_popup.i18n.t(
        "settings.show"
    )
    assert settings_panel._mobile_regen_btn.text() == main_popup.i18n.t(
        "settings.regenerate"
    )
    assert settings_panel._mobile_qr_btn.text() == main_popup.i18n.t(
        "settings.show_qr"
    )

    QTest.mouseClick(settings_panel._mobile_qr_btn, Qt.MouseButton.LeftButton)

    assert opened == [
        (settings_panel, _TEST_TOKEN, settings_panel._mobile_qr_btn)
    ]


def test_qr_dialog_opens_closes_and_invalidates_after_rotation(monkeypatch):
    active_token = [_TEST_TOKEN]
    monkeypatch.setattr(
        main_popup, "load_mobile_token", lambda: active_token[0]
    )
    parent = main_popup.QWidget()
    dialog = main_popup._PairingQrDialog(parent, _TEST_TOKEN)
    dialog.show()
    _QT_APP.processEvents()

    assert dialog.isVisible()
    assert dialog._active is True
    assert not dialog._qr_label.pixmap().isNull()

    active_token[0] = "fedcba9876543210fedc"
    dialog._invalidate_if_rotated()

    assert dialog._active is False
    assert dialog._token == ""
    assert dialog._qr_label.pixmap().isNull()
    assert dialog._qr_label.text() == main_popup.i18n.t("settings.qr_inactive")

    close_button = dialog.findChild(QPushButton, "primaryBtn")
    assert close_button is not None
    QTest.mouseClick(close_button, Qt.MouseButton.LeftButton)
    _QT_APP.processEvents()
    assert not dialog.isVisible()
    parent.deleteLater()


def test_existing_token_dialog_still_displays_and_copies(monkeypatch):
    dialog = main_popup._TokenDialog(None, "Mobile token", _TEST_TOKEN)
    dialog.show()
    _QT_APP.processEvents()

    assert dialog._field.text() == _TEST_TOKEN
    QTest.mouseClick(dialog._copy_btn, Qt.MouseButton.LeftButton)
    assert QApplication.clipboard().text() == _TEST_TOKEN

    dialog.reject()
    dialog.deleteLater()
    _QT_APP.processEvents()
