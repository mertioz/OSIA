"""Deterministic, privacy-aware permission-scope normalization (WOR-192).

Private canonical values are intended only for the protected permission store.
Callers must use :attr:`NormalizedScope.safe_category` or ``public_metadata``
for logs, protocol frames and diagnostics.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import ntpath
import os
import posixpath
import re
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote, unquote, urlsplit, urlunsplit


class ScopeNormalizationError(ValueError):
    """A stable, value-free scope-normalization failure."""

    def __init__(self, code: str = "invalid_scope"):
        super().__init__(code)
        self.code = code


class WebScopeKind(str, Enum):
    EXACT_ORIGIN = "exact_origin"
    EXACT_URL = "exact_url"
    SUBDOMAIN_WILDCARD = "subdomain_wildcard"


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ScopeNormalizationError("non_finite_number")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ScopeNormalizationError("non_string_key")
            normalized[key] = _canonical_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise ScopeNormalizationError("unsupported_canonical_type")


def canonical_serialize(value: Any) -> str:
    """Return the single stable JSON representation used for hashes/storage."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_serialize(value).encode("utf-8")).hexdigest()


def sensitive_hash_equal(left: str, right: str) -> bool:
    """Compare sensitive hashes without data-dependent early exit."""

    return hmac.compare_digest(str(left or ""), str(right or ""))


@dataclass(frozen=True, repr=False)
class NormalizedScope:
    kind: str
    private_canonical: str
    safe_category: str
    recursive: bool = False
    grantable: bool = True

    def __post_init__(self) -> None:
        if not self.kind or not self.safe_category:
            raise ScopeNormalizationError("invalid_scope_metadata")
        if not isinstance(self.recursive, bool) or not isinstance(self.grantable, bool):
            raise ScopeNormalizationError("invalid_scope_flags")
        try:
            decoded = json.loads(self.private_canonical)
        except (TypeError, ValueError) as exc:
            raise ScopeNormalizationError("invalid_private_scope") from exc
        canonical = canonical_serialize(decoded)
        if not sensitive_hash_equal(canonical, self.private_canonical):
            raise ScopeNormalizationError("non_canonical_private_scope")

    @property
    def scope_hash(self) -> str:
        return canonical_hash({
            "grantable": self.grantable,
            "kind": self.kind,
            "private": json.loads(self.private_canonical),
            "recursive": self.recursive,
        })

    def protected_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "private_canonical": self.private_canonical,
            "safe_category": self.safe_category,
            "recursive": self.recursive,
            "grantable": self.grantable,
        }

    def public_metadata(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "safe_category": self.safe_category,
            "recursive": self.recursive,
            "grantable": self.grantable,
            "scope_hash": self.scope_hash,
        }

    def __repr__(self) -> str:
        return (
            "NormalizedScope("
            f"kind={self.kind!r}, safe_category={self.safe_category!r}, "
            f"recursive={self.recursive!r}, grantable={self.grantable!r}, "
            f"scope_hash={self.scope_hash[:12]!r})"
        )


def normalized_scope_from_protected(payload: Mapping[str, Any]) -> NormalizedScope:
    try:
        if not isinstance(payload, Mapping):
            raise TypeError("scope_not_mapping")
        if not all(
            isinstance(payload.get(name), str)
            for name in ("kind", "private_canonical", "safe_category")
        ):
            raise TypeError("scope_text_field_invalid")
        if "recursive" in payload and not isinstance(payload["recursive"], bool):
            raise TypeError("scope_recursive_invalid")
        if "grantable" in payload and not isinstance(payload["grantable"], bool):
            raise TypeError("scope_grantable_invalid")
        return NormalizedScope(
            kind=str(payload["kind"]),
            private_canonical=str(payload["private_canonical"]),
            safe_category=str(payload["safe_category"]),
            recursive=bool(payload.get("recursive", False)),
            grantable=bool(payload.get("grantable", True)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScopeNormalizationError("invalid_stored_scope") from exc


def _scope(
    kind: str,
    private: Mapping[str, Any],
    safe_category: str,
    *,
    recursive: bool = False,
    grantable: bool = True,
) -> NormalizedScope:
    return NormalizedScope(
        kind=kind,
        private_canonical=canonical_serialize(private),
        safe_category=safe_category,
        recursive=bool(recursive),
        grantable=bool(grantable),
    )


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _contains_parent_component(value: str, separator_pattern: str) -> bool:
    return any(part == ".." for part in re.split(separator_pattern, value))


def _looks_windows_path(value: str, base: str) -> bool:
    return bool(
        os.name == "nt"
        or _WINDOWS_DRIVE_RE.match(value)
        or _WINDOWS_DRIVE_RE.match(base)
        or value.startswith("\\\\")
        or base.startswith("\\\\")
    )


def normalize_file_scope(
    path: str | os.PathLike[str],
    *,
    base: str | os.PathLike[str],
    recursive: bool = False,
) -> NormalizedScope:
    """Normalize one lexical path without following symlinks or junctions."""

    raw = os.fspath(path)
    raw_base = os.fspath(base)
    if not isinstance(raw, str) or not isinstance(raw_base, str):
        raise ScopeNormalizationError("path_must_be_text")
    if not raw or not raw_base or "\x00" in raw or "\x00" in raw_base:
        raise ScopeNormalizationError("invalid_path")

    if _looks_windows_path(raw, raw_base):
        value = raw.replace("/", "\\")
        base_value = raw_base.replace("/", "\\")
        lowered = value.lower()
        if lowered.startswith(_WINDOWS_DEVICE_PREFIXES) or base_value.lower().startswith(
            _WINDOWS_DEVICE_PREFIXES
        ):
            raise ScopeNormalizationError("unsupported_device_path")
        if _contains_parent_component(value, r"[\\/]+"):
            raise ScopeNormalizationError("parent_traversal_ambiguous")
        if _contains_parent_component(base_value, r"[\\/]+"):
            raise ScopeNormalizationError("base_traversal_ambiguous")
        for candidate in (value, base_value):
            components = [
                part for part in re.split(r"[\\/]+", candidate) if part
            ]
            for component in components:
                if component.endswith((" ", ".")):
                    raise ScopeNormalizationError("ambiguous_windows_component")
                base_name = component.split(":", 1)[0].split(".", 1)[0].upper()
                if base_name in _WINDOWS_RESERVED_NAMES:
                    raise ScopeNormalizationError("unsupported_device_path")
        if _WINDOWS_DRIVE_RE.match(value) and not ntpath.isabs(value):
            raise ScopeNormalizationError("drive_relative_path")
        if not ntpath.isabs(base_value):
            raise ScopeNormalizationError("base_not_absolute")
        if not ntpath.splitdrive(base_value)[0]:
            raise ScopeNormalizationError("base_not_fully_qualified")
        absolute = value if ntpath.isabs(value) else ntpath.join(base_value, value)
        canonical = ntpath.normcase(ntpath.normpath(absolute))
        if not ntpath.splitdrive(canonical)[0]:
            raise ScopeNormalizationError("path_not_fully_qualified")
        if canonical.startswith("\\\\"):
            drive, _tail = ntpath.splitdrive(canonical)
            if not drive or drive.count("\\") < 3:
                raise ScopeNormalizationError("invalid_unc_path")
        style = "windows"
    else:
        if _contains_parent_component(raw, r"/+"):
            raise ScopeNormalizationError("parent_traversal_ambiguous")
        if _contains_parent_component(raw_base, r"/+"):
            raise ScopeNormalizationError("base_traversal_ambiguous")
        if not posixpath.isabs(raw_base):
            raise ScopeNormalizationError("base_not_absolute")
        absolute = raw if posixpath.isabs(raw) else posixpath.join(raw_base, raw)
        canonical = posixpath.normpath(absolute)
        style = "posix"

    return _scope(
        "file",
        {"path": canonical, "style": style},
        "directory_tree" if recursive else "local_path",
        recursive=recursive,
    )


def combine_scopes(
    scopes: Mapping[str, NormalizedScope],
    *,
    safe_category: str,
) -> NormalizedScope:
    if not scopes:
        raise ScopeNormalizationError("empty_scope_set")
    private = {
        str(name): json.loads(scope.private_canonical)
        for name, scope in sorted(scopes.items())
    }
    return _scope(
        "scope_set",
        private,
        safe_category,
        recursive=any(scope.recursive for scope in scopes.values()),
        grantable=all(scope.grantable for scope in scopes.values()),
    )


def _canonical_host(host: str) -> str:
    candidate = str(host or "").rstrip(".").lower()
    if not candidate:
        raise ScopeNormalizationError("missing_hostname")
    try:
        if ":" in candidate:  # IPv6 literal; urlsplit has already validated it.
            return candidate
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ScopeNormalizationError("invalid_hostname") from exc


def normalize_web_scope(url: str, *, kind: WebScopeKind) -> NormalizedScope:
    if not isinstance(kind, WebScopeKind):
        raise ScopeNormalizationError("invalid_web_scope_kind")
    raw = str(url or "").strip()
    if not raw or "\x00" in raw:
        raise ScopeNormalizationError("invalid_url")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ScopeNormalizationError("invalid_url") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ScopeNormalizationError("unsupported_url_scheme")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ScopeNormalizationError("userinfo_not_allowed")
    host = _canonical_host(parsed.hostname or "")

    wildcard = host.startswith("*.")
    if kind == WebScopeKind.SUBDOMAIN_WILDCARD:
        if not wildcard or host.count(".") < 2:
            raise ScopeNormalizationError("explicit_wildcard_required")
        host = host[2:]
    elif wildcard:
        raise ScopeNormalizationError("wildcard_not_exact")

    default_port = 80 if scheme == "http" else 443
    canonical_port = None if port in (None, default_port) else port
    host_for_netloc = f"[{host}]" if ":" in host else host
    netloc = host_for_netloc + (f":{canonical_port}" if canonical_port else "")

    if kind in {WebScopeKind.EXACT_ORIGIN, WebScopeKind.SUBDOMAIN_WILDCARD}:
        if parsed.path not in {"", "/"} or parsed.query:
            raise ScopeNormalizationError("origin_scope_has_url_components")
        canonical = f"{scheme}://{netloc}"
    else:
        path = parsed.path or "/"
        decoded_path = unquote(path, errors="strict")
        trailing = decoded_path.endswith("/")
        normalized_path = posixpath.normpath(decoded_path)
        if not normalized_path.startswith("/"):
            normalized_path = "/" + normalized_path
        if trailing and normalized_path != "/":
            normalized_path += "/"
        normalized_path = quote(
            normalized_path,
            safe="/:@-._~!$&'()*+,;=",
        )
        canonical = urlunsplit((scheme, netloc, normalized_path, parsed.query, ""))

    return _scope(
        "web",
        {"identity": canonical, "scope_kind": kind.value},
        "web_origin" if kind != WebScopeKind.EXACT_URL else "web_url",
    )


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def normalize_command_scope(
    executable: str,
    argv: Iterable[str],
    *,
    cwd: str | os.PathLike[str],
    env_allowlist: Iterable[str] = (),
) -> NormalizedScope:
    if not isinstance(argv, (list, tuple)):
        raise ScopeNormalizationError("argv_list_required")
    exe = str(executable or "").strip()
    if not exe or "\x00" in exe:
        raise ScopeNormalizationError("invalid_executable")
    normalized_argv = []
    for value in argv:
        if not isinstance(value, str) or "\x00" in value:
            raise ScopeNormalizationError("invalid_argv")
        normalized_argv.append(value)

    cwd_scope = normalize_file_scope(cwd, base=cwd, recursive=False)
    resolved = shutil.which(exe)
    if resolved:
        executable_scope = normalize_file_scope(resolved, base=cwd, recursive=False)
        executable_identity = json.loads(executable_scope.private_canonical)["path"]
    elif os.path.isabs(exe):
        executable_scope = normalize_file_scope(exe, base=cwd, recursive=False)
        executable_identity = json.loads(executable_scope.private_canonical)["path"]
    else:
        executable_identity = exe.lower() if os.name == "nt" else exe

    env_names = []
    for name in env_allowlist:
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise ScopeNormalizationError("invalid_environment_name")
        env_names.append(name.upper() if os.name == "nt" else name)

    return _scope(
        "command",
        {
            "argv": normalized_argv,
            "cwd": json.loads(cwd_scope.private_canonical)["path"],
            "environment_allowlist": sorted(set(env_names)),
            "executable": executable_identity,
            "resolved": bool(resolved or os.path.isabs(exe)),
        },
        "local_command",
        grantable=bool(resolved or os.path.isabs(exe)),
    )


_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _opaque(value: str, code: str) -> str:
    candidate = str(value or "")
    if not _OPAQUE_ID_RE.fullmatch(candidate):
        raise ScopeNormalizationError(code)
    return candidate


def normalize_connector_scope(
    connector_id: str,
    operation: str,
    *,
    account_ref: str,
    tenant_ref: str = "",
    schema_hash: str = "",
) -> NormalizedScope:
    connector = _opaque(connector_id, "invalid_connector_identity")
    op = _opaque(operation, "invalid_connector_operation")
    account = _opaque(account_ref, "invalid_account_identity")
    tenant = _opaque(tenant_ref, "invalid_tenant_identity") if tenant_ref else ""
    schema = str(schema_hash or "")
    if schema and not re.fullmatch(r"[0-9a-f]{64}", schema):
        raise ScopeNormalizationError("invalid_schema_hash")
    return _scope(
        "connector",
        {
            "account_ref": account,
            "connector_id": connector,
            "operation": op,
            "schema_hash": schema,
            "tenant_ref": tenant,
        },
        "connector_operation",
    )


def normalize_gmail_scope(account_ref: str, operation: str) -> NormalizedScope:
    return normalize_connector_scope(
        "gmail", operation, account_ref=account_ref,
    )


def normalize_mcp_scope(
    server_id: str,
    tool_name: str,
    *,
    account_ref: str,
    schema_hash: str,
    tenant_ref: str = "",
) -> NormalizedScope:
    return normalize_connector_scope(
        f"mcp:{_opaque(server_id, 'invalid_mcp_server_identity')}",
        tool_name,
        account_ref=account_ref,
        tenant_ref=tenant_ref,
        schema_hash=schema_hash,
    )


def normalize_purchase_scope(
    merchant_origin: str,
    amount_minor: int,
    currency: str,
    order_fingerprint: str,
) -> NormalizedScope:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ScopeNormalizationError("minor_units_integer_required")
    if amount_minor < 0:
        raise ScopeNormalizationError("negative_purchase_amount")
    currency_code = str(currency or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency_code):
        raise ScopeNormalizationError("invalid_iso4217_currency")
    origin = normalize_web_scope(
        merchant_origin, kind=WebScopeKind.EXACT_ORIGIN,
    )
    fingerprint = str(order_fingerprint or "")
    if not fingerprint:
        raise ScopeNormalizationError("missing_order_fingerprint")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        fingerprint = canonical_hash({"order_fingerprint": fingerprint})
    return _scope(
        "purchase",
        {
            "amount_minor": amount_minor,
            "currency": currency_code,
            "merchant": json.loads(origin.private_canonical)["identity"],
            "order_fingerprint": fingerprint,
        },
        "purchase",
    )


def normalize_exact_call_scope(
    call_hash: str,
    *,
    safe_category: str,
    grantable: bool = False,
) -> NormalizedScope:
    if not re.fullmatch(r"[0-9a-f]{64}", str(call_hash or "")):
        raise ScopeNormalizationError("invalid_call_hash")
    return _scope(
        "exact_call",
        {"call_hash": call_hash},
        safe_category,
        grantable=grantable,
    )


__all__ = [
    "NormalizedScope", "ScopeNormalizationError", "WebScopeKind",
    "canonical_hash", "canonical_serialize", "combine_scopes",
    "normalize_command_scope", "normalize_connector_scope",
    "normalize_exact_call_scope", "normalize_file_scope", "normalize_gmail_scope",
    "normalize_mcp_scope", "normalize_purchase_scope", "normalize_web_scope",
    "normalized_scope_from_protected", "sensitive_hash_equal",
]
