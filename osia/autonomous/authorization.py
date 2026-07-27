"""Task-scoped Autonomous authorization and durable dispatch barriers (WOR-194).

This module coordinates the existing permission facade/store for one Autonomous
run.  It does not decide whether a call is allowed: every real call is still
normalized and evaluated by :mod:`osia.action_policy` immediately before its
dispatcher.  Checkpoints retain only opaque references, safe summaries and
hashes; private requests live only in the current PC process until converted
into a protected grant.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping

from osia.autonomous import state


AUTHORIZATION_PROTOCOL_VERSION = 1
AUTHORIZATION_REQUEST_TTL_SECONDS = 15 * 60
DEFAULT_GRANT_TTL_SECONDS = 60 * 60
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ABS_PATH_RE = re.compile(
    r"(?:(?P<q>[\"'`])(?P<quoted>(?:[A-Za-z]:[\\/]|/)[^\"'`\r\n]+)(?P=q)"
    r"|(?P<bare>(?:[A-Za-z]:[\\/]|/)[^\s,;\r\n]+))"
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PRIVATE_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_FILENAME_RE = re.compile(r"(?<![\w.-])[\w -]{1,100}\.[A-Za-z0-9]{1,12}(?![\w.-])")


class AuthorizationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class _Runtime:
    task_state: dict
    save: Callable[[dict], Any]
    emit: Callable[[dict], Any] | None
    origin_ref: str


_CURRENT_RUNTIME: ContextVar[_Runtime | None] = ContextVar(
    "osia_autonomous_authorization_runtime", default=None,
)
_PRIVATE_LOCK = threading.RLock()
_PRIVATE_REQUESTS: dict[tuple[str, str], Any] = {}
_PRIVATE_DESCRIPTORS: dict[tuple[str, str], dict[str, Any]] = {}


def _clear_private_run(run_id: str, *, request_id: str | None = None) -> None:
    """Forget process-local private material once it can no longer be used."""

    run_ref = str(run_id or "")
    with _PRIVATE_LOCK:
        if request_id is None:
            for key in tuple(_PRIVATE_DESCRIPTORS):
                if key[0] == run_ref:
                    _PRIVATE_DESCRIPTORS.pop(key, None)
            for key in tuple(_PRIVATE_REQUESTS):
                if key[0] == run_ref:
                    _PRIVATE_REQUESTS.pop(key, None)
        else:
            _PRIVATE_REQUESTS.pop((run_ref, str(request_id)), None)


def _private_scope_display(request: Any) -> str:
    """Return a bounded PC-local scope detail without exposing it to mobile."""

    try:
        decoded = json.loads(request.private_scope.private_canonical)
    except (AttributeError, TypeError, ValueError):
        return ""
    values: list[str] = []

    def _collect(value: Any, key: str = "") -> None:
        if len(values) >= 4:
            return
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                _collect(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                _collect(child, key)
        elif isinstance(value, str) and key in {
            "path", "identity", "executable", "cwd",
        }:
            if value and value not in values:
                values.append(value)

    _collect(decoded)
    return ", ".join(values)[:320]


def _now_iso() -> str:
    return state._now_iso()


def _opaque(value: Any, code: str) -> str:
    candidate = str(value or "")
    if not _OPAQUE_RE.fullmatch(candidate):
        raise AuthorizationError(code)
    return candidate


def _revision_ref(value: Any, code: str) -> str:
    if type(value) is not int or value < 0:
        raise AuthorizationError(code)
    return str(value)


def _request_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _checkpoint(runtime: _Runtime) -> None:
    _safe_call(runtime.save, runtime.task_state)


def _emit(runtime: _Runtime, event: str) -> None:
    if runtime.emit is not None:
        _safe_call(runtime.emit, {"event": event, **pc_projection(runtime.task_state)})


@contextmanager
def run_authorization_scope(
    task_state: dict,
    *,
    save: Callable[[dict], Any] = state.save_task_state,
    emit: Callable[[dict], Any] | None = None,
    origin_ref: str = "desktop",
):
    """Bind one run to action-policy and dispatcher hooks for this turn."""

    runtime = _Runtime(task_state, save, emit, _opaque(origin_ref, "invalid_origin"))
    token = _CURRENT_RUNTIME.set(runtime)
    try:
        yield runtime
    finally:
        _CURRENT_RUNTIME.reset(token)


def current_runtime() -> _Runtime | None:
    return _CURRENT_RUNTIME.get()


def _task_position(st: dict) -> tuple[str, str]:
    cursor = st.get("cursor") or {}
    try:
        p = int(cursor.get("phase", 0) or 0)
        t = int(cursor.get("task", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return "phase", "task"
    phases = st.get("phases") or []
    phase_id = f"phase:{p}"
    task_id = f"task:{t}"
    if 0 <= p < len(phases) and isinstance(phases[p], dict):
        phase_id = f"phase:{p}"
        tasks = phases[p].get("tasks") or []
        if 0 <= t < len(tasks) and isinstance(tasks[t], dict):
            raw = str(tasks[t].get("id") or f"{p}.{t}")
            task_id = f"task:{raw}"
    return phase_id, task_id


def operation_id_for_call(st: dict, tool_identity: str, call_hash: str) -> str:
    """Stable across suspend/resume, but distinct across plan tasks/revisions."""

    phase_id, task_id = _task_position(st)
    material = "\0".join((
        str(st.get("run_id") or ""),
        str(st.get("plan_revision") or 0),
        phase_id,
        task_id,
        str(tool_identity or ""),
        str(call_hash or ""),
    )).encode("utf-8", errors="strict")
    return "op_" + hashlib.sha256(material).hexdigest()[:48]


def bind_request_execution_context(context, tool_identity: str, call_hash: str):
    """Rebind action-policy's generic context to the stable prepared operation."""

    runtime = current_runtime()
    if runtime is None:
        return context
    st = runtime.task_state
    operation_id = operation_id_for_call(st, tool_identity, call_hash)
    return replace(
        context,
        execution_type=context.execution_type.__class__.AUTONOMOUS,
        interactive=False,
        operation_id=operation_id,
        run_id=str(st.get("run_id") or context.run_id),
        plan_revision=_revision_ref(
            int(st.get("plan_revision", 0)), "invalid_plan_revision"
        ),
        task_revision=_revision_ref(
            int(st.get("authorization_revision", 0)),
            "invalid_authorization_revision",
        ),
    )


def _plan_text(st: dict) -> str:
    parts = [str(st.get("_runtime_objective", st.get("objective", "")) or "")]
    for phase in st.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        parts.append(str(phase.get("name") or ""))
        for task in phase.get("tasks") or []:
            if isinstance(task, dict):
                parts.append(str(task.get(
                    "_runtime_objective", task.get("objective", "")
                ) or ""))
                parts.append(str(task.get(
                    "_runtime_success_criterion", task.get("success_criterion", "")
                ) or ""))
    return "\n".join(parts)


def _absolute_paths(text: str) -> list[str]:
    values: list[str] = []
    for match in _ABS_PATH_RE.finditer(str(text or "")):
        raw = match.group("quoted") or match.group("bare") or ""
        candidate = raw.rstrip(".,;:!?)]}")
        if candidate and candidate not in values:
            values.append(candidate)
    return values[:32]


def _tool_for_path_text(text: str, paths: list[str]) -> tuple[str, dict] | None:
    low = str(text or "").lower()
    if not paths:
        return None
    if any(word in low for word in ("rename", "move ", "renomm", "déplac", "deplac")):
        if len(paths) >= 2:
            return "rename_file", {"old_path": paths[0], "new_path": paths[1]}
        return None
    if any(word in low for word in ("copy", "copier", "dupli")):
        if len(paths) >= 2:
            return "copy_file", {"src": paths[0], "dst": paths[1]}
        return None
    if any(word in low for word in ("delete", "remove", "supprim", "effac")):
        directory = any(word in low for word in (
            "directory", "folder", "tree", "dossier", "répertoire", "repertoire",
        ))
        if directory:
            return "delete_directory", {
                "path": paths[0],
                "recursive": any(word in low for word in (
                    "recursive", "recursively", "tree", "récurs", "recurs",
                )),
            }
        return "delete_file", {"path": paths[0]}
    if any(word in low for word in (
        "directory", "folder", "dossier", "répertoire", "repertoire",
    )) and any(word in low for word in ("create", "make", "créer", "creer")):
        return "create_directory", {"path": paths[0]}
    if any(word in low for word in (
        "write", "create", "save", "generate", "écri", "ecri", "créer",
        "creer", "enregistr", "génér", "gener",
    )):
        # Content is intentionally not authority.  The grant remains bound to
        # the exact normalized target scope selected by the user.
        return "write_file", {"path": paths[0], "content": ""}
    return None


def _unknown_capabilities(text: str) -> list[tuple[str, str, str]]:
    low = str(text or "").lower()
    candidates = (
        (("command", "shell", "powershell", "terminal"), "shell", "local_command"),
        (("gmail", "email", "e-mail", "courriel", "mail "), "gmail", "gmail_operation"),
        (("browser", "website", "web ", "navigateur", "site "), "browser", "browser_target"),
        (("publish", "submit", "send", "publier", "soumettre", "envoyer"), "external", "external_commit"),
        (("purchase", "payment", "buy", "acheter", "paiement", "payer"), "payments", "payment"),
        (("clipboard", "presse-papiers"), "clipboard", "clipboard"),
    )
    return [
        (category, target, "unknown")
        for words, category, target in candidates
        if any(word in low for word in words)
    ]


def _classification_for_descriptor(tool: str, arguments: dict):
    from osia.action_policy import trusted_deletion_classification

    if tool not in {"delete_file", "delete_directory"}:
        try:
            from osia.system_resources import classify_non_deletion_system_mutation

            evidence = classify_non_deletion_system_mutation(tool, arguments)
            if evidence is not None and evidence.blocks_destruction:
                return trusted_deletion_classification(system_destruction=True)
        except Exception:
            pass
        return trusted_deletion_classification()
    try:
        from osia.deletion_plan import DeletionPreflightKind, prepare_deletion_call

        preflight = prepare_deletion_call(tool, arguments)
        if preflight.kind is DeletionPreflightKind.PLANNED and preflight.plan is not None:
            return trusted_deletion_classification(
                system_destruction=preflight.plan.system_destruction,
                colossal_deletion=preflight.plan.colossal_deletion,
            )
    except Exception:
        # An unavailable inventory is never pre-authorized; the live call will
        # re-run the sealed deletion preflight and its universal barriers.
        return None
    return None


def derive_capability_proposal(st: dict) -> tuple[list[dict], dict[str, dict]]:
    """Derive advisory safe entries plus process-local exact descriptors."""

    from osia.action_policy import build_permission_request, make_execution_context
    from osia.tool_contracts import get_tool_spec

    text = _plan_text(st)
    paths = _absolute_paths(text)
    descriptors: dict[str, dict] = {}
    summary: list[dict] = []
    exact = _tool_for_path_text(text, paths)
    if exact is not None:
        tool, arguments = exact
        spec = get_tool_spec(tool)
        classification = _classification_for_descriptor(tool, arguments)
        non_delegable = bool(
            classification is not None
            and (classification.system_destruction or classification.colossal_deletion)
        )
        context = make_execution_context(
            execution_type=__import__(
                "osia.action_policy", fromlist=["ExecutionType"]
            ).ExecutionType.AUTONOMOUS,
            interactive=False,
            run_id=str(st.get("run_id") or ""),
            plan_revision=str(st.get("plan_revision") or 1),
            task_revision="1",
            operation_id="proposal_" + uuid.uuid4().hex,
        )
        request = build_permission_request(
            tool,
            arguments,
            execution_context=context,
            deletion_classification=(
                classification
                if classification is not None
                else __import__(
                    "osia.action_policy", fromlist=["TRUSTED_CLEAR_CLASSIFICATION"]
                ).TRUSTED_CLEAR_CLASSIFICATION
            ),
            force_sensitive=True,
        )
        capability_id = "cap_" + hashlib.sha256(
            f"{tool}\0{request.scope_hash}".encode("utf-8")
        ).hexdigest()[:32]
        known_and_grantable = bool(
            classification is not None
            and request.private_scope.grantable
            and not non_delegable
        )
        entry = {
            "capability_id": capability_id,
            "label": str(spec.capability if spec is not None else request.capability),
            "tool_category": request.tool_category,
            "effect_class": request.effect_class,
            "operation_class": tool,
            "target_category": request.safe_category,
            "target_scope_hash": request.scope_hash if known_and_grantable else "",
            "target_precision": "exact" if known_and_grantable else "unknown",
            "expected_use_count": 1,
            "grant_ttl_seconds": DEFAULT_GRANT_TTL_SECONDS,
            "ordinary_confirmation_required": True,
            "delegable": known_and_grantable,
            "requires_later_scope_extension": not known_and_grantable,
            "universal_barrier": (
                "system_destruction" if classification and classification.system_destruction
                else "colossal_deletion_pc_only"
                if classification and classification.colossal_deletion else ""
            ),
            "can_approve_mobile": False,
        }
        summary.append(entry)
        if known_and_grantable:
            descriptors[capability_id] = {
                "tool_identity": tool,
                "arguments": dict(arguments),
                "classification": classification,
                "expected_use_count": 1,
                "private_display": ", ".join(paths[:2]),
            }

    for category, target, precision in _unknown_capabilities(text):
        capability_id = "cap_" + hashlib.sha256(
            f"unknown\0{category}\0{target}".encode("utf-8")
        ).hexdigest()[:32]
        if any(item["capability_id"] == capability_id for item in summary):
            continue
        summary.append({
            "capability_id": capability_id,
            "label": f"{category}:{target}",
            "tool_category": category,
            "effect_class": "unknown_high_risk",
            "operation_class": target,
            "target_category": target,
            "target_scope_hash": "",
            "target_precision": precision,
            "expected_use_count": None,
            "grant_ttl_seconds": None,
            "ordinary_confirmation_required": True,
            "delegable": False,
            "requires_later_scope_extension": True,
            "universal_barrier": "",
            "can_approve_mobile": category not in {"shell", "gmail", "payments"},
        })

    if not summary:
        summary.append({
            "capability_id": "cap_read_only_plan",
            "label": "Read-only planning and inspection",
            "tool_category": "read_only",
            "effect_class": "read_only",
            "operation_class": "inspect",
            "target_category": "read_only",
            "target_scope_hash": "",
            "target_precision": "bounded",
            "expected_use_count": None,
            "grant_ttl_seconds": None,
            "ordinary_confirmation_required": False,
            "delegable": True,
            "requires_later_scope_extension": False,
            "universal_barrier": "",
            "can_approve_mobile": True,
        })
    return summary, descriptors


def _authorization_request(st: dict, *, origin_ref: str, policy_epoch: int) -> dict:
    now = time.time()
    return {
        "protocol_version": AUTHORIZATION_PROTOCOL_VERSION,
        "request_id": _request_id("auth"),
        "run_id": str(st.get("run_id") or ""),
        "plan_revision": int(st.get("plan_revision", 1)),
        "authorization_revision": int(st.get("authorization_revision", 0)),
        "expected_policy_epoch": int(policy_epoch),
        "origin_ref": _opaque(origin_ref, "invalid_origin"),
        "issued_at": now,
        "expires_at": now + AUTHORIZATION_REQUEST_TTL_SECONDS,
        "responded": False,
    }


def prepare_planned_state(
    st: dict,
    *,
    origin_ref: str = "desktop",
    policy=None,
) -> dict:
    """Persistable Plan -> review transition; never starts the orchestrator."""

    from osia.action_policy import PermissionMode, get_default_permission_policy

    active_policy = policy or get_default_permission_policy()
    permission_state = active_policy.store.load_state()
    summary, descriptors = derive_capability_proposal(st)
    request = _authorization_request(
        st, origin_ref=origin_ref, policy_epoch=permission_state.policy_epoch,
    )
    state.initialize_authorization_pending(
        st,
        summary,
        policy_epoch=permission_state.policy_epoch,
        request=request,
    )
    st["authorization_origin_ref"] = _opaque(origin_ref, "invalid_origin")
    st["_runtime_capability_descriptors"] = descriptors
    st["_runtime_pc_capability_details"] = {
        capability_id: descriptor.get("private_display", "")
        for capability_id, descriptor in descriptors.items()
    }
    with _PRIVATE_LOCK:
        for capability_id, descriptor in descriptors.items():
            _PRIVATE_DESCRIPTORS[(str(st["run_id"]), capability_id)] = descriptor
    if permission_state.permission_mode == PermissionMode.FULL_ACCESS:
        st["authorization_status"] = "not_required_full_access"
        st["authorization_source"] = "full_access"
        st["run_state"] = "ready"
        st["authorization_request"] = None
        _clear_private_run(str(st.get("run_id") or ""))
    return st


def restore_planned_runtime_descriptors(st: dict) -> bool:
    """Rebuild exact memory-only proposal data after a process restart."""

    if st.get("run_state") != "authorization_pending":
        return False
    run_id = str(st.get("run_id") or "")
    st.pop("_runtime_capability_descriptors", None)
    st.pop("_runtime_pc_capability_details", None)
    with _PRIVATE_LOCK:
        for key in tuple(_PRIVATE_DESCRIPTORS):
            if key[0] == run_id:
                _PRIVATE_DESCRIPTORS.pop(key, None)
    persisted = {
        str(item.get("capability_id") or ""): item
        for item in st.get("capability_summary") or []
        if isinstance(item, dict)
    }
    derived_summary, descriptors = derive_capability_proposal(st)
    derived = {
        str(item.get("capability_id") or ""): item
        for item in derived_summary
        if isinstance(item, dict)
    }
    if set(derived) != set(persisted):
        return False
    for capability_id, item in derived.items():
        sealed = persisted[capability_id]
        if (
            str(item.get("target_scope_hash") or "")
            != str(sealed.get("target_scope_hash") or "")
            or bool(item.get("delegable")) != bool(sealed.get("delegable"))
            or str(item.get("universal_barrier") or "")
            != str(sealed.get("universal_barrier") or "")
        ):
            return False
    st["_runtime_capability_descriptors"] = descriptors
    st["_runtime_pc_capability_details"] = {
        capability_id: descriptor.get("private_display", "")
        for capability_id, descriptor in descriptors.items()
    }
    with _PRIVATE_LOCK:
        for capability_id, descriptor in descriptors.items():
            _PRIVATE_DESCRIPTORS[(run_id, capability_id)] = descriptor
    return True


def inject_proposal_requests(st: dict, requests: Iterable[Any]) -> list[dict]:
    """Focused-test/integration hook for already normalized advisory requests."""

    summary: list[dict] = []
    descriptors: dict[str, dict] = {}
    for request in requests:
        capability_id = "cap_" + hashlib.sha256(
            f"{request.tool_identity}\0{request.scope_hash}".encode("utf-8")
        ).hexdigest()[:32]
        entry = {
            "capability_id": capability_id,
            "label": request.capability,
            "tool_category": request.tool_category,
            "effect_class": request.effect_class,
            "operation_class": request.tool_identity,
            "target_category": request.safe_category,
            "target_scope_hash": request.scope_hash if request.private_scope.grantable else "",
            "target_precision": "exact" if request.private_scope.grantable else "unknown",
            "expected_use_count": 1,
            "grant_ttl_seconds": DEFAULT_GRANT_TTL_SECONDS,
            "ordinary_confirmation_required": True,
            "delegable": bool(request.private_scope.grantable),
            "requires_later_scope_extension": not request.private_scope.grantable,
            "universal_barrier": (
                "system_destruction" if request.system_destruction
                else "colossal_deletion_pc_only" if request.colossal_deletion else ""
            ),
            "can_approve_mobile": request.safe_category not in {
                "local_path", "local_paths", "local_command",
                "gmail_operation", "connector_operation", "tool_operation",
            },
        }
        summary.append(entry)
        if entry["delegable"] and not entry["universal_barrier"]:
            descriptors[capability_id] = {"request": request, "expected_use_count": 1}
            with _PRIVATE_LOCK:
                _PRIVATE_DESCRIPTORS[(str(st.get("run_id") or ""), capability_id)] = (
                    descriptors[capability_id]
                )
    st["capability_summary"] = summary
    st["_runtime_capability_descriptors"] = descriptors
    return summary


def response_for_current_request(
    st: dict,
    *,
    action: str,
    origin_ref: str | None = None,
) -> dict:
    request = st.get("authorization_request") or st.get("pending_scope_extension")
    if not isinstance(request, dict):
        raise AuthorizationError("authorization_request_missing")
    return {
        "protocol_version": AUTHORIZATION_PROTOCOL_VERSION,
        "request_id": request.get("request_id"),
        "run_id": request.get("run_id"),
        "plan_revision": request.get("plan_revision"),
        "authorization_revision": request.get("authorization_revision"),
        "expected_policy_epoch": request.get("expected_policy_epoch"),
        "origin_ref": origin_ref or request.get("origin_ref"),
        "action": action,
    }


def _validate_response(
    st: dict,
    request: dict,
    response: Mapping[str, Any],
    *,
    origin_ref: str,
    current_policy_epoch: int,
) -> str:
    if not isinstance(response, Mapping):
        raise AuthorizationError("invalid_authorization_response")
    if response.get("protocol_version") != AUTHORIZATION_PROTOCOL_VERSION:
        raise AuthorizationError("wrong_protocol_version")
    request_id = _opaque(response.get("request_id"), "invalid_request_id")
    if request_id != request.get("request_id"):
        raise AuthorizationError("wrong_request_id")
    if request_id in (st.get("handled_authorization_requests") or []):
        raise AuthorizationError("duplicate_response")
    if response.get("run_id") != st.get("run_id") or request.get("run_id") != st.get("run_id"):
        raise AuthorizationError("wrong_run_id")
    if response.get("plan_revision") != st.get("plan_revision") or request.get(
        "plan_revision"
    ) != st.get("plan_revision"):
        raise AuthorizationError("wrong_plan_revision")
    if response.get("authorization_revision") != st.get(
        "authorization_revision"
    ) or request.get("authorization_revision") != st.get("authorization_revision"):
        raise AuthorizationError("wrong_authorization_revision")
    if response.get("expected_policy_epoch") != current_policy_epoch or request.get(
        "expected_policy_epoch"
    ) != current_policy_epoch:
        raise AuthorizationError("wrong_policy_epoch")
    expected_origin = _opaque(request.get("origin_ref"), "invalid_request_origin")
    if _opaque(origin_ref, "invalid_origin") != expected_origin or response.get(
        "origin_ref"
    ) != expected_origin:
        raise AuthorizationError("wrong_origin")
    if request.get("responded") is True:
        raise AuthorizationError("duplicate_response")
    try:
        expires_at = float(request.get("expires_at"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuthorizationError("invalid_request_expiration") from exc
    if expires_at <= time.time():
        raise AuthorizationError("expired_request")
    if st.get("run_state") in {"completed", "failed", "cancelled"}:
        raise AuthorizationError("run_terminal")
    return request_id


def _descriptor_request(
    st: dict,
    capability_id: str,
    descriptor: dict,
    *,
    authorization_revision: int,
    policy,
):
    from osia.action_policy import (
        ExecutionContext,
        ExecutionType,
        build_permission_request,
        make_execution_context,
    )

    existing = descriptor.get("request")
    if existing is not None:
        old = existing.execution_context
        context = ExecutionContext(
            execution_id=old.execution_id,
            execution_type=ExecutionType.AUTONOMOUS,
            interactive=False,
            owner_ref=old.owner_ref,
            machine_ref=old.machine_ref,
            device_ref=old.device_ref,
            run_id=str(st.get("run_id") or ""),
            scheduled_task_id="",
            task_revision=str(authorization_revision),
            plan_revision=str(st.get("plan_revision") or 0),
            operation_id=old.operation_id,
            policy_epoch=int(st.get("policy_epoch", 0)),
        )
        return replace(existing, execution_context=context)
    context = make_execution_context(
        execution_type=ExecutionType.AUTONOMOUS,
        interactive=False,
        run_id=str(st.get("run_id") or ""),
        task_revision=str(authorization_revision),
        plan_revision=str(st.get("plan_revision") or 0),
        policy_epoch=int(st.get("policy_epoch", 0)),
        operation_id="grant_" + capability_id,
        store=policy.store,
    )
    return build_permission_request(
        descriptor["tool_identity"],
        descriptor["arguments"],
        execution_context=context,
        deletion_classification=descriptor["classification"],
        force_sensitive=True,
        policy=policy,
    )


def authorize_and_start(
    st: dict,
    response: Mapping[str, Any],
    *,
    selected_capability_ids: Iterable[str] | None = None,
    origin_ref: str = "desktop",
    policy=None,
) -> list[str]:
    """Create exact protected grants, then transition to ``ready``."""

    from osia.action_policy import (
        DecisionType,
        PermissionMode,
        UsePolicy,
        create_grant,
        get_default_permission_policy,
    )

    active_policy = policy or get_default_permission_policy()
    permission_state = active_policy.store.load_state()
    if permission_state.permission_mode == PermissionMode.FULL_ACCESS:
        st["policy_epoch"] = permission_state.policy_epoch
        st["authorization_status"] = "not_required_full_access"
        st["authorization_source"] = "full_access"
        st["run_state"] = "ready"
        st["authorization_request"] = None
        return []
    request = st.get("authorization_request")
    if not isinstance(request, dict) or st.get("run_state") != "authorization_pending":
        raise AuthorizationError("authorization_request_missing")
    request_id = _validate_response(
        st,
        request,
        response,
        origin_ref=origin_ref,
        current_policy_epoch=permission_state.policy_epoch,
    )
    action = str(response.get("action") or "")
    if action not in {"approve", "deny", "cancel"}:
        raise AuthorizationError("invalid_authorization_action")
    if action == "deny":
        request["responded"] = True
        st.setdefault("handled_authorization_requests", []).append(request_id)
        state.append_log(st, "authorization_denied", ref=request_id)
        _clear_private_run(str(st.get("run_id") or ""))
        return []
    if action == "cancel":
        request["responded"] = True
        st.setdefault("handled_authorization_requests", []).append(request_id)
        st["status"] = "aborted"
        st["run_state"] = "cancelled"
        st["authorization_status"] = "revoked"
        state.append_log(st, "authorization_cancelled", ref=request_id)
        _clear_private_run(str(st.get("run_id") or ""))
        return []

    selected = set(
        selected_capability_ids
        if selected_capability_ids is not None
        else (
            item.get("capability_id")
            for item in st.get("capability_summary") or []
            if isinstance(item, dict) and item.get("delegable")
        )
    )
    known_ids = {
        item.get("capability_id") for item in st.get("capability_summary") or []
        if isinstance(item, dict)
    }
    if not selected.issubset(known_ids):
        raise AuthorizationError("unknown_capability_selection")
    summaries = {
        item.get("capability_id"): item
        for item in st.get("capability_summary") or []
        if isinstance(item, dict)
    }
    if origin_ref != "desktop" and any(
        summaries.get(capability_id, {}).get("can_approve_mobile") is not True
        for capability_id in selected
    ):
        raise AuthorizationError("local_pc_review_required")
    next_revision = int(st.get("authorization_revision", 0)) + 1
    grants = []
    try:
        for capability_id in sorted(selected):
            with _PRIVATE_LOCK:
                descriptor = _PRIVATE_DESCRIPTORS.get(
                    (str(st["run_id"]), capability_id)
                )
            if descriptor is None:
                continue
            permission_request = _descriptor_request(
                st,
                capability_id,
                descriptor,
                authorization_revision=next_revision,
                policy=active_policy,
            )
            # A proposal remains advisory. Re-evaluate deterministic universal
            # barriers immediately before any grant creation.
            decision = active_policy.evaluate(permission_request)
            if decision.decision_type in {
                DecisionType.BLOCK_SYSTEM_DESTRUCTION,
                DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION,
            }:
                continue
            if not permission_request.private_scope.grantable:
                continue
            expected = max(1, int(descriptor.get("expected_use_count", 1) or 1))
            grant = create_grant(
                permission_request,
                use_policy=UsePolicy.BOUNDED,
                max_uses=expected,
                expires_at=time.time() + DEFAULT_GRANT_TTL_SECONDS,
                persistent=bool(active_policy.store.secure_available),
                constraints={"scope_only": True},
            )
            active_policy.store.add_grant(grant)
            grants.append(grant)
    except Exception:
        # Never leave a partially-created set of run grants usable after a
        # protected-store error.
        rollback = active_policy.store.revoke_run_grants(
            str(st.get("run_id") or "")
        )
        st["policy_epoch"] = rollback.policy_epoch
        request["expected_policy_epoch"] = rollback.policy_epoch
        raise

    request["responded"] = True
    st.setdefault("handled_authorization_requests", []).append(request_id)

    st["authorization_revision"] = next_revision
    st["policy_epoch"] = permission_state.policy_epoch
    st["grant_refs"] = [grant.grant_id for grant in grants]
    st["authorization_status"] = "authorized"
    st["authorization_source"] = "task_scoped"
    st["authorization_origin_ref"] = _opaque(origin_ref, "invalid_origin")
    st["run_state"] = "ready"
    st["authorization_request"] = None
    state.append_log(st, "authorized", ref=str(next_revision), note=f"{len(grants)} grant(s)")
    _clear_private_run(str(st.get("run_id") or ""))
    return list(st["grant_refs"])


def _base_operation_record(request, decision) -> dict:
    runtime = current_runtime()
    st = runtime.task_state if runtime is not None else {}
    phase_id, task_id = _task_position(st)
    return {
        "operation_id": request.operation_id,
        "task_id": task_id,
        "phase_id": phase_id,
        "plan_revision": int(st.get("plan_revision", 0) or 0),
        "tool_identity": request.tool_identity,
        "normalized_call_hash": request.normalized_call_hash,
        "private_scope_hash": request.scope_hash,
        "grant_ref": decision.grant_ref,
        "authorization_source": decision.authorization_source.value,
        "policy_epoch": decision.policy_epoch,
        "status": "prepared",
        "dispatch_attempt_count": 0,
        "started_at": None,
        "completed_at": None,
        "execution_outcome": "not_started",
        "safe_reason_code": decision.reason_code,
    }


def request_block_reason(request) -> str | None:
    """Durable state barriers consulted by the central policy facade."""

    runtime = current_runtime()
    if runtime is None:
        return None
    st = runtime.task_state
    if state.equivalent_ambiguity_barrier(
        st,
        tool_identity=request.tool_identity,
        normalized_call_hash=request.normalized_call_hash,
        private_scope_hash=request.scope_hash,
    ) is not None:
        return "duplicate_outcome_unknown"
    completed = None
    if request.effect_class != "read_only":
        completed = state.equivalent_operation_record(
            st,
            tool_identity=request.tool_identity,
            normalized_call_hash=request.normalized_call_hash,
            private_scope_hash=request.scope_hash,
            statuses={"success_verified"},
        )
    if completed is not None:
        return "duplicate_success_verified"
    run_state = st.get("run_state")
    if run_state == "suspended_authorization_revoked":
        return "authorization_revoked"
    if run_state == "blocked_outcome_unknown":
        return "outcome_unknown_blocked"
    if run_state == "authorization_pending":
        return "authorization_pending"
    return None


def suspend_authorization_unavailable(reason_code: str) -> None:
    """Turn an internal authorization failure into a durable no-dispatch stop."""

    runtime = current_runtime()
    if runtime is None:
        return
    st = runtime.task_state
    st["run_state"] = "suspended_authorization_revoked"
    st["authorization_status"] = "revoked"
    st["revocation"] = {
        "revoked": True,
        "revoked_at": _now_iso(),
        "reason_code": str(reason_code or "authorization_unavailable"),
    }
    _checkpoint(runtime)
    _emit(runtime, "autonomous_authorization_revoked")


def observe_permission_decision(request, decision) -> None:
    """Checkpoint prepared/authorized/suspended decisions; never changes them."""

    runtime = current_runtime()
    if runtime is None:
        return
    from osia.action_policy import DecisionType

    st = runtime.task_state
    record = state.operation_record(st, request.operation_id) or _base_operation_record(
        request, decision
    )
    if record.get("status") in {
        "success_verified", "partial_effect_verified", "outcome_unknown",
    }:
        # Evaluation of an equivalent later call cannot rewrite the durable
        # outcome that is the basis of the no-replay decision.
        _checkpoint(runtime)
        return
    record.update({
        "grant_ref": decision.grant_ref,
        "authorization_source": decision.authorization_source.value,
        "policy_epoch": decision.policy_epoch,
        "safe_reason_code": decision.reason_code,
    })
    if decision.decision_type == DecisionType.ALLOW:
        record["status"] = "authorized"
        state.upsert_operation_record(st, record)
        _checkpoint(runtime)
        return
    if decision.decision_type == DecisionType.SUSPEND_SCOPE_EXTENSION:
        record["status"] = "suspended_scope_extension"
        state.upsert_operation_record(st, record)
        _suspend_for_scope_extension(runtime, request, record)
        return
    if decision.decision_type in {
        DecisionType.DENY,
        DecisionType.BLOCK_SYSTEM_DESTRUCTION,
    }:
        record["status"] = "blocked_authorization"
        state.upsert_operation_record(st, record)
        if decision.reason_code in {"policy_epoch_mismatch", "authorization_revoked"}:
            st["run_state"] = "suspended_authorization_revoked"
            st["authorization_status"] = "revoked"
            st["policy_epoch"] = decision.policy_epoch
            _emit(runtime, "autonomous_authorization_revoked")
        _checkpoint(runtime)


def _suspend_for_scope_extension(runtime: _Runtime, request, record: dict) -> None:
    st = runtime.task_state
    existing = st.get("pending_scope_extension")
    if isinstance(existing, dict) and existing.get("operation_id") == request.operation_id:
        _checkpoint(runtime)
        return
    request_id = _request_id("scope")
    now = time.time()
    local_only = request.safe_category in {
        "local_path", "local_paths", "local_command", "gmail_operation",
        "connector_operation", "tool_operation",
    }
    pending = {
        "protocol_version": AUTHORIZATION_PROTOCOL_VERSION,
        "request_id": request_id,
        "run_id": str(st.get("run_id") or ""),
        "plan_revision": int(st.get("plan_revision", 0)),
        "authorization_revision": int(st.get("authorization_revision", 0)),
        "expected_policy_epoch": int(record.get("policy_epoch", 0)),
        "origin_ref": runtime.origin_ref,
        "issued_at": now,
        "expires_at": now + AUTHORIZATION_REQUEST_TTL_SECONDS,
        "responded": False,
        "operation_id": request.operation_id,
        "task_label": record.get("task_id", "task"),
        "safe_category": request.safe_category,
        "effect_class": request.effect_class,
        "target_type": request.safe_category,
        "requested_use_count": 1,
        "local_pc_review_required": local_only,
        "can_approve_pc": bool(request.private_scope.grantable),
        "can_approve_mobile": bool(request.private_scope.grantable and not local_only),
        "universal_barrier": (
            "system_destruction" if request.system_destruction
            else "colossal_deletion_pc_only" if request.colossal_deletion else ""
        ),
    }
    st["pending_scope_extension"] = pending
    st["run_state"] = "suspended_scope_extension"
    st["authorization_status"] = "scope_extension_required"
    with _PRIVATE_LOCK:
        _PRIVATE_REQUESTS[(str(st.get("run_id") or ""), request_id)] = request
    _checkpoint(runtime)
    _emit(runtime, "autonomous_scope_extension_request")


def on_permit_consumed(request, permit) -> None:
    """Durably mark DISPATCHED immediately after exact permit consumption."""

    runtime = current_runtime()
    if runtime is None:
        return
    st = runtime.task_state
    record = state.operation_record(st, request.operation_id) or {
        **_base_operation_record(request, type("D", (), {
            "grant_ref": permit.grant_ref,
            "authorization_source": permit.authorization_source,
            "policy_epoch": permit.policy_epoch,
            "reason_code": "permit_consumed",
        })()),
    }
    record.update({
        "status": "dispatched",
        "dispatch_attempt_count": int(record.get("dispatch_attempt_count", 0) or 0) + 1,
        "started_at": _now_iso(),
        "execution_outcome": "dispatched",
        "safe_reason_code": "permit_consumed",
        "grant_ref": permit.grant_ref,
        "authorization_source": permit.authorization_source.value,
        "policy_epoch": permit.policy_epoch,
    })
    state.upsert_operation_record(st, record)
    _checkpoint(runtime)


def record_tool_execution(tool_identity: str, arguments: Mapping[str, Any], execution_record) -> None:
    """Translate the typed dispatcher outcome into the durable operation log."""

    runtime = current_runtime()
    if runtime is None:
        return
    from osia.tool_contracts import ExecutionOutcome, normalized_call_hash

    st = runtime.task_state
    call_hash = normalized_call_hash(tool_identity, arguments)
    operation_id = operation_id_for_call(st, tool_identity, call_hash)
    record = state.operation_record(st, operation_id)
    if record is None:
        return
    if (
        record.get("status") in {
            "success_verified", "partial_effect_verified", "outcome_unknown",
        }
        and not bool(getattr(execution_record, "dispatched", False))
    ):
        # A later rejected duplicate must never erase a durable real outcome.
        _checkpoint(runtime)
        return
    if (
        record.get("status") in {"suspended_scope_extension", "blocked_authorization"}
        and not bool(getattr(execution_record, "dispatched", False))
    ):
        # The central facade already checkpointed the typed zero-dispatch
        # suspension.  A wrapper's REJECTED record must not erase it.
        _checkpoint(runtime)
        return
    if (
        execution_record.outcome.value == "rejected_before_execution"
        and not bool(getattr(execution_record, "dispatched", False))
        and record.get("status") == "authorized"
    ):
        try:
            from osia.action_policy import get_default_permission_policy

            current_epoch = (
                get_default_permission_policy().store.load_state().policy_epoch
            )
        except Exception:
            current_epoch = -1
        if current_epoch != int(st.get("policy_epoch", -2)):
            record.update({
                "status": "blocked_authorization",
                "completed_at": _now_iso(),
                "execution_outcome": "rejected_before_execution",
                "safe_reason_code": "policy_epoch_changed_before_dispatch",
            })
            state.upsert_operation_record(st, record)
            st["run_state"] = "suspended_authorization_revoked"
            st["authorization_status"] = "revoked"
            st["policy_epoch"] = current_epoch
            _checkpoint(runtime)
            _emit(runtime, "autonomous_authorization_revoked")
            return
    outcome = execution_record.outcome
    status = {
        ExecutionOutcome.SUCCESS_VERIFIED: "success_verified",
        # A dispatched side effect that the contract cannot verify is not safe
        # to replay merely because its immediate return lacked an error.
        ExecutionOutcome.SUCCESS_UNVERIFIED: "outcome_unknown",
        ExecutionOutcome.CANCELLED_PROVEN: "cancelled_proven",
        ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT: "failed_before_dispatch",
        ExecutionOutcome.REJECTED_BEFORE_EXECUTION: "failed_before_dispatch",
        ExecutionOutcome.OUTCOME_UNKNOWN: "outcome_unknown",
    }.get(outcome, "failed_before_dispatch")
    record.update({
        "status": status,
        "completed_at": _now_iso(),
        "execution_outcome": outcome.value,
        "safe_reason_code": (
            "outcome_unknown" if outcome is ExecutionOutcome.OUTCOME_UNKNOWN
            else status
        ),
    })
    state.upsert_operation_record(st, record)
    if outcome in {
        ExecutionOutcome.OUTCOME_UNKNOWN,
        ExecutionOutcome.SUCCESS_UNVERIFIED,
    }:
        state.add_ambiguity_barrier(st, record)
        st["run_state"] = "blocked_outcome_unknown"
        st["authorization_status"] = "outcome_unknown"
        _checkpoint(runtime)
        _emit(runtime, "autonomous_outcome_unknown")
        return
    _checkpoint(runtime)


def mark_partial_effect(st: dict, operation_id: str, *, save=state.save_task_state) -> None:
    record = state.operation_record(st, operation_id)
    if record is None:
        raise AuthorizationError("operation_missing")
    record.update({
        "status": "partial_effect_verified",
        "completed_at": _now_iso(),
        "execution_outcome": "partial_effect_verified",
        "safe_reason_code": "partial_effect_verified",
    })
    state.upsert_operation_record(st, record)
    state.add_ambiguity_barrier(st, record)
    st["run_state"] = "blocked_outcome_unknown"
    st["authorization_status"] = "outcome_unknown"
    _safe_call(save, st)


def approve_scope_extension(
    st: dict,
    response: Mapping[str, Any],
    *,
    origin_ref: str,
    policy=None,
) -> str | None:
    """Approve/deny one exact prepared operation without regenerating the plan."""

    from osia.action_policy import (
        DecisionType,
        UsePolicy,
        create_grant,
        get_default_permission_policy,
    )

    active_policy = policy or get_default_permission_policy()
    permission_state = active_policy.store.load_state()
    pending = st.get("pending_scope_extension")
    if not isinstance(pending, dict) or st.get("run_state") != "suspended_scope_extension":
        raise AuthorizationError("scope_extension_missing")
    request_id = _validate_response(
        st,
        pending,
        response,
        origin_ref=origin_ref,
        current_policy_epoch=permission_state.policy_epoch,
    )
    action = str(response.get("action") or "")
    if action not in {"approve_exact", "deny", "cancel"}:
        raise AuthorizationError("invalid_scope_extension_action")
    if origin_ref != "desktop" and (
        pending.get("local_pc_review_required")
        or pending.get("can_approve_mobile") is not True
        or bool(pending.get("universal_barrier"))
    ):
        raise AuthorizationError("local_pc_review_required")
    if action == "deny":
        pending["responded"] = True
        st.setdefault("handled_authorization_requests", []).append(request_id)
        state.append_log(st, "scope_extension_denied", ref=request_id)
        _clear_private_run(
            str(st.get("run_id") or ""), request_id=request_id,
        )
        return None
    if action == "cancel":
        pending["responded"] = True
        st.setdefault("handled_authorization_requests", []).append(request_id)
        st["status"] = "aborted"
        st["run_state"] = "cancelled"
        st["authorization_status"] = "revoked"
        state.append_log(st, "scope_extension_cancelled", ref=request_id)
        _clear_private_run(
            str(st.get("run_id") or ""), request_id=request_id,
        )
        return None
    with _PRIVATE_LOCK:
        original = _PRIVATE_REQUESTS.get((str(st.get("run_id") or ""), request_id))
    if original is None:
        raise AuthorizationError("private_scope_unavailable")
    next_revision = int(st.get("authorization_revision", 0)) + 1
    old_context = original.execution_context
    context = replace(
        old_context,
        task_revision=str(next_revision),
        plan_revision=str(st.get("plan_revision") or 0),
        policy_epoch=permission_state.policy_epoch,
    )
    permission_request = replace(original, execution_context=context)
    decision = active_policy.evaluate(permission_request)
    if decision.decision_type in {
        DecisionType.BLOCK_SYSTEM_DESTRUCTION,
        DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION,
    }:
        raise AuthorizationError("universal_barrier")
    if not permission_request.private_scope.grantable:
        raise AuthorizationError("scope_not_grantable")
    grant = create_grant(
        permission_request,
        use_policy=UsePolicy.SINGLE,
        expires_at=min(
            float(pending["expires_at"]), time.time() + DEFAULT_GRANT_TTL_SECONDS
        ),
        persistent=bool(active_policy.store.secure_available),
        constraints={"scope_only": True},
    )
    active_policy.store.add_grant(grant)
    pending["responded"] = True
    st.setdefault("handled_authorization_requests", []).append(request_id)
    st["authorization_revision"] = next_revision
    st["policy_epoch"] = permission_state.policy_epoch
    st.setdefault("grant_refs", []).append(grant.grant_id)
    st["pending_scope_extension"] = None
    st["authorization_status"] = "authorized"
    st["authorization_source"] = "task_scoped"
    st["authorization_origin_ref"] = _opaque(origin_ref, "invalid_origin")
    st["run_state"] = "ready"
    state.append_log(st, "scope_extension_approved", ref=request_id)
    _clear_private_run(str(st.get("run_id") or ""), request_id=request_id)
    return grant.grant_id


def revoke_run_authorization(
    st: dict,
    *,
    grant_ref: str | None = None,
    policy=None,
    save=state.save_task_state,
) -> dict:
    """Immediately revoke one grant or every grant belonging to this run."""

    from osia.action_policy import get_default_permission_policy

    active_policy = policy or get_default_permission_policy()
    if grant_ref:
        result = active_policy.store.revoke_grant(grant_ref)
    else:
        result = active_policy.store.revoke_run_grants(str(st.get("run_id") or ""))
    st["policy_epoch"] = result.policy_epoch
    st["run_state"] = "suspended_authorization_revoked"
    st["authorization_status"] = "revoked"
    st["pending_scope_extension"] = None
    _clear_private_run(str(st.get("run_id") or ""))
    st["revocation"] = {
        "revoked": True,
        "revoked_at": _now_iso(),
        "reason_code": "run_authorization_revoked",
    }
    state.append_log(st, "authorization_revoked", ref=grant_ref or st.get("run_id"))
    _safe_call(save, st)
    return result.public_metadata()


def apply_external_permission_revocation(
    st: dict,
    *,
    policy_epoch: int,
    grant_ref: str | None = None,
    revoke_all: bool = False,
    save=state.save_task_state,
) -> dict:
    """Refresh a loaded run after the protected store revoked its authority."""

    if not isinstance(st, dict) or type(policy_epoch) is not int or policy_epoch < 0:
        raise ValueError("invalid_external_permission_revocation")
    target = str(grant_ref or "")
    refs = [str(item) for item in (st.get("grant_refs") or []) if str(item)]
    st["grant_refs"] = [] if revoke_all else [item for item in refs if item != target]
    st["policy_epoch"] = policy_epoch
    st["pending_scope_extension"] = None
    st["authorization_request"] = None
    _clear_private_run(str(st.get("run_id") or ""))
    st["revocation"] = {
        "revoked": True,
        "revoked_at": _now_iso(),
        "reason_code": "run_authorization_revoked",
    }
    if st.get("run_state") not in {
        "completed", "failed", "cancelled", "blocked_outcome_unknown",
    }:
        st["run_state"] = "suspended_authorization_revoked"
        st["authorization_status"] = "revoked"
    state.append_log(st, "authorization_revoked", ref=target or st.get("run_id"))
    _safe_call(save, st)
    return st


def prepare_replan(st: dict, *, origin_ref: str = "desktop", policy=None) -> dict:
    """Increment plan revision and require a fresh review for the new plan tail."""

    from osia.action_policy import get_default_permission_policy

    active_policy = policy or get_default_permission_policy()
    _clear_private_run(str(st.get("run_id") or ""))
    active_policy.store.revoke_run_grants(str(st.get("run_id") or ""))
    st["plan_revision"] = int(st.get("plan_revision", 0)) + 1
    st["authorization_revision"] = 0
    st["grant_refs"] = []
    st["pending_scope_extension"] = None
    return prepare_planned_state(st, origin_ref=origin_ref, policy=active_policy)


def revalidate_loaded_authorization(
    st: dict,
    *,
    origin_ref: str = "desktop",
    policy=None,
) -> dict:
    """Fail closed when a checkpoint's protected authority is no longer live."""

    from osia.action_policy import PermissionMode, get_default_permission_policy

    active_policy = policy or get_default_permission_policy()
    permission_state = active_policy.store.load_state()
    st["authorization_origin_ref"] = _opaque(origin_ref, "invalid_origin")
    restored_pending = restore_planned_runtime_descriptors(st)
    if permission_state.permission_mode == PermissionMode.FULL_ACCESS:
        st["policy_epoch"] = permission_state.policy_epoch
        st["authorization_source"] = "full_access"
        if (
            st.get("run_state") == "suspended_authorization_revoked"
            or bool((st.get("revocation") or {}).get("revoked"))
        ):
            st["run_state"] = "suspended_authorization_revoked"
            st["authorization_status"] = "revoked"
        elif st.get("run_state") not in {
            "completed", "failed", "cancelled", "blocked_outcome_unknown",
        }:
            st["run_state"] = "ready"
            st["authorization_status"] = "not_required_full_access"
        st["authorization_request"] = None
        return st

    if (
        st.get("run_state") == "suspended_authorization_revoked"
        or bool((st.get("revocation") or {}).get("revoked"))
    ):
        st["policy_epoch"] = permission_state.policy_epoch
        st["run_state"] = "suspended_authorization_revoked"
        st["authorization_status"] = "revoked"
        st["authorization_request"] = None
        st["pending_scope_extension"] = None
        return st

    if st.get("run_state") == "suspended_scope_extension":
        pending = st.get("pending_scope_extension")
        request_id = pending.get("request_id") if isinstance(pending, dict) else ""
        with _PRIVATE_LOCK:
            private_available = (
                str(st.get("run_id") or ""), str(request_id or "")
            ) in _PRIVATE_REQUESTS
        if not private_available:
            active_policy.store.revoke_run_grants(str(st.get("run_id") or ""))
            return prepare_planned_state(
                st, origin_ref=origin_ref, policy=active_policy,
            )

    terminal_or_barrier = st.get("run_state") in {
        "completed", "failed", "cancelled", "blocked_outcome_unknown",
    }
    if terminal_or_barrier:
        return st
    refs_available = active_policy.store.autonomous_grant_refs_available(
        st.get("grant_refs"),
        run_id=str(st.get("run_id") or ""),
        plan_revision=int(st.get("plan_revision", 0) or 0),
        now=time.time(),
    )
    needs_review = (
        st.get("authorization_source") == "full_access"
        or st.get("authorization_source") not in {"task_scoped"}
        or (
            st.get("authorization_source") == "task_scoped"
            and (
                st.get("policy_epoch") != permission_state.policy_epoch
                or not refs_available
            )
        )
    )
    request = st.get("authorization_request")
    try:
        request_epoch = int(request.get("expected_policy_epoch", -1))
        request_expires_at = float(request.get("expires_at", 0.0) or 0.0)
    except (AttributeError, TypeError, ValueError, OverflowError):
        request_epoch = -1
        request_expires_at = 0.0
    pending_request_valid = bool(
        restored_pending
        and isinstance(request, dict)
        and request.get("responded") is not True
        and request_epoch == permission_state.policy_epoch
        and request_expires_at > time.time()
    )
    if st.get("run_state") == "authorization_pending" and pending_request_valid:
        request["origin_ref"] = _opaque(origin_ref, "invalid_origin")
        return st
    if st.get("run_state") == "authorization_pending" or needs_review:
        st["policy_epoch"] = permission_state.policy_epoch
        st["authorization_revision"] = 0
        st["grant_refs"] = []
        st["authorization_status"] = "pending"
        st["authorization_source"] = "none"
        st["run_state"] = "authorization_pending"
        st["pending_scope_extension"] = None
        st["authorization_request"] = _authorization_request(
            st,
            origin_ref=origin_ref,
            policy_epoch=permission_state.policy_epoch,
        )
        state.append_log(st, "authorization_review_required", note="resume_fail_closed")
    return st


def pc_projection(st: dict) -> dict:
    """Local display projection; may include explicitly local private details."""

    if not isinstance(st, dict):
        return {}
    request = st.get("authorization_request")
    pending = st.get("pending_scope_extension")
    pending_projection = dict(pending) if isinstance(pending, dict) else None
    if pending_projection is not None:
        with _PRIVATE_LOCK:
            private_request = _PRIVATE_REQUESTS.get((
                str(st.get("run_id") or ""),
                str(pending_projection.get("request_id") or ""),
            ))
        private_detail = _private_scope_display(private_request)
        if private_detail:
            pending_projection["private_scope_detail"] = private_detail
    return {
        "protocol_version": AUTHORIZATION_PROTOCOL_VERSION,
        "run_id": str(st.get("run_id") or ""),
        "plan_revision": int(st.get("plan_revision", 0) or 0),
        "authorization_revision": int(st.get("authorization_revision", 0) or 0),
        "policy_epoch": int(st.get("policy_epoch", 0) or 0),
        "run_state": str(st.get("run_state") or ""),
        "authorization_status": str(st.get("authorization_status") or ""),
        "authorization_source": str(st.get("authorization_source") or "none"),
        "grant_refs": list(st.get("grant_refs") or []),
        "capability_summary": [
            dict(item) for item in st.get("capability_summary") or []
            if isinstance(item, dict)
        ],
        "authorization_request": dict(request) if isinstance(request, dict) else None,
        "pending_scope_extension": pending_projection,
        "private_scope_details": dict(st.get("_runtime_pc_capability_details") or {}),
        "operation_records": [
            dict(item) for item in st.get("operation_records") or []
            if isinstance(item, dict)
        ],
    }


def mobile_projection(st: dict) -> dict:
    """Sanitized current-run projection; private arguments never cross the WS."""

    projection = pc_projection(st)
    projection.pop("private_scope_details", None)
    # Operation hashes and grant refs are authority/diagnostic internals, not a
    # mobile display need.  Only safe capability and request summaries travel.
    projection.pop("operation_records", None)
    projection["grant_count"] = len(projection.pop("grant_refs", []))
    for item in projection.get("capability_summary") or []:
        if isinstance(item, dict):
            item.pop("target_scope_hash", None)
    pending = projection.get("pending_scope_extension")
    if isinstance(pending, dict):
        pending.pop("private_scope_detail", None)
    return projection


def mobile_safe_text(value: Any, *, limit: int = 240) -> str:
    """Remove private targets from display text shared with mobile clients."""

    text = str(value or "")
    text = _EMAIL_RE.sub("[private recipient]", text)
    text = _PRIVATE_URL_RE.sub("[private URL]", text)
    text = _ABS_PATH_RE.sub("[local target]", text)
    text = _FILENAME_RE.sub("[local file]", text)
    return " ".join(text.split())[:max(0, int(limit))]


__all__ = [
    "AUTHORIZATION_PROTOCOL_VERSION", "AuthorizationError",
    "apply_external_permission_revocation", "approve_scope_extension",
    "authorize_and_start",
    "bind_request_execution_context", "current_runtime",
    "derive_capability_proposal", "restore_planned_runtime_descriptors",
    "inject_proposal_requests",
    "mark_partial_effect", "mobile_projection", "observe_permission_decision",
    "mobile_safe_text",
    "on_permit_consumed", "operation_id_for_call", "pc_projection",
    "prepare_planned_state", "prepare_replan", "record_tool_execution",
    "request_block_reason", "response_for_current_request",
    "revalidate_loaded_authorization",
    "revoke_run_authorization", "run_authorization_scope",
    "suspend_authorization_unavailable",
]
