"""Local, evidence-backed execution learning for canonical OSIA Chat.

Learning records only structured projections of terminal ToolAttempt and
ActionTruth data. It never executes tools, selects an approach, changes tool
availability, or creates/activates Skills. User corrections enter through an
explicit structured API; natural-language turns are not mined for corrections.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from osia.config import CONFIG_DIR


SCHEMA_VERSION = 1
MAX_OBSERVATIONS = 10_000
MAX_CONTEXT_PATTERNS = 3
MAX_CONTEXT_CHARS = 1_200
CONFIDENCE_HALF_LIFE_SECONDS = 90 * 24 * 60 * 60
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}$",
    re.IGNORECASE,
)
_APP = re.compile(r"^[a-z0-9][a-z0-9 ._+-]{0,95}$", re.IGNORECASE)
_OUTCOMES = frozenset({
    "success_verified", "failure_verified", "outcome_unknown",
    "user_correction",
})
_TOOL_FAMILIES = frozenset({
    "browser", "desktop", "system", "file", "api_mcp", "generic",
})
_OPERATION_FAMILIES = frozenset({
    "search", "navigation", "interaction", "input", "transfer", "read",
    "write", "delete", "execute", "other",
})
_CORRECTION_CODES = frozenset({
    "wrong_application", "wrong_domain", "wrong_operation",
    "approach_did_not_work", "result_was_incorrect", "other",
})
_TERMINAL_STATUSES = frozenset({
    "succeeded", "failed_before_effect", "failed_after_effect",
    "unknown_after_start", "cancelled_proven",
})
_NON_LEARNABLE_TOOLS = frozenset({
    "save_memory", "create_scheduled_task", "update_scheduled_task",
    "delete_scheduled_task",
})
_PROTECTED_FAILURE_PREFIXES = (
    "permission_", "policy_", "safety_", "confirmation_",
    "authorization_", "access_denied", "user_denied", "command_blocked",
)
_ATTEMPT_SCAN_CACHE: dict[str, float] = {}
_ATTEMPT_SCAN_CACHE_LOCK = threading.Lock()


class LearningStoreError(ValueError):
    """Stored learning data is unreadable or does not match its schema."""


def _safe_token(value: Any, *, field: str, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    if not _SAFE_TOKEN.fullmatch(text):
        raise LearningStoreError(f"invalid {field}")
    return text


def _safe_scope_label(value: Any, *, field: str) -> str:
    text = str(value or "").strip().casefold()
    if not text or len(text) > 253:
        raise LearningStoreError(f"invalid {field}")
    return text


def normalize_scope(scope: Mapping[str, Any]) -> dict[str, str]:
    """Validate the small set of factual scope dimensions retained here."""

    if not isinstance(scope, Mapping):
        raise LearningStoreError("scope must be an object")
    allowed = {"platform", "application", "domain", "workspace_ref"}
    if set(scope) - allowed:
        raise LearningStoreError("unsupported scope dimension")
    result: dict[str, str] = {}
    if scope.get("platform"):
        platform = _safe_scope_label(scope["platform"], field="platform")
        if not re.fullmatch(r"[a-z0-9_.-]{1,32}", platform):
            raise LearningStoreError("invalid platform")
        result["platform"] = platform
    if scope.get("application"):
        application = _safe_scope_label(scope["application"], field="application")
        if not _APP.fullmatch(application) or "/" in application or "\\" in application:
            raise LearningStoreError("invalid application")
        result["application"] = application
    if scope.get("domain"):
        domain = _safe_scope_label(scope["domain"], field="domain").rstrip(".")
        if not _DOMAIN.fullmatch(domain):
            raise LearningStoreError("invalid domain")
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            pass
        else:
            raise LearningStoreError("IP addresses are not learning domains")
        result["domain"] = domain
    if scope.get("workspace_ref"):
        workspace_ref = _safe_scope_label(
            scope["workspace_ref"], field="workspace_ref",
        )
        if not re.fullmatch(r"(?:(?:hmac|sha)256:)?[a-f0-9]{64}", workspace_ref):
            raise LearningStoreError("workspace reference must be opaque")
        result["workspace_ref"] = workspace_ref
    if not result:
        raise LearningStoreError("scope must contain a factual dimension")
    return result


def _normal_tool_name(value: Any) -> str:
    return _safe_token(value, field="approach").casefold()


def _operation_family(tool_name: str) -> str:
    name = tool_name.casefold().replace("-", "_")
    words = set(name.split("_"))
    if name in {"bash", "run_command"} or words & {"execute", "command", "shell"}:
        return "execute"
    if words & {"search", "find", "lookup", "query"}:
        return "search"
    if words & {"navigate", "navigation", "open", "launch", "browse"}:
        return "navigation"
    if words & {"click", "press", "select", "scroll", "activate"}:
        return "interaction"
    if words & {"type", "fill", "input", "enter"}:
        return "input"
    if words & {"download", "upload", "transfer", "attach"}:
        return "transfer"
    if words & {"delete", "remove", "trash"}:
        return "delete"
    if words & {"write", "create", "edit", "update", "save"}:
        return "write"
    if words & {"read", "get", "list", "inspect", "observe", "screenshot"}:
        return "read"
    return "other"


def _request_operation_family(user_text: str) -> str:
    text = str(user_text or "").casefold()
    phrases = (
        ("search", r"\b(search|find|look up|lookup|rechercher|chercher)\b"),
        ("navigation", r"\b(open|navigate|go to|browse|visit|ouvrir|aller sur)\b"),
        ("interaction", r"\b(click|press|select|scroll|cliquer|sélectionner)\b"),
        ("input", r"\b(type|fill|enter|saisir|remplir|taper)\b"),
        ("transfer", r"\b(download|upload|attach|télécharger|joindre)\b"),
        ("delete", r"\b(delete|remove|supprimer|effacer)\b"),
        ("write", r"\b(create|write|edit|update|save|créer|écrire|modifier)\b"),
        ("execute", r"\b(run|execute|command|script|exécuter|lancer)\b"),
        ("read", r"\b(read|inspect|summari[sz]e|review|lire|résumer)\b"),
    )
    for family, pattern in phrases:
        if re.search(pattern, text):
            return family
    return "other"


def _family_for_attempt(tool_name: str, target: Mapping[str, Any]) -> str:
    kind = str(target.get("kind") or "").casefold()
    if kind in _TOOL_FAMILIES:
        return kind
    name = tool_name.casefold()
    if name.startswith(("browser_", "web_")):
        return "browser"
    if name.startswith(("desktop_", "computer_")):
        return "desktop"
    if name in {"bash", "run_command"}:
        return "system"
    if name.startswith(("file_", "read_file", "write_file")):
        return "file"
    return "generic"


def _domain_from_safe_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 2_048:
        return ""
    try:
        parsed = urlsplit(text if "://" in text else "https://" + text)
        host = str(parsed.hostname or "").encode("idna").decode("ascii").casefold()
        return normalize_scope({"domain": host})["domain"] if host else ""
    except (UnicodeError, ValueError, LearningStoreError):
        return ""


def observation_from_tool_attempt(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project one terminal canonical ToolAttempt and its ActionTruth safely."""

    if not isinstance(record, Mapping):
        return None
    if str(record.get("owner_scope") or "") != "chat":
        return None
    tool_name = str(record.get("tool_name") or "").casefold()
    if not tool_name or tool_name in _NON_LEARNABLE_TOOLS:
        return None
    status = str(record.get("status") or "")
    if status not in _TERMINAL_STATUSES:
        return None
    truth = record.get("action_truth")
    if not isinstance(truth, Mapping) or not truth.get("action_id"):
        return None
    terminal = str(record.get("terminal_outcome") or "")
    effect_truth = str(truth.get("effect_truth") or "")
    truth_state = str(truth.get("current_state") or "")
    if (
        status == "succeeded"
        and terminal in {"success_verified", "confirmed_success"}
        and effect_truth == "verified"
        and truth_state == "effect_verified"
    ):
        outcome = "success_verified"
    elif (
        status in {"failed_before_effect", "failed_after_effect"}
        and terminal in {"confirmed_failure_before_effect", "failed_after_effect"}
    ):
        reason = str(record.get("reason_code") or "").casefold()
        if (
            status == "failed_before_effect"
            and reason.startswith(_PROTECTED_FAILURE_PREFIXES)
        ):
            return None
        outcome = "failure_verified"
    elif (
        status == "unknown_after_start"
        and terminal == "outcome_unknown"
    ):
        outcome = "outcome_unknown"
    else:
        return None

    target = truth.get("target")
    target = target if isinstance(target, Mapping) else {}
    family = _family_for_attempt(tool_name, target)
    operation = _operation_family(tool_name)
    scope: dict[str, str] = {"platform": sys.platform}
    if family == "browser":
        domain = _domain_from_safe_url(target.get("safe_url"))
        if domain:
            scope["domain"] = domain
    elif family == "desktop":
        application = str(target.get("application") or "").strip()
        if application:
            try:
                scope["application"] = normalize_scope(
                    {"application": application},
                )["application"]
            except LearningStoreError:
                pass
    elif family == "system" and target.get("provider_resource_ref"):
        workspace_ref = str(target.get("provider_resource_ref") or "")
        try:
            scope["workspace_ref"] = normalize_scope(
                {"workspace_ref": workspace_ref},
            )["workspace_ref"]
        except LearningStoreError:
            pass
    try:
        safe_scope = normalize_scope(scope)
        evidence_ref = _safe_token(
            record.get("tool_attempt_id"), field="tool_attempt_id",
        )
        action_id = _safe_token(truth.get("action_id"), field="action_id")
        semantic_effect_id = _safe_token(
            truth.get("semantic_effect_id"),
            field="semantic_effect_id",
            allow_empty=True,
        )
        approach = _normal_tool_name(tool_name)
        occurred_at = float(record.get("completed_at") or record.get("updated_at") or 0)
        if not math.isfinite(occurred_at) or occurred_at <= 0:
            return None
        latency_ms = None
        try:
            started_at = float(record.get("started_at") or 0)
            elapsed_ms = (occurred_at - started_at) * 1_000
            if started_at > 0 and 0 <= elapsed_ms <= 7 * 24 * 60 * 60 * 1_000:
                latency_ms = round(elapsed_ms)
        except (TypeError, ValueError, OverflowError):
            pass
        observation_id = "obs_" + hashlib.sha256(
            evidence_ref.encode("utf-8")
        ).hexdigest()[:32]
        return {
            "observation_id": observation_id,
            "kind": "tool_attempt",
            "evidence_ref": evidence_ref,
            "action_id": action_id,
            "semantic_effect_id": semantic_effect_id,
            "truth_state": _safe_token(truth_state, field="truth_state"),
            "effect_truth": _safe_token(effect_truth, field="effect_truth"),
            "attempt_status": _safe_token(status, field="attempt_status"),
            "terminal_outcome": _safe_token(terminal, field="terminal_outcome"),
            "scope": safe_scope,
            "tool_family": family,
            "operation_family": operation,
            "approach": approach,
            "outcome": outcome,
            "occurred_at": occurred_at,
            "latency_ms": latency_ms,
            "correction_code": "",
        }
    except (LearningStoreError, TypeError, ValueError, OverflowError):
        return None


def _validate_observation(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise LearningStoreError("observation must be an object")
    kind = str(raw.get("kind") or "")
    if kind not in {"tool_attempt", "user_correction"}:
        raise LearningStoreError("invalid observation kind")
    observation_id = _safe_token(
        raw.get("observation_id"), field="observation_id",
    )
    evidence_ref = _safe_token(raw.get("evidence_ref"), field="evidence_ref")
    scope = normalize_scope(raw.get("scope") or {})
    tool_family = str(raw.get("tool_family") or "")
    operation_family = str(raw.get("operation_family") or "")
    if tool_family not in _TOOL_FAMILIES:
        raise LearningStoreError("invalid tool family")
    if operation_family not in _OPERATION_FAMILIES:
        raise LearningStoreError("invalid operation family")
    approach = _normal_tool_name(raw.get("approach"))
    outcome = str(raw.get("outcome") or "")
    if outcome not in _OUTCOMES:
        raise LearningStoreError("invalid outcome")
    occurred_at = float(raw.get("occurred_at") or 0)
    if not math.isfinite(occurred_at) or occurred_at <= 0:
        raise LearningStoreError("invalid observation time")
    latency = raw.get("latency_ms")
    if latency is not None:
        if isinstance(latency, bool) or not isinstance(latency, int) or latency < 0:
            raise LearningStoreError("invalid latency")
    correction_code = str(raw.get("correction_code") or "")
    if kind == "tool_attempt":
        if outcome not in {
            "success_verified", "failure_verified", "outcome_unknown",
        }:
            raise LearningStoreError("invalid tool-attempt outcome")
        action_id = _safe_token(raw.get("action_id"), field="action_id")
        semantic_effect_id = _safe_token(
            raw.get("semantic_effect_id"),
            field="semantic_effect_id",
            allow_empty=True,
        )
        status = _safe_token(raw.get("attempt_status"), field="attempt_status")
        terminal = _safe_token(raw.get("terminal_outcome"), field="terminal_outcome")
        truth_state = _safe_token(raw.get("truth_state"), field="truth_state")
        effect_truth = _safe_token(raw.get("effect_truth"), field="effect_truth")
        if correction_code:
            raise LearningStoreError("tool attempt cannot contain a correction code")
    else:
        if outcome != "user_correction":
            raise LearningStoreError("invalid user-correction outcome")
        if str(raw.get("stance") or "") != "contradicts":
            raise LearningStoreError("unsupported correction stance")
        correction_code = _safe_token(
            raw.get("correction_code"), field="correction_code",
        )
        if correction_code not in _CORRECTION_CODES:
            raise LearningStoreError("invalid correction code")
        action_id = semantic_effect_id = status = terminal = ""
        truth_state = effect_truth = ""
    return {
        "observation_id": observation_id,
        "kind": kind,
        "evidence_ref": evidence_ref,
        "action_id": action_id,
        "semantic_effect_id": semantic_effect_id,
        "truth_state": truth_state,
        "effect_truth": effect_truth,
        "attempt_status": status,
        "terminal_outcome": terminal,
        "scope": scope,
        "tool_family": tool_family,
        "operation_family": operation_family,
        "approach": approach,
        "outcome": outcome,
        "occurred_at": occurred_at,
        "latency_ms": latency,
        "correction_code": correction_code,
        "stance": "contradicts" if kind == "user_correction" else "",
    }


def _empty_snapshot() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": time.time(),
        "enabled": True,
        "observations": [],
        "ignored_patterns": [],
    }


class LearningStore:
    """Separate atomic local store for execution-learning metadata."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self._secure_default_directory = path is None
        self.path = (
            Path(path)
            if path is not None else
            Path(CONFIG_DIR) / "learning" / "learning.json"
        )
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self._thread_lock = threading.RLock()

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._secure_default_directory and sys.platform != "win32":
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        lock_path = self.path.with_name(self.path.name + ".lock")
        with self._thread_lock, lock_path.open("a+b") as lock_file:
            if sys.platform != "win32":
                try:
                    os.chmod(lock_path, 0o600)
                except OSError:
                    pass
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

    def _read_path_unlocked(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LearningStoreError("learning state is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise LearningStoreError("unsupported learning schema")
        if not isinstance(raw.get("enabled", True), bool):
            raise LearningStoreError("invalid learning enabled state")
        observations = raw.get("observations", [])
        ignored = raw.get("ignored_patterns", [])
        if not isinstance(observations, list) or not isinstance(ignored, list):
            raise LearningStoreError("invalid learning collections")
        validated = [_validate_observation(item) for item in observations]
        identifiers = [item["observation_id"] for item in validated]
        if len(identifiers) != len(set(identifiers)):
            raise LearningStoreError("duplicate learning observation")
        ignored_patterns = sorted({
            _safe_token(value, field="ignored pattern") for value in ignored
        })
        updated_at = float(raw.get("updated_at") or 0)
        if not math.isfinite(updated_at) or updated_at <= 0:
            raise LearningStoreError("invalid learning update time")
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": updated_at,
            "enabled": raw.get("enabled", True),
            "observations": validated[-MAX_OBSERVATIONS:],
            "ignored_patterns": ignored_patterns,
        }

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_snapshot()
        try:
            return self._read_path_unlocked(self.path)
        except LearningStoreError as live_error:
            if not self.backup_path.exists():
                raise live_error
            try:
                snapshot = self._read_path_unlocked(self.backup_path)
            except LearningStoreError:
                raise live_error
            self._write_unlocked(snapshot)
            return snapshot

    def _write_atomic_path_unlocked(self, path: Path, snapshot: dict[str, Any]) -> None:
        temp_path = path.parent / (
            f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        )
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            if sys.platform != "win32":
                try:
                    os.chmod(temp_path, 0o600)
                except OSError:
                    pass
            os.replace(temp_path, path)
            if sys.platform != "win32":
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_unlocked(self, snapshot: Mapping[str, Any]) -> None:
        clean = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": float(snapshot["updated_at"]),
            "enabled": bool(snapshot.get("enabled", True)),
            "observations": [
                _validate_observation(item)
                for item in snapshot.get("observations", [])
            ][-MAX_OBSERVATIONS:],
            "ignored_patterns": sorted({
                _safe_token(value, field="ignored pattern")
                for value in snapshot.get("ignored_patterns", [])
            }),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic_path_unlocked(self.path, clean)
        self._write_atomic_path_unlocked(self.backup_path, clean)

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            return self._load_unlocked()

    def add_observations(self, observations: Iterable[Mapping[str, Any]]) -> int:
        clean_items = [_validate_observation(item) for item in observations]
        if not clean_items:
            return 0
        with self._locked():
            snapshot = self._load_unlocked()
            if not snapshot["enabled"]:
                return 0
            known = {
                item["observation_id"] for item in snapshot["observations"]
            }
            added = [
                item for item in clean_items
                if item["observation_id"] not in known
            ]
            if not added:
                return 0
            snapshot["observations"] = (
                snapshot["observations"] + added
            )[-MAX_OBSERVATIONS:]
            snapshot["updated_at"] = time.time()
            self._write_unlocked(snapshot)
            return len(added)

    def record_user_correction(
        self,
        *,
        scope: Mapping[str, Any],
        tool_family: str,
        operation_family: str,
        approach: str,
        evidence_ref: str,
        correction_code: str,
        occurred_at: float | None = None,
    ) -> str:
        """Record an explicitly submitted, structured user correction.

        This accepts identifiers and a bounded correction category only; it
        deliberately does not accept or retain the original user message.
        """

        evidence_ref = _safe_token(evidence_ref, field="correction evidence")
        correction_code = _safe_token(
            correction_code, field="correction code",
        )
        if correction_code not in _CORRECTION_CODES:
            raise LearningStoreError("invalid correction code")
        normalized_scope = normalize_scope(scope)
        normalized_scope.setdefault("platform", sys.platform)
        normalized_scope = normalize_scope(normalized_scope)
        if tool_family not in _TOOL_FAMILIES:
            raise LearningStoreError("invalid tool family")
        if operation_family not in _OPERATION_FAMILIES:
            raise LearningStoreError("invalid operation family")
        approach = _normal_tool_name(approach)
        timestamp = float(occurred_at if occurred_at is not None else time.time())
        fingerprint = json.dumps([
            normalized_scope, tool_family, operation_family,
            approach, evidence_ref, correction_code,
        ], sort_keys=True, separators=(",", ":"))
        observation_id = "corr_" + hashlib.sha256(
            fingerprint.encode("utf-8")
        ).hexdigest()[:32]
        observation = {
            "observation_id": observation_id,
            "kind": "user_correction",
            "evidence_ref": evidence_ref,
            "action_id": "",
            "semantic_effect_id": "",
            "truth_state": "",
            "effect_truth": "",
            "attempt_status": "",
            "terminal_outcome": "",
            "scope": normalized_scope,
            "tool_family": tool_family,
            "operation_family": operation_family,
            "approach": approach,
            "outcome": "user_correction",
            "occurred_at": timestamp,
            "latency_ms": None,
            "correction_code": correction_code,
            "stance": "contradicts",
        }
        self.add_observations([observation])
        return observation_id

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean")
        with self._locked():
            snapshot = self._load_unlocked()
            snapshot["enabled"] = enabled
            snapshot["updated_at"] = time.time()
            self._write_unlocked(snapshot)

    def ignore_pattern(self, pattern_id: str, *, ignored: bool = True) -> None:
        pattern_id = _safe_token(pattern_id, field="pattern id")
        with self._locked():
            snapshot = self._load_unlocked()
            known = {item["pattern_id"] for item in _patterns(snapshot["observations"])}
            if pattern_id not in known:
                raise KeyError("learning pattern not found")
            ignored_set = set(snapshot["ignored_patterns"])
            if ignored:
                ignored_set.add(pattern_id)
            else:
                ignored_set.discard(pattern_id)
            snapshot["ignored_patterns"] = sorted(ignored_set)
            snapshot["updated_at"] = time.time()
            self._write_unlocked(snapshot)

    def delete_scope(self, scope: Mapping[str, Any]) -> int:
        normalized = normalize_scope(scope)
        with self._locked():
            snapshot = self._load_unlocked()
            before = len(snapshot["observations"])
            snapshot["observations"] = [
                item for item in snapshot["observations"]
                if not all(item["scope"].get(key) == value
                           for key, value in normalized.items())
            ]
            removed = before - len(snapshot["observations"])
            if removed:
                current_ids = {
                    item["pattern_id"] for item in _patterns(snapshot["observations"])
                }
                snapshot["ignored_patterns"] = [
                    value for value in snapshot["ignored_patterns"]
                    if value in current_ids
                ]
                snapshot["updated_at"] = time.time()
                self._write_unlocked(snapshot)
            return removed

    def reset(self) -> None:
        with self._locked():
            snapshot = self._load_unlocked()
            snapshot["observations"] = []
            snapshot["ignored_patterns"] = []
            snapshot["updated_at"] = time.time()
            self._write_unlocked(snapshot)

    def inspect(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        return {
            "schema_version": SCHEMA_VERSION,
            "enabled": snapshot["enabled"],
            "updated_at": snapshot["updated_at"],
            "observation_count": len(snapshot["observations"]),
            "patterns": [
                {**pattern, "ignored": pattern["pattern_id"] in snapshot["ignored_patterns"]}
                for pattern in _patterns(snapshot["observations"])
            ],
            "observations": snapshot["observations"],
        }

    def context_for_request(
        self, user_text: str, *, now: float | None = None,
    ) -> str:
        snapshot = self.snapshot()
        if not snapshot["enabled"]:
            return ""
        family = _request_operation_family(user_text)
        if family == "other":
            return ""
        patterns = _patterns(snapshot["observations"], now=now)
        domains = _request_domains(user_text)
        candidates = []
        ignored = set(snapshot["ignored_patterns"])
        for pattern in patterns:
            if pattern["pattern_id"] in ignored:
                continue
            if pattern["operation_family"] != family:
                continue
            if pattern["scope"].get("platform") != sys.platform:
                continue
            has_exact_scope = False
            app = pattern["scope"].get("application")
            if app and _request_names_application(user_text, app):
                has_exact_scope = True
            domain = pattern["scope"].get("domain")
            if domain and domain in domains:
                has_exact_scope = True
            workspace = pattern["scope"].get("workspace_ref")
            if workspace and workspace in str(user_text or ""):
                has_exact_scope = True
            if not has_exact_scope:
                continue
            # Two verified outcomes are needed before a tool pattern is
            # projected as a pattern. A single explicit correction remains
            # inspectable and may be surfaced only in its exact named scope.
            if pattern["verified_outcome_count"] < 2 and not pattern["user_correction_count"]:
                continue
            candidates.append(pattern)
        candidates.sort(
            key=lambda item: (
                item["user_correction_count"] > 0,
                item["verified_outcome_count"],
                item["last_updated_at"],
            ),
            reverse=True,
        )
        if not candidates:
            return ""
        header = "Historical execution evidence matched explicit scope and operation details in this request:"
        final_note = (
            "These records describe prior outcomes; they do not select an action or change available tools."
        )
        lines = [header]
        for pattern in candidates[:MAX_CONTEXT_PATTERNS]:
            scope_text = ", ".join(
                f"{key}={value}" for key, value in sorted(pattern["scope"].items())
            )
            if not scope_text:
                continue
            refs = list(pattern["supporting_evidence_refs"][:1])
            refs += list(pattern["contradicting_evidence_refs"][:1])
            refs += list(pattern["unknown_evidence_refs"][:1])
            evidence_text = ", ".join(refs)
            line = (
                f"- {scope_text}; {pattern['tool_family']}/{pattern['operation_family']} "
                f"via {pattern['approach']}: {pattern['verified_success_count']} "
                f"verified successes, {pattern['verified_failure_count']} verified failures, "
                f"{pattern['outcome_unknown_count']} unknown outcomes, "
                f"{pattern['user_correction_count']} explicit user corrections; "
                f"descriptive confidence {pattern['confidence']:.2f}"
            )
            if evidence_text:
                line += f"; evidence {evidence_text}"
            candidate_lines = [*lines, line, final_note]
            if len("\n".join(candidate_lines)) > MAX_CONTEXT_CHARS:
                break
            lines.append(line)
        if len(lines) == 1:
            return ""
        return "\n".join([*lines, final_note])

    def propose_skill_candidate(self, pattern_id: str) -> dict[str, Any] | None:
        """Return a review-only candidate after repeated independent evidence."""

        snapshot = self.snapshot()
        patterns = _patterns(snapshot["observations"])
        selected = next((
            item for item in patterns if item["pattern_id"] == str(pattern_id)
        ), None)
        if selected is None:
            return None
        related = [
            item for item in patterns
            if item["tool_family"] == selected["tool_family"]
            and item["operation_family"] == selected["operation_family"]
            and item["approach"] == selected["approach"]
        ]
        if not snapshot["enabled"]:
            return None
        success_count = sum(item["verified_success_count"] for item in related)
        scope_keys = {json.dumps(item["scope"], sort_keys=True) for item in related}
        failure_count = sum(item["verified_failure_count"] for item in related)
        correction_count = sum(item["user_correction_count"] for item in related)
        refs = []
        for pattern in related:
            refs.extend(pattern["supporting_evidence_refs"])
        if success_count < 8 or len(scope_keys) < 3:
            return None
        related_scopes = {json.dumps(item["scope"], sort_keys=True) for item in related}
        weighted_success = weighted_failure = weighted_correction = 0.0
        now = time.time()
        for observation in snapshot["observations"]:
            if (
                observation["tool_family"] != selected["tool_family"]
                or observation["operation_family"] != selected["operation_family"]
                or observation["approach"] != selected["approach"]
                or json.dumps(observation["scope"], sort_keys=True) not in related_scopes
            ):
                continue
            weight = math.exp(-math.log(2) * max(
                0.0, now - observation["occurred_at"],
            ) / CONFIDENCE_HALF_LIFE_SECONDS)
            if observation["outcome"] == "success_verified":
                weighted_success += weight
            elif observation["outcome"] == "failure_verified":
                weighted_failure += weight
            elif observation["outcome"] == "user_correction":
                weighted_correction += weight
        confidence = (weighted_success + 1) / (
            weighted_success + weighted_failure + weighted_correction + 2
        )
        if confidence < 0.8:
            return None
        return {
            "candidate_id": "skill_candidate_" + selected["pattern_id"],
            "status": "review_required",
            "tool_family": selected["tool_family"],
            "operation_family": selected["operation_family"],
            "approach": selected["approach"],
            "scope_count": len(scope_keys),
            "evidence_scopes": sorted(
                (item["scope"] for item in related),
                key=lambda item: json.dumps(item, sort_keys=True),
            )[:64],
            "verified_success_count": success_count,
            "verified_failure_count": failure_count,
            "user_correction_count": correction_count,
            "confidence": round(confidence, 3),
            "evidence_refs": sorted(set(refs))[:64],
            "runtime_activation": False,
            "promotion_requires_explicit_review": True,
        }


def _pattern_identity(
    scope: Mapping[str, str], tool_family: str,
    operation_family: str, approach: str,
) -> str:
    material = json.dumps(
        [dict(scope), tool_family, operation_family, approach],
        sort_keys=True, separators=(",", ":"),
    )
    return "pattern_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _patterns(
    observations: Iterable[Mapping[str, Any]], *, now: float | None = None,
) -> list[dict[str, Any]]:
    current_time = float(now if now is not None else time.time())
    grouped: dict[str, dict[str, Any]] = {}
    for raw in observations:
        item = _validate_observation(raw)
        key = _pattern_identity(
            item["scope"], item["tool_family"],
            item["operation_family"], item["approach"],
        )
        group = grouped.setdefault(key, {
            "pattern_id": key,
            "scope": item["scope"],
            "tool_family": item["tool_family"],
            "operation_family": item["operation_family"],
            "approach": item["approach"],
            "verified_success_count": 0,
            "verified_failure_count": 0,
            "outcome_unknown_count": 0,
            "user_correction_count": 0,
            "supporting_evidence_refs": [],
            "contradicting_evidence_refs": [],
            "unknown_evidence_refs": [],
            "last_updated_at": 0.0,
            "latencies_ms": [],
            "weighted_success": 0.0,
            "weighted_failure": 0.0,
            "weighted_correction": 0.0,
        })
        age = max(0.0, current_time - float(item["occurred_at"]))
        weight = math.exp(-math.log(2) * age / CONFIDENCE_HALF_LIFE_SECONDS)
        evidence_ref = item["evidence_ref"]
        if item["outcome"] == "success_verified":
            group["verified_success_count"] += 1
            group["supporting_evidence_refs"].append(evidence_ref)
            group["weighted_success"] += weight
        elif item["outcome"] == "failure_verified":
            group["verified_failure_count"] += 1
            group["contradicting_evidence_refs"].append(evidence_ref)
            group["weighted_failure"] += weight
        elif item["outcome"] == "outcome_unknown":
            group["outcome_unknown_count"] += 1
            group["unknown_evidence_refs"].append(evidence_ref)
        else:
            group["user_correction_count"] += 1
            group["contradicting_evidence_refs"].append(evidence_ref)
            group["weighted_correction"] += weight
        group["last_updated_at"] = max(
            group["last_updated_at"], float(item["occurred_at"]),
        )
        if item["latency_ms"] is not None:
            group["latencies_ms"].append(item["latency_ms"])
    result = []
    for group in grouped.values():
        verified = (
            group["verified_success_count"] + group["verified_failure_count"]
        )
        evidence_mass = (
            group["weighted_success"] + group["weighted_failure"]
            + group["weighted_correction"]
        )
        group["verified_outcome_count"] = verified
        group["confidence"] = round(
            (group["weighted_success"] + 1.0) / (evidence_mass + 2.0), 3,
        )
        group["evidence_strength"] = round(evidence_mass, 3)
        latencies = sorted(group.pop("latencies_ms"))
        group["median_latency_ms"] = (
            latencies[len(latencies) // 2] if latencies else None
        )
        for field in (
            "supporting_evidence_refs", "contradicting_evidence_refs",
            "unknown_evidence_refs",
        ):
            group[field] = sorted(group[field])
        group.pop("weighted_success")
        group.pop("weighted_failure")
        group.pop("weighted_correction")
        result.append(group)
    return sorted(
        result,
        key=lambda item: (item["last_updated_at"], item["pattern_id"]),
        reverse=True,
    )


def _request_domains(user_text: str) -> set[str]:
    found = set()
    for candidate in re.findall(
        r"(?i)(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"[a-z]{2,63}",
        str(user_text or ""),
    ):
        try:
            domain = normalize_scope({"domain": candidate})["domain"]
        except LearningStoreError:
            continue
        found.add(domain)
    return found


def _request_names_application(user_text: str, application: str) -> bool:
    label = application.casefold().removesuffix(".exe")
    if not label:
        return False
    return bool(re.search(
        r"(?<![\w.])" + re.escape(label) + r"(?![\w.])",
        str(user_text or ""),
        flags=re.IGNORECASE,
    ))


class LearningEngine:
    """Best-effort projection of durable Chat ToolAttempts into local history."""

    def __init__(self, learning_store: LearningStore | None = None, attempt_store=None):
        self.learning_store = learning_store or LearningStore()
        self.attempt_store = attempt_store
        self._last_scan_time = 0.0

    def ingest_terminal_attempts(self) -> int:
        if not self.learning_store.snapshot()["enabled"]:
            return 0
        store = self.attempt_store
        if store is None:
            from osia.tool_attempts import get_tool_attempt_store

            store = get_tool_attempt_store()
        directory = getattr(store, "attempts_dir", None)
        if directory is None or not Path(directory).exists():
            return 0
        directory = Path(directory)
        try:
            cache_key = str(directory.resolve())
        except OSError:
            cache_key = str(directory.absolute())
        with _ATTEMPT_SCAN_CACHE_LOCK:
            cached_scan_time = _ATTEMPT_SCAN_CACHE.get(cache_key, 0.0)
        last_scan_time = max(self._last_scan_time, cached_scan_time)
        observations = []
        for path in directory.glob("attempt_*.json"):
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                continue
            if (
                last_scan_time > 0
                and modified_at < last_scan_time - 300
            ):
                continue
            attempt_id = path.stem
            if not _SAFE_TOKEN.fullmatch(attempt_id):
                continue
            try:
                record = store.get_attempt(attempt_id)
            except Exception:
                # A corrupt attempt remains a canonical execution-store issue;
                # learning skips it and never affects the active Chat turn.
                continue
            observation = observation_from_tool_attempt(record or {})
            if observation is not None:
                observations.append(observation)
        added = self.learning_store.add_observations(observations)
        self._last_scan_time = time.time()
        with _ATTEMPT_SCAN_CACHE_LOCK:
            _ATTEMPT_SCAN_CACHE[cache_key] = max(
                _ATTEMPT_SCAN_CACHE.get(cache_key, 0.0),
                self._last_scan_time,
            )
        return added

    def context_for_request(self, user_text: str) -> str:
        return self.learning_store.context_for_request(user_text)


def inspect_learning(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    return LearningStore(path).inspect()


def reset_learning(path: str | os.PathLike[str] | None = None) -> None:
    LearningStore(path).reset()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect local OSIA execution learning")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inspect")
    subparsers.add_parser("reset")
    subparsers.add_parser("enable")
    subparsers.add_parser("disable")
    delete_parser = subparsers.add_parser("delete-scope")
    delete_parser.add_argument("--scope", required=True, help="JSON scope object")
    ignore_parser = subparsers.add_parser("ignore")
    ignore_parser.add_argument("pattern_id")
    unignore_parser = subparsers.add_parser("unignore")
    unignore_parser.add_argument("pattern_id")
    candidate_parser = subparsers.add_parser("skill-candidate")
    candidate_parser.add_argument("pattern_id")
    correction_parser = subparsers.add_parser("record-correction")
    correction_parser.add_argument("--scope", required=True, help="JSON scope object")
    correction_parser.add_argument("--tool-family", required=True)
    correction_parser.add_argument("--operation-family", required=True)
    correction_parser.add_argument("--approach", required=True)
    correction_parser.add_argument("--evidence-ref", required=True)
    correction_parser.add_argument("--code", required=True, choices=sorted(_CORRECTION_CODES))
    args = parser.parse_args(argv)
    store = LearningStore()
    if args.command == "inspect":
        output: Any = store.inspect()
    elif args.command == "reset":
        store.reset()
        output = {"reset": True}
    elif args.command in {"enable", "disable"}:
        store.set_enabled(args.command == "enable")
        output = {"enabled": args.command == "enable"}
    elif args.command == "delete-scope":
        output = {"deleted_observations": store.delete_scope(json.loads(args.scope))}
    elif args.command in {"ignore", "unignore"}:
        store.ignore_pattern(args.pattern_id, ignored=args.command == "ignore")
        output = {"pattern_id": args.pattern_id, "ignored": args.command == "ignore"}
    elif args.command == "skill-candidate":
        output = store.propose_skill_candidate(args.pattern_id)
    elif args.command == "record-correction":
        observation_id = store.record_user_correction(
            scope=json.loads(args.scope),
            tool_family=args.tool_family,
            operation_family=args.operation_family,
            approach=args.approach,
            evidence_ref=args.evidence_ref,
            correction_code=args.code,
        )
        output = {"observation_id": observation_id}
    else:
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "LearningEngine", "LearningStore", "LearningStoreError",
    "inspect_learning", "normalize_scope", "observation_from_tool_attempt",
    "reset_learning",
]
