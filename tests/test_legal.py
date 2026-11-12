import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from osia import legal


class LegalManifestTests(unittest.TestCase):
    def test_bundled_manifest_matches_canonical_version_and_urls(self):
        self.assertEqual(legal.TERMS_VERSION, "2026-11-12.1")
        self.assertEqual(legal.PRIVACY_VERSION, "2026-11-12.1")
        self.assertEqual(legal.LAST_UPDATED, "2026-11-12")
        self.assertEqual(legal.EFFECTIVE_DATE, "2026-11-12")
        self.assertEqual(legal.PUBLIC_CONTACT_EMAIL, "contact@osia.space")
        self.assertEqual(legal.GOVERNING_LAW, "France")
        self.assertIsNone(legal.MINIMUM_AGE)
        self.assertFalse(legal.TERMS_ACCEPTANCE_REQUIRED)
        self.assertFalse(legal.PRIVACY_NOTICE_REQUIRED)
        self.assertEqual(
            legal.LEGAL_MANIFEST["canonicalUrls"]["en"]["terms"],
            "https://osia.space/en/legal/terms",
        )
        self.assertEqual(
            legal.LEGAL_MANIFEST["canonicalUrls"]["fr"]["privacy"],
            "https://osia.space/legal/privacy",
        )

    def test_legal_links_follow_the_interface_language(self):
        self.assertEqual(legal.online_url("terms", "fr-CA"), "https://osia.space/legal/terms")
        self.assertEqual(
            legal.online_url("legalNotice", "fr"), "https://osia.space/legal/mentions-legales",
        )
        self.assertEqual(
            legal.online_url("privacy", "ja"), "https://osia.space/en/legal/privacy",
        )
        self.assertEqual(legal.online_url("unknown"), "")

    def test_the_legal_bundle_contains_only_the_manifest(self):
        self.assertEqual(
            sorted(path.name for path in legal.LEGAL_DATA_DIR.glob("*.json")),
            ["legal-manifest.json"],
        )

    def test_startup_does_not_block_without_an_explicit_acceptance_requirement(self):
        state = legal.AcceptanceState()
        for release in (False, True):
            decision = legal.startup_decision(state, release_build=release)
            self.assertFalse(decision.require_terms_acceptance)
            self.assertFalse(decision.show_privacy_update)

    def test_acceptance_is_controlled_independently_from_document_release_status(self):
        empty = legal.AcceptanceState()
        decision = legal.startup_decision(
            empty,
            release_build=True,
            terms_version="2026-10-04.1",
            material_change=True,
            acceptance_required=True,
        )
        self.assertTrue(decision.require_terms_acceptance)
        accepted = legal.AcceptanceState(
            accepted_terms_version="2026-10-04.1",
            accepted_terms_at="2026-10-04T00:00:00Z",
            seen_privacy_version="2026-10-04.1",
        )
        decision = legal.startup_decision(
            accepted,
            release_build=True,
            terms_version="2026-10-04.1",
            privacy_version="2026-10-04.1",
            acceptance_required=True,
            privacy_notice_required=True,
        )
        self.assertFalse(decision.require_terms_acceptance)
        self.assertFalse(decision.show_privacy_update)

    def test_acceptance_round_trip_is_local(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legal_acceptance.json"
            accepted = legal.record_terms_acceptance(
                path,
                accepted_at=datetime(2026, 10, 4, tzinfo=timezone.utc),
            )
            self.assertEqual(accepted.accepted_terms_version, legal.TERMS_VERSION)
            self.assertEqual(legal.load_acceptance(path), accepted)


if __name__ == "__main__":
    unittest.main()
