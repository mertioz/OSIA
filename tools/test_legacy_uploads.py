#!/usr/bin/env python3
"""Focused deterministic WOR-136 legacy-upload security tests."""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.attachments import AttachmentBatchManager  # noqa: E402
from osia.legacy_uploads import (  # noqa: E402
    LegacyUploadError,
    LegacyUploadManager,
    reconcile_legacy_uploads,
    run_with_legacy_cleanup,
)


def _frame(data: bytes, filename: str = "public.txt", **fields) -> dict:
    return {
        "type": "file",
        "filename": filename,
        "data": base64.b64encode(data).decode("ascii"),
        **fields,
    }


class LegacyUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="osia-legacy-upload-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "legacy"

    def manager(self, **kwargs) -> LegacyUploadManager:
        return LegacyUploadManager(self.root, **kwargs)

    def assert_code(self, code: str, operation) -> None:
        with self.assertRaises(LegacyUploadError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_01_valid_small_legacy_file(self) -> None:
        manager = self.manager()
        upload = manager.save_frame(
            _frame(b"small public payload", declared_byte_size=20)
        )
        self.assertEqual(upload.display_name, "public.txt")
        self.assertEqual(upload.path.read_bytes(), b"small public payload")
        self.assertEqual(upload.path.parent, self.root.resolve())
        self.assertEqual(manager.outstanding_count, 1)

    def test_02_strict_invalid_base64_and_payload_shape_rejected(self) -> None:
        manager = self.manager()
        cases = (
            ({"type": "file", "filename": "a.txt", "data": "YW Jj"}, "invalid_base64"),
            ({"type": "file", "filename": "a.txt", "data": "YWJ"}, "invalid_base64"),
            ({"type": "file", "filename": "a.txt", "data": "AAAA===="}, "invalid_base64"),
            ({"type": "file", "filename": "a.txt", "data": ["YWJj"]}, "invalid_payload"),
            ({"type": "file", "filename": ["a.txt"], "data": "YWJj"}, "invalid_filename"),
        )
        for frame, code in cases:
            with self.subTest(frame=frame, code=code):
                self.assert_code(code, lambda frame=frame: manager.save_frame(frame))
        self.assertFalse(self.root.exists())

    def test_03_decoded_size_boundary_accounts_for_padding(self) -> None:
        manager = self.manager(max_bytes=4)
        upload = manager.save_frame(_frame(b"abcd", size=4))
        self.assertEqual(upload.byte_size, 4)
        manager.cleanup([upload.path])
        self.assert_code(
            "declared_size_mismatch",
            lambda: manager.save_frame(_frame(b"abc", size=4)),
        )

    def test_04_encoded_size_preflight_rejects_before_decode(self) -> None:
        manager = self.manager(max_bytes=3)
        with mock.patch("osia.legacy_uploads.base64.b64decode") as decode:
            self.assert_code(
                "encoded_payload_too_large",
                lambda: manager.save_frame(_frame(b"four")),
            )
        decode.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_05_oversize_decoded_payload_rejected(self) -> None:
        manager = self.manager(max_bytes=4)
        self.assert_code(
            "decoded_payload_too_large",
            lambda: manager.save_frame(_frame(b"abcde")),
        )
        self.assertEqual(manager.outstanding_count, 0)

    def test_06_too_many_files_rejected(self) -> None:
        manager = self.manager(max_files=2, max_bytes=20)
        first = manager.save_frame(_frame(b"a", "one.txt"))
        second = manager.save_frame(_frame(b"b", "two.txt"))
        self.assert_code(
            "too_many_files",
            lambda: manager.save_frame(_frame(b"c", "three.txt")),
        )
        manager.cleanup([first.path, second.path])

    def test_07_path_traversal_filename_is_display_only(self) -> None:
        manager = self.manager()
        upload = manager.save_frame(_frame(b"x", "../..\\outside/secret.txt"))
        self.assertEqual(upload.display_name, "secret.txt")
        self.assertEqual(upload.path.parent, self.root.resolve())
        self.assertNotIn("secret", upload.path.stem)
        self.assertFalse((Path(self.temp.name) / "outside").exists())

    def test_08_windows_reserved_filename_is_neutralized(self) -> None:
        manager = self.manager()
        for index, name in enumerate(("CON.txt", "folder\\nul", "LPT9.log")):
            with self.subTest(name=name):
                upload = manager.save_frame(_frame(bytes([index]), name))
                self.assertTrue(upload.display_name.startswith("_"))

    def test_09_identical_timestamp_and_client_name_do_not_collide(self) -> None:
        manager = self.manager()
        with mock.patch("time.time", return_value=1_700_000_000.0):
            first = manager.save_frame(_frame(b"first", "same.txt"))
            second = manager.save_frame(_frame(b"second", "same.txt"))
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(first.path.read_bytes(), b"first")
        self.assertEqual(second.path.read_bytes(), b"second")

    def test_10_concurrent_same_name_uploads_are_collision_safe(self) -> None:
        manager = self.manager(max_files=20, max_bytes=100)
        payloads = [bytes([index]) for index in range(12)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            uploads = list(
                pool.map(
                    lambda data: manager.save_frame(_frame(data, "same.bin")),
                    payloads,
                )
            )
        self.assertEqual(len({upload.path for upload in uploads}), len(payloads))
        self.assertEqual(
            {upload.path.read_bytes() for upload in uploads},
            set(payloads),
        )

    def test_11_storage_names_are_unique_server_generated_and_exclusive(self) -> None:
        tokens = iter(("0" * 32, "0" * 32, "1" * 32))
        manager = self.manager(
            max_bytes=10,
            token_factory=lambda _size: next(tokens),
        )
        first = manager.save_frame(_frame(b"a", "client-secret-name.txt"))
        second = manager.save_frame(_frame(b"b", "client-secret-name.txt"))
        self.assertRegex(first.path.name, r"^legacy-[0-9a-f]{32}\.txt$")
        self.assertRegex(second.path.name, r"^legacy-[0-9a-f]{32}\.txt$")
        self.assertNotEqual(first.path, second.path)
        self.assertNotIn("client-secret-name", first.path.name)

    def test_12_cleanup_after_successful_image_consumption(self) -> None:
        manager = self.manager()
        upload = manager.save_frame(_frame(b"image-bytes", "image.png"))

        async def scenario():
            async def consume():
                return upload.path.read_bytes()

            return await run_with_legacy_cleanup(manager, [upload.path], consume)

        self.assertEqual(asyncio.run(scenario()), b"image-bytes")
        self.assertFalse(upload.path.exists())

    def test_13_cleanup_after_successful_regular_file_consumption(self) -> None:
        manager = self.manager()
        upload = manager.save_frame(_frame(b"document", "document.txt"))

        async def scenario():
            async def consume():
                self.assertIn(str(upload.path), f"- {upload.path}")
                return "done"

            return await run_with_legacy_cleanup(manager, [upload.path], consume)

        self.assertEqual(asyncio.run(scenario()), "done")
        self.assertFalse(upload.path.exists())

    def test_14_cleanup_after_model_provider_exception(self) -> None:
        manager = self.manager()
        upload = manager.save_frame(_frame(b"provider", "provider.txt"))

        async def scenario():
            async def fail():
                raise RuntimeError("synthetic provider failure")

            await run_with_legacy_cleanup(manager, [upload.path], fail)

        with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
            asyncio.run(scenario())
        self.assertFalse(upload.path.exists())

    def test_15_cleanup_after_cancellation(self) -> None:
        manager = self.manager()
        upload = manager.save_frame(_frame(b"cancel", "cancel.txt"))

        async def scenario():
            started = asyncio.Event()

            async def wait_forever():
                started.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(
                run_with_legacy_cleanup(manager, [upload.path], wait_forever)
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertFalse(upload.path.exists())

    def test_16_cleanup_after_disconnect_is_idempotent(self) -> None:
        manager = self.manager()
        uploads = [
            manager.save_frame(_frame(b"a", "a.txt")),
            manager.save_frame(_frame(b"b", "b.txt")),
        ]
        paths = [upload.path for upload in uploads]
        self.assertEqual(manager.cleanup(paths), 2)
        self.assertEqual(manager.cleanup(paths), 0)
        self.assertFalse(any(path.exists() for path in paths))

    def test_17_restart_reconciliation_removes_only_owned_patterns(self) -> None:
        self.root.mkdir(parents=True)
        current = self.root / f"legacy-{'a' * 32}.txt"
        previous = self.root / "1700000000000_previous.txt"
        unrelated = self.root / "notes.txt"
        current.write_bytes(b"current")
        previous.write_bytes(b"previous")
        unrelated.write_bytes(b"unrelated")
        self.assertEqual(reconcile_legacy_uploads(self.root), 2)
        self.assertFalse(current.exists())
        self.assertFalse(previous.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(reconcile_legacy_uploads(self.root), 0)

    def test_18_restart_cleanup_does_not_delete_active_transactional_files(self) -> None:
        modern_root = Path(self.temp.name) / "transactional"
        active_batch = modern_root / "batch-active"
        active_batch.mkdir(parents=True)
        active_file = active_batch / "file-active.part"
        active_file.write_bytes(b"active")
        self.root.mkdir(parents=True)
        (self.root / f"legacy-{'b' * 32}.bin").write_bytes(b"orphan")
        reconcile_legacy_uploads(self.root)
        self.assertEqual(active_file.read_bytes(), b"active")

    def test_19_cleanup_never_deletes_outside_legacy_directory(self) -> None:
        manager = self.manager()
        outside = Path(self.temp.name) / f"legacy-{'c' * 32}.txt"
        outside.write_bytes(b"outside")
        self.assertEqual(manager.cleanup([outside]), 0)
        self.assertEqual(reconcile_legacy_uploads(self.root), 0)
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_20_modern_transactional_attachment_non_regression(self) -> None:
        modern_root = Path(self.temp.name) / "modern"
        manager = AttachmentBatchManager(modern_root)
        data = b"modern public payload"
        digest = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc)
        common = {
            "protocol_version": 1,
            "batch_id": "batch_public",
            "conversation_id": "",
            "client_request_id": "request_public",
            "request_id": "request_public",
        }

        def event(event_type: str, sequence: int, **fields) -> dict:
            return {
                "type": event_type,
                **common,
                "event_id": f"event_public_{sequence}",
                "sequence": sequence,
                **fields,
            }

        frames = [
            event(
                "attachment_batch_prepare",
                1,
                source_device_role="mobile",
                file_count=1,
                aggregate_size=len(data),
                created_at=now.isoformat(),
                expiry=(now + timedelta(minutes=15)).isoformat(),
                files=[{
                    "file_id": "file_public",
                    "logical_order": 0,
                    "display_name": "public.txt",
                    "original_extension": "txt",
                    "declared_mime": "text/plain",
                    "detected_type_category": "file",
                    "declared_byte_size": len(data),
                    "sha256": digest,
                    "chunk_size": 20 * 1024,
                    "expected_chunk_count": 1,
                }],
            ),
            event("attachment_file_begin", 2, file_id="file_public", logical_order=0),
            event(
                "attachment_file_chunk",
                3,
                file_id="file_public",
                chunk_index=0,
                expected_chunk_count=1,
                byte_offset=0,
                payload_length=len(data),
                data=base64.b64encode(data).decode("ascii"),
            ),
            event(
                "attachment_file_complete",
                4,
                file_id="file_public",
                declared_byte_size=len(data),
                sha256=digest,
            ),
            event(
                "attachment_batch_commit",
                5,
                file_count=1,
                aggregate_size=len(data),
            ),
        ]
        responses = []
        for frame in frames:
            responses.extend(manager.handle(frame))
        self.assertEqual(responses[-1]["type"], "attachment_batch_commit_ack")
        self.assertEqual(
            manager.consume_for_turn(
                "batch_public", "conversation_public", "request_public"
            )[0].path.read_bytes(),
            data,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
