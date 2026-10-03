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
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# ── Storage layout ─────────────────────────────────────────────────────────

CONVERSATIONS_DIR = Path.home() / ".osia" / "conversations"
TITLE_MAX_LEN = 60
DEFAULT_TITLE = "New conversation"
CONVERSATIONS_SNAPSHOT_VERSION = 2
FOLDER_STATE_SCHEMA_VERSION = 1
FOLDER_NAME_MAX_LEN = 60
_RECENT_FOLDER_OPERATIONS = 256

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


@contextmanager
def _store_lock():
    """Serialize mutations across desktop/server processes on every OS."""

    _ensure_dir()
    lock_path = CONVERSATIONS_DIR / ".store.lock"
    with _lock, lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _path_for(conv_id: str) -> Path:
    return CONVERSATIONS_DIR / f"{conv_id}.json"


def _deletion_path_unlocked(conv_id: str) -> tuple[Path | None, str]:
    """Resolve the unique file represented by one catalogue ID.

    Current files use their ID as the filename. A few older files expose an
    embedded ID with a different filename, while the catalogue uses that
    embedded ID. Only scan the store when the direct path does not identify
    that same conversation; ambiguity must never delete an arbitrary file.
    """
    if not conv_id or any(char in conv_id for char in ("/", "\\")):
        return None, "invalid_conversation_id"
    direct = _path_for(conv_id)
    if direct.exists():
        try:
            data = json.loads(direct.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            # The lease check below will fail closed on unreadable data.
            return direct, ""
        if isinstance(data, dict) and str(data.get("id") or "") == conv_id:
            return direct, ""
    matches = []
    for path in CONVERSATIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and str(data.get("id") or "") == conv_id:
            matches.append(path)
            if len(matches) > 1:
                return None, "conversation_id_ambiguous"
    if matches:
        return matches[0], ""
    return None, (
        "conversation_identity_mismatch" if direct.exists()
        else "conversation_not_found"
    )


_CANONICAL_CHAT_BINDING_FIELDS = (
    "conversation_id", "request_id", "turn_id", "run_id",
    "provider_id", "model_id", "effective_reasoning",
)


def _has_exact_user_cancelled_terminal(user_message: dict) -> bool:
    """Return whether the exact accepted turn has a durable Stop tombstone."""

    expected = {
        field: str(user_message.get("_" + field) or "")
        for field in _CANONICAL_CHAT_BINDING_FIELDS
    }
    turn_id = expected["turn_id"]
    if not turn_id or any(not value for value in expected.values()):
        return False
    try:
        from osia.turn_terminality import load_persisted_terminal_result

        terminal = load_persisted_terminal_result(turn_id)
    except Exception:
        return False
    binding = (
        terminal.get("execution_binding")
        if isinstance(terminal, dict) else None
    )
    return bool(
        isinstance(binding, dict)
        and terminal.get("terminal_state") == "user_cancelled"
        and str(terminal.get("reservation_id") or "")
        and all(str(binding.get(field) or "") == value
                for field, value in expected.items())
    )


def _assistant_targets_exact_user_cancelled_terminal(
    messages: list,
    *,
    request_id: str,
    turn_id: str,
) -> bool:
    """Fence late assistant output for an exact durably cancelled turn."""

    return any(
        isinstance(item, dict)
        and item.get("role") == "user"
        and str(item.get("_turn_id") or "") == turn_id
        and str(
            item.get("_request_id")
            or item.get("_client_request_id")
            or ""
        ) == request_id
        and _has_exact_user_cancelled_terminal(item)
        for item in messages
    )


def _unresolved_canonical_chat_users(conversation: dict):
    """Yield accepted user turns without an exact final or Stop tombstone."""
    messages = list((conversation or {}).get("messages") or [])
    for index, user_message in enumerate(messages):
        if not isinstance(user_message, dict) or user_message.get("role") != "user":
            continue
        assistant_message_id = str(
            user_message.get("_assistant_message_id") or ""
        )
        objective = str(user_message.get("_terminality_objective") or "")
        binding = {
            field: str(user_message.get("_" + field) or "")
            for field in _CANONICAL_CHAT_BINDING_FIELDS
        }
        if (
            not assistant_message_id
            or not objective
            or any(not value for value in binding.values())
        ):
            continue
        final_committed = any(
            isinstance(candidate, dict)
            and candidate.get("role") == "assistant"
            and str(candidate.get("_message_id") or "")
            == assistant_message_id
            and all(
                str(candidate.get("_" + field) or "") == value
                for field, value in binding.items()
            )
            and (
                candidate.get("_terminal") is True
                or (
                    str(candidate.get("_terminal_state") or "")
                    and str(candidate.get("_terminal_reservation_id") or "")
                )
            )
            for candidate in messages[index + 1:]
        )
        if (
            not final_committed
            and not _has_exact_user_cancelled_terminal(user_message)
        ):
            yield user_message, binding


def _has_active_canonical_chat_lease(conversation: dict) -> bool:
    """Protect accepted canonical turns until their exact final is durable."""

    return next(_unresolved_canonical_chat_users(conversation), None) is not None


def _background_execution_lease_active(conversation: dict) -> bool:
    lease = (conversation or {}).get("_background_execution_lease")
    if not isinstance(lease, dict) or not str(lease.get("lease_id") or ""):
        return False
    try:
        owner_pid = int(lease.get("owner_pid") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if owner_pid <= 0:
        return False
    try:
        from osia.turn_terminality import live_process_identity

        alive, current_identity = live_process_identity(owner_pid)
    except Exception:
        return True
    expected_identity = str(lease.get("owner_process_identity") or "")
    return bool(
        alive
        and (
            not expected_identity
            or not current_identity
            or expected_identity == current_identity
        )
    )


def _post_turn_media_lease_active(conversation: dict) -> bool:
    """Keep one conversation exclusive until its correlated media is done."""

    lease = (conversation or {}).get("_post_turn_media_lease")
    if not isinstance(lease, dict) or not str(lease.get("lease_id") or ""):
        return False
    try:
        owner_pid = int(lease.get("owner_pid") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if owner_pid <= 0:
        return False
    try:
        from osia.turn_terminality import live_process_identity

        alive, current_identity = live_process_identity(owner_pid)
    except Exception:
        return True
    expected_identity = str(lease.get("owner_process_identity") or "")
    return bool(
        alive
        and (
            not expected_identity
            or not current_identity
            or expected_identity == current_identity
        )
    )


def claim_background_execution(conv_id: str, lease_id: str) -> bool:
    """Reserve one linked background run against canonical Chat admission."""

    stable_lease_id = str(lease_id or "").strip()[:256]
    if not conv_id or not stable_lease_id:
        return False
    with _store_lock():
        conv = get_conversation(conv_id)
        if (
            conv is None
            or _has_active_canonical_chat_lease(conv)
            or _post_turn_media_lease_active(conv)
        ):
            return False
        conv.pop("_post_turn_media_lease", None)
        existing = conv.get("_background_execution_lease")
        if _background_execution_lease_active(conv):
            return bool(
                isinstance(existing, dict)
                and str(existing.get("lease_id") or "") == stable_lease_id
                and int(existing.get("owner_pid") or 0) == os.getpid()
            )
        from osia.turn_terminality import live_process_identity

        _owner_alive, owner_identity = live_process_identity(os.getpid())
        conv["_background_execution_lease"] = {
            "lease_id": stable_lease_id,
            "owner_pid": os.getpid(),
            "owner_process_identity": owner_identity,
            "claimed_at": _now_iso(),
        }
        _write_atomic(_path_for(conv_id), conv)
        return True


def release_background_execution(conv_id: str, lease_id: str) -> bool:
    """Release only the exact linked background-run lease."""

    stable_lease_id = str(lease_id or "").strip()[:256]
    if not conv_id or not stable_lease_id:
        return False
    with _store_lock():
        conv = get_conversation(conv_id)
        lease = (
            conv.get("_background_execution_lease")
            if isinstance(conv, dict) else None
        )
        if (
            not isinstance(lease, dict)
            or str(lease.get("lease_id") or "") != stable_lease_id
        ):
            return False
        conv.pop("_background_execution_lease", None)
        _write_atomic(_path_for(conv_id), conv)
        return True


def release_post_turn_media_cas(conv_id: str, lease_id: str) -> str:
    """Release one exact media lease with an unambiguous durable result."""

    stable_lease_id = str(lease_id or "").strip()[:256]
    if not conv_id or not stable_lease_id:
        return "indeterminate"
    with _store_lock():
        path = _path_for(conv_id)
        try:
            conv = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "confirmed_absent_or_different"
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return "indeterminate"
        if (
            not isinstance(conv, dict)
            or str(conv.get("id") or "") != conv_id
            or not isinstance(conv.get("messages"), list)
        ):
            return "indeterminate"
        lease = conv.get("_post_turn_media_lease")
        if (
            not isinstance(lease, dict)
            or str(lease.get("lease_id") or "") != stable_lease_id
        ):
            return "confirmed_absent_or_different"
        conv.pop("_post_turn_media_lease", None)
        try:
            _write_atomic(path, conv)
        except (OSError, TypeError, ValueError):
            return "indeterminate"
        return "released"


def release_post_turn_media(conv_id: str, lease_id: str) -> bool:
    """Backward-compatible bool wrapper for the exact media-lease CAS."""

    return release_post_turn_media_cas(conv_id, lease_id) == "released"


def _path_has_active_canonical_chat_lease(path: Path) -> bool:
    try:
        conversation = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        # Destructive operations fail closed when an existing conversation is
        # unreadable; they must not erase the only durable recovery authority.
        return True
    return (
        True
        if not isinstance(conversation, dict) else
        _has_active_canonical_chat_lease(conversation)
        or _background_execution_lease_active(conversation)
        or _post_turn_media_lease_active(conversation)
    )


def _cancel_dead_owner_turns_for_deletion(path: Path) -> bool:
    """Install exact Stop tombstones for orphaned turns before user deletion.

    A live or unproven owner remains protected. The file is rechecked by the
    caller after cancellation; this never treats an unknown tool effect as a
    verified success or invents an assistant response.
    """
    try:
        conversation = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(conversation, dict):
        return False
    unresolved = list(_unresolved_canonical_chat_users(conversation))
    if not unresolved:
        return not _path_has_active_canonical_chat_lease(path)
    from osia.turn_terminality import (
        TurnTerminalityController,
        live_process_identity,
        persisted_turn_state_exists,
    )

    controllers = []
    for message, binding in unresolved:
        try:
            owner_pid = int(message.get("_execution_owner_pid") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if owner_pid <= 0:
            return False
        alive, current_identity = live_process_identity(owner_pid)
        expected_identity = str(
            message.get("_execution_owner_process_identity") or ""
        )
        if alive and (
            not expected_identity
            or not current_identity
            or expected_identity == current_identity
        ):
            return False
        turn_id = binding["turn_id"]
        objective = str(message.get("_terminality_objective") or "")
        if not persisted_turn_state_exists(turn_id):
            return False
        try:
            controller = TurnTerminalityController(
                turn_id=turn_id,
                objective=objective,
                execution_required=True,
            )
            state = controller.snapshot()
        except (OSError, TypeError, ValueError):
            return False
        durable_binding = state.get("execution_binding") or {}
        if (
            state.get("terminal_state") not in (None, "", "user_cancelled")
            or any(
                str(durable_binding.get(field) or "") != value
                for field, value in binding.items()
            )
        ):
            return False
        controllers.append(controller)
    for controller in controllers:
        if not controller.try_request_cancellation(
            effect_possible=True,
            reason_code="conversation_delete",
        ):
            return False
    return not _path_has_active_canonical_chat_lease(path)


def _folder_state_path() -> Path:
    """Return the metadata path relative to the active conversation store.

    Keeping this dynamic is important for deterministic tests which redirect
    ``CONVERSATIONS_DIR`` to a disposable directory.
    """

    return CONVERSATIONS_DIR / "_folders" / "state.json"


def _write_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically — temp file + rename so a crashed write
    never leaves a half-finished file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _next_revision(conv: dict) -> int:
    try:
        current = int(conv.get("revision") or 0)
    except (TypeError, ValueError):
        current = 0
    return max(0, current) + 1


def _default_folder_state() -> dict:
    return {
        "schema_version": FOLDER_STATE_SCHEMA_VERSION,
        "revision": 0,
        "last_operation_id": "",
        "folders": [],
        "memberships": {},
        "recent_operations": [],
    }


def _normalize_folder_state(raw) -> dict:
    """Return a compatible canonical folder state without touching history."""

    state = _default_folder_state()
    if not isinstance(raw, dict):
        return state
    try:
        state["revision"] = max(0, int(raw.get("revision") or 0))
    except (TypeError, ValueError):
        pass
    state["last_operation_id"] = str(raw.get("last_operation_id") or "")

    folders: list[tuple[int, int, dict]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw.get("folders") or []):
        if not isinstance(item, dict):
            continue
        folder_id = str(item.get("id") or "").strip()[:160]
        name = str(item.get("name") or "").strip()[:FOLDER_NAME_MAX_LEN]
        if not folder_id or not name or folder_id in seen_ids:
            continue
        seen_ids.add(folder_id)
        try:
            requested_order = max(0, int(item.get("order") or 0))
        except (TypeError, ValueError):
            requested_order = index
        folders.append((requested_order, index, {
            "id": folder_id,
            "name": name,
            "order": 0,
            "collapsed": bool(item.get("collapsed", False)),
            "allow_folder_context": bool(
                item.get("allow_folder_context", False)
            ),
        }))
    folders.sort(key=lambda value: (value[0], value[1], value[2]["id"]))
    state["folders"] = []
    for order, (_requested, _index, folder) in enumerate(folders):
        folder["order"] = order
        state["folders"].append(folder)

    memberships = raw.get("memberships")
    if isinstance(memberships, dict):
        state["memberships"] = {
            str(conv_id): str(folder_id)
            for conv_id, folder_id in memberships.items()
            if str(conv_id).strip() and str(folder_id) in seen_ids
        }

    recent: list[dict] = []
    for item in raw.get("recent_operations") or []:
        if not isinstance(item, dict):
            continue
        operation_id = str(item.get("id") or "").strip()[:200]
        kind = str(item.get("kind") or "").strip()[:80]
        result = item.get("result")
        if operation_id and kind and isinstance(result, dict):
            recent.append({
                "id": operation_id,
                "kind": kind,
                "result": dict(result),
            })
    state["recent_operations"] = recent[-_RECENT_FOLDER_OPERATIONS:]
    return state


def _load_folder_state_unlocked() -> dict:
    path = _folder_state_path()
    if not path.exists():
        # Migration is intentionally lossless and lazy: every pre-WOR-243
        # conversation is represented as ungrouped without rewriting its file.
        state = _default_folder_state()
        _recover_bulk_transactions_unlocked(state)
        return state
    try:
        state = _normalize_folder_state(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError):
        state = _default_folder_state()
    _recover_bulk_transactions_unlocked(state)
    return state


def _recover_bulk_transactions_unlocked(state: dict) -> None:
    """Finish or roll back interrupted Project conversation transactions."""

    root = _folder_state_path().parent
    if not root.exists():
        return
    completed = {
        (
            str(item.get("id") or ""),
            str(item.get("kind") or ""),
        )
        for item in state.get("recent_operations") or []
    }
    transactions = [
        *root.glob("bulk-*"),
        *root.glob("project-create-*"),
        *root.glob("project-delete-*"),
    ]
    for transaction in transactions:
        if not transaction.is_dir():
            continue
        manifest_path = transaction / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Unknown data under the metadata directory is never destroyed.
            continue
        operation_id = str(manifest.get("operation_id") or "")
        kind = str(manifest.get("kind") or "delete_ungrouped")
        names = manifest.get("files") or []
        if (
            not operation_id
            or kind not in {
                "create_in_folder",
                "delete_folder",
                "delete_ungrouped",
            }
            or not isinstance(names, list)
        ):
            continue
        committed = (operation_id, kind) in completed
        recovered = True
        for raw_name in names:
            name = Path(str(raw_name or "")).name
            if not name or name != str(raw_name) or not name.endswith(".json"):
                recovered = False
                continue
            staged = transaction / name
            original = CONVERSATIONS_DIR / name
            if kind == "create_in_folder":
                try:
                    if committed:
                        if original.exists():
                            staged.unlink(missing_ok=True)
                        elif staged.exists():
                            os.replace(staged, original)
                        else:
                            recovered = False
                    else:
                        staged.unlink(missing_ok=True)
                except OSError:
                    recovered = False
                continue
            if not staged.exists():
                continue
            try:
                if committed:
                    staged.unlink()
                else:
                    if original.exists():
                        recovered = False
                        continue
                    os.replace(staged, original)
            except OSError:
                recovered = False
        if not recovered:
            continue
        try:
            manifest_path.unlink(missing_ok=True)
            transaction.rmdir()
        except OSError:
            pass


def _write_folder_state_unlocked(state: dict) -> None:
    _write_atomic(_folder_state_path(), _normalize_folder_state(state))


def _folder_receipt(
    state: dict,
    *,
    operation_id: str,
    result: dict,
    replayed: bool,
) -> dict:
    revision = int(state.get("revision") or 0)
    return {
        **dict(result),
        "success": bool(result.get("success", True)),
        "operation_id": operation_id,
        "revision": revision,
        "snapshot_revision": revision,
        "replayed": replayed,
    }


def _mutate_folder_state(operation_id: str | None, kind: str, mutate) -> dict:
    """Apply one idempotent folder mutation under the canonical store lock."""

    op_id = str(operation_id or "").strip()[:200]
    if not op_id:
        op_id = f"op_{uuid.uuid4().hex}"
    with _store_lock():
        state = _load_folder_state_unlocked()
        for previous in state.get("recent_operations") or []:
            if previous.get("id") != op_id:
                continue
            if previous.get("kind") != kind:
                return _folder_receipt(
                    state,
                    operation_id=op_id,
                    result={
                        "success": False,
                        "error": "operation_id_conflict",
                    },
                    replayed=True,
                )
            return _folder_receipt(
                state,
                operation_id=op_id,
                result=previous.get("result") or {},
                replayed=True,
            )

        result = mutate(state)
        if not isinstance(result, dict):
            result = {"success": False, "error": "invalid_mutation_result"}
        if result.get("success") is False:
            return _folder_receipt(
                state,
                operation_id=op_id,
                result=result,
                replayed=False,
            )

        state["revision"] = int(state.get("revision") or 0) + 1
        state["last_operation_id"] = op_id
        stored_result = dict(result)
        stored_result["success"] = True
        state.setdefault("recent_operations", []).append({
            "id": op_id,
            "kind": kind,
            "result": stored_result,
        })
        state["recent_operations"] = state["recent_operations"][
            -_RECENT_FOLDER_OPERATIONS:
        ]
        _write_folder_state_unlocked(state)
        return _folder_receipt(
            state,
            operation_id=op_id,
            result=stored_result,
            replayed=False,
        )


def _commit_receipt(
    conv: dict,
    *,
    committed: bool,
    operation_id: str,
    message_id: str = "",
) -> dict:
    return {
        "conversation": conv,
        "committed": committed,
        "revision": int(conv.get("revision") or 0),
        "operation_id": operation_id,
        "message_id": message_id,
    }


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


def _normalize_lookup_text(value: str | None) -> str:
    """Normalize conversation titles/queries for safe human-facing lookup."""

    raw = str(value or "").strip().casefold()
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", raw)
    asciiish = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    asciiish = re.sub(r"[^\w]+", " ", asciiish, flags=re.UNICODE)
    return re.sub(r"\s+", " ", asciiish).strip()


def _conversation_candidate(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title") or DEFAULT_TITLE,
        "updated_at": item.get("updated_at"),
    }


def _resolve_ok(item: dict) -> dict:
    return {
        "ok": True,
        "conversation_id": item.get("id"),
        "title": item.get("title") or DEFAULT_TITLE,
    }


def _resolve_error(code: str, message: str, candidates: list[dict] | None = None) -> dict:
    result = {"ok": False, "code": code, "message": message}
    if candidates is not None:
        result["candidates"] = candidates
    return result


def resolve_conversation_reference(
    *,
    conversation_id: str | None = None,
    title: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict:
    """Resolve a user-specified conversation reference without guessing.

    Resolution order is exact id, exact normalized title, then a unique
    contains/token query over recent conversations. Ambiguous or missing
    references return ``ok:false`` with candidates so the caller can ask for
    clarification instead of silently picking the wrong chat.
    """

    raw_id = str(conversation_id or "").strip()
    if raw_id and raw_id.casefold() != "null":
        conv = get_conversation(raw_id)
        if conv is None:
            return _resolve_error(
                "conversation_not_found",
                "conversation_id does not match an existing conversation",
            )
        return _resolve_ok(conv)

    items = list_conversations()[: max(1, int(limit or 50))]
    norm_title = _normalize_lookup_text(title)
    if norm_title:
        matches = [
            item for item in items
            if _normalize_lookup_text(item.get("title")) == norm_title
        ]
        if len(matches) == 1:
            return _resolve_ok(matches[0])
        if len(matches) > 1:
            return _resolve_error(
                "conversation_ambiguous",
                "more than one conversation has that title",
                [_conversation_candidate(item) for item in matches[:8]],
            )
        return _resolve_error(
            "conversation_not_found",
            "no conversation has that exact title",
        )

    norm_query = _normalize_lookup_text(query)
    if norm_query:
        generic_tokens = {
            "chat", "conversation", "convo", "discussion", "thread",
            "le", "la", "les", "un", "une", "des", "du", "de", "au", "aux",
            "the", "a", "an",
        }
        all_tokens = [token for token in norm_query.split(" ") if token]
        query_tokens = [token for token in all_tokens if token not in generic_tokens]
        if not query_tokens:
            query_tokens = all_tokens

        def matches_query(item: dict) -> bool:
            norm = _normalize_lookup_text(item.get("title"))
            if not norm:
                return False
            return norm_query in norm or all(token in norm for token in query_tokens)

        matches = [item for item in items if matches_query(item)]
        if len(matches) == 1:
            return _resolve_ok(matches[0])
        if len(matches) > 1:
            return _resolve_error(
                "conversation_ambiguous",
                "more than one conversation matches that query",
                [_conversation_candidate(item) for item in matches[:8]],
            )
        return _resolve_error(
            "conversation_not_found",
            "no recent conversation matches that query",
        )

    return _resolve_error(
        "conversation_not_found",
        "no conversation reference was provided",
    )


# ── Public API ─────────────────────────────────────────────────────────────

def create_conversation(title: str | None = None) -> dict:
    """Create and persist a new conversation. Returns the full dict."""
    with _store_lock():
        conv_id = uuid.uuid4().hex
        now = _now_iso()
        operation_id = f"op_{uuid.uuid4().hex}"
        conv = {
            "id": conv_id,
            "title": title or DEFAULT_TITLE,
            "created_at": now,
            "updated_at": now,
            "revision": 1,
            "last_operation_id": operation_id,
            "messages": [],
        }
        _write_atomic(_path_for(conv_id), conv)
        return conv


def resolve_request_conversation(
    request_id: str,
    title: str | None = None,
) -> tuple[dict, bool]:
    """Resolve or reserve the one durable first-chat conversation for a request."""

    stable_request_id = str(request_id or "").strip()
    if not stable_request_id:
        raise ValueError("request id is required")
    with _store_lock():
        matches: list[dict] = []
        for path in CONVERSATIONS_DIR.glob("*.json"):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("conversation store is unreadable") from exc
            if not isinstance(candidate, dict):
                raise RuntimeError("conversation store is unreadable")
            messages = candidate.get("messages")
            if not isinstance(messages, list):
                raise RuntimeError("conversation messages are unreadable")
            if any(
                isinstance(message, dict)
                and message.get("role") == "user"
                and str(
                    message.get("_request_id")
                    or message.get("_client_request_id")
                    or ""
                ) == stable_request_id
                for message in messages
            ):
                matches.append(candidate)
        if len(matches) > 1:
            raise ValueError("request id is bound to multiple conversations")
        if matches:
            return matches[0], False

        conv_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"osia:first-chat-request:v1:{stable_request_id}",
        ).hex
        path = _path_for(conv_id)
        try:
            reserved = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            reserved = None
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("conversation reservation is unreadable") from exc
        if reserved is not None:
            if (
                not isinstance(reserved, dict)
                or str(reserved.get("id") or "") != conv_id
                or not isinstance(reserved.get("messages"), list)
                or str(reserved.get("_first_request_id") or "")
                != stable_request_id
            ):
                raise ValueError("request conversation reservation conflict")
            return reserved, False

        now = _now_iso()
        operation_id = f"op_{uuid.uuid4().hex}"
        conv = {
            "id": conv_id,
            "title": title or DEFAULT_TITLE,
            "created_at": now,
            "updated_at": now,
            "revision": 1,
            "last_operation_id": operation_id,
            "messages": [],
            "_first_request_id": stable_request_id,
        }
        _write_atomic(path, conv)
        return conv, True


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


def _list_conversations_unlocked(state: dict) -> list[dict]:
    _ensure_dir()
    memberships = state.get("memberships") or {}
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
            "revision": int(data.get("revision") or 0),
            "operation_id": str(data.get("last_operation_id") or ""),
            "folder_id": memberships.get(str(data.get("id") or "")),
        })
    items.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    return items


def list_conversations() -> list[dict]:
    """Return conversation summaries, including canonical folder metadata."""

    with _store_lock():
        state = _load_folder_state_unlocked()
        return _list_conversations_unlocked(state)


def list_folders() -> list[dict]:
    """Return the canonical ordered folder entities, including empty folders."""

    with _store_lock():
        state = _load_folder_state_unlocked()
        return [dict(folder) for folder in state.get("folders") or []]


def conversation_snapshot() -> dict:
    """Return the versioned compatible conversations + folders snapshot."""

    with _store_lock():
        state = _load_folder_state_unlocked()
        return {
            "type": "conversations",
            "version": CONVERSATIONS_SNAPSHOT_VERSION,
            "snapshot_revision": int(state.get("revision") or 0),
            "operation_id": str(state.get("last_operation_id") or ""),
            "items": _list_conversations_unlocked(state),
            "folders": [
                dict(folder) for folder in state.get("folders") or []
            ],
        }


def folder_for_conversation(conv_id: str) -> dict | None:
    """Return the canonical folder for a conversation, if it has one."""

    if not conv_id:
        return None
    with _store_lock():
        state = _load_folder_state_unlocked()
        folder_id = (state.get("memberships") or {}).get(conv_id)
        if not folder_id:
            return None
        return next(
            (
                dict(folder)
                for folder in state.get("folders") or []
                if folder.get("id") == folder_id
            ),
            None,
        )


def conversation_ids_in_folder(folder_id: str) -> list[str]:
    """Return stable IDs assigned to one folder without loading histories."""

    if not folder_id:
        return []
    with _store_lock():
        state = _load_folder_state_unlocked()
        memberships = state.get("memberships") or {}
        existing_ids = {
            str(item.get("id") or "")
            for item in _list_conversations_unlocked(state)
        }
        return sorted(
            conv_id
            for conv_id, assigned in memberships.items()
            if assigned == folder_id and conv_id in existing_ids
        )


def apply_folder_mutation(message: dict) -> dict:
    """Apply one canonical Mobile/PC transport mutation to the PC store."""

    msg = message if isinstance(message, dict) else {}
    msg_type = str(msg.get("type") or "")
    operation_id = str(msg.get("operation_id") or "")
    if msg_type == "create_conversation_folder":
        return create_folder(msg.get("name", ""), operation_id=operation_id)
    if msg_type == "rename_conversation_folder":
        return rename_folder(
            str(msg.get("folder_id") or ""),
            msg.get("name", ""),
            operation_id=operation_id,
        )
    if msg_type == "set_conversation_folder_state":
        return set_folder_collapsed(
            str(msg.get("folder_id") or ""),
            bool(msg.get("collapsed", False)),
            operation_id=operation_id,
        )
    if msg_type == "set_conversation_folder_context":
        return set_folder_context_policy(
            str(msg.get("folder_id") or ""),
            bool(msg.get("allow_folder_context", False)),
            operation_id=operation_id,
        )
    if msg_type == "reorder_conversation_folders":
        raw_order = msg.get("folder_ids")
        return reorder_folders(
            list(raw_order) if isinstance(raw_order, list) else [],
            operation_id=operation_id,
        )
    if msg_type == "move_conversation_to_folder":
        return move_conversation_to_folder(
            str(msg.get("conversation_id") or msg.get("id") or ""),
            msg.get("folder_id"),
            operation_id=operation_id,
        )
    if msg_type == "new_conversation_in_project":
        return create_conversation_in_folder(
            str(msg.get("folder_id") or ""),
            operation_id=operation_id,
        )
    if msg_type == "delete_conversation_project":
        return delete_folder_with_conversations(
            str(msg.get("folder_id") or ""),
            msg.get("expected_count"),
            operation_id=operation_id,
        )
    return {
        "success": False,
        "error": "unsupported_folder_mutation",
        "operation_id": operation_id,
        "revision": 0,
        "replayed": False,
    }


def create_folder(name: str, *, operation_id: str | None = None) -> dict:
    """Create a stable folder at the end of the canonical folder order."""

    clean_name = str(name or "").strip()[:FOLDER_NAME_MAX_LEN]
    if not clean_name:
        return {
            "success": False,
            "error": "invalid_folder_name",
            "operation_id": str(operation_id or ""),
            "revision": 0,
            "replayed": False,
        }

    def mutate(state: dict) -> dict:
        folder = {
            "id": f"folder_{uuid.uuid4().hex}",
            "name": clean_name,
            "order": len(state.get("folders") or []),
            "collapsed": False,
            "allow_folder_context": False,
        }
        state.setdefault("folders", []).append(folder)
        return {"success": True, "folder": dict(folder)}

    return _mutate_folder_state(operation_id, "create_folder", mutate)


def rename_folder(
    folder_id: str,
    name: str,
    *,
    operation_id: str | None = None,
) -> dict:
    clean_name = str(name or "").strip()[:FOLDER_NAME_MAX_LEN]

    def mutate(state: dict) -> dict:
        if not clean_name:
            return {"success": False, "error": "invalid_folder_name"}
        for folder in state.get("folders") or []:
            if folder.get("id") == folder_id:
                folder["name"] = clean_name
                return {"success": True, "folder": dict(folder)}
        return {"success": False, "error": "folder_not_found"}

    return _mutate_folder_state(operation_id, "rename_folder", mutate)


def set_folder_collapsed(
    folder_id: str,
    collapsed: bool,
    *,
    operation_id: str | None = None,
) -> dict:
    def mutate(state: dict) -> dict:
        for folder in state.get("folders") or []:
            if folder.get("id") == folder_id:
                folder["collapsed"] = bool(collapsed)
                return {"success": True, "folder": dict(folder)}
        return {"success": False, "error": "folder_not_found"}

    return _mutate_folder_state(operation_id, "set_folder_collapsed", mutate)


def set_folder_context_policy(
    folder_id: str,
    allow: bool,
    *,
    operation_id: str | None = None,
) -> dict:
    def mutate(state: dict) -> dict:
        for folder in state.get("folders") or []:
            if folder.get("id") == folder_id:
                folder["allow_folder_context"] = bool(allow)
                return {"success": True, "folder": dict(folder)}
        return {"success": False, "error": "folder_not_found"}

    return _mutate_folder_state(operation_id, "set_folder_context_policy", mutate)


def reorder_folders(
    ordered_ids: list[str],
    *,
    operation_id: str | None = None,
) -> dict:
    requested = [str(folder_id or "") for folder_id in ordered_ids]

    def mutate(state: dict) -> dict:
        folders = state.get("folders") or []
        current = [str(folder.get("id") or "") for folder in folders]
        if len(requested) != len(set(requested)) or set(requested) != set(current):
            return {"success": False, "error": "invalid_folder_order"}
        by_id = {str(folder.get("id") or ""): folder for folder in folders}
        state["folders"] = [by_id[folder_id] for folder_id in requested]
        for order, folder in enumerate(state["folders"]):
            folder["order"] = order
        return {
            "success": True,
            "folder_ids": list(requested),
        }

    return _mutate_folder_state(operation_id, "reorder_folders", mutate)


def move_conversation_to_folder(
    conv_id: str,
    folder_id: str | None,
    *,
    operation_id: str | None = None,
) -> dict:
    """Change membership metadata only; conversation history is never cloned."""

    target = str(folder_id or "").strip() or None

    def mutate(state: dict) -> dict:
        if get_conversation(conv_id) is None:
            return {"success": False, "error": "conversation_not_found"}
        folder_ids = {
            str(folder.get("id") or "") for folder in state.get("folders") or []
        }
        if target is not None and target not in folder_ids:
            return {"success": False, "error": "folder_not_found"}
        memberships = state.setdefault("memberships", {})
        if target is None:
            memberships.pop(conv_id, None)
        else:
            memberships[conv_id] = target
        return {
            "success": True,
            "conversation_id": conv_id,
            "folder_id": target,
        }

    return _mutate_folder_state(operation_id, "move_conversation", mutate)


def create_conversation_in_folder(
    folder_id: str,
    *,
    operation_id: str | None = None,
) -> dict:
    """Atomically create one empty conversation inside an existing Project."""

    target = str(folder_id or "").strip()
    op_id = str(operation_id or "").strip()[:200] or f"op_{uuid.uuid4().hex}"
    with _store_lock():
        state = _load_folder_state_unlocked()
        for previous in state.get("recent_operations") or []:
            if previous.get("id") != op_id:
                continue
            if previous.get("kind") != "create_in_folder":
                return _folder_receipt(
                    state,
                    operation_id=op_id,
                    result={"success": False, "error": "operation_id_conflict"},
                    replayed=True,
                )
            return _folder_receipt(
                state,
                operation_id=op_id,
                result=previous.get("result") or {},
                replayed=True,
            )

        folder = next(
            (
                item
                for item in state.get("folders") or []
                if str(item.get("id") or "") == target
            ),
            None,
        )
        if folder is None:
            return _folder_receipt(
                state,
                operation_id=op_id,
                result={"success": False, "error": "folder_not_found"},
                replayed=False,
            )

        conv_id = uuid.uuid4().hex
        now = _now_iso()
        conv = {
            "id": conv_id,
            "title": DEFAULT_TITLE,
            "created_at": now,
            "updated_at": now,
            "revision": 1,
            "last_operation_id": op_id,
            "messages": [],
        }
        transaction = (
            _folder_state_path().parent / f"project-create-{uuid.uuid4().hex}"
        )
        staged = transaction / f"{conv_id}.json"
        manifest = transaction / "manifest.json"
        try:
            _write_atomic(manifest, {
                "operation_id": op_id,
                "kind": "create_in_folder",
                "files": [staged.name],
            })
            _write_atomic(staged, conv)
            state.setdefault("memberships", {})[conv_id] = target
            folder["collapsed"] = False
            result = {
                "success": True,
                "conversation": dict(conv),
                "conversation_id": conv_id,
                "folder_id": target,
            }
            state["revision"] = int(state.get("revision") or 0) + 1
            state["last_operation_id"] = op_id
            state.setdefault("recent_operations", []).append({
                "id": op_id,
                "kind": "create_in_folder",
                "result": dict(result),
            })
            state["recent_operations"] = state["recent_operations"][
                -_RECENT_FOLDER_OPERATIONS:
            ]
            _write_folder_state_unlocked(state)
        except OSError:
            try:
                staged.unlink(missing_ok=True)
                manifest.unlink(missing_ok=True)
                transaction.rmdir()
            except OSError:
                pass
            current = _load_folder_state_unlocked()
            return _folder_receipt(
                current,
                operation_id=op_id,
                result={"success": False, "error": "conversation_create_failed"},
                replayed=False,
            )

        try:
            os.replace(staged, _path_for(conv_id))
            manifest.unlink(missing_ok=True)
            transaction.rmdir()
        except OSError:
            _recover_bulk_transactions_unlocked(state)
        return _folder_receipt(
            state,
            operation_id=op_id,
            result=result,
            replayed=False,
        )


def delete_folder_with_conversations(
    folder_id: str,
    expected_count,
    *,
    operation_id: str | None = None,
) -> dict:
    """Atomically delete one Project and every conversation assigned to it."""

    target = str(folder_id or "").strip()
    op_id = str(operation_id or "").strip()[:200] or f"op_{uuid.uuid4().hex}"
    try:
        expected = int(expected_count)
    except (TypeError, ValueError):
        return {
            "success": False,
            "error": "invalid_expected_count",
            "operation_id": op_id,
            "revision": 0,
            "replayed": False,
        }
    if isinstance(expected_count, bool) or expected < 0:
        return {
            "success": False,
            "error": "invalid_expected_count",
            "operation_id": op_id,
            "revision": 0,
            "replayed": False,
        }

    with _store_lock():
        state = _load_folder_state_unlocked()
        for previous in state.get("recent_operations") or []:
            if previous.get("id") != op_id:
                continue
            if previous.get("kind") != "delete_folder":
                return _folder_receipt(
                    state,
                    operation_id=op_id,
                    result={"success": False, "error": "operation_id_conflict"},
                    replayed=True,
                )
            return _folder_receipt(
                state,
                operation_id=op_id,
                result=previous.get("result") or {},
                replayed=True,
            )

        folder = next(
            (
                item
                for item in state.get("folders") or []
                if str(item.get("id") or "") == target
            ),
            None,
        )
        if folder is None:
            return _folder_receipt(
                state,
                operation_id=op_id,
                result={"success": False, "error": "folder_not_found"},
                replayed=False,
            )

        memberships = state.get("memberships") or {}
        assigned_ids = {
            str(conv_id)
            for conv_id, assigned in memberships.items()
            if str(assigned or "") == target
        }
        candidates = sorted(
            (conv_id, _path_for(conv_id))
            for conv_id in assigned_ids
            if _path_for(conv_id).is_file()
        )
        if len(candidates) != expected:
            return _folder_receipt(
                state,
                operation_id=op_id,
                result={
                    "success": False,
                    "error": "folder_count_changed",
                    "expected_count": expected,
                    "actual_count": len(candidates),
                },
                replayed=False,
            )
        busy_ids = [
            conv_id for conv_id, path in candidates
            if _path_has_active_canonical_chat_lease(path)
            and not _cancel_dead_owner_turns_for_deletion(path)
        ]
        if busy_ids:
            return _folder_receipt(
                state,
                operation_id=op_id,
                result={
                    "success": False,
                    "error": "conversation_busy",
                    "busy_ids": busy_ids,
                },
                replayed=False,
            )

        transaction = (
            _folder_state_path().parent / f"project-delete-{uuid.uuid4().hex}"
        )
        moved: list[tuple[Path, Path]] = []
        try:
            transaction.mkdir(parents=True, exist_ok=False)
            _write_atomic(transaction / "manifest.json", {
                "operation_id": op_id,
                "kind": "delete_folder",
                "files": [path.name for _conv_id, path in candidates],
            })
            for _conv_id, source in candidates:
                staged = transaction / source.name
                os.replace(source, staged)
                moved.append((source, staged))

            state["folders"] = [
                item
                for item in state.get("folders") or []
                if str(item.get("id") or "") != target
            ]
            for order, item in enumerate(state["folders"]):
                item["order"] = order
            state["memberships"] = {
                conv_id: assigned
                for conv_id, assigned in memberships.items()
                if str(assigned or "") != target
            }
            result = {
                "success": True,
                "folder_id": target,
                "folder_name": str(folder.get("name") or ""),
                "deleted_count": len(candidates),
                "deleted_ids": [conv_id for conv_id, _path in candidates],
            }
            state["revision"] = int(state.get("revision") or 0) + 1
            state["last_operation_id"] = op_id
            state.setdefault("recent_operations", []).append({
                "id": op_id,
                "kind": "delete_folder",
                "result": dict(result),
            })
            state["recent_operations"] = state["recent_operations"][
                -_RECENT_FOLDER_OPERATIONS:
            ]
            _write_folder_state_unlocked(state)
        except OSError:
            for source, staged in reversed(moved):
                try:
                    os.replace(staged, source)
                except OSError:
                    pass
            try:
                (transaction / "manifest.json").unlink(missing_ok=True)
                transaction.rmdir()
            except OSError:
                pass
            current = _load_folder_state_unlocked()
            return _folder_receipt(
                current,
                operation_id=op_id,
                result={"success": False, "error": "folder_delete_failed"},
                replayed=False,
            )

        for _source, staged in moved:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (transaction / "manifest.json").unlink(missing_ok=True)
            transaction.rmdir()
        except OSError:
            pass
        return _folder_receipt(
            state,
            operation_id=op_id,
            result=result,
            replayed=False,
        )


def _delete_all_ungrouped_conversations(
    *,
    operation_id: str | None = None,
    expected_count: int | None = None,
) -> dict:
    """Atomically remove every ungrouped conversation and preserve folders.

    Candidates are first moved out of the visible store under the process/file
    lock. If any move or metadata commit fails, every moved file is restored.
    Replaying the same operation ID returns the original durable receipt.
    """

    op_id = str(operation_id or "").strip()[:200] or f"op_{uuid.uuid4().hex}"
    with _store_lock():
        state = _load_folder_state_unlocked()
        for previous in state.get("recent_operations") or []:
            if previous.get("id") != op_id:
                continue
            if previous.get("kind") != "delete_ungrouped":
                return _folder_receipt(
                    state,
                    operation_id=op_id,
                    result={"success": False, "error": "operation_id_conflict"},
                    replayed=True,
                )
            return _folder_receipt(
                state,
                operation_id=op_id,
                result=previous.get("result") or {},
                replayed=True,
            )

        foldered_ids = set((state.get("memberships") or {}).keys())
        candidates: list[tuple[str, Path]] = []
        for path in CONVERSATIONS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            conv_id = str(data.get("id") or "")
            if conv_id and conv_id not in foldered_ids:
                candidates.append((conv_id, path))
        candidates.sort(key=lambda item: item[0])
        if expected_count is not None and len(candidates) != expected_count:
            return _folder_receipt(
                state,
                operation_id=op_id,
                result={
                    "success": False,
                    "error": "ungrouped_count_changed",
                    "expected_count": expected_count,
                    "actual_count": len(candidates),
                },
                replayed=False,
            )
        busy_ids = [
            conv_id for conv_id, path in candidates
            if _path_has_active_canonical_chat_lease(path)
            and not _cancel_dead_owner_turns_for_deletion(path)
        ]
        if busy_ids:
            return _folder_receipt(
                state,
                operation_id=op_id,
                result={
                    "success": False,
                    "error": "conversation_busy",
                    "busy_ids": busy_ids,
                },
                replayed=False,
            )

        trash = _folder_state_path().parent / f"bulk-{uuid.uuid4().hex}"
        moved: list[tuple[Path, Path]] = []
        try:
            if candidates:
                trash.mkdir(parents=True, exist_ok=False)
                _write_atomic(trash / "manifest.json", {
                    "operation_id": op_id,
                    "files": [path.name for _conv_id, path in candidates],
                })
            for _conv_id, source in candidates:
                target = trash / source.name
                os.replace(source, target)
                moved.append((source, target))

            result = {
                "success": True,
                "deleted_count": len(candidates),
                "deleted_ids": [conv_id for conv_id, _path in candidates],
            }
            state["revision"] = int(state.get("revision") or 0) + 1
            state["last_operation_id"] = op_id
            state.setdefault("recent_operations", []).append({
                "id": op_id,
                "kind": "delete_ungrouped",
                "result": dict(result),
            })
            state["recent_operations"] = state["recent_operations"][
                -_RECENT_FOLDER_OPERATIONS:
            ]
            _write_folder_state_unlocked(state)
        except OSError:
            for source, target in reversed(moved):
                try:
                    os.replace(target, source)
                except OSError:
                    pass
            try:
                (trash / "manifest.json").unlink(missing_ok=True)
                trash.rmdir()
            except OSError:
                pass
            return _folder_receipt(
                state,
                operation_id=op_id,
                result={"success": False, "error": "bulk_delete_failed"},
                replayed=False,
            )

        for _source, target in moved:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (trash / "manifest.json").unlink(missing_ok=True)
            trash.rmdir()
        except OSError:
            pass
        return _folder_receipt(
            state,
            operation_id=op_id,
            result=result,
            replayed=False,
        )


def delete_all_ungrouped_conversations(
    *,
    operation_id: str | None = None,
) -> dict:
    """Delete the current ungrouped set; retained for existing callers."""

    return _delete_all_ungrouped_conversations(operation_id=operation_id)


def delete_confirmed_ungrouped_conversations(
    expected_count: int,
    *,
    operation_id: str | None = None,
) -> dict:
    """Delete only when the canonical count still matches confirmation."""

    try:
        expected = max(0, int(expected_count))
    except (TypeError, ValueError):
        return {
            "success": False,
            "error": "invalid_expected_count",
            "operation_id": str(operation_id or ""),
            "revision": 0,
            "replayed": False,
        }
    return _delete_all_ungrouped_conversations(
        operation_id=operation_id,
        expected_count=expected,
    )


def commit_message(
    conv_id: str,
    role: str,
    content: str,
    *,
    metadata: dict | None = None,
) -> dict | None:
    """Append a message to a conversation, bump ``updated_at``, and
    auto-set the title from the first non-tool user message (if the
    title is still the default).

    Returns a durable commit receipt, or ``None`` if the conversation doesn't
    exist. Replaying the same message id is a successful no-op receipt.
    """
    with _store_lock():
        conv = get_conversation(conv_id)
        if conv is None:
            return None
        correlation = {
            key: value
            for key, value in (metadata or {}).items()
            if isinstance(key, str) and key.startswith("_")
        }
        message_id = str(correlation.get("_message_id") or "")
        turn_id = str(correlation.get("_turn_id") or "")
        request_id = str(
            correlation.get("_request_id")
            or correlation.get("_client_request_id")
            or ""
        )
        operation_id = str(correlation.get("_operation_id") or message_id or "")
        if not message_id:
            message_id = f"msg_{uuid.uuid4().hex}"
            correlation["_message_id"] = message_id
        if not operation_id:
            operation_id = message_id
        correlation["_operation_id"] = operation_id
        canonical_user_binding = {
            field: str(correlation.get("_" + field) or "")
            for field in _CANONICAL_CHAT_BINDING_FIELDS
        }
        is_canonical_user_turn = bool(
            role == "user"
            and str(correlation.get("_assistant_message_id") or "")
            and str(correlation.get("_terminality_objective") or "")
            and all(canonical_user_binding.values())
        )
        if is_canonical_user_turn:
            messages = conv.setdefault("messages", [])
            same_request_users = [
                item for item in messages
                if isinstance(item, dict)
                and item.get("role") == "user"
                and str(
                    item.get("_request_id")
                    or item.get("_client_request_id")
                    or ""
                ) == request_id
            ]
            if len(same_request_users) > 1:
                raise ValueError("duplicate canonical request id")
            if same_request_users:
                existing = same_request_users[0]
                existing_message_id = str(
                    existing.get("_message_id") or ""
                )
                existing_operation_id = str(
                    existing.get("_operation_id")
                    or existing_message_id
                    or ""
                )
                incoming_hash = str(
                    correlation.get("_client_content_hash") or ""
                )
                existing_hash = str(
                    existing.get("_client_content_hash") or ""
                )
                if (
                    (
                        existing.get("content") != content
                        and not (
                            incoming_hash
                            and existing_hash
                            and incoming_hash == existing_hash
                        )
                    )
                    or (
                        incoming_hash
                        and existing_hash
                        and incoming_hash != existing_hash
                    )
                ):
                    raise ValueError(
                        "request id was already used with different content"
                    )
                if existing_message_id == message_id and (
                    existing_operation_id != operation_id
                    or any(
                        str(existing.get("_" + field) or "") != value
                        for field, value in canonical_user_binding.items()
                    )
                    or str(existing.get("_assistant_message_id") or "")
                    != str(correlation.get("_assistant_message_id") or "")
                    or str(existing.get("_terminality_objective") or "")
                    != str(correlation.get("_terminality_objective") or "")
                ):
                    raise ValueError("canonical request correlation conflict")
                return _commit_receipt(
                    conv,
                    committed=False,
                    operation_id=existing_operation_id,
                    message_id=existing_message_id,
                )
            if _background_execution_lease_active(conv):
                raise ValueError(
                    "conversation has an active background execution"
                )
            conv.pop("_background_execution_lease", None)
            if _post_turn_media_lease_active(conv):
                raise ValueError(
                    "conversation has active post-turn media"
                )
            conv.pop("_post_turn_media_lease", None)
            if _has_active_canonical_chat_lease(conv):
                raise ValueError("conversation has an active canonical turn")
        elif (
            role == "assistant"
            and _assistant_targets_exact_user_cancelled_terminal(
                conv.get("messages") or [],
                request_id=request_id,
                turn_id=turn_id,
            )
        ):
            raise ValueError("cancelled canonical turn rejects assistant output")
        elif _has_active_canonical_chat_lease(conv):
            exact_canonical_assistant = bool(
                role == "assistant"
                and (
                    correlation.get("_terminal") is True
                    or (
                        str(correlation.get("_terminal_state") or "")
                        and str(
                            correlation.get("_terminal_reservation_id") or ""
                        )
                    )
                )
                and any(
                    isinstance(item, dict)
                    and item.get("role") == "user"
                    and str(item.get("_assistant_message_id") or "")
                    == message_id
                    and all(
                        str(item.get("_" + field) or "")
                        == str(correlation.get("_" + field) or "")
                        for field in _CANONICAL_CHAT_BINDING_FIELDS
                    )
                    for item in conv.get("messages") or []
                )
            )
            if not exact_canonical_assistant:
                raise ValueError("conversation has an active canonical turn")
        if message_id or turn_id or request_id:
            is_correlated_turn = bool(turn_id or request_id)
            if is_correlated_turn and role not in {"user", "assistant"}:
                raise ValueError("correlated messages must be user or assistant")
            if is_correlated_turn and (not turn_id or not request_id):
                raise ValueError("correlated messages require request, turn, and message ids")
            messages = conv.setdefault("messages", [])
            for existing in messages:
                if not isinstance(existing, dict):
                    continue
                existing_message_id = str(existing.get("_message_id") or "")
                existing_turn_id = str(existing.get("_turn_id") or "")
                existing_request_id = str(
                    existing.get("_request_id")
                    or existing.get("_client_request_id")
                    or ""
                )
                existing_operation_id = str(
                    existing.get("_operation_id")
                    or existing_message_id
                    or ""
                )
                if existing_message_id == message_id:
                    if (
                        existing.get("role") != role
                        or existing_turn_id != turn_id
                        or existing_request_id != request_id
                        or existing_operation_id != operation_id
                        or existing.get("content") != content
                    ):
                        raise ValueError("message id correlation conflict")
                    return _commit_receipt(
                        conv,
                        committed=False,
                        operation_id=operation_id,
                        message_id=message_id,
                    )
                if existing_operation_id == operation_id:
                    raise ValueError("operation id correlation conflict")
                if (
                    is_correlated_turn
                    and existing_turn_id == turn_id
                    and existing.get("role") == role
                ):
                    raise ValueError("one correlated message per role and turn")
            if is_correlated_turn and role == "assistant":
                accepted_user = next(
                    (
                        item
                        for item in reversed(messages)
                        if isinstance(item, dict)
                        and item.get("role") == "user"
                        and str(item.get("_turn_id") or "") == turn_id
                        and str(
                            item.get("_request_id")
                            or item.get("_client_request_id")
                            or ""
                        )
                        == request_id
                    ),
                    None,
                )
                if accepted_user is None:
                    raise ValueError("assistant message has no accepted user turn")
                accepted_operation_id = str(
                    accepted_user.get("_operation_id")
                    or accepted_user.get("_message_id")
                    or ""
                )
                if accepted_operation_id and accepted_operation_id == operation_id:
                    raise ValueError("assistant operation id must differ from user operation id")
                visible_tail = next(
                    (
                        item
                        for item in reversed(messages)
                        if isinstance(item, dict)
                        and item.get("role") in {"user", "assistant"}
                        and not item.get("_internal")
                    ),
                    None,
                )
                if visible_tail is not accepted_user:
                    raise ValueError("stale assistant persistence rejected")
        entry = {
            "role": role,
            "content": content,
        }
        entry.update(correlation)
        revision = _next_revision(conv)
        entry["_sequence"] = revision
        conv.setdefault("messages", []).append(entry)
        post_turn_media_lease_id = str(
            correlation.get("_post_turn_media_lease_id") or ""
        ).strip()[:256]
        if role == "assistant" and post_turn_media_lease_id:
            try:
                media_owner_pid = int(
                    correlation.get("_post_turn_media_owner_pid") or 0
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("invalid post-turn media owner") from exc
            if media_owner_pid <= 0:
                raise ValueError("invalid post-turn media owner")
            conv["_post_turn_media_lease"] = {
                "lease_id": post_turn_media_lease_id,
                "turn_id": turn_id,
                "request_id": request_id,
                "message_id": message_id,
                "owner_pid": media_owner_pid,
                "owner_process_identity": str(
                    correlation.get("_post_turn_media_owner_process_identity")
                    or ""
                ),
                "claimed_at": _now_iso(),
            }
        conv["updated_at"] = _now_iso()
        conv["revision"] = revision
        conv["last_operation_id"] = operation_id
        if (conv.get("title") in (None, "", DEFAULT_TITLE)
                and not conv.get("manual_title")
                and role == "user"
                and not _is_tool_envelope(content)):
            conv["title"] = _make_title(content)
        _write_atomic(_path_for(conv_id), conv)
        return _commit_receipt(
            conv,
            committed=True,
            operation_id=operation_id,
            message_id=message_id,
        )


def commit_durable_assistant_message(
    conv_id: str,
    content: str,
    *,
    metadata: dict,
) -> dict | None:
    """Append one exactly correlated durable result after intervening turns.

    Ordinary assistant commits retain their strict visible-tail guard. A durable
    run may legitimately finish after a newer turn, so this narrow path validates
    the original accepted user/request/turn and the one-assistant invariant while
    deliberately allowing the result to append at the current tail.
    """

    with _store_lock():
        conv = get_conversation(conv_id)
        if conv is None:
            return None
        correlation = {
            key: value
            for key, value in (metadata or {}).items()
            if isinstance(key, str) and key.startswith("_")
        }
        message_id = str(correlation.get("_message_id") or "")
        turn_id = str(correlation.get("_turn_id") or "")
        request_id = str(
            correlation.get("_request_id")
            or correlation.get("_client_request_id")
            or ""
        )
        operation_id = str(correlation.get("_operation_id") or message_id or "")
        if not all((message_id, turn_id, request_id, operation_id)):
            raise ValueError("durable assistant correlation is incomplete")
        correlated_conversation = str(
            correlation.get("_conversation_id") or conv_id
        )
        if correlated_conversation != conv_id:
            raise ValueError("durable assistant conversation mismatch")
        messages = conv.setdefault("messages", [])
        accepted_user = None
        for existing in messages:
            if not isinstance(existing, dict):
                continue
            existing_message_id = str(existing.get("_message_id") or "")
            existing_turn_id = str(existing.get("_turn_id") or "")
            existing_request_id = str(
                existing.get("_request_id")
                or existing.get("_client_request_id")
                or ""
            )
            existing_operation_id = str(
                existing.get("_operation_id") or existing_message_id or ""
            )
            if existing_message_id == message_id:
                if (
                    existing.get("role") != "assistant"
                    or existing_turn_id != turn_id
                    or existing_request_id != request_id
                    or existing_operation_id != operation_id
                    or existing.get("content") != content
                ):
                    raise ValueError("message id correlation conflict")
                enriched = False
                for key, value in correlation.items():
                    if key in existing:
                        if existing[key] != value:
                            raise ValueError("durable message metadata conflict")
                        continue
                    existing[key] = value
                    enriched = True
                if enriched:
                    conv["updated_at"] = _now_iso()
                    conv["revision"] = _next_revision(conv)
                    conv["last_operation_id"] = operation_id
                    _write_atomic(_path_for(conv_id), conv)
                return _commit_receipt(
                    conv,
                    committed=enriched,
                    operation_id=operation_id,
                    message_id=message_id,
                )
            if existing_operation_id == operation_id:
                raise ValueError("operation id correlation conflict")
            if (
                existing_turn_id == turn_id
                and existing.get("role") == "assistant"
            ):
                raise ValueError("one correlated message per role and turn")
            if (
                existing_turn_id == turn_id
                and existing_request_id == request_id
                and existing.get("role") == "user"
            ):
                accepted_user = existing
        if accepted_user is None:
            raise ValueError("assistant message has no accepted user turn")
        accepted_operation_id = str(
            accepted_user.get("_operation_id")
            or accepted_user.get("_message_id")
            or ""
        )
        if accepted_operation_id == operation_id:
            raise ValueError("assistant operation id must differ from user operation id")
        entry = {"role": "assistant", "content": content}
        entry.update(correlation)
        revision = _next_revision(conv)
        entry["_sequence"] = revision
        messages.append(entry)
        conv["updated_at"] = _now_iso()
        conv["revision"] = revision
        conv["last_operation_id"] = operation_id
        _write_atomic(_path_for(conv_id), conv)
        return _commit_receipt(
            conv,
            committed=True,
            operation_id=operation_id,
            message_id=message_id,
        )


def add_message(
    conv_id: str,
    role: str,
    content: str,
    *,
    metadata: dict | None = None,
) -> dict | None:
    """Backward-compatible wrapper returning the updated conversation."""

    receipt = commit_message(
        conv_id,
        role,
        content,
        metadata=metadata,
    )
    return receipt.get("conversation") if receipt else None


def find_correlated_turn(conv_id: str, request_id: str) -> dict | None:
    """Return one persisted correlated turn without mutating conversation data."""

    if not conv_id or not request_id:
        return None
    with _lock:
        conv = get_conversation(conv_id)
        if conv is None:
            return None
        user_messages = []
        assistant_messages = []
        for item in conv.get("messages") or []:
            if not isinstance(item, dict):
                continue
            item_request_id = str(
                item.get("_request_id")
                or item.get("_client_request_id")
                or ""
            )
            if item_request_id != request_id:
                continue
            if item.get("role") == "user":
                user_messages.append(item)
            elif item.get("role") == "assistant":
                assistant_messages.append(item)
        if not user_messages:
            return None
        if len(user_messages) != 1 or len(assistant_messages) > 1:
            raise ValueError("persisted turn correlation conflict")
        user_message = user_messages[0]
        assistant_message = (
            assistant_messages[0] if assistant_messages else None
        )
        turn_id = str(user_message.get("_turn_id") or "")
        if not turn_id:
            return None
        if assistant_message is not None and str(
            assistant_message.get("_turn_id") or ""
        ) != turn_id:
            raise ValueError("persisted turn correlation conflict")
        route = {}
        for public_key, metadata_key in (
            ("run_id", "_run_id"),
            ("provider_id", "_provider_id"),
            ("model_id", "_model_id"),
            ("effective_reasoning", "_effective_reasoning"),
        ):
            user_value = str(user_message.get(metadata_key) or "")
            assistant_value = str(
                (assistant_message or {}).get(metadata_key) or ""
            )
            if user_value and assistant_value and user_value != assistant_value:
                raise ValueError("persisted turn route binding conflict")
            route[public_key] = assistant_value or user_value
        return {
            "conversation_id": conv_id,
            "request_id": request_id,
            "turn_id": turn_id,
            **route,
            "user_message_id": str(user_message.get("_message_id") or ""),
            "assistant_message_id": str(
                (assistant_message or {}).get("_message_id") or ""
            ),
            "user_content": user_message.get("content"),
            "terminality_objective": str(
                user_message.get("_terminality_objective") or ""
            ),
            "client_content_hash": str(
                user_message.get("_client_content_hash") or ""
            ),
            "client_base_content_hash": str(
                user_message.get("_client_base_content_hash") or ""
            ),
            "attachment_manifest_hash": str(
                user_message.get("_attachment_manifest_hash") or ""
            ),
            "input_identity_version": str(
                user_message.get("_input_identity_version") or ""
            ),
            "input_kind": str(user_message.get("_input_kind") or ""),
            "attachments": list(user_message.get("_attachments") or []),
            "execution_owner": str(
                user_message.get("_execution_owner") or ""
            ),
            "execution_owner_pid": user_message.get(
                "_execution_owner_pid"
            ),
            "execution_owner_process_identity": str(
                user_message.get("_execution_owner_process_identity") or ""
            ),
            "response_event_id": str(
                (assistant_message or {}).get("_response_event_id") or ""
            ),
            "response_sequence": int(
                (assistant_message or {}).get("_response_sequence") or 0
            ),
            "terminal_event_id": str(
                (assistant_message or {}).get("_terminal_event_id") or ""
            ),
            "terminal_sequence": int(
                (assistant_message or {}).get("_terminal_sequence") or 0
            ),
            "assistant_content": (
                (assistant_message or {}).get("content")
                if assistant_message is not None
                else None
            ),
            "language": str(
                (assistant_message or {}).get("_language")
                or user_message.get("_language")
                or ""
            ),
            "terminal_status": str(
                (assistant_message or {}).get("_terminal_status") or "done"
            ),
            "stage": str((assistant_message or {}).get("_stage") or ""),
            "error_category": str(
                (assistant_message or {}).get("_error_category") or ""
            ),
            "reason_code": str(
                (assistant_message or {}).get("_reason_code") or ""
            ),
            "durable_run_id": str(
                (assistant_message or {}).get("_durable_run_id") or ""
            ),
            "durable_status": str(
                (assistant_message or {}).get("_durable_status") or ""
            ),
            "durable_outcome_unknown": bool(
                (assistant_message or {}).get("_durable_outcome_unknown")
            ),
            "durable_provider_id": str(
                (assistant_message or {}).get("_durable_provider_id") or ""
            ),
            "durable_model_id": str(
                (assistant_message or {}).get("_durable_model_id") or ""
            ),
        }


def delete_conversation_with_receipt(
    conv_id: str,
    *,
    operation_id: str | None = None,
) -> dict:
    """Delete one conversation and durably advance the list revision.

    The versioned receipt is shared by the Desktop and WebSocket callers so a
    success acknowledgement can be tied to the file removal and metadata
    commit that completed under the canonical store lock.
    """
    stable_id = str(conv_id or "")
    op_id = str(operation_id or "").strip()[:200]
    if not op_id:
        op_id = f"op_{uuid.uuid4().hex}"
    with _store_lock():
        state = _load_folder_state_unlocked()
        path, lookup_error = _deletion_path_unlocked(stable_id)
        if path is None:
            return _folder_receipt(
                state,
                operation_id=op_id,
                result={
                    "success": False,
                    "error": lookup_error,
                    "deleted_ids": [],
                },
                replayed=False,
            )
        if (
            _path_has_active_canonical_chat_lease(path)
            and not _cancel_dead_owner_turns_for_deletion(path)
        ):
            return _folder_receipt(
                state,
                operation_id=op_id,
                result={
                    "success": False,
                    "error": "conversation_busy",
                    "deleted_ids": [],
                },
                replayed=False,
            )
        memberships = state.get("memberships") or {}
        staged = (
            _folder_state_path().parent
            / f"delete-{uuid.uuid4().hex}.json"
        )
        try:
            staged.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, staged)
            memberships.pop(stable_id, None)
            # This revision orders complete list snapshots, including an
            # ungrouped single delete. Previously that common path left the
            # revision unchanged, allowing Mobile to accept an older snapshot
            # after it had reconciled a newer deletion event.
            state["revision"] = int(state.get("revision") or 0) + 1
            state["last_operation_id"] = op_id
            _write_folder_state_unlocked(state)
        except OSError:
            if staged.exists() and not path.exists():
                try:
                    os.replace(staged, path)
                except OSError:
                    pass
            current = _load_folder_state_unlocked()
            return _folder_receipt(
                current,
                operation_id=op_id,
                result={
                    "success": False,
                    "error": "conversation_delete_failed",
                    "deleted_ids": [],
                },
                replayed=False,
            )
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            from osia.image_inputs import release_media_owner

            release_media_owner(stable_id)
        except Exception:
            pass
        return _folder_receipt(
            state,
            operation_id=op_id,
            result={"success": True, "deleted_ids": [stable_id]},
            replayed=False,
        )


def delete_conversation(conv_id: str) -> bool:
    """Compatibility boolean wrapper for the canonical deletion receipt."""
    return bool(delete_conversation_with_receipt(conv_id).get("success"))


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
    with _store_lock():
        conv = get_conversation(conv_id)
        if conv is None:
            return False
        title = title.strip()[:TITLE_MAX_LEN]
        if not title:
            return False
        if conv.get("title") == title and conv.get("manual_title") is True:
            return True
        conv["title"] = title
        conv["manual_title"] = True
        conv["updated_at"] = _now_iso()
        conv["revision"] = _next_revision(conv)
        conv["last_operation_id"] = f"op_{uuid.uuid4().hex}"
        _write_atomic(_path_for(conv_id), conv)
        return True
