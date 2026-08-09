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
    """Finish or roll back an interrupted bulk-delete transaction."""

    root = _folder_state_path().parent
    if not root.exists():
        return
    completed = {
        str(item.get("id") or "")
        for item in state.get("recent_operations") or []
        if item.get("kind") == "delete_ungrouped"
    }
    for transaction in root.glob("bulk-*"):
        if not transaction.is_dir():
            continue
        manifest_path = transaction / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Unknown data under the metadata directory is never destroyed.
            continue
        operation_id = str(manifest.get("operation_id") or "")
        names = manifest.get("files") or []
        if not operation_id or not isinstance(names, list):
            continue
        committed = operation_id in completed
        recovered = True
        for raw_name in names:
            name = Path(str(raw_name or "")).name
            if not name or name != str(raw_name) or not name.endswith(".json"):
                recovered = False
                continue
            staged = transaction / name
            if not staged.exists():
                continue
            try:
                if committed:
                    staged.unlink()
                else:
                    original = CONVERSATIONS_DIR / name
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
    return {
        **dict(result),
        "success": bool(result.get("success", True)),
        "operation_id": operation_id,
        "revision": int(state.get("revision") or 0),
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
        user_message = None
        assistant_message = None
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
                user_message = item
            elif item.get("role") == "assistant":
                assistant_message = item
        if user_message is None:
            return None
        turn_id = str(user_message.get("_turn_id") or "")
        if not turn_id:
            return None
        if assistant_message is not None and str(
            assistant_message.get("_turn_id") or ""
        ) != turn_id:
            raise ValueError("persisted turn correlation conflict")
        return {
            "conversation_id": conv_id,
            "request_id": request_id,
            "turn_id": turn_id,
            "user_message_id": str(user_message.get("_message_id") or ""),
            "assistant_message_id": str(
                (assistant_message or {}).get("_message_id") or ""
            ),
            "user_content": user_message.get("content"),
            "client_content_hash": str(
                user_message.get("_client_content_hash") or ""
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
        }


def delete_conversation(conv_id: str) -> bool:
    """Delete a conversation file. Returns True if the file existed and
    was removed; False if it didn't exist or couldn't be removed.
    """
    if not conv_id:
        return False
    with _store_lock():
        path = _path_for(conv_id)
        if not path.exists():
            return False
        state = _load_folder_state_unlocked()
        memberships = state.get("memberships") or {}
        staged = (
            _folder_state_path().parent
            / f"delete-{conv_id}-{uuid.uuid4().hex}.json"
        )
        try:
            staged.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, staged)
            if conv_id in memberships:
                memberships.pop(conv_id, None)
                state["revision"] = int(state.get("revision") or 0) + 1
                state["last_operation_id"] = f"op_{uuid.uuid4().hex}"
                _write_folder_state_unlocked(state)
        except OSError:
            if staged.exists() and not path.exists():
                try:
                    os.replace(staged, path)
                except OSError:
                    pass
            return False
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        return True


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
