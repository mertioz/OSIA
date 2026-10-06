"""Bundled legal text and local acceptance state for OSIA PC."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from osia.config import CONFIG_DIR


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


LEGAL_DATA_DIR = Path(__file__).resolve().parent / "legal_documents"
LEGAL_SOURCE_FILE = LEGAL_DATA_DIR / "legal-source.json"
LEGAL_MANIFEST_FILE = LEGAL_DATA_DIR / "legal-manifest.json"
LEGAL_SOURCE = _load_json(LEGAL_SOURCE_FILE)
LEGAL_MANIFEST = _load_json(LEGAL_MANIFEST_FILE)

TERMS_VERSION = str(LEGAL_MANIFEST.get("termsVersion", ""))
PRIVACY_VERSION = str(LEGAL_MANIFEST.get("privacyVersion", ""))
LAST_UPDATED = str(LEGAL_MANIFEST.get("lastUpdated", ""))
PUBLIC_CONTACT_EMAIL = str(LEGAL_MANIFEST.get("publicContactEmail", ""))
GOVERNING_LAW = str(LEGAL_MANIFEST.get("governingLaw", "France"))
EFFECTIVE_DATE = LEGAL_MANIFEST.get("effectiveDate")
MATERIAL_CHANGE = bool(LEGAL_MANIFEST.get("materialChange", False))
TERMS_ACCEPTANCE_REQUIRED = bool(
    LEGAL_MANIFEST.get("termsAcceptanceRequired", False)
)
PRIVACY_NOTICE_REQUIRED = bool(
    LEGAL_MANIFEST.get("privacyNoticeRequired", False)
)
MINIMUM_AGE: int | None = LEGAL_MANIFEST.get("minimumAge")
CANONICAL_SHA256 = str(LEGAL_MANIFEST.get("contentSha256", ""))
TERMS_URL = str(LEGAL_MANIFEST.get("canonicalUrls", {}).get("fr", {}).get("terms", ""))
PRIVACY_URL = str(LEGAL_MANIFEST.get("canonicalUrls", {}).get("fr", {}).get("privacy", ""))
LEGAL_MANIFEST_URL = str(
    LEGAL_MANIFEST.get("canonicalUrls", {}).get("fr", {}).get("manifest", "")
)
ACCEPTANCE_FILE = Path(CONFIG_DIR) / "legal_acceptance.json"


def get_document(document: str, locale: str = "en") -> dict[str, Any]:
    """Return a bundled Terms or Privacy document without network access."""
    selected_locale = "fr" if locale.lower().startswith("fr") else "en"
    documents = LEGAL_SOURCE.get("documents", {})
    localized = documents.get(selected_locale, {})
    result = localized.get(document)
    if not isinstance(result, dict):
        raise KeyError(f"Bundled legal document is unavailable: {selected_locale}/{document}")
    return result


def online_url(document: str, locale: str = "en") -> str:
    selected_locale = "fr" if locale.lower().startswith("fr") else "en"
    urls = LEGAL_MANIFEST.get("canonicalUrls", {}).get(selected_locale, {})
    key = "terms" if document == "terms" else "privacy"
    return str(urls.get(key, ""))


@dataclass(frozen=True)
class AcceptanceState:
    accepted_terms_version: str | None = None
    accepted_terms_at: str | None = None
    seen_privacy_version: str | None = None


@dataclass(frozen=True)
class StartupDecision:
    require_terms_acceptance: bool
    show_privacy_update: bool


def is_release_build() -> bool:
    return bool(getattr(sys, "frozen", False)) or os.environ.get(
        "OSIA_RELEASE_BUILD"
    ) == "1"


def startup_decision(
    state: AcceptanceState,
    *,
    release_build: bool | None = None,
    terms_version: str = TERMS_VERSION,
    privacy_version: str = PRIVACY_VERSION,
    material_change: bool = MATERIAL_CHANGE,
    acceptance_required: bool = TERMS_ACCEPTANCE_REQUIRED,
    privacy_notice_required: bool = PRIVACY_NOTICE_REQUIRED,
) -> StartupDecision:
    """Use explicit product acceptance flags, independently of publication status."""
    release = is_release_build() if release_build is None else release_build
    if not release:
        return StartupDecision(False, False)
    terms_changed = state.accepted_terms_version != terms_version
    require_terms = acceptance_required and (
        state.accepted_terms_version is None or (terms_changed and material_change)
    )
    return StartupDecision(
        require_terms,
        not require_terms
        and privacy_notice_required
        and state.seen_privacy_version != privacy_version,
    )


def load_acceptance(path: Path = ACCEPTANCE_FILE) -> AcceptanceState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return AcceptanceState()
    if not isinstance(raw, dict):
        return AcceptanceState()
    return AcceptanceState(
        accepted_terms_version=_clean(raw.get("accepted_terms_version")),
        accepted_terms_at=_clean(raw.get("accepted_terms_at")),
        seen_privacy_version=_clean(raw.get("seen_privacy_version")),
    )


def record_terms_acceptance(
    path: Path = ACCEPTANCE_FILE,
    *,
    accepted_at: datetime | None = None,
) -> AcceptanceState:
    when = (accepted_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = AcceptanceState(
        accepted_terms_version=TERMS_VERSION,
        accepted_terms_at=when.isoformat().replace("+00:00", "Z"),
        seen_privacy_version=PRIVACY_VERSION,
    )
    _save(state, path)
    return state


def record_privacy_seen(path: Path = ACCEPTANCE_FILE) -> AcceptanceState:
    current = load_acceptance(path)
    state = AcceptanceState(
        accepted_terms_version=current.accepted_terms_version,
        accepted_terms_at=current.accepted_terms_at,
        seen_privacy_version=PRIVACY_VERSION,
    )
    _save(state, path)
    return state


def _clean(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _save(state: AcceptanceState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "accepted_terms_version": state.accepted_terms_version,
        "accepted_terms_at": state.accepted_terms_at,
        "seen_privacy_version": state.seen_privacy_version,
    }
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
