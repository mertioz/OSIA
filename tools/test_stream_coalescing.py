#!/usr/bin/env python3
"""Deterministic WOR-122 assistant-text coalescing regression tests."""

from __future__ import annotations

import heapq
import unittest
from dataclasses import dataclass
from typing import Any

from osia.server.text_coalescer import AssistantTextCoalescer


@dataclass
class _Handle:
    when: float
    callback: Any
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class _SimulatedLoop:
    def __init__(self) -> None:
        self.now = 0.0
        self._order = 0
        self._scheduled: list[tuple[float, int, _Handle]] = []

    def call_later(self, delay: float, callback) -> _Handle:
        self._order += 1
        handle = _Handle(self.now + delay, callback)
        heapq.heappush(
            self._scheduled,
            (handle.when, self._order, handle),
        )
        return handle

    def advance_to(self, when: float) -> None:
        while self._scheduled and self._scheduled[0][0] <= when:
            scheduled, _, handle = heapq.heappop(self._scheduled)
            self.now = scheduled
            if not handle.cancelled:
                handle.callback()
        self.now = when


@dataclass(frozen=True)
class _Input:
    at_ms: int
    kind: str
    content: str = ""
    conversation_id: str = "conv-a"
    request_id: str = "req-a"
    turn_id: str = "turn-a"
    message_id: str = "message-a"


def _frame(item: _Input, sequence: int) -> dict[str, Any]:
    frame = {
        "type": item.kind,
        "conversation_id": item.conversation_id,
        "request_id": item.request_id,
        "client_request_id": item.request_id,
        "turn_id": item.turn_id,
        "message_id": item.message_id,
        "event_id": f"event-{sequence}",
        "event_type": item.kind,
        "sequence": sequence,
        "terminal": item.kind in {"final", "error", "cancelled"},
    }
    if item.content:
        frame["content"] = item.content
    return frame


def _visible_join(parts: list[str]) -> str:
    text = ""
    for part in parts:
        if text and not text.endswith((" ", "\n")) and not part.startswith(" "):
            text += " "
        text += part
    return text


def _run(
    items: list[_Input],
    *,
    delay_ms: int = 50,
    max_bytes: int = 1024,
) -> tuple[list[tuple[float, Any]], _SimulatedLoop, AssistantTextCoalescer]:
    loop = _SimulatedLoop()
    emitted: list[tuple[float, Any]] = []
    coalescer = AssistantTextCoalescer(
        lambda frame: emitted.append((loop.now, frame)),
        loop=loop,  # type: ignore[arg-type]
        delay_seconds=delay_ms / 1000,
        max_bytes=max_bytes,
    )
    for sequence, item in enumerate(items, 1):
        loop.advance_to(item.at_ms / 1000)
        frame = _frame(item, sequence)
        if not coalescer.offer(frame):
            emitted.append((loop.now, frame))
    if items:
        loop.advance_to(items[-1].at_ms / 1000 + delay_ms / 1000)
    return emitted, loop, coalescer


def _text_frames(emitted: list[tuple[float, Any]]) -> list[dict[str, Any]]:
    return [
        frame
        for _, frame in emitted
        if isinstance(frame, dict) and frame.get("type") == "response_chunk"
    ]


def _fixtures() -> dict[str, list[_Input]]:
    return {
        "short": [
            _Input(0, "response_chunk", "OK."),
            _Input(20, "final"),
        ],
        "long": [
            *[
                _Input(index * 5, "response_chunk", f"Sentence {index}.")
                for index in range(120)
            ],
            _Input(600, "final"),
        ],
        "punctuation": [
            *[
                _Input(
                    index * 4,
                    "response_chunk",
                    f"Clause {index}{',;:!?'[index % 5]}",
                )
                for index in range(90)
            ],
            _Input(360, "final"),
        ],
        "tool_heavy": [
            event
            for cycle in range(12)
            for event in [
                *[
                    _Input(
                        cycle * 40 + offset * 5,
                        "response_chunk",
                        f"Part {cycle}-{offset}.",
                    )
                    for offset in range(4)
                ],
                _Input(cycle * 40 + 22, "tool_event"),
            ]
        ]
        + [_Input(490, "final")],
        "cancellation": [
            *[
                _Input(index * 5, "response_chunk", f"Cancel {index}.")
                for index in range(12)
            ],
            _Input(60, "cancelled"),
        ],
    }


class AssistantTextCoalescerTests(unittest.TestCase):
    def test_long_response_reduces_text_frames_by_at_least_70_percent(self) -> None:
        items = _fixtures()["long"]
        emitted, _, _ = _run(items)
        baseline = sum(item.kind == "response_chunk" for item in items)
        after = len(_text_frames(emitted))
        self.assertLessEqual(after, baseline * 0.30)

    def test_short_reply_uses_bounded_first_visible_delay(self) -> None:
        emitted, _, _ = _run(_fixtures()["short"])
        first_text_at_ms = _text_frames_with_times(emitted)[0][0] * 1000
        self.assertLessEqual(first_text_at_ms, 50)

    def test_reconstructed_text_is_exact_and_ordered(self) -> None:
        for name, items in _fixtures().items():
            with self.subTest(name=name):
                emitted, _, _ = _run(items)
                expected = _visible_join(
                    [item.content for item in items if item.kind == "response_chunk"]
                )
                actual = _visible_join(
                    [str(frame["content"]) for frame in _text_frames(emitted)]
                )
                self.assertEqual(actual.encode("utf-8"), expected.encode("utf-8"))

    def test_request_turn_conversation_and_message_boundaries_never_merge(self) -> None:
        items = [
            _Input(0, "response_chunk", "one"),
            _Input(1, "response_chunk", "two", request_id="req-b"),
            _Input(2, "response_chunk", "three", conversation_id="conv-b"),
            _Input(3, "response_chunk", "four", turn_id="turn-b"),
            _Input(4, "response_chunk", "five", message_id="message-b"),
            _Input(5, "final", request_id="req-b"),
        ]
        emitted, _, _ = _run(items)
        self.assertEqual(
            [frame["content"] for frame in _text_frames(emitted)],
            ["one", "two", "three", "four", "five"],
        )

    def test_tool_error_cancellation_and_final_flush_text_first(self) -> None:
        for terminal in ("tool_event", "error", "cancelled", "final"):
            with self.subTest(terminal=terminal):
                items = [
                    _Input(0, "response_chunk", "one"),
                    _Input(1, "response_chunk", "two"),
                    _Input(2, terminal),
                ]
                emitted, _, _ = _run(items)
                self.assertEqual([frame["type"] for _, frame in emitted], [
                    "response_chunk",
                    terminal,
                ])
                self.assertEqual(emitted[0][1]["content"], "one two")
                self.assertEqual(emitted[0][0], emitted[1][0])

    def test_non_text_frames_are_never_coalesced(self) -> None:
        non_text = [
            "audio",
            "attachment_file_chunk",
            "control",
            "tool_event",
            "heartbeat_data",
        ]
        items = [_Input(index, kind) for index, kind in enumerate(non_text)]
        emitted, _, _ = _run(items)
        self.assertEqual([frame["type"] for _, frame in emitted], non_text)

        raw_emitted: list[Any] = []
        loop = _SimulatedLoop()
        coalescer = AssistantTextCoalescer(
            raw_emitted.append,
            loop=loop,  # type: ignore[arg-type]
        )
        binary = b"\x00\x01attachment-bytes"
        self.assertFalse(coalescer.offer(binary))
        raw_emitted.append(binary)
        self.assertEqual(raw_emitted, [binary])

    def test_byte_threshold_flushes_without_indefinite_buffering(self) -> None:
        items = [
            _Input(0, "response_chunk", "é" * 200),
            _Input(1, "response_chunk", "é" * 60),
        ]
        emitted, _, _ = _run(items, delay_ms=100, max_bytes=512)
        frames = _text_frames(emitted)
        self.assertEqual(len(frames), 1)
        self.assertGreaterEqual(len(frames[0]["content"].encode("utf-8")), 512)
        self.assertEqual(emitted[0][0], 0.001)

    def test_disconnect_clear_cancels_pending_timer_without_replay(self) -> None:
        emitted, loop, coalescer = _run([], delay_ms=50)
        self.assertTrue(
            coalescer.offer(_frame(_Input(0, "response_chunk", "pending"), 1))
        )
        coalescer.clear()
        loop.advance_to(1.0)
        self.assertEqual(emitted, [])

    def test_matrix_preserves_non_text_counts_and_integrity(self) -> None:
        for delay_ms in (25, 50, 100):
            for max_bytes in (512, 1024, 2048):
                for name, items in _fixtures().items():
                    with self.subTest(
                        delay_ms=delay_ms,
                        max_bytes=max_bytes,
                        name=name,
                    ):
                        emitted, _, _ = _run(
                            items,
                            delay_ms=delay_ms,
                            max_bytes=max_bytes,
                        )
                        expected_non_text = sum(
                            item.kind != "response_chunk" for item in items
                        )
                        actual_non_text = sum(
                            not isinstance(frame, dict)
                            or frame.get("type") != "response_chunk"
                            for _, frame in emitted
                        )
                        self.assertEqual(actual_non_text, expected_non_text)
                        expected = _visible_join([
                            item.content
                            for item in items
                            if item.kind == "response_chunk"
                        ])
                        actual = _visible_join([
                            str(frame["content"])
                            for frame in _text_frames(emitted)
                        ])
                        self.assertEqual(actual, expected)


def _text_frames_with_times(
    emitted: list[tuple[float, Any]],
) -> list[tuple[float, dict[str, Any]]]:
    return [
        (at, frame)
        for at, frame in emitted
        if isinstance(frame, dict) and frame.get("type") == "response_chunk"
    ]


class OutboundChannelIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_flushes_text_before_control_and_close(self) -> None:
        from osia.server.server import _OutboundChannel, _WRITER_STOP

        channel = _OutboundChannel()
        await channel.send_json(_frame(_Input(0, "response_chunk", "one"), 1))
        await channel.send_json(_frame(_Input(1, "response_chunk", "two"), 2))
        await channel.send_json(_frame(_Input(2, "tool_event"), 3))
        await channel.close()

        queued = [await channel._queue.get() for _ in range(3)]
        self.assertEqual(queued[0][1]["content"], "one two")
        self.assertEqual(queued[1][1]["type"], "tool_event")
        self.assertIs(queued[2][0], _WRITER_STOP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
