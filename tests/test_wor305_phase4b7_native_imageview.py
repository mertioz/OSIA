"""Focused native imageView lifecycle tests for WOR-305 Phase 4B.7."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt


class NativeImageViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "sample.png"
        Image.new("RGB", (3, 2), color=(12, 34, 56)).save(self.image)
        self.client = AppServerClient(base_dir=self.root / "account")
        self.attempt = _TurnAttempt(thread_id="thread-a", turn_id="turn-a")
        self.client._attempts[self.attempt.thread_id] = self.attempt
        self.interrupts = []
        self.client._send_request_no_wait = lambda method, params: self.interrupts.append((method, params))

    def payload(self, method="item/started", item_id="exec-image-1", path=None):
        return method, {
            "item": {"type": "imageView", "id": item_id,
                     "path": str(self.image) if path is None else path},
            "threadId": "thread-a", "turnId": "turn-a",
        }

    def dispatch(self, method="item/started", item_id="exec-image-1", path=None):
        method, params = self.payload(method, item_id, path)
        self.client._dispatch_notification(method, params)
        return params

    def image_events(self):
        return [event for event in self.attempt.capability_events if event.identity == "imageView"]

    def test_supported_provider_image_view_shape_is_correlated(self):
        # Keep the provider item shape synthetic and local to this regression;
        # the original capture has machine-specific paths and is release evidence.
        captured = [
            {"method": "item/started", "params": self.payload()[1]},
            {
                "method": "item/completed",
                "params": self.payload("item/completed")[1],
            },
        ]
        self.assertEqual([row["method"] for row in captured], ["item/started", "item/completed"])
        self.assertEqual(set(captured[0]["params"]["item"]), {"type", "id", "path"})
        self.assertEqual(captured[0]["params"]["item"], captured[1]["params"]["item"])
        for row in captured:
            params = deepcopy(row["params"])
            params["threadId"] = "thread-a"
            params["turnId"] = "turn-a"
            params["item"]["path"] = str(self.image)
            self.client._dispatch_notification(row["method"], params)
        self.assertEqual([event.outcome for event in self.image_events()],
                         ["running_read_only", "success_verified"])
        self.assertFalse(self.interrupts)

    def test_valid_view_preserves_exact_image_and_turn(self):
        self.dispatch()
        self.dispatch("item/completed")
        selected, completed = self.image_events()
        digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.assertEqual(selected.item_id, completed.item_id)
        self.assertEqual(completed.arguments["path"], str(self.image.resolve()))
        self.assertEqual(completed.arguments["sha256"], digest)
        self.assertEqual((completed.arguments["width"], completed.arguments["height"]), (3, 2))
        self.assertFalse(self.attempt.native_action)

    def test_malformed_or_missing_image_never_claims_observation(self):
        self.dispatch(path="relative.png")
        self.assertEqual(self.image_events(), [])
        self.assertEqual(self.attempt.unsafe_category, "invalid_native_item:imageView")
        self.assertEqual(len(self.interrupts), 1)

    def test_foreign_turn_cannot_complete_selected_image(self):
        self.dispatch()
        method, params = self.payload("item/completed")
        params["turnId"] = "foreign-turn"
        self.client._dispatch_notification(method, params)
        self.assertEqual(len(self.image_events()), 1)
        self.assertIn("exec-image-1", self.attempt.native_image_views)
        self.assertFalse(self.interrupts)

    def test_wrong_item_or_changed_file_cannot_complete(self):
        self.dispatch()
        self.dispatch("item/completed", item_id="foreign-item")
        self.assertEqual([e.outcome for e in self.image_events()], ["running_read_only"])
        self.assertEqual(self.attempt.unsafe_category, "invalid_native_item:imageView")

        other = _TurnAttempt(thread_id="thread-b", turn_id="turn-b")
        self.client._attempts["thread-b"] = other
        self.client._dispatch_notification("item/started", {
            "item": {"type": "imageView", "id": "exec-change", "path": str(self.image)},
            "threadId": "thread-b", "turnId": "turn-b",
        })
        Image.new("RGB", (3, 2), color=(56, 34, 12)).save(self.image)
        self.client._dispatch_notification("item/completed", {
            "item": {"type": "imageView", "id": "exec-change", "path": str(self.image)},
            "threadId": "thread-b", "turnId": "turn-b",
        })
        self.assertEqual([e.outcome for e in other.capability_events if e.identity == "imageView"],
                         ["running_read_only"])
        self.assertEqual(other.unsafe_category, "invalid_native_item:imageView")

    def test_repeated_views_have_distinct_read_only_completions(self):
        for item_id in ("exec-image-1", "exec-image-2"):
            self.dispatch(item_id=item_id)
            self.dispatch("item/completed", item_id=item_id)
        self.assertEqual([e.item_id for e in self.image_events() if e.phase == "completed"],
                         ["exec-image-1", "exec-image-2"])
        self.assertFalse(self.attempt.native_effect_started)
        self.assertFalse(self.interrupts)


if __name__ == "__main__":
    unittest.main()
