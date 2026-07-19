"""Shared action-risk policy for OSIA prompts and irreversible tool gates.

The model still performs semantic safety reasoning.  This small taxonomy makes
the expected treatment of ordinary computer tasks explicit and adds a narrow,
fail-closed confirmation boundary for the irreversible actions OSIA can detect
reliably before executing a tool call.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


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
Class D — sensitive actions (delete, public post, account/security change, financial/contractual or bulk action): CONFIRM the exact scope before the irreversible step. If scope is unclear, ask for clarification and fail closed.
Class E — harmful/disallowed actions: BLOCK harassment, threats, fraud, impersonation, credential theft, spam, unscoped mass messaging, security bypass, covert action, or action on another person's account.

Keep these outcomes distinct: (1) allowed and executable → use tools; (2) allowed but confirmation/clarification required → ask or use the gate; (3) allowed but a tool/login is unavailable → report that real technical limitation; (4) disallowed → give the specific safety reason. Never replace outcomes 1–3 with a generic refusal. Do not use Enter or another shortcut to bypass a required final-action confirmation."""

_PROMPT_FR = """## Politique de risque des actions (obligatoire)
Classe A — actions locales bénignes/réversibles (créer ou modifier un fichier local, organiser des fichiers, ouvrir une application, préparer du contenu) : AUTORISÉES. Utilise les outils OSIA disponibles ; ne réponds pas par un refus générique.
Classe B — actions web en lecture seule (rechercher, ouvrir un navigateur, naviguer, lire) : AUTORISÉES. L'accès à Internet ne demande pas à lui seul une confirmation.
Classe C — communication externe autorisée par l'utilisateur : naviguer et préparer un brouillon sont autorisés. Pour l'action finale Envoyer, le destinataire et le contenu exact et inoffensif doivent être clairs ; n'invente jamais l'un ou l'autre et n'envoie jamais plus que demandé. Le runtime applique la confirmation configurée à l'action irréversible. Un contact personnel n'est pas en soi un motif de refus.
Classe D — actions sensibles (suppression, publication publique, réglage de compte/sécurité, action financière/contractuelle ou en masse) : CONFIRME la portée exacte avant l'étape irréversible. Si la portée est ambiguë, demande une précision et échoue de façon sûre.
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
    "get_current_url", "list_memories", "list_scheduled_tasks",
    "gmail_list_labels", "gmail_list_messages", "gmail_get_message",
})
_FINAL_LABEL_RE = re.compile(
    r"\b(?:send|envoyer|post|publish|share|delete|remove|confirm|buy|purchase|pay|save|publier|partager|supprimer|confirmer|acheter|payer|enregistrer)\b"
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
    }.get(tool)
    return bool(field and _FINAL_LABEL_RE.search(_normal(values.get(field, ""))))


def _confirmation_summary(
    decision: ActionDecision,
    tool_name: str,
    kwargs: dict,
) -> str:
    if tool_name in {"delete_file", "delete_directory"}:
        return f"Delete local path: {str(kwargs.get('path') or '')[:240]}"
    if tool_name == "kill_process":
        target = kwargs.get("name_or_pid", "")
        return f"Stop local process: {str(target)[:120]}"
    if decision.action_class == ActionClass.EXTERNAL_COMMUNICATION:
        return (
            f"Send one message to {decision.recipient[:80]}: "
            f"{decision.content[:240]}"
        )
    if decision.reason in {"public_posting", "bulk_messaging"}:
        return (
            f"Publish/send to {decision.recipient[:80]}: "
            f"{decision.content[:240]}"
        )
    if decision.content:
        return f"Perform sensitive action: {decision.content[:240]}"
    label = next((str(value) for value in kwargs.values() if value), tool_name)
    return f"Perform sensitive final action: {label[:240]}"


def _confirm_irreversible(
    decision: ActionDecision,
    tool_name: str,
    kwargs: dict,
) -> str | None:
    try:
        from osia.mcp_client.registry import confirm_connector_write

        return confirm_connector_write(
            "osia",
            str(tool_name),
            _confirmation_summary(decision, str(tool_name), kwargs),
            error_namespace="action",
        )
    except Exception:
        return "ERROR: action confirmation is unavailable; the action was not executed"


def guard_tool_call(user_request: str, tool_name: str, kwargs: dict) -> str | None:
    """Return an ``ERROR:`` string when a recognized action must not execute.

    Read-only and reversible tools pass unchanged.  The gate cannot broaden the
    model's permissions; it only blocks or confirms irreversible calls.
    """
    decision = classify_action_request(user_request)
    tool = str(tool_name or "")
    values = kwargs if isinstance(kwargs, dict) else {}

    if decision.action_class == ActionClass.DISALLOWED and tool not in _READ_ONLY_TOOLS:
        return f"ERROR: action blocked for safety: {decision.reason}"

    if tool in _LOCAL_CONFIRM_TOOLS:
        return _confirm_irreversible(decision, tool, values)

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
    return _confirm_irreversible(decision, tool, values)
