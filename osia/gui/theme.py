"""Central semantic palettes and live theme selection for the desktop UI.

The default palette intentionally matches the pre-WOR-129 PC colours.  Other
palettes reuse the dark, restrained choices established by OSIA Mobile while
keeping the PC preference local to ``settings.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor


THEME_SETTING_KEY = "ui_theme"
DEFAULT_THEME_KEY = "osia"

SEMANTIC_COLOR_FIELDS = (
    "background",
    "surface",
    "elevated_surface",
    "glass",
    "primary",
    "secondary",
    "text",
    "muted_text",
    "border",
    "success",
    "warning",
    "error",
    "focus",
    "disabled",
    "user_bubble",
    "assistant_bubble",
)


@dataclass(frozen=True)
class ThemePalette:
    key: str
    name_key: str
    background: str
    surface: str
    elevated_surface: str
    glass: str
    primary: str
    secondary: str
    text: str
    muted_text: str
    border: str
    success: str
    warning: str
    error: str
    focus: str
    disabled: str
    user_bubble: str
    assistant_bubble: str

    def color(self, field: str, alpha: int | None = None) -> QColor:
        if field not in SEMANTIC_COLOR_FIELDS:
            raise KeyError(field)
        color = QColor(getattr(self, field))
        if alpha is not None:
            color.setAlpha(max(0, min(255, int(alpha))))
        return color


def _palette(
    key: str,
    name_key: str,
    *,
    background: str,
    surface: str,
    elevated: str,
    glass: str,
    primary: str,
    secondary: str,
    text: str,
    muted: str,
    border: str,
    success: str = "#6fcf97",
    warning: str = "#e0a23c",
    error: str = "#e0a3a3",
    disabled: str = "#777480",
    user_bubble: str | None = None,
    assistant_bubble: str | None = None,
) -> ThemePalette:
    return ThemePalette(
        key=key,
        name_key=name_key,
        background=background,
        surface=surface,
        elevated_surface=elevated,
        glass=glass,
        primary=primary,
        secondary=secondary,
        text=text,
        muted_text=muted,
        border=border,
        success=success,
        warning=warning,
        error=error,
        focus=primary,
        disabled=disabled,
        user_bubble=user_bubble or elevated,
        assistant_bubble=assistant_bubble or surface,
    )


# The first/default entry is the exact established PC palette. Rose, amber and
# neutral follow Mobile's existing palettes; azure and emerald extend the same
# dark-surface contrast model requested for PC.
PALETTES: dict[str, ThemePalette] = {
    "osia": _palette(
        "osia", "theme.osia",
        background="#0d0b12", surface="#0f0d16", elevated="#13111a",
        glass="#1a1625", primary="#c4b5f5", secondary="#8c6edc",
        text="#e8e6f0", muted="#9d9aad", border="#2a2438",
        disabled="#858291",
        user_bubble="#2a2440", assistant_bubble="#0f0d16",
    ),
    "azure": _palette(
        "azure", "theme.azure",
        background="#09111a", surface="#0e1a26", elevated="#132536",
        glass="#162c40", primary="#8ac7ff", secondary="#378fe5",
        text="#f1f7ff", muted="#8da0b3", border="#294057",
        disabled="#8da0b3",
        user_bubble="#183b59", assistant_bubble="#0e1a26",
    ),
    "emerald": _palette(
        "emerald", "theme.emerald",
        background="#08130f", surface="#0d1d17", elevated="#132a21",
        glass="#173428", primary="#8ee6bd", secondary="#2ca875",
        text="#effcf6", muted="#88a598", border="#27483a",
        disabled="#88a598",
        user_bubble="#174331", assistant_bubble="#0d1d17",
    ),
    "rose": _palette(
        "rose", "theme.rose",
        background="#120b0e", surface="#22161b", elevated="#2e1b22",
        glass="#2e1b22", primary="#f5b5c4", secondary="#ed4f87",
        text="#fff3f6", muted="#9c8b91", border="#3a2530",
        disabled="#9c8b91",
        user_bubble="#38222a", assistant_bubble="#22161b",
    ),
    "amber": _palette(
        "amber", "theme.amber",
        background="#110e0a", surface="#1f1a12", elevated="#2b2419",
        glass="#2b2419", primary="#e6c97a", secondary="#c79d3e",
        text="#faf1dc", muted="#9c8c6e", border="#3a301f",
        disabled="#9c8c6e",
        warning="#e6c97a", user_bubble="#2b2419",
        assistant_bubble="#1f1a12",
    ),
    "neutral": _palette(
        "neutral", "theme.neutral",
        background="#000000", surface="#101010", elevated="#1a1a1a",
        glass="#1a1a1a", primary="#ededed", secondary="#6b6b6b",
        text="#ffffff", muted="#8a8a8a", border="#262626",
        disabled="#8a8a8a", user_bubble="#1f1f1f",
        assistant_bubble="#101010",
    ),
}


def palette_for_key(key: object) -> ThemePalette:
    if isinstance(key, str):
        palette = PALETTES.get(key.strip().lower())
        if palette is not None:
            return palette
    return PALETTES[DEFAULT_THEME_KEY]


def _rgb(color: QColor) -> str:
    return f"{color.red()}, {color.green()}, {color.blue()}"


def themed_stylesheet(template: str, palette: ThemePalette) -> str:
    """Map the legacy default QSS tokens to ``palette`` semantic colours.

    Returning the untouched template for the default is deliberate: it proves
    that enabling WOR-129 does not alter a single established default QSS token.
    The mapping lets the large existing stylesheet remain one source of widget
    state rules instead of duplicating it per preset.
    """
    if palette.key == DEFAULT_THEME_KEY:
        return template

    replacements = {
        "#0d0b12": palette.background,
        "#0f0d16": palette.surface,
        "#13111a": palette.elevated_surface,
        "#1a1625": palette.glass,
        "#c4b5f5": palette.primary,
        "#e8e6f0": palette.text,
        "#9d9aad": palette.muted_text,
        "#8a8798": palette.muted_text,
        "#858291": palette.disabled,
        "#e0a3a3": palette.error,
        "#ff6b6b": palette.error,
        "#e0a23c": palette.warning,
        "#c79030": palette.warning,
        "#6fcf97": palette.success,
    }
    result = template
    for old, new in replacements.items():
        result = re.sub(re.escape(old), new, result, flags=re.IGNORECASE)

    rgb_fields = (
        ((196, 181, 245), "primary"),
        ((232, 230, 240), "text"),
        ((13, 11, 18), "background"),
        ((15, 13, 22), "surface"),
        ((19, 17, 26), "elevated_surface"),
        ((133, 130, 145), "disabled"),
        ((220, 80, 80), "error"),
    )
    for (red, green, blue), field in rgb_fields:
        pattern = rf"(?<!\d){red}\s*,\s*{green}\s*,\s*{blue}(?!\d)"
        result = re.sub(pattern, _rgb(palette.color(field)), result)
    return result


class ThemeManager(QObject):
    """Own the active palette and persist its local PC preference."""

    theme_changing = pyqtSignal(str)
    theme_changed = pyqtSignal(str)

    def __init__(
        self,
        settings_loader: Callable[[], dict] | None = None,
        setting_saver: Callable[[str, object], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if settings_loader is None or setting_saver is None:
            from osia.config import load_settings, set_setting

            settings_loader = settings_loader or load_settings
            setting_saver = setting_saver or set_setting
        self._settings_loader = settings_loader
        self._setting_saver = setting_saver
        try:
            saved = (settings_loader() or {}).get(THEME_SETTING_KEY)
        except Exception:
            saved = None
        self._palette = palette_for_key(saved)

    @property
    def palette(self) -> ThemePalette:
        return self._palette

    @property
    def key(self) -> str:
        return self._palette.key

    def set_theme(self, key: object, *, persist: bool = True) -> bool:
        next_palette = palette_for_key(key)
        if next_palette.key == self._palette.key:
            return False
        self.theme_changing.emit(next_palette.key)
        self._palette = next_palette
        if persist:
            try:
                self._setting_saver(THEME_SETTING_KEY, next_palette.key)
            except Exception:
                # A read-only/broken preferences directory must never prevent a
                # live visual change; the next launch safely falls back.
                pass
        self.theme_changed.emit(next_palette.key)
        return True


_manager: ThemeManager | None = None


def get_theme_manager() -> ThemeManager:
    global _manager
    if _manager is None:
        _manager = ThemeManager()
    return _manager
