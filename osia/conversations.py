#!/usr/bin/env python3
"""
OSIA Conversations — file-backed conversation history.

Each conversation lives in its own JSON file under
``~/.osia/conversations/<uuid>.json`` with the schema::

    {
        "id":         "<hex-uuid>",
        "title":      "<auto from first user message>",
        "created_at": "<ISO-8601 local timestamp>",
        "updated_at": "<ISO-8601 local timestamp>",
        "messages":   [{"role": "...", "content": "..."}, ...]
    }

The message list uses the OpenAI chat-completions shape so it can be
fed straight back to ``OSIAAgent.run(history=...)`` to resume a chat.
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

# ── Storage layout ─────────────────────────────────────────────────────────

CONVERSATIONS_DIR = Path.home() / ".osia" / "conversations"
TITLE_MAX_LEN = 60
DEFAULT_TITLE = "New conversation"

# ── Smart title — keyword extraction ───────────────────────────────────────
# Multi-word phrases are stripped first (so "est-ce que" doesn't survive as
# "est"/"ce"/"que"), then single-word stop words are filtered, then the
# first 2-4 surviving tokens become the title.

_FR_STOP_PHRASES = (
    "est-ce que", "est-ce qu'", "qu'est-ce que", "qu'est-ce qu'",
    "peux-tu", "pourrais-tu", "veux-tu", "voudrais-tu", "sais-tu",
    "tu peux", "tu pourrais", "tu sais", "tu veux", "tu voudrais",
    "je veux", "je voudrais", "j'aimerais", "j'ai envie",
    "j'ai besoin de", "j'ai besoin d'", "j'aurais besoin",
    "dis-moi", "donne-moi", "montre-moi", "trouve-moi", "cherche-moi",
    "s'il te plaît", "s'il vous plaît",
)

_EN_STOP_PHRASES = (
    "could you", "can you", "would you", "will you",
    "i would like to", "i'd like to", "i'd like",
    "i want to", "i want", "i need to", "i need",
    "tell me", "show me", "give me", "find me",
    "look up", "search for",
)

_STOP_WORDS = frozenset({
    # ── English ──
    "the", "a", "an", "to", "of", "in", "on", "at", "for", "from", "by",
    "with", "without", "into", "onto",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "and", "or", "but", "if", "then", "than", "so",
    "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their",
    "this", "that", "these", "those",
    "can", "could", "would", "should", "shall", "will", "may", "might", "must",
    "do", "did", "does", "done", "doing", "have", "had", "has",
    "what", "which", "who", "whom", "where", "when", "why", "how",
    "please", "thanks", "thank",
    "hi", "hey", "hello", "yo",
    "osia",
    # ── French ──
    "le", "la", "les", "l", "un", "une", "des", "de", "du", "d",
    "et", "ou", "ou ", "mais", "donc", "car", "ni", "si",
    "à", "au", "aux", "en", "sur", "sous", "dans", "par", "pour",
    "avec", "sans", "vers", "chez", "entre",
    "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles",
    "me", "te", "se", "lui", "leur", "moi", "toi", "soi",
    "mon", "ma", "mes", "ton", "ta", "tes", "son", "sa", "ses",
    "notre", "votre", "leurs", "nos", "vos",
    "ce", "cet", "cette", "ces", "ça", "ceci", "cela",
    "que", "qui", "quoi", "quel", "quelle", "quels", "quelles", "dont",
    "comment", "pourquoi", "quand", "où", "combien", "qu",
    "est", "es", "suis", "sommes", "êtes", "sont", "être", "était",
    "ai", "as", "a", "avons", "avez", "ont", "avoir",
    "vais", "vas", "va", "allons", "allez", "vont", "aller",
    "peux", "peut", "pouvons", "pouvez", "peuvent", "pouvoir",
    "veux", "veut", "voulons", "voulez", "veulent", "vouloir",
    "dois", "doit", "devons", "devez", "doivent", "devoir",
    "fais", "fait", "faisons", "faites", "font", "faire",
    "dis", "dit", "disons", "dites", "disent", "dire",
    "voudrais", "voudrait", "aurais", "aurait", "serais", "serait",
    "pas", "ne", "n", "y",
    "bonjour", "salut", "coucou", "merci",
    "stp", "svp",
})

# Token = run of letters (incl. accented) and digits; apostrophes/hyphens
# are split points so contractions are broken into their stems.
_WORD_RE = re.compile(r"[A-Za-z0-9À-ÖØ-öø-ÿ]+")


def smart_title(text, *, min_words: int = 2, max_words: int = 4) -> str:
    """Derive a short, keyword-based title from a user message.

    Strips multi-word stop phrases (FR + EN), then single-word stop
    words, then returns the first ``min_words``..``max_words``
    surviving tokens joined by spaces — preserving original order so
    the title still reads naturally. Falls back to the legacy first-
    line truncation if no keywords survive.
    """
    if not isinstance(text, str) or not text.strip():
        return DEFAULT_TITLE

    lowered = text.lower()
    for phrase in _FR_STOP_PHRASES + _EN_STOP_PHRASES:
        lowered = lowered.replace(phrase, " ")

    tokens = _WORD_RE.findall(lowered)
    keywords = [t for t in tokens if len(t) > 1 and t not in _STOP_WORDS]

    if not keywords:
        # Nothing useful survived — fall back to the legacy first-line
        # truncation so the title is at least *something* recognisable.
        first_line = text.strip().split("\n", 1)[0].strip()
        if len(first_line) > TITLE_MAX_LEN:
            first_line = first_line[: TITLE_MAX_LEN - 1].rstrip() + "…"
        return first_line or DEFAULT_TITLE

    chosen = keywords[:max(min_words, min(max_words, len(keywords)))]
    title = " ".join(chosen)
    if len(title) > TITLE_MAX_LEN:
        title = title[: TITLE_MAX_LEN - 1].rstrip() + "…"
    return title

# Re-entrant so add_message() can call get_conversation() while holding it.
_lock = threading.RLock()


# ── Internals ──────────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _path_for(conv_id: str) -> Path:
    return CONVERSATIONS_DIR / f"{conv_id}.json"


def _write_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically — temp file + rename so a crashed write
    never leaves a half-finished file behind.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _is_tool_envelope(content) -> bool:
    """Tool-result messages are user-role too (they're sent back to the
    model as ``<tool_result>…</tool_result>``). They must never become
    the conversation title.
    """
    if not isinstance(content, str):
        return False
    return content.lstrip().startswith("<tool_result>")


def _make_title(content) -> str:
    """Keyword-based title derived from a user message (see ``smart_title``)."""
    return smart_title(content)


# ── Public API ─────────────────────────────────────────────────────────────

def create_conversation(title: str | None = None) -> dict:
    """Create and persist a new conversation. Returns the full dict."""
    _ensure_dir()
    with _lock:
        conv_id = uuid.uuid4().hex
        now = _now_iso()
        conv = {
            "id": conv_id,
            "title": title or DEFAULT_TITLE,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        _write_atomic(_path_for(conv_id), conv)
        return conv


def get_conversation(conv_id: str) -> dict | None:
    """Load a single conversation by id, or ``None`` if it doesn't exist
    or the file is unreadable.
    """
    if not conv_id:
        return None
    path = _path_for(conv_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_conversations() -> list[dict]:
    """Return summaries of every stored conversation (NO messages),
    sorted by ``updated_at`` descending. Unreadable files are skipped.

    Each summary has: ``id``, ``title``, ``created_at``, ``updated_at``,
    ``message_count``.
    """
    _ensure_dir()
    items: list[dict] = []
    for path in CONVERSATIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items.append({
            "id": data.get("id"),
            "title": data.get("title") or DEFAULT_TITLE,
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "message_count": len(data.get("messages") or []),
        })
    items.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    return items


def add_message(conv_id: str, role: str, content: str) -> dict | None:
    """Append a message to a conversation, bump ``updated_at``, and
    auto-set the title from the first non-tool user message (if the
    title is still the default).

    Returns the updated conversation dict, or ``None`` if the
    conversation doesn't exist.
    """
    with _lock:
        conv = get_conversation(conv_id)
        if conv is None:
            return None
        conv.setdefault("messages", []).append({
            "role": role,
            "content": content,
        })
        conv["updated_at"] = _now_iso()
        if (conv.get("title") in (None, "", DEFAULT_TITLE)
                and not conv.get("manual_title")
                and role == "user"
                and not _is_tool_envelope(content)):
            conv["title"] = _make_title(content)
        _write_atomic(_path_for(conv_id), conv)
        return conv


def delete_conversation(conv_id: str) -> bool:
    """Delete a conversation file. Returns True if the file existed and
    was removed; False if it didn't exist or couldn't be removed.
    """
    if not conv_id:
        return False
    with _lock:
        path = _path_for(conv_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False


def rename_conversation(conv_id: str, title: str) -> bool:
    """Rename a conversation to a user-chosen title (WOR-76).

    The title is trimmed and capped at ``TITLE_MAX_LEN``. Sets the
    ``manual_title`` flag so the auto-titler in ``add_message`` never
    overwrites a manual rename later. Atomic write under ``_lock``,
    mirroring ``delete_conversation``'s shape.

    Returns True on success; False if the conversation doesn't exist or
    the title is empty/whitespace after trimming.
    """
    if not conv_id or not isinstance(title, str):
        return False
    with _lock:
        conv = get_conversation(conv_id)
        if conv is None:
            return False
        title = title.strip()[:TITLE_MAX_LEN]
        if not title:
            return False
        conv["title"] = title
        conv["manual_title"] = True
        conv["updated_at"] = _now_iso()
        _write_atomic(_path_for(conv_id), conv)
        return True
