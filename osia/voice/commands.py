"""Voice command interception (WOR-10).

Lightweight, dependency-free detection of spoken control commands that must
be handled locally instead of being forwarded to the LLM. Currently this is
the multilingual "new chat" / "new conversation" command.

Detection is accent- and case-insensitive, but only complete approved commands
match. The two documented imperative forms ("démarre un nouveau chat" and
"start a new chat") are explicit entries rather than substring exceptions.

Kept independent of :mod:`osia.i18n` and the agent so it can be imported from
both the STT layer (:mod:`osia.voice.stt`) and the desktop app
(:mod:`osia.main`) without import cycles.
"""

from __future__ import annotations

import re
import unicodedata

# Complete approved commands per supported language. Variants are listed
# explicitly rather than substring- or fuzzy-matched to keep false positives
# near zero.
_NEW_CHAT_TRIGGERS = (
    # French
    "nouveau chat", "nouvelle conversation", "nouvelle discussion",
    "démarre un nouveau chat",
    # English
    "new chat", "new conversation", "start a new chat",
    # Chinese (simplified + traditional)
    "新对话", "新對話", "新聊天", "新的对话",
    # Japanese
    "新しいチャット", "新しい会話",
    # German
    "neues gespräch", "neue unterhaltung", "neuer chat",
    # Spanish
    "nueva conversación", "nueva conversacion", "nuevo chat",
)


def _normalize(text: str) -> str:
    """Normalize safe command-surface differences only.

    Case, accents, surrounding/repeated whitespace, and approved terminal
    sentence punctuation are ignored. Internal punctuation remains significant
    so it cannot silently turn a longer or malformed utterance into a command.
    """
    text = unicodedata.normalize("NFKD", text or "").lower().strip()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[.!?…。！？]+$", "", text).strip()
    return re.sub(r"\s+", " ", text).strip()


# Pre-normalize the trigger list once at import time.
_NEW_CHAT_NORM = tuple(t for t in (_normalize(p) for p in _NEW_CHAT_TRIGGERS) if t)


def is_new_chat_command(text: str) -> bool:
    """Return True if ``text`` is a spoken "new chat / new conversation" command.

    The normalized utterance must equal one complete approved command.
    """
    norm = _normalize(text)
    return bool(norm) and norm in _NEW_CHAT_NORM
