import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from osia import legal


class LegalManifestTests(unittest.TestCase):
    def test_bundled_manifest_matches_site_urls_and_review_version(self):
        self.assertEqual(legal.TERMS_URL, "https://osia.space/legal/terms")
        self.assertEqual(legal.PRIVACY_URL, "https://osia.space/legal/privacy")
        self.assertEqual(legal.TERMS_VERSION, "2026-07-14-draft.1")
        self.assertEqual(legal.PRIVACY_VERSION, "2026-07-14-draft.1")
        self.assertEqual(legal.PUBLICATION_STATE, "draftForLegalReview")
        self.assertEqual(legal.PUBLIC_CONTACT_EMAIL, "contact@osia.space")
        self.assertEqual(legal.GOVERNING_LAW, "France")
        self.assertEqual(legal.PLANNED_EFFECTIVE_YEAR, 2026)
        self.assertIsNone(legal.EFFECTIVE_DATE)

    def test_dev_and_review_builds_never_block(self):
        state = legal.AcceptanceState()
        self.assertFalse(
            legal.startup_decision(state, release_build=False).require_terms_acceptance
        )
        self.assertFalse(
            legal.startup_decision(state, release_build=True).require_terms_acceptance
        )

    def test_old_material_terms_require_one_new_acceptance(self):
        old = legal.AcceptanceState(
            accepted_terms_version="2026-07-01.1",
            accepted_terms_at="2026-07-01T00:00:00Z",
            seen_privacy_version="2026-08-01.1",
        )
        options = dict(
            release_build=True,
            publication_state="approved",
            terms_version="2026-08-01.1",
            privacy_version="2026-08-01.1",
            material_change=True,
            acceptance_required=True,
        )
        self.assertTrue(
            legal.startup_decision(old, **options).require_terms_acceptance
        )
        current = legal.AcceptanceState(
            accepted_terms_version="2026-08-01.1",
            accepted_terms_at="2026-08-01T00:00:00Z",
            seen_privacy_version="2026-08-01.1",
        )
        decision = legal.startup_decision(current, **options)
        self.assertFalse(decision.require_terms_acceptance)
        self.assertFalse(decision.show_privacy_update)

    def test_acceptance_round_trip_is_local(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legal_acceptance.json"
            accepted = legal.record_terms_acceptance(
                path,
                accepted_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(accepted.accepted_terms_version, legal.TERMS_VERSION)
            self.assertEqual(legal.load_acceptance(path), accepted)


if __name__ == "__main__":
    unittest.main()
