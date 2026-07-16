"""Bounded presentation-only text reveal for one active desktop response."""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from osia.gui.motion import reduced_motion_requested


class DesktopTextStreamController(QObject):
    """Keep a canonical target and reveal one ordered prefix with one timer."""

    text_changed = pyqtSignal(str)

    TICK_MS = 16
    CHARS_PER_SECOND = 200
    CATCH_UP_FACTOR = 0.08

    def __init__(
        self,
        parent: QObject | None = None,
        reduced_motion: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._reduced_motion = reduced_motion or reduced_motion_requested
        self._target = ""
        self._revealed = 0
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    @property
    def target_text(self) -> str:
        return self._target

    @property
    def displayed_text(self) -> str:
        return self._target[: self._revealed]

    @property
    def active(self) -> bool:
        return self._active

    @property
    def timer_active(self) -> bool:
        return self._timer.isActive()

    def begin(self) -> None:
        self._timer.stop()
        self._target = ""
        self._revealed = 0
        self._active = True

    def append_chunk(self, chunk: object, *, separator: str = "") -> None:
        text = str(chunk or "")
        if not text:
            return
        if not self._active:
            self.begin()
        if separator and self._target and not self._target[-1].isspace() and not text[0].isspace():
            self._target += separator
        self._target += text
        if self._reduced_motion():
            self._reveal_all(keep_active=True)
        elif not self._timer.isActive():
            self._timer.start()

    def finalize(self, full_text: object) -> None:
        """Replace with the canonical final and expose it synchronously."""
        self._target = str(full_text or "")
        self._reveal_all(keep_active=False)

    def cancel(self, *, flush: bool = True) -> None:
        self._timer.stop()
        if flush:
            self._reveal_all(keep_active=False)
        else:
            self._active = False

    def error(self, final_text: object | None = None) -> None:
        if final_text is None:
            self.cancel(flush=True)
        else:
            self.finalize(final_text)

    def clear(self) -> None:
        self._timer.stop()
        self._target = ""
        self._revealed = 0
        self._active = False

    def _reveal_all(self, *, keep_active: bool) -> None:
        self._timer.stop()
        self._revealed = len(self._target)
        self._active = keep_active
        self.text_changed.emit(self._target)

    def _on_tick(self) -> None:
        if not self._active:
            self._timer.stop()
            return
        if self._reduced_motion():
            self._reveal_all(keep_active=True)
            return
        remaining = len(self._target) - self._revealed
        if remaining <= 0:
            self._timer.stop()
            return
        base = max(1, round(self.CHARS_PER_SECOND * self.TICK_MS / 1000))
        step = max(base, int(remaining * self.CATCH_UP_FACTOR + 0.999))
        self._revealed += min(step, remaining)
        self.text_changed.emit(self.displayed_text)
        if self._revealed >= len(self._target):
            self._timer.stop()
