"""Bounded semantic form inspection and verified filling (WOR-176).

This module never owns Playwright.  One :class:`SemanticFormSession` instance
is owned by ``browser._AsyncBrowserOwner`` and every method is called on that
owner's asyncio thread.  Field references and value fingerprints are therefore
process-local, ephemeral, and never cross a persistence boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import Counter
from contextlib import suppress
from urllib.parse import urlsplit


FORM_REFERENCE_TTL_SECONDS = 10 * 60
FORM_MAX_FIELDS_DEFAULT = 50
FORM_MAX_FIELDS_HARD = 100
FORM_MAX_FRAMES = 24
FORM_MAX_FRAME_DEPTH = 4
FORM_MAX_DOM_CANDIDATES = 250
FORM_MAX_LIVE_REFERENCES = 500
FORM_MAX_STALE_REFERENCES = 1000
FORM_INSPECTION_TIMEOUT_SECONDS = 6.0
FORM_FILL_TIMEOUT_SECONDS = 18.0
FORM_FIELD_ACTION_TIMEOUT_MS = 5000
FORM_VALIDATION_QUIET_MS = 250
FORM_VALIDATION_TIMEOUT_MS = 5000
FORM_TEXT_LIMIT = 120
FORM_MAX_VALUE_CHARS = 16_000

_FIELD_REF_RE = re.compile(r"^fld_[A-Za-z0-9_-]{24}$")
_FIELD_SELECTOR = "input, textarea, select"
_WAIT_VALIDATION_STABLE_JS = r"""
(el, options) => new Promise(resolve => {
  const quietMs = options.quietMs;
  const timeoutMs = options.timeoutMs;
  let quietTimer = null;
  let finished = false;
  const done = () => {
    if (finished) return;
    finished = true;
    observer.disconnect();
    el.removeEventListener('input', changed);
    el.removeEventListener('change', changed);
    clearTimeout(quietTimer);
    clearTimeout(deadlineTimer);
    resolve(true);
  };
  const changed = () => {
    clearTimeout(quietTimer);
    quietTimer = setTimeout(done, quietMs);
  };
  const observer = new MutationObserver(changed);
  observer.observe(document.documentElement, {
    subtree: true, childList: true, characterData: true, attributes: true,
  });
  el.addEventListener('input', changed);
  el.addEventListener('change', changed);
  const deadlineTimer = setTimeout(done, timeoutMs);
  changed();
})
"""
_TEXT_TYPES = frozenset({
    "text", "search", "url", "email", "tel", "number", "date",
    "time", "datetime-local", "month", "week",
})
_SAFE_ROLES = frozenset({
    "textbox", "searchbox", "spinbutton", "combobox", "listbox",
    "checkbox", "radio",
})
_UNSUPPORTED_TYPES = frozenset({
    "button", "submit", "reset", "image", "file", "color", "range",
    "hidden", "password",
})
_SAFE_AUTOCOMPLETE = frozenset({
    "name", "honorific-prefix", "given-name", "additional-name",
    "family-name", "honorific-suffix", "nickname", "username",
    "organization-title", "organization", "street-address",
    "address-line1", "address-line2", "address-line3", "address-level1",
    "address-level2", "address-level3", "address-level4", "country",
    "country-name", "postal-code", "language", "bday", "bday-day",
    "bday-month", "bday-year", "sex", "url", "photo", "email", "tel",
    "tel-country-code", "tel-national", "tel-area-code", "tel-local",
})
_SENSITIVE_AUTOCOMPLETE = frozenset({
    "current-password", "new-password", "one-time-code", "cc-name",
    "cc-given-name", "cc-additional-name", "cc-family-name", "cc-number",
    "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc", "cc-type",
    "transaction-currency", "transaction-amount",
})
_PRIVATE_TEXT_RE = re.compile(
    r"(?:[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"(?:\+?\d[\d\s().-]{6,}\d)|"
    r"(?:https?://|file://|[A-Za-z]:\\|/(?:home|Users|private|tmp)/)|"
    r"(?:private[_ -]?marker|secret[_ -]?marker|do[_ -]?not[_ -]?leak|"
    r"access[_ -]?token|api[_ -]?key|bearer\s+|cookie\s*=))",
    re.IGNORECASE,
)
_SAFE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")


_FIELD_METADATA_JS = r"""
(el) => {
  const attr = (name) => (el.getAttribute(name) || '').trim();
  const text = (node) => ((node && (node.innerText || node.textContent)) || '')
      .replace(/\s+/g, ' ').trim().slice(0, 512);
  const tag = (el.tagName || '').toLowerCase();
  const inputType = tag === 'textarea' ? 'textarea'
      : tag === 'select' ? (el.multiple ? 'select-multiple' : 'select-one')
      : String(el.type || attr('type') || 'text').toLowerCase();
  let label = attr('aria-label');
  let labelSource = label ? 'aria-label' : '';
  if (!label) {
    const ids = attr('aria-labelledby').split(/\s+/).filter(Boolean).slice(0, 8);
    const parts = ids.map((id) => text(el.ownerDocument.getElementById(id)))
        .filter(Boolean);
    if (parts.length) { label = parts.join(' ').slice(0, 512); labelSource = 'aria-labelledby'; }
  }
  if (!label && el.labels && el.labels.length) {
    label = Array.from(el.labels).slice(0, 4).map(text).filter(Boolean)
        .join(' ').slice(0, 512);
    if (label) labelSource = 'label';
  }
  let role = attr('role').toLowerCase();
  if (!role) {
    if (tag === 'textarea') role = 'textbox';
    else if (tag === 'select') role = el.multiple ? 'listbox' : 'combobox';
    else if (inputType === 'checkbox') role = 'checkbox';
    else if (inputType === 'radio') role = 'radio';
    else if (inputType === 'number') role = 'spinbutton';
    else role = 'textbox';
  }
  const path = [];
  let node = el;
  for (let depth = 0; node && node.nodeType === 1 && depth < 7; depth += 1) {
    const nodeTag = (node.tagName || '').toLowerCase();
    let ordinal = 1;
    for (let sibling = node.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
      if ((sibling.tagName || '').toLowerCase() === nodeTag) ordinal += 1;
    }
    path.push(`${nodeTag}:${ordinal}`);
    node = node.parentElement;
  }
  const value = ('value' in el && typeof el.value === 'string') ? el.value : '';
  const validity = el.validity || null;
  let validationReason = '';
  if (validity && !validity.valid) {
    for (const key of ['valueMissing', 'typeMismatch', 'patternMismatch',
                       'tooLong', 'tooShort', 'rangeUnderflow', 'rangeOverflow',
                       'stepMismatch', 'badInput', 'customError']) {
      if (validity[key]) { validationReason = key; break; }
    }
  }
  return {
    connected: !!el.isConnected,
    tag,
    role,
    label,
    labelSource,
    name: attr('name'),
    inputType,
    autocomplete: attr('autocomplete').toLowerCase(),
    placeholder: attr('placeholder'),
    disabled: !!el.disabled || attr('aria-disabled').toLowerCase() === 'true',
    readonly: !!el.readOnly || attr('aria-readonly').toLowerCase() === 'true',
    required: !!el.required || attr('aria-required').toLowerCase() === 'true',
    checked: (inputType === 'checkbox' || inputType === 'radio') ? !!el.checked : null,
    selected: tag === 'select' ? el.selectedIndex >= 0 : null,
    selectedIndex: tag === 'select' ? el.selectedIndex : null,
    hasValue: inputType === 'checkbox' || inputType === 'radio'
        ? !!el.checked : value.length > 0,
    ariaInvalid: attr('aria-invalid').toLowerCase(),
    validationValid: validity ? !!validity.valid : null,
    validationReason,
    structural: path.join('>'),
  };
}
"""

_FRAME_FIELDS_JS = (
    """(elements, options) => {
      const {nodes, url, maxCandidates, limit, includeHidden, includeDisabled} = options;
      if (location.href !== url) return null;
      const metadataFor = """
    + _FIELD_METADATA_JS
    + r""";
      const hasBox = (node) => {
        const box = node.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
      };
      const isVisible = (el) => {
        const style = getComputedStyle(el);
        if (style.display === 'contents') {
          return Array.from(el.childNodes).some((child) => {
            if (child.nodeType === 1) return isVisible(child);
            if (child.nodeType !== 3) return false;
            const range = document.createRange();
            range.selectNode(child);
            return hasBox(range);
          });
        }
        if (style.visibility !== 'visible') return false;
        if (typeof el.checkVisibility === 'function') {
          if (!el.checkVisibility()) return false;
        } else {
          const details = el.closest('details,summary');
          if (details?.tagName === 'DETAILS' && !details.open) return false;
        }
        return hasBox(el);
      };
      const hasDisabledAria = (el) => {
        for (let node = el; node; node = node.parentElement || node.getRootNode().host) {
          const value = (node.getAttribute('aria-disabled') || '').toLowerCase();
          if (value === 'true') return true;
          if (value === 'false') return false;
        }
        return false;
      };
      const fields = [];
      let detached = 0;
      let included = 0;
      for (const el of elements.slice(0, maxCandidates)) {
        try {
          if (!el.isConnected) { detached += 1; continue; }
          const visible = isVisible(el);
          if (!visible && !includeHidden) continue;
          const metadata = metadataFor(el);
          const disabled = metadata.disabled || el.matches(':disabled');
          if (disabled && !includeDisabled) continue;
          // Playwright interprets inherited aria-disabled using the control's role.
          const checkEnabled = !disabled && hasDisabledAria(el);
          nodes[fields.length] = el;
          fields.push({...metadata, visible, disabled, checkEnabled});
          if (includeDisabled || !checkEnabled) included += 1;
          if (included >= limit) break;
        } catch {
          detached += 1;
        }
      }
      return {fields, candidateCount: elements.length, detached};
    }
    """
)

_REPLACEMENT_METADATA_JS = (
    "(el, original) => !original.isConnected && el.ownerDocument === original.ownerDocument "
    "? (" + _FIELD_METADATA_JS + ")(el) : null"
)

_SELECT_MATCH_JS = r"""
(el, wanted) => {
  const target = String(wanted).trim();
  const matches = Array.from(el.options || []).map((option, index) => ({
    index,
    hit: option.value === String(wanted) ||
         ((option.label || option.textContent || '').replace(/\s+/g, ' ').trim() === target),
  })).filter((item) => item.hit);
  return {count: matches.length, index: matches.length === 1 ? matches[0].index : -1};
}
"""


def _json_result(payload: dict, *, error: bool = False) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return f"ERROR: {encoded}" if error else encoded


def _error(category: str, field_ref: str | None = None) -> str:
    payload = {"ok": False, "category": category}
    if field_ref and _FIELD_REF_RE.fullmatch(field_ref):
        payload["field_ref"] = field_ref
    return _json_result(payload, error=True)


class FormInspectionTimeout(str):
    """A terminal receipt from the bounded form read."""

    def __new__(cls):
        return super().__new__(cls, _json_result({
            "ok": False,
            "category": "inspection_timeout",
            "message": (
                "Form inspection timed out before returning a snapshot. "
                "Inspect the form again in the same tab."
            ),
        }, error=True))


def form_fill_error_receipt(
    category: str,
    field_ref: str | None = None,
    *,
    dispatched: bool,
) -> str:
    """Return a value-free typed terminal/uncertain semantic-fill receipt."""

    payload = {
        "ok": False,
        "category": str(category or "fill_failed")[:64],
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "effect_unknown" if dispatched else "not_dispatched",
            "outcome": (
                "outcome_unknown"
                if dispatched else "confirmed_failure_before_effect"
            ),
            "reason_code": str(category or "fill_failed")[:64],
        },
    }
    if field_ref and _FIELD_REF_RE.fullmatch(field_ref):
        payload["field_ref"] = field_ref
    return _json_result(payload, error=True)


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(str(url or ""))
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _frame_depth(frame) -> int:
    depth = 0
    current = frame
    while depth <= FORM_MAX_FRAME_DEPTH:
        try:
            current = current.parent_frame
        except Exception:
            return FORM_MAX_FRAME_DEPTH + 1
        if current is None:
            return depth
        depth += 1
    return depth


def _is_same_origin_frame(frame, main_origin) -> bool:
    try:
        url = str(frame.url or "")
    except Exception:
        return False
    return main_origin is not None and _origin(url) == main_origin


def _safe_text(value, *, limit=FORM_TEXT_LIMIT) -> tuple[str, bool, bool]:
    text = " ".join(str(value or "").split())
    if not text:
        return "", False, False
    if _PRIVATE_TEXT_RE.search(text):
        return "", False, True
    truncated = len(text) > limit
    return text[:limit], truncated, False


def _safe_name(value) -> tuple[str, bool]:
    text = str(value or "").strip()
    looks_private = (
        len(text) > 80
        or bool(re.search(r"[0-9a-f]{16,}", text, flags=re.IGNORECASE))
        or (len(text) > 20 and sum(char.isdigit() for char in text) >= 6)
    )
    if (
        not _SAFE_NAME_RE.fullmatch(text)
        or _PRIVATE_TEXT_RE.search(text)
        or looks_private
    ):
        return "", bool(text)
    return text, False


def _validation(metadata: dict) -> dict:
    aria_invalid = metadata.get("ariaInvalid")
    valid = metadata.get("validationValid")
    if aria_invalid == "true" or valid is False:
        reason = metadata.get("validationReason") or "aria_invalid"
        safe_reason = {
            "valueMissing": "value_missing",
            "typeMismatch": "type_mismatch",
            "patternMismatch": "pattern_mismatch",
            "tooLong": "too_long",
            "tooShort": "too_short",
            "rangeUnderflow": "range_underflow",
            "rangeOverflow": "range_overflow",
            "stepMismatch": "step_mismatch",
            "badInput": "bad_input",
            "customError": "custom_error",
            "aria_invalid": "aria_invalid",
        }.get(str(reason), "invalid")
        return {"status": "invalid", "reason": safe_reason}
    if aria_invalid == "false" or valid is True:
        return {"status": "valid"}
    return {"status": "unknown"}


def _safe_role(metadata: dict) -> str:
    role = str(metadata.get("role") or "").lower()
    if role in _SAFE_ROLES:
        return role
    input_type = str(metadata.get("inputType") or "text").lower()
    if input_type == "checkbox":
        return "checkbox"
    if input_type == "radio":
        return "radio"
    if input_type == "number":
        return "spinbutton"
    if input_type.startswith("select-"):
        return "listbox" if input_type == "select-multiple" else "combobox"
    return "textbox"


def _raw_signature(metadata: dict) -> tuple:
    return (
        metadata.get("tag"), metadata.get("role"), metadata.get("label"),
        metadata.get("name"), metadata.get("inputType"),
        metadata.get("autocomplete"),
    )


def _duplicate_signature(metadata: dict) -> tuple:
    return (
        metadata.get("role"), metadata.get("label"), metadata.get("inputType"),
    )


def _normalize_text(value, input_type: str) -> str:
    text = str(value if value is not None else "")
    if input_type in _TEXT_TYPES or input_type == "textarea":
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _expected_checked(value) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on", "checked"}:
        return True
    if normalized in {"false", "0", "no", "off", "unchecked"}:
        return False
    return None


async def _read_frame_fields(frame, *, url, include_hidden, include_disabled, limit):
    # Let Playwright select in its usual DOM order, including open shadow roots.
    # Keep the exact nodes with their metadata without leaving state on the page.
    nodes = await frame.evaluate_handle("() => ({__proto__: null})")
    try:
        summary = await frame.locator(_FIELD_SELECTOR).evaluate_all(_FRAME_FIELDS_JS, {
            "nodes": nodes,
            "url": url,
            "maxCandidates": FORM_MAX_DOM_CANDIDATES,
            "limit": limit,
            "includeHidden": include_hidden,
            "includeDisabled": include_disabled,
        })
        handles = await nodes.get_properties() if summary and summary["fields"] else {}
        return summary, handles
    finally:
        with suppress(Exception):
            await nodes.dispose()


class SemanticFormSession:
    """Private lifecycle and resolver for one browser owner."""

    def __init__(self, *, clock=None):
        self._clock = clock or time.monotonic
        self._secret = secrets.token_bytes(32)
        self._references: dict[str, dict] = {}
        self._stale: dict[str, tuple[str, float]] = {}

    def _purge(self) -> None:
        now = self._clock()
        for field_ref, entry in tuple(self._references.items()):
            if entry["expires_at"] <= now:
                self._references.pop(field_ref, None)
                self._stale[field_ref] = (
                    "expired_reference", now + FORM_REFERENCE_TTL_SECONDS,
                )
        for field_ref, (_reason, expires_at) in tuple(self._stale.items()):
            if expires_at <= now:
                self._stale.pop(field_ref, None)
        self._bound_stale()

    def _bound_stale(self) -> None:
        excess = len(self._stale) - FORM_MAX_STALE_REFERENCES
        if excess > 0:
            for field_ref in tuple(self._stale)[:excess]:
                self._stale.pop(field_ref, None)

    def reset(self) -> None:
        self._purge()
        expires_at = self._clock() + FORM_REFERENCE_TTL_SECONDS
        for field_ref in tuple(self._references):
            self._stale[field_ref] = ("stale_reference", expires_at)
        self._references.clear()
        self._bound_stale()

    def invalidate_page(self, page) -> None:
        self._purge()
        expires_at = self._clock() + FORM_REFERENCE_TTL_SECONDS
        for field_ref, entry in tuple(self._references.items()):
            if entry.get("page") is page:
                self._references.pop(field_ref, None)
                self._stale[field_ref] = ("stale_reference", expires_at)
        self._bound_stale()

    def _new_ref(self) -> str:
        while True:
            value = "fld_" + secrets.token_urlsafe(18)
            if value not in self._references and value not in self._stale:
                return value

    def _token(self, scope: str, value: str, *, size=16) -> str:
        return hmac.new(
            self._secret,
            f"{scope}\0{value}".encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).hexdigest()[: size * 2]

    async def inspect(
        self,
        *,
        page,
        context_generation: int,
        include_hidden: bool = False,
        include_disabled: bool = False,
        max_fields: int = FORM_MAX_FIELDS_DEFAULT,
    ) -> str:
        if not isinstance(include_hidden, bool) or not isinstance(
            include_disabled, bool
        ):
            return _error("invalid_inspection_option")
        try:
            return await asyncio.wait_for(
                self._inspect(
                    page=page,
                    context_generation=context_generation,
                    include_hidden=include_hidden,
                    include_disabled=include_disabled,
                    max_fields=max_fields,
                ),
                timeout=FORM_INSPECTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return FormInspectionTimeout()
        except Exception:
            return _error("inspection_failed")

    async def _inspect(
        self,
        *,
        page,
        context_generation: int,
        include_hidden: bool,
        include_disabled: bool,
        max_fields,
    ) -> str:
        self._purge()
        if isinstance(max_fields, bool):
            return _error("invalid_max_fields")
        try:
            requested = int(max_fields)
        except (TypeError, ValueError):
            return _error("invalid_max_fields")
        if requested <= 0:
            return _error("invalid_max_fields")
        limit = min(requested, FORM_MAX_FIELDS_HARD)
        warnings = Counter()
        text_truncated = False
        fields_truncated = requested > FORM_MAX_FIELDS_HARD
        frames_truncated = False
        try:
            frames = list(page.frames)
            main_origin = _origin(page.url)
        except Exception:
            return _error("page_unavailable")
        if len(frames) > FORM_MAX_FRAMES:
            frames = frames[:FORM_MAX_FRAMES]
            frames_truncated = True

        raw_fields = []
        frame_records = []
        try:
            main_frame = page.main_frame
        except Exception:
            main_frame = frames[0] if frames else None
        for frame in frames:
            depth = _frame_depth(frame)
            if depth > FORM_MAX_FRAME_DEPTH:
                warnings["frame_depth_exceeded"] += 1
                frames_truncated = True
                continue
            if frame is not main_frame and not _is_same_origin_frame(frame, main_origin):
                warnings["cross_origin_frame_inaccessible"] += 1
                continue
            frame_ref = "frm_" + secrets.token_urlsafe(12)
            frame_records.append((frame, frame_ref, depth))
            try:
                summary, handles = await _read_frame_fields(
                    frame,
                    url=frame.url,
                    include_hidden=include_hidden,
                    include_disabled=include_disabled,
                    limit=limit - len(raw_fields),
                )
            except Exception:
                summary = None
            if summary is None:
                warnings["frame_inspection_failed"] += 1
                continue
            if summary["candidateCount"] > FORM_MAX_DOM_CANDIDATES:
                warnings["frame_field_candidates_truncated"] += 1
                fields_truncated = True
            if summary["detached"]:
                warnings["detached_field_ignored"] += summary["detached"]
            for index, metadata in enumerate(summary["fields"]):
                node = handles.get(str(index))
                handle = node.as_element() if node is not None else None
                if handle is None:
                    warnings["detached_field_ignored"] += 1
                    continue
                try:
                    if len(raw_fields) < limit:
                        if metadata.pop("checkEnabled"):
                            metadata["disabled"] = not await handle.is_enabled()
                        if include_disabled or not metadata["disabled"]:
                            raw_fields.append({
                                "frame": frame,
                                "frame_ref": frame_ref,
                                "frame_depth": depth,
                                "handle": handle,
                                "metadata": metadata,
                            })
                            continue
                except Exception:
                    warnings["detached_field_ignored"] += 1
                with suppress(Exception):
                    await handle.dispose()
            if len(raw_fields) >= limit:
                fields_truncated = True
                break

        duplicate_counts = Counter(
            _duplicate_signature(item["metadata"]) for item in raw_fields
        )
        duplicate_ordinals = Counter()
        output_fields = []
        expires_at = self._clock() + FORM_REFERENCE_TTL_SECONDS
        for item in raw_fields:
            metadata = item["metadata"]
            signature = _raw_signature(metadata)
            duplicate_signature = _duplicate_signature(metadata)
            duplicate_ordinals[duplicate_signature] += 1
            duplicate_count = duplicate_counts[duplicate_signature]
            duplicate_ordinal = duplicate_ordinals[duplicate_signature]

            safe_label, label_cut, label_redacted = _safe_text(metadata.get("label"))
            safe_name, name_redacted = _safe_name(metadata.get("name"))
            safe_placeholder, placeholder_cut, placeholder_redacted = _safe_text(
                metadata.get("placeholder")
            )
            label_source = metadata.get("labelSource") or ""
            if not safe_label:
                if safe_placeholder:
                    safe_label = safe_placeholder
                    label_source = "placeholder"
            if label_cut or placeholder_cut:
                text_truncated = True
            if label_redacted or name_redacted or placeholder_redacted:
                warnings["sensitive_metadata_redacted"] += 1

            autocomplete = str(metadata.get("autocomplete") or "").split()
            autocomplete_purpose = next(
                (token for token in reversed(autocomplete) if token in _SAFE_AUTOCOMPLETE),
                "",
            )
            sensitive_autocomplete = any(
                token in _SENSITIVE_AUTOCOMPLETE for token in autocomplete
            )
            input_type = str(metadata.get("inputType") or "text").lower()
            sensitive = input_type == "password" or sensitive_autocomplete
            fillable = (
                not sensitive
                and input_type not in _UNSUPPORTED_TYPES
                and not bool(metadata.get("disabled"))
                and not bool(metadata.get("readonly"))
            )
            field_ref = self._new_ref()
            structural_hash = self._token(
                "structure", str(metadata.get("structural") or ""), size=20,
            )
            self._references[field_ref] = {
                "created_at": self._clock(),
                "expires_at": expires_at,
                "context_generation": int(context_generation),
                "page": page,
                "frame": item["frame"],
                "handle": item["handle"],
                "signature": signature,
                "structural_hash": structural_hash,
                "duplicate_count": duplicate_count,
                "duplicate_ordinal": duplicate_ordinal,
                "input_type": input_type,
                "tag": metadata.get("tag"),
                "sensitive": sensitive,
            }
            field_output = {
                "field_ref": field_ref,
                "frame_ref": item["frame_ref"],
                "frame_depth": item["frame_depth"],
                "role": _safe_role(metadata),
                "input_type": input_type[:32],
                "visible": bool(metadata.get("visible")),
                "enabled": not bool(metadata.get("disabled")),
                "readonly": bool(metadata.get("readonly")),
                "required": bool(metadata.get("required")),
                "has_value": bool(metadata.get("hasValue")),
                "fillable": fillable,
                "sensitive": sensitive,
                "validation": _validation(metadata),
                "duplicate_count": duplicate_count,
                "duplicate_ordinal": duplicate_ordinal,
            }
            if safe_label:
                field_output["accessible_label"] = safe_label
                field_output["label_source"] = label_source[:32]
            if safe_name:
                field_output["html_name"] = safe_name
            if autocomplete_purpose:
                field_output["autocomplete"] = autocomplete_purpose
            if label_source == "placeholder" and safe_placeholder:
                field_output["placeholder"] = safe_placeholder
            if metadata.get("checked") is not None:
                field_output["checked"] = bool(metadata.get("checked"))
            if metadata.get("selected") is not None:
                field_output["selected"] = bool(metadata.get("selected"))
            output_fields.append(field_output)

        if len(self._references) > FORM_MAX_LIVE_REFERENCES:
            oldest = sorted(
                self._references.items(), key=lambda pair: pair[1]["created_at"]
            )[: len(self._references) - FORM_MAX_LIVE_REFERENCES]
            for field_ref, _entry in oldest:
                self._references.pop(field_ref, None)
                self._stale[field_ref] = (
                    "expired_reference",
                    self._clock() + FORM_REFERENCE_TTL_SECONDS,
                )
            self._bound_stale()

        warning_items = [
            {"code": code, "count": count}
            for code, count in sorted(warnings.items())
        ]
        return _json_result({
            "ok": True,
            "snapshot_id": "form_" + secrets.token_urlsafe(12),
            "fields": output_fields,
            "field_count": len(output_fields),
            "frame_count": len(frame_records),
            "expires_in_seconds": FORM_REFERENCE_TTL_SECONDS,
            "truncated": {
                "fields": fields_truncated,
                "frames": frames_truncated,
                "text": text_truncated,
            },
            "warnings": warning_items,
        })

    async def fill(
        self,
        *,
        page,
        context_generation: int,
        field_ref: str,
        value,
    ) -> str:
        safe_ref = str(field_ref or "")[:128]
        if value is not None and not isinstance(value, (str, bool, int, float)):
            return form_fill_error_receipt(
                "invalid_value", safe_ref, dispatched=False,
            )
        if isinstance(value, str) and len(value) > FORM_MAX_VALUE_CHARS:
            return form_fill_error_receipt(
                "value_too_long", safe_ref, dispatched=False,
            )
        progress = {"dispatched": False}
        try:
            return await asyncio.wait_for(
                self._fill(
                    page=page,
                    context_generation=context_generation,
                    field_ref=safe_ref,
                    value=value,
                    progress=progress,
                ),
                timeout=FORM_FILL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return form_fill_error_receipt(
                "fill_timeout", safe_ref,
                dispatched=bool(progress["dispatched"]),
            )
        except Exception:
            return form_fill_error_receipt(
                "fill_failed", safe_ref,
                dispatched=bool(progress["dispatched"]),
            )

    def _entry(self, field_ref: str) -> tuple[dict | None, str | None]:
        self._purge()
        if not _FIELD_REF_RE.fullmatch(field_ref):
            return None, "invalid_reference"
        entry = self._references.get(field_ref)
        if entry is not None:
            return entry, None
        stale = self._stale.get(field_ref)
        return None, stale[0] if stale else "invalid_reference"

    async def _metadata_for_handle(self, handle) -> dict | None:
        try:
            metadata = await handle.evaluate(_FIELD_METADATA_JS)
        except Exception:
            return None
        return metadata if isinstance(metadata, dict) else None

    def _matches_entry(self, entry: dict, metadata: dict) -> bool:
        return (
            bool(metadata.get("connected"))
            and _raw_signature(metadata) == entry["signature"]
            and str(metadata.get("tag")) == str(entry.get("tag"))
            and str(metadata.get("inputType")) == str(entry.get("input_type"))
        )

    async def _reresolve(self, entry: dict):
        try:
            locator = entry["frame"].locator(_FIELD_SELECTOR)
            count = min(await locator.count(), FORM_MAX_DOM_CANDIDATES)
        except Exception:
            return None, "stale_reference"
        matches = []
        for index in range(count):
            handle = None
            try:
                handle = await locator.nth(index).element_handle()
                metadata = await handle.evaluate(
                    _REPLACEMENT_METADATA_JS, entry["handle"],
                )
                if not isinstance(metadata, dict) or not self._matches_entry(entry, metadata):
                    continue
                structural_hash = self._token(
                    "structure", str(metadata.get("structural") or ""), size=20,
                )
                if structural_hash != entry["structural_hash"]:
                    continue
                matches.append((handle, metadata))
                handle = None
            except Exception:
                continue
            finally:
                if handle is not None:
                    with suppress(Exception):
                        await handle.dispose()
            if len(matches) > 1:
                break
        if len(matches) == 1:
            return matches[0], None
        for handle, _metadata in matches:
            with suppress(Exception):
                await handle.dispose()
        return None, "ambiguous_reference" if len(matches) > 1 else "stale_reference"

    async def _resolve(self, entry: dict):
        metadata = await self._metadata_for_handle(entry.get("handle"))
        if metadata is None:
            return None, False, "stale_reference"
        if self._matches_entry(entry, metadata):
            return (entry["handle"], metadata), False, None
        if metadata.get("connected"):
            return None, False, "stale_reference"
        replacement, error = await self._reresolve(entry)
        return replacement, True, error

    async def _current_value(self, handle, input_type: str):
        if input_type in {"checkbox", "radio"}:
            return bool(await handle.is_checked())
        if input_type.startswith("select-"):
            metadata = await self._metadata_for_handle(handle)
            return None if metadata is None else metadata.get("selectedIndex")
        return _normalize_text(await handle.input_value(), input_type)

    async def _fill(
        self,
        *,
        page,
        context_generation: int,
        field_ref: str,
        value,
        progress: dict,
    ) -> str:
        entry, entry_error = self._entry(field_ref)
        if entry is None:
            return form_fill_error_receipt(
                entry_error or "invalid_reference", field_ref, dispatched=False,
            )
        if entry.get("context_generation") != int(context_generation):
            return form_fill_error_receipt(
                "stale_reference", field_ref, dispatched=False,
            )
        if entry.get("page") is not page:
            return form_fill_error_receipt(
                "inactive_page", field_ref, dispatched=False,
            )
        try:
            if page.is_closed() or entry["frame"] not in list(page.frames):
                return form_fill_error_receipt(
                    "stale_reference", field_ref, dispatched=False,
                )
            await page.bring_to_front()
        except Exception:
            return form_fill_error_receipt(
                "page_unavailable", field_ref, dispatched=False,
            )
        if entry.get("sensitive"):
            return form_fill_error_receipt(
                "sensitive_field_not_authorized", field_ref, dispatched=False,
            )
        input_type = str(entry.get("input_type") or "")
        resolved, reresolved, resolve_error = await self._resolve(entry)
        if resolved is None:
            return form_fill_error_receipt(
                resolve_error or "stale_reference", field_ref, dispatched=False,
            )
        handle, metadata = resolved
        try:
            visible = bool(await handle.is_visible())
            enabled = bool(await handle.is_enabled())
        except Exception:
            return form_fill_error_receipt(
                "stale_reference", field_ref, dispatched=False,
            )
        if not visible:
            return form_fill_error_receipt(
                "hidden_field", field_ref, dispatched=False,
            )
        if not enabled or bool(metadata.get("disabled")):
            return form_fill_error_receipt(
                "disabled_field", field_ref, dispatched=False,
            )
        if bool(metadata.get("readonly")):
            return form_fill_error_receipt(
                "readonly_field", field_ref, dispatched=False,
            )
        if input_type in _UNSUPPORTED_TYPES or (
            input_type not in _TEXT_TYPES
            and input_type not in {"textarea", "select-one", "select-multiple", "checkbox", "radio"}
        ):
            return form_fill_error_receipt(
                "unsupported_control", field_ref, dispatched=False,
            )

        operation = "fill"
        expected = None
        try:
            before = await self._current_value(handle, input_type)
            if input_type in {"checkbox", "radio"}:
                expected = _expected_checked(value)
                if expected is None:
                    return form_fill_error_receipt(
                        "invalid_checked_state", field_ref, dispatched=False,
                    )
                if input_type == "radio" and not expected:
                    return form_fill_error_receipt(
                        "unsupported_radio_state", field_ref, dispatched=False,
                    )
                operation = "check" if expected else "uncheck"
                progress["dispatched"] = True
                if expected:
                    await handle.check(timeout=FORM_FIELD_ACTION_TIMEOUT_MS)
                else:
                    await handle.uncheck(timeout=FORM_FIELD_ACTION_TIMEOUT_MS)
            elif input_type.startswith("select-"):
                operation = "select"
                match = await handle.evaluate(_SELECT_MATCH_JS, str(value))
                if not isinstance(match, dict) or match.get("count") != 1:
                    category = (
                        "ambiguous_option" if isinstance(match, dict)
                        and int(match.get("count") or 0) > 1
                        else "invalid_option"
                    )
                    return form_fill_error_receipt(
                        category, field_ref, dispatched=False,
                    )
                expected = int(match["index"])
                progress["dispatched"] = True
                await handle.select_option(
                    index=expected, timeout=FORM_FIELD_ACTION_TIMEOUT_MS,
                )
            else:
                expected = _normalize_text(value, input_type)
                progress["dispatched"] = True
                await handle.fill(
                    str(value if value is not None else ""),
                    timeout=FORM_FIELD_ACTION_TIMEOUT_MS,
                )
        except Exception:
            # The operation may have dispatched input/change before a framework
            # replaced the node.  Re-resolve only to verify; never replay it.
            if not progress["dispatched"]:
                return form_fill_error_receipt(
                    "unverified_result", field_ref, dispatched=False,
                )
            replacement, replacement_error = await self._reresolve(entry)
            if replacement is None:
                return form_fill_error_receipt(
                    "ambiguous_result" if replacement_error == "ambiguous_reference"
                    else "unverified_result",
                    field_ref,
                    dispatched=True,
                )
            handle, metadata = replacement
            reresolved = True

        try:
            await handle.evaluate(_WAIT_VALIDATION_STABLE_JS, {
                "quietMs": FORM_VALIDATION_QUIET_MS,
                "timeoutMs": FORM_VALIDATION_TIMEOUT_MS,
            })
        except Exception:
            # Verification below still decides success; an unavailable event
            # observer never causes the action to be replayed.
            await asyncio.sleep(0)

        post_metadata = await self._metadata_for_handle(handle)
        if post_metadata is None or not self._matches_entry(entry, post_metadata):
            replacement, replacement_error = await self._reresolve(entry)
            if replacement is None:
                return form_fill_error_receipt(
                    "ambiguous_result" if replacement_error == "ambiguous_reference"
                    else "unverified_result",
                    field_ref,
                    dispatched=True,
                )
            handle, post_metadata = replacement
            reresolved = True
        try:
            after = await self._current_value(handle, input_type)
        except Exception:
            return form_fill_error_receipt(
                "unverified_result", field_ref, dispatched=True,
            )

        verified = after == expected
        if not verified:
            return form_fill_error_receipt(
                "postcondition_mismatch", field_ref, dispatched=True,
            )
        validation = _validation(post_metadata)
        if validation.get("status") == "invalid":
            return form_fill_error_receipt(
                "validation_rejected", field_ref, dispatched=True,
            )
        changed = before != after
        state_token = self._token(
            f"field-state:{field_ref}", repr(after), size=16,
        )
        result = {
            "ok": True,
            "field_ref": field_ref,
            "operation": operation,
            "verified": True,
            "value_present": bool(post_metadata.get("hasValue")),
            "changed": changed,
            "reresolved": reresolved,
            "validation": validation,
            "state_token": state_token,
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_confirmed",
                "outcome": "confirmed_success",
                "reason_code": "semantic_fill_verified",
            },
        }
        if input_type not in {"checkbox", "radio", "select-one", "select-multiple"}:
            result["normalized_length"] = len(str(after))
        return _json_result(result)


__all__ = [
    "FORM_FIELD_ACTION_TIMEOUT_MS",
    "FORM_FILL_TIMEOUT_SECONDS",
    "FORM_MAX_FIELDS_DEFAULT",
    "FORM_REFERENCE_TTL_SECONDS",
    "FORM_VALIDATION_TIMEOUT_MS",
    "SemanticFormSession",
    "form_fill_error_receipt",
]
