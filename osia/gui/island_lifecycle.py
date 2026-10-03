"""Owned, thread-safe activity projected by the floating capsule.

Notifications carry no state: the GUI reads the current projection when it
handles them. A queued notification can therefore never resurrect an old turn.
Admission (a busy conversation) and animation (active work) are independent.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable


class IslandLifecycle:
    def __init__(self, notify: Callable[[], None]) -> None:
        self._lock = threading.RLock()
        self._notify = notify
        self._owners: dict[Hashable, tuple[Hashable, str, bool]] = {}
        self._closed = False

    def begin(self, source: Hashable, owner: Hashable, state: str = "idle") -> bool:
        """Called only at admission; callbacks must use update, never begin."""
        with self._lock:
            if self._closed:
                return False
            current = self._owners.get(source)
            if current is not None and current[0] == owner:
                return False
            self._owners[source] = (owner, self._state(state), False)
        self._notify()
        return True

    def update(
        self, source: Hashable, owner: Hashable, state: str, *, terminal: bool = False,
    ) -> bool:
        with self._lock:
            current = self._owners.get(source)
            if self._closed or current is None or current[0] != owner or current[2]:
                return False
            self._owners[source] = (
                owner, "idle" if terminal else self._state(state), terminal,
            )
        self._notify()
        return True

    def finish(self, source: Hashable, owner: Hashable) -> bool:
        with self._lock:
            current = self._owners.get(source)
            if current is None or current[0] != owner:
                return False
            del self._owners[source]
        self._notify()
        return True

    def current_state(self) -> str:
        with self._lock:
            states = {entry[1] for entry in self._owners.values() if not entry[2]}
            # Actual recording/playback keeps priority over background turns.
            for state in ("listening", "speaking", "thinking"):
                if state in states:
                    return state
            return "idle"

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._owners.clear()
        self._notify()

    @staticmethod
    def _state(state: str) -> str:
        return state if state in {"idle", "listening", "speaking", "thinking"} else "idle"
