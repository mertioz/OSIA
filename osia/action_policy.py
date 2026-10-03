"""Integrity-only action policy for OSIA dispatch boundaries.

WOR-301 removed the user permission system: OSIA runs with permanent Full
Access and no ordinary confirmation.  What remains in this module is execution
integrity, not authority:

* a syntax-based catastrophic host-destruction guard
  (``system_mutation_barrier`` / ``ERROR: BLOCK_SYSTEM_DESTRUCTION``); uncertain
  or unsupported inputs are left to ordinary tool validation/runtime handling;
* the harmful-request block (fraud, harassment, credential theft, ...);
* trusted pre-dispatch bindings for browser element activation and uploads;
* the exact current deletion-plan binding used for target freshness;
* caller context for canonical Chat and Scheduled Task triggers. Legacy
  Autonomous labels are inert compatibility data, not a second runtime.

There are no modes, grants, permits, epochs, journals or confirmations.  The
legacy permit helpers are inert shims kept only so that older call sites
degrade to "no permit" instead of importing a removed symbol.
"""

from __future__ import annotations

import getpass
import platform
import re
import unicodedata
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from osia.scope_normalization import canonical_hash


class ActionClass(str, Enum):
    BENIGN_LOCAL = "A"
    READ_ONLY_WEB = "B"
    EXTERNAL_COMMUNICATION = "C"
    SENSITIVE = "D"
    DISALLOWED = "E"


class ActionOutcome(str, Enum):
    ALLOW = "allowed"
    BLOCK = "blocked"
    # Legacy values kept for existing importers; never produced anymore.
    CONFIRM = "confirmation_required"
    CLARIFY = "clarification_required"


@dataclass(frozen=True)
class ActionDecision:
    action_class: ActionClass
    outcome: ActionOutcome
    reason: str
    routes: tuple[str, ...] = ()
    recipient: str = ""
    content: str = ""
    final_send_requested: bool = False


def _normal(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


_HARMFUL_PATTERNS = (
    (r"\b(?:harass|harceler|repeated insulting messages?)\b", "harassment"),
    (r"\b(?:threaten|menacer|send threats?)\b", "threats"),
    (r"\b(?:spam.{0,30}(?:people|contacts|users|recipients)|send unsolicited messages?|envoyer des pourriels)\b", "spam"),
    (r"\b(?:fraudulent message|defraud|scam (?:someone|people|victims)|escroquer)\b", "fraud"),
    (r"\b(?:impersonate|impersonating|usurper l.identite|pretend to be)\b", "impersonation"),
    (r"\b(?:steal|collect|harvest)\s+(?:a\s+)?(?:account\s+)?(?:password|credential|login|token)s?\b", "credential_theft"),
    (r"\b(?:create|send|use|launch)\b.{0,40}\b(?:phishing|hameconnage)\b", "credential_theft"),
    (r"\b(?:bypass|evade|contourner)\s+(?:the\s+)?(?:login|2fa|mfa|account security|authentication)\b", "security_bypass"),
    (r"\b(?:someone else.s|another person.s|compte d.une autre personne)\s+(?:account|compte)\b", "other_person_account"),
    (r"\b(?:covertly|secretly|without (?:their|the owner.s) knowledge|a son insu)\b", "covert_action"),
    (r"\b(?:random|unspecified)\s+(?:people|recipients|contacts|users)\b", "unscoped_mass_messaging"),
)

_COMMUNICATION_RE = re.compile(
    r"\b(?:message|messages|email|emails|e-mail|mail|dm|courriel|texto|sms)\b"
)
_SEND_RE = re.compile(r"\b(?:send|sending|sent|envoyer|envoie|envoi|transmettre)\b")
_DRAFT_RE = re.compile(r"\b(?:draft|prepare|write|rediger|redige|preparer|prepare)\b")
_PUBLIC_POST_RE = re.compile(
    r"\b(?:post|publish|share|publier|partager)\b.*\b(?:public|profile|social|feed|timeline|profil|reseau)\b"
)
_DELETE_RE = re.compile(r"\b(?:delete|remove|erase|supprimer|effacer)\b.*\b(?:file|folder|directory|fichier|dossier)s?\b")
_ACCOUNT_SECURITY_RE = re.compile(
    r"\b(?:change|modify|update|enable|disable|reset|changer|modifier|activer|desactiver|reinitialiser)\b.*\b(?:account security|password|2fa|mfa|security settings|mot de passe|securite du compte)\b"
)
_FINANCIAL_RE = re.compile(
    r"\b(?:buy|purchase|pay|transfer|sign|accept contract|acheter|payer|virer|signer|accepter le contrat)\b"
)
_FORM_COMMIT_RE = re.compile(
    r"(?:\b(?:submit|soumettre)\b|"
    r"\b(?:apply|postuler)\b.{0,50}\b(?:job|application|candidature|emploi)\b|"
    r"\b(?:save|enregistrer)\b.{0,80}\b(?:form|application|profile|formulaire|"
    r"candidature|profil)\b|"
    r"\b(?:confirm|finalize|confirmer|finaliser)\b.{0,80}\b(?:form|application|"
    r"order|request|checkout|formulaire|candidature|commande|demande)\b|"
    r"\b(?:send application|envoyer la candidature)\b)"
)


def classify_action_request(text: str) -> ActionDecision:
    """Classify one request for the absolute safety block.

    Harmful requests are DISALLOWED and must never dispatch.  Every ordinary or
    recognised destructive intent is ALLOW: there is no confirmation step
    anymore, so the classification only protects the safety block and tells the
    retry / anti-duplicate logic whether a call is a final external commitment.
    """

    normalized = _normal(text)
    for pattern, reason in _HARMFUL_PATTERNS:
        if re.search(pattern, normalized):
            return ActionDecision(
                ActionClass.DISALLOWED,
                ActionOutcome.BLOCK,
                reason,
            )

    if _PUBLIC_POST_RE.search(normalized):
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.ALLOW,
            "public_posting",
        )
    if _DELETE_RE.search(normalized):
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.ALLOW,
            "destructive_local_action",
        )
    if _ACCOUNT_SECURITY_RE.search(normalized):
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.ALLOW,
            "account_security_change",
        )
    if _FINANCIAL_RE.search(normalized):
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.ALLOW,
            "financial_or_contractual_action",
        )
    if _FORM_COMMIT_RE.search(normalized):
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.ALLOW,
            "external_form_commitment",
        )

    if _COMMUNICATION_RE.search(normalized) and (
        _SEND_RE.search(normalized) or _DRAFT_RE.search(normalized)
    ):
        return ActionDecision(
            ActionClass.EXTERNAL_COMMUNICATION,
            ActionOutcome.ALLOW,
            "explicit_message",
        )

    return ActionDecision(
        ActionClass.BENIGN_LOCAL,
        ActionOutcome.ALLOW,
        "benign_local",
    )


def action_policy_prompt_block(lang: str | None = None) -> str:
    """The permission prompt is gone: OSIA is permanently Full Access."""

    del lang
    return ""


_FINAL_LABEL_RE = re.compile(
    r"\b(?:send|envoyer|post|publish|share|delete|remove|confirm|buy|purchase|"
    r"pay|save|submit|apply|finalize|publier|partager|supprimer|confirmer|"
    r"acheter|payer|enregistrer|soumettre|postuler|finaliser)\b"
)


def _is_final_external_step(
    decision: ActionDecision,
    tool_name: str,
    kwargs: dict,
) -> bool:
    if decision.action_class not in {
        ActionClass.EXTERNAL_COMMUNICATION,
        ActionClass.SENSITIVE,
        ActionClass.DISALLOWED,
    }:
        return False
    tool = str(tool_name or "")
    values = kwargs if isinstance(kwargs, dict) else {}
    if tool in {"press_key", "desktop_press"}:
        return _normal(values.get("key", "")) in {"enter", "return"}
    field = {
        "find_and_click": "text",
        "click": "selector",
        "desktop_click": "description",
        "activate_desktop_surface": "objective",
    }.get(tool)
    return bool(field and _FINAL_LABEL_RE.search(_normal(values.get(field, ""))))


def is_final_external_tool_call(
    user_request: str,
    tool_name: str,
    kwargs: dict,
) -> bool:
    """Whether one browser/computer action is a recognized final commitment."""

    return _is_final_external_step(
        classify_action_request(user_request),
        str(tool_name or ""),
        kwargs if isinstance(kwargs, dict) else {},
    )


class ExecutionType(str, Enum):
    """Canonical execution owners: the Chat runtime and Scheduled triggers.

    The historical ``AUTONOMOUS`` value was removed with the Autonomous engine
    (WOR-303); persisted pre-Chat contexts are migrated by their readers.
    """

    CHAT = "chat"
    SCHEDULED = "scheduled"


_OPAQUE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _require_opaque_ref(value: str, *, optional: bool = False) -> str:
    candidate = str(value or "")
    if optional and not candidate:
        return ""
    if not _OPAQUE_REF_RE.fullmatch(candidate):
        raise ValueError("invalid_opaque_reference")
    return candidate


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    execution_type: ExecutionType
    interactive: bool
    owner_ref: str
    machine_ref: str
    operation_id: str
    policy_epoch: int
    device_ref: str = ""
    run_id: str = ""
    scheduled_task_id: str = ""
    task_revision: str = ""
    plan_revision: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution_type, ExecutionType):
            raise ValueError("invalid_execution_type")
        if not isinstance(self.interactive, bool):
            raise ValueError("invalid_interactive_state")
        for field_name in ("execution_id", "owner_ref", "machine_ref", "operation_id"):
            _require_opaque_ref(getattr(self, field_name))
        for field_name in (
            "device_ref", "run_id", "scheduled_task_id", "task_revision",
            "plan_revision",
        ):
            _require_opaque_ref(getattr(self, field_name), optional=True)
        if type(self.policy_epoch) is not int or self.policy_epoch < 0:
            raise ValueError("invalid_policy_epoch")

    def public_metadata(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "execution_type": self.execution_type.value,
            "interactive": self.interactive,
            "operation_id": self.operation_id,
            "policy_epoch": self.policy_epoch,
        }


_CURRENT_EXECUTION_CONTEXT: ContextVar[ExecutionContext | None] = ContextVar(
    "osia_execution_context", default=None,
)


@contextmanager
def execution_context_scope(context: ExecutionContext):
    if not isinstance(context, ExecutionContext):
        raise ValueError("invalid_execution_context")
    token = _CURRENT_EXECUTION_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_EXECUTION_CONTEXT.reset(token)


def _hashed_local_ref(label: str, value: str) -> str:
    return f"{label}:{canonical_hash({'value': str(value or '')})[:32]}"


def make_execution_context(
    *,
    execution_type: ExecutionType = ExecutionType.CHAT,
    interactive: bool = True,
    owner_ref: str | None = None,
    machine_ref: str | None = None,
    device_ref: str = "",
    run_id: str = "",
    scheduled_task_id: str = "",
    task_revision: str = "",
    plan_revision: str = "",
    policy_epoch: int | None = None,
    execution_id: str | None = None,
    operation_id: str | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        execution_id=execution_id or uuid.uuid4().hex,
        execution_type=execution_type,
        interactive=bool(interactive),
        owner_ref=owner_ref or _hashed_local_ref("owner", getpass.getuser()),
        machine_ref=machine_ref or _hashed_local_ref("machine", platform.node()),
        device_ref=device_ref,
        run_id=run_id,
        scheduled_task_id=scheduled_task_id,
        task_revision=task_revision,
        plan_revision=plan_revision,
        operation_id=operation_id or uuid.uuid4().hex,
        policy_epoch=0 if policy_epoch is None else int(policy_epoch),
    )


def _deletion_preflight(
    tool_name: str,
    values: Mapping[str, Any],
    *,
    mcp_destructive: bool = False,
):
    from osia.deletion_plan import (
        DeletionPlanError,
        DeletionPreflightKind,
        clear_pending_deletion,
        prepare_deletion_call,
    )

    clear_pending_execution_permit()
    clear_pending_deletion()
    try:
        result = prepare_deletion_call(
            tool_name,
            values,
            mcp_destructive=mcp_destructive,
        )
    except DeletionPlanError as exc:
        if exc.code == "system_destruction":
            return True, None, "ERROR: BLOCK_SYSTEM_DESTRUCTION"
        return True, None, f"ERROR: DELETION_PREFLIGHT_REQUIRED ({exc.code})"
    if result.kind is DeletionPreflightKind.NOT_DELETION:
        return False, None, None
    return True, result.plan, None


def _deletion_binding_arguments(
    tool_name: str,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = dict(values)
    if tool_name == "delete_file":
        return {"path": str(normalized.get("path") or "")}
    if tool_name == "delete_directory":
        return {
            "path": str(normalized.get("path") or ""),
            "recursive": bool(normalized.get("recursive", False)),
        }
    if tool_name in {"bash", "run_command"}:
        bound = {"command": str(normalized.get("command") or "")}
        if normalized.get("timeout") is not None:
            bound["timeout"] = normalized.get("timeout")
        return bound
    return normalized


def _hold_plan_permit(
    tool_name: str,
    values: Mapping[str, Any],
    plan,
    *,
    policy_epoch: int,
    strength: str,
) -> str | None:
    try:
        from osia.deletion_plan import issue_pending_deletion

        binding_values = _deletion_binding_arguments(tool_name, values)
        issue_pending_deletion(
            tool_name,
            binding_values,
            plan,
            policy_epoch=policy_epoch,
            confirmation_strength=strength,
        )
        return None
    except Exception:
        clear_pending_execution_permit()
        return "ERROR: deletion execution permit unavailable; nothing was deleted"


def _system_destruction_error(plan) -> str | None:
    """Reject a deletion plan only for explicit catastrophic host evidence."""

    if plan is None:
        return None
    if bool(getattr(plan, "system_destruction", False)):
        return "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    return None


def _finish_ordinary_deletion_authorization(
    tool_name: str,
    values: Mapping[str, Any],
    plan,
) -> str | None:
    """Bind the exact verified deletion plan for dispatch (anti-replay only)."""

    if plan is None:
        return None
    return _hold_plan_permit(
        tool_name,
        values,
        plan,
        policy_epoch=0,
        strength="ordinary",
    )


def _system_mutation_barrier(
    tool_name: str,
    values: Mapping[str, Any],
) -> str | None:
    try:
        from osia.system_resources import classify_non_deletion_system_mutation

        evidence = classify_non_deletion_system_mutation(tool_name, values)
        if evidence is None or not evidence.blocks_destruction:
            return None
        # The narrow catastrophic-host guard is enforced directly, without a
        # user permission store or turn-level authority.
        return "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    except Exception:
        # An unavailable or inconclusive classifier is not evidence of host
        # destruction.  Ordinary tool validation/runtime failures stay with
        # the tool that actually performs the requested operation.
        return None


def system_mutation_barrier(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Public central facade used again at direct native I/O boundaries."""

    return _system_mutation_barrier(str(tool_name or ""), arguments)


def guard_tool_call(user_request: str, tool_name: str, kwargs: dict) -> str | None:
    """Return an ``ERROR:`` string when a call must not execute.

    WOR-305: ordinary actions execute without confirmation.  The gate only
    enforces (1) the catastrophic host-destruction barrier, (2) the trusted
    browser/upload bindings and (3) the exact deletion-plan binding.  It never
    asks a user for permission and never refuses a technically available tool
    for relevance, mode, request or semantic reasons. ToolAttempt and ActionTruth
    retain facts; historical state does not veto a fresh model request. Exact
    attempt ownership, current target freshness, cancellation, and
    BLOCK_SYSTEM_DESTRUCTION remain separate integrity checks.
    """

    tool = str(tool_name or "")
    values = kwargs if isinstance(kwargs, dict) else {}

    system_barrier = _system_mutation_barrier(tool, values)
    if system_barrier is not None:
        return system_barrier

    if tool == "activate_browser_element":
        try:
            from osia.tools.browser_controller import prepare_browser_action_authorization

            prepared_action = prepare_browser_action_authorization(values)
        except Exception:
            prepared_action = {"error": "browser_action_preflight_unavailable"}
        if not isinstance(prepared_action, Mapping) or prepared_action.get("error"):
            code = str(
                prepared_action.get("error")
                if isinstance(prepared_action, Mapping)
                else "browser_action_preflight_unavailable"
            )
            return f"ERROR: browser action rejected before dispatch ({code[:80]})"
        values["trusted_final_action"] = (
            prepared_action.get("trusted_final_action") is True
        )
        values["destination_origin"] = str(
            prepared_action.get("destination_origin") or ""
        )[:512]
        target_binding = str(
            prepared_action.get("trusted_target_binding") or ""
        )
        if not re.fullmatch(r"[0-9a-f]{64}", target_binding):
            return "ERROR: browser action rejected before dispatch (invalid_target_binding)"
        values["trusted_target_binding"] = target_binding

    # Upload authorization is bound from trusted live browser/file state before
    # dispatch.  The returned fields are opaque or safe metadata and replace
    # any model-supplied binding values.
    if tool == "upload_browser_file":
        try:
            from osia.tools.browser_controller import prepare_upload_authorization

            prepared = prepare_upload_authorization(values)
        except Exception:
            prepared = {"error": "upload_preflight_unavailable"}
        if not isinstance(prepared, Mapping) or prepared.get("error"):
            code = str(
                prepared.get("error") if isinstance(prepared, Mapping)
                else "upload_preflight_unavailable"
            )
            return f"ERROR: upload rejected before selection ({code[:80]})"
        for field in (
            "prepared_upload_ref", "destination_origin", "file_size",
            "detected_mime", "operation_expires_at",
        ):
            values[field] = prepared[field]

    # Build trusted deletion evidence before dispatch.  A direct deletion or
    # exact shell deletion can proceed only with a complete plan; non-deletion
    # shell behavior falls through unchanged.
    if tool in {"delete_file", "delete_directory", "bash", "run_command"}:
        handled, deletion_plan, preflight_error = _deletion_preflight(tool, values)
        if preflight_error is not None:
            # Preserve the established contract for a missing ordinary target.
            # No deletion permit is issued, and the native boundary repeats
            # preflight before any I/O, so the call still fails closed if the
            # target remains absent.
            if not preflight_error.endswith("(deletion_root_missing)"):
                return preflight_error
            return None
        if handled and deletion_plan is not None:
            destruction = _system_destruction_error(deletion_plan)
            if destruction is not None:
                return destruction
            return _finish_ordinary_deletion_authorization(
                tool, values, deletion_plan,
            )
    return None


class DecisionType(str, Enum):
    """Historical decision vocabulary retained for execution-integrity callers.

    No authority is attached to these values; durable anti-replay now lives in
    the canonical Chat runtime's durable tool attempts.
    """

    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    REQUIRE_REINFORCED_CONFIRMATION = "require_reinforced_confirmation"
    SUSPEND_SCOPE_EXTENSION = "suspend_scope_extension"
    DENY = "deny"
    BLOCK_SYSTEM_DESTRUCTION = "block_system_destruction"
    REQUIRE_COLOSSAL_DELETION_CONFIRMATION = (
        "require_colossal_deletion_confirmation"
    )


def permission_request_is_genuinely_read_only(request) -> bool:
    """Require both the registered and runtime contracts to prove observation."""

    from osia.tool_contracts import SideEffectClass

    tool_spec = getattr(request, "tool_spec", None)
    effective = getattr(request, "effective_contract", None)
    return bool(
        tool_spec is not None
        and effective is not None
        and tool_spec.side_effect is SideEffectClass.READ_ONLY
        and effective.side_effect is SideEffectClass.READ_ONLY
    )


def authorize_mcp_deletion_call(
    server_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Authorize one structurally declared MCP deletion without trusting MCP claims."""

    full_name = f"mcp__{server_id}__{tool_name}"
    handled, plan, preflight_error = _deletion_preflight(
        full_name,
        arguments,
        mcp_destructive=True,
    )
    if preflight_error is not None:
        return preflight_error
    if not handled or plan is None:
        return "ERROR: MCP deletion preflight unavailable; the tool was not executed"
    destruction = _system_destruction_error(plan)
    if destruction is not None:
        return destruction
    # WOR-301: no connector confirmation; bind the exact plan and dispatch.
    return _finish_ordinary_deletion_authorization(
        full_name,
        arguments,
        plan,
    )


def execute_authorized_deletion_call(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    directory: bool,
    recursive: bool,
) -> str:
    """Consume the exact plan permit, delete deterministically, and sanitize results."""

    from osia.deletion_plan import (
        DeletionPlanError,
        consume_pending_deletion_for_dispatch,
        execute_deletion_dispatch,
        pending_deletion_matches,
    )

    binding_arguments = _deletion_binding_arguments(tool_name, arguments)
    if not pending_deletion_matches(tool_name, binding_arguments):
        return "ERROR: deletion authorization unavailable; nothing was deleted"
    try:
        dispatch = consume_pending_deletion_for_dispatch(
            tool_name,
            binding_arguments,
            policy_epoch=0,
        )
        result = execute_deletion_dispatch(dispatch)
    except DeletionPlanError as exc:
        if exc.code in {
            "deletion_plan_changed", "deletion_plan_expired",
            "deletion_permit_expired", "deletion_policy_epoch_changed",
            "deletion_policy_version_changed", "deletion_permit_inventory_mismatch",
        }:
            return "ERROR: DELETION_PLAN_CHANGED_REQUIRES_REVALIDATION"
        return f"ERROR: deletion blocked before I/O ({exc.code})"
    except Exception:
        return "ERROR: deletion blocked before I/O (internal_preflight_failure)"
    return result.model_text(directory=directory, recursive=recursive)


def begin_external_deletion_dispatch(
    tool_name: str,
    arguments: Mapping[str, Any],
):
    """Revalidate and consume a deletion permit immediately before MCP dispatch."""

    from osia.deletion_plan import (
        DeletionPlanError,
        consume_pending_deletion_for_dispatch,
        pending_deletion_matches,
    )

    if not pending_deletion_matches(tool_name, arguments):
        raise DeletionPlanError("deletion_authorization_missing")
    return consume_pending_deletion_for_dispatch(
        tool_name,
        arguments,
        policy_epoch=0,
    )


def external_deletion_result(dispatch, raw_result: Any) -> str:
    """Verify the exact local postcondition; MCP response text is never trusted."""

    del raw_result
    from osia.deletion_plan import (
        verify_external_deletion_dispatch,
    )

    result = verify_external_deletion_dispatch(dispatch)
    return result.model_text(directory=False, recursive=False)


# ---------------------------------------------------------------------------
# Inert permit compatibility shims (WOR-301)
# ---------------------------------------------------------------------------
# No execution permit exists anymore.  These names are kept so older call
# sites degrade to "no permit held" instead of importing a removed symbol.


def pending_execution_permit_matches(
    tool_name: str, arguments: Mapping[str, Any],
) -> bool:
    del tool_name, arguments
    return False


def consume_pending_execution_permit(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> bool:
    del tool_name, arguments
    return False


def clear_pending_execution_permit() -> None:
    return None
