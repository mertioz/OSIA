#!/usr/bin/env python3
"""Focused WOR-89 Phase A tests for :mod:`osia.memory`.

Pure local-store tests: every store instance points at a temporary directory, so
the real CONFIG_DIR/memory.json is never created or modified by this script.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.memory import MemorySecretError, MemoryStore, SCHEMA_VERSION


def _fields(**overrides):
    base = {
        "type": "route",
        "category": "app",
        "key": "Discord",
        "title": "Discord",
        "value": {"kind": "path", "data": r"C:\Program Files\Discord\Discord.exe"},
        "confidence": 1.4,
        "source": "explicit_user",
        "tags": [" chat ", "voice", "chat"],
        "device": {"origin": "pc", "platform": "win32"},
    }
    base.update(overrides)
    return base


class MemoryStoreTests(unittest.TestCase):
    def make_store(self, tmp: str) -> tuple[MemoryStore, Path]:
        path = Path(tmp) / "memory.json"
        return MemoryStore(path), path

    def test_empty_load_does_not_write_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self.make_store(tmp)
            snapshot = store.load()
            self.assertEqual(snapshot["schema_version"], SCHEMA_VERSION)
            self.assertEqual(snapshot["entries"], [])
            self.assertFalse(path.exists())

    def test_create_upsert_list_get_normalizes_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.make_store(tmp)
            entry = store.upsert_entry(_fields())

            self.assertTrue(entry["id"].startswith("mem_"))
            self.assertEqual(entry["key"], "discord")
            self.assertEqual(entry["title"], "Discord")
            self.assertEqual(entry["confidence"], 1.0)
            self.assertEqual(entry["tags"], ["chat", "voice"])
            self.assertEqual(entry["value"]["kind"], "path")

            self.assertEqual(len(store.list_entries()), 1)
            self.assertEqual(store.get_entry(entry["id"])["id"], entry["id"])

    def test_update_preserves_created_at_and_changes_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.make_store(tmp)
            first = store.upsert_entry(_fields())
            time.sleep(0.01)
            updated = store.upsert_entry(
                _fields(
                    id=first["id"],
                    title="Discord Stable",
                    value={"kind": "path", "data": r"C:\Apps\Discord.exe"},
                )
            )

            self.assertEqual(updated["created_at"], first["created_at"])
            self.assertNotEqual(updated["updated_at"], first["updated_at"])
            self.assertEqual(updated["title"], "Discord Stable")

    def test_delete_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.make_store(tmp)
            first = store.upsert_entry(_fields(key="Discord", title="Discord"))
            second = store.upsert_entry(
                _fields(
                    key="Steam",
                    title="Steam",
                    value={"kind": "path", "data": r"C:\Program Files\Steam\steam.exe"},
                )
            )

            self.assertTrue(store.delete_entry(first["id"]))
            self.assertFalse(store.delete_entry(first["id"]))
            self.assertEqual([e["id"] for e in store.list_entries()], [second["id"]])

            store.clear()
            self.assertEqual(store.list_entries(), [])

    def test_search_relevance_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.make_store(tmp)
            discord = store.upsert_entry(_fields(key="Discord", title="Discord"))
            store.upsert_entry(
                _fields(
                    type="route",
                    category="folder",
                    key="projects",
                    title="Projects Folder",
                    value={"kind": "path", "data": r"C:\Users\vertu\Projects"},
                )
            )

            results = store.search("discord voice", limit=5)
            self.assertGreaterEqual(len(results), 1)
            self.assertEqual(results[0]["id"], discord["id"])
            self.assertEqual(store.search("projects", categories=["folder"])[0]["key"], "projects")

    def test_secret_rejection(self):
        secret_values = [
            "sk-" + "a" * 40,
            "ghp_" + "b" * 40,
            "github_pat_" + "c" * 40,
            "xoxb-" + "d" * 40,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
            "access_token=abcdef1234567890abcdef",
            "Ab3_DeF456GhIjK789LmNoP012QrStU345VwXyZ678aBcDeF901GhIjK",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.make_store(tmp)
            for index, value in enumerate(secret_values):
                with self.subTest(index=index):
                    with self.assertRaises(MemorySecretError):
                        store.upsert_entry(
                            _fields(
                                key=f"secret-{index}",
                                title=f"Secret {index}",
                                value={"kind": "text", "data": value},
                            )
                        )

            with self.assertRaises(MemorySecretError):
                store.upsert_entry(
                    _fields(
                        key="oauth",
                        title="OAuth",
                        value={
                            "kind": "text",
                            "data": {"access_token": "abcdef1234567890abcdef"},
                        },
                    )
                )

    def test_path_and_url_normal_values_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.make_store(tmp)
            path_entry = store.upsert_entry(_fields())
            url_entry = store.upsert_entry(
                _fields(
                    type="route",
                    category="site",
                    key="dashboard",
                    title="Dashboard",
                    value={"kind": "url", "data": "https://example.com/settings?view=general"},
                )
            )

            self.assertEqual(path_entry["value"]["kind"], "path")
            self.assertEqual(url_entry["value"]["kind"], "url")

    def test_atomic_save_writes_valid_json_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self.make_store(tmp)
            store.upsert_entry(_fields())

            data = json.loads(path.read_text(encoding="utf-8"))
            backup = path.with_name("memory.json.bak")
            backup_data = json.loads(backup.read_text(encoding="utf-8"))

            self.assertEqual(data["schema_version"], SCHEMA_VERSION)
            self.assertEqual(backup_data["schema_version"], SCHEMA_VERSION)
            self.assertFalse(list(path.parent.glob("memory.json.tmp*")))

    def test_backup_recovery_restores_valid_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self.make_store(tmp)
            entry = store.upsert_entry(_fields())
            path.write_text("{not valid json", encoding="utf-8")

            recovered = MemoryStore(path).load()

            self.assertEqual(len(recovered["entries"]), 1)
            self.assertEqual(recovered["entries"][0]["id"], entry["id"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["entries"][0]["id"], entry["id"])

    def test_corrupt_live_and_bad_backup_moves_corrupt_file_and_loads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self.make_store(tmp)
            path.write_text("{not valid json", encoding="utf-8")
            path.with_name("memory.json.bak").write_text("{also bad", encoding="utf-8")

            snapshot = store.load()

            self.assertEqual(snapshot["entries"], [])
            self.assertFalse(path.exists())
            corrupt_files = list(path.parent.glob("memory.corrupt.*.json"))
            self.assertEqual(len(corrupt_files), 1)

    def test_export_snapshot_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.make_store(tmp)
            entry = store.upsert_entry(_fields())
            snapshot = store.export_snapshot()

            self.assertEqual(snapshot["schema_version"], SCHEMA_VERSION)
            self.assertEqual(snapshot["entries"][0]["id"], entry["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
