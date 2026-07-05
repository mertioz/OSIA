#!/usr/bin/env python3
"""Runtime helpers that connect OSIA memory to the local store.

The model-facing save path is the internal ``save_memory`` tool. The older
language-specific detectors remain as conservative helpers and test fixtures,
but agent turns no longer depend on them to decide what should be saved.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unicodedata
import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from osia.memory import (
    MemorySecretError,
    MemoryStore,
    MemoryValidationError,
)


MAX_CONTEXT_ENTRIES = 8
MAX_CONTEXT_CHARS = 1600
AUTO_MEMORY_MAX_CHARS = 220
AUTO_MEMORY_MAX_WORDS = 34


_SAVE_MEMORY_CONTEXT = contextvars.ContextVar("osia_save_memory_context", default=None)


@dataclass(frozen=True)
class ExplicitMemoryInstruction:
    trigger: str
    content: str


@dataclass(frozen=True)
class CandidateMemory:
    reason: str
    entry: dict[str, Any]


@dataclass(frozen=True)
class MemorySaveResult:
    matched: bool
    saved: bool = False
    entry: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None


class _ProtectedExplicitMemory(Exception):
    """Raised when a weaker model candidate would overwrite explicit memory."""


_TRIGGER_PATTERNS = [
    ("retiens que", re.compile(r"^\s*retiens\s+que\s*(?P<content>[\s\S]*?)\s*$")),
    (
        "souviens-toi que",
        re.compile(r"^\s*souviens[- ]toi\s+que\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    (
        "mémorise que",
        re.compile(r"^\s*memorise\s+que\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    ("note que", re.compile(r"^\s*note\s+que\s*(?P<content>[\s\S]*?)\s*$")),
    (
        "garde ça en tête",
        re.compile(
            r"^\s*garde\s+(?:ca|cela)\s+en\s+tete\s*(?:[:,-]\s*|que\s+)"
            r"(?P<content>[\s\S]*?)\s*$"
        ),
    ),
    (
        "garde en tête que",
        re.compile(r"^\s*garde\s+en\s+tete\s+que\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    (
        "à retenir",
        re.compile(r"^\s*a\s+retenir\s*[:,-]\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    (
        "pour plus tard",
        re.compile(r"^\s*pour\s+plus\s+tard\s*[:, -]\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    (
        "pour la prochaine fois",
        re.compile(
            r"^\s*pour\s+la\s+prochaine\s+fois\s*[:, -]\s*"
            r"(?P<content>[\s\S]*?)\s*$"
        ),
    ),
    (
        "note dans ta mémoire que",
        re.compile(
            r"^\s*note\s+dans\s+ta\s+memoire\s+que\s*(?P<content>[\s\S]*?)\s*$"
        ),
    ),
    (
        "mets dans ta mémoire que",
        re.compile(
            r"^\s*mets\s+dans\s+ta\s+memoire\s+que\s*(?P<content>[\s\S]*?)\s*$"
        ),
    ),
    (
        "souviens t'en",
        re.compile(r"^\s*souviens\s+t[' ]?en\s*[:, -]\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    (
        "souviens-toi",
        re.compile(r"^\s*souviens[- ]toi\s*[:, -]\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    (
        "remember that",
        re.compile(r"^\s*remember\s+that\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    ("save that", re.compile(r"^\s*save\s+that\s*(?P<content>[\s\S]*?)\s*$")),
    ("remember:", re.compile(r"^\s*remember\s*:\s*(?P<content>[\s\S]*?)\s*$")),
    ("note:", re.compile(r"^\s*note\s*:\s*(?P<content>[\s\S]*?)\s*$")),
    (
        "remember this",
        re.compile(r"^\s*remember\s+this\s*:\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    (
        "keep in mind that",
        re.compile(r"^\s*keep\s+in\s+mind\s+that\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    (
        "for later",
        re.compile(r"^\s*for\s+later\s*[:, -]\s*(?P<content>[\s\S]*?)\s*$"),
    ),
    (
        "save this for later",
        re.compile(
            r"^\s*save\s+this\s+for\s+later\s*:\s*(?P<content>[\s\S]*?)\s*$"
        ),
    ),
]

_ABOUT_ME_PATTERNS = [
    re.compile(r"\btu\s+sais\s+quoi\s+de\s+moi\b"),
    re.compile(r"\bqu[' ]?est[- ]ce\s+que\s+tu\s+sais\s+de\s+moi\b"),
    re.compile(r"\bque\s+sais[- ]tu\s+de\s+moi\b"),
    re.compile(r"\bqu[' ]?est[- ]ce\s+que\s+tu\s+as\s+memorise\s+sur\s+moi\b"),
    re.compile(r"\bqu[' ]?est[- ]ce\s+que\s+tu\s+sais\s+de\s+mes\s+preferences\b"),
    re.compile(r"\bwhat\s+do\s+you\s+know\s+about\s+me\b"),
    re.compile(r"\bwhat\s+do\s+you\s+remember\s+about\s+me\b"),
    re.compile(r"\bwhat\s+do\s+you\s+know\s+about\s+my\s+preferences\b"),
]

_PREFERENCE_MARKERS = (
    "j'aime",
    "j'adore",
    "j adore",
    "jaime",
    "je prefere",
    "je deteste",
    "je n'aime pas",
    "j'utilise",
    "ma preference",
    "ma preferee",
    "mon prefere",
    "i like",
    "i love",
    "i prefer",
    "i dislike",
    "i use",
    "my favorite",
    "my preference",
)

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "dans",
    "de",
    "des",
    "d",
    "du",
    "en",
    "est",
    "fact",
    "i",
    "is",
    "j",
    "je",
    "la",
    "le",
    "les",
    "ma",
    "me",
    "mes",
    "moi",
    "mon",
    "my",
    "n",
    "pas",
    "que",
    "qui",
    "quoi",
    "sont",
    "that",
    "the",
    "to",
    "un",
    "une",
    "user",
    "aime",
    "adore",
    "prefere",
    "deteste",
    "utilise",
    "like",
    "love",
    "prefer",
    "dislike",
    "use",
}

_TEMPORARY_TERMS = (
    "aujourd'hui",
    "aujourdhui",
    "maintenant",
    "ce soir",
    "demain",
    "hier",
    "cette semaine",
    "la semaine prochaine",
    "today",
    "right now",
    "now",
    "tonight",
    "tomorrow",
    "yesterday",
    "this week",
    "next week",
)

_UNCERTAIN_TERMS = (
    "peut etre",
    "peut-etre",
    "je crois",
    "je pense",
    "pas sur",
    "pas sûr",
    "probablement",
    "maybe",
    "i think",
    "i guess",
    "not sure",
    "probably",
    "blague",
    "pour rire",
    "joke",
    "kidding",
    "lol",
    "mdr",
)

_QUESTION_START_RE = re.compile(
    r"^\s*(?:est[- ]ce\s+que|peux[- ]tu|pourquoi|comment|"
    r"can\s+you|could\s+you|do\s+you|does\s+|what\s+|why\s+|how\s+|is\s+|are\s+)"
)
_SOCIAL_QUESTION_TAIL_PATTERNS = (
    re.compile(r"(?:[,;]\s*)?(?:tu\s+savais|non|ok|d['\u2019 ]?accord)\s*\?\s*$", re.IGNORECASE),
    re.compile(r"(?:[,;]\s*)?(?:you\s+know|right)\s*\?\s*$", re.IGNORECASE),
)
_AUTO_LEADING_PREFIX_RE = re.compile(r"^\s*(?:et|and)\s+", re.IGNORECASE)


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    without_marks = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    without_marks = (
        without_marks.replace("’", "'")
        .replace("‘", "'")
        .replace("ʼ", "'")
    )
    without_marks = without_marks.translate({
        ord("\u2019"): "'",
        ord("\u2018"): "'",
        ord("\u02bc"): "'",
        ord("\u0060"): "'",
    })
    return without_marks.casefold()


def _trim_sentence(text: str) -> str:
    return str(text or "").strip().strip(" \t\r\n.")


def _strip_social_question_tail(text: str) -> tuple[str, bool]:
    stripped = str(text or "").strip()
    for pattern in _SOCIAL_QUESTION_TAIL_PATTERNS:
        updated = pattern.sub("", stripped).rstrip(" \t,;:")
        if updated != stripped:
            return updated, True
    return stripped, False


def _normalize_auto_memory_content(text: str) -> str:
    content, _tail_removed = _strip_social_question_tail(text)
    content = _AUTO_LEADING_PREFIX_RE.sub("", content).strip()
    content = re.sub(
        r"^\s*j(['\u2019 ]?)(aime|adore|utilise)\s+aussi\s+",
        r"j\1\2 ",
        content,
        flags=re.IGNORECASE,
    )
    content = re.sub(
        r"^\s*je\s+(pr[eé]f[eè]re|deteste|d[eé]teste)\s+aussi\s+",
        r"je \1 ",
        content,
        flags=re.IGNORECASE,
    )
    content = re.sub(
        r"^\s*i\s+also\s+(like|love|prefer|dislike|use)\s+",
        r"i \1 ",
        content,
        flags=re.IGNORECASE,
    )
    content = re.sub(
        r"^\s*i\s+(like|love|prefer|dislike|use)\s+also\s+",
        r"i \1 ",
        content,
        flags=re.IGNORECASE,
    )
    return content.strip()


def _ensure_sentence(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return text
    if text[-1] not in ".!?":
        return f"{text}."
    return text


def _is_probably_question(text: str) -> bool:
    folded = _fold(text)
    return "?" in text or bool(_QUESTION_START_RE.search(folded))


def _contains_phrase(folded_text: str, phrase: str) -> bool:
    if " " in phrase or "'" in phrase or "-" in phrase:
        return phrase in folded_text
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", folded_text))


def _auto_shape_is_safe(user_text: str) -> bool:
    text = str(user_text or "").strip()
    if not text or len(text) > AUTO_MEMORY_MAX_CHARS or "\n" in text:
        return False
    if len(re.findall(r"\S+", text)) > AUTO_MEMORY_MAX_WORDS:
        return False
    if _is_probably_question(text):
        return False
    folded = _fold(text)
    if any(_contains_phrase(folded, term) for term in _TEMPORARY_TERMS):
        return False
    if any(_contains_phrase(folded, term) for term in _UNCERTAIN_TERMS):
        return False
    return True


def looks_like_unsaved_memory_statement(user_text: str) -> bool:
    """Return True when a normal turn resembles memory but was not saved."""

    content = _normalize_auto_memory_content(user_text)
    if not content:
        return False
    folded = _fold(content)
    if any(marker in folded for marker in _PREFERENCE_MARKERS):
        return True
    if re.search(r"\b(?:mon|ma|mes|my)\b.+\b(?:est|sont|is|are)\b", folded):
        return True
    if folded.startswith("quand je dis ") or folded.startswith("when i say "):
        return True
    return False


def detect_explicit_memory_instruction(
    user_text: str,
) -> ExplicitMemoryInstruction | None:
    """Return an explicit memory instruction when the turn starts with one."""

    original = str(user_text or "")
    folded = _fold(original)
    for trigger, pattern in _TRIGGER_PATTERNS:
        match = pattern.match(folded)
        if not match:
            continue
        start = match.start("content")
        end = match.end("content")
        return ExplicitMemoryInstruction(
            trigger=trigger,
            content=original[start:end].strip(),
        )
    return None


def looks_like_preference(content: str) -> bool:
    folded = _fold(content)
    return any(marker in folded for marker in _PREFERENCE_MARKERS)


def _memory_value_text(content: str) -> str:
    content = str(content or "").strip()
    patterns = [
        (r"^je\s+n[' ]?aime\s+pas\s+(.+)$", "L'utilisateur n'aime pas {topic}."),
        (r"^j[' ]?aime\s+(.+)$", "L'utilisateur aime {topic}."),
        (r"^j[' ]?adore\s+(.+)$", "L'utilisateur adore {topic}."),
        (r"^je\s+prefere\s+(.+)$", "L'utilisateur préfère {topic}."),
        (r"^je\s+deteste\s+(.+)$", "L'utilisateur déteste {topic}."),
        (r"^j[' ]?utilise\s+(.+)$", "L'utilisateur utilise {topic}."),
        (r"^i\s+do\s+not\s+like\s+(.+)$", "The user does not like {topic}."),
        (r"^i\s+like\s+(.+)$", "The user likes {topic}."),
        (r"^i\s+love\s+(.+)$", "The user loves {topic}."),
        (r"^i\s+prefer\s+(.+)$", "The user prefers {topic}."),
        (r"^i\s+dislike\s+(.+)$", "The user dislikes {topic}."),
        (r"^i\s+use\s+(.+)$", "The user uses {topic}."),
    ]
    folded = _fold(content)
    for pattern, template in patterns:
        match = re.match(pattern, folded, flags=re.IGNORECASE)
        if not match:
            continue
        topic = _trim_sentence(content[match.start(1):match.end(1)])
        return template.format(topic=topic)
    return _ensure_sentence(content)


def _title_topic(content: str) -> str:
    folded = _fold(content)
    for prefix in (
        "je n'aime pas",
        "j'aime",
        "jaime",
        "j'adore",
        "je prefere",
        "je deteste",
        "j'utilise",
        "i do not like",
        "i like",
        "i love",
        "i prefer",
        "i dislike",
        "i use",
    ):
        if folded.startswith(prefix):
            return _trim_sentence(content[len(prefix):]) or _trim_sentence(content)
    return _trim_sentence(content)


def _memory_key(content: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", _fold(content))
    useful = [token for token in tokens if token not in _STOPWORDS]
    if not useful:
        useful = tokens
    key = "-".join(useful[:5]).strip("-")
    return (key or "user-memory")[:64]


def _without_owner(label: str) -> str:
    return re.sub(
        r"^\s*(?:mon|ma|mes|my)\s+",
        "",
        str(label or "").strip(),
        flags=re.IGNORECASE,
    )


def _clean_path_or_url(value: str) -> str:
    return str(value or "").strip().strip("\"'`“”‘’").rstrip(".,;")


def _base_entry(
    *,
    memory_type: str,
    category: str,
    key: str,
    title: str,
    value_kind: str,
    value_data: Any,
    source: str,
    confidence: float,
) -> dict[str, Any]:
    tag = source if source in {"explicit_user", "agent_candidate", "verified_tool"} else "agent_candidate"
    return {
        "type": memory_type,
        "category": category,
        "key": _memory_key(key),
        "title": title,
        "value": {
            "kind": value_kind,
            "data": value_data,
        },
        "confidence": confidence,
        "source": source,
        "tags": [tag],
        "device": {
            "origin": "pc",
            "platform": sys.platform,
        },
    }


def _match_folded(original: str, patterns: list[re.Pattern[str]]) -> re.Match[str] | None:
    folded = _fold(original)
    for pattern in patterns:
        match = pattern.match(folded)
        if match:
            return match
    return None


def _build_folder_route_entry(
    content: str,
    *,
    source: str,
    confidence: float,
) -> dict[str, Any] | None:
    path = r"(?P<path>(?:[a-z]:[\\/]|~[\\/]|/)[^,;\r\n]+)"
    patterns = [
        re.compile(
            rf"^(?P<label>(?:mon|ma|mes)\s+[^,;\r\n]*\bdossier\b[^,;\r\n]*)"
            rf"\s+(?:est|se\s+trouve)\s+(?:dans\s+|a\s+)?{path}\s*$"
        ),
        re.compile(
            rf"^(?P<label>my\s+[^,;\r\n]*\bfolder\b[^,;\r\n]*)"
            rf"\s+(?:is|lives|is\s+located)\s+(?:in\s+|at\s+)?{path}\s*$"
        ),
    ]
    match = _match_folded(content, patterns)
    if not match:
        return None
    label = _without_owner(content[match.start("label"):match.end("label")])
    path_value = _clean_path_or_url(content[match.start("path"):match.end("path")])
    if not path_value:
        return None
    topic = _trim_sentence(label) or "folder"
    return _base_entry(
        memory_type="route",
        category="folder",
        key=topic,
        title=f"Folder: {topic[:60]}",
        value_kind="path",
        value_data=path_value,
        source=source,
        confidence=confidence,
    )


def _build_site_route_entry(
    content: str,
    *,
    source: str,
    confidence: float,
) -> dict[str, Any] | None:
    url = r"(?P<url>https?://[^\s,;\r\n]+)"
    patterns = [
        re.compile(rf"^(?P<label>(?:mon|ma|mes)\s+[^,;\r\n]+)\s+(?:est|sont)\s+{url}\s*$"),
        re.compile(rf"^(?P<label>my\s+[^,;\r\n]+)\s+(?:is|are)\s+{url}\s*$"),
    ]
    match = _match_folded(content, patterns)
    if not match:
        return None
    url_value = _clean_path_or_url(content[match.start("url"):match.end("url")])
    parsed = urlparse(url_value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    label = _without_owner(content[match.start("label"):match.end("label")])
    topic = _trim_sentence(label) or parsed.netloc
    return _base_entry(
        memory_type="route",
        category="site",
        key=topic,
        title=f"Site: {topic[:60]}",
        value_kind="url",
        value_data=url_value,
        source=source,
        confidence=confidence,
    )


def _build_workflow_entry(
    content: str,
    *,
    source: str,
    confidence: float,
) -> dict[str, Any] | None:
    patterns = [
        re.compile(
            r"^quand\s+je\s+dis\s+(?P<trigger>[^,]+),?\s+"
            r"(?P<action>(?:ouvre|lance|affiche|va\s+sur)\s+[\s\S]+?)\s*$"
        ),
        re.compile(
            r"^when\s+i\s+say\s+(?P<trigger>[^,]+),?\s+"
            r"(?P<action>(?:open|launch|show|go\s+to)\s+[\s\S]+?)\s*$"
        ),
    ]
    match = _match_folded(content, patterns)
    if not match:
        return None
    trigger = _trim_sentence(content[match.start("trigger"):match.end("trigger")])
    action = _trim_sentence(content[match.start("action"):match.end("action")])
    if not trigger or not action:
        return None
    return _base_entry(
        memory_type="workflow",
        category="workflow",
        key=f"workflow {trigger}",
        title=f"Workflow: {trigger[:60]}",
        value_kind="steps",
        value_data=[_ensure_sentence(f"Quand l'utilisateur dit {trigger}, {action}")],
        source=source,
        confidence=confidence,
    )


def _build_preference_entry(
    content: str,
    *,
    source: str,
    confidence: float,
) -> dict[str, Any] | None:
    patterns = [
        re.compile(r"^je\s+n[' ]?aime\s+pas\s+.+$"),
        re.compile(r"^j[' ]?aime\s+.+$"),
        re.compile(r"^j[' ]?adore\s+.+$"),
        re.compile(r"^je\s+prefere\s+.+$"),
        re.compile(r"^je\s+deteste\s+.+$"),
        re.compile(r"^j[' ]?utilise\s+.+$"),
        re.compile(r"^i\s+do\s+not\s+like\s+.+$"),
        re.compile(r"^i\s+like\s+.+$"),
        re.compile(r"^i\s+love\s+.+$"),
        re.compile(r"^i\s+prefer\s+.+$"),
        re.compile(r"^i\s+dislike\s+.+$"),
        re.compile(r"^i\s+use\s+.+$"),
    ]
    if not _match_folded(content, patterns):
        return None
    topic = _title_topic(content)
    if len(topic) > 60:
        topic = topic[:57].rstrip() + "..."
    return _base_entry(
        memory_type="preference",
        category="user",
        key=content,
        title=f"Preference: {topic}",
        value_kind="text",
        value_data=_memory_value_text(content),
        source=source,
        confidence=confidence,
    )


def _build_user_fact_entry(
    content: str,
    *,
    source: str,
    confidence: float,
) -> dict[str, Any] | None:
    patterns = [
        re.compile(
            r"^(?P<label>(?:mon|ma|mes)\s+[a-z0-9 '\-]{2,70})"
            r"\s+(?:est|sont)\s+(?P<value>[\s\S]{1,120})$"
        ),
        re.compile(
            r"^(?P<label>my\s+[a-z0-9 '\-]{2,70})"
            r"\s+(?:is|are)\s+(?P<value>[\s\S]{1,120})$"
        ),
    ]
    match = _match_folded(content, patterns)
    if not match:
        return None
    label = _without_owner(content[match.start("label"):match.end("label")])
    value = _trim_sentence(content[match.start("value"):match.end("value")])
    if not label or not value:
        return None
    folded_label = _fold(label)
    memory_type = "preference" if (
        "preference" in folded_label
        or "preferee" in folded_label
        or "navigateur principal" in folded_label
        or "browser" in folded_label
    ) else "fact"
    prefix = "Preference" if memory_type == "preference" else "Fact"
    return _base_entry(
        memory_type=memory_type,
        category="user",
        key=label,
        title=f"{prefix}: {_trim_sentence(label)[:60]}",
        value_kind="text",
        value_data=_ensure_sentence(content),
        source=source,
        confidence=confidence,
    )


def _build_memory_entry_from_content(
    content: str,
    *,
    source: str,
    confidence: float,
    allow_generic_fallback: bool,
) -> dict[str, Any] | None:
    content = str(content or "").strip()
    for builder in (
        _build_folder_route_entry,
        _build_site_route_entry,
        _build_workflow_entry,
        _build_preference_entry,
        _build_user_fact_entry,
    ):
        entry = builder(content, source=source, confidence=confidence)
        if entry:
            return entry
    if not allow_generic_fallback:
        return None
    memory_type = "preference" if looks_like_preference(content) else "fact"
    topic = _title_topic(content)
    if len(topic) > 60:
        topic = topic[:57].rstrip() + "..."
    title_prefix = "Preference" if memory_type == "preference" else "Fact"
    return _base_entry(
        memory_type=memory_type,
        category="user",
        key=content,
        title=f"{title_prefix}: {topic}",
        value_kind="text",
        value_data=_memory_value_text(content),
        source=source,
        confidence=confidence,
    )


def build_explicit_memory_entry(content: str) -> dict[str, Any]:
    """Map explicit user text to a schema-v1 memory entry."""

    return _build_memory_entry_from_content(
        str(content or "").strip(),
        source="explicit_user",
        confidence=1.0,
        allow_generic_fallback=True,
    ) or _base_entry(
        memory_type="fact",
        category="user",
        key="user-memory",
        title="Fact: user memory",
        value_kind="text",
        value_data=_ensure_sentence(content),
        source="explicit_user",
        confidence=1.0,
    )


def detect_candidate_memory(user_text: str) -> CandidateMemory | None:
    """Return a high-confidence automatic memory candidate, if one is obvious."""

    original = str(user_text or "").strip()
    if detect_explicit_memory_instruction(original) is not None:
        return None
    content = _normalize_auto_memory_content(original)
    if not _auto_shape_is_safe(content):
        return None
    entry = _build_memory_entry_from_content(
        content,
        source="agent_candidate",
        confidence=0.85,
        allow_generic_fallback=False,
    )
    if not entry:
        return None
    if entry["category"] in {"folder", "site"}:
        entry["confidence"] = 0.9
    elif entry["type"] == "fact":
        entry["confidence"] = 0.8
    return CandidateMemory(reason=f"{entry['type']}:{entry['category']}", entry=entry)


def _with_existing_entry_id(
    store: MemoryStore,
    entry: dict[str, Any],
) -> dict[str, Any]:
    try:
        matches = store.list_entries(
            types=[entry.get("type", "")],
            categories=[entry.get("category", "")],
        )
    except Exception:
        return entry
    key = str(entry.get("key") or "").strip().lower()
    source = str(entry.get("source") or "").strip()
    for existing in matches:
        if str(existing.get("key") or "").strip().lower() != key:
            continue
        if source == "agent_candidate" and existing.get("source") == "explicit_user":
            continue
        updated = dict(entry)
        updated["id"] = existing.get("id")
        return updated
    return entry


def set_save_memory_context(
    *,
    store: MemoryStore | None = None,
    user_text: str = "",
) -> contextvars.Token:
    """Expose per-turn context to the worker thread running ``save_memory``."""

    return _SAVE_MEMORY_CONTEXT.set({"store": store, "user_text": user_text or ""})


def reset_save_memory_context(token: contextvars.Token) -> None:
    _SAVE_MEMORY_CONTEXT.reset(token)


def _current_save_memory_context() -> dict[str, Any]:
    context = _SAVE_MEMORY_CONTEXT.get()
    return context if isinstance(context, dict) else {}


def looks_like_memory_request(user_text: str) -> bool:
    """Broad guard-only detector for turns that ask OSIA to persist memory.

    This is intentionally not the save mechanism. It only helps classify the
    source metadata and trigger a retry when a model answers a memory request
    without calling ``save_memory``.
    """

    folded = _fold(user_text or "")
    hints = (
        "remember",
        "memorize",
        "memorise",
        "memory",
        "save this",
        "save that",
        "store this",
        "store that",
        "note that",
        "keep in mind",
        "for later",
        "retiens",
        "retenir",
        "souviens",
        "memorise",
        "memoire",
        "garde en tete",
        "note dans",
        "mets dans",
    )
    return any(hint in folded for hint in hints)


def _source_for_save_memory_tool(user_text: str) -> str:
    return "explicit_user" if looks_like_memory_request(user_text) else "agent_candidate"


def _find_same_memory_key(
    store: MemoryStore,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    key = str(entry.get("key") or "").strip().lower()
    if not key:
        return None
    try:
        matches = store.list_entries(
            types=[entry.get("type", "")],
            categories=[entry.get("category", "")],
        )
    except Exception:
        return None
    for existing in matches:
        if str(existing.get("key") or "").strip().lower() == key:
            return existing
    return None


def _prepare_save_memory_tool_entry(
    store: MemoryStore,
    entry: dict[str, Any],
) -> dict[str, Any]:
    existing = _find_same_memory_key(store, entry)
    if not existing:
        return entry
    if (
        entry.get("source") == "agent_candidate"
        and existing.get("source") == "explicit_user"
        and not _same_memory_value(existing, entry)
    ):
        raise _ProtectedExplicitMemory("explicit user memory has a different value")
    prepared = dict(entry)
    prepared["id"] = existing.get("id")
    return prepared


def _save_memory_tool_result(
    *,
    ok: bool,
    entry: dict[str, Any] | None = None,
    code: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    if ok and entry:
        summary = _format_memory_line(entry).lstrip("- ").strip()
        return {
            "ok": True,
            "entry_id": entry.get("id"),
            "title": entry.get("title"),
            "summary": summary,
        }
    return {
        "ok": False,
        "code": code or "unknown",
        "message": message or "memory save failed",
    }


def execute_save_memory_tool(
    payload: dict[str, Any],
    *,
    store: MemoryStore | None = None,
    user_text: str | None = None,
) -> dict[str, Any]:
    """Validate, persist, and verify one model-requested memory entry."""

    if not isinstance(payload, dict):
        return _save_memory_tool_result(
            ok=False,
            code="validation_error",
            message="save_memory payload must be an object",
        )

    context = _current_save_memory_context()
    active_store = store or context.get("store") or MemoryStore()
    active_user_text = user_text if user_text is not None else context.get("user_text", "")
    source = _source_for_save_memory_tool(str(active_user_text or ""))
    entry = {
        "type": payload.get("type"),
        "category": payload.get("category"),
        "key": payload.get("key"),
        "title": payload.get("title"),
        "value": payload.get("value"),
        "confidence": payload.get("confidence", 1.0),
        "source": source,
        "tags": payload.get("tags") or [],
        "device": {
            "origin": "pc",
            "platform": sys.platform,
        },
    }
    try:
        prepared = _prepare_save_memory_tool_entry(active_store, entry)
        saved = active_store.upsert_entry(prepared)
        verified = active_store.get_entry(saved["id"])
        if not verified:
            return _save_memory_tool_result(
                ok=False,
                code="storage_error",
                message="memory entry was not readable after write",
            )
        return _save_memory_tool_result(ok=True, entry=verified)
    except MemorySecretError as exc:
        return _save_memory_tool_result(
            ok=False,
            code="secret_rejected",
            message=str(exc),
        )
    except (MemoryValidationError, _ProtectedExplicitMemory) as exc:
        return _save_memory_tool_result(
            ok=False,
            code="validation_error",
            message=str(exc),
        )
    except Exception as exc:
        return _save_memory_tool_result(
            ok=False,
            code="storage_error",
            message=str(exc),
        )


def parse_save_memory_tool_result(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        data = result
    else:
        text = str(result or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict) and isinstance(data.get("ok"), bool):
        return data
    return None


def save_memory_tool_result_is_error(result: Any) -> bool:
    data = parse_save_memory_tool_result(result)
    return bool(data is not None and data.get("ok") is False)


def claims_memory_saved(text: str) -> bool:
    """Return True when a reply claims local memory was updated."""

    folded = _fold(text or "")
    negative_markers = (
        "could not save",
        "couldn't save",
        "did not save",
        "not saved",
        "no memory was saved",
        "je ne peux pas memoriser",
        "je n ai pas memorise",
        "pas memorise",
        "pas enregistre",
        "n a pas ete memorise",
        "memoire n a pas ete mise",
    )
    if any(marker in folded for marker in negative_markers):
        return False
    positive_patterns = (
        r"\bi(?:'| )?ll remember\b",
        r"\bi will remember\b",
        r"\bi(?:'| ha)?ve saved\b",
        r"\bi saved (?:it|that|this)\b",
        r"\badded (?:it|that|this).{0,40}\bmemory\b",
        r"\bi(?:'| )?ll keep (?:that|this|it) in mind\b",
        r"\bsaved to (?:my |local )?memory\b",
        r"\bc[' ]?est memorise\b",
        r"\bc[' ]?est note\b",
        r"\bje (?:le |la |l )?(?:retiens|memorise|garde en memoire)\b",
        r"\bj[' ]?ai (?:memorise|enregistre|note)\b",
        r"\bj[' ]?ai ajoute .{0,40}\bmemoire\b",
        r"\bje m[' ]?en souviendrai\b",
        r"\bje m'en souviendrai\b",
    )
    return any(re.search(pattern, folded) for pattern in positive_patterns)


def memory_not_saved_response(lang: str = "fr") -> str:
    if lang == "fr":
        return "Je n'ai rien ajoute a la memoire locale."
    return "I did not add anything to local memory."


def save_explicit_memory_if_requested(
    user_text: str,
    *,
    store: MemoryStore | None = None,
) -> MemorySaveResult:
    """Persist an explicit memory instruction and verify the entry exists."""

    instruction = detect_explicit_memory_instruction(user_text)
    if instruction is None:
        return MemorySaveResult(matched=False)
    if not instruction.content:
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error="memory content is empty",
        )

    try:
        active_store = store or MemoryStore()
        entry = active_store.upsert_entry(
            build_explicit_memory_entry(instruction.content)
        )
        verified = active_store.get_entry(entry["id"])
        if not verified:
            return MemorySaveResult(
                matched=True,
                error_code="storage",
                error="memory entry was not readable after write",
            )
        return MemorySaveResult(matched=True, saved=True, entry=verified)
    except MemorySecretError as exc:
        return MemorySaveResult(
            matched=True,
            error_code="secret",
            error=str(exc),
        )
    except MemoryValidationError as exc:
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error=str(exc),
        )
    except Exception as exc:
        return MemorySaveResult(
            matched=True,
            error_code="storage",
            error=str(exc),
        )


def save_candidate_memory_if_obvious(
    user_text: str,
    *,
    store: MemoryStore | None = None,
) -> MemorySaveResult:
    """Quietly persist a high-confidence automatic memory candidate."""

    candidate = detect_candidate_memory(user_text)
    if candidate is None:
        return MemorySaveResult(matched=False)

    try:
        active_store = store or MemoryStore()
        entry = _with_existing_entry_id(active_store, candidate.entry)
        saved = active_store.upsert_entry(entry)
        verified = active_store.get_entry(saved["id"])
        if not verified:
            return MemorySaveResult(
                matched=True,
                error_code="storage",
                error="memory entry was not readable after write",
            )
        return MemorySaveResult(matched=True, saved=True, entry=verified)
    except MemorySecretError as exc:
        return MemorySaveResult(
            matched=True,
            error_code="secret",
            error=str(exc),
        )
    except MemoryValidationError as exc:
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error=str(exc),
        )
    except Exception as exc:
        return MemorySaveResult(
            matched=True,
            error_code="storage",
            error=str(exc),
        )


def _memory_exception_result(exc: Exception) -> MemorySaveResult:
    if isinstance(exc, MemorySecretError):
        return MemorySaveResult(matched=True, error_code="secret", error=str(exc))
    if isinstance(exc, MemoryValidationError):
        return MemorySaveResult(matched=True, error_code="validation", error=str(exc))
    return MemorySaveResult(matched=True, error_code="storage", error=str(exc))


def _value_data(entry: dict[str, Any]) -> Any:
    value = entry.get("value") if isinstance(entry.get("value"), dict) else {}
    return value.get("data")


def _same_memory_value(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_value = left.get("value") if isinstance(left.get("value"), dict) else {}
    right_value = right.get("value") if isinstance(right.get("value"), dict) else {}
    return (
        str(left_value.get("kind") or "").strip().lower()
        == str(right_value.get("kind") or "").strip().lower()
        and left_value.get("data") == right_value.get("data")
    )


def _upsert_verified_entry(
    entry: dict[str, Any],
    *,
    store: MemoryStore | None = None,
) -> MemorySaveResult:
    """Upsert a verified-tool memory without weakening explicit user entries."""

    try:
        active_store = store or MemoryStore()
        prepared = dict(entry)
        key = str(prepared.get("key") or "").strip().lower()
        matches = active_store.list_entries(
            types=[prepared.get("type", "")],
            categories=[prepared.get("category", "")],
        )
        for existing in matches:
            if str(existing.get("key") or "").strip().lower() != key:
                continue
            if existing.get("source") == "explicit_user":
                if not _same_memory_value(existing, prepared):
                    return MemorySaveResult(
                        matched=True,
                        saved=False,
                        entry=existing,
                        error_code="protected_explicit",
                        error="explicit user memory has a different value",
                    )
                try:
                    active_store.mark_used(existing["id"])
                    existing = active_store.get_entry(existing["id"]) or existing
                except Exception:
                    pass
                return MemorySaveResult(matched=True, saved=True, entry=existing)
            prepared["id"] = existing.get("id")
            break

        saved = active_store.upsert_entry(prepared)
        verified = active_store.get_entry(saved["id"])
        if not verified:
            return MemorySaveResult(
                matched=True,
                error_code="storage",
                error="memory entry was not readable after write",
            )
        return MemorySaveResult(matched=True, saved=True, entry=verified)
    except Exception as exc:
        return _memory_exception_result(exc)


def _is_under_directory(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _is_temporary_path(path: Path) -> bool:
    try:
        temp_root = Path(tempfile.gettempdir())
    except Exception:
        return False
    return _is_under_directory(path, temp_root)


def _validated_existing_path(path_value: Any, category: str) -> str | None:
    raw = _clean_path_or_url(str(path_value or ""))
    if not raw:
        return None
    try:
        path = Path(raw).expanduser()
    except Exception:
        return None
    try:
        if category == "folder":
            valid = path.is_dir()
        else:
            valid = path.is_file()
    except OSError:
        return None
    if not valid:
        return None
    if _is_temporary_path(path):
        return None
    return str(path)


def _normalize_site_url(url: Any, *, require_root: bool = True) -> str | None:
    raw = _clean_path_or_url(str(url or ""))
    if not raw:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    if require_root and (
        parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment
    ):
        return None
    netloc = parsed.netloc.lower()
    return urlunparse((parsed.scheme.lower(), netloc, "", "", "", ""))


def _site_label_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or parsed.netloc or "site").lower()
    return host[4:] if host.startswith("www.") else host


def remember_verified_route(
    *,
    category: str,
    label: str,
    path: str,
    title: str | None = None,
    store: MemoryStore | None = None,
    confidence: float = 0.95,
) -> MemorySaveResult:
    """Remember a verified local route only after the caller proved it exists."""

    category = str(category or "").strip().lower()
    if category not in {"app", "folder", "file"}:
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error="verified route category must be app, folder, or file",
        )
    verified_path = _validated_existing_path(path, category)
    if not verified_path:
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error="verified route path does not exist or is temporary",
        )
    label = _trim_sentence(label) or Path(verified_path).stem
    entry = _base_entry(
        memory_type="route",
        category=category,
        key=label,
        title=title or f"{category.replace('_', ' ').title()}: {label[:60]}",
        value_kind="path",
        value_data=verified_path,
        source="verified_tool",
        confidence=confidence,
    )
    return _upsert_verified_entry(entry, store=store)


def remember_verified_site(
    url: str,
    *,
    label: str | None = None,
    store: MemoryStore | None = None,
    confidence: float = 0.9,
) -> MemorySaveResult:
    """Remember a root site URL after browser navigation/opening succeeded."""

    verified_url = _normalize_site_url(url, require_root=True)
    if not verified_url:
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error="verified site URL must be a root http(s) URL",
        )
    route_label = _trim_sentence(label or "") or _site_label_from_url(verified_url)
    entry = _base_entry(
        memory_type="route",
        category="site",
        key=route_label,
        title=f"Site: {route_label[:60]}",
        value_kind="url",
        value_data=verified_url,
        source="verified_tool",
        confidence=confidence,
    )
    return _upsert_verified_entry(entry, store=store)


def remember_verified_deep_link(
    uri: str,
    *,
    label: str,
    store: MemoryStore | None = None,
    confidence: float = 0.9,
) -> MemorySaveResult:
    """Remember a deep link only when the caller has externally verified it."""

    clean_uri = _clean_path_or_url(uri)
    try:
        from osia.tools import deep_links
        allowed = deep_links.is_allowed(clean_uri)
    except Exception:
        allowed = False
    if not allowed:
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error="verified deep link is not allowed",
        )
    route_label = _trim_sentence(label)
    if not route_label:
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error="verified deep link label is required",
        )
    entry = _base_entry(
        memory_type="route",
        category="deep_link",
        key=route_label,
        title=f"Deep link: {route_label[:60]}",
        value_kind="uri",
        value_data=clean_uri,
        source="verified_tool",
        confidence=confidence,
    )
    return _upsert_verified_entry(entry, store=store)


def remember_verified_steam_app(
    *,
    appid: str,
    label: str,
    store: MemoryStore | None = None,
    confidence: float = 0.95,
) -> MemorySaveResult:
    """Remember a Steam app ID after Steam manifests verify the app."""

    clean_appid = str(appid or "").strip()
    if not re.fullmatch(r"\d{1,12}", clean_appid):
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error="verified Steam appid must be numeric",
        )
    route_label = _trim_sentence(label) or f"Steam app {clean_appid}"
    entry = _base_entry(
        memory_type="route",
        category="game",
        key=route_label,
        title=f"Game: {route_label[:60]}",
        value_kind="steam_appid",
        value_data=clean_appid,
        source="verified_tool",
        confidence=confidence,
    )
    return _upsert_verified_entry(entry, store=store)


def remember_verified_workflow(
    *,
    trigger: str,
    steps: list[str],
    store: MemoryStore | None = None,
    confidence: float = 0.9,
) -> MemorySaveResult:
    """Remember a workflow only when a caller passes already-verified steps."""

    clean_trigger = _trim_sentence(trigger)
    clean_steps = [_ensure_sentence(_trim_sentence(step)) for step in (steps or [])]
    clean_steps = [step for step in clean_steps if step]
    if not clean_trigger or not clean_steps or len(clean_steps) > 8:
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error="verified workflow requires a trigger and 1-8 steps",
        )
    if any(len(step) > 220 for step in clean_steps):
        return MemorySaveResult(
            matched=True,
            error_code="validation",
            error="verified workflow step is too long",
        )
    entry = _base_entry(
        memory_type="workflow",
        category="workflow",
        key=f"workflow {clean_trigger}",
        title=f"Workflow: {clean_trigger[:60]}",
        value_kind="steps",
        value_data=clean_steps,
        source="verified_tool",
        confidence=confidence,
    )
    return _upsert_verified_entry(entry, store=store)


def lookup_verified_app_route(
    app_name: str,
    *,
    store: MemoryStore | None = None,
) -> dict[str, Any] | None:
    """Return a usable remembered app executable route, ignoring stale paths."""

    key = _memory_key(app_name)
    if not key or key == "user-memory":
        return None
    try:
        active_store = store or MemoryStore()
        entries = active_store.list_entries(types=["route"], categories=["app"])
    except Exception:
        return None
    candidates = []
    for entry in entries:
        if str(entry.get("key") or "").strip().lower() != key:
            continue
        value = entry.get("value") if isinstance(entry.get("value"), dict) else {}
        if str(value.get("kind") or "").strip().lower() != "path":
            continue
        path = _validated_existing_path(value.get("data"), "app")
        if not path:
            continue
        candidates.append((
            float(entry.get("confidence") or 0.0),
            str(entry.get("last_used_at") or ""),
            str(entry.get("updated_at") or ""),
            entry,
            path,
        ))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    entry = candidates[0][3]
    return {"id": entry.get("id"), "path": candidates[0][4], "entry": entry}


def mark_memory_route_used(
    entry_id: str | None,
    *,
    store: MemoryStore | None = None,
) -> bool:
    if not entry_id:
        return False
    try:
        return bool((store or MemoryStore()).mark_used(str(entry_id)))
    except Exception:
        return False


def memory_save_response(result: MemorySaveResult, lang: str = "fr") -> str:
    if result.saved:
        return "C’est mémorisé." if lang == "fr" else "It's memorized."
    if result.error_code in {"secret", "validation"}:
        return (
            "Je ne peux pas mémoriser cette information."
            if lang == "fr"
            else "I can't memorize that information."
        )
    return (
        "La mémorisation a échoué."
        if lang == "fr"
        else "Memory save failed."
    )


def is_about_me_query(user_text: str) -> bool:
    folded = _fold(user_text)
    return any(pattern.search(folded) for pattern in _ABOUT_ME_PATTERNS)


def _sort_memory_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda entry: (
            str(entry.get("last_used_at") or ""),
            str(entry.get("updated_at") or ""),
            str(entry.get("created_at") or ""),
        ),
        reverse=True,
    )


def _entry_text(entry: dict[str, Any]) -> str:
    value = entry.get("value") if isinstance(entry.get("value"), dict) else {}
    data = value.get("data")
    if isinstance(data, str):
        return data.strip()
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(data)


def _format_memory_line(entry: dict[str, Any]) -> str:
    memory_type = str(entry.get("type") or "fact").strip().lower()
    if memory_type == "preference":
        label = "User preference"
    elif memory_type == "fact":
        label = "User fact"
    elif memory_type == "route":
        label = "User route"
    elif memory_type == "workflow":
        label = "User workflow"
    else:
        label = memory_type.replace("_", " ").title() or "Memory"
    text = _entry_text(entry)
    return f"- {label}: {text}"


def build_memory_turn_status(
    user_text: str,
    result: MemorySaveResult,
    *,
    max_chars: int = 600,
) -> str | None:
    """Build a transient per-turn memory status for the model request."""

    if result.saved and result.entry:
        line = _format_memory_line(result.entry)
        text = (
            "Local OSIA memory updated this turn "
            "(backend write verified; 100% local):\n"
            f"{line}\n"
            "You may mention the memory was saved only because this status "
            "confirms the write succeeded."
        )
    elif result.matched or looks_like_unsaved_memory_statement(user_text):
        text = (
            "No new local OSIA memory was saved this turn. Do not claim that "
            "you remembered, saved, or added the current information to memory."
        )
    else:
        return None

    if len(text) > max_chars:
        return text[: max(0, max_chars - 3)].rstrip() + "..."
    return text


def build_memory_context(
    user_text: str,
    *,
    store: MemoryStore | None = None,
    max_entries: int = MAX_CONTEXT_ENTRIES,
    max_total_chars: int = MAX_CONTEXT_CHARS,
) -> str | None:
    """Build a transient local-memory block for a model request."""

    try:
        active_store = store or MemoryStore()
        max_entries = max(0, int(max_entries))
        max_total_chars = max(0, int(max_total_chars))
        if max_entries == 0 or max_total_chars == 0:
            return None
        if is_about_me_query(user_text):
            entries = _sort_memory_entries(active_store.list_entries())[:max_entries]
        else:
            entries = active_store.search(user_text, limit=max_entries)
        if not entries:
            if is_about_me_query(user_text):
                return (
                    "No local OSIA memories are available for this turn "
                    "(MemoryStore returned zero entries). Do not claim to know "
                    "stored user facts, and do not say you read local files."
                )
            return None

        lines = [
            "Local OSIA memory relevant to this turn "
            "(100% local; not chat history; only entries below are loaded):",
            "Do not add stored facts from chat history or local files.",
        ]
        for entry in entries[:max_entries]:
            line = _format_memory_line(entry)
            candidate = "\n".join(lines + [line])
            if len(candidate) > max_total_chars:
                break
            lines.append(line)
        if len(lines) == 2:
            return None

        context = "\n".join(lines)
        if len(context) > max_total_chars:
            context = context[: max(0, max_total_chars - 3)].rstrip() + "..."
        return context
    except Exception as exc:
        print(f"[MEMORY] context unavailable: {exc}")
        return None
