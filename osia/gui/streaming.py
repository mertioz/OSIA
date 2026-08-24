"""Bounded presentation-only text reveal for one active desktop response."""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QObject, QTextBoundaryFinder, QTimer, pyqtSignal

from osia.gui.motion import reduced_motion_requested


class DesktopTextStreamController(QObject):
    """Keep a canonical target and reveal one ordered prefix with one timer."""

    text_changed = pyqtSignal(str)
    completed = pyqtSignal()

    TICK_MS = 16
    CHARS_PER_SECOND = 200
    CATCH_UP_FACTOR = 0.08
    FINAL_CATCH_UP_FACTOR = 0.16
    FINAL_MIN_CHARS_PER_TICK = 4

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
        self._provider_complete = False
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

    @property
    def provider_complete(self) -> bool:
        return self._provider_complete

    def begin(self) -> None:
        self._timer.stop()
        self._target = ""
        self._revealed = 0
        self._active = True
        self._provider_complete = False

    def append_chunk(self, chunk: object, *, separator: str = "") -> None:
        text = str(chunk or "")
        if not text or self._provider_complete:
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
        """Drain the canonical final independently from provider completion."""

        final = str(full_text or "")
        shown = self.displayed_text
        self._target = final
        self._provider_complete = True
        self._active = True
        if not final:
            self._finish_reveal(emit_text=True)
            return
        if not final.startswith(shown):
            # Provisional provider text may be replaced by the canonical final.
            # Restart only this presentation prefix; the canonical text itself
            # is never altered or replayed into another turn.
            self._revealed = 0
            self.text_changed.emit("")
        else:
            self._revealed = min(self._revealed, len(final))
        if self._reduced_motion():
            self._finish_reveal(emit_text=True)
        elif self._revealed >= len(self._target):
            self._finish_reveal(emit_text=False)
        elif not self._timer.isActive():
            self._timer.start()

    def cancel(self, *, flush: bool = True) -> None:
        self._timer.stop()
        if flush:
            self._provider_complete = True
            self._finish_reveal(emit_text=True)
        else:
            self._active = False
            self._provider_complete = True
            self.completed.emit()

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
        self._provider_complete = False

    def _reveal_all(self, *, keep_active: bool) -> None:
        self._timer.stop()
        self._revealed = len(self._target)
        self._active = keep_active
        self.text_changed.emit(self._target)

    def _finish_reveal(self, *, emit_text: bool) -> None:
        self._timer.stop()
        self._revealed = len(self._target)
        self._active = False
        if emit_text:
            self.text_changed.emit(self._target)
        self.completed.emit()

    @staticmethod
    def _cluster_boundary(text: str, desired: int) -> int:
        """Return the next complete Unicode grapheme boundary."""

        end = min(max(0, desired), len(text))
        if end <= 0 or end >= len(text):
            return end
        finder = QTextBoundaryFinder(
            QTextBoundaryFinder.BoundaryType.Grapheme,
            text,
        )
        finder.setPosition(end)
        if finder.isAtBoundary():
            return end
        following = finder.toNextBoundary()
        return len(text) if following < 0 else following

    def _on_tick(self) -> None:
        if not self._active:
            self._timer.stop()
            return
        if self._reduced_motion():
            if self._provider_complete:
                self._finish_reveal(emit_text=True)
            else:
                self._reveal_all(keep_active=True)
            return
        remaining = len(self._target) - self._revealed
        if remaining <= 0:
            self._timer.stop()
            if self._provider_complete:
                self._finish_reveal(emit_text=False)
            return
        base = max(1, round(self.CHARS_PER_SECOND * self.TICK_MS / 1000))
        catch_up = (
            self.FINAL_CATCH_UP_FACTOR
            if self._provider_complete else self.CATCH_UP_FACTOR
        )
        if self._provider_complete:
            base = max(base, self.FINAL_MIN_CHARS_PER_TICK)
        step = max(base, int(remaining * catch_up + 0.999))
        self._revealed = self._cluster_boundary(
            self._target,
            self._revealed + min(step, remaining),
        )
        self.text_changed.emit(self.displayed_text)
        if self._revealed >= len(self._target):
            if self._provider_complete:
                self._finish_reveal(emit_text=False)
            else:
                self._timer.stop()
