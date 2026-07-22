"""Embedded, localized release history for the desktop application.

Remote updater notes describe a newer available build.  This module remains
the offline source of truth for versions already shipped with OSIA, so the
installed version's notes survive cache cleanup and network outages.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from osia.version import parse_product_version


@dataclass(frozen=True)
class ReleaseEntry:
    version: str
    released_on: str | None
    public: bool
    changes_en: tuple[str, ...]
    changes_fr: tuple[str, ...]

    def changes(self, language: str) -> tuple[str, ...]:
        return self.changes_fr if language == "fr" else self.changes_en


_RELEASES = (
    ReleaseEntry(
        version="1.0.5",
        released_on="2026-07-21",
        public=True,
        changes_en=(
            "Explore three guide levels: Getting Started, Master OSIA, and the developer-focused Advanced Guide.",
            "Learn every major OSIA feature through a complete, practical Master OSIA manual.",
            "Open the simplified guide page directly at its navigation and useful content.",
            "Review the changelog and update history offline from Settings.",
            "Close OSIA directly from the main menu.",
            "Send image attachments to compatible multimodal providers from PC or mobile.",
            "Attach files before sending and combine them with one message on PC.",
            "Responses now combine careful reasoning with clearer, more concise explanations.",
            "Settings navigation and content now scroll independently, with additional stability and distribution improvements.",
        ),
        changes_fr=(
            "Découvrez trois niveaux : Démarrage essentiel, Maîtriser OSIA et le Guide avancé destiné aux développeurs.",
            "Retrouvez chaque fonction majeure d’OSIA dans un manuel Maîtriser OSIA complet et pratique.",
            "Ouvrez la page de guide simplifiée directement sur sa navigation et son contenu utile.",
            "Consultez hors ligne le journal des modifications et l’historique des mises à jour depuis les Réglages.",
            "Fermez OSIA directement depuis le menu principal.",
            "Envoyez des images jointes aux fournisseurs multimodaux compatibles depuis le PC ou le mobile.",
            "Joignez des fichiers avant l’envoi et combinez-les avec un message sur PC.",
            "Les réponses associent désormais un raisonnement attentif à des explications plus claires et concises.",
            "La navigation et le contenu des Réglages défilent désormais indépendamment, avec des améliorations de stabilité et de distribution.",
        ),
    ),
    ReleaseEntry(
        version="1.0.4",
        released_on="2026-07-20",
        public=True,
        changes_en=(
            "Updates prepare more safely and can roll back if something goes wrong.",
            "The Windows installer and update screens have a clearer design.",
            "The update journey keeps your installation choices.",
        ),
        changes_fr=(
            "Les mises à jour se préparent plus sûrement et peuvent revenir en arrière en cas de problème.",
            "L’installateur Windows et les écrans de mise à jour sont plus clairs.",
            "Le parcours de mise à jour conserve vos choix d’installation.",
        ),
    ),
    ReleaseEntry(
        version="1.0.3",
        released_on="2026-07-20",
        public=False,
        changes_en=(
            "Added a dedicated OSIA update screen.",
            "Kept installation choices automatically during updates.",
            "Internal build superseded by 1.0.4 before public release.",
        ),
        changes_fr=(
            "Ajout d’un écran dédié aux mises à jour d’OSIA.",
            "Conservation automatique des choix d’installation pendant les mises à jour.",
            "Version interne remplacée par la 1.0.4 avant toute publication.",
        ),
    ),
)

RELEASE_HISTORY = tuple(
    sorted(_RELEASES, key=lambda entry: parse_product_version(entry.version), reverse=True)
)

_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_LEADING_MARKUP_RE = re.compile(r"^(?:#{1,6}\s+|[-+*>]\s+|\d+[.)]\s+)")
_WHITESPACE_RE = re.compile(r"\s+")


def release_for_version(version: str) -> ReleaseEntry | None:
    """Return the embedded entry for ``version`` without doing any I/O."""

    return next((entry for entry in RELEASE_HISTORY if entry.version == version), None)


def manifest_release_notes(version: str) -> list[str]:
    """Return plain English notes for the release manifest generator."""

    entry = release_for_version(version)
    return list(entry.changes_en) if entry is not None else []


def normalize_remote_notes(notes) -> tuple[str, ...]:
    """Normalize already-validated remote notes into bounded plain text.

    The updater rejects literal angle brackets at the manifest boundary.  This
    final presentation guard also decodes entities, removes any encoded tags,
    and strips common Markdown decoration before text reaches a Qt label.
    """

    normalized: list[str] = []
    for raw in notes or ():
        if not isinstance(raw, str):
            continue
        text = html.unescape(raw)
        text = _HTML_TAG_RE.sub("", text)
        text = _INLINE_LINK_RE.sub(r"\1", text)
        text = _LEADING_MARKUP_RE.sub("", text.strip())
        text = (
            text.replace("*", "").replace("_", "").replace("`", "")
            .replace("~~", "")
        )
        text = _WHITESPACE_RE.sub(" ", text).strip()
        if text:
            normalized.append(text[:240])
    return tuple(normalized[:5])


__all__ = [
    "RELEASE_HISTORY",
    "ReleaseEntry",
    "manifest_release_notes",
    "normalize_remote_notes",
    "release_for_version",
]
