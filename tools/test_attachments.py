#!/usr/bin/env python3
"""Deterministic WOR-123 attachment contract and storage tests."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.attachments import (  # noqa: E402
    ATTACHMENT_CHUNK_BYTES,
    ATTACHMENT_PROTOCOL_VERSION,
    DEFAULT_BATCH_TTL_SECONDS,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_FILES,
    AttachmentBatchManager,
    AttachmentProtocolError,
    build_agent_input,
    cleanup_orphan_batches,
    sanitize_display_name,
)


class _Harness:
    def __init__(self, root: Path, *, now: float = 1_800_000_000.0):
        self.now = now
        self.manager = AttachmentBatchManager(root, clock=lambda: self.now)
        self.event_number = 0
        self.sequence_by_batch: dict[str, int] = {}

    def _event_id(self) -> str:
        self.event_number += 1
        return f"event_{self.event_number:08d}"

    def metadata(self, data: bytes, index: int) -> dict:
        return {
            "file_id": f"file_{index:08d}",
            "logical_order": index,
            "display_name": f"public-{index}.txt",
            "original_extension": "txt",
            "declared_mime": "text/plain",
            "detected_type_category": "file",
            "declared_byte_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "chunk_size": ATTACHMENT_CHUNK_BYTES,
            "expected_chunk_count": math.ceil(
                len(data) / ATTACHMENT_CHUNK_BYTES
            ),
        }

    def metadata_for_size(self, size: int, index: int) -> dict:
        item = self.metadata(b"x", index)
        item["declared_byte_size"] = size
        item["expected_chunk_count"] = math.ceil(
            size / ATTACHMENT_CHUNK_BYTES
        )
        return item

    def prepare_frame(
        self,
        files: list[dict],
        *,
        batch_id: str = "batch_test",
        request_id: str = "request_test",
        conversation_id: str = "conv_test",
        aggregate_size: int | None = None,
    ) -> dict:
        self.sequence_by_batch[batch_id] = 1
        created = datetime.fromtimestamp(self.now - 1, tz=timezone.utc)
        expires = datetime.fromtimestamp(
            self.now + DEFAULT_BATCH_TTL_SECONDS - 1,
            tz=timezone.utc,
        )
        return {
            "type": "attachment_batch_prepare",
            "protocol_version": ATTACHMENT_PROTOCOL_VERSION,
            "batch_id": batch_id,
            "conversation_id": conversation_id,
            "client_request_id": request_id,
            "request_id": request_id,
            "source_device_role": "mobile",
            "file_count": len(files),
            "aggregate_size": (
                sum(item["declared_byte_size"] for item in files)
                if aggregate_size is None
                else aggregate_size
            ),
            "created_at": created.isoformat(),
            "expiry": expires.isoformat(),
            "event_id": self._event_id(),
            "sequence": 1,
            "files": files,
        }

    def prepare(self, contents: list[bytes], **kwargs) -> tuple[dict, list[dict]]:
        frame = self.prepare_frame(
            [self.metadata(data, index) for index, data in enumerate(contents)],
            **kwargs,
        )
        return frame, self.manager.handle(frame)

    def event(
        self,
        event_type: str,
        *,
        batch_id: str = "batch_test",
        request_id: str = "request_test",
        conversation_id: str = "conv_test",
        **fields,
    ) -> tuple[dict, list[dict]]:
        batch = self.manager._batches.get(batch_id)
        if batch is not None and event_type.startswith("attachment_file_"):
            file_id = fields.get("file_id")
            file_state = batch.files.get(file_id)
            if file_state is not None:
                if event_type == "attachment_file_begin":
                    fields.setdefault("logical_order", file_state.logical_order)
                elif event_type == "attachment_file_chunk":
                    fields.setdefault(
                        "expected_chunk_count",
                        file_state.expected_chunk_count,
                    )
                    encoded = fields.get("data")
                    if isinstance(encoded, str):
                        fields.setdefault(
                            "payload_length",
                            len(base64.b64decode(encoded)),
                        )
                elif event_type == "attachment_file_complete":
                    fields.setdefault(
                        "declared_byte_size", file_state.declared_size,
                    )
                    fields.setdefault("sha256", file_state.declared_sha256)
        if batch is not None and event_type == "attachment_batch_commit":
            fields.setdefault("file_count", batch.file_count)
            fields.setdefault("aggregate_size", batch.aggregate_size)
        sequence = self.sequence_by_batch[batch_id] + 1
        self.sequence_by_batch[batch_id] = sequence
        frame = {
            "type": event_type,
            "protocol_version": ATTACHMENT_PROTOCOL_VERSION,
            "batch_id": batch_id,
            "conversation_id": conversation_id,
            "client_request_id": request_id,
            "request_id": request_id,
            "event_id": self._event_id(),
            "sequence": sequence,
            **fields,
        }
        return frame, self.manager.handle(frame)

    def upload(
        self,
        contents: list[bytes],
        *,
        batch_id: str = "batch_test",
        request_id: str = "request_test",
        conversation_id: str = "conv_test",
    ) -> dict:
        _, response = self.prepare(
            contents,
            batch_id=batch_id,
            request_id=request_id,
            conversation_id=conversation_id,
        )
        if response[0]["type"] != "attachment_batch_accept":
            raise AssertionError(response)
        for index, data in enumerate(contents):
            file_id = f"file_{index:08d}"
            self.event(
                "attachment_file_begin",
                batch_id=batch_id,
                request_id=request_id,
                conversation_id=conversation_id,
                file_id=file_id,
                logical_order=index,
            )
            for chunk_index, offset in enumerate(
                range(0, len(data), ATTACHMENT_CHUNK_BYTES)
            ):
                chunk = data[offset : offset + ATTACHMENT_CHUNK_BYTES]
                _, chunk_response = self.event(
                    "attachment_file_chunk",
                    batch_id=batch_id,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    file_id=file_id,
                    chunk_index=chunk_index,
                    expected_chunk_count=math.ceil(
                        len(data) / ATTACHMENT_CHUNK_BYTES
                    ),
                    byte_offset=offset,
                    payload_length=len(chunk),
                    data=base64.b64encode(chunk).decode("ascii"),
                )
                if chunk_response:
                    raise AssertionError(chunk_response)
            self.event(
                "attachment_file_complete",
                batch_id=batch_id,
                request_id=request_id,
                conversation_id=conversation_id,
                file_id=file_id,
                declared_byte_size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        _, response = self.event(
            "attachment_batch_commit",
            batch_id=batch_id,
            request_id=request_id,
            conversation_id=conversation_id,
            file_count=len(contents),
            aggregate_size=sum(map(len, contents)),
        )
        return response[0]


class AttachmentMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.h = _Harness(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_reject(self, frame: dict, category: str) -> dict:
        response = self.h.manager.handle(frame)[0]
        self.assertEqual(response["type"], "attachment_batch_reject")
        self.assertEqual(response["category"], category)
        self.assertEqual(list(self.root.glob("batch-*")), [])
        return response

    def test_one_multiple_and_exactly_ten_files_accept(self) -> None:
        for count in (1, 3, MAX_ATTACHMENT_FILES):
            with self.subTest(count=count):
                root = self.root / str(count)
                h = _Harness(root)
                files = [h.metadata(b"x", index) for index in range(count)]
                response = h.manager.handle(
                    h.prepare_frame(files, batch_id=f"batch_{count:04d}")
                )[0]
                self.assertEqual(response["type"], "attachment_batch_accept")
                self.assertEqual(response["file_count"], count)

    def test_eleven_files_rejected(self) -> None:
        files = [self.h.metadata(b"x", index) for index in range(11)]
        self.assert_reject(
            self.h.prepare_frame(files),
            "invalid_file_count",
        )

    def test_aggregate_exactly_20_mib_accepts(self) -> None:
        files = [self.h.metadata_for_size(MAX_ATTACHMENT_BYTES, 0)]
        response = self.h.manager.handle(self.h.prepare_frame(files))[0]
        self.assertEqual(response["type"], "attachment_batch_accept")
        self.assertEqual(response["aggregate_size"], MAX_ATTACHMENT_BYTES)

    def test_aggregate_and_individual_over_20_mib_reject(self) -> None:
        oversized = self.h.metadata_for_size(MAX_ATTACHMENT_BYTES + 1, 0)
        self.assert_reject(
            self.h.prepare_frame([oversized]),
            "aggregate_too_large",
        )
        oversized = self.h.metadata_for_size(MAX_ATTACHMENT_BYTES + 1, 0)
        self.assert_reject(
            self.h.prepare_frame(
                [oversized], aggregate_size=MAX_ATTACHMENT_BYTES
            ),
            "file_too_large",
        )
        files = [
            self.h.metadata_for_size(MAX_ATTACHMENT_BYTES, 0),
            self.h.metadata_for_size(1, 1),
        ]
        self.assert_reject(
            self.h.prepare_frame(files),
            "aggregate_too_large",
        )

    def test_zero_file_zero_size_and_non_integer_reject(self) -> None:
        empty = self.h.prepare_frame([], aggregate_size=0)
        self.assert_reject(empty, "invalid_file_count")
        item = self.h.metadata_for_size(1, 0)
        frame = self.h.prepare_frame([item])
        frame["aggregate_size"] = True
        self.assert_reject(frame, "aggregate_too_large")

    def test_aggregate_mismatch_duplicate_ids_and_orders_reject(self) -> None:
        files = [self.h.metadata(b"a", 0), self.h.metadata(b"b", 1)]
        self.assert_reject(
            self.h.prepare_frame(files, aggregate_size=3),
            "aggregate_mismatch",
        )
        files = [self.h.metadata(b"a", 0), self.h.metadata(b"b", 1)]
        files[1]["file_id"] = files[0]["file_id"]
        self.assert_reject(self.h.prepare_frame(files), "duplicate_file_id")
        files = [self.h.metadata(b"a", 0), self.h.metadata(b"b", 1)]
        files[1]["logical_order"] = 0
        self.assert_reject(
            self.h.prepare_frame(files),
            "duplicate_logical_order",
        )

    def test_malformed_hash_protocol_expiry_and_chunk_count_reject(self) -> None:
        item = self.h.metadata(b"x", 0)
        item["sha256"] = "not-a-hash"
        self.assert_reject(self.h.prepare_frame([item]), "invalid_hash")
        item = self.h.metadata(b"x", 0)
        frame = self.h.prepare_frame([item])
        frame["protocol_version"] = 99
        self.assert_reject(frame, "unsupported_protocol_version")
        item = self.h.metadata(b"x", 0)
        frame = self.h.prepare_frame([item])
        frame["expiry"] = frame["created_at"]
        self.assert_reject(frame, "expired_batch")
        item = self.h.metadata(b"x", 0)
        item["expected_chunk_count"] = 2
        self.assert_reject(self.h.prepare_frame([item]), "chunk_count_mismatch")

    def test_prepare_duplicate_is_idempotent_and_conflict_is_rejected(self) -> None:
        frame = self.h.prepare_frame([self.h.metadata(b"x", 0)])
        first = self.h.manager.handle(frame)
        for _ in range(50):
            self.assertEqual(self.h.manager.handle(dict(frame)), first)
        self.assertEqual(len(list(self.root.glob("batch-*"))), 1)
        conflict = dict(frame)
        conflict["aggregate_size"] = 2
        response = self.h.manager.handle(conflict)[0]
        self.assertEqual(response["category"], "event_id_conflict")

    def test_only_one_bounded_batch_can_be_active_per_connection(self) -> None:
        first = self.h.prepare_frame(
            [self.h.metadata(b"one", 0)], batch_id="batch_first"
        )
        self.assertEqual(
            self.h.manager.handle(first)[0]["type"],
            "attachment_batch_accept",
        )
        second = self.h.prepare_frame(
            [self.h.metadata(b"two", 0)], batch_id="batch_second"
        )
        response = self.h.manager.handle(second)[0]
        self.assertEqual(response["type"], "attachment_batch_reject")
        self.assertEqual(response["category"], "attachment_batch_in_progress")
        self.assertEqual(len(list(self.root.glob("batch-*"))), 1)

    def test_safe_names_traversal_reserved_controls_and_unicode(self) -> None:
        self.assertEqual(sanitize_display_name(r"C:\private\public.txt"), "public.txt")
        self.assertNotIn("..", sanitize_display_name("../../public.txt"))
        self.assertEqual(sanitize_display_name("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_display_name("bad\x00name.txt"), "badname.txt")
        self.assertEqual(sanitize_display_name("e\u0301.txt"), "é.txt")
        self.assertLessEqual(len(sanitize_display_name("x" * 500 + ".txt")), 180)


class AttachmentTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.h = _Harness(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_one_and_multiple_files_commit_exact_bytes_and_hash(self) -> None:
        for contents in ([b"public marker"], [b"one", b"two", b"three"]):
            with self.subTest(count=len(contents)):
                root = self.root / str(len(contents))
                h = _Harness(root)
                ack = h.upload(contents, batch_id=f"batch_{len(contents):04d}")
                self.assertEqual(ack["type"], "attachment_batch_commit_ack")
                self.assertEqual(ack["file_count"], len(contents))
                self.assertTrue(all(item["hash_match"] for item in ack["attachments"]))
                committed = h.manager.consume_for_turn(
                    f"batch_{len(contents):04d}", "conv_test", "request_test"
                )
                self.assertEqual(
                    [item.path.read_bytes() for item in committed],
                    list(contents),
                )
                self.assertTrue(all(item.path.name.endswith(".ready") for item in committed))
                self.assertTrue(all(item.path.name.startswith("att_") for item in committed))

    def test_out_of_order_missing_wrong_length_extra_and_hash_fail_terminally(self) -> None:
        cases = ("out_of_order_chunk", "missing_chunks", "invalid_chunk_length", "hash_mismatch")
        for case in cases:
            with self.subTest(case=case):
                h = _Harness(self.root / case)
                data = b"x" * (ATTACHMENT_CHUNK_BYTES + 1)
                if case == "hash_mismatch":
                    metadata = h.metadata(data, 0)
                    metadata["sha256"] = "0" * 64
                    response = h.manager.handle(
                        h.prepare_frame(
                            [metadata], batch_id=f"batch_{case}"
                        )
                    )
                    self.assertEqual(
                        response[0]["type"], "attachment_batch_accept"
                    )
                else:
                    h.prepare([data], batch_id=f"batch_{case}")
                h.event("attachment_file_begin", batch_id=f"batch_{case}", file_id="file_00000000")
                if case == "missing_chunks":
                    _, response = h.event(
                        "attachment_file_complete",
                        batch_id=f"batch_{case}",
                        file_id="file_00000000",
                        declared_byte_size=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                else:
                    chunk_index = 1 if case == "out_of_order_chunk" else 0
                    offset = ATTACHMENT_CHUNK_BYTES if chunk_index else 0
                    payload = data[offset : offset + ATTACHMENT_CHUNK_BYTES]
                    if case == "invalid_chunk_length":
                        payload = payload[:-1]
                    _, response = h.event(
                        "attachment_file_chunk",
                        batch_id=f"batch_{case}",
                        file_id="file_00000000",
                        chunk_index=chunk_index,
                        byte_offset=offset,
                        data=base64.b64encode(payload).decode("ascii"),
                    )
                    if case == "hash_mismatch":
                        second = data[ATTACHMENT_CHUNK_BYTES:]
                        h.event(
                            "attachment_file_chunk",
                            batch_id=f"batch_{case}",
                            file_id="file_00000000",
                            chunk_index=1,
                            byte_offset=ATTACHMENT_CHUNK_BYTES,
                            data=base64.b64encode(second).decode("ascii"),
                        )
                        _, response = h.event(
                            "attachment_file_complete",
                            batch_id=f"batch_{case}",
                            file_id="file_00000000",
                            declared_byte_size=len(data),
                            sha256="0" * 64,
                        )
                self.assertEqual(response[0]["type"], "attachment_batch_failed")
                self.assertEqual(response[0]["category"], case)
                self.assertEqual(list((self.root / case).glob("batch-*")), [])

    def test_duplicate_chunks_are_idempotent_and_conflicts_fail(self) -> None:
        data = b"public"
        self.h.prepare([data])
        self.h.event("attachment_file_begin", file_id="file_00000000")
        fields = {
            "file_id": "file_00000000",
            "chunk_index": 0,
            "byte_offset": 0,
            "data": base64.b64encode(data).decode("ascii"),
        }
        self.h.event("attachment_file_chunk", **fields)
        for _ in range(50):
            _, response = self.h.event("attachment_file_chunk", **fields)
            self.assertEqual(response, [])
        conflict = dict(fields)
        conflict["data"] = base64.b64encode(b"PUBLIC").decode("ascii")
        _, response = self.h.event("attachment_file_chunk", **conflict)
        self.assertEqual(response[0]["category"], "conflicting_duplicate_chunk")
        self.assertEqual(list(self.root.glob("batch-*")), [])

    def test_failed_event_replay_returns_same_terminal_result(self) -> None:
        self.h.prepare([b"abc"])
        self.h.event("attachment_file_begin", file_id="file_00000000")
        frame, first = self.h.event(
            "attachment_file_chunk",
            file_id="file_00000000",
            chunk_index=0,
            byte_offset=0,
            data=base64.b64encode(b"too long").decode("ascii"),
        )
        self.assertEqual(first[0]["category"], "invalid_chunk_length")
        self.assertEqual(self.h.manager.handle(frame), first)

    def test_redundant_lifecycle_metadata_is_verified(self) -> None:
        cases = {
            "invalid_logical_order": (
                "attachment_file_begin",
                {"file_id": "file_00000000", "logical_order": 1},
            ),
            "invalid_chunk_length": (
                "attachment_file_chunk",
                {
                    "file_id": "file_00000000",
                    "chunk_index": 0,
                    "byte_offset": 0,
                    "payload_length": 2,
                    "expected_chunk_count": 1,
                    "data": base64.b64encode(b"abc").decode("ascii"),
                },
            ),
            "size_mismatch": (
                "attachment_file_complete",
                {
                    "file_id": "file_00000000",
                    "declared_byte_size": 2,
                    "sha256": hashlib.sha256(b"abc").hexdigest(),
                },
            ),
            "file_count_mismatch": (
                "attachment_batch_commit",
                {"file_count": 2, "aggregate_size": 3},
            ),
        }
        for index, (category, (event_type, fields)) in enumerate(cases.items()):
            with self.subTest(category=category):
                h = _Harness(self.root / f"strict-{index}")
                h.prepare([b"abc"])
                if event_type in {
                    "attachment_file_chunk",
                    "attachment_file_complete",
                }:
                    h.event(
                        "attachment_file_begin",
                        file_id="file_00000000",
                    )
                _, response = h.event(event_type, **fields)
                self.assertEqual(response[0]["category"], category)
                self.assertEqual(
                    list((self.root / f"strict-{index}").glob("batch-*")),
                    [],
                )

    def test_commit_replay_is_exact_and_turn_consumption_is_once(self) -> None:
        ack = self.h.upload([b"public"])
        _, replay = self.h.event(
            "attachment_batch_commit",
            file_count=1,
            aggregate_size=6,
        )
        self.assertEqual(replay[0], ack)
        committed = self.h.manager.consume_for_turn(
            "batch_test", "conv_test", "request_test"
        )
        self.assertEqual(len(committed), 1)
        with self.assertRaisesRegex(
            AttachmentProtocolError,
            "attachment_batch_already_consumed",
        ):
            self.h.manager.consume_for_turn(
                "batch_test", "conv_test", "request_test"
            )

    def test_new_conversation_preflight_then_one_time_binding(self) -> None:
        self.h.upload([b"public"], conversation_id="")
        self.h.manager.validate_turn_reference(
            "batch_test", "", "request_test"
        )
        committed = self.h.manager.consume_for_turn(
            "batch_test", "conv_created", "request_test"
        )
        self.assertEqual(len(committed), 1)
        with self.assertRaisesRegex(AttachmentProtocolError, "conversation_id_mismatch"):
            self.h.manager.validate_turn_reference(
                "batch_test", "conv_other", "request_test"
            )

    def test_cancel_timeout_disconnect_and_release_cleanup(self) -> None:
        for index in range(50):
            batch = f"batch_cancel_{index:03d}"
            self.h.prepare([b"x"], batch_id=batch)
            _, response = self.h.event(
                "attachment_batch_cancel", batch_id=batch
            )
            self.assertEqual(response[0]["category"], "canceled")
        self.assertEqual(list(self.root.glob("batch-*")), [])

        h = _Harness(self.root / "timeout")
        h.prepare([b"x"])
        h.now += DEFAULT_BATCH_TTL_SECONDS
        self.assertEqual(h.manager.expire(), 1)
        self.assertEqual(list((self.root / "timeout").glob("batch-*")), [])

        h = _Harness(self.root / "disconnect")
        h.prepare([b"x"])
        self.assertEqual(h.manager.cleanup_disconnected(), 1)
        self.assertEqual(list((self.root / "disconnect").glob("batch-*")), [])

        h = _Harness(self.root / "release")
        h.upload([b"x"])
        h.manager.consume_for_turn("batch_test", "conv_test", "request_test")
        self.assertTrue(h.manager.release("batch_test"))
        self.assertEqual(list((self.root / "release").glob("batch-*")), [])

    def test_stale_batches_cannot_mutate_a_new_batch(self) -> None:
        for index in range(50):
            old = f"batch_old_{index:03d}"
            new = f"batch_new_{index:03d}"
            self.h.prepare([b"old"], batch_id=old)
            self.h.event("attachment_batch_cancel", batch_id=old)
            self.h.prepare([b"new"], batch_id=new)
            _, stale = self.h.event(
                "attachment_file_begin",
                batch_id=old,
                file_id="file_00000000",
            )
            self.assertEqual(stale[0]["category"], "unknown_or_stale_batch")
            self.assertEqual(
                len(list(self.root.glob("batch-*"))),
                1,
            )
            self.h.manager.release(new)
            ack = self.h.upload([b"fresh"], batch_id=f"batch_fresh_{index:03d}")
            self.assertEqual(ack["type"], "attachment_batch_commit_ack")
            self.h.manager.release(f"batch_fresh_{index:03d}")

    def test_agent_ingestion_preserves_file_order_and_image_vision(self) -> None:
        ack = self.h.upload([b"one", b"two"])
        self.assertEqual(ack["file_count"], 2)
        committed = self.h.manager.consume_for_turn(
            "batch_test", "conv_test", "request_test"
        )
        agent_input = build_agent_input("inspect", committed)
        self.assertIsInstance(agent_input, str)
        self.assertLess(agent_input.index("public-0.txt"), agent_input.index("public-1.txt"))
        self.assertIn("1. public-0.txt:", agent_input)
        self.assertIn("2. public-1.txt:", agent_input)
        self.assertIn("ne réutilise jamais", agent_input)
        for item in committed:
            self.assertEqual(agent_input.count(str(item.path)), 1)

        image_root = self.root / "image"
        image = b"\x89PNG\r\n\x1a\n" + b"public-image"
        h = _Harness(image_root)
        h.upload([image])
        committed = h.manager.consume_for_turn(
            "batch_test", "conv_test", "request_test"
        )
        agent_input = build_agent_input("inspect", committed)
        self.assertIsInstance(agent_input, list)
        self.assertEqual(agent_input[0]["type"], "text")
        self.assertIn("public-0.txt", agent_input[0]["text"])
        self.assertIn("incapacité du fournisseur", agent_input[0]["text"])
        self.assertIn("inspect", agent_input[0]["text"])
        self.assertTrue(
            agent_input[1]["image_url"]["url"].startswith("data:image/png;base64,")
        )


class AttachmentOrphanCleanupTests(unittest.TestCase):
    def test_startup_cleanup_is_bounded_and_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orphan = root / "batch-public"
            orphan.mkdir()
            (orphan / "att_public.part").write_bytes(b"public")
            os.utime(orphan, (1, 1))
            unrelated = root / "unrelated"
            unrelated.mkdir()
            os.utime(unrelated, (1, 1))
            if hasattr(os, "symlink"):
                link = root / "batch-link"
                try:
                    link.symlink_to(unrelated, target_is_directory=True)
                except OSError:
                    link = None
            else:
                link = None
            removed = cleanup_orphan_batches(
                root,
                now=10_000,
                max_age_seconds=1,
            )
            self.assertEqual(removed, 1)
            self.assertFalse(orphan.exists())
            self.assertTrue(unrelated.exists())
            if link is not None:
                self.assertTrue(link.exists())
                with self.assertRaisesRegex(
                    AttachmentProtocolError,
                    "temporary_storage_failed",
                ):
                    AttachmentBatchManager(link)


if __name__ == "__main__":
    unittest.main(verbosity=2)
