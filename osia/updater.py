"""Qt-free Windows application updater core.

The GUI schedules this module on worker threads.  This module owns every trust
decision: embedded metadata, strict channel manifests, HTTPS endpoints,
streaming verification, persistent state, and the fixed Inno launch plan.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from osia.config import CONFIG_DIR
from osia.runtime import is_frozen, resource_path
from osia.version import PRODUCT_VERSION, parse_product_version


UPDATER_SCHEMA_VERSION = 1
PRODUCT = "OSIA"
PLATFORM = "windows"
ARCHITECTURE = "x86_64"
ALLOWED_CHANNELS = frozenset({"internal-preview", "stable"})
ALLOWED_INSTALLER_HOST = "download.osia.space"
CHANNEL_URLS = {
    "internal-preview": (
        "https://download.osia.space/channels/internal-preview/windows-x64.json"
    ),
    "stable": "https://download.osia.space/channels/stable/windows-x64.json",
}
ROLLOUT_STATES = frozenset({"enabled", "paused"})

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
MANIFEST_MAX_BYTES = 64 * 1024
INSTALLER_MAX_BYTES = 2 * 1024 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
PART_MAX_AGE_SECONDS = 24 * 60 * 60
INSTALLER_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

FIRST_CHECK_SECONDS = 30.0
NORMAL_CHECK_SECONDS = 6 * 60 * 60.0
RETRY_DELAYS_SECONDS = (15 * 60.0, 60 * 60.0)
JITTER_FRACTION = 0.10
DISMISS_SECONDS = 24 * 60 * 60.0

STATE_PATH = Path(CONFIG_DIR) / "update-state.json"
UPDATES_DIR = Path(CONFIG_DIR) / "updates"

INSTALLER_ARGUMENTS = (
    "/CURRENTUSER",
    "/CLOSEAPPLICATIONS",
    "/NORESTART",
    "/NORESTARTAPPLICATIONS",
    "/LOGCLOSEAPPLICATIONS",
    "/OSIAUPDATE=1",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_RFC3339_UTC_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z"
)


class UpdaterError(RuntimeError):
    """Safe updater failure identified by a non-secret public code."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


class MetadataError(UpdaterError):
    pass


class ManifestError(UpdaterError):
    pass


class DownloadError(UpdaterError):
    pass


class DownloadCancelled(DownloadError):
    pass


@dataclass(frozen=True)
class AppMetadata:
    product: str
    version: str
    channel: str
    platform: str
    architecture: str
    build_timestamp: str
    commit_sha: str | None
    updater_schema_version: int
    build_override: dict | None


@dataclass(frozen=True)
class InstallerSpec:
    url: str
    size_bytes: int
    sha256: str
    signed: bool
    signature_publisher: str | None


@dataclass(frozen=True)
class TemporaryUnsignedStablePolicy:
    """Explicit pre-WOR-220 trust boundary for official stable manifests."""

    enabled: bool
    authority_issue: str
    removal_issue: str
    channel: str
    manifest_url: str
    minimum_version: str
    installer_host: str


@dataclass(frozen=True)
class ManualUpdateBridge:
    """Exact immutable installer offered to clients with the legacy policy."""

    version: str
    url: str
    size_bytes: int
    sha256: str


TEMPORARY_UNSIGNED_STABLE_POLICY = TemporaryUnsignedStablePolicy(
    enabled=True,
    authority_issue="WOR-183",
    removal_issue="WOR-220",
    channel="stable",
    manifest_url=CHANNEL_URLS["stable"],
    minimum_version="1.1.0",
    installer_host=ALLOWED_INSTALLER_HOST,
)

LEGACY_UNSIGNED_STABLE_BRIDGE = ManualUpdateBridge(
    version="1.1.1",
    url="https://download.osia.space/windows/1.1.1/OSIA-Setup.exe",
    size_bytes=1_185_800_867,
    sha256="519c64d75eb4d793f6bf1c50bba64a3b7d3c8c2e81092df230da7724d6fd1e10",
)


@dataclass(frozen=True)
class UpdateManifest:
    schema_version: int
    product: str
    platform: str
    architecture: str
    channel: str
    version: str
    published_at: str
    installer: InstallerSpec
    mandatory: bool
    minimum_supported_version: str
    minimum_updater_version: str
    release_notes: tuple[str, ...]
    release_notes_url: None
    rollout_state: str
    temporary_unsigned_stable: bool = False

    def as_dict(self) -> dict:
        return {
            "schemaVersion": self.schema_version,
            "product": self.product,
            "platform": self.platform,
            "architecture": self.architecture,
            "channel": self.channel,
            "version": self.version,
            "publishedAt": self.published_at,
            "installer": {
                "url": self.installer.url,
                "sizeBytes": self.installer.size_bytes,
                "sha256": self.installer.sha256,
                "signed": self.installer.signed,
                "signaturePublisher": self.installer.signature_publisher,
            },
            "mandatory": self.mandatory,
            "minimumSupportedVersion": self.minimum_supported_version,
            "minimumUpdaterVersion": self.minimum_updater_version,
            "releaseNotes": list(self.release_notes),
            "releaseNotesUrl": None,
            "rollout": {"state": self.rollout_state},
        }


@dataclass(frozen=True)
class ManifestFetchResult:
    status: str
    manifest: UpdateManifest | None
    channel_statuses: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class InstallerPlan:
    executable: Path
    arguments: tuple[str, ...]

    @property
    def command(self) -> list[str]:
        return [str(self.executable), *self.arguments]


def _reject_constant(_value: str):
    raise ValueError("non_finite_number")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _load_json_strict(payload: bytes | str, *, maximum: int) -> object:
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if len(raw) > maximum:
        raise ValueError("json_too_large")
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc


def _require_object(value: object, code: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(code)
    return value


def _require_exact_fields(value: Mapping, fields: set[str], code: str) -> None:
    if set(value) != fields:
        raise ValueError(code)


def _require_plain_int(value: object, code: str) -> int:
    if type(value) is not int:
        raise ValueError(code)
    return value


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or _RFC3339_UTC_RE.fullmatch(value) is None:
        raise ValueError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("invalid_timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError("invalid_timestamp")
    return value


def _validate_version(value: object, code: str = "invalid_version") -> str:
    try:
        parse_product_version(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(code) from exc
    return value  # type: ignore[return-value]


def compare_versions(left: str, right: str) -> int:
    a = parse_product_version(left)
    b = parse_product_version(right)
    return (a > b) - (a < b)


def parse_app_metadata(payload: bytes | str) -> AppMetadata:
    try:
        raw = _require_object(
            _load_json_strict(payload, maximum=MANIFEST_MAX_BYTES),
            "invalid_metadata",
        )
        _require_exact_fields(
            raw,
            {
                "product",
                "version",
                "updateChannel",
                "platform",
                "architecture",
                "buildTimestamp",
                "commitSha",
                "updaterSchemaVersion",
                "buildOverride",
            },
            "unknown_metadata_field",
        )
        if raw["product"] != PRODUCT:
            raise ValueError("metadata_product_mismatch")
        version = _validate_version(raw["version"], "invalid_metadata_version")
        channel = raw["updateChannel"]
        if channel not in ALLOWED_CHANNELS:
            raise ValueError("invalid_metadata_channel")
        if raw["platform"] != PLATFORM:
            raise ValueError("metadata_platform_mismatch")
        if raw["architecture"] != ARCHITECTURE:
            raise ValueError("metadata_architecture_mismatch")
        timestamp = _validate_timestamp(raw["buildTimestamp"])
        commit = raw["commitSha"]
        if commit is not None and (
            not isinstance(commit, str) or _COMMIT_RE.fullmatch(commit) is None
        ):
            raise ValueError("invalid_metadata_commit")
        schema = _require_plain_int(raw["updaterSchemaVersion"], "invalid_metadata_schema")
        if schema != UPDATER_SCHEMA_VERSION:
            raise ValueError("unsupported_metadata_schema")
        override = raw["buildOverride"]
        if override is not None:
            override = _require_object(override, "invalid_build_override")
            _require_exact_fields(
                override,
                {"sourceVersion", "version", "channel"},
                "invalid_build_override",
            )
            _validate_version(override["sourceVersion"], "invalid_build_override")
            if override["sourceVersion"] != PRODUCT_VERSION:
                raise ValueError("invalid_build_override")
            if _validate_version(override["version"], "invalid_build_override") != version:
                raise ValueError("invalid_build_override")
            if override["channel"] != channel or channel != "internal-preview":
                raise ValueError("invalid_build_override")
        return AppMetadata(
            product=PRODUCT,
            version=version,
            channel=channel,
            platform=PLATFORM,
            architecture=ARCHITECTURE,
            build_timestamp=timestamp,
            commit_sha=commit,
            updater_schema_version=schema,
            build_override=dict(override) if override is not None else None,
        )
    except ValueError as exc:
        raise MetadataError(str(exc)) from exc


def load_app_metadata(path: Path | None = None) -> AppMetadata:
    metadata_path = Path(path) if path is not None else resource_path("app_metadata.json")
    try:
        payload = metadata_path.read_bytes()
    except OSError as exc:
        raise MetadataError("metadata_unavailable") from exc
    return parse_app_metadata(payload)


def updater_enabled(metadata: AppMetadata | None = None) -> bool:
    if sys.platform != "win32" or not is_frozen():
        return False
    try:
        current = metadata or load_app_metadata()
    except MetadataError:
        return False
    return (
        current.product == PRODUCT
        and current.platform == PLATFORM
        and current.architecture == ARCHITECTURE
        and current.channel in ALLOWED_CHANNELS
    )


def _validate_release_notes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 5:
        raise ValueError("invalid_release_notes")
    notes: list[str] = []
    for note in value:
        if (
            not isinstance(note, str)
            or not note
            or len(note) > 240
            or any(ord(character) < 32 or ord(character) == 127 for character in note)
            or "<" in note
            or ">" in note
        ):
            raise ValueError("invalid_release_notes")
        notes.append(note)
    return tuple(notes)


def _validate_installer_url(value: object, version: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_installer_url")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != ALLOWED_INSTALLER_HOST
        or parsed.hostname != ALLOWED_INSTALLER_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/windows/{version}/OSIA-Setup.exe"
    ):
        raise ValueError("invalid_installer_url")
    return value


def _temporary_unsigned_stable_allowed(
    metadata: AppMetadata,
    *,
    source_url: str | None,
    version: str,
    installer_url: str,
    signed: bool,
    signature_publisher: str | None,
) -> bool:
    """Authorize only the official stable release configuration pre-WOR-220."""

    policy = TEMPORARY_UNSIGNED_STABLE_POLICY
    parsed_installer = urlsplit(installer_url)
    return (
        policy.enabled
        and metadata.product == PRODUCT
        and metadata.platform == PLATFORM
        and metadata.architecture == ARCHITECTURE
        and metadata.channel == policy.channel
        and source_url == policy.manifest_url
        and compare_versions(version, policy.minimum_version) >= 0
        and parsed_installer.hostname == policy.installer_host
        and parsed_installer.path == f"/windows/{version}/OSIA-Setup.exe"
        and signed is False
        and signature_publisher is None
    )


def parse_manifest(
    payload: bytes | str,
    metadata: AppMetadata,
    *,
    source_url: str | None = None,
) -> UpdateManifest:
    try:
        raw = _require_object(
            _load_json_strict(payload, maximum=MANIFEST_MAX_BYTES),
            "invalid_manifest",
        )
        _require_exact_fields(
            raw,
            {
                "schemaVersion",
                "product",
                "platform",
                "architecture",
                "channel",
                "version",
                "publishedAt",
                "installer",
                "mandatory",
                "minimumSupportedVersion",
                "minimumUpdaterVersion",
                "releaseNotes",
                "releaseNotesUrl",
                "rollout",
            },
            "unknown_manifest_field",
        )
        schema = _require_plain_int(raw["schemaVersion"], "invalid_schema")
        if schema != UPDATER_SCHEMA_VERSION:
            raise ValueError("unsupported_schema")
        if raw["product"] != PRODUCT:
            raise ValueError("product_mismatch")
        if raw["platform"] != metadata.platform:
            raise ValueError("platform_mismatch")
        if raw["architecture"] != metadata.architecture:
            raise ValueError("architecture_mismatch")
        if raw["channel"] != metadata.channel:
            raise ValueError("channel_mismatch")
        version = _validate_version(raw["version"])
        comparison = compare_versions(version, metadata.version)
        if comparison == 0:
            raise ValueError("equal_version")
        if comparison < 0:
            raise ValueError("downgrade")
        published = _validate_timestamp(raw["publishedAt"])

        installer = _require_object(raw["installer"], "invalid_installer")
        _require_exact_fields(
            installer,
            {"url", "sizeBytes", "sha256", "signed", "signaturePublisher"},
            "unknown_installer_field",
        )
        url = _validate_installer_url(installer["url"], version)
        size = _require_plain_int(installer["sizeBytes"], "invalid_size")
        if size <= 0 or size > INSTALLER_MAX_BYTES:
            raise ValueError("invalid_size")
        digest = installer["sha256"]
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError("invalid_sha256")
        signed = installer["signed"]
        if type(signed) is not bool:
            raise ValueError("invalid_signed_state")
        publisher = installer["signaturePublisher"]
        if signed:
            if not isinstance(publisher, str) or not publisher.strip():
                raise ValueError("invalid_signature_publisher")
        elif publisher is not None:
            raise ValueError("invalid_signature_publisher")
        temporary_unsigned_stable = False
        if metadata.channel == "stable" and not signed:
            temporary_unsigned_stable = _temporary_unsigned_stable_allowed(
                metadata,
                source_url=source_url,
                version=version,
                installer_url=url,
                signed=signed,
                signature_publisher=publisher,
            )
            if not temporary_unsigned_stable:
                raise ValueError("stable_unsigned")

        mandatory = raw["mandatory"]
        if type(mandatory) is not bool:
            raise ValueError("invalid_mandatory_state")
        minimum_supported = _validate_version(
            raw["minimumSupportedVersion"], "invalid_minimum_supported_version"
        )
        minimum_updater = _validate_version(
            raw["minimumUpdaterVersion"], "invalid_minimum_updater_version"
        )
        notes = _validate_release_notes(raw["releaseNotes"])
        if raw["releaseNotesUrl"] is not None:
            raise ValueError("invalid_release_notes_url")
        rollout = _require_object(raw["rollout"], "invalid_rollout")
        _require_exact_fields(rollout, {"state"}, "unknown_rollout_field")
        rollout_state = rollout["state"]
        if rollout_state not in ROLLOUT_STATES:
            raise ValueError("unsupported_rollout_state")
        return UpdateManifest(
            schema_version=schema,
            product=PRODUCT,
            platform=metadata.platform,
            architecture=metadata.architecture,
            channel=metadata.channel,
            version=version,
            published_at=published,
            installer=InstallerSpec(
                url=url,
                size_bytes=size,
                sha256=digest,
                signed=signed,
                signature_publisher=publisher,
            ),
            mandatory=mandatory,
            minimum_supported_version=minimum_supported,
            minimum_updater_version=minimum_updater,
            release_notes=notes,
            release_notes_url=None,
            rollout_state=rollout_state,
            temporary_unsigned_stable=temporary_unsigned_stable,
        )
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc


def serialize_manifest(manifest: UpdateManifest) -> str:
    return json.dumps(manifest.as_dict(), ensure_ascii=False, separators=(",", ":"))


def _default_state() -> dict:
    return {
        "schemaVersion": 1,
        "preview_opt_in": None,
        "channelCaches": {},
        "etag": None,
        "lastModified": None,
        "cachedManifest": None,
        "lastCheckedAt": None,
        "notifiedVersion": None,
        "dismissedVersion": None,
        "dismissedUntil": None,
        "pending": None,
        "lastInstalledVersion": None,
        "lastError": None,
    }


class UpdateStateStore:
    """Small atomic, non-secret JSON state store."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else STATE_PATH
        self._lock = threading.RLock()

    def load(self) -> dict:
        with self._lock:
            try:
                raw = _load_json_strict(self.path.read_bytes(), maximum=256 * 1024)
                state = _require_object(raw, "invalid_state")
                if state.get("schemaVersion") != 1:
                    return _default_state()
                clean = _default_state()
                for key in clean:
                    if key in state:
                        clean[key] = state[key]
                return clean
            except (OSError, ValueError):
                return _default_state()

    def save(self, state: Mapping) -> None:
        with self._lock:
            payload = _default_state()
            for key in payload:
                if key in state:
                    payload[key] = state[key]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            try:
                with temp.open("w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

    def update(self, **changes) -> dict:
        with self._lock:
            state = self.load()
            state.update(changes)
            self.save(state)
            return state


_CHANNEL_CACHE_FIELDS = (
    "etag",
    "lastModified",
    "cachedManifest",
    "lastCheckedAt",
    "lastError",
)


class _ChannelStateStore:
    """Adapt one shared updater state file to one isolated channel cache."""

    def __init__(self, store: UpdateStateStore, channel: str):
        self._store = store
        self._channel = channel

    def load(self) -> dict:
        parent = self._store.load()
        clean = _default_state()
        caches = parent.get("channelCaches")
        entry = caches.get(self._channel) if isinstance(caches, dict) else None
        if isinstance(entry, dict):
            for key in _CHANNEL_CACHE_FIELDS:
                if key in entry:
                    clean[key] = entry[key]
            return clean

        # One-time compatibility with the pre-WOR-170 single-channel cache.
        cached = parent.get("cachedManifest")
        if isinstance(cached, dict) and cached.get("channel") == self._channel:
            for key in _CHANNEL_CACHE_FIELDS:
                clean[key] = parent.get(key)
        return clean

    def update(self, **changes) -> dict:
        with self._store._lock:
            parent = self._store.load()
            caches = parent.get("channelCaches")
            caches = dict(caches) if isinstance(caches, dict) else {}
            current = caches.get(self._channel)
            entry = dict(current) if isinstance(current, dict) else {}
            if not entry:
                cached = parent.get("cachedManifest")
                if isinstance(cached, dict) and cached.get("channel") == self._channel:
                    for key in _CHANNEL_CACHE_FIELDS:
                        entry[key] = parent.get(key)
            for key in _CHANNEL_CACHE_FIELDS:
                if key in changes:
                    entry[key] = changes[key]
            caches[self._channel] = entry
            parent["channelCaches"] = caches
            self._store.save(parent)
            clean = _default_state()
            clean.update(entry)
            return clean


def initialize_preview_opt_in(
    metadata: AppMetadata | None,
    store: UpdateStateStore,
) -> bool:
    """Resolve and persist the one-time Stable/Preview preference migration."""

    legacy_state_exists = store.path.is_file()
    state = store.load()
    current = state.get("preview_opt_in")
    if type(current) is bool:
        return current

    if current is not None or not legacy_state_exists:
        enabled = False
    else:
        cached = state.get("cachedManifest")
        cached_channel = cached.get("channel") if isinstance(cached, dict) else None
        legacy_channel = (
            cached_channel
            if cached_channel in ALLOWED_CHANNELS
            else metadata.channel if metadata is not None else None
        )
        enabled = legacy_channel == "internal-preview"
    store.update(preview_opt_in=enabled)
    return enabled


def set_preview_opt_in(store: UpdateStateStore, enabled: bool) -> None:
    """Persist an explicit preference without touching pending installations."""

    if type(enabled) is not bool:
        raise ValueError("invalid_preview_opt_in")
    with store._lock:
        state = store.load()
        state["preview_opt_in"] = enabled
        cached = state.get("cachedManifest")
        if (
            not enabled
            and isinstance(cached, dict)
            and cached.get("channel") == "internal-preview"
        ):
            state["cachedManifest"] = None
            state["notifiedVersion"] = None
            state["dismissedVersion"] = None
            state["dismissedUntil"] = None
        store.save(state)


def _jitter(delay: float, rng: Callable[[float, float], float]) -> float:
    spread = delay * JITTER_FRACTION
    return max(0.0, rng(delay - spread, delay + spread))


class UpdateScheduler:
    """Thread-safe single-flight and retry-delay state machine."""

    def __init__(self, rng: Callable[[float, float], float] = random.uniform):
        self._rng = rng
        self._lock = threading.Lock()
        self._in_flight = False
        self._failure_count = 0

    @property
    def in_flight(self) -> bool:
        with self._lock:
            return self._in_flight

    def first_delay(self) -> float:
        return _jitter(FIRST_CHECK_SECONDS, self._rng)

    def begin(self) -> bool:
        with self._lock:
            if self._in_flight:
                return False
            self._in_flight = True
            return True

    def complete(self, success: bool) -> float:
        with self._lock:
            self._in_flight = False
            if success:
                self._failure_count = 0
                delay = NORMAL_CHECK_SECONDS
            else:
                index = min(self._failure_count, len(RETRY_DELAYS_SECONDS))
                if index < len(RETRY_DELAYS_SECONDS):
                    delay = RETRY_DELAYS_SECONDS[index]
                else:
                    delay = NORMAL_CHECK_SECONDS
                self._failure_count += 1
            return _jitter(delay, self._rng)


def prepare_notification_state(
    store: UpdateStateStore,
    version: str,
    *,
    manual: bool,
    now: float | None = None,
) -> bool:
    timestamp = time.time() if now is None else float(now)
    state = store.load()
    if state.get("dismissedVersion") != version:
        state["dismissedVersion"] = None
        state["dismissedUntil"] = None
    dismissed_until = state.get("dismissedUntil")
    actively_dismissed = (
        state.get("dismissedVersion") == version
        and isinstance(dismissed_until, (int, float))
        and float(dismissed_until) > timestamp
    )
    if state.get("dismissedVersion") == version and not actively_dismissed:
        state["dismissedVersion"] = None
        state["dismissedUntil"] = None
    allowed = manual or not actively_dismissed
    if allowed and not manual and state.get("notifiedVersion") != version:
        state["notifiedVersion"] = version
    store.save(state)
    return allowed


def dismiss_update(
    store: UpdateStateStore, version: str, *, now: float | None = None
) -> None:
    timestamp = time.time() if now is None else float(now)
    state = store.load()
    state["dismissedVersion"] = version
    state["dismissedUntil"] = timestamp + DISMISS_SECONDS
    store.save(state)


class _HTTPSResponse:
    def __init__(self, connection: http.client.HTTPSConnection, response):
        self._connection = connection
        self._response = response
        self.status = int(response.status)
        self.headers = response.headers

    def read(self, amount: int = -1) -> bytes:
        return self._response.read(amount)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()


def _open_https(
    url: str,
    headers: Mapping[str, str],
    connect_timeout: float,
    read_timeout: float,
):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != ALLOWED_INSTALLER_HOST
        or parsed.query
        or parsed.fragment
    ):
        raise UpdaterError("untrusted_url")
    connection = http.client.HTTPSConnection(
        ALLOWED_INSTALLER_HOST,
        timeout=connect_timeout,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("GET", parsed.path, headers=dict(headers))
        response = connection.getresponse()
        if connection.sock is not None:
            connection.sock.settimeout(read_timeout)
        return _HTTPSResponse(connection, response)
    except Exception:
        connection.close()
        raise


def fetch_channel_manifest(
    metadata: AppMetadata,
    store: UpdateStateStore,
    *,
    open_response: Callable = _open_https,
) -> ManifestFetchResult:
    if metadata.channel not in CHANNEL_URLS:
        raise ManifestError("invalid_metadata_channel")
    state = store.load()
    channel_url = CHANNEL_URLS[metadata.channel]
    headers = {
        "Accept": "application/json",
        "User-Agent": f"OSIA-Updater/{metadata.version}",
        "Cache-Control": "no-cache",
    }
    if isinstance(state.get("etag"), str) and state["etag"]:
        headers["If-None-Match"] = state["etag"]
    if isinstance(state.get("lastModified"), str) and state["lastModified"]:
        headers["If-Modified-Since"] = state["lastModified"]
    try:
        with open_response(
            channel_url,
            headers,
            CONNECT_TIMEOUT_SECONDS,
            READ_TIMEOUT_SECONDS,
        ) as response:
            if response.status == 304:
                cached = state.get("cachedManifest")
                if not isinstance(cached, dict):
                    raise ManifestError("not_modified_without_cache")
                manifest = parse_manifest(
                    json.dumps(cached, ensure_ascii=False, allow_nan=False),
                    metadata,
                    source_url=channel_url,
                )
                store.update(lastCheckedAt=int(time.time()), lastError=None)
                return ManifestFetchResult("not_modified", manifest)
            if 300 <= response.status < 400:
                raise ManifestError("redirect_rejected")
            if response.status != 200:
                raise ManifestError("manifest_http_error")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(16 * 1024, MANIFEST_MAX_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MANIFEST_MAX_BYTES:
                    raise ManifestError("manifest_too_large")
            payload = b"".join(chunks)
            manifest = parse_manifest(payload, metadata, source_url=channel_url)
            etag = response.headers.get("ETag")
            modified = response.headers.get("Last-Modified")
            store.update(
                etag=etag if isinstance(etag, str) else None,
                lastModified=modified if isinstance(modified, str) else None,
                cachedManifest=manifest.as_dict(),
                lastCheckedAt=int(time.time()),
                lastError=None,
            )
            return ManifestFetchResult("available", manifest)
    except UpdaterError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise ManifestError("offline") from exc


def fetch_eligible_manifest(
    metadata: AppMetadata,
    store: UpdateStateStore,
    *,
    preview_opt_in: bool,
    open_response: Callable = _open_https,
) -> ManifestFetchResult:
    """Fetch eligible channels independently and select the newest release."""

    if type(preview_opt_in) is not bool:
        raise ManifestError("invalid_preview_opt_in")
    channels = (
        ("stable", "internal-preview")
        if preview_opt_in
        else ("stable",)
    )
    results: dict[str, ManifestFetchResult] = {}
    errors: dict[str, ManifestError] = {}
    statuses: list[tuple[str, str]] = []

    for channel in channels:
        channel_metadata = replace(metadata, channel=channel)
        try:
            result = fetch_channel_manifest(
                channel_metadata,
                _ChannelStateStore(store, channel),
                open_response=open_response,
            )
            results[channel] = result
            statuses.append((channel, result.status))
        except ManifestError as exc:
            errors[channel] = exc
            statuses.append((channel, exc.code))

    candidates = [
        result.manifest
        for result in results.values()
        if result.manifest is not None
        and result.manifest.rollout_state == "enabled"
    ]
    if candidates:
        selected = candidates[0]
        for candidate in candidates[1:]:
            if compare_versions(candidate.version, selected.version) > 0:
                selected = candidate
        store.update(
            cachedManifest=selected.as_dict(),
            lastCheckedAt=int(time.time()),
            lastError=None,
        )
        return ManifestFetchResult("available", selected, tuple(statuses))

    stable_error = errors.get("stable")
    if (
        stable_error is not None
        and stable_error.code not in {"equal_version", "downgrade"}
    ):
        raise stable_error

    stable_result = results.get("stable")
    store.update(
        cachedManifest=None,
        lastCheckedAt=int(time.time()),
        lastError=None,
    )
    if (
        stable_result is not None
        and stable_result.manifest is not None
        and stable_result.manifest.rollout_state == "paused"
    ):
        return ManifestFetchResult("paused", stable_result.manifest, tuple(statuses))
    return ManifestFetchResult("no_update", None, tuple(statuses))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def installer_path(version: str, updates_dir: Path | None = None) -> Path:
    _validate_version(version)
    root = Path(updates_dir) if updates_dir is not None else UPDATES_DIR
    return root / f"{version}-OSIA-Setup.exe"


def partial_path(version: str, updates_dir: Path | None = None) -> Path:
    _validate_version(version)
    root = Path(updates_dir) if updates_dir is not None else UPDATES_DIR
    return root / f"{version}.part"


def cleanup_updates(
    updates_dir: Path | None = None,
    *,
    preserve_versions: tuple[str, ...] = (),
    now: float | None = None,
) -> None:
    root = Path(updates_dir) if updates_dir is not None else UPDATES_DIR
    if not root.is_dir():
        return
    timestamp = time.time() if now is None else float(now)
    preserve = {installer_path(version, root).name for version in preserve_versions}
    for path in root.iterdir():
        if not path.is_file():
            continue
        try:
            age = timestamp - path.stat().st_mtime
            if path.suffix == ".part" and age > PART_MAX_AGE_SECONDS:
                path.unlink()
            elif (
                path.name.endswith("-OSIA-Setup.exe")
                and path.name not in preserve
                and age > INSTALLER_MAX_AGE_SECONDS
            ):
                path.unlink()
        except OSError:
            continue


def _require_installable_channel(manifest: UpdateManifest) -> None:
    if manifest.channel == "stable":
        # Temporary pre-WOR-220 authorization is stamped only by parse_manifest
        # after the exact official stable source and release shape are checked.
        if (
            manifest.temporary_unsigned_stable
            and not manifest.installer.signed
            and manifest.installer.signature_publisher is None
        ):
            return
        # Other stable releases remain closed until WOR-220 implements
        # in-process Authenticode verification. A manifest assertion alone is
        # not trust.
        raise DownloadError("stable_authenticode_unavailable")
    if manifest.channel != "internal-preview":
        raise DownloadError("unsupported_channel")


def download_installer(
    manifest: UpdateManifest,
    *,
    updates_dir: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    open_response: Callable = _open_https,
) -> Path:
    _require_installable_channel(manifest)
    root = Path(updates_dir) if updates_dir is not None else UPDATES_DIR
    root.mkdir(parents=True, exist_ok=True)
    cleanup_updates(root, preserve_versions=(manifest.version,))
    final = installer_path(manifest.version, root)
    partial = partial_path(manifest.version, root)
    if final.is_file():
        if (
            final.stat().st_size == manifest.installer.size_bytes
            and _hash_file(final) == manifest.installer.sha256
        ):
            if progress:
                progress(manifest.installer.size_bytes, manifest.installer.size_bytes)
            return final
        final.unlink(missing_ok=True)
    partial.unlink(missing_ok=True)
    free = shutil.disk_usage(root).free
    if free < manifest.installer.size_bytes:
        raise DownloadError("insufficient_disk_space")

    digest = hashlib.sha256()
    received = 0
    try:
        with open_response(
            manifest.installer.url,
            {
                "Accept": "application/octet-stream",
                "User-Agent": f"OSIA-Updater/{manifest.version}",
                "Accept-Encoding": "identity",
            },
            CONNECT_TIMEOUT_SECONDS,
            READ_TIMEOUT_SECONDS,
        ) as response:
            if 300 <= response.status < 400:
                raise DownloadError("redirect_rejected")
            if response.status != 200:
                raise DownloadError("download_http_error")
            length = response.headers.get("Content-Length")
            if (
                not isinstance(length, str)
                or len(length) > 10
                or re.fullmatch(r"[0-9]+", length) is None
            ):
                raise DownloadError("content_length_required")
            if int(length) != manifest.installer.size_bytes:
                raise DownloadError("content_length_mismatch")
            encoding = response.headers.get("Content-Encoding")
            if encoding not in (None, "", "identity"):
                raise DownloadError("content_encoding_rejected")
            with partial.open("xb") as handle:
                while True:
                    if cancel_requested is not None and cancel_requested():
                        raise DownloadCancelled("download_cancelled")
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > manifest.installer.size_bytes:
                        raise DownloadError("byte_count_mismatch")
                    handle.write(chunk)
                    digest.update(chunk)
                    if progress:
                        progress(received, manifest.installer.size_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        if received != manifest.installer.size_bytes:
            raise DownloadError("byte_count_mismatch")
        if digest.hexdigest() != manifest.installer.sha256:
            raise DownloadError("sha256_mismatch")
        os.replace(partial, final)
        return final
    except UpdaterError:
        partial.unlink(missing_ok=True)
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        partial.unlink(missing_ok=True)
        raise DownloadError("download_interrupted") from exc


def prepare_installer_plan(manifest: UpdateManifest, path: Path) -> InstallerPlan:
    _require_installable_channel(manifest)
    candidate = Path(path)
    expected = installer_path(manifest.version, candidate.parent)
    if candidate.name != expected.name or not candidate.is_file():
        raise DownloadError("installer_missing")
    try:
        size = candidate.stat().st_size
        digest = _hash_file(candidate)
    except OSError as exc:
        raise DownloadError("installer_unreadable") from exc
    if size != manifest.installer.size_bytes or digest != manifest.installer.sha256:
        candidate.unlink(missing_ok=True)
        raise DownloadError("installer_reverification_failed")
    return InstallerPlan(candidate.resolve(), INSTALLER_ARGUMENTS)


def _pending_payload(manifest: UpdateManifest, plan: InstallerPlan) -> dict:
    return {
        "version": manifest.version,
        "path": str(plan.executable),
        "sha256": manifest.installer.sha256,
        "sizeBytes": manifest.installer.size_bytes,
        "status": "launching",
        "launchedAt": int(time.time()),
    }


def launch_update_installer(
    manifest: UpdateManifest,
    path: Path,
    store: UpdateStateStore,
    *,
    process_factory: Callable = subprocess.Popen,
):
    plan = prepare_installer_plan(manifest, path)
    state = store.load()
    state["pending"] = _pending_payload(manifest, plan)
    state["lastError"] = None
    store.save(state)
    kwargs: dict = {
        "cwd": str(plan.executable.parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "shell": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    try:
        process = process_factory(plan.command, **kwargs)
    except Exception as exc:
        state = store.load()
        state["pending"] = None
        state["lastError"] = "installer_launch_failed"
        store.save(state)
        raise DownloadError("installer_launch_failed") from exc
    return process


def reconcile_pending_update(
    metadata: AppMetadata,
    store: UpdateStateStore,
) -> str | None:
    state = store.load()
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return None
    target = pending.get("version")
    if not isinstance(target, str):
        state["pending"] = None
        state["lastError"] = "invalid_pending_state"
        store.save(state)
        return "failed"
    if metadata.version == target:
        state["pending"] = None
        state["lastInstalledVersion"] = target
        state["notifiedVersion"] = None
        state["dismissedVersion"] = None
        state["dismissedUntil"] = None
        state["cachedManifest"] = None
        state["etag"] = None
        state["lastModified"] = None
        state["lastError"] = None
        store.save(state)
        return "success"
    state["pending"] = None
    state["lastError"] = "update_version_mismatch"
    store.save(state)
    return "failed"


def safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, UpdaterError):
        return exc.code
    return "unexpected_updater_error"


__all__ = [
    "ALLOWED_CHANNELS",
    "AppMetadata",
    "CHANNEL_URLS",
    "DISMISS_SECONDS",
    "DownloadCancelled",
    "DownloadError",
    "INSTALLER_ARGUMENTS",
    "INSTALLER_MAX_BYTES",
    "InstallerPlan",
    "InstallerSpec",
    "LEGACY_UNSIGNED_STABLE_BRIDGE",
    "MANIFEST_MAX_BYTES",
    "ManifestError",
    "ManifestFetchResult",
    "MetadataError",
    "UpdateManifest",
    "TEMPORARY_UNSIGNED_STABLE_POLICY",
    "UpdateScheduler",
    "UpdateStateStore",
    "cleanup_updates",
    "compare_versions",
    "dismiss_update",
    "download_installer",
    "fetch_channel_manifest",
    "fetch_eligible_manifest",
    "initialize_preview_opt_in",
    "installer_path",
    "launch_update_installer",
    "load_app_metadata",
    "parse_app_metadata",
    "parse_manifest",
    "partial_path",
    "prepare_installer_plan",
    "prepare_notification_state",
    "reconcile_pending_update",
    "safe_error_code",
    "serialize_manifest",
    "set_preview_opt_in",
    "updater_enabled",
]
