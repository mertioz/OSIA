"""Provider-operation continuation behind one durable, fenced contract.

Opaque provider references are evidence handles only.  They never authorize a
new semantic mutation: an unavailable lookup falls back to ActionTruth
reconciliation instead of issuing another operation.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from osia.durable_transport import FailureDomain


class ProviderContinuationState(str, Enum):
    STILL_RUNNING = "still_running"
    COMPLETED_WITH_RECEIPT = "completed_with_receipt"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    EXPLICIT_NOT_APPLIED = "explicit_not_applied"
    RESULT_UNAVAILABLE = "result_unavailable"
    LOOKUP_UNSUPPORTED = "provider_lookup_unsupported"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CANCELLED_AUTHORITY = "cancelled_authority"
    TERMINAL_AUTHORITY = "terminal_authority"


@dataclass(frozen=True)
class ProviderContinuationResult:
    state: ProviderContinuationState
    reason_code: str
    callback_id: str = ""
    evidence_ref: str = ""
    provider_references: dict[str, str] = field(default_factory=dict)
    failure_domain: FailureDomain | None = None
    observed_at: float = 0.0
    execution_authorized: bool = False


class ProviderContinuationAdapter(Protocol):
    name: str

    def supports(self, attempt: Mapping[str, Any]) -> bool: ...

    def resume_or_reconcile(
        self, attempt: Mapping[str, Any]
    ) -> ProviderContinuationResult: ...


class DurableReceiptAdapter:
    """Resolve outcomes already proven by the canonical local receipt."""

    name = "durable_receipt"

    def supports(self, attempt: Mapping[str, Any]) -> bool:
        truth = attempt.get("action_truth")
        return isinstance(truth, Mapping)

    def resume_or_reconcile(
        self, attempt: Mapping[str, Any]
    ) -> ProviderContinuationResult:
        truth = attempt.get("action_truth") or {}
        effect_truth = str(truth.get("effect_truth") or "")
        status = str(attempt.get("status") or "")
        refs = dict(attempt.get("provider_references") or {})
        attempt_id = str(attempt.get("tool_attempt_id") or "")
        if effect_truth == "verified":
            return ProviderContinuationResult(
                ProviderContinuationState.COMPLETED_WITH_RECEIPT,
                "durable_effect_already_verified",
                callback_id="local_verified_" + hashlib.sha256(
                    attempt_id.encode("utf-8")
                ).hexdigest()[:24],
                evidence_ref=str(
                    ((truth.get("evidence") or [{}])[-1] or {}).get(
                        "evidence_ref"
                    )
                    or "durable_action_truth"
                ),
                provider_references=refs,
                observed_at=time.time(),
            )
        if effect_truth == "not_applied":
            return ProviderContinuationResult(
                ProviderContinuationState.EXPLICIT_NOT_APPLIED,
                "durable_non_application_already_verified",
                callback_id="local_not_applied_" + hashlib.sha256(
                    attempt_id.encode("utf-8")
                ).hexdigest()[:24],
                evidence_ref="durable_action_truth",
                provider_references=refs,
                observed_at=time.time(),
            )
        if status == "failed_before_effect":
            return ProviderContinuationResult(
                ProviderContinuationState.FAILED_BEFORE_EFFECT,
                "durable_failure_before_effect",
                callback_id="local_failed_before_" + hashlib.sha256(
                    attempt_id.encode("utf-8")
                ).hexdigest()[:24],
                evidence_ref="durable_attempt_receipt",
                provider_references=refs,
                observed_at=time.time(),
            )
        return ProviderContinuationResult(
            ProviderContinuationState.RECONCILIATION_REQUIRED,
            "durable_receipt_not_terminal",
            provider_references=refs,
            observed_at=time.time(),
            failure_domain=FailureDomain.SEMANTIC_EFFECT_UNCERTAINTY,
        )


class ChatGPTAccountContinuationAdapter:
    """Fail safely when a restarted native app-server lacks lookup support."""

    name = "chatgpt_account_native"

    def supports(self, attempt: Mapping[str, Any]) -> bool:
        return str(attempt.get("provider_id") or "") == "chatgpt_account"

    def resume_or_reconcile(
        self, attempt: Mapping[str, Any]
    ) -> ProviderContinuationResult:
        refs = dict(attempt.get("provider_references") or {})
        if not any(
            refs.get(key) for key in (
                "item_id", "tool_call_id", "turn_id", "thread_id",
            )
        ):
            return ProviderContinuationResult(
                ProviderContinuationState.RESULT_UNAVAILABLE,
                "provider_reference_unavailable",
                provider_references=refs,
                observed_at=time.time(),
                failure_domain=FailureDomain.PROVIDER_TRANSPORT_FAILURE,
            )
        # Current Codex app-server sessions expose native lifecycle callbacks
        # but no durable cross-process lookup endpoint. Preserve the reference
        # and hand control to semantic observers; never create another item.
        return ProviderContinuationResult(
            ProviderContinuationState.LOOKUP_UNSUPPORTED,
            "provider_lookup_not_supported_after_restart",
            provider_references=refs,
            observed_at=time.time(),
            failure_domain=FailureDomain.PROVIDER_TRANSPORT_FAILURE,
        )


class LookupContinuationAdapter:
    """Small adapter for providers that expose a stable operation lookup."""

    def __init__(self, provider_id: str, lookup) -> None:
        self.provider_id = str(provider_id or "")
        self.lookup = lookup
        self.name = f"{self.provider_id}_operation_lookup"

    def supports(self, attempt: Mapping[str, Any]) -> bool:
        return bool(
            str(attempt.get("provider_id") or "") == self.provider_id
            and isinstance(attempt.get("provider_references"), Mapping)
        )

    def resume_or_reconcile(
        self, attempt: Mapping[str, Any]
    ) -> ProviderContinuationResult:
        refs = dict(attempt.get("provider_references") or {})
        try:
            raw = self.lookup(dict(refs))
        except Exception:
            return ProviderContinuationResult(
                ProviderContinuationState.RESULT_UNAVAILABLE,
                "provider_lookup_transport_failure",
                provider_references=refs,
                observed_at=time.time(),
                failure_domain=FailureDomain.PROVIDER_TRANSPORT_FAILURE,
            )
        if not isinstance(raw, Mapping):
            return ProviderContinuationResult(
                ProviderContinuationState.RESULT_UNAVAILABLE,
                "provider_lookup_result_invalid",
                provider_references=refs,
                observed_at=time.time(),
                failure_domain=FailureDomain.PROVIDER_OPERATION_FAILURE,
            )
        try:
            state = ProviderContinuationState(str(raw.get("state") or ""))
        except ValueError:
            state = ProviderContinuationState.RECONCILIATION_REQUIRED
        supplied = raw.get("provider_references")
        merged = dict(refs)
        if isinstance(supplied, Mapping):
            merged.update({
                str(key): str(value)
                for key, value in supplied.items()
                if value is not None
            })
        callback_id = str(raw.get("callback_id") or "")
        if not callback_id:
            callback_id = "lookup_" + hashlib.sha256(
                repr(sorted(merged.items())).encode("utf-8")
                + state.value.encode("utf-8")
            ).hexdigest()[:32]
        return ProviderContinuationResult(
            state,
            str(raw.get("reason_code") or "provider_lookup_result"),
            callback_id=callback_id,
            evidence_ref=str(raw.get("evidence_ref") or ""),
            provider_references=merged,
            observed_at=time.time(),
            failure_domain=(
                FailureDomain.PROVIDER_OPERATION_FAILURE
                if state is ProviderContinuationState.RESULT_UNAVAILABLE
                else None
            ),
        )


class ProviderContinuationCoordinator:
    def __init__(self, store, adapters=()) -> None:
        self.store = store
        self.adapters = (
            DurableReceiptAdapter(),
            *tuple(adapters),
            ChatGPTAccountContinuationAdapter(),
        )

    def resume_or_reconcile_provider_attempt(
        self, attempt_id: str,
    ) -> ProviderContinuationResult:
        attempt = self.store.get_attempt(attempt_id)
        if not isinstance(attempt, dict):
            return ProviderContinuationResult(
                ProviderContinuationState.RESULT_UNAVAILABLE,
                "attempt_not_found",
                failure_domain=FailureDomain.PROVIDER_OPERATION_FAILURE,
            )
        authority = str(attempt.get("authority_state") or "active")
        if authority in {"terminal", "superseded"}:
            local = DurableReceiptAdapter().resume_or_reconcile(attempt)
            if local.state in {
                ProviderContinuationState.COMPLETED_WITH_RECEIPT,
                ProviderContinuationState.EXPLICIT_NOT_APPLIED,
                ProviderContinuationState.FAILED_BEFORE_EFFECT,
            }:
                return local
        chosen = None
        for adapter in self.adapters:
            if adapter.supports(attempt):
                result = adapter.resume_or_reconcile(attempt)
                chosen = result
                if result.state is not ProviderContinuationState.RECONCILIATION_REQUIRED:
                    break
        if chosen is None:
            chosen = ProviderContinuationResult(
                ProviderContinuationState.RECONCILIATION_REQUIRED,
                "provider_continuation_adapter_unavailable",
                provider_references=dict(
                    attempt.get("provider_references") or {}
                ),
                failure_domain=FailureDomain.PROVIDER_TRANSPORT_FAILURE,
            )
        callback_id = chosen.callback_id or (
            "continuation_" + hashlib.sha256(
                f"{attempt_id}:{chosen.state.value}:{chosen.reason_code}".encode(
                    "utf-8"
                )
            ).hexdigest()[:32]
        )
        mapped_outcome = {
            ProviderContinuationState.STILL_RUNNING: "still_running",
            ProviderContinuationState.COMPLETED_WITH_RECEIPT:
                "completed_with_receipt",
            ProviderContinuationState.FAILED_BEFORE_EFFECT:
                "failed_before_effect",
            ProviderContinuationState.EXPLICIT_NOT_APPLIED:
                "explicit_not_applied",
        }.get(chosen.state, "reconciliation_required")
        updated, authoritative = self.store.apply_provider_callback(
            attempt_id,
            callback_id=callback_id,
            execution_epoch=str(attempt.get("execution_epoch") or ""),
            outcome=mapped_outcome,
            provider_references=chosen.provider_references,
            reason_code=chosen.reason_code,
            evidence_ref=chosen.evidence_ref,
            failure_domain=(
                chosen.failure_domain.value if chosen.failure_domain else ""
            ),
        )
        if authoritative and chosen.state in {
            ProviderContinuationState.COMPLETED_WITH_RECEIPT,
            ProviderContinuationState.FAILED_BEFORE_EFFECT,
            ProviderContinuationState.EXPLICIT_NOT_APPLIED,
        }:
            try:
                self.store.record_terminal_result(
                    attempt_id,
                    status=(
                        "succeeded"
                        if chosen.state
                        is ProviderContinuationState.COMPLETED_WITH_RECEIPT
                        else "failed_before_effect"
                    ),
                    terminal_outcome=(
                        "success_verified"
                        if chosen.state
                        is ProviderContinuationState.COMPLETED_WITH_RECEIPT
                        else "confirmed_failure_before_effect"
                    ),
                    dispatch_state=(
                        "effect_applied"
                        if chosen.state
                        is ProviderContinuationState.COMPLETED_WITH_RECEIPT
                        else "cancelled_before_effect"
                        if (updated.get("action_truth") or {}).get(
                            "dispatch_started_at"
                        ) else "not_dispatched"
                    ),
                    reason_code=chosen.reason_code,
                )
            except (RuntimeError, TypeError, ValueError):
                # The fenced evidence is already durable. A concurrent terminal
                # owner may have won; it remains authoritative.
                pass
        reported_state = chosen.state
        reported_reason = chosen.reason_code
        if authority == "cancelled" and chosen.state not in {
            ProviderContinuationState.COMPLETED_WITH_RECEIPT,
            ProviderContinuationState.EXPLICIT_NOT_APPLIED,
        }:
            reported_state = ProviderContinuationState.CANCELLED_AUTHORITY
            reported_reason = "authoritative_user_cancellation"
        elif authority in {"terminal", "superseded"} and chosen.state not in {
            ProviderContinuationState.COMPLETED_WITH_RECEIPT,
            ProviderContinuationState.EXPLICIT_NOT_APPLIED,
            ProviderContinuationState.FAILED_BEFORE_EFFECT,
        }:
            reported_state = ProviderContinuationState.TERMINAL_AUTHORITY
            reported_reason = "attempt_execution_authority_terminal"
        return ProviderContinuationResult(
            reported_state,
            reported_reason,
            callback_id=callback_id,
            evidence_ref=chosen.evidence_ref,
            provider_references=dict(chosen.provider_references),
            failure_domain=chosen.failure_domain,
            observed_at=chosen.observed_at,
            execution_authorized=bool(
                authoritative
                and chosen.state is ProviderContinuationState.STILL_RUNNING
            ),
        )


def resume_or_reconcile_provider_attempt(
    attempt_id: str, *, store=None, adapters=(),
) -> ProviderContinuationResult:
    if store is None:
        from osia.tool_attempts import get_tool_attempt_store

        store = get_tool_attempt_store()
    return ProviderContinuationCoordinator(
        store, adapters=adapters,
    ).resume_or_reconcile_provider_attempt(attempt_id)


__all__ = [
    "ChatGPTAccountContinuationAdapter", "DurableReceiptAdapter",
    "LookupContinuationAdapter", "ProviderContinuationAdapter",
    "ProviderContinuationCoordinator", "ProviderContinuationResult",
    "ProviderContinuationState", "resume_or_reconcile_provider_attempt",
]
