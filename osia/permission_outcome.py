"""Typed result of one interactive permission delivery attempt (WOR-252)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PermissionOutcome(str, Enum):
    ALLOWED = "allowed"
    USER_DENIED = "user_denied"
    PRESENTATION_FAILED = "presentation_failed"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    INTERNAL_FAILURE = "internal_failure"


@dataclass(frozen=True)
class PermissionResult:
    """One typed broker result, optionally bound to its opaque request id."""

    outcome: PermissionOutcome
    correlation_id: str = ""

    def __post_init__(self) -> None:
        if (
            self.outcome is PermissionOutcome.USER_DENIED
            and not str(self.correlation_id or "").strip()
        ):
            raise ValueError("user_denied_requires_correlation")

    @property
    def approved(self) -> bool:
        return self.outcome is PermissionOutcome.ALLOWED


_OUTCOME_TOKEN = re.compile(
    r"(?<![a-z0-9_])permission_outcome=("
    + "|".join(item.value for item in PermissionOutcome)
    + r")(?![a-z0-9_])"
)


def normalize_permission_result(
    value: Any,
    *,
    correlation_id: str = "",
) -> PermissionResult:
    """Adapt typed and legacy synchronous providers without losing failures.

    Legacy ``False`` is an explicit synchronous provider denial. Infrastructure
    failures must return a typed result (or raise); they are never converted to
    denial here.
    """

    if isinstance(value, PermissionResult):
        if (
            value.outcome is PermissionOutcome.USER_DENIED
            and not value.correlation_id
        ):
            return PermissionResult(
                PermissionOutcome.INTERNAL_FAILURE,
                str(correlation_id or ""),
            )
        return value
    if value is True:
        return PermissionResult(
            PermissionOutcome.ALLOWED,
            str(correlation_id or ""),
        )
    if value is False:
        return PermissionResult(
            PermissionOutcome.USER_DENIED,
            str(correlation_id or ""),
        )
    if value is None:
        return PermissionResult(
            PermissionOutcome.PRESENTATION_FAILED,
            str(correlation_id or ""),
        )
    return PermissionResult(
        PermissionOutcome.INTERNAL_FAILURE,
        str(correlation_id or ""),
    )


def permission_error_text(
    namespace: str,
    origin_id: str,
    tool_name: str,
    result: PermissionResult,
) -> str:
    """Return a bounded machine-typed ``ERROR:`` preserving legacy wording."""

    prefix = (
        f"ERROR: {namespace}/{origin_id}/{tool_name}: "
        f"permission_outcome={result.outcome.value}; "
    )
    detail = {
        PermissionOutcome.USER_DENIED: "user declined",
        PermissionOutcome.PRESENTATION_FAILED: (
            "sensitive action blocked (confirmation required, "
            "no approver available)"
        ),
        PermissionOutcome.TIMEOUT: "approval request timed out",
        PermissionOutcome.DISCONNECTED: "approval channel disconnected",
        PermissionOutcome.INTERNAL_FAILURE: "internal permission failure",
        PermissionOutcome.ALLOWED: "confirmation scope unavailable",
    }[result.outcome]
    return prefix + detail


def permission_outcome_from_text(value: Any) -> PermissionOutcome | None:
    match = _OUTCOME_TOKEN.search(str(value or "").lower())
    if not match:
        return None
    try:
        return PermissionOutcome(match.group(1))
    except ValueError:
        return None


_EXECUTION_STATE_TEXT = {
    "active": {
        "fr": "L’opération précédente est déjà active. Aucune seconde tentative n’a été créée.",
        "en": "The previous operation is already active. No second attempt was created.",
    },
    "unresolved": {
        "fr": "L’étape précédente reste inachevée et aucune nouvelle tentative n’a été acceptée pendant ce tour.",
        "en": "The previous step remains unfinished and no new attempt was accepted during this turn.",
    },
    "permission_user_denied": {
        "fr": "L’autorisation a été explicitement refusée. L’opération n’a pas été exécutée.",
        "en": "Permission was explicitly denied. The operation was not executed.",
    },
    "permission_presentation_failed": {
        "fr": "La demande d’autorisation n’a pas pu être présentée. L’opération n’a pas été exécutée et les étapes déjà terminées sont conservées.",
        "en": "The permission request could not be presented. The operation was not executed, and completed steps were preserved.",
    },
    "permission_timeout": {
        "fr": "La demande d’autorisation a expiré. L’opération n’a pas été exécutée et les étapes déjà terminées sont conservées.",
        "en": "The permission request timed out. The operation was not executed, and completed steps were preserved.",
    },
    "permission_disconnected": {
        "fr": "Le canal d’autorisation s’est déconnecté. L’opération n’a pas été exécutée et les étapes déjà terminées sont conservées.",
        "en": "The permission channel disconnected. The operation was not executed, and completed steps were preserved.",
    },
    "permission_internal_failure": {
        "fr": "Une erreur interne de permission a empêché l’autorisation. L’opération n’a pas été exécutée et les étapes déjà terminées sont conservées.",
        "en": "An internal permission failure blocked approval. The operation was not executed, and completed steps were preserved.",
    },
}


def execution_state_text(code: str, language: str) -> str:
    selected = _EXECUTION_STATE_TEXT.get(
        str(code or ""), _EXECUTION_STATE_TEXT["unresolved"],
    )
    locale = "fr" if str(language or "").lower().startswith("fr") else "en"
    return selected[locale]


__all__ = [
    "PermissionOutcome",
    "PermissionResult",
    "execution_state_text",
    "normalize_permission_result",
    "permission_error_text",
    "permission_outcome_from_text",
]
