"""Sanitized, monotonic document-processing progress for WOR-187.

The context is runtime-only.  Source capabilities are used solely as private
map keys and are never passed to the sink; outbound consumers receive a fresh
scope-local ``item_*`` identifier plus allow-listed state.
"""

from __future__ import annotations

import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable


DOCUMENT_PHASES = (
    "identifying", "inspecting", "extracting", "indexing", "ready",
)
_PHASE_RANK = {phase: index for index, phase in enumerate(DOCUMENT_PHASES)}
_SAFE_FAILURES = frozenset({
    "active_content_detected", "corrupted_document", "drm_protected",
    "encrypted_document", "expired_source", "external_reference_detected",
    "extraction_failure", "file_identity_mismatch", "invalid_source_reference",
    "missing_source", "ocr_unavailable", "protected_document",
    "resource_limit_exceeded", "source_changed", "source_unavailable",
    "unauthorized_source", "unsupported_format", "unsupported_legacy_format",
})
_SAFE_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,31}$")


@dataclass
class _ItemState:
    item_id: str
    rank: int = -1
    terminal: bool = False
    document_type: str = "document"


@dataclass
class _ProgressContext:
    scope_type: str
    scope_id: str
    revision: int
    sink: Callable[[dict], None]
    items: dict[str, _ItemState] = field(default_factory=dict)


_CURRENT: ContextVar[_ProgressContext | None] = ContextVar(
    "osia_document_progress", default=None,
)


def _safe_type(value) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _SAFE_TYPE_RE.fullmatch(candidate) else "document"


def _safe_failure(value) -> str:
    candidate = str(value or "")
    return candidate if candidate in _SAFE_FAILURES else "extraction_failure"


@contextmanager
def document_progress_scope(
    scope_type: str,
    scope_id: str,
    *,
    revision: int = 0,
    sink: Callable[[dict], None] | None = None,
):
    """Bind one Chat turn or Autonomous run to a sanitized progress sink."""

    if (
        sink is None
        or scope_type not in {"chat", "autonomous"}
        or not isinstance(scope_id, str)
        or not scope_id
    ):
        yield
        return
    context = _ProgressContext(
        scope_type=scope_type,
        scope_id=scope_id[:256],
        revision=max(0, int(revision or 0)),
        sink=sink,
    )
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def emit_document_progress(
    source_key: str,
    phase: str,
    *,
    document_type: str = "",
    failure_category: str = "",
    completed_count: int | None = None,
    total_count: int | None = None,
) -> None:
    """Emit one monotonic item transition without exposing ``source_key``."""

    context = _CURRENT.get()
    if context is None or phase not in {*DOCUMENT_PHASES, "failed"}:
        return
    private_key = str(source_key or "")
    if not private_key:
        private_key = "anonymous:" + secrets.token_hex(16)
    item = context.items.get(private_key)
    if item is None:
        item = _ItemState(item_id="item_" + secrets.token_hex(12))
        context.items[private_key] = item
    if item.terminal:
        return
    if phase == "failed":
        terminal = True
    else:
        rank = _PHASE_RANK[phase]
        if rank <= item.rank:
            return
        item.rank = rank
        terminal = phase == "ready"
    if document_type:
        item.document_type = _safe_type(document_type)
    item.terminal = terminal
    counts = None
    if (
        type(completed_count) is int
        and type(total_count) is int
        and 0 <= completed_count <= total_count <= 1_000_000
    ):
        counts = {"completed": completed_count, "total": total_count}
    event = {
        "scope_type": context.scope_type,
        "scope_id": context.scope_id,
        "revision": context.revision,
        "item_id": item.item_id,
        "document_type": item.document_type,
        "phase": phase,
        "terminal": terminal,
        "failure_category": (
            _safe_failure(failure_category) if phase == "failed" else ""
        ),
        "counts": counts,
    }
    try:
        context.sink(event)
    except Exception:
        # Progress projection is best-effort and must never break extraction.
        return


__all__ = [
    "DOCUMENT_PHASES", "document_progress_scope", "emit_document_progress",
]
