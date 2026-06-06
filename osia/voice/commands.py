"""Voice command interception (WOR-10).

Lightweight, dependency-free detection of spoken control commands that must
be handled locally instead of being forwarded to the LLM. Currently this is
the multilingual "new chat" / "new conversation" command.

Detection is accent- and case-insensitive and tolerates small leading/trailing
words ("démarre un nouveau chat", "start a new chat") by matching a trigger as
a whole phrase inside a *short* utterance. Long sentences that merely mention
the phrase ("parle-moi du nouveau chat de l'app") are deliberately not matched.

Kept independent of :mod:`osia.i18n` and the agent so it can be imported from
both the STT layer (:mod:`osia.voice.stt`) and the desktop app
(:mod:`osia.main`) without import cycles.
"""

from __future__ import annotations

import re
import unicodedata

# Canonical trigger phrases per supported language. Variants are listed
# explicitly rather than fuzzy-matched to keep false positives near zero.
_NEW_CHAT_TRIGGERS = (
    # French
    "nouveau chat", "nouvelle conversation", "nouvelle discussion",
    # English
    "new chat", "new conversation",
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
    """Lowercase, strip accents/diacritics and punctuation, collapse spaces.

    CJK text has no accents or spaces, so it passes through essentially intact
    (only surrounding punctuation is dropped).
    """
    text = unicodedata.normalize("NFKD", text or "").lower()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Replace anything that isn't a word char or whitespace with a space.
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


# Pre-normalize the trigger list once at import time.
_NEW_CHAT_NORM = tuple(t for t in (_normalize(p) for p in _NEW_CHAT_TRIGGERS) if t)


def is_new_chat_command(text: str) -> bool:
    """Return True if ``text`` is a spoken "new chat / new conversation" command.

    Matches an exact normalized trigger, or a trigger contained in a short
    utterance (≤ 5 words, or ≤ 16 chars for space-less CJK) to tolerate minor
    leading/trailing words.
    """
    norm = _normalize(text)
    if not norm:
        return False

    # Voice commands are terse. The length guard prevents matching the phrase
    # when it appears inside a longer, unrelated sentence.
    short = len(norm.split()) <= 5 or len(norm) <= 16

    for trig in _NEW_CHAT_NORM:
        if norm == trig:
            return True
        if short and trig in norm:
            return True
    return False
