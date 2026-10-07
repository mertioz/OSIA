import hashlib
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from osia import legal


def _canonical_legal_source_bytes(source):
    """Serialize the approved legal source in the site generator's key order.

    The approved SHA-256 covers JSON.stringify(source, null, 2) plus a trailing
    newline, not the Desktop copy's on-disk JSON formatting or key order.
    """
    operator = source["operator"]
    urls = source["urls"]
    documents = source["documents"]
    canonical = {
        "schemaVersion": source["schemaVersion"],
        "version": source["version"],
        "lastUpdated": source["lastUpdated"],
        "effectiveDate": source["effectiveDate"],
        "languages": source["languages"],
        "futureLanguages": source["futureLanguages"],
        "operator": {
            key: operator[key]
            for key in (
                "publicIdentity", "legalIdentity", "tradeName", "contact",
                "governingLaw",
            )
        },
        "agePolicy": source["agePolicy"],
        "termsAcceptanceRequired": source["termsAcceptanceRequired"],
        "privacyNoticeRequired": source["privacyNoticeRequired"],
        "materialChange": source["materialChange"],
        "urls": {
            locale: {
                key: urls[locale][key]
                for key in (
                    "terms", "privacy", "legalNotice", "thirdParty",
                    "licenses", "manifest",
                )
            }
            for locale in ("fr", "en")
        },
        "documents": {
            locale: {
                document: {
                    key: documents[locale][document][key]
                    for key in ("file", "slug", "title", "description", "markdown")
                }
                for document in (
                    "terms", "privacy", "legalNotice", "thirdParty", "licenses",
                )
            }
            for locale in ("en", "fr")
        },
    }
    return (json.dumps(canonical, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

class LegalManifestTests(unittest.TestCase):
    def test_bundled_manifest_matches_canonical_version_and_urls(self):
        self.assertEqual(legal.TERMS_VERSION, "2026-10-05.4")
        self.assertEqual(legal.PRIVACY_VERSION, "2026-10-05.4")
        self.assertEqual(legal.LAST_UPDATED, "2026-10-05")
        self.assertEqual(legal.EFFECTIVE_DATE, "2026-10-06")
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

    def test_source_hash_and_complete_english_french_offline_documents(self):
        self.assertEqual(
            hashlib.sha256(_canonical_legal_source_bytes(legal.LEGAL_SOURCE)).hexdigest(),
            legal.CANONICAL_SHA256,
        )
        self.assertEqual(
            legal.CANONICAL_SHA256,
            "80a13b01fad30ce9ef8b7974f4452fd0a2f653f78dc49ed75db54a7e921427a6",
        )
        for locale in ("en", "fr"):
            for document in ("terms", "privacy"):
                page = legal.get_document(document, locale)
                self.assertGreater(len(page["markdown"]), 7000)
                headings = re.findall(r"(?m)^## ", page["markdown"])
                self.assertGreaterEqual(len(headings), 30)

    def test_public_copy_has_no_draft_markers_or_retired_runtime_claims(self):
        corpus = str(legal.LEGAL_SOURCE["documents"])
        self.assertNotRegex(
            corpus,
            re.compile(
                r"draft|brouillon|legal review|revue juridique|not in force|non en vigueur",
                re.IGNORECASE,
            ),
        )
        self.assertNotRegex(
            corpus,
            re.compile(
                r"Autonomous|GoalContract|WorldState|permission broker|anti-replay|semantic equivalence",
                re.IGNORECASE,
            ),
        )
        self.assertNotRegex(
            corpus,
            re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{30,}|ghp_[A-Za-z0-9]{30,})\b"),
        )

    def test_legal_notice_contains_only_minimum_operator_approved_identity_fields(self):
        corpus = str(legal.LEGAL_SOURCE["documents"])
        markdown_corpus = "\n".join(
            page["markdown"]
            for locale in ("en", "fr")
            for page in legal.LEGAL_SOURCE["documents"][locale].values()
        ).replace("\n- ", " ")
        self.assertIn("SIREN : 101 523 215", corpus)
        self.assertIn("SIREN: 101 523 215", corpus)
        self.assertIn("Cloudflare, Inc.", corpus)
        self.assertIn("+1 (650) 319-8930", corpus)
        self.assertIn("Téléphone professionnel : 07 59 54 40 53", corpus)
        self.assertIn("Professional telephone: +33 7 59 54 40 53", corpus)
        self.assertIn("86 rue de Montagny", corpus)
        self.assertIn("69008 Lyon", corpus)
        for locale in ("en", "fr"):
            for key, page in legal.LEGAL_SOURCE["documents"][locale].items():
                if key == "legalNotice":
                    continue
                self.assertNotRegex(
                    page["markdown"],
                    re.compile(r"rue de Montagny|69008 Lyon|07 59 54 40 53|\+33 7 59 54 40 53", re.IGNORECASE),
                )
        self.assertEqual(
            set(re.findall(r"\b\d{5}\s+[A-Za-zÀ-ÿ-]+", markdown_corpus)),
            {"69008 Lyon", "94107 USA", "94107 États-Unis"},
        )
        self.assertNotRegex(corpus, re.compile(r"\bSIRET\b|\b(?:APE|NAF|RCS|RNE)\b|\bDPO\b", re.IGNORECASE))
        self.assertNotRegex(corpus, re.compile(r"\bVAT\s*(?:number|ID)?\s*[:#]", re.IGNORECASE))

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
