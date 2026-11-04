import os
import unittest
from unittest.mock import call, patch

from osia import i18n, legal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication, QPushButton
except ImportError:
    QApplication = None

if QApplication is not None:
    from osia.gui.legal_dialog import LegalAcceptanceDialog, maybe_show_release_legal_gate
    from osia.gui.main_popup import _SettingsPanel


class LegalLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication is None:
            raise unittest.SkipTest("PyQt6 is not installed in this test environment")
        cls.application = QApplication.instance() or QApplication([])

    def test_settings_buttons_open_official_pages_in_the_current_language(self):
        with (
            patch.object(_SettingsPanel, "_configure_chatgpt_file_watcher"),
            patch.object(_SettingsPanel, "_load_state"),
        ):
            panel = _SettingsPanel()
        try:
            for locale in ("fr", "en"):
                with (
                    self.subTest(locale=locale),
                    patch.object(i18n, "get_language", return_value=locale),
                    patch.object(legal.webbrowser, "open") as open_url,
                ):
                    panel._legal_terms_btn.click()
                    panel._legal_privacy_btn.click()
                    panel._legal_notice_btn.click()
                    urls = legal.LEGAL_MANIFEST["canonicalUrls"][locale]
                    self.assertEqual(
                        open_url.call_args_list,
                        [call(urls[name]) for name in ("terms", "privacy", "legalNotice")],
                    )
        finally:
            panel.close()
            panel.deleteLater()

    def test_acceptance_dialog_buttons_open_the_same_official_pages(self):
        with (
            patch.object(i18n, "get_language", return_value="en"),
            patch.object(legal.webbrowser, "open") as open_url,
        ):
            dialog = LegalAcceptanceDialog()
            try:
                buttons = {
                    button.text(): button for button in dialog.findChildren(QPushButton)
                }
                buttons[i18n.t("legal.open_terms")].click()
                buttons[i18n.t("legal.open_privacy")].click()
                self.assertEqual(
                    open_url.call_args_list,
                    [
                        call("https://osia.space/en/legal/terms"),
                        call("https://osia.space/en/legal/privacy"),
                    ],
                )
                self.assertFalse(dialog.ok_button.isEnabled())
            finally:
                dialog.close()
                dialog.deleteLater()

    def test_release_privacy_notice_opens_online(self):
        with (
            patch.object(legal, "load_acceptance"),
            patch.object(
                legal, "startup_decision", return_value=legal.StartupDecision(False, True),
            ),
            patch.object(legal, "record_privacy_seen") as record_seen,
            patch.object(i18n, "get_language", return_value="fr"),
            patch.object(legal.webbrowser, "open") as open_url,
        ):
            self.assertTrue(maybe_show_release_legal_gate())
            open_url.assert_called_once_with("https://osia.space/legal/privacy")
            record_seen.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
