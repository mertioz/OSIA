"""Native, accent-aware visual surfaces for the desktop Guide slideshow."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from osia.guide_catalog import GuideSlide, localized
from osia.utils.assets import asset_path


class _ScaledPixmapLabel(QLabel):
    """A bounded aspect-preserving instructional screenshot surface."""

    def __init__(self, pixmap: QPixmap, alt_text: str, parent=None) -> None:
        super().__init__(parent)
        self._source = pixmap
        self.setObjectName("guideImage")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(170)
        self.setAccessibleName(alt_text)
        self.setAccessibleDescription(alt_text)
        self.setToolTip(alt_text)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        if self._source.isNull() or self._source.width() <= 0:
            return 220
        natural = round(width * self._source.height() / self._source.width())
        return max(170, min(430, natural))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not self._source.isNull():
            self.setPixmap(self._source.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))


class GuideVisualWidget(QFrame):
    """Render a screenshot locator or a compact native semantic mini-demo."""

    def __init__(self, slide: GuideSlide, language: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("guideVisual")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(260, 220)
        alt = localized(slide.visual.alt, language)
        self.setAccessibleName(alt)
        self.setAccessibleDescription(alt)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        if slide.visual.kind == "screenshot" and slide.visual.asset:
            path = Path(asset_path(slide.visual.asset))
            pixmap = QPixmap(str(path)) if path.is_file() else QPixmap()
            if not pixmap.isNull():
                layout.addWidget(_ScaledPixmapLabel(pixmap, alt), 1)
                caption = QLabel(alt)
                caption.setObjectName("guideVisualCaption")
                caption.setWordWrap(True)
                caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(caption)
                return

        badge = QLabel(self._symbol(slide.visual.kind))
        badge.setObjectName("guideVisualBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setAccessibleName(alt)
        layout.addStretch(1)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignHCenter)

        title = QLabel(localized(slide.title, language))
        title.setObjectName("guideVisualTitle")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        nodes = QHBoxLayout()
        nodes.setSpacing(8)
        labels = self._node_labels(slide.visual.kind, language)
        for index, label_text in enumerate(labels):
            node = QLabel(label_text)
            node.setObjectName("guideVisualNode")
            node.setProperty("active", index == 1)
            node.setAlignment(Qt.AlignmentFlag.AlignCenter)
            node.setWordWrap(True)
            node.setMinimumSize(72, 54)
            nodes.addWidget(node, 1)
        layout.addLayout(nodes)
        layout.addStretch(1)

    @staticmethod
    def _symbol(kind: str) -> str:
        return {
            "diagram": "↗",
            "callout": "✓",
            "screenshot": "▣",
        }.get(kind, "✦")

    @staticmethod
    def _node_labels(kind: str, language: str) -> tuple[str, str, str]:
        french = language == "fr"
        if kind == "diagram":
            return ("Entrée", "OSIA", "Résultat") if french else ("Input", "OSIA", "Result")
        if kind == "callout":
            return ("Sûr", "Clair", "Maîtrisé") if french else ("Safe", "Clear", "In control")
        return ("Choisir", "Vérifier", "Continuer") if french else ("Choose", "Review", "Continue")


__all__ = ["GuideVisualWidget", "_ScaledPixmapLabel"]
