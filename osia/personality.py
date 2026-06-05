#!/usr/bin/env python3
"""OSIA personality + skills (Bloc 6).

The user's custom *personality* and any number of *skill* files live as real
Markdown under ``~/.osia`` so they can be edited from the GUI (the "OSIA" tab)
or by hand:

* ``~/.osia/personality.md`` — a single document prepended to the system
  prompt at conversation start. Absent/empty → :data:`DEFAULT_PERSONALITY` is
  used.
* ``~/.osia/skills/*.md``     — one Markdown file per skill. Each can be
  enabled or disabled; the enabled set is persisted in ``settings.json`` under
  ``skills_enabled`` (``{"<name>.md": true}``). Enabled skills are appended to
  the system prompt after the personality.

Only the *enabled/disabled* flags live in settings.json — the prose stays in
the Markdown files. See :func:`osia.agent.get_system_prompt`.
"""

import os

from osia.config import get_setting, set_setting

# ``~`` resolves to the OSIA user's home; ``~/.osia`` already exists (browser
# profiles, conversation store) so we only ever create the ``skills`` subdir.
OSIA_DIR = os.path.expanduser("~/.osia")
PERSONALITY_FILE = os.path.join(OSIA_DIR, "personality.md")
SKILLS_DIR = os.path.join(OSIA_DIR, "skills")

# Used whenever ``personality.md`` is missing or empty.
DEFAULT_PERSONALITY = (
    "You are OSIA, a local AI voice assistant. You are helpful, concise, and "
    "friendly.\n"
    "You respond in the user's language. You have access to the user's "
    "computer and can\n"
    "execute commands, browse the web, manage files, and more."
)

# Seed content for a freshly created skill — ``{name}`` is the stem (no ".md").
SKILL_TEMPLATE = (
    "# Skill: {name}\n"
    "## Description\n"
    "What this skill does.\n"
    "## Instructions\n"
    "Specific instructions for OSIA when handling this type of task.\n"
)


def _ensure_dirs() -> None:
    os.makedirs(SKILLS_DIR, exist_ok=True)


# ── Personality ────────────────────────────────────────────────────────────

def load_personality() -> str:
    """Return the saved personality text, or :data:`DEFAULT_PERSONALITY` when
    the file is missing or empty."""
    try:
        with open(PERSONALITY_FILE, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except Exception:
        pass
    return DEFAULT_PERSONALITY


def save_personality(text: str) -> None:
    """Persist the personality document (creating ~/.osia if needed)."""
    _ensure_dirs()
    with open(PERSONALITY_FILE, "w", encoding="utf-8") as f:
        f.write(text or "")


def reset_personality() -> str:
    """Restore the default personality on disk and return it."""
    save_personality(DEFAULT_PERSONALITY)
    return DEFAULT_PERSONALITY


# ── Skills ─────────────────────────────────────────────────────────────────

def _safe_skill_name(name: str) -> str:
    """Normalise a user-supplied skill name to a bare ``<slug>.md`` basename:
    strip any path components and guarantee the ``.md`` suffix. Returns '' for
    an empty name."""
    name = os.path.basename((name or "").strip())
    if not name:
        return ""
    if not name.lower().endswith(".md"):
        name += ".md"
    return name


def list_skills() -> list[str]:
    """Return the sorted list of skill filenames (``<name>.md``)."""
    try:
        names = [
            f for f in os.listdir(SKILLS_DIR)
            if f.lower().endswith(".md")
            and os.path.isfile(os.path.join(SKILLS_DIR, f))
        ]
    except Exception:
        return []
    names.sort(key=str.lower)
    return names


def read_skill(name: str) -> str:
    name = _safe_skill_name(name)
    if not name:
        return ""
    try:
        with open(os.path.join(SKILLS_DIR, name), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def write_skill(name: str, text: str) -> None:
    name = _safe_skill_name(name)
    if not name:
        return
    _ensure_dirs()
    with open(os.path.join(SKILLS_DIR, name), "w", encoding="utf-8") as f:
        f.write(text or "")


def create_skill(name: str) -> str:
    """Create ``<name>.md`` seeded with :data:`SKILL_TEMPLATE` (unless it
    already exists) and return its filename. The new skill starts enabled.
    Returns '' if the name is empty."""
    name = _safe_skill_name(name)
    if not name:
        return ""
    _ensure_dirs()
    path = os.path.join(SKILLS_DIR, name)
    if not os.path.exists(path):
        stem = name[:-3]  # drop ".md"
        with open(path, "w", encoding="utf-8") as f:
            f.write(SKILL_TEMPLATE.format(name=stem))
        # A freshly created skill starts enabled. Set the flag only on actual
        # creation so re-typing the name of an existing (possibly disabled)
        # skill into the "+" dialog leaves its prose AND its on/off state alone.
        set_skill_enabled(name, True)
    return name


def delete_skill(name: str) -> None:
    name = _safe_skill_name(name)
    if not name:
        return
    try:
        os.remove(os.path.join(SKILLS_DIR, name))
    except Exception:
        pass
    # Drop its enabled flag so settings.json doesn't keep a dangling entry.
    enabled = dict(get_setting("skills_enabled", {}) or {})
    if name in enabled:
        enabled.pop(name, None)
        set_setting("skills_enabled", enabled)


# ── Enabled-state (persisted in settings.json) ─────────────────────────────

def is_skill_enabled(name: str) -> bool:
    """Whether a skill is enabled. Skills with no recorded flag (e.g. a file
    dropped into the folder by hand) default to enabled."""
    name = _safe_skill_name(name)
    enabled = get_setting("skills_enabled", {}) or {}
    return bool(enabled.get(name, True))


def set_skill_enabled(name: str, enabled: bool) -> None:
    name = _safe_skill_name(name)
    if not name:
        return
    current = dict(get_setting("skills_enabled", {}) or {})
    current[name] = bool(enabled)
    set_setting("skills_enabled", current)


def load_enabled_skills_text() -> str:
    """Concatenate the text of every enabled skill file, in filename order,
    into a single block. Returns '' when none are enabled."""
    blocks = []
    for name in list_skills():
        if not is_skill_enabled(name):
            continue
        text = read_skill(name).strip()
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)
