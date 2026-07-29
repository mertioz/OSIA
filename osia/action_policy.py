"""Shared action-risk policy for OSIA prompts and irreversible tool gates.

The model still performs semantic safety reasoning.  This small taxonomy makes
the expected treatment of ordinary computer tasks explicit and adds a narrow,
fail-closed confirmation boundary for the irreversible actions OSIA can detect
reliably before executing a tool call.
"""

from __future__ import annotations

import re
import math
import json
import secrets
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, TYPE_CHECKING

from osia.scope_normalization import (
    NormalizedScope,
    ScopeNormalizationError,
    WebScopeKind,
    canonical_hash,
    canonical_serialize,
    combine_scopes,
    normalize_exact_call_scope,
    normalize_file_scope,
    normalize_gmail_scope,
    normalize_mcp_scope,
    normalize_web_scope,
    normalized_scope_from_protected,
    sensitive_hash_equal,
)

if TYPE_CHECKING:
    from osia.permission_store import PermissionStore


class ActionClass(str, Enum):
    BENIGN_LOCAL = "A"
    READ_ONLY_WEB = "B"
    EXTERNAL_COMMUNICATION = "C"
    SENSITIVE = "D"
    DISALLOWED = "E"


class ActionOutcome(str, Enum):
    ALLOW = "allowed"
    CONFIRM = "confirmation_required"
    CLARIFY = "clarification_required"
    BLOCK = "blocked"


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
_BULK_RE = re.compile(
    r"\b(?:bulk|batch|mailing list|all (?:my )?contacts|everyone|groupe entier|liste de diffusion)\b"
)
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
_WEB_RE = re.compile(
    r"\b(?:web|internet|website|site|browser|brave|chrome|firefox|navigate|search|research|page|navigateur|rechercher)\b"
)
_LOCAL_ACTION_RE = re.compile(
    r"\b(?:create|write|edit|organize|open|creer|ecrire|modifier|organiser|ouvrir)\b.*\b(?:file|folder|document|application|app|fichier|dossier)\b"
)


def _message_details(text: str) -> tuple[str, str]:
    """Extract only clearly delimited recipient/content; never guess either."""
    raw = str(text or "").strip()
    recipient = ""
    recipient_match = re.search(
        r"\bto\s+([^:\n,]{2,80}?)(?=\s*(?::|,|\b(?:saying|with|message)\b|$))",
        raw,
        re.IGNORECASE,
    )
    if recipient_match is None:
        recipient_match = re.search(
            r"\b(?:à|au|aux)\s+([^:\n,]{2,80}?)(?=\s*(?::|,|\b(?:disant|avec|message)\b|$))",
            raw,
            re.IGNORECASE,
        )
    if recipient_match:
        start, end = recipient_match.span(1)
        recipient = raw[start:end].strip(" \t.,:")

    content = ""
    quoted = re.search(r"[\"“”«](.{1,500}?)[\"“”»]", raw, re.DOTALL)
    if quoted:
        content = quoted.group(1).strip()
    else:
        labelled = re.search(
            r"(?:message|content|contenu|saying|disant)\s*:\s*(.{1,500})$",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        if labelled:
            content = labelled.group(1).strip()
        elif recipient_match:
            colon = raw.find(":", recipient_match.end())
            if colon >= 0:
                content = raw[colon + 1:].strip()
    return recipient, content


def classify_action_request(text: str) -> ActionDecision:
    """Classify a request into the explicit A-E action policy.

    The classifier is deliberately small and conservative.  It protects the
    concrete tool gates below; it does not replace provider safety reasoning.
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
        _recipient, content = _message_details(text)
        if not content:
            return ActionDecision(
                ActionClass.SENSITIVE,
                ActionOutcome.CLARIFY,
                "post_content_missing",
                ("browser", "computer_use"),
            )
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.CONFIRM,
            "public_posting",
            ("browser", "computer_use"),
            recipient="public profile",
            content=content,
        )
    if _DELETE_RE.search(normalized):
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.CONFIRM,
            "destructive_local_action",
            ("filesystem",),
            content=str(text or "")[:500],
        )
    if _ACCOUNT_SECURITY_RE.search(normalized):
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.CONFIRM,
            "account_security_change",
            ("browser", "computer_use"),
            content=str(text or "")[:500],
        )
    if _FINANCIAL_RE.search(normalized):
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.CONFIRM,
            "financial_or_contractual_action",
            ("browser", "computer_use"),
            content=str(text or "")[:500],
        )
    if _FORM_COMMIT_RE.search(normalized):
        return ActionDecision(
            ActionClass.SENSITIVE,
            ActionOutcome.CONFIRM,
            "external_form_commitment",
            ("browser", "computer_use"),
        )

    communication = bool(_COMMUNICATION_RE.search(normalized))
    send_requested = bool(_SEND_RE.search(normalized))
    draft_requested = bool(_DRAFT_RE.search(normalized))
    if communication and (send_requested or draft_requested):
        recipient, content = _message_details(text)
        if not recipient:
            return ActionDecision(
                ActionClass.EXTERNAL_COMMUNICATION,
                ActionOutcome.CLARIFY,
                "recipient_missing",
                ("browser", "computer_use"),
                content=content,
                final_send_requested=send_requested,
            )
        if not content:
            return ActionDecision(
                ActionClass.EXTERNAL_COMMUNICATION,
                ActionOutcome.CLARIFY,
                "message_content_missing",
                ("browser", "computer_use"),
                recipient=recipient,
                final_send_requested=send_requested,
            )
        if _BULK_RE.search(normalized):
            return ActionDecision(
                ActionClass.SENSITIVE,
                ActionOutcome.CONFIRM,
                "bulk_messaging",
                ("browser", "computer_use"),
                recipient=recipient,
                content=content,
                final_send_requested=send_requested,
            )
        return ActionDecision(
            ActionClass.EXTERNAL_COMMUNICATION,
            ActionOutcome.ALLOW,
            "explicit_message" if send_requested else "explicit_draft",
            ("browser", "computer_use"),
            recipient=recipient,
            content=content,
            final_send_requested=send_requested,
        )

    if _WEB_RE.search(normalized):
        routes = ["open_browser", "navigate", "read_page"]
        if _LOCAL_ACTION_RE.search(normalized):
            routes.append("write_file")
        return ActionDecision(
            ActionClass.READ_ONLY_WEB,
            ActionOutcome.ALLOW,
            "read_only_web",
            tuple(routes),
        )
    return ActionDecision(
        ActionClass.BENIGN_LOCAL,
        ActionOutcome.ALLOW,
        "benign_local",
        ("filesystem", "open_application"),
    )


_PROMPT_EN = """## Action-risk policy (mandatory)
Class A — benign local/reversible actions (create or edit a local file, organize files, open an application, prepare content): ALLOWED. Use the available OSIA tools; do not produce a generic safety refusal.
Class B — read-only web actions (search, open a browser, navigate, read): ALLOWED. Internet access alone does not require confirmation.
Class C — user-authorized external communication: navigating and drafting are allowed. For a final Send, the recipient and exact harmless content must be clear; never invent either and never send more than requested. The runtime applies the configured confirmation gate to the irreversible Send action. A personal contact is not by itself a reason to refuse.
Class D — sensitive actions (delete, public post, account/security change, financial/contractual or bulk action, final external form submission/application): CONFIRM the exact scope before the irreversible step. Preparing ordinary fields is allowed, but Submit/Apply/Save/Confirm and a final Enter/Return remain gated. If scope is unclear, ask for clarification and fail closed.
Class E — harmful/disallowed actions: BLOCK harassment, threats, fraud, impersonation, credential theft, spam, unscoped mass messaging, security bypass, covert action, or action on another person's account.

Keep these outcomes distinct: (1) allowed and executable → use tools; (2) allowed but confirmation/clarification required → ask or use the gate; (3) allowed but a tool/login is unavailable → report that real technical limitation; (4) disallowed → give the specific safety reason. Never replace outcomes 1–3 with a generic refusal. Do not use Enter or another shortcut to bypass a required final-action confirmation."""

_PROMPT_FR = """## Politique de risque des actions (obligatoire)
Classe A — actions locales bénignes/réversibles (créer ou modifier un fichier local, organiser des fichiers, ouvrir une application, préparer du contenu) : AUTORISÉES. Utilise les outils OSIA disponibles ; ne réponds pas par un refus générique.
Classe B — actions web en lecture seule (rechercher, ouvrir un navigateur, naviguer, lire) : AUTORISÉES. L'accès à Internet ne demande pas à lui seul une confirmation.
Classe C — communication externe autorisée par l'utilisateur : naviguer et préparer un brouillon sont autorisés. Pour l'action finale Envoyer, le destinataire et le contenu exact et inoffensif doivent être clairs ; n'invente jamais l'un ou l'autre et n'envoie jamais plus que demandé. Le runtime applique la confirmation configurée à l'action irréversible. Un contact personnel n'est pas en soi un motif de refus.
Classe D — actions sensibles (suppression, publication publique, réglage de compte/sécurité, action financière/contractuelle ou en masse, soumission/candidature finale d'un formulaire externe) : CONFIRME la portée exacte avant l'étape irréversible. La préparation des champs ordinaires est autorisée, mais Soumettre/Postuler/Enregistrer/Confirmer et un appui final sur Entrée restent soumis au garde. Si la portée est ambiguë, demande une précision et échoue de façon sûre.
Classe E — actions nuisibles/interdites : BLOQUE harcèlement, menaces, fraude, usurpation d'identité, vol d'identifiants, spam, envoi de masse non délimité, contournement de sécurité, action dissimulée ou action sur le compte d'un tiers.

Distingue toujours : (1) autorisé et exécutable → utilise les outils ; (2) autorisé mais confirmation/précision requise → demande-la ou utilise le garde ; (3) autorisé mais outil/connexion indisponible → indique la vraie limite technique ; (4) interdit → donne la raison de sûreté précise. Ne remplace jamais les cas 1–3 par un refus générique. N'utilise pas Entrée ou un raccourci pour contourner la confirmation d'une action finale."""


def action_policy_prompt_block(lang: str | None = None) -> str:
    return _PROMPT_FR if str(lang or "").lower().startswith("fr") else _PROMPT_EN


_LOCAL_CONFIRM_TOOLS = frozenset({"delete_file", "delete_directory", "kill_process"})
_READ_ONLY_TOOLS = frozenset({
    "read_file", "list_files", "get_file_info", "search_files",
    "take_screenshot", "get_system_info", "list_running_processes",
    "get_clipboard", "is_running", "is_installed", "has_window",
    "steam_game_state", "web_state", "read_page", "get_page_text",
    "get_current_url", "inspect_form_fields", "list_memories", "list_scheduled_tasks",
    "gmail_list_labels", "gmail_list_messages", "gmail_get_message",
})
_FINAL_LABEL_RE = re.compile(
    r"\b(?:send|envoyer|post|publish|share|delete|remove|confirm|buy|purchase|"
    r"pay|save|submit|apply|finalize|publier|partager|supprimer|confirmer|"
    r"acheter|payer|enregistrer|soumettre|postuler|finaliser)\b"
)


# Dedicated owner for the stronger local colossal-deletion review.  The desktop
# GUI registers a provider that receives the sealed private DeletionPlan; the
# server registers a sanitized, non-approving mobile notifier.  Missing owner is
# a denial.  Ordinary confirmation plumbing remains unchanged.
_reinforced_deletion_confirmation_provider = None


def set_reinforced_deletion_confirmation_provider(fn) -> None:
    global _reinforced_deletion_confirmation_provider
    _reinforced_deletion_confirmation_provider = fn


def get_reinforced_deletion_confirmation_provider():
    return _reinforced_deletion_confirmation_provider


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
    }.get(tool)
    return bool(field and _FINAL_LABEL_RE.search(_normal(values.get(field, ""))))


def _confirmation_summary(
    decision: ActionDecision,
    tool_name: str,
    kwargs: dict,
) -> str:
    # Confirmation UI is an observability surface: never echo paths, commands,
    # recipients, bodies, selectors, IDs or arbitrary model arguments into it.
    del decision, kwargs
    try:
        from osia.tool_contracts import (
            effective_contract,
            get_tool_spec,
            safe_confirmation_summary,
        )

        spec = get_tool_spec(str(tool_name))
        contract = effective_contract(spec, {}) if spec is not None else None
        return safe_confirmation_summary(spec, contract)
    except Exception:
        return "Perform one sensitive action"


def _confirm_irreversible(
    decision: ActionDecision,
    tool_name: str,
    kwargs: dict,
) -> str | None:
    try:
        from osia.mcp_client.registry import (
            ConfirmationDomain,
            confirm_sensitive_action,
        )

        return confirm_sensitive_action(
            ConfirmationDomain.NATIVE,
            "osia",
            str(tool_name),
            _confirmation_summary(decision, str(tool_name), kwargs),
            call_arguments=kwargs,
        )
    except Exception:
        return "ERROR: action confirmation is unavailable; the action was not executed"


def _confirmation_with_permit(
    decision: ActionDecision,
    tool_name: str,
    kwargs: dict,
) -> str | None:
    context = _CURRENT_EXECUTION_CONTEXT.get()
    if (
        context is not None
        and context.execution_type == ExecutionType.AUTONOMOUS
        and pending_execution_permit_matches(tool_name, kwargs)
    ):
        # A fresh exact grant/Full-Access permit already passed the central
        # facade for this call.  Do not open a second ordinary dialog.
        return None
    blocked = _confirm_irreversible(decision, tool_name, kwargs)
    if blocked is not None:
        return blocked
    try:
        if ensure_confirmed_execution_permit(tool_name, kwargs):
            return None
    except Exception:
        pass
    return "ERROR: execution permit unavailable; the action was not executed"


def _explicit_central_authority(
    tool_name: str,
    kwargs: Mapping[str, Any],
) -> bool:
    """Mint a permit only for an exact grant or Full Access decision."""

    try:
        policy = get_default_permission_policy()
        request = build_permission_request(
            tool_name,
            kwargs,
            confirmation_domain="action",
            origin_id="osia",
            force_sensitive=True,
            policy=policy,
        )
        permission_decision = policy.evaluate(request)
        if (
            permission_decision.decision_type != DecisionType.ALLOW
            or permission_decision.authorization_source
            not in {AuthorizationSource.GRANT, AuthorizationSource.FULL_ACCESS}
        ):
            return False
        permit = policy.mint_permit(request, permission_decision)
        if permit is None:
            return False
        hold_execution_permit(permit, request, policy=policy)
        return True
    except Exception:
        return False


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


def _permission_request_for_deletion(
    tool_name: str,
    values: Mapping[str, Any],
    plan,
    *,
    confirmation_domain: str,
    origin_id: str,
    policy: "PermissionPolicy",
) -> "PermissionRequest":
    from osia.deletion_plan import DeletionPlan

    if not isinstance(plan, DeletionPlan):
        raise ValueError("untrusted_deletion_plan")
    context = make_execution_context(
        operation_id=plan.operation_id,
        store=policy.store,
    )
    classification = trusted_deletion_classification(
        system_destruction=plan.system_destruction,
        colossal_deletion=plan.colossal_deletion,
    )
    return build_permission_request(
        tool_name,
        values,
        confirmation_domain=confirmation_domain,
        origin_id=origin_id,
        execution_context=context,
        deletion_classification=classification,
        force_sensitive=True,
        policy=policy,
    )


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


def _authorize_colossal_plan(
    tool_name: str,
    values: Mapping[str, Any],
    plan,
    *,
    confirmation_domain: str,
    origin_id: str,
) -> str:
    policy = get_default_permission_policy()
    try:
        request = _permission_request_for_deletion(
            tool_name,
            values,
            plan,
            confirmation_domain=confirmation_domain,
            origin_id=origin_id,
            policy=policy,
        )
        decision = policy.evaluate(request)
    except Exception:
        return "ERROR: deletion classification unavailable; nothing was deleted"
    if decision.decision_type == DecisionType.BLOCK_SYSTEM_DESTRUCTION:
        return "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    if decision.decision_type != DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION:
        return permission_error_for_decision(decision)
    provider = get_reinforced_deletion_confirmation_provider()
    if provider is None:
        return "ERROR: REQUIRE_COLOSSAL_DELETION_CONFIRMATION"
    try:
        approved = provider(plan)
    except Exception:
        approved = False
    if approved is not True:
        return "ERROR: REQUIRE_COLOSSAL_DELETION_CONFIRMATION"
    permit = policy.mint_permit(
        request,
        decision,
        reinforced=True,
        ttl_seconds=max(0.1, plan.expires_at - time.time()),
    )
    if permit is None:
        return "ERROR: reinforced execution permit unavailable; nothing was deleted"
    hold_execution_permit(permit, request, policy=policy)
    held = _hold_plan_permit(
        tool_name,
        values,
        plan,
        policy_epoch=decision.policy_epoch,
        strength="reinforced",
    )
    return held or ""


def _universal_deletion_decision(
    tool_name: str,
    values: Mapping[str, Any],
    plan,
    *,
    confirmation_domain: str,
    origin_id: str,
) -> tuple[bool, str | None]:
    policy = get_default_permission_policy()
    try:
        request = _permission_request_for_deletion(
            tool_name,
            values,
            plan,
            confirmation_domain=confirmation_domain,
            origin_id=origin_id,
            policy=policy,
        )
        decision = policy.evaluate(request)
    except Exception:
        return True, "ERROR: deletion classification unavailable; nothing was deleted"
    if decision.decision_type == DecisionType.BLOCK_SYSTEM_DESTRUCTION:
        return True, "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    if decision.decision_type == DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION:
        result = _authorize_colossal_plan(
            tool_name,
            values,
            plan,
            confirmation_domain=confirmation_domain,
            origin_id=origin_id,
        )
        return True, result or None
    return False, None


def _finish_ordinary_deletion_authorization(
    tool_name: str,
    values: Mapping[str, Any],
    plan,
) -> str | None:
    if plan is None:
        return None
    try:
        epoch = get_default_permission_policy().store.load_state().policy_epoch
    except Exception:
        clear_pending_execution_permit()
        return "ERROR: deletion policy state unavailable; nothing was deleted"
    return _hold_plan_permit(
        tool_name,
        values,
        plan,
        policy_epoch=epoch,
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
        policy = get_default_permission_policy()
        request = build_permission_request(
            tool_name,
            values,
            deletion_classification=trusted_deletion_classification(
                system_destruction=True,
            ),
            force_sensitive=True,
            policy=policy,
        )
        decision = policy.evaluate(request)
        return permission_error_for_decision(decision)
    except Exception:
        # A recognized destructive semantic whose evidence construction failed
        # cannot be allowed to fall through to dispatch.
        if tool_name in {"write_file", "edit_file_lines", "rename_file"}:
            return "ERROR: BLOCK_SYSTEM_DESTRUCTION"
        if tool_name in {"bash", "run_command"} and re.search(
            r"(?i)(?:^|\s)(?:truncate|clear-content|move-item|remove-item|"
            r"format(?:-volume)?|remove-(?:partition|volume)|clear-disk|"
            r"mkfs|wipefs|sgdisk|fdisk|parted|lvremove|vgremove|pvremove|"
            r"reg(?:\.exe)?\s+delete)(?:\s|$)|(?<!>)>(?!>)",
            str(values.get("command") or ""),
        ):
            return "ERROR: BLOCK_SYSTEM_DESTRUCTION"
        return None


def system_mutation_barrier(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Public central facade used again at direct native I/O boundaries."""

    return _system_mutation_barrier(str(tool_name or ""), arguments)


def autonomous_permission_gate(
    tool_name: str,
    values: Mapping[str, Any],
    *,
    deletion_plan=None,
) -> str | None:
    """Evaluate every native Autonomous call through the central facade."""

    context = _CURRENT_EXECUTION_CONTEXT.get()
    if context is None or context.execution_type != ExecutionType.AUTONOMOUS:
        return None
    try:
        from osia.tool_contracts import (
            get_tool_spec,
            tool_is_eligible_in_current_surface,
        )

        spec = get_tool_spec(tool_name)
        if spec is not None and (
            not spec.autonomous_eligible
            or not tool_is_eligible_in_current_surface(spec)
        ):
            return "ERROR: tool is not eligible for Autonomous execution"
    except Exception:
        return "ERROR: autonomous tool eligibility unavailable"
    if tool_name.startswith("gmail_") or tool_name.startswith("mcp__"):
        # Their wrappers own the same facade and consume permits at the actual
        # remote dispatch boundary.
        return None
    if pending_execution_permit_matches(tool_name, values):
        return None
    policy = get_default_permission_policy()
    try:
        classification = TRUSTED_CLEAR_CLASSIFICATION
        if deletion_plan is not None:
            classification = trusted_deletion_classification(
                system_destruction=bool(deletion_plan.system_destruction),
                colossal_deletion=bool(deletion_plan.colossal_deletion),
            )
        request = build_permission_request(
            tool_name,
            values,
            confirmation_domain="action",
            origin_id="osia",
            deletion_classification=classification,
            policy=policy,
        )
        permission_decision = policy.evaluate(request)
        if permission_decision.decision_type != DecisionType.ALLOW:
            return permission_error_for_decision(permission_decision)
        if permission_decision.authorization_source == AuthorizationSource.NONE:
            return None
        permit = policy.mint_permit(request, permission_decision)
        if permit is None:
            return "ERROR: execution permit unavailable; the action was not executed"
        hold_execution_permit(permit, request, policy=policy)
        return None
    except Exception:
        try:
            from osia.autonomous.authorization import (
                suspend_authorization_unavailable,
            )

            suspend_authorization_unavailable("authorization_unavailable")
        except Exception:
            pass
        return "ERROR: autonomous authorization unavailable; the action was not executed"


def scheduled_permission_gate(
    tool_name: str,
    values: Mapping[str, Any],
    *,
    deletion_plan=None,
) -> str | None:
    """Evaluate every native Scheduled call through the central facade."""

    context = _CURRENT_EXECUTION_CONTEXT.get()
    if context is None or context.execution_type != ExecutionType.SCHEDULED:
        return None
    try:
        from osia.tool_contracts import get_tool_spec

        spec = get_tool_spec(tool_name)
        if spec is not None and not spec.scheduled_eligible:
            return "ERROR: tool is not eligible for Scheduled Task execution"
    except Exception:
        return "ERROR: scheduled tool eligibility unavailable"
    if tool_name.startswith("gmail_") or tool_name.startswith("mcp__"):
        # Their wrappers evaluate the same policy at the remote dispatch edge.
        return None
    if pending_execution_permit_matches(tool_name, values):
        return None
    policy = get_default_permission_policy()
    try:
        classification = TRUSTED_CLEAR_CLASSIFICATION
        if deletion_plan is not None:
            classification = trusted_deletion_classification(
                system_destruction=bool(deletion_plan.system_destruction),
                colossal_deletion=bool(deletion_plan.colossal_deletion),
            )
        request = build_permission_request(
            tool_name,
            values,
            confirmation_domain="action",
            origin_id="osia",
            deletion_classification=classification,
            policy=policy,
        )
        from osia import scheduled_tasks

        blocked = scheduled_tasks.validate_scheduled_permission_request(request)
        if blocked:
            scheduled_tasks.mark_scheduled_authorization_failure(
                context.scheduled_task_id, blocked,
            )
            return f"ERROR: scheduled authorization blocked: {blocked}"
        permission_decision = policy.evaluate(request)
        if permission_decision.decision_type != DecisionType.ALLOW:
            scheduled_tasks.mark_scheduled_authorization_failure(
                context.scheduled_task_id, permission_decision.reason_code,
            )
            return permission_error_for_decision(permission_decision)
        if permission_decision.authorization_source == AuthorizationSource.NONE:
            return None
        permit = policy.mint_permit(request, permission_decision)
        if permit is None:
            scheduled_tasks.mark_scheduled_authorization_failure(
                context.scheduled_task_id, "execution_permit_unavailable",
            )
            return "ERROR: execution permit unavailable; the action was not executed"
        hold_execution_permit(permit, request, policy=policy)
        return None
    except Exception:
        try:
            from osia import scheduled_tasks

            scheduled_tasks.mark_scheduled_authorization_failure(
                context.scheduled_task_id, "authorization_unavailable",
            )
        except Exception:
            pass
        return "ERROR: scheduled authorization unavailable; the action was not executed"


def guard_tool_call(user_request: str, tool_name: str, kwargs: dict) -> str | None:
    """Return an ``ERROR:`` string when a recognized action must not execute.

    Read-only and reversible tools pass unchanged.  The gate cannot broaden the
    model's permissions; it only blocks or confirms irreversible calls.
    """
    decision = classify_action_request(user_request)
    tool = str(tool_name or "")
    values = kwargs if isinstance(kwargs, dict) else {}

    system_barrier = _system_mutation_barrier(tool, values)
    if system_barrier is not None:
        return system_barrier

    # Product-policy blocks are absolute.  They must run before deletion
    # permits, Autonomous/Scheduled grants, Full Access, or any other source of
    # execution authority so no permission mode can broaden the policy.
    if decision.action_class == ActionClass.DISALLOWED:
        return f"ERROR: action blocked for safety: {decision.reason}"

    if tool == "activate_browser_element":
        try:
            from osia.tools.browser import prepare_browser_action_authorization

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
    # any permission source evaluates the request.  The returned fields are
    # opaque or safe metadata and replace any model-supplied binding values.
    if tool == "upload_browser_file":
        try:
            from osia.tools.browser import prepare_upload_authorization

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

    # Build trusted deletion evidence before any permission-mode decision.  A
    # direct deletion or exact shell deletion can proceed only with a complete
    # plan; non-deletion shell behavior falls through unchanged.
    deletion_plan = None
    if tool in {"delete_file", "delete_directory", "bash", "run_command"}:
        handled, deletion_plan, preflight_error = _deletion_preflight(tool, values)
        if preflight_error is not None:
            # Preserve the established confirmation contract for a missing
            # ordinary target.  No deletion permit is issued, and the native
            # boundary repeats preflight before any I/O, so the call still
            # fails closed if the target remains absent.
            if not preflight_error.endswith("(deletion_root_missing)"):
                return preflight_error
            deletion_plan = None
            handled = True
        if handled and deletion_plan is not None:
            final, barrier_error = _universal_deletion_decision(
                tool,
                values,
                deletion_plan,
                confirmation_domain="action",
                origin_id="osia",
            )
            if final:
                return barrier_error

    autonomous_error = autonomous_permission_gate(
        tool, values, deletion_plan=deletion_plan,
    )
    if autonomous_error is not None:
        return autonomous_error

    scheduled_error = scheduled_permission_gate(
        tool, values, deletion_plan=deletion_plan,
    )
    if scheduled_error is not None:
        return scheduled_error

    if tool in _LOCAL_CONFIRM_TOOLS:
        blocked = _confirmation_with_permit(decision, tool, values)
        if blocked is not None:
            return blocked
        return _finish_ordinary_deletion_authorization(
            tool, values, deletion_plan,
        )

    # Contract-driven gates complement the semantic final-step detector. Gmail
    # and MCP wrappers retain ownership of their established confirmation flow
    # so one call never creates two prompts.
    try:
        from osia.tool_contracts import (
            ConfirmationPolicy,
            effective_contract,
            get_tool_spec,
        )

        spec = get_tool_spec(tool)
        contract = effective_contract(spec, values, user_request) if spec else None
        requires_contract_confirmation = bool(
            contract is not None
            and contract.confirmation in {
                ConfirmationPolicy.CONFIRMATION_REQUIRED,
                ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION,
            }
        )
    except Exception:
        return "ERROR: action policy check failed; the action was not executed"
    wrapper_owns_confirmation = tool.startswith("gmail_") or tool.startswith("mcp__")
    if requires_contract_confirmation and not wrapper_owns_confirmation:
        if not _is_final_external_step(decision, tool, values):
            blocked = _confirmation_with_permit(decision, tool, values)
            if blocked is not None:
                return blocked
            return _finish_ordinary_deletion_authorization(
                tool, values, deletion_plan,
            )

    if not _is_final_external_step(decision, tool, values):
        return None
    if decision.outcome == ActionOutcome.CLARIFY:
        return (
            "ERROR: final external action not authorized: "
            f"{decision.reason}; ask the user for the missing detail"
        )
    if (
        decision.action_class == ActionClass.EXTERNAL_COMMUNICATION
        and not decision.final_send_requested
    ):
        return "ERROR: the user requested a draft, not the final Send action"
    if decision.outcome == ActionOutcome.BLOCK:
        return f"ERROR: action blocked for safety: {decision.reason}"
    blocked = _confirmation_with_permit(decision, tool, values)
    if blocked is not None:
        return blocked
    return _finish_ordinary_deletion_authorization(tool, values, deletion_plan)


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


# ---------------------------------------------------------------------------
# WOR-192 central typed permission core
# ---------------------------------------------------------------------------

PERMISSION_SCHEMA_VERSION = 1
GRANT_SCHEMA_VERSION = 1
PERMIT_SCHEMA_VERSION = 1
POLICY_VERSION = "wor192-phase1-v1"


class PermissionMode(str, Enum):
    ASK = "ask"
    TASK_SCOPED = "task_scoped"
    FULL_ACCESS = "full_access"


class ExecutionType(str, Enum):
    CHAT = "chat"
    AUTONOMOUS = "autonomous"
    SCHEDULED = "scheduled"


class DecisionType(str, Enum):
    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    REQUIRE_REINFORCED_CONFIRMATION = "require_reinforced_confirmation"
    SUSPEND_SCOPE_EXTENSION = "suspend_scope_extension"
    DENY = "deny"
    BLOCK_SYSTEM_DESTRUCTION = "block_system_destruction"
    REQUIRE_COLOSSAL_DELETION_CONFIRMATION = (
        "require_colossal_deletion_confirmation"
    )


class AuthorizationSource(str, Enum):
    NONE = "none"
    CONFIRMATION = "confirmation"
    GRANT = "grant"
    FULL_ACCESS = "full_access"


AUTOMATIC_PERMISSION_CATEGORIES = (
    "files_modify",
    "files_delete",
    "commands",
    "browser_account",
    "connectors",
    "send_publish_submit",
    "purchases_payments",
    "autonomous_actions",
    "scheduled_actions",
)

_EXACT_STRUCTURED_BROWSER_ACTIONS = frozenset({
    "navigate_browser", "activate_browser_element", "select_browser_tab",
    "close_browser_tab", "reload_browser_page", "browser_back",
    "browser_forward", "open_new_browser_tab", "return_to_browser_opener",
    "search_web", "open_search_result", "paginate_browser",
    "scroll_infinite", "handle_browser_dialog", "download_browser_file",
    "fill_form_field",
})


class UsePolicy(str, Enum):
    SINGLE = "single"
    BOUNDED = "bounded"
    RECURRING = "recurring"


def _validate_constraint_mapping(
    values: Mapping[str, Any], *, grant: bool,
) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        raise ValueError("invalid_permission_constraints")
    allowed = (
        {"allowed", "max_amount_minor", "currency", "scope_only"}
        if grant else
        {
            "allowed", "max_amount_minor", "amount_minor", "currency",
            "request_currency", "destination_origin", "connector_id",
        }
    )
    normalized = dict(values)
    if set(normalized) - allowed:
        raise ValueError("unknown_permission_constraint")
    if "allowed" in normalized and not isinstance(normalized["allowed"], bool):
        raise ValueError("invalid_allowed_constraint")
    if "scope_only" in normalized and not isinstance(
        normalized["scope_only"], bool
    ):
        raise ValueError("invalid_scope_only_constraint")
    for name in ("max_amount_minor", "amount_minor"):
        if name in normalized and (
            type(normalized[name]) is not int or normalized[name] < 0
        ):
            raise ValueError("invalid_amount_constraint")
    for name in ("currency", "request_currency"):
        if name in normalized and (
            not isinstance(normalized[name], str)
            or not re.fullmatch(r"[A-Za-z]{3}", normalized[name])
        ):
            raise ValueError("invalid_currency_constraint")
    if "destination_origin" in normalized:
        origin = normalized["destination_origin"]
        if not isinstance(origin, str):
            raise ValueError("invalid_destination_origin")
        try:
            canonical = json.loads(normalize_web_scope(
                origin, kind=WebScopeKind.EXACT_ORIGIN,
            ).private_canonical)["identity"]
        except (KeyError, TypeError, ValueError, ScopeNormalizationError) as exc:
            raise ValueError("invalid_destination_origin") from exc
        if canonical != origin:
            raise ValueError("noncanonical_destination_origin")
    if "connector_id" in normalized:
        connector_id = normalized["connector_id"]
        if (
            not isinstance(connector_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", connector_id)
        ):
            raise ValueError("invalid_connector_constraint")
    return normalized


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


_CLASSIFICATION_SEAL = object()


@dataclass(frozen=True, repr=False)
class TrustedDeletionClassification:
    system_destruction: bool
    colossal_deletion: bool
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _CLASSIFICATION_SEAL:
            raise ValueError("untrusted_deletion_classification")

    def __repr__(self) -> str:
        return (
            "TrustedDeletionClassification("
            f"system_destruction={self.system_destruction!r}, "
            f"colossal_deletion={self.colossal_deletion!r})"
        )


def trusted_deletion_classification(
    *,
    system_destruction: bool = False,
    colossal_deletion: bool = False,
) -> TrustedDeletionClassification:
    """Trusted planner boundary; free-form model labels are never accepted."""

    if not isinstance(system_destruction, bool) or not isinstance(
        colossal_deletion, bool
    ):
        raise ValueError("classification_must_be_boolean")
    return TrustedDeletionClassification(
        system_destruction=system_destruction,
        colossal_deletion=colossal_deletion,
        _seal=_CLASSIFICATION_SEAL,
    )


TRUSTED_CLEAR_CLASSIFICATION = trusted_deletion_classification()


@dataclass(frozen=True, repr=False)
class PermissionRequest:
    tool_identity: str
    confirmation_domain: str
    tool_spec: Any = field(repr=False)
    effective_contract: Any = field(repr=False)
    capability: str
    private_scope: NormalizedScope = field(repr=False)
    safe_category: str
    effect_class: str
    normalized_call_hash: str = field(repr=False)
    deletion_classification: TrustedDeletionClassification
    operation_id: str
    execution_context: ExecutionContext
    tool_category: str
    optional_constraints: Mapping[str, Any] = field(default_factory=dict, repr=False)
    ambiguous_legacy_identity: bool = False
    autonomous_equivalence_hash: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        from osia.tool_contracts import EffectiveToolContract, ToolSpec

        if not isinstance(self.tool_spec, ToolSpec):
            raise ValueError("invalid_tool_spec")
        if not isinstance(self.effective_contract, EffectiveToolContract):
            raise ValueError("invalid_effective_tool_contract")
        if self.effective_contract.spec.name != self.tool_spec.name:
            raise ValueError("tool_contract_mismatch")
        if self.tool_identity != self.tool_spec.name:
            raise ValueError("tool_identity_mismatch")
        if self.capability != self.tool_spec.capability:
            raise ValueError("capability_mismatch")
        if self.effect_class != self.effective_contract.side_effect.value:
            raise ValueError("effect_class_mismatch")
        if self.safe_category != self.private_scope.safe_category:
            raise ValueError("safe_category_mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", self.normalized_call_hash):
            raise ValueError("invalid_call_hash")
        if not isinstance(
            self.deletion_classification, TrustedDeletionClassification
        ):
            raise ValueError("untrusted_deletion_classification")
        if self.operation_id != self.execution_context.operation_id:
            raise ValueError("operation_context_mismatch")
        _require_opaque_ref(self.operation_id)
        _require_opaque_ref(self.confirmation_domain)
        _require_opaque_ref(self.tool_category)
        _validate_constraint_mapping(self.optional_constraints, grant=False)
        if not isinstance(self.ambiguous_legacy_identity, bool):
            raise ValueError("invalid_legacy_identity_state")
        if self.autonomous_equivalence_hash and not re.fullmatch(
            r"[0-9a-f]{64}", self.autonomous_equivalence_hash,
        ):
            raise ValueError("invalid_autonomous_equivalence_hash")

    @property
    def system_destruction(self) -> bool:
        return self.deletion_classification.system_destruction

    @property
    def colossal_deletion(self) -> bool:
        return self.deletion_classification.colossal_deletion

    @property
    def scope_hash(self) -> str:
        return self.private_scope.scope_hash

    def public_metadata(self) -> dict[str, Any]:
        return {
            "tool_identity": self.tool_identity,
            "confirmation_domain": self.confirmation_domain,
            "capability": self.capability,
            "safe_category": self.safe_category,
            "effect_class": self.effect_class,
            "operation_id": self.operation_id,
            "execution_type": self.execution_context.execution_type.value,
        }


def automatic_permission_category(request: PermissionRequest) -> str | None:
    """Map one request from authoritative execution and tool metadata only."""

    from osia.tool_contracts import SideEffectClass

    if not isinstance(request, PermissionRequest):
        raise ValueError("invalid_permission_request")
    if request.execution_context.execution_type == ExecutionType.SCHEDULED:
        return "scheduled_actions"
    if request.execution_context.execution_type == ExecutionType.AUTONOMOUS:
        return "autonomous_actions"
    if request.safe_category == "purchase" or any(
        name in request.optional_constraints
        for name in ("amount_minor", "request_currency")
    ):
        return "purchases_payments"
    if request.tool_identity in {"delete_file", "delete_directory"}:
        return "files_delete"
    if request.tool_category == "shell":
        return "commands"
    if request.effect_class == SideEffectClass.IRREVERSIBLE_EXTERNAL.value:
        return "send_publish_submit"
    if (
        request.tool_category in {"gmail", "mcp"}
        and request.effect_class != SideEffectClass.READ_ONLY.value
    ):
        return "connectors"
    if (
        request.tool_category == "browser"
        and request.effect_class != SideEffectClass.READ_ONLY.value
    ):
        return "browser_account"
    if (
        request.tool_category == "filesystem"
        and request.effect_class != SideEffectClass.READ_ONLY.value
    ):
        return "files_modify"
    return None


def _automatic_permission_enabled(
    state: PermissionState, request: PermissionRequest,
) -> bool:
    category = automatic_permission_category(request)
    if category is None:
        return False
    try:
        matrix = state.optional_settings.get("automatic_categories")
    except (AttributeError, TypeError, ValueError):
        return False
    if matrix is None:
        return state.permission_mode == PermissionMode.FULL_ACCESS
    return isinstance(matrix, Mapping) and matrix.get(category) is True


@dataclass(frozen=True)
class PermissionDecision:
    decision_type: DecisionType
    reason_code: str
    authorization_source: AuthorizationSource
    grant_ref: str
    policy_epoch: int
    execution_type: ExecutionType
    safe_category: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision_type, DecisionType):
            raise ValueError("invalid_decision_type")
        if not isinstance(self.authorization_source, AuthorizationSource):
            raise ValueError("invalid_authorization_source")
        if not isinstance(self.execution_type, ExecutionType):
            raise ValueError("invalid_execution_type")
        _require_opaque_ref(self.reason_code)
        _require_opaque_ref(self.grant_ref, optional=True)
        if type(self.policy_epoch) is not int or self.policy_epoch < 0:
            raise ValueError("invalid_policy_epoch")


@dataclass(frozen=True, repr=False)
class Grant:
    schema_version: int
    grant_id: str
    permission_mode: PermissionMode
    execution_type: ExecutionType
    owner_ref: str
    machine_ref: str
    device_ref: str
    run_id: str
    scheduled_task_id: str
    task_revision: str
    plan_revision: str
    tool_category: str
    effect_class: str
    capability: str
    normalized_target_scope: NormalizedScope = field(repr=False)
    constraints_canonical: str = field(repr=False)
    issued_at: float
    expires_at: float | None
    persistent: bool
    use_policy: UsePolicy
    max_uses: int | None
    use_count: int
    revocation_epoch: int
    policy_version: str
    scope_hash: str = field(repr=False)
    call_hash: str = field(repr=False)
    identity_version: str
    integrity_metadata: str = field(repr=False)
    last_use_metadata_canonical: str = field(default="{}", repr=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != GRANT_SCHEMA_VERSION:
            raise ValueError("unsupported_grant_schema")
        if not isinstance(self.permission_mode, PermissionMode):
            raise ValueError("invalid_grant_permission_mode")
        if self.permission_mode != PermissionMode.TASK_SCOPED:
            raise ValueError("grant_must_be_task_scoped")
        if not isinstance(self.execution_type, ExecutionType):
            raise ValueError("invalid_grant_execution_type")
        if not isinstance(self.use_policy, UsePolicy):
            raise ValueError("invalid_use_policy")
        if not isinstance(self.persistent, bool):
            raise ValueError("invalid_grant_persistence")
        for field_name in (
            "grant_id", "owner_ref", "machine_ref", "tool_category",
            "effect_class", "identity_version",
        ):
            _require_opaque_ref(getattr(self, field_name))
        if (
            not isinstance(self.capability, str)
            or not self.capability
            or len(self.capability) > 512
            or any(ord(ch) < 32 for ch in self.capability)
        ):
            raise ValueError("invalid_capability")
        for field_name in (
            "device_ref", "run_id", "scheduled_task_id", "task_revision",
            "plan_revision",
        ):
            _require_opaque_ref(getattr(self, field_name), optional=True)
        if (
            type(self.issued_at) is not float
            or not math.isfinite(self.issued_at)
        ):
            raise ValueError("invalid_grant_issue_time")
        if self.expires_at is not None and (
            type(self.expires_at) is not float
            or not math.isfinite(self.expires_at)
        ):
            raise ValueError("invalid_grant_expiration")
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise ValueError("invalid_grant_expiration")
        if (
            type(self.use_count) is not int
            or type(self.revocation_epoch) is not int
            or self.use_count < 0
            or self.revocation_epoch < 0
        ):
            raise ValueError("invalid_grant_counter")
        if self.max_uses is not None and type(self.max_uses) is not int:
            raise ValueError("invalid_grant_max_uses")
        if self.use_policy == UsePolicy.SINGLE and self.max_uses != 1:
            raise ValueError("single_use_requires_one")
        if self.use_policy == UsePolicy.BOUNDED and (
            self.max_uses is None or self.max_uses < 1
        ):
            raise ValueError("bounded_use_requires_max")
        if self.use_policy == UsePolicy.RECURRING and self.max_uses is not None:
            raise ValueError("recurring_use_has_no_max")
        if self.max_uses is not None and self.use_count > self.max_uses:
            raise ValueError("grant_use_count_exceeded")
        if not sensitive_hash_equal(self.scope_hash, self.normalized_target_scope.scope_hash):
            raise ValueError("grant_scope_hash_mismatch")
        for value, code in (
            (self.call_hash, "invalid_grant_call_hash"),
            (self.integrity_metadata, "invalid_grant_integrity"),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(code)
        _validate_constraint_mapping(
            json_loads_canonical(self.constraints_canonical), grant=True,
        )
        canonical_serialize(json_loads_canonical(self.last_use_metadata_canonical))

    @property
    def constraints(self) -> dict[str, Any]:
        return json_loads_canonical(self.constraints_canonical)

    @property
    def exhausted(self) -> bool:
        return self.max_uses is not None and self.use_count >= self.max_uses

    def _integrity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "grant_id": self.grant_id,
            "permission_mode": self.permission_mode.value,
            "execution_type": self.execution_type.value,
            "owner_ref": self.owner_ref,
            "machine_ref": self.machine_ref,
            "device_ref": self.device_ref,
            "run_id": self.run_id,
            "scheduled_task_id": self.scheduled_task_id,
            "task_revision": self.task_revision,
            "plan_revision": self.plan_revision,
            "tool_category": self.tool_category,
            "effect_class": self.effect_class,
            "capability": self.capability,
            "normalized_target_scope": self.normalized_target_scope.protected_payload(),
            "constraints": self.constraints,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "persistent": self.persistent,
            "use_policy": self.use_policy.value,
            "max_uses": self.max_uses,
            "use_count": self.use_count,
            "revocation_epoch": self.revocation_epoch,
            "policy_version": self.policy_version,
            "scope_hash": self.scope_hash,
            "call_hash": self.call_hash,
            "identity_version": self.identity_version,
            "last_use_metadata": json_loads_canonical(
                self.last_use_metadata_canonical
            ),
        }

    def integrity_is_valid(self) -> bool:
        return sensitive_hash_equal(
            self.integrity_metadata, canonical_hash(self._integrity_payload())
        )

    def to_protected_dict(self) -> dict[str, Any]:
        payload = self._integrity_payload()
        payload["integrity_metadata"] = self.integrity_metadata
        return payload

    def public_metadata(self) -> dict[str, Any]:
        return {
            "grant_ref": self.grant_id,
            "execution_type": self.execution_type.value,
            "tool_category": self.tool_category,
            "effect_class": self.effect_class,
            "capability": self.capability,
            "expires_at": self.expires_at,
            "persistent": self.persistent,
            "use_policy": self.use_policy.value,
            "max_uses": self.max_uses,
            "use_count": self.use_count,
            "revocation_epoch": self.revocation_epoch,
            "policy_version": self.policy_version,
            "safe_category": self.normalized_target_scope.safe_category,
        }

    def __repr__(self) -> str:
        return (
            "Grant("
            f"grant_id={self.grant_id!r}, execution_type={self.execution_type!r}, "
            f"tool_category={self.tool_category!r}, capability={self.capability!r}, "
            f"safe_category={self.normalized_target_scope.safe_category!r}, "
            f"persistent={self.persistent!r}, use_count={self.use_count!r})"
        )

    @classmethod
    def from_protected_dict(cls, payload: Mapping[str, Any]) -> "Grant":
        try:
            if not isinstance(payload, Mapping):
                raise TypeError("grant_not_mapping")
            text_fields = (
                "grant_id", "permission_mode", "execution_type", "owner_ref",
                "machine_ref", "tool_category", "effect_class", "capability",
                "use_policy", "policy_version", "scope_hash", "call_hash",
                "identity_version", "integrity_metadata",
            )
            optional_text_fields = (
                "device_ref", "run_id", "scheduled_task_id", "task_revision",
                "plan_revision",
            )
            if not all(isinstance(payload.get(name), str) for name in text_fields):
                raise TypeError("grant_text_field_invalid")
            if not all(
                name not in payload or isinstance(payload[name], str)
                for name in optional_text_fields
            ):
                raise TypeError("grant_optional_text_field_invalid")
            for name in ("schema_version", "use_count", "revocation_epoch"):
                if type(payload.get(name)) is not int:
                    raise TypeError("grant_integer_field_invalid")
            if payload.get("max_uses") is not None and type(
                payload.get("max_uses")
            ) is not int:
                raise TypeError("grant_max_uses_invalid")
            if type(payload.get("persistent")) is not bool:
                raise TypeError("grant_persistent_invalid")
            for name in ("issued_at", "expires_at"):
                if payload.get(name) is not None and (
                    isinstance(payload.get(name), bool)
                    or not isinstance(payload.get(name), (int, float))
                ):
                    raise TypeError("grant_time_field_invalid")
            if not isinstance(payload.get("constraints", {}), Mapping):
                raise TypeError("grant_constraints_invalid")
            if not isinstance(payload.get("last_use_metadata", {}), Mapping):
                raise TypeError("grant_last_use_invalid")
            grant = cls(
                schema_version=int(payload["schema_version"]),
                grant_id=str(payload["grant_id"]),
                permission_mode=PermissionMode(payload["permission_mode"]),
                execution_type=ExecutionType(payload["execution_type"]),
                owner_ref=str(payload["owner_ref"]),
                machine_ref=str(payload["machine_ref"]),
                device_ref=str(payload.get("device_ref", "")),
                run_id=str(payload.get("run_id", "")),
                scheduled_task_id=str(payload.get("scheduled_task_id", "")),
                task_revision=str(payload.get("task_revision", "")),
                plan_revision=str(payload.get("plan_revision", "")),
                tool_category=str(payload["tool_category"]),
                effect_class=str(payload["effect_class"]),
                capability=str(payload["capability"]),
                normalized_target_scope=normalized_scope_from_protected(
                    payload["normalized_target_scope"]
                ),
                constraints_canonical=canonical_serialize(payload.get("constraints", {})),
                issued_at=float(payload["issued_at"]),
                expires_at=(
                    None if payload.get("expires_at") is None
                    else float(payload["expires_at"])
                ),
                persistent=bool(payload["persistent"]),
                use_policy=UsePolicy(payload["use_policy"]),
                max_uses=(
                    None if payload.get("max_uses") is None
                    else int(payload["max_uses"])
                ),
                use_count=int(payload["use_count"]),
                revocation_epoch=int(payload["revocation_epoch"]),
                policy_version=str(payload["policy_version"]),
                scope_hash=str(payload["scope_hash"]),
                call_hash=str(payload["call_hash"]),
                identity_version=str(payload["identity_version"]),
                integrity_metadata=str(payload["integrity_metadata"]),
                last_use_metadata_canonical=canonical_serialize(
                    payload.get("last_use_metadata", {})
                ),
            )
        except (KeyError, TypeError, ValueError, ScopeNormalizationError) as exc:
            raise ValueError("corrupt_grant") from exc
        if not grant.integrity_is_valid():
            raise ValueError("corrupt_grant")
        return grant


def json_loads_canonical(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_canonical_object") from exc
    if not isinstance(decoded, dict) or canonical_serialize(decoded) != value:
        raise ValueError("invalid_canonical_object")
    return decoded


def create_grant(
    request: PermissionRequest,
    *,
    use_policy: UsePolicy = UsePolicy.SINGLE,
    max_uses: int | None = None,
    expires_at: float | None = None,
    persistent: bool = False,
    constraints: Mapping[str, Any] | None = None,
    issued_at: float | None = None,
) -> Grant:
    if not request.private_scope.grantable:
        raise ValueError("scope_not_grantable")
    now = time.time() if issued_at is None else float(issued_at)
    if use_policy == UsePolicy.SINGLE:
        max_uses = 1
    elif use_policy == UsePolicy.RECURRING:
        max_uses = None
    grant = Grant(
        schema_version=GRANT_SCHEMA_VERSION,
        grant_id=uuid.uuid4().hex,
        permission_mode=PermissionMode.TASK_SCOPED,
        execution_type=request.execution_context.execution_type,
        owner_ref=request.execution_context.owner_ref,
        machine_ref=request.execution_context.machine_ref,
        device_ref=request.execution_context.device_ref,
        run_id=request.execution_context.run_id,
        scheduled_task_id=request.execution_context.scheduled_task_id,
        task_revision=request.execution_context.task_revision,
        plan_revision=request.execution_context.plan_revision,
        tool_category=request.tool_category,
        effect_class=request.effect_class,
        capability=request.capability,
        normalized_target_scope=request.private_scope,
        constraints_canonical=canonical_serialize(
            _validate_constraint_mapping(constraints or {}, grant=True)
        ),
        issued_at=now,
        expires_at=None if expires_at is None else float(expires_at),
        persistent=bool(persistent),
        use_policy=use_policy,
        max_uses=max_uses,
        use_count=0,
        revocation_epoch=request.execution_context.policy_epoch,
        policy_version=POLICY_VERSION,
        scope_hash=request.scope_hash,
        call_hash=request.normalized_call_hash,
        identity_version="opaque-v1",
        integrity_metadata="0" * 64,
        last_use_metadata_canonical="{}",
    )
    return replace(grant, integrity_metadata=canonical_hash(grant._integrity_payload()))


@dataclass(frozen=True)
class PermissionState:
    permission_schema_version: int = PERMISSION_SCHEMA_VERSION
    permission_mode: PermissionMode = PermissionMode.ASK
    policy_epoch: int = 0
    policy_version: str = POLICY_VERSION
    grants: tuple[Grant, ...] = ()
    optional_settings_canonical: str = "{}"
    migration_marker: str = "wor192-safe-default-v1"

    def __post_init__(self) -> None:
        if (
            type(self.permission_schema_version) is not int
            or self.permission_schema_version != PERMISSION_SCHEMA_VERSION
        ):
            raise ValueError("unsupported_permission_schema")
        if not isinstance(self.permission_mode, PermissionMode):
            raise ValueError("invalid_permission_mode")
        if type(self.policy_epoch) is not int or self.policy_epoch < 0:
            raise ValueError("invalid_policy_epoch")
        if self.policy_version != POLICY_VERSION:
            raise ValueError("unsupported_policy_version")
        if not all(isinstance(grant, Grant) for grant in self.grants):
            raise ValueError("invalid_grant_collection")
        json_loads_canonical(self.optional_settings_canonical)
        _require_opaque_ref(self.migration_marker)

    @property
    def optional_settings(self) -> dict[str, Any]:
        value = json_loads_canonical(self.optional_settings_canonical)
        if not isinstance(value, dict):
            raise ValueError("invalid_permission_settings")
        return value

    def to_protected_dict(self) -> dict[str, Any]:
        return {
            "permission_schema_version": self.permission_schema_version,
            "permission_mode": self.permission_mode.value,
            "policy_epoch": self.policy_epoch,
            "policy_version": self.policy_version,
            "grants": [grant.to_protected_dict() for grant in self.grants],
            "optional_settings": json_loads_canonical(
                self.optional_settings_canonical
            ),
            "migration_marker": self.migration_marker,
        }

    def public_metadata(self) -> dict[str, Any]:
        return {
            "permission_schema_version": self.permission_schema_version,
            "permission_mode": self.permission_mode.value,
            "policy_epoch": self.policy_epoch,
            "policy_version": self.policy_version,
            "grant_count": len(self.grants),
            "migration_marker": self.migration_marker,
        }

    @classmethod
    def from_protected_dict(cls, payload: Mapping[str, Any]) -> "PermissionState":
        try:
            if not isinstance(payload, Mapping):
                raise TypeError("permission_state_not_mapping")
            if type(payload.get("permission_schema_version")) is not int:
                raise TypeError("permission_schema_invalid")
            if type(payload.get("policy_epoch")) is not int:
                raise TypeError("permission_epoch_invalid")
            for name in (
                "permission_mode", "policy_version", "migration_marker",
            ):
                if not isinstance(payload.get(name), str):
                    raise TypeError("permission_text_field_invalid")
            if not isinstance(payload.get("grants", []), list):
                raise TypeError("permission_grants_invalid")
            if not isinstance(payload.get("optional_settings", {}), Mapping):
                raise TypeError("permission_settings_invalid")
            return cls(
                permission_schema_version=int(payload["permission_schema_version"]),
                permission_mode=PermissionMode(payload["permission_mode"]),
                policy_epoch=int(payload["policy_epoch"]),
                policy_version=str(payload["policy_version"]),
                grants=tuple(
                    Grant.from_protected_dict(item)
                    for item in payload.get("grants", [])
                ),
                optional_settings_canonical=canonical_serialize(
                    payload.get("optional_settings", {})
                ),
                migration_marker=str(payload["migration_marker"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("corrupt_permission_state") from exc


@dataclass(frozen=True)
class ExecutionPermit:
    schema_version: int
    permit_id: str
    execution_id: str
    operation_id: str
    tool_identity: str
    normalized_call_hash: str = field(repr=False)
    scope_hash: str = field(repr=False)
    confirmation_domain: str
    authorization_source: AuthorizationSource
    grant_ref: str
    policy_epoch: int
    issued_at: float
    expires_at: float
    used: bool
    required_confirmation_strength: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != PERMIT_SCHEMA_VERSION:
            raise ValueError("unsupported_permit_schema")
        for field_name in (
            "permit_id", "execution_id", "operation_id", "confirmation_domain",
            "required_confirmation_strength",
        ):
            _require_opaque_ref(getattr(self, field_name))
        _require_opaque_ref(self.grant_ref, optional=True)
        if not isinstance(self.authorization_source, AuthorizationSource):
            raise ValueError("invalid_authorization_source")
        if not isinstance(self.used, bool):
            raise ValueError("invalid_permit_use_state")
        if self.authorization_source == AuthorizationSource.NONE:
            raise ValueError("permit_requires_authority")
        if (
            type(self.issued_at) is not float
            or not math.isfinite(self.issued_at)
            or type(self.expires_at) is not float
            or not math.isfinite(self.expires_at)
            or self.expires_at <= self.issued_at
        ):
            raise ValueError("invalid_permit_expiration")
        if type(self.policy_epoch) is not int or self.policy_epoch < 0:
            raise ValueError("invalid_policy_epoch")


class PermitError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ExecutionPermitLedger:
    """Process-local exact permits with atomic, fail-closed consumption."""

    def __init__(self, *, clock=None, maximum: int = 4096):
        self._clock = clock or time.time
        self._maximum = max(1, min(int(maximum), 16384))
        self._lock = threading.Lock()
        self._permits: dict[str, ExecutionPermit] = {}
        self._operations: dict[str, str] = {}

    def operation_is_duplicate(self, operation_id: str) -> bool:
        with self._lock:
            return self._operations.get(operation_id) in {
                "pending", "dispatched", "outcome_unknown", "cancelled",
            }

    def issue(
        self,
        request: PermissionRequest,
        *,
        authorization_source: AuthorizationSource,
        grant_ref: str = "",
        confirmation_strength: str = "ordinary",
        ttl_seconds: float = 30.0,
    ) -> ExecutionPermit:
        ttl = max(0.1, min(float(ttl_seconds), 120.0))
        now = float(self._clock())
        with self._lock:
            self._purge_locked(now)
            if self._operations.get(request.operation_id) in {
                "pending", "dispatched", "outcome_unknown", "cancelled",
            }:
                raise PermitError("duplicate_operation")
            permit = ExecutionPermit(
                schema_version=PERMIT_SCHEMA_VERSION,
                permit_id=secrets.token_hex(24),
                execution_id=request.execution_context.execution_id,
                operation_id=request.operation_id,
                tool_identity=request.tool_identity,
                normalized_call_hash=request.normalized_call_hash,
                scope_hash=request.scope_hash,
                confirmation_domain=request.confirmation_domain,
                authorization_source=authorization_source,
                grant_ref=grant_ref,
                policy_epoch=request.execution_context.policy_epoch,
                issued_at=now,
                expires_at=now + ttl,
                used=False,
                required_confirmation_strength=confirmation_strength,
            )
            self._permits[permit.permit_id] = permit
            self._operations[request.operation_id] = "pending"
            while len(self._permits) > self._maximum:
                old_id = next(iter(self._permits))
                old = self._permits.pop(old_id)
                self._operations[old.operation_id] = "cancelled"
            return permit

    def consume(
        self,
        permit_id: str,
        request: PermissionRequest,
        *,
        current_policy_epoch: int,
    ) -> ExecutionPermit:
        now = float(self._clock())
        with self._lock:
            permit = self._permits.pop(str(permit_id or ""), None)
            if permit is None:
                raise PermitError("permit_missing_or_used")
            # Removal is deliberately first: every failed validation burns the
            # permit and cannot authorize a later or altered dispatch.
            self._operations[permit.operation_id] = "cancelled"
            if permit.expires_at <= now:
                raise PermitError("permit_expired")
            if permit.used:
                raise PermitError("permit_already_used")
            if permit.policy_epoch != current_policy_epoch:
                raise PermitError("permit_epoch_changed")
            exact = (
                permit.execution_id == request.execution_context.execution_id
                and permit.operation_id == request.operation_id
                and permit.tool_identity == request.tool_identity
                and permit.confirmation_domain == request.confirmation_domain
                and sensitive_hash_equal(
                    permit.normalized_call_hash, request.normalized_call_hash
                )
                and sensitive_hash_equal(permit.scope_hash, request.scope_hash)
            )
            if not exact:
                raise PermitError("permit_scope_mismatch")
            consumed = replace(permit, used=True)
            self._operations[permit.operation_id] = "dispatched"
            return consumed

    def invalidate_for_epoch(self, current_epoch: int) -> int:
        with self._lock:
            stale = [
                permit_id for permit_id, permit in self._permits.items()
                if permit.policy_epoch != current_epoch
            ]
            for permit_id in stale:
                permit = self._permits.pop(permit_id)
                self._operations[permit.operation_id] = "cancelled"
            return len(stale)

    def cancel(self, permit_id: str) -> bool:
        with self._lock:
            permit = self._permits.pop(str(permit_id or ""), None)
            if permit is None:
                return False
            self._operations[permit.operation_id] = "cancelled"
            return True

    def mark_outcome_unknown(self, operation_id: str) -> None:
        with self._lock:
            if self._operations.get(operation_id) == "dispatched":
                self._operations[operation_id] = "outcome_unknown"

    def _purge_locked(self, now: float) -> None:
        for permit_id, permit in tuple(self._permits.items()):
            if permit.expires_at <= now:
                self._permits.pop(permit_id, None)
                self._operations[permit.operation_id] = "cancelled"


def _decision(
    request: PermissionRequest,
    decision_type: DecisionType,
    reason_code: str,
    *,
    source: AuthorizationSource = AuthorizationSource.NONE,
    grant_ref: str = "",
    epoch: int | None = None,
) -> PermissionDecision:
    decision = PermissionDecision(
        decision_type=decision_type,
        reason_code=reason_code,
        authorization_source=source,
        grant_ref=grant_ref,
        policy_epoch=(
            request.execution_context.policy_epoch if epoch is None else epoch
        ),
        execution_type=request.execution_context.execution_type,
        safe_category=request.safe_category,
    )
    try:
        from osia.autonomous.authorization import observe_permission_decision

        observe_permission_decision(request, decision)
    except Exception:
        # Authorization observability can only narrow execution.  A checkpoint
        # failure must not broaden this deterministic decision.
        pass
    return decision


class PermissionPolicy:
    """The single deterministic evaluator and permit facade."""

    def __init__(
        self,
        store: "PermissionStore",
        *,
        permit_ledger: ExecutionPermitLedger | None = None,
        clock=None,
    ):
        self.store = store
        self.clock = clock or time.time
        self.permit_ledger = permit_ledger or ExecutionPermitLedger(clock=self.clock)
        self._mint_lock = threading.Lock()
        try:
            self.store.add_epoch_listener(self.permit_ledger.invalidate_for_epoch)
        except AttributeError:
            pass

    def _journal(
        self,
        request: PermissionRequest,
        event_code: str,
        result: str,
        *,
        authorization_source: AuthorizationSource = AuthorizationSource.NONE,
    ) -> None:
        try:
            self.store.append_journal_event(
                event_code,
                execution_type=request.execution_context.execution_type.value,
                safe_category=request.safe_category,
                authorization_source=authorization_source.value,
                result=result,
            )
        except Exception:
            pass

    def evaluate(self, request: PermissionRequest) -> PermissionDecision:
        from osia.tool_contracts import ConfirmationPolicy, SideEffectClass

        if not isinstance(request, PermissionRequest):
            raise ValueError("invalid_permission_request")

        # Universal barriers precede every mode, grant and optional constraint.
        if request.system_destruction:
            self._journal(
                request, "universal_barrier_blocked", "system_destruction",
            )
            return _decision(
                request,
                DecisionType.BLOCK_SYSTEM_DESTRUCTION,
                "system_destruction_absolute",
            )
        if request.colossal_deletion:
            self._journal(
                request, "authorization_required", "colossal_deletion_review",
            )
            return _decision(
                request,
                DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION,
                "colossal_deletion_reinforced_approval",
            )

        try:
            from osia.autonomous.authorization import request_block_reason

            durable_block = request_block_reason(request)
        except Exception:
            # A broken Autonomous binding must fail closed, but this optional
            # coordinator must never deny ordinary interactive chat.
            durable_block = (
                "authorization_state_unavailable"
                if request.execution_context.execution_type
                == ExecutionType.AUTONOMOUS
                else None
            )
        if durable_block:
            self._journal(request, "authorization_required", durable_block)
            return _decision(request, DecisionType.DENY, durable_block)

        if request.execution_context.execution_type == ExecutionType.SCHEDULED:
            try:
                from osia import scheduled_tasks

                scheduled_block = (
                    scheduled_tasks.validate_scheduled_permission_request(request)
                    if scheduled_tasks.is_bound_scheduled_context(
                        request.execution_context
                    ) else None
                )
            except Exception:
                scheduled_block = "authorization_state_unavailable"
            if scheduled_block:
                try:
                    scheduled_tasks.mark_scheduled_authorization_failure(
                        request.execution_context.scheduled_task_id,
                        scheduled_block,
                    )
                except Exception:
                    pass
                self._journal(
                    request, "authorization_required", scheduled_block,
                )
                return _decision(request, DecisionType.DENY, scheduled_block)

        state = self.store.load_state()
        epoch = state.policy_epoch
        if request.execution_context.policy_epoch != epoch:
            return _decision(
                request, DecisionType.DENY, "policy_epoch_mismatch", epoch=epoch,
            )

        automatic_authority = _automatic_permission_enabled(state, request)
        if (
            automatic_authority
            and not _full_access_constraints_allow(request, state)
        ):
            self._journal(
                request, "authorization_required", "optional_constraint_failed",
                authorization_source=AuthorizationSource.FULL_ACCESS,
            )
            return _decision(
                request, DecisionType.DENY, "optional_constraint_failed",
                epoch=epoch,
            )

        grant, grant_reason = self.store.find_matching_grant(
            request, now=self.clock()
        )
        if grant is not None:
            if not _optional_constraints_allow(request, grant.constraints):
                if not automatic_authority:
                    return _decision(
                        request, DecisionType.DENY, "optional_constraint_failed",
                        epoch=epoch,
                    )
            else:
                if self.permit_ledger.operation_is_duplicate(request.operation_id):
                    return _decision(
                        request, DecisionType.DENY,
                        "duplicate_or_ambiguous_operation",
                        epoch=epoch,
                    )
                return _decision(
                    request, DecisionType.ALLOW, "exact_grant",
                    source=AuthorizationSource.GRANT,
                    grant_ref=grant.grant_id,
                    epoch=epoch,
                )

        if automatic_authority:
            if (
                not _optional_constraints_allow(request, {})
            ):
                self._journal(
                    request, "authorization_required", "optional_constraint_failed",
                    authorization_source=AuthorizationSource.FULL_ACCESS,
                )
                return _decision(
                    request, DecisionType.DENY, "optional_constraint_failed",
                    epoch=epoch,
                )
            if self.permit_ledger.operation_is_duplicate(request.operation_id):
                return _decision(
                    request, DecisionType.DENY, "duplicate_or_ambiguous_operation",
                    epoch=epoch,
                )
            return _decision(
                request, DecisionType.ALLOW, (
                    "full_access" if state.permission_mode == PermissionMode.FULL_ACCESS
                    else "automatic_category"
                ),
                source=AuthorizationSource.FULL_ACCESS,
                epoch=epoch,
            )

        if self.permit_ledger.operation_is_duplicate(request.operation_id):
            return _decision(
                request, DecisionType.DENY, "duplicate_or_ambiguous_operation",
                epoch=epoch,
            )

        if request.effective_contract.confirmation == ConfirmationPolicy.DENIED_BY_POLICY:
            return _decision(
                request, DecisionType.DENY, "tool_contract_denied", epoch=epoch,
            )

        sensitive = request.effective_contract.confirmation in {
            ConfirmationPolicy.CONFIRMATION_REQUIRED,
            ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION,
        }
        if (
            request.execution_context.execution_type
            in {ExecutionType.AUTONOMOUS, ExecutionType.SCHEDULED}
            and request.effective_contract.side_effect != SideEffectClass.READ_ONLY
        ):
            sensitive = True
        if not sensitive or (
            request.effective_contract.side_effect == SideEffectClass.READ_ONLY
            and request.effective_contract.confirmation
            == ConfirmationPolicy.NO_CONFIRMATION
        ):
            return _decision(
                request, DecisionType.ALLOW, "structurally_safe",
                epoch=epoch,
            )

        if not request.execution_context.interactive:
            decision_type = (
                DecisionType.SUSPEND_SCOPE_EXTENSION
                if request.execution_context.execution_type
                in {ExecutionType.AUTONOMOUS, ExecutionType.SCHEDULED}
                else DecisionType.DENY
            )
            reason = grant_reason if grant_reason != "no_grant" else (
                "scope_extension_required"
                if decision_type == DecisionType.SUSPEND_SCOPE_EXTENSION
                else "unattended_sensitive_action"
            )
            self._journal(request, "authorization_required", reason)
            return _decision(request, decision_type, reason, epoch=epoch)

        self._journal(request, "authorization_required", (
            grant_reason if grant_reason != "no_grant" else "ask_sensitive_action"
        ))
        return _decision(
            request, DecisionType.REQUIRE_CONFIRMATION,
            grant_reason if grant_reason != "no_grant" else "ask_sensitive_action",
            epoch=epoch,
        )

    def mint_permit(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
        *,
        confirmed: bool = False,
        reinforced: bool = False,
        ttl_seconds: float = 30.0,
    ) -> ExecutionPermit | None:
        source = decision.authorization_source
        strength = "ordinary"
        if decision.decision_type == DecisionType.BLOCK_SYSTEM_DESTRUCTION:
            return None
        if decision.decision_type in {
            DecisionType.REQUIRE_REINFORCED_CONFIRMATION,
            DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION,
        }:
            if not reinforced:
                return None
            source = AuthorizationSource.CONFIRMATION
            strength = "reinforced"
        elif decision.decision_type == DecisionType.REQUIRE_CONFIRMATION:
            if not confirmed:
                return None
            source = AuthorizationSource.CONFIRMATION
        elif decision.decision_type != DecisionType.ALLOW:
            return None
        elif source == AuthorizationSource.NONE:
            # Structurally safe calls need no authorization permit.
            return None

        with self._mint_lock:
            current = self.store.load_state()
            if current.policy_epoch != decision.policy_epoch:
                return None
            if source == AuthorizationSource.GRANT:
                if not self.store.claim_grant_use(
                    decision.grant_ref, request, now=self.clock()
                ):
                    return None
            try:
                return self.permit_ledger.issue(
                    request,
                    authorization_source=source,
                    grant_ref=decision.grant_ref,
                    confirmation_strength=strength,
                    ttl_seconds=ttl_seconds,
                )
            except PermitError:
                return None

    def consume_permit(
        self, permit: ExecutionPermit | str, request: PermissionRequest,
    ) -> ExecutionPermit:
        permit_id = permit.permit_id if isinstance(permit, ExecutionPermit) else permit
        epoch = self.store.load_state().policy_epoch
        return self.permit_ledger.consume(
            permit_id, request, current_policy_epoch=epoch,
        )

    def cancel_permit(self, permit: ExecutionPermit | str) -> bool:
        permit_id = permit.permit_id if isinstance(permit, ExecutionPermit) else permit
        return self.permit_ledger.cancel(permit_id)


def _optional_constraints_allow(
    request: PermissionRequest, grant_constraints: Mapping[str, Any],
) -> bool:
    grant_values = dict(grant_constraints)
    request_values = dict(request.optional_constraints)
    known = {
        "allowed", "max_amount_minor", "amount_minor", "currency",
        "request_currency", "scope_only", "destination_origin", "connector_id",
    }
    if set(grant_values) - known or set(request_values) - known:
        return False
    for values in (grant_values, request_values):
        if "allowed" in values and not isinstance(values["allowed"], bool):
            return False
        if "scope_only" in values and not isinstance(values["scope_only"], bool):
            return False
    if grant_values.get("allowed") is False or request_values.get("allowed") is False:
        return False
    maximum = grant_values.get(
        "max_amount_minor", request_values.get("max_amount_minor")
    )
    amount = request_values.get("amount_minor")
    if maximum is not None:
        if (
            type(maximum) is not int
            or type(amount) is not int
            or maximum < 0
            or amount < 0
        ):
            return False
        if amount > maximum:
            return False
    expected_currency = grant_values.get("currency")
    actual_currency = request_values.get(
        "request_currency", request_values.get("currency")
    )
    if expected_currency is not None:
        if (
            not isinstance(expected_currency, str)
            or not isinstance(actual_currency, str)
            or not re.fullmatch(r"[A-Z]{3}", expected_currency.upper())
            or expected_currency.upper() != actual_currency.upper()
        ):
            return False
    return True


def _request_payment_values(
    request: PermissionRequest,
) -> tuple[int | None, str | None, bool]:
    values = dict(request.optional_constraints)
    amount = values.get("amount_minor")
    currency = values.get("request_currency", values.get("currency"))
    is_payment = request.safe_category == "purchase" or (
        "amount_minor" in values or "request_currency" in values
    )
    if request.private_scope.kind == "purchase":
        is_payment = True
        try:
            private = json.loads(request.private_scope.private_canonical)
            amount = private.get("amount_minor", amount)
            currency = private.get("currency", currency)
        except (TypeError, ValueError):
            return None, None, True
    return (
        amount if type(amount) is int and amount >= 0 else None,
        currency.upper() if isinstance(currency, str) else None,
        is_payment,
    )


def _request_external_destination(
    request: PermissionRequest,
) -> tuple[str, str, bool]:
    """Return exact normalized origin/connector plus whether limits apply."""

    values = dict(request.optional_constraints)
    origin = values.get("destination_origin", "")
    connector = values.get("connector_id", "")
    capability = str(request.capability or "").lower()
    matching = (
        request.safe_category == "purchase"
        or (
            request.tool_category in {"gmail", "mcp", "browser"}
            and request.effect_class != "read_only"
        )
        or any(
            token in capability
            for token in ("send", "publish", "submit", "purchase", "payment")
        )
    )
    if origin:
        matching = True
    try:
        private = json.loads(request.private_scope.private_canonical)
    except (TypeError, ValueError):
        private = {}
    if request.private_scope.kind == "web":
        origin = private.get("identity", origin)
        matching = True
    elif request.private_scope.kind == "purchase":
        origin = private.get("merchant", origin)
    elif request.private_scope.kind == "connector":
        connector = private.get("connector_id", connector)
    return str(origin or ""), str(connector or ""), matching


def _full_access_constraints_allow(
    request: PermissionRequest, state: PermissionState,
) -> bool:
    """Apply only explicitly enabled protected Full Access restrictions."""

    try:
        full_access = state.optional_settings.get("full_access", {})
        if not isinstance(full_access, Mapping):
            return False
        constraints = full_access.get("constraints", {})
        if not isinstance(constraints, Mapping):
            return False

        payment = constraints.get("payment", {})
        if not isinstance(payment, Mapping):
            return False
        if payment.get("enabled") is True:
            amount, currency, matching = _request_payment_values(request)
            if matching:
                maximum = payment.get("max_amount_minor")
                expected_currency = payment.get("currency")
                if (
                    type(maximum) is not int
                    or maximum < 0
                    or amount is None
                    or amount > maximum
                    or not isinstance(expected_currency, str)
                    or not re.fullmatch(r"[A-Z]{3}", expected_currency)
                    or currency is None
                    or currency != expected_currency
                ):
                    return False

        destinations = constraints.get("destinations", {})
        if not isinstance(destinations, Mapping):
            return False
        if destinations.get("enabled") is True:
            origin, connector, matching = _request_external_destination(request)
            if matching:
                origins = destinations.get("origins", [])
                connectors = destinations.get("connectors", [])
                if not isinstance(origins, list) or not isinstance(connectors, list):
                    return False
                if origin:
                    return origin in origins
                if connector:
                    return connector in connectors
                return False
        return True
    except (AttributeError, TypeError, ValueError):
        return False


_CURRENT_EXECUTION_CONTEXT: ContextVar[ExecutionContext | None] = ContextVar(
    "osia_permission_execution_context", default=None,
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
    store: "PermissionStore | None" = None,
) -> ExecutionContext:
    import getpass
    import platform

    active_store = store or get_default_permission_policy().store
    epoch = active_store.load_state().policy_epoch if policy_epoch is None else policy_epoch
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
        policy_epoch=int(epoch),
    )


def _request_scope(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    call_hash: str,
    confirmation_domain: str,
    origin_id: str,
    schema_hash: str,
    account_ref: str,
    tenant_ref: str,
) -> tuple[NormalizedScope, bool]:
    import os

    values = dict(arguments)
    file_fields = [
        name for name in ("path", "old_path", "new_path", "src", "dst")
        if isinstance(values.get(name), str) and values.get(name)
    ]
    if file_fields:
        scopes = {
            name: normalize_file_scope(
                values[name], base=os.getcwd(),
                recursive=bool(values.get("recursive", False)),
            )
            for name in file_fields
        }
        if len(scopes) == 1:
            return next(iter(scopes.values())), False
        return combine_scopes(scopes, safe_category="local_paths"), False
    if confirmation_domain == "gmail" or tool_name.startswith("gmail_"):
        identity = account_ref or "legacy-default"
        return normalize_gmail_scope(identity, tool_name), not bool(account_ref)
    if confirmation_domain == "mcp" or tool_name.startswith("mcp__"):
        try:
            return normalize_mcp_scope(
                origin_id, tool_name,
                account_ref=account_ref or "legacy-default",
                schema_hash=schema_hash,
                tenant_ref=tenant_ref,
            ), not bool(account_ref)
        except ScopeNormalizationError:
            return normalize_exact_call_scope(
                call_hash, safe_category="connector_operation", grantable=False,
            ), True
    if tool_name in {"bash", "run_command"}:
        # The current tool schema is free-form shell text. Phase 1 permits may
        # bind that exact call, but grants cannot broaden it into a generic shell.
        return normalize_exact_call_scope(
            call_hash, safe_category="local_command", grantable=False,
        ), False
    if tool_name == "set_volume":
        # A bounded volume level is a non-private, reversible local setting.
        # Keep the extension bound to this exact normalized call so mobile
        # approval cannot broaden into another setting or argument value.
        return normalize_exact_call_scope(
            call_hash, safe_category="safe_local_setting", grantable=True,
        ), False
    if tool_name == "read_document" and re.fullmatch(
        r"src_[0-9a-f]{32}", str(arguments.get("source_ref") or ""),
    ):
        # Autonomous document approval is bound to one exact opaque source and
        # one exact read call.  It cannot authorize a path or another source.
        return normalize_exact_call_scope(
            call_hash, safe_category="document_source", grantable=True,
        ), False
    if tool_name == "render_pdf" and arguments.get("destination") == "desktop":
        # The model names only a well-known destination. The permit remains
        # bound to this exact structured call; no raw path crosses the model
        # or authorization boundary.
        return normalize_exact_call_scope(
            call_hash, safe_category="local_destination", grantable=True,
        ), False
    if tool_name == "upload_browser_file":
        return normalize_exact_call_scope(
            call_hash, safe_category="browser_upload", grantable=True,
        ), False
    if tool_name == "activate_browser_element" and arguments.get(
        "trusted_final_action"
    ) is True:
        return normalize_exact_call_scope(
            call_hash, safe_category="browser_final_action", grantable=True,
        ), False
    if tool_name in _EXACT_STRUCTURED_BROWSER_ACTIONS:
        # Autonomous Web approval is single-call and argument-bound.  This is
        # deliberately not an origin-wide or browser-wide permission.
        return normalize_exact_call_scope(
            call_hash, safe_category="browser_action", grantable=True,
        ), False
    return normalize_exact_call_scope(
        call_hash, safe_category="tool_operation", grantable=False,
    ), False


def _tool_category(tool_name: str, confirmation_domain: str) -> str:
    if confirmation_domain == "gmail" or tool_name.startswith("gmail_"):
        return "gmail"
    if confirmation_domain == "mcp" or tool_name.startswith("mcp__"):
        return "mcp"
    if tool_name in {"bash", "run_command"}:
        return "shell"
    if tool_name in {
        "read_file", "write_file", "delete_file", "delete_directory",
        "render_pdf",
        "create_directory", "rename_file", "copy_file", "edit_file_lines",
        "list_files", "get_file_info", "search_files",
    }:
        return "filesystem"
    if tool_name in {
        "click", "find_and_click", "press_key", "desktop_click",
        "desktop_press", "desktop_type", "fill", "fill_form_field",
        "navigate", "open_browser", "type_text", "get_browser_state",
        "navigate_browser", "inspect_browser_page",
        "wait_for_browser_condition", "activate_browser_element",
        "list_browser_tabs", "select_browser_tab", "close_browser_tab",
        "reload_browser_page", "browser_back", "browser_forward",
        "open_new_browser_tab", "return_to_browser_opener", "search_web",
        "open_search_result", "paginate_browser", "scroll_infinite",
        "list_browser_dialogs", "handle_browser_dialog",
        "upload_browser_file", "download_browser_file",
    }:
        return "browser"
    return "native"


def _derived_request_constraints(
    arguments: Mapping[str, Any],
    scope: NormalizedScope,
    supplied: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Add only exact values already supported by deterministic normalizers."""

    constraints = dict(supplied or {})
    amount = arguments.get("amount_minor")
    currency = arguments.get("currency")
    if "amount_minor" not in constraints and type(amount) is int and amount >= 0:
        constraints["amount_minor"] = amount
    if (
        "request_currency" not in constraints
        and isinstance(currency, str)
        and re.fullmatch(r"[A-Za-z]{3}", currency)
    ):
        constraints["request_currency"] = currency.upper()

    if "destination_origin" not in constraints:
        for field_name in (
            "merchant_origin", "destination_url", "target_url", "origin", "url",
        ):
            candidate = arguments.get(field_name)
            if not isinstance(candidate, str) or not candidate:
                continue
            try:
                web_scope = normalize_web_scope(
                    candidate, kind=WebScopeKind.EXACT_ORIGIN,
                )
                constraints["destination_origin"] = json.loads(
                    web_scope.private_canonical
                )["identity"]
            except (KeyError, TypeError, ValueError, ScopeNormalizationError):
                pass
            break

    if "connector_id" not in constraints and scope.kind == "connector":
        try:
            connector_id = json.loads(scope.private_canonical)["connector_id"]
            if isinstance(connector_id, str):
                constraints["connector_id"] = connector_id
        except (KeyError, TypeError, ValueError):
            pass
    return _validate_constraint_mapping(constraints, grant=False)


def build_permission_request(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    confirmation_domain: str = "action",
    origin_id: str = "osia",
    execution_context: ExecutionContext | None = None,
    deletion_classification: TrustedDeletionClassification = (
        TRUSTED_CLEAR_CLASSIFICATION
    ),
    optional_constraints: Mapping[str, Any] | None = None,
    account_ref: str = "",
    tenant_ref: str = "",
    connector_schema_hash: str = "",
    force_sensitive: bool = False,
    policy: PermissionPolicy | None = None,
) -> PermissionRequest:
    from osia.tool_contracts import (
        ConfirmationPolicy,
        SideEffectClass,
        compatibility_tool_spec,
        effective_contract,
        get_tool_spec,
        normalized_call_hash,
    )

    identity = str(tool_name or "")
    values = dict(arguments) if isinstance(arguments, Mapping) else {}
    spec = get_tool_spec(identity)
    if spec is None:
        spec = compatibility_tool_spec(identity, lambda **_kwargs: None)
        if force_sensitive:
            spec = replace(
                spec,
                capability=f"{confirmation_domain}:{identity}",
                side_effect=SideEffectClass.UNKNOWN_HIGH_RISK,
                confirmation=ConfirmationPolicy.CONFIRMATION_REQUIRED,
                dynamic=True,
                connector_id=origin_id,
            )
    contract = effective_contract(spec, values)
    if force_sensitive and contract.confirmation not in {
        ConfirmationPolicy.CONFIRMATION_REQUIRED,
        ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION,
    }:
        contract = replace(
            contract,
            confirmation=ConfirmationPolicy.CONFIRMATION_REQUIRED,
        )
    call_hash = normalized_call_hash(identity, values)
    schema_hash = connector_schema_hash or canonical_hash(spec.input_schema)
    if not re.fullmatch(r"[0-9a-f]{64}", schema_hash):
        raise ValueError("invalid_connector_schema_hash")
    scope, ambiguous = _request_scope(
        identity, values, call_hash=call_hash,
        confirmation_domain=confirmation_domain,
        origin_id=origin_id,
        schema_hash=schema_hash,
        account_ref=account_ref,
        tenant_ref=tenant_ref,
    )
    active_policy = policy or get_default_permission_policy()
    context = execution_context or _CURRENT_EXECUTION_CONTEXT.get()
    if context is None:
        context = make_execution_context(store=active_policy.store)
    elif context.policy_epoch != active_policy.store.load_state().policy_epoch:
        # Preserve the supplied stale epoch: the evaluator must reject it.
        pass
    try:
        from osia.autonomous.authorization import bind_request_execution_context

        context = bind_request_execution_context(context, identity, call_hash)
    except Exception:
        # A malformed Autonomous binding fails closed at evaluation through the
        # stale/missing run fields; ordinary chat remains unchanged.
        pass
    if context.execution_type == ExecutionType.SCHEDULED:
        from osia.scheduled_tasks import (
            bind_request_execution_context,
            is_bound_scheduled_context,
        )

        if is_bound_scheduled_context(context):
            context = bind_request_execution_context(context, identity, call_hash)
    autonomous_equivalence_hash = ""
    if context.execution_type == ExecutionType.AUTONOMOUS:
        try:
            from osia.autonomous.authorization import operation_equivalence_hash

            autonomous_equivalence_hash = operation_equivalence_hash(
                identity, values,
            )
        except Exception:
            autonomous_equivalence_hash = call_hash
    return PermissionRequest(
        tool_identity=identity,
        confirmation_domain=str(confirmation_domain),
        tool_spec=spec,
        effective_contract=contract,
        capability=spec.capability,
        private_scope=scope,
        safe_category=scope.safe_category,
        effect_class=contract.side_effect.value,
        normalized_call_hash=call_hash,
        deletion_classification=deletion_classification,
        operation_id=context.operation_id,
        execution_context=context,
        tool_category=_tool_category(identity, confirmation_domain),
        optional_constraints=_derived_request_constraints(
            values, scope, optional_constraints,
        ),
        ambiguous_legacy_identity=ambiguous,
        autonomous_equivalence_hash=autonomous_equivalence_hash,
    )


_DEFAULT_POLICY_LOCK = threading.Lock()
_DEFAULT_POLICY: PermissionPolicy | None = None


def get_default_permission_policy() -> PermissionPolicy:
    global _DEFAULT_POLICY
    with _DEFAULT_POLICY_LOCK:
        if _DEFAULT_POLICY is None:
            from osia.permission_store import PermissionStore

            _DEFAULT_POLICY = PermissionPolicy(PermissionStore())
        return _DEFAULT_POLICY


def set_default_permission_policy(policy: PermissionPolicy | None) -> None:
    """Test/runtime injection point; ``None`` restores lazy safe defaults."""

    global _DEFAULT_POLICY
    with _DEFAULT_POLICY_LOCK:
        _DEFAULT_POLICY = policy


def evaluate_permission(
    request: PermissionRequest, *, policy: PermissionPolicy | None = None,
) -> PermissionDecision:
    return (policy or get_default_permission_policy()).evaluate(request)


@dataclass(frozen=True, repr=False)
class _PendingExecutionPermit:
    permit: ExecutionPermit
    request: PermissionRequest = field(repr=False)
    policy: PermissionPolicy = field(repr=False)


_PENDING_EXECUTION_PERMIT: ContextVar[_PendingExecutionPermit | None] = ContextVar(
    "osia_pending_execution_permit", default=None,
)


def clear_pending_execution_permit() -> None:
    pending = _PENDING_EXECUTION_PERMIT.get()
    _PENDING_EXECUTION_PERMIT.set(None)
    if pending is not None:
        pending.policy.cancel_permit(pending.permit)


def hold_execution_permit(
    permit: ExecutionPermit,
    request: PermissionRequest,
    *,
    policy: PermissionPolicy | None = None,
) -> None:
    _PENDING_EXECUTION_PERMIT.set(
        _PendingExecutionPermit(
            permit=permit,
            request=request,
            policy=policy or get_default_permission_policy(),
        )
    )


def pending_execution_permit_matches(
    tool_name: str, arguments: Mapping[str, Any],
) -> bool:
    pending = _PENDING_EXECUTION_PERMIT.get()
    if pending is None:
        return False
    try:
        from osia.tool_contracts import normalized_call_hash

        return (
            pending.request.tool_identity == str(tool_name or "")
            and sensitive_hash_equal(
                pending.request.normalized_call_hash,
                normalized_call_hash(tool_name, arguments),
            )
        )
    except Exception:
        return False


def ensure_confirmed_execution_permit(
    tool_name: str, arguments: Mapping[str, Any],
) -> bool:
    """Adapt the established ``None == approved`` confirmation contract."""

    if pending_execution_permit_matches(tool_name, arguments):
        return True
    policy = get_default_permission_policy()
    request = build_permission_request(
        tool_name,
        arguments,
        confirmation_domain="action",
        origin_id="osia",
        force_sensitive=True,
        policy=policy,
    )
    decision = policy.evaluate(request)
    permit = policy.mint_permit(
        request,
        decision,
        confirmed=decision.decision_type == DecisionType.REQUIRE_CONFIRMATION,
    )
    if permit is None:
        return False
    hold_execution_permit(permit, request, policy=policy)
    return True


def consume_pending_execution_permit(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> bool:
    pending = _PENDING_EXECUTION_PERMIT.get()
    _PENDING_EXECUTION_PERMIT.set(None)
    if pending is None:
        return False
    try:
        from osia.tool_contracts import normalized_call_hash

        actual_hash = normalized_call_hash(tool_name, arguments)
        candidate = replace(
            pending.request,
            normalized_call_hash=actual_hash,
        )
        consumed = pending.policy.consume_permit(pending.permit, candidate)
        try:
            from osia.autonomous.authorization import on_permit_consumed

            on_permit_consumed(candidate, consumed)
        except Exception:
            pass
        return True
    except Exception:
        return False


def permission_error_for_decision(decision: PermissionDecision) -> str:
    if decision.decision_type == DecisionType.BLOCK_SYSTEM_DESTRUCTION:
        return "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    if decision.decision_type == DecisionType.REQUIRE_COLOSSAL_DELETION_CONFIRMATION:
        return "ERROR: REQUIRE_COLOSSAL_DELETION_CONFIRMATION"
    if decision.decision_type == DecisionType.SUSPEND_SCOPE_EXTENSION:
        return f"ERROR: SUSPEND_SCOPE_EXTENSION: {decision.reason_code}"
    return f"ERROR: permission denied: {decision.reason_code}"


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
    final, barrier_error = _universal_deletion_decision(
        full_name,
        arguments,
        plan,
        confirmation_domain="mcp",
        origin_id=server_id,
    )
    if final:
        return barrier_error
    try:
        from osia.mcp_client.registry import confirm_connector_write

        blocked = confirm_connector_write(
            server_id,
            tool_name,
            "Delete one connected item",
            call_arguments=dict(arguments),
            policy_tool_name=full_name,
        )
    except Exception:
        blocked = "ERROR: MCP deletion confirmation unavailable"
    if blocked is not None:
        return blocked
    return _finish_ordinary_deletion_authorization(
        full_name,
        arguments,
        plan,
    )


def _current_policy_epoch() -> int:
    return int(get_default_permission_policy().store.load_state().policy_epoch)


def execute_authorized_deletion_call(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    directory: bool,
    recursive: bool,
) -> str:
    """Consume the exact plan permit, delete deterministically, and sanitize results."""

    from osia.deletion_plan import (
        DeletionExecutionOutcome,
        DeletionPlanError,
        consume_pending_deletion_for_dispatch,
        execute_deletion_dispatch,
        pending_deletion_matches,
    )

    binding_arguments = _deletion_binding_arguments(tool_name, arguments)
    if not pending_deletion_matches(tool_name, binding_arguments):
        return "ERROR: deletion authorization unavailable; nothing was deleted"
    # Direct focused tests call the tool immediately after guard_tool_call,
    # while the normal agent dispatcher has already consumed this permit.  If
    # it is still pending, burn it here before the deletion-specific permit.
    if pending_execution_permit_matches(tool_name, arguments):
        if not consume_pending_execution_permit(tool_name, arguments):
            return "ERROR: execution permit unavailable; nothing was deleted"
    try:
        dispatch = consume_pending_deletion_for_dispatch(
            tool_name,
            binding_arguments,
            policy_epoch=_current_policy_epoch(),
        )
        result = execute_deletion_dispatch(dispatch)
    except DeletionPlanError as exc:
        if exc.code in {
            "deletion_plan_changed", "deletion_plan_expired",
            "deletion_permit_expired", "deletion_policy_epoch_changed",
            "deletion_policy_version_changed", "deletion_permit_inventory_mismatch",
        }:
            return "ERROR: DELETION_PLAN_CHANGED_REQUIRES_NEW_CONFIRMATION"
        return f"ERROR: deletion blocked before I/O ({exc.code})"
    except Exception:
        return "ERROR: deletion blocked before I/O (internal_preflight_failure)"
    if result.outcome is DeletionExecutionOutcome.OUTCOME_UNKNOWN:
        try:
            get_default_permission_policy().permit_ledger.mark_outcome_unknown(
                dispatch.plan.operation_id
            )
        except Exception:
            pass
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
        policy_epoch=_current_policy_epoch(),
    )


def external_deletion_result(dispatch, raw_result: Any) -> str:
    """Verify the exact local postcondition; MCP response text is never trusted."""

    del raw_result
    from osia.deletion_plan import (
        DeletionExecutionOutcome,
        verify_external_deletion_dispatch,
    )

    result = verify_external_deletion_dispatch(dispatch)
    try:
        if result.outcome is DeletionExecutionOutcome.OUTCOME_UNKNOWN:
            get_default_permission_policy().permit_ledger.mark_outcome_unknown(
                dispatch.plan.operation_id
            )
    except Exception:
        pass
    return result.model_text(directory=False, recursive=False)


def register_grant(
    grant: Grant, *, policy: PermissionPolicy | None = None,
) -> None:
    (policy or get_default_permission_policy()).store.add_grant(grant)


def revoke_grant(
    grant_id: str, *, policy: PermissionPolicy | None = None,
):
    return (policy or get_default_permission_policy()).store.revoke_grant(grant_id)


def revoke_all_grants(*, policy: PermissionPolicy | None = None):
    return (policy or get_default_permission_policy()).store.revoke_all_grants()


def revoke_full_access(*, policy: PermissionPolicy | None = None):
    return (policy or get_default_permission_policy()).store.revoke_full_access()


def activate_full_access(
    *,
    activation_proof,
    policy: PermissionPolicy | None = None,
):
    return (policy or get_default_permission_policy()).store.activate_full_access(
        activation_proof=activation_proof,
    )


def update_full_access_constraints(
    *, policy: PermissionPolicy | None = None, **constraints,
):
    return (
        policy or get_default_permission_policy()
    ).store.update_full_access_constraints(**constraints)


def update_automatic_permission(
    category: str,
    enabled: bool,
    *,
    policy: PermissionPolicy | None = None,
):
    return (
        policy or get_default_permission_policy()
    ).store.update_automatic_permissions({category: enabled})


def apply_default_access(*, policy: PermissionPolicy | None = None):
    return (
        policy or get_default_permission_policy()
    ).store.apply_default_access()


def permission_center_snapshot(*, policy: PermissionPolicy | None = None):
    return (
        policy or get_default_permission_policy()
    ).store.permission_center_snapshot()


def revoke_permission_center_grant(
    row_token: str, *, policy: PermissionPolicy | None = None,
):
    return (
        policy or get_default_permission_policy()
    ).store.revoke_center_grant(row_token)


def revoke_permission_center_group(
    group_token: str, *, policy: PermissionPolicy | None = None,
):
    return (
        policy or get_default_permission_policy()
    ).store.revoke_center_group(group_token)


def revoke_all_delegated_authority(*, policy: PermissionPolicy | None = None):
    return (
        policy or get_default_permission_policy()
    ).store.revoke_all_delegated_authority()


def clear_permission_journal(*, policy: PermissionPolicy | None = None) -> bool:
    return (policy or get_default_permission_policy()).store.clear_journal()


def record_permission_journal_event(
    event_code: str,
    *,
    policy: PermissionPolicy | None = None,
    **safe_fields,
) -> bool:
    return (
        policy or get_default_permission_policy()
    ).store.append_journal_event(event_code, **safe_fields)


def reset_permissions_to_ask(*, policy: PermissionPolicy | None = None):
    return (policy or get_default_permission_policy()).store.reset_to_ask()


# Descriptive compatibility name for callers that prefer the fully qualified
# type; ``Grant`` remains the concise canonical schema name.
PermissionGrant = Grant
