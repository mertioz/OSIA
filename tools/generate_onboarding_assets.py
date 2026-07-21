"""Generate sanitized guide screenshots from real OSIA Qt settings widgets.

This is a maintainer tool, not an application entry point.  It creates a fresh
temporary profile, blocks probes/network/service actions, renders deterministic
OSIA-only PNGs, strips metadata with Pillow, and deletes the profile on exit.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
import types


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "assets" / "onboarding"
CAPTURE_SIZE = (900, 580)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _strip_png_metadata(path: Path) -> None:
    from PIL import Image

    with Image.open(path) as source:
        clean = source.convert("RGB")
        if clean.size != CAPTURE_SIZE:
            clean = clean.resize(CAPTURE_SIZE, Image.Resampling.LANCZOS)
        clean.save(path, format="PNG", optimize=True, compress_level=9)


def main() -> int:
    args = _parse_args()
    output = args.output.resolve()
    expected = DEFAULT_OUTPUT.resolve()
    if output != expected:
        raise SystemExit(f"refusing non-curated output path: {output}")
    output.mkdir(parents=True, exist_ok=True)

    if sys.platform != "win32":
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    else:
        os.environ.pop("QT_QPA_PLATFORM", None)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    sys.path.insert(0, str(ROOT))

    with tempfile.TemporaryDirectory(prefix="osia-guide-assets-") as temporary:
        isolated_home = Path(temporary) / "home"
        profile = Path(temporary) / "profile"
        isolated_home.mkdir()
        profile.mkdir()
        os.environ["HOME"] = str(isolated_home)
        os.environ["USERPROFILE"] = str(isolated_home)
        os.environ["OSIA_LAN_DIRECT_V1"] = "0"
        os.environ["OSIA_WEBRTC_DIRECT_V1"] = "0"
        os.environ["OSIA_RELAY_ENABLED"] = "0"

        from osia import config

        config.CONFIG_DIR = str(profile)
        config.API_KEYS_FILE = str(profile / "api_keys.json")
        config.SETTINGS_FILE = str(profile / "settings.json")
        config.MOBILE_TOKEN_FILE = str(profile / "mobile_token.txt")
        settings = dict(config.DEFAULT_SETTINGS)
        settings.update(
            {
                "language": "en",
                "selected_model": "gpt-4.1",
                "selected_model_provider": "openai",
                "ai_hierarchy_enabled": True,
                "ai_hierarchy": {
                    "heavy": ["anthropic:claude-sonnet-4-6", "openai:gpt-4.1"],
                    "medium": ["google:gemini-2.5-flash", "openai:gpt-4.1-mini"],
                },
                "tts_provider": "local",
                "stt_provider": "local",
                "stt_fallback_provider": "groq",
            }
        )
        config.save_settings(settings)
        (profile / "mobile_token.txt").write_text(
            "DEMO-TOKEN-NOT-A-SECRET", encoding="utf-8"
        )

        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QFont
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication
        from osia import i18n
        from osia.gui import main_popup
        from osia.gui.update_ui import UpdateController

        class _DormantThread:
            def __init__(self, *args, target=None, **_kwargs):
                self.target = target if target is not None else (
                    args[0] if args else None
                )

            def start(self):
                return None

        main_popup.threading = types.SimpleNamespace(Thread=_DormantThread)
        main_popup.refresh_ollama_models_async = lambda callback: callback([])
        main_popup.autostart_enabled = lambda: False
        main_popup.set_autostart = lambda _enabled: True
        main_popup.get_service_status = lambda _name: "inactive"
        main_popup.is_service_enabled = lambda _name: False
        main_popup.enable_service = lambda _name: True
        main_popup.disable_service = lambda _name: True
        main_popup.restart_service = lambda _name: True
        main_popup.stop_service = lambda _name: True

        app = QApplication.instance() or QApplication([])
        app.setFont(QFont("Segoe UI", 9))
        i18n.set_language("en", persist=False)
        update_controller = UpdateController()
        update_controller._timer.stop()
        panel = main_popup._SettingsPanel(update_controller)
        panel.resize(*CAPTURE_SIZE)
        panel.show()
        panel._startup_timer.stop()
        panel._mcp_timer.stop()

        def settle() -> None:
            for _ in range(4):
                app.processEvents()
            QTest.qWait(260)
            app.processEvents()

        def capture(name: str) -> None:
            settle()
            destination = output / name
            if not panel.grab().save(str(destination), "PNG"):
                raise RuntimeError(f"could not save {destination}")
            _strip_png_metadata(destination)

        panel.show_category("settings.cat.general")
        capture("settings-menu.png")

        panel.show_answer_settings(chatgpt=True)
        panel._apply_chatgpt_status(
            {"connected": False, "available": True, "error": "disconnected"}
        )
        panel._settings_scroll.verticalScrollBar().setValue(0)
        capture("settings-chatgpt-account.png")

        provider_index = panel._ai_provider_combo.findData("openai")
        panel._ai_provider_combo.setCurrentIndex(provider_index)
        panel._provider_cards["openai"].set_key("DEMO-NOT-A-SECRET")
        panel._settings_scroll.verticalScrollBar().setValue(0)
        capture("settings-api-provider.png")

        panel.show_mobile_settings()
        panel._mobile_token = "DEMO-TOKEN-NOT-A-SECRET"
        panel._update_token_mask()
        panel._token_mask_label.setText("••••••••••••••••••••")
        panel._settings_scroll.verticalScrollBar().setValue(0)
        capture("settings-mobile-pairing.png")

        panel.show_category("settings.cat.voice")
        panel._settings_scroll.verticalScrollBar().setValue(0)
        capture("settings-voice.png")

        panel.show_category("settings.cat.models")
        settle()
        hierarchy_y = panel._hier_title.mapTo(panel._settings_stack, QPoint()).y()
        panel._settings_scroll.verticalScrollBar().setValue(max(0, hierarchy_y - 8))
        capture("settings-model-hierarchy.png")

        panel.close()
        panel.deleteLater()
        update_controller.deleteLater()
        settle()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
