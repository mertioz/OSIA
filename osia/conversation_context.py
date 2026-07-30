"""Deterministic, privacy-safe conversation context construction (WOR-210).

The conversation store remains the source of truth.  This module only builds a
bounded provider view and a metadata-only receipt; it never rewrites stored
messages and never asks a provider to summarize private history.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass


_LOG = logging.getLogger(__name__)

CONTEXT_RECEIPT_SCHEMA_VERSION = 1
RECENT_MESSAGE_COUNT = 10
MAX_HISTORY_MESSAGES = 20_000
MAX_MESSAGE_CHARS = 8_000_000
IMAGE_TOKEN_ESTIMATE = 8_192

# Kept outside ``osia.models`` because that provider registry is a protected
# compatibility surface.  Exact current entries use published provider limits;
# unknown/discovered ids fall back conservatively instead of guessing upward.
_MODEL_CONTEXT_LIMITS: dict[tuple[str, str], int] = {
    ("chatgpt_account", "gpt-5.6-sol"): 258_400,
    ("chatgpt_account", "gpt-5.6-terra"): 258_400,
    ("chatgpt_account", "gpt-5.6-luna"): 258_400,
    ("chatgpt_account", "gpt-5.5"): 258_400,
    ("chatgpt_account", "gpt-5.4"): 258_400,
    ("chatgpt_account", "gpt-5.4-mini"): 258_400,
    ("chatgpt_account", "gpt-5.3-codex-spark"): 258_400,
    ("anthropic", "claude-opus-4-8"): 1_000_000,
    ("anthropic", "claude-opus-4-7"): 1_000_000,
    ("anthropic", "claude-opus-4-6"): 1_000_000,
    ("anthropic", "claude-sonnet-4-6"): 1_000_000,
    ("anthropic", "claude-haiku-4-5-20251001"): 200_000,
    ("openai", "gpt-5.5"): 1_050_000,
    ("openai", "gpt-5.4"): 1_050_000,
    ("openai", "gpt-5.4-mini"): 400_000,
    ("openai", "gpt-4.1"): 1_047_576,
    ("openai", "gpt-4.1-mini"): 1_047_576,
    ("openai", "gpt-4.1-nano"): 1_047_576,
    ("deepseek", "deepseek-v4-pro"): 1_000_000,
    ("deepseek", "deepseek-v4-flash"): 1_000_000,
    ("google", "gemini-3.5-flash"): 1_048_576,
    ("google", "gemini-3.1-pro-preview"): 1_048_576,
    ("google", "gemini-3-flash-preview"): 1_048_576,
    ("google", "gemini-3.1-flash-lite"): 1_048_576,
    ("google", "gemini-2.5-flash"): 1_048_576,
    ("google", "gemini-2.5-flash-lite"): 1_048_576,
    ("xai", "grok-3"): 131_072,
    ("xai", "grok-3-mini"): 131_072,
    ("groq", "llama-3.3-70b-versatile"): 131_072,
    ("groq", "llama-3.1-8b-instant"): 131_072,
    ("groq", "qwen/qwen3-32b"): 131_072,
    ("groq", "openai/gpt-oss-120b"): 131_072,
    ("groq", "openai/gpt-oss-20b"): 131_072,
    ("groq", "moonshotai/kimi-k2-instruct"): 131_072,
    ("mistral", "mistral-large-2512"): 128_000,
    ("mistral", "mistral-medium-3-5"): 128_000,
    ("mistral", "mistral-small-2603"): 128_000,
    ("mistral", "codestral-2508"): 128_000,
}

_PROVIDER_CONTEXT_DEFAULTS = {
    "chatgpt_account": 258_400,
    "anthropic": 200_000,
    "openai": 128_000,
    "deepseek": 128_000,
    "google": 128_000,
    "xai": 128_000,
    "groq": 131_072,
    "mistral": 128_000,
    "ollama": 32_768,
}

_ALLOWED_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
_WIRE_KEYS = frozenset({"role", "content", "name", "tool_call_id", "tool_calls"})
_OPAQUE_REF_RE = re.compile(r"\b(?:doc|src|att|artifact|file)_[A-Za-z0-9_-]{8,}\b")
_SECURITY_RE = re.compile(
    r"\b(?:confirm(?:ed|ation)?|approve[ds]?|authori[sz](?:e|ed|ation)|"
    r"permission|consent|security|s[ée]curit[ée]|confirme[rz]?|autorisation|"
    r"mot de passe|password|two[- ]factor|2fa)\b",
    re.IGNORECASE,
)
_CRITICAL_RE = re.compile(
    r"\b(?:must|never|always|do not|don't|cannot|constraint|decision|requirement|"
    r"prefer|preference|my name|call me|remember|unresolved|pending|question|"
    r"marker|important|permission|confirm|approve|consent|artifact|document|"
    r"tool result|doit|jamais|toujours|ne pas|interdit|contrainte|d[ée]cision|"
    r"exigence|pr[ée]f[èe]re|pr[ée]f[ée]rence|mon nom|appelle[- ]moi|"
    r"souviens|non r[ée]solu|en attente|question|marqueur|important|"
    r"autorisation|confirme|document|artefact|r[ée]sultat d['’]outil)\b",
    re.IGNORECASE,
)
_MARKER_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_NAMED_ENTITY_RE = re.compile(r"(?<![.!?]\s)\b[A-Z][a-zÀ-ÖØ-öø-ÿ]{2,}\b")
_ENTITY_DECLARATION_RE = re.compile(
    r"\b(?:my name is|call me|project|client|customer|organization|person|"
    r"mon nom est|appelle[- ]moi|projet|client|organisation|personne)\s+"
    r"[A-Z][^\W\d_]{2,}\b",
    re.IGNORECASE,
)


class ContextBuildError(ValueError):
    """Stable, content-free failure raised before any provider request."""

    def __init__(self, category: str, receipt: dict | None = None):
        super().__init__(category)
        self.category = category
        self.receipt = copy.deepcopy(receipt) if isinstance(receipt, dict) else None


@dataclass(frozen=True)
class ContextBuildResult:
    messages: list[dict]
    receipt: dict


@dataclass
class _Entry:
    message: dict
    source_indexes: list[int]
    source_ids: list[str]


def resolve_context_limit(provider, model) -> int:
    """Resolve an exact model limit or a conservative provider fallback."""

    provider_id = str(provider or "").strip().lower()
    model_id = str(model or "").strip()
    return _MODEL_CONTEXT_LIMITS.get(
        (provider_id, model_id),
        _PROVIDER_CONTEXT_DEFAULTS.get(provider_id, 32_768),
    )


def _input_budget(context_limit: int) -> int:
    # This is input headroom, not a response-token cap. Providers still own
    # their native output limits exactly as before WOR-28.
    reserve = max(4_096, min(65_536, context_limit // 8))
    return max(1, context_limit - reserve)


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _validate_content(content) -> None:
    if isinstance(content, str):
        if len(content) > MAX_MESSAGE_CHARS:
            raise ContextBuildError("oversized_message")
        return
    if not isinstance(content, list) or len(content) > 64:
        raise ContextBuildError("malformed_message_content")
    text_chars = 0
    for part in content:
        if not isinstance(part, dict) or not isinstance(part.get("type"), str):
            raise ContextBuildError("malformed_message_content")
        if part.get("type") == "text":
            value = part.get("text")
            if not isinstance(value, str):
                raise ContextBuildError("malformed_message_content")
            text_chars += len(value)
    if text_chars > MAX_MESSAGE_CHARS:
        raise ContextBuildError("oversized_message")
    try:
        rendered = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContextBuildError("malformed_message_content") from exc
    if len(rendered) > MAX_MESSAGE_CHARS:
        raise ContextBuildError("oversized_message")


def _content_hash(content) -> str:
    try:
        rendered = json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContextBuildError("malformed_message_content") from exc
    return hashlib.sha256(rendered.encode("utf-8", errors="replace")).hexdigest()


def _stable_message_id(
    raw: dict,
    *,
    conversation_id: str,
    index: int,
    role: str,
    content,
) -> str:
    supplied = str(raw.get("_message_id") or "").strip()
    if supplied and re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", supplied):
        return supplied
    digest = hashlib.sha256(
        (
            f"{conversation_id}\0{index}\0{role}\0{_content_hash(content)}"
        ).encode("utf-8", errors="replace")
    ).hexdigest()[:24]
    return f"msg_{digest}"


def _safe_identifier(value, prefix: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        return ""
    digest = hashlib.sha256(rendered.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}ref_{digest[:24]}"


def _opaque_message_id(value: str) -> str:
    digest = hashlib.sha256(
        str(value or "").encode("utf-8", errors="replace")
    ).hexdigest()
    return f"msgref_{digest[:24]}"


def _canonical_entries(messages: list, conversation_id: str) -> tuple[list[_Entry], int]:
    if not isinstance(messages, list) or len(messages) > MAX_HISTORY_MESSAGES:
        raise ContextBuildError("malformed_or_oversized_history")
    entries: list[_Entry] = []
    seen: dict[str, tuple[str, str]] = {}
    conversation_markers: set[str] = set()
    duplicates_removed = 0
    for index, raw in enumerate(messages):
        if not isinstance(raw, dict):
            raise ContextBuildError("malformed_history_message")
        role = str(raw.get("role") or "")
        if role not in _ALLOWED_ROLES:
            raise ContextBuildError("malformed_history_role")
        content = raw.get("content", "")
        _validate_content(content)
        for key in ("name", "tool_call_id"):
            if key in raw and not isinstance(raw.get(key), str):
                raise ContextBuildError("malformed_history_message")
        if "tool_calls" in raw and raw.get("tool_calls") is not None:
            tool_calls = raw.get("tool_calls")
            if not isinstance(tool_calls, list) or len(tool_calls) > 128:
                raise ContextBuildError("malformed_history_message")
            try:
                encoded_calls = json.dumps(
                    tool_calls, ensure_ascii=False, separators=(",", ":"),
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise ContextBuildError("malformed_history_message") from exc
            if len(encoded_calls) > MAX_MESSAGE_CHARS:
                raise ContextBuildError("oversized_message")
        marker = str(raw.get("_conversation_id") or "").strip()
        if marker:
            conversation_markers.add(marker)
            if conversation_id and marker != conversation_id:
                raise ContextBuildError("conversation_scope_mismatch")
        message_id = _stable_message_id(
            raw,
            conversation_id=conversation_id,
            index=index,
            role=role,
            content=content,
        )
        signature = (role, _content_hash(content))
        previous = seen.get(message_id)
        if previous is not None:
            if previous != signature:
                raise ContextBuildError("message_identity_conflict")
            duplicates_removed += 1
            continue
        seen[message_id] = signature
        message = {
            key: copy.deepcopy(value)
            for key, value in raw.items()
            if key in _WIRE_KEYS
        }
        message["role"] = role
        message["content"] = copy.deepcopy(content)
        if (
            entries
            and entries[-1].message.get("role") == role
            and isinstance(entries[-1].message.get("content"), str)
            and isinstance(content, str)
        ):
            previous_content = entries[-1].message.get("content", "")
            entries[-1].message["content"] = "\n\n".join(
                part for part in (previous_content, content) if part
            )
            entries[-1].source_indexes.append(index)
            entries[-1].source_ids.append(message_id)
            continue
        entries.append(_Entry(message, [index], [message_id]))
    if len(conversation_markers) > 1:
        raise ContextBuildError("mixed_conversation_history")
    return entries, duplicates_removed


def _estimate_content_tokens(content) -> int:
    if isinstance(content, str):
        # Deliberately conservative across tokenizers and languages. This is an
        # estimate, reported as such, and errs toward earlier compaction.
        return max(1, math.ceil(len(content.encode("utf-8", errors="replace")) / 2))
    total = 0
    for part in content if isinstance(content, list) else ():
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            total += _estimate_content_tokens(str(part.get("text") or ""))
        elif part.get("type") in {"image_url", "image_ref"}:
            total += IMAGE_TOKEN_ESTIMATE
    return max(1, total)


def estimate_message_tokens(message: dict) -> int:
    total = 6 + _estimate_content_tokens(message.get("content", ""))
    for key in ("name", "tool_call_id"):
        if message.get(key):
            total += _estimate_content_tokens(str(message[key]))
    if message.get("tool_calls"):
        rendered = json.dumps(
            message["tool_calls"], ensure_ascii=False, separators=(",", ":"),
        )
        total += _estimate_content_tokens(rendered)
    return total


def estimate_messages_tokens(messages: list[dict]) -> int:
    return 3 + sum(estimate_message_tokens(message) for message in messages)


def _is_tool_result(message: dict) -> bool:
    return message.get("role") == "tool" or _content_text(
        message.get("content", "")
    ).lstrip().startswith("<tool_result>")


def _is_security_confirmation(message: dict) -> bool:
    return message.get("role") == "user" and bool(
        _SECURITY_RE.search(_content_text(message.get("content", "")))
    )


def _has_named_entity(text: str) -> bool:
    if _ENTITY_DECLARATION_RE.search(text):
        return True
    for match in _NAMED_ENTITY_RE.finditer(text):
        if match.start() <= 0:
            continue
        prefix = text[:match.start()].rstrip()
        if prefix and prefix[-1] not in ".!?":
            return True
    return False


def _excerpt_priority(message: dict) -> int:
    text = _content_text(message.get("content", ""))
    if not text.strip():
        return 0
    if _is_security_confirmation(message):
        return 100
    if _is_tool_result(message) or _OPAQUE_REF_RE.search(text):
        return 90
    if _CRITICAL_RE.search(text):
        return 80
    if _MARKER_RE.search(text):
        return 70
    if "?" in text:
        return 60
    if message.get("role") == "user" and _has_named_entity(text):
        return 45
    if message.get("role") == "user":
        return 20
    return 0


def _critical_excerpt(message: dict, *, max_chars: int = 360) -> str:
    text = re.sub(r"\s+", " ", _content_text(message.get("content", ""))).strip()
    if not text:
        return ""
    fragments = re.split(r"(?<=[.!?])\s+|\s*[\r\n]+\s*", text)
    selected = [
        fragment.strip()
        for fragment in fragments
        if fragment.strip() and (
            _CRITICAL_RE.search(fragment)
            or _MARKER_RE.search(fragment)
            or _OPAQUE_REF_RE.search(fragment)
            or _SECURITY_RE.search(fragment)
            or "?" in fragment
        )
    ]
    if not selected and message.get("role") == "user":
        selected = fragments[:1]
    excerpt = " ".join(selected[:3]).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 1].rstrip() + "…"
    return excerpt


def _compaction_note(items: list[tuple[_Entry, str]]) -> dict | None:
    if not items:
        return None
    lines = [
        "[OSIA deterministic context compaction]",
        "Older history was compacted locally. The entries below are selected "
        "incomplete excerpts, not the original messages in full. Preserve explicit "
        "user constraints, decisions, unresolved questions, confirmations and opaque "
        "references while answering the latest user message.",
    ]
    for entry, excerpt in items:
        source_id = _opaque_message_id(entry.source_ids[0])
        role = str(entry.message.get("role") or "")
        lines.append(
            f"- id={source_id} role={role} excerpt="
            + json.dumps(excerpt, ensure_ascii=False)
        )
    return {"role": "system", "content": "\n".join(lines)}


def _message_counts(messages: list[dict]) -> dict[str, int]:
    counts = Counter(str(message.get("role") or "") for message in messages)
    return {role: counts.get(role, 0) for role in sorted(_ALLOWED_ROLES)}


def _ranges(indexes: list[int]) -> list[str]:
    if not indexes:
        return []
    ordered = sorted(set(indexes))
    out: list[str] = []
    start = previous = ordered[0]
    for index in ordered[1:]:
        if index == previous + 1:
            previous = index
            continue
        out.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = index
    out.append(str(start) if start == previous else f"{start}-{previous}")
    return out


def _structure_hash(messages: list[dict]) -> str:
    structure = [
        {
            "role": str(message.get("role") or ""),
            "wire_hash": _content_hash({
                key: message.get(key)
                for key in sorted(_WIRE_KEYS)
                if key in message
            }),
        }
        for message in messages
    ]
    rendered = json.dumps(structure, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _reference_counts(metadata_messages, included_ids: set[str], conversation_id: str) -> tuple[int, int]:
    artifact_ids: set[str] = set()
    attachment_ids: set[str] = set()
    for index, raw in enumerate(metadata_messages if isinstance(metadata_messages, list) else ()):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "")
        content = raw.get("content", "")
        try:
            message_id = _stable_message_id(
                raw,
                conversation_id=conversation_id,
                index=index,
                role=role,
                content=content,
            )
        except ContextBuildError:
            continue
        if message_id not in included_ids:
            continue
        for reference in raw.get("_artifact_refs") or ():
            if isinstance(reference, dict):
                artifact_id = str(
                    reference.get("artifact_id") or reference.get("document_id") or ""
                )
                if re.fullmatch(r"doc_[0-9a-f]{32}", artifact_id):
                    artifact_ids.add(artifact_id)
        for reference in raw.get("_attachments") or ():
            if isinstance(reference, dict):
                attachment_id = str(reference.get("attachment_id") or "")
                if re.fullmatch(r"att_[A-Za-z0-9_-]{8,}", attachment_id):
                    attachment_ids.add(attachment_id)
    return len(artifact_ids), len(attachment_ids)


def _receipt(
    *,
    provider: str,
    model: str,
    conversation_id: str,
    turn_id: str,
    context_limit: int,
    input_budget: int,
    before: list[dict],
    after: list[dict],
    entries: list[_Entry],
    included_entry_indexes: set[int],
    compacted_source_ids: list[str],
    summarized_source_ids: list[str],
    strategy: str,
    duplicates_removed: int,
    metadata_messages,
) -> dict:
    included_source_indexes = [
        source_index
        for entry_index, entry in enumerate(entries)
        if entry_index in included_entry_indexes
        for source_index in entry.source_indexes
    ]
    included_message_ids = [
        message_id
        for entry_index, entry in enumerate(entries)
        if entry_index in included_entry_indexes
        for message_id in entry.source_ids
    ]
    included_id_set = set(included_message_ids) | set(summarized_source_ids)
    artifact_count, attachment_count = _reference_counts(
        metadata_messages, included_id_set, conversation_id,
    )
    return {
        "schema_version": CONTEXT_RECEIPT_SCHEMA_VERSION,
        "conversation_id": _safe_identifier(conversation_id, "conv"),
        "turn_id": _safe_identifier(turn_id, "turn"),
        "provider_id": str(provider or "").strip().lower(),
        "model_id": str(model or "").strip(),
        "context_limit": context_limit,
        "input_budget": input_budget,
        "message_counts_by_role": {
            "before": _message_counts(before),
            "after": _message_counts(after),
        },
        "estimated_tokens_before": estimate_messages_tokens(before),
        "estimated_tokens_after": estimate_messages_tokens(after),
        "included_message_ranges": _ranges(included_source_indexes),
        "included_message_ids": [
            _opaque_message_id(value) for value in included_message_ids
        ],
        "summarized_message_ids": [
            _opaque_message_id(value) for value in summarized_source_ids
        ],
        "compacted_message_ids": [
            _opaque_message_id(value) for value in compacted_source_ids
        ],
        "included_artifact_reference_count": artifact_count,
        "included_attachment_reference_count": attachment_count,
        "included_tool_result_reference_count": sum(
            1 for message in after if _is_tool_result(message)
        ),
        "duplicates_removed": duplicates_removed,
        "compaction_strategy": strategy,
        "canonical_context_hash": _structure_hash(after),
    }


def build_context(
    messages: list,
    *,
    provider: str,
    model: str,
    conversation_id: str | None = None,
    turn_id: str | None = None,
    context_limit: int | None = None,
    metadata_messages: list | None = None,
) -> ContextBuildResult:
    """Return the exact bounded logical context and a sanitized receipt."""

    conversation = str(conversation_id or "")
    entries, duplicates_removed = _canonical_entries(messages, conversation)
    before = [copy.deepcopy(entry.message) for entry in entries]
    resolved_limit = int(
        context_limit
        if context_limit is not None
        else resolve_context_limit(provider, model)
    )
    if resolved_limit < 8_192:
        raise ContextBuildError("invalid_context_limit")
    budget = _input_budget(resolved_limit)
    before_tokens = estimate_messages_tokens(before)
    if before_tokens <= budget:
        included = set(range(len(entries)))
        receipt = _receipt(
            provider=provider,
            model=model,
            conversation_id=conversation,
            turn_id=str(turn_id or ""),
            context_limit=resolved_limit,
            input_budget=budget,
            before=before,
            after=before,
            entries=entries,
            included_entry_indexes=included,
            compacted_source_ids=[],
            summarized_source_ids=[],
            strategy="full_history",
            duplicates_removed=duplicates_removed,
            metadata_messages=metadata_messages if metadata_messages is not None else messages,
        )
        return ContextBuildResult(before, receipt)

    instruction_indexes = {
        index
        for index, entry in enumerate(entries)
        if entry.message.get("role") in {"system", "developer"}
    }
    conversational = [
        index for index in range(len(entries)) if index not in instruction_indexes
    ]
    recent_indexes = set(conversational[-RECENT_MESSAGE_COUNT:])
    latest_user = next(
        (
            index for index in reversed(conversational)
            if entries[index].message.get("role") == "user"
            and not _is_tool_result(entries[index].message)
        ),
        None,
    )
    active_tool_indexes = {
        index
        for index in conversational
        if _is_tool_result(entries[index].message)
        and (latest_user is None or index > latest_user)
    }
    security_indexes = {
        index
        for index in conversational
        if _is_security_confirmation(entries[index].message)
    }
    required = instruction_indexes | recent_indexes | active_tool_indexes | security_indexes
    if latest_user is not None:
        required.add(latest_user)

    required_messages = [
        copy.deepcopy(entry.message)
        for index, entry in enumerate(entries)
        if index in required
    ]
    if estimate_messages_tokens(required_messages) > budget:
        failure = {
            "schema_version": CONTEXT_RECEIPT_SCHEMA_VERSION,
            "conversation_id": _safe_identifier(conversation, "conv"),
            "turn_id": _safe_identifier(turn_id, "turn"),
            "provider_id": str(provider or "").strip().lower(),
            "model_id": str(model or "").strip(),
            "context_limit": resolved_limit,
            "input_budget": budget,
            "estimated_tokens_before": before_tokens,
            "estimated_tokens_after": estimate_messages_tokens(required_messages),
            "compaction_strategy": "failed_required_context_too_large",
            "canonical_context_hash": _structure_hash(required_messages),
        }
        record_context_receipt(failure)
        raise ContextBuildError("required_context_too_large", failure)

    omitted_indexes = [index for index in range(len(entries)) if index not in required]
    summary_items: list[tuple[_Entry, str]] = []
    generic_user_samples = 0
    seen_generic: set[str] = set()
    for index in omitted_indexes:
        entry = entries[index]
        priority = _excerpt_priority(entry.message)
        if priority <= 0:
            continue
        excerpt = _critical_excerpt(entry.message)
        if not excerpt:
            continue
        if priority == 20:
            fingerprint = hashlib.sha256(
                re.sub(r"\s+", " ", excerpt.casefold()).encode("utf-8")
            ).hexdigest()
            if fingerprint in seen_generic or generic_user_samples >= 8:
                continue
            seen_generic.add(fingerprint)
            generic_user_samples += 1
        summary_items.append((entry, excerpt))

    note = _compaction_note(summary_items)
    if note is not None:
        tentative = [note, *required_messages]
        if estimate_messages_tokens(tentative) > budget:
            # Preserve high-value entries first, but retain chronological order
            # among entries that fit. Critical/security/reference entries are
            # never silently discarded: if they cannot fit even as short excerpts,
            # fail before the provider request.
            high_value = [
                item for item in summary_items
                if _excerpt_priority(item[0].message) >= 45
            ]
            shortened = [
                (entry, _critical_excerpt(entry.message, max_chars=160))
                for entry, _excerpt in high_value
            ]
            note = _compaction_note(shortened)
            tentative = ([note] if note is not None else []) + required_messages
            if estimate_messages_tokens(tentative) > budget:
                failure = {
                    "schema_version": CONTEXT_RECEIPT_SCHEMA_VERSION,
                    "conversation_id": _safe_identifier(conversation, "conv"),
                    "turn_id": _safe_identifier(turn_id, "turn"),
                    "provider_id": str(provider or "").strip().lower(),
                    "model_id": str(model or "").strip(),
                    "context_limit": resolved_limit,
                    "input_budget": budget,
                    "estimated_tokens_before": before_tokens,
                    "estimated_tokens_after": estimate_messages_tokens(tentative),
                    "compaction_strategy": "failed_critical_context_too_large",
                    "canonical_context_hash": _structure_hash(tentative),
                }
                record_context_receipt(failure)
                raise ContextBuildError("critical_context_too_large", failure)
            summary_items = shortened

    after = [
        copy.deepcopy(entry.message)
        for index, entry in enumerate(entries)
        if index in instruction_indexes
    ]
    if note is not None:
        if (
            after
            and after[-1].get("role") in {"system", "developer"}
            and isinstance(after[-1].get("content"), str)
        ):
            after[-1]["content"] = after[-1]["content"] + "\n\n" + note["content"]
        else:
            after.append(note)
    after.extend(
        copy.deepcopy(entry.message)
        for index, entry in enumerate(entries)
        if index in required and index not in instruction_indexes
    )

    # A generated user note can become adjacent to a required user frame. Match
    # the established provider compatibility rule by coalescing text roles.
    coalesced: list[dict] = []
    for message in after:
        previous = coalesced[-1] if coalesced else None
        if (
            isinstance(previous, dict)
            and previous.get("role") == message.get("role")
            and isinstance(previous.get("content"), str)
            and isinstance(message.get("content"), str)
        ):
            previous["content"] = "\n\n".join(
                value for value in (previous["content"], message["content"]) if value
            )
        else:
            coalesced.append(copy.deepcopy(message))
    after = coalesced
    if estimate_messages_tokens(after) > budget:
        raise ContextBuildError("context_budget_invariant_failed")

    summarized_ids = [
        message_id
        for entry, _excerpt in summary_items
        for message_id in entry.source_ids
    ]
    compacted_ids = [
        message_id
        for index in omitted_indexes
        for message_id in entries[index].source_ids
    ]
    receipt = _receipt(
        provider=provider,
        model=model,
        conversation_id=conversation,
        turn_id=str(turn_id or ""),
        context_limit=resolved_limit,
        input_budget=budget,
        before=before,
        after=after,
        entries=entries,
        included_entry_indexes=required,
        compacted_source_ids=compacted_ids,
        summarized_source_ids=summarized_ids,
        strategy="deterministic_critical_excerpts_recent_verbatim_v1",
        duplicates_removed=duplicates_removed,
        metadata_messages=metadata_messages if metadata_messages is not None else messages,
    )
    return ContextBuildResult(after, receipt)


def record_context_receipt(receipt: dict) -> dict:
    """Log only the already-sanitized metadata receipt."""

    safe = copy.deepcopy(receipt) if isinstance(receipt, dict) else {}
    _LOG.info("conversation_context_receipt %s", json.dumps(safe, sort_keys=True))
    return safe


__all__ = [
    "ContextBuildError",
    "ContextBuildResult",
    "build_context",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "record_context_receipt",
    "resolve_context_limit",
]
