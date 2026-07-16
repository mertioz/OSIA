"""Bounded coalescing for adjacent assistant text-delta frames.

The coalescer is deliberately transport-agnostic.  It runs before the server's
single WebSocket writer, so LAN, WebRTC and encrypted-relay paths receive the
same larger ``response_chunk`` frames without any protocol change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any


TEXT_COALESCE_DELAY_SECONDS = 0.050
TEXT_COALESCE_MAX_BYTES = 1024

_REPLACED_FIELDS = frozenset({"content", "event_id", "sequence"})


class AssistantTextCoalescer:
    """Coalesce adjacent, correlated ``response_chunk`` frames.

    ``offer`` returns ``True`` only when it consumed a coalescible text frame.
    Any other frame flushes pending text first and returns ``False`` so the
    caller can enqueue that frame immediately after the flushed text.
    """

    __slots__ = (
        "_delay_seconds",
        "_emit",
        "_loop",
        "_max_bytes",
        "_pending",
        "_pending_key",
        "_timer",
    )

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        loop: asyncio.AbstractEventLoop,
        delay_seconds: float = TEXT_COALESCE_DELAY_SECONDS,
        max_bytes: int = TEXT_COALESCE_MAX_BYTES,
    ) -> None:
        if delay_seconds <= 0:
            raise ValueError("delay_seconds must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._emit = emit
        self._loop = loop
        self._delay_seconds = delay_seconds
        self._max_bytes = max_bytes
        self._pending: dict[str, Any] | None = None
        self._pending_key: tuple[str, str, str, str] | None = None
        self._timer: asyncio.TimerHandle | None = None

    @staticmethod
    def _frame_key(frame: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
        if (
            frame.get("type") != "response_chunk"
            or frame.get("event_type") != "response_chunk"
            or frame.get("terminal") is not False
        ):
            return None
        content = frame.get("content")
        conversation_id = str(frame.get("conversation_id") or "")
        request_id = str(
            frame.get("request_id") or frame.get("client_request_id") or ""
        )
        turn_id = str(frame.get("turn_id") or "")
        message_id = str(frame.get("message_id") or "")
        if (
            not isinstance(content, str)
            or not content
            or not conversation_id
            or not request_id
            or not turn_id
            or not message_id
        ):
            return None
        return conversation_id, request_id, turn_id, message_id

    @staticmethod
    def _stable_fields(frame: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in frame.items()
            if key not in _REPLACED_FIELDS
        }

    @staticmethod
    def _join(left: str, right: str) -> str:
        # Mobile historically inserts one separator between stripped sentence
        # chunks.  Preserve that exact visible reconstruction when those chunks
        # become one network payload.
        needs_space = (
            bool(left)
            and not left.endswith((" ", "\n"))
            and not right.startswith(" ")
        )
        return left + (" " if needs_space else "") + right

    def offer(self, frame: Any) -> bool:
        """Consume one text frame, or flush before a non-text/boundary frame."""
        if not isinstance(frame, Mapping):
            self.flush()
            return False
        key = self._frame_key(frame)
        if key is None:
            self.flush()
            return False

        incoming = dict(frame)
        if self._pending is not None and (
            key != self._pending_key
            or self._stable_fields(incoming) != self._stable_fields(self._pending)
        ):
            self.flush()

        if self._pending is None:
            self._pending = incoming
            self._pending_key = key
            self._timer = self._loop.call_later(
                self._delay_seconds,
                self.flush,
            )
        else:
            merged_content = self._join(
                str(self._pending["content"]),
                str(incoming["content"]),
            )
            # Keep the newest canonical event metadata (event_id/sequence) so
            # the emitted stream remains strictly monotonic for current Mobile.
            incoming["content"] = merged_content
            self._pending = incoming

        if len(str(self._pending["content"]).encode("utf-8")) >= self._max_bytes:
            self.flush()
        return True

    def flush(self) -> None:
        """Emit pending text exactly once, cancelling its deadline."""
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()
        pending = self._pending
        self._pending = None
        self._pending_key = None
        if pending is not None:
            self._emit(pending)

    def clear(self) -> None:
        """Discard pending text and cancel its timer after a failed transport."""
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()
        self._pending = None
        self._pending_key = None
