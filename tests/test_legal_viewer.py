import unittest

try:
    from PyQt6.QtWidgets import QApplication, QComboBox, QTextBrowser
    from osia.gui.legal_viewer import LegalDocumentDialog
except ImportError:
    QApplication = None
    QComboBox = None
    QTextBrowser = None
    LegalDocumentDialog = None


class LegalViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication is None:
            raise unittest.SkipTest("PyQt6 is not installed in this test environment")
        cls.application = QApplication.instance() or QApplication([])

    def test_terms_and_privacy_render_from_the_offline_bundle(self):
        for key, expected in (
            ("terms", "35. Contact"),
            ("privacy", "38. Contact"),
        ):
            with self.subTest(document=key):
                dialog = LegalDocumentDialog(None, key)
                reader = dialog.findChild(QTextBrowser)
                self.assertIsNotNone(reader)
                rendered = reader.toMarkdown()
                self.assertIn(expected, rendered)
                self.assertIn("Aymen Gherbi", rendered)
                self.assertIn("2026-10-05.4", dialog._metadata.text())
                dialog.close()

    def test_language_selector_switches_the_offline_copy(self):
        dialog = LegalDocumentDialog(None, "privacy")
        reader = dialog.findChild(QTextBrowser)
        selector = dialog.findChild(QComboBox)
        self.assertIsNotNone(reader)
        self.assertIsNotNone(selector)

        selector.setCurrentIndex(1)
        rendered = reader.toMarkdown()
        self.assertIn("1. Champ d’application", rendered)
        self.assertIn("38. Contact", rendered)
        self.assertNotIn("Brouillon", rendered)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
