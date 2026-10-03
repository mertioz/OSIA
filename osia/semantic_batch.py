"""Conservative typed batching for deterministic native semantic primitives.

The batch owns no dispatch mechanism.  It validates one already-known local
sequence, calls an injected exact primitive dispatcher, and exposes only one
final semantic checkpoint.  Every primitive remains independently journaled by
the caller at its actual OS/UIA dispatch boundary.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from osia.action_truth import CanonicalEffectClass, settle_policy_for

MAX_SEMANTIC_BATCH_PRIMITIVES = 8
_BATCH_SAFE_PATTERNS = frozenset({"Invoke", "SelectionItem", "Toggle"})


def _text(value: Any, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        return ""
    return text


def _observable_effect(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, Mapping) else {}
    kind = _text(raw.get("kind"), 48)
    surface_ref = _text(raw.get("surface_ref"), 160)
    expected_text = _text(raw.get("expected_text"), 240)
    if kind != "exact_surface_text" or not surface_ref or not expected_text:
        return {}
    return {
        "kind": kind,
        "surface_ref": surface_ref,
        "expected_text": expected_text,
    }


def normalize_semantic_batch(
    *,
    surface_ref: Any,
    objective: Any,
    action_pattern: Any,
    observable_effect: Any,
    semantic_batch: Any,
) -> tuple[dict[str, Any], str]:
    """Return one bounded safe plan or a stable pre-dispatch reason code."""

    raw_items = semantic_batch if isinstance(semantic_batch, list) else []
    if not 2 <= len(raw_items) <= MAX_SEMANTIC_BATCH_PRIMITIVES:
        return {}, "semantic_batch_size_invalid"
    items = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            return {}, "semantic_batch_primitive_invalid"
        item_ref = _text(raw.get("surface_ref"), 160)
        item_objective = _text(raw.get("objective"), 240)
        pattern = _text(raw.get("action_pattern"), 32)
        effect = _observable_effect(raw.get("observable_effect"))
        if not item_ref or not item_objective or not pattern or not effect:
            return {}, "semantic_batch_primitive_invalid"
        if pattern not in _BATCH_SAFE_PATTERNS:
            return {}, "semantic_batch_structural_transition"
        if raw.get("branch_independent") is not True:
            return {}, "semantic_batch_branch_boundary"
        if raw.get("transition_boundary") != "none":
            return {}, "semantic_batch_structural_transition"
        if raw.get("safety_class") != "reversible_local":
            return {}, "semantic_batch_sensitive_boundary"
        items.append({
            "index": index,
            "surface_ref": item_ref,
            "objective": item_objective,
            "action_pattern": pattern,
            "observable_effect": effect,
        })

    first = items[0]
    if (
        first["surface_ref"] != _text(surface_ref, 160)
        or first["objective"].casefold() != _text(objective, 240).casefold()
        or first["action_pattern"] != _text(action_pattern, 32)
    ):
        return {}, "semantic_batch_primary_identity_mismatch"
    final_effect = _observable_effect(observable_effect)
    if not final_effect or final_effect != items[-1]["observable_effect"]:
        return {}, "semantic_batch_final_checkpoint_mismatch"
    effect_identities = [
        (
            item["observable_effect"]["surface_ref"],
            item["observable_effect"]["expected_text"],
        )
        for item in items
    ]
    if len(set(effect_identities)) != len(effect_identities):
        return {}, "semantic_batch_prefix_not_exactly_reconcilable"
    material = json.dumps(
        {
            "items": items,
            "final_observable_effect": final_effect,
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": 1,
        "semantic_batch_id": "batch_" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()[:24],
        "items": items,
        "final_observable_effect": final_effect,
        "semantic_checkpoint_count": 1,
    }, ""


def _aggregate_result(
    plan: Mapping[str, Any],
    receipts: list[dict[str, Any]],
    *,
    outcome: str,
    dispatch_state: str,
    reason_code: str,
    stopped_at_index: int | None = None,
) -> dict[str, Any]:
    final_receipt = receipts[-1] if receipts else {}
    dispatched_receipts = [
        item for item in receipts
        if item.get("dispatch_state") in {"dispatched", "effect_unknown"}
    ]
    last_dispatched_receipt = (
        dispatched_receipts[-1] if dispatched_receipts else {}
    )
    primitive_source = last_dispatched_receipt or final_receipt
    primitive = primitive_source.get("primitive")
    primitive = primitive if isinstance(primitive, dict) else {}
    rejected_receipt = (
        final_receipt
        if final_receipt.get("dispatch_state") == "not_dispatched" else {}
    )
    dispatched_count = len(dispatched_receipts)
    if stopped_at_index is None:
        partial_state = "completed_batch"
    elif rejected_receipt and dispatched_count:
        partial_state = "partial_prefix_dispatched"
    elif rejected_receipt:
        partial_state = "failed_before_any_effect"
    else:
        partial_state = "ambiguous_in_batch"
    early_checkpoint = {}
    if partial_state == "partial_prefix_dispatched":
        prefix_item = list(plan.get("items") or [])[dispatched_count - 1]
        settle_policy = settle_policy_for(
            operation="desktop_semantic_batch_checkpoint",
            family="system",
            effect_class=CanonicalEffectClass.NON_IDEMPOTENT_WRITE,
        )
        early_checkpoint = {
            "checkpoint_id": (
                f"{str(plan.get('semantic_batch_id') or '')}.prefix."
                f"{dispatched_count}"
            ),
            "prefix_length": dispatched_count,
            "observable_effect": dict(
                prefix_item.get("observable_effect") or {}
            ),
            "settle_policy": settle_policy.to_dict(),
            # Compatibility projection for older readers.  These values are
            # derived from the typed local-UI policy, not a universal law.
            "settle_slot_budget": settle_policy.max_probes,
            "settle_interval_seconds": (
                settle_policy.probe_delays_seconds[1]
                if len(settle_policy.probe_delays_seconds) > 1 else 0.0
            ),
        }
    batch = {
        "schema_version": 1,
        "semantic_batch_id": str(plan.get("semantic_batch_id") or ""),
        "semantic_checkpoint_count": 1,
        "primitive_count": len(plan.get("items") or []),
        "partial_batch_state": partial_state,
        "dispatched_prefix_length": dispatched_count,
        "dispatched_count": dispatched_count,
        "completed_dispatch_count": sum(
            1 for item in receipts if item.get("dispatch_state") == "dispatched"
        ),
        "stopped_at_index": stopped_at_index,
        "rejected_primitive_index": (
            stopped_at_index if rejected_receipt else None
        ),
        "rejection_reason": str(rejected_receipt.get("reason_code") or ""),
        "early_checkpoint": early_checkpoint,
        "primitive_receipts": receipts,
    }
    return {
        "text": (
            "Deterministic semantic batch dispatched; one final read-only "
            "checkpoint is required."
            if outcome in {"success_unverified", "success_verified"} else
            "ERROR: semantic batch stopped; later primitives were not dispatched."
        ),
        "receipt": {
            "schema_version": 1,
            "dispatch_state": dispatch_state,
            "outcome": outcome,
            "reason_code": reason_code,
            "surface_ref": str(primitive_source.get("surface_ref") or ""),
            "structural_epoch": primitive_source.get("structural_epoch", 0),
            "primitive": dict(primitive),
            "semantic_batch": batch,
        },
    }


def execute_semantic_batch(
    plan: Mapping[str, Any],
    dispatch: Callable[[dict[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Dispatch a safe plan until its first uncertain or stale primitive."""

    receipts: list[dict[str, Any]] = []
    baseline_epoch: int | None = None
    items = list(plan.get("items") or [])
    for index, item in enumerate(items):
        try:
            result = dispatch(dict(item))
        except Exception:
            result = {}
        receipt = result.get("receipt") if isinstance(result, Mapping) else None
        receipt = dict(receipt) if isinstance(receipt, Mapping) else {}
        state = str(receipt.get("dispatch_state") or "not_dispatched")
        outcome = str(receipt.get("outcome") or "")
        if state == "not_dispatched":
            receipts.append(receipt)
            stopped_reason = str(receipt.get("reason_code") or "")
            dispatched_prefix = any(
                item.get("dispatch_state") in {"dispatched", "effect_unknown"}
                for item in receipts[:-1]
            )
            return _aggregate_result(
                plan, receipts,
                outcome=(
                    "outcome_unknown" if dispatched_prefix else
                    "confirmed_failure_before_effect"
                ),
                dispatch_state=(
                    "effect_unknown" if dispatched_prefix else "not_dispatched"
                ),
                reason_code=(
                    "semantic_batch_prefix_checkpoint_pending"
                    if dispatched_prefix else
                    stopped_reason or "semantic_batch_dispatch_unavailable"
                ),
                stopped_at_index=index,
            )
        receipts.append(receipt)
        if state != "dispatched" or outcome not in {
            "success_unverified", "success_verified",
        }:
            return _aggregate_result(
                plan, receipts,
                outcome="outcome_unknown",
                dispatch_state="effect_unknown",
                reason_code="semantic_batch_primitive_outcome_unknown",
                stopped_at_index=index,
            )
        try:
            epoch = int(receipt.get("structural_epoch") or 0)
        except (TypeError, ValueError, OverflowError):
            epoch = 0
        if baseline_epoch is None:
            baseline_epoch = epoch
        elif index < len(items) - 1 and epoch != baseline_epoch:
            return _aggregate_result(
                plan, receipts,
                outcome="outcome_unknown",
                dispatch_state="effect_unknown",
                reason_code="semantic_batch_structural_transition",
                stopped_at_index=index,
            )
    final = receipts[-1]
    return _aggregate_result(
        plan,
        receipts,
        outcome=str(final.get("outcome") or "success_unverified"),
        dispatch_state="dispatched",
        reason_code="semantic_batch_dispatched",
    )


def classify_batch_observation(
    plan: Mapping[str, Any],
    observed_effect: Any,
    *,
    applied_subset_ambiguous: bool = False,
) -> dict[str, Any]:
    """Classify exact final/prefix evidence without authorizing a replay."""

    batch_id = str(plan.get("semantic_batch_id") or "")
    items = list(plan.get("items") or [])
    if applied_subset_ambiguous:
        return {
            "state": "outcome_unknown",
            "semantic_batch_id": batch_id,
            "resume_from_index": None,
            "anti_replay_required": False,
        }
    raw_observations = (
        observed_effect if isinstance(observed_effect, list)
        else [observed_effect]
    )
    effects = [
        effect for effect in (
            _observable_effect(item) for item in raw_observations[:16]
        )
        if effect
    ]
    matches = sorted({
        int(item.get("index", index))
        for index, item in enumerate(items)
        if isinstance(item, Mapping)
        and item.get("observable_effect") in effects
    })
    if not matches:
        return {
            "state": "outcome_unknown",
            "semantic_batch_id": batch_id,
            "resume_from_index": None,
            "anti_replay_required": False,
        }
    normalized_final = _observable_effect(plan.get("final_observable_effect"))
    normalized_ordered_plan = bool(
        items
        and normalized_final
        and normalized_final == _observable_effect(
            items[-1].get("observable_effect")
            if isinstance(items[-1], Mapping) else {}
        )
    )
    if normalized_ordered_plan:
        # A normalized batch is strictly ordered and every primitive owns a
        # unique exact post-primitive witness. Observing witness N therefore
        # proves the applied prefix through N even when earlier transient
        # witness text is no longer visible in the final surface state.
        applied_count = matches[-1] + 1
    else:
        # Legacy/untrusted plan shapes do not carry the canonical final
        # checkpoint contract. Keep their discontinuous evidence fail-closed.
        matched_indexes = set(matches)
        applied_count = 0
        while applied_count in matched_indexes:
            applied_count += 1
        if applied_count == 0 or any(
            index >= applied_count for index in matched_indexes
        ):
            return {
                "state": "outcome_unknown",
                "semantic_batch_id": batch_id,
                "resume_from_index": None,
                "anti_replay_required": False,
            }
    if applied_count == len(items):
        return {
            "state": "verified",
            "semantic_batch_id": batch_id,
            "resume_from_index": len(items),
            "anti_replay_required": False,
        }
    return {
        "state": "exact_prefix",
        "semantic_batch_id": batch_id,
        "applied_count": applied_count,
        "not_applied_count": len(items) - applied_count,
        "resume_from_index": applied_count,
        "anti_replay_required": False,
    }


__all__ = [
    "MAX_SEMANTIC_BATCH_PRIMITIVES",
    "classify_batch_observation",
    "execute_semantic_batch",
    "normalize_semantic_batch",
]
