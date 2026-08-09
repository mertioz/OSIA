#!/usr/bin/env python3
"""Turn-scoped deterministic execution strategy (WOR-248).

This module is deliberately orchestration-only.  It does not execute tools and
does not replace provider-native capabilities.  It gives Chat and Autonomous a
bounded semantic goal, an ordered set of applicable mechanisms, and a small
state machine which validates every fallback before dispatch.

The canonical order is:

0. exact direct capability (provider/API/MCP/native tool),
1. deep link or deterministic route,
2. DOM/Playwright,
3. native accessibility action,
4. keyboard navigation with observable focus,
5. local/zoomed vision,
6. global vision and coordinates.

Only registered/applicable mechanisms participate.  A failed mutation with an
ambiguous effect freezes all alternatives until the existing attempt is
reconciled; identical failed actions need new evidence before they can run
again; stale targets need an explicit read-only re-resolution observation.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from enum import Enum, IntEnum
from typing import Iterable
from urllib.parse import urlparse


class StrategyLayer(IntEnum):
    DIRECT_CAPABILITY = 0
    DEEP_LINK = 1
    DOM = 2
    NATIVE_ACCESSIBILITY = 3
    KEYBOARD = 4
    LOCAL_VISION = 5
    GLOBAL_VISION = 6


_LAYER_LABELS = {
    StrategyLayer.DIRECT_CAPABILITY: "exact_direct_capability",
    StrategyLayer.DEEP_LINK: "deep_link_or_deterministic_route",
    StrategyLayer.DOM: "dom_or_playwright",
    StrategyLayer.NATIVE_ACCESSIBILITY: "native_accessibility",
    StrategyLayer.KEYBOARD: "keyboard_with_observable_focus",
    StrategyLayer.LOCAL_VISION: "local_or_zoomed_vision",
    StrategyLayer.GLOBAL_VISION: "global_vision_and_coordinates",
}


def _normalized(value: object) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def _bounded(value: object, maximum: int = 180) -> str:
    return " ".join(str(value or "").split())[:maximum]


_SURFACE_ALIASES = {
    "library": ("library", "bibliotheque"),
    "store": ("store", "shop", "boutique"),
    "settings": ("settings", "preferences", "parametres", "reglages"),
    "downloads": ("downloads", "telechargements"),
    "friends": ("friends", "amis"),
    "profile": ("profile", "profil"),
    "inbox": ("inbox", "boite de reception", "boite mail"),
}

_ACTION_PATTERNS = (
    ("search", r"\b(search|find|look for|cherche|chercher|recherche|rechercher)\b"),
    ("open", r"\b(open|launch|start|ouvre|ouvrir|lance|lancer|demarre|demarrer)\b"),
    ("navigate", r"\b(go to|navigate|visit|va a|aller a|rends toi|accede)\b"),
    ("activate", r"\b(click|select|activate|clique|selectionne|active)\b"),
    ("type", r"\b(type|enter|write|saisis|ecris|tape)\b"),
    ("send", r"\b(send|submit|envoie|envoyer|soumet|soumettre)\b"),
    ("create", r"\b(create|add|cree|creer|ajoute|ajouter)\b"),
    ("delete", r"\b(delete|remove|supprime|supprimer|efface|effacer)\b"),
)

_KNOWN_APPLICATIONS = (
    "steam", "spotify", "vscode", "visual studio code", "discord",
    "brave", "chrome", "firefox", "edge", "outlook", "gmail",
)


@dataclass(frozen=True)
class SemanticGoal:
    target_object: str
    required_application: str
    required_surface: str
    required_account: str
    requested_action: str
    observable_success_postcondition: str
    fingerprint: str
    requires_execution: bool = False
    required_postconditions: tuple[str, ...] = ()

    def public_dict(self) -> dict:
        return asdict(self)


def derive_semantic_goal(
    request: str,
    *,
    postcondition: str = "",
    postconditions: Iterable[str] = (),
) -> SemanticGoal:
    """Derive bounded goal invariants without an extra model call.

    The parser intentionally extracts only explicit constraints.  Missing
    surface/account values stay empty, which permits broader equivalents.  It
    never invents a native-vs-web requirement merely from an application name.
    """

    source = _bounded(request, 4000)
    norm = _normalized(source)

    application = ""
    for candidate in _KNOWN_APPLICATIONS:
        if re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", norm):
            application = "vscode" if candidate == "visual studio code" else candidate
            break

    surface = ""
    for canonical, aliases in _SURFACE_ALIASES.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", norm) for alias in aliases):
            surface = canonical
            break

    account = ""
    if re.search(r"\b(my|mine|our|mon|ma|mes|notre|nos)\b", norm):
        account = "user_account"
    elif re.search(r"\b(account|compte|profile|profil)\s+[^,.;]{1,48}", norm):
        account = "specified_account"

    action = ""
    action_match = None
    for canonical, pattern in _ACTION_PATTERNS:
        for match in re.finditer(pattern, norm):
            if (
                (match.start() > 0 and norm[match.start() - 1] in "-_")
                or (match.end() < len(norm) and norm[match.end()] in "-_")
            ):
                continue
            if action_match is None or match.start() > action_match.start():
                action = canonical
                action_match = match

    target = ""
    quoted = re.findall(r"[\"'\u201c\u201d\u00ab\u00bb]([^\"'\u201c\u201d\u00ab\u00bb]{1,160})[\"'\u201c\u201d\u00ab\u00bb]", source)
    if quoted:
        target = _bounded(quoted[-1], 160)
    if action == "search" and not quoted:
        search_match = re.search(
            r"\b(?:search(?:\s+for)?|find|look\s+for|cherche(?:r)?|"
            r"recherche(?:r)?)\b\s+(?:for\s+|pour\s+)?(.{1,220})$",
            norm,
        )
        if search_match:
            candidate = search_match.group(1).strip(" .?!,;:")
            candidate = re.split(
                r"(?:[;!?]|\.\s+)",
                candidate,
                maxsplit=1,
            )[0].strip()
            if " for " in candidate:
                candidate = candidate.rsplit(" for ", 1)[-1]
            if " pour " in candidate:
                candidate = candidate.rsplit(" pour ", 1)[-1]
            target = _bounded(candidate, 160)
    elif action_match is not None and not quoted:
        candidate = norm[action_match.end():].strip(" .?!,;:")
        candidate = re.split(r"[.;!?]", candidate, maxsplit=1)[0].strip()
        candidate = re.sub(
            r"^(?:to|for|on|at|in|the|a|an|le|la|les|un|une|des|du|de|a|au|aux)\s+",
            "",
            candidate,
        )
        candidate = re.split(
            r"\b(?:and then|then|and|puis|ensuite|et)\b",
            candidate,
            maxsplit=1,
        )[0].strip(" .?!,;:")
        if candidate:
            target = _bounded(candidate, 160)
    if not target:
        target = application or surface or _bounded(source, 160)

    explicit_postconditions = tuple(
        _bounded(item, 320) for item in postconditions if _bounded(item, 320)
    )
    if postcondition:
        success = _bounded(postcondition, 320)
    elif explicit_postconditions:
        success = explicit_postconditions[0]
    else:
        where = " ".join(part for part in (application, surface) if part)
        if action == "search":
            success = f"{target} is visible in the search results"
            if where:
                success += f" on {where}"
        elif action in {"open", "navigate"}:
            success = f"{where or target} is visibly open at the requested destination"
        elif action:
            success = f"the requested {action} on {target} is observably complete"
        else:
            success = f"the requested result for {target} is observable"

    requires_execution = bool(action) and bool(
        application
        or surface
        or re.search(
            r"\b(click|type|send|submit|create|delete|remove|open|launch|"
            r"navigate|search|clique|saisis|envoie|cree|supprime|ouvre|lance|va)\b",
            norm,
        )
    )
    if (
        action == "create"
        and not application
        and not surface
        and not re.search(
            r"\b(file|folder|directory|spreadsheet|workbook|document|pdf|"
            r"presentation|slides?|calendar|event|reminder|scheduled task|"
            r"account|record|database|table|project|issue|ticket|fichier|"
            r"dossier|repertoire|classeur|feuille de calcul|document|"
            r"presentation|diapos?|calendrier|evenement|rappel|tache|compte|"
            r"enregistrement|base de donnees|tableau|projet|ticket)\b",
            norm,
        )
    ):
        requires_execution = False
    required = explicit_postconditions or (success,)
    payload = {
        "target_object": target,
        "required_application": application,
        "required_surface": surface,
        "required_account": account,
        "requested_action": action,
        "observable_success_postcondition": success,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {**payload, "required_postconditions": required},
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    return SemanticGoal(
        **payload,
        fingerprint=fingerprint,
        requires_execution=requires_execution,
        required_postconditions=required,
    )


@dataclass(frozen=True)
class StrategyMechanism:
    mechanism_id: str
    layer: StrategyLayer
    reason_selected: str
    application: str = ""
    surface: str = ""
    account: str = ""
    applicable: bool = True
    covers_goal: bool = True
    mutation: bool = False

    @property
    def label(self) -> str:
        return _LAYER_LABELS[self.layer]


class AttemptStatus(str, Enum):
    SELECTED = "selected"
    PROGRESSED = "progressed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STAGNATED = "stagnated"
    STALE = "stale_target"
    AMBIGUOUS = "ambiguous_effect"
    BLOCKED = "blocked"


@dataclass
class StrategyAttempt:
    attempt_id: str
    mechanism_id: str
    layer: int
    mechanism: str
    reason_selected: str
    action_signature: str
    status: str = AttemptStatus.SELECTED.value
    observations_gained: list[str] = field(default_factory=list)
    failure_or_stagnation_reason: str = ""
    alternatives_still_applicable: list[str] = field(default_factory=list)
    postcondition_evidence: str = ""

    def public_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StrategyDecision:
    allowed: bool
    reason_code: str
    attempt_id: str = ""
    mechanism_id: str = ""


_TERMINAL_FOR_ESCALATION = frozenset({
    AttemptStatus.PROGRESSED.value,
    AttemptStatus.SUCCEEDED.value,
    AttemptStatus.FAILED.value,
    AttemptStatus.STAGNATED.value,
    AttemptStatus.STALE.value,
    AttemptStatus.AMBIGUOUS.value,
})


class ExecutionStrategy:
    """Bounded strategy state for one active semantic goal."""

    _MAX_IDENTICAL_ACTION_ATTEMPTS = 2

    def __init__(self, goal: SemanticGoal, mechanisms: Iterable[StrategyMechanism] = ()):
        self.goal = goal
        self._mechanisms: dict[str, StrategyMechanism] = {}
        self.attempts: list[StrategyAttempt] = []
        self.current_mechanism = ""
        self.current_layer: int | None = None
        self.postcondition_evidence: list[str] = []
        self.proven_postconditions: dict[str, str] = {}
        self.succeeded = False
        self.frozen_reason = ""
        self._evidence_revision = 0
        self._evidence_tokens: set[str] = set()
        self._failed_signatures: dict[str, int] = {}
        self._signature_attempt_counts: dict[str, int] = {}
        self._stale_mechanisms: dict[str, int] = {}
        self._terminal_prompt_tokens: set[str] = set()
        self._lock = threading.RLock()
        for mechanism in mechanisms:
            self.register(mechanism)

    def register(self, mechanism: StrategyMechanism) -> None:
        with self._lock:
            self._mechanisms.setdefault(mechanism.mechanism_id, mechanism)

    def _semantic_error(self, mechanism: StrategyMechanism) -> str:
        goal = self.goal
        if (
            goal.required_application
            and mechanism.application
            and _normalized(goal.required_application) != _normalized(mechanism.application)
        ):
            return "required_application_changed"
        if (
            goal.required_surface
            and mechanism.surface
            and _normalized(goal.required_surface) != _normalized(mechanism.surface)
        ):
            return "required_surface_changed"
        if (
            goal.required_account
            and mechanism.account
            and _normalized(goal.required_account) != _normalized(mechanism.account)
        ):
            return "required_account_changed"
        return ""

    def _exhausted(self, mechanism_id: str) -> bool:
        return any(
            attempt.mechanism_id == mechanism_id
            and attempt.status in _TERMINAL_FOR_ESCALATION
            for attempt in self.attempts
        )

    def remaining_mechanisms(self) -> list[StrategyMechanism]:
        with self._lock:
            return sorted(
                (
                    mechanism for mechanism in self._mechanisms.values()
                    if mechanism.applicable
                    and not self._semantic_error(mechanism)
                    and not self._exhausted(mechanism.mechanism_id)
                ),
                key=lambda item: (int(item.layer), item.mechanism_id),
            )

    def _higher_unexhausted(self, layer: StrategyLayer) -> list[StrategyMechanism]:
        return [
            item for item in self.remaining_mechanisms()
            if int(item.layer) < int(layer)
        ]

    @staticmethod
    def action_signature(mechanism_id: str, action: object) -> str:
        payload = json.dumps(
            {"mechanism": mechanism_id, "action": action},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            default=lambda _value: "<unsupported>",
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def preview(self, mechanism: StrategyMechanism) -> StrategyDecision:
        with self._lock:
            if self.succeeded:
                return StrategyDecision(False, "postcondition_already_satisfied")
            if self.frozen_reason:
                return StrategyDecision(False, "ambiguous_effect_requires_reconciliation")
            if any(
                self._evidence_revision <= revision
                for revision in self._stale_mechanisms.values()
            ):
                return StrategyDecision(False, "stale_target_requires_reresolution")
            if not mechanism.applicable:
                return StrategyDecision(False, "mechanism_not_applicable")
            semantic_error = self._semantic_error(mechanism)
            if semantic_error:
                return StrategyDecision(False, semantic_error)
            higher = self._higher_unexhausted(mechanism.layer)
            if higher:
                return StrategyDecision(
                    False,
                    "higher_strategy_still_applicable:" + ",".join(
                        item.mechanism_id for item in higher[:8]
                    ),
                )
            return StrategyDecision(True, "strategy_selected", mechanism_id=mechanism.mechanism_id)

    def select(self, mechanism: StrategyMechanism, action: object) -> StrategyDecision:
        with self._lock:
            self.register(mechanism)
            preview = self.preview(mechanism)
            signature = self.action_signature(mechanism.mechanism_id, action)
            if not preview.allowed:
                duplicate = next(
                    (
                        item for item in reversed(self.attempts)
                        if item.action_signature == signature
                        and item.status == AttemptStatus.BLOCKED.value
                        and item.failure_or_stagnation_reason == preview.reason_code
                    ),
                    None,
                )
                if duplicate is not None:
                    return StrategyDecision(
                        False, preview.reason_code, duplicate.attempt_id,
                        mechanism.mechanism_id,
                    )
                attempt = StrategyAttempt(
                    attempt_id=f"strategy_{len(self.attempts) + 1:03d}",
                    mechanism_id=mechanism.mechanism_id,
                    layer=int(mechanism.layer),
                    mechanism=mechanism.label,
                    reason_selected=mechanism.reason_selected,
                    action_signature=signature,
                    status=AttemptStatus.BLOCKED.value,
                    failure_or_stagnation_reason=preview.reason_code,
                    alternatives_still_applicable=[
                        item.mechanism_id for item in self.remaining_mechanisms()
                    ],
                )
                self.attempts.append(attempt)
                return StrategyDecision(
                    False, preview.reason_code, attempt.attempt_id,
                    mechanism.mechanism_id,
                )
            if (
                self._signature_attempt_counts.get(signature, 0)
                >= self._MAX_IDENTICAL_ACTION_ATTEMPTS
            ):
                return StrategyDecision(False, "identical_action_retry_budget_exhausted")
            stale_revision = self._stale_mechanisms.get(mechanism.mechanism_id)
            if stale_revision is not None and self._evidence_revision <= stale_revision:
                return StrategyDecision(False, "stale_target_requires_reresolution")
            failed_revision = self._failed_signatures.get(signature)
            if failed_revision is not None and self._evidence_revision <= failed_revision:
                return StrategyDecision(False, "identical_failed_action_without_new_evidence")
            attempt = StrategyAttempt(
                attempt_id=f"strategy_{len(self.attempts) + 1:03d}",
                mechanism_id=mechanism.mechanism_id,
                layer=int(mechanism.layer),
                mechanism=mechanism.label,
                reason_selected=mechanism.reason_selected,
                action_signature=signature,
                alternatives_still_applicable=[
                    item.mechanism_id for item in self.remaining_mechanisms()
                    if item.mechanism_id != mechanism.mechanism_id
                ],
            )
            self.attempts.append(attempt)
            self._signature_attempt_counts[signature] = (
                self._signature_attempt_counts.get(signature, 0) + 1
            )
            self.current_mechanism = mechanism.mechanism_id
            self.current_layer = int(mechanism.layer)
            return StrategyDecision(
                True, "strategy_selected", attempt.attempt_id,
                mechanism.mechanism_id,
            )

    def observe(self, observation: str, *, evidence_token: str = "") -> None:
        with self._lock:
            token = _bounded(evidence_token or observation, 120)
            if not token or token in self._evidence_tokens:
                return
            self._evidence_tokens.add(token)
            self._evidence_revision += 1
            if self.attempts:
                self.attempts[-1].observations_gained.append(token)

    def record(
        self,
        decision: StrategyDecision,
        outcome: str,
        *,
        observation: str = "",
        failure_reason: str = "",
        postcondition_evidence: str = "",
    ) -> StrategyAttempt | None:
        with self._lock:
            attempt = next(
                (item for item in reversed(self.attempts) if item.attempt_id == decision.attempt_id),
                None,
            )
            if attempt is None or not decision.allowed:
                return attempt
            mechanism = self._mechanisms[attempt.mechanism_id]
            normalized_outcome = _normalized(outcome).replace(" ", "_")
            if observation:
                attempt.observations_gained.append(_bounded(observation, 160))
                self._evidence_revision += 1
            if normalized_outcome in {"ambiguous", "pending", "unknown_after_start"}:
                attempt.status = AttemptStatus.AMBIGUOUS.value
                attempt.failure_or_stagnation_reason = (
                    _bounded(failure_reason, 120) or "effect_possible_postcondition_unknown"
                )
                self.frozen_reason = attempt.failure_or_stagnation_reason
            elif normalized_outcome == "stale":
                attempt.status = AttemptStatus.STALE.value
                attempt.failure_or_stagnation_reason = (
                    _bounded(failure_reason, 120) or "stale_target"
                )
                self._stale_mechanisms[attempt.mechanism_id] = self._evidence_revision
                self._failed_signatures[attempt.action_signature] = self._evidence_revision
            elif normalized_outcome in {"stagnated", "stagnation"}:
                attempt.status = AttemptStatus.STAGNATED.value
                attempt.failure_or_stagnation_reason = (
                    _bounded(failure_reason, 120) or "strategy_stagnated"
                )
                self._failed_signatures[attempt.action_signature] = self._evidence_revision
            elif normalized_outcome in {"failed", "failure", "rejected"}:
                attempt.status = AttemptStatus.FAILED.value
                attempt.failure_or_stagnation_reason = (
                    _bounded(failure_reason, 120) or "strategy_failed"
                )
                self._failed_signatures[attempt.action_signature] = self._evidence_revision
            elif normalized_outcome in {"progress", "progressed"}:
                attempt.status = AttemptStatus.PROGRESSED.value
            elif normalized_outcome in {"succeeded", "success"}:
                evidence = _bounded(postcondition_evidence, 180)
                if not evidence:
                    attempt.status = AttemptStatus.FAILED.value
                    attempt.failure_or_stagnation_reason = "postcondition_unverified"
                    self._failed_signatures[attempt.action_signature] = self._evidence_revision
                elif mechanism.covers_goal and len(self.goal.required_postconditions) == 1:
                    attempt.status = AttemptStatus.SUCCEEDED.value
                    attempt.postcondition_evidence = evidence
                    self.postcondition_evidence.append(evidence)
                    self.proven_postconditions[
                        self.goal.required_postconditions[0]
                    ] = evidence
                    self.succeeded = True
                else:
                    attempt.status = AttemptStatus.PROGRESSED.value
                    attempt.postcondition_evidence = evidence
                    self.postcondition_evidence.append(evidence)
            else:
                attempt.status = AttemptStatus.FAILED.value
                attempt.failure_or_stagnation_reason = "invalid_strategy_outcome"
                self._failed_signatures[attempt.action_signature] = self._evidence_revision
            attempt.alternatives_still_applicable = [
                item.mechanism_id for item in self.remaining_mechanisms()
            ]
            return attempt

    def reconcile(self, *, succeeded: bool, evidence: str = "") -> None:
        with self._lock:
            if not self.frozen_reason:
                return
            if succeeded and evidence:
                bounded = _bounded(evidence, 180)
                self.postcondition_evidence.append(bounded)
                required = self.goal.required_postconditions or (
                    self.goal.observable_success_postcondition,
                )
                if len(required) == 1:
                    self.proven_postconditions[required[0]] = bounded
                    self.succeeded = True
            self.frozen_reason = ""
            self._evidence_revision += 1

    def verify_postcondition(
        self,
        evidence: str,
        *,
        postcondition: str = "",
    ) -> bool:
        """Accept only explicit read-only evidence for the goal postcondition."""

        with self._lock:
            bounded = _bounded(evidence, 180)
            if not bounded or self.frozen_reason:
                return False
            required = self.goal.required_postconditions or (
                self.goal.observable_success_postcondition,
            )
            target = _bounded(postcondition, 320)
            if not target and len(required) == 1:
                target = required[0]
            if target not in required:
                return False
            self.proven_postconditions[target] = bounded
            self.succeeded = all(item in self.proven_postconditions for item in required)
            self.postcondition_evidence.append(bounded)
            self._evidence_revision += 1
            return True

    def final_diagnostic(self) -> dict:
        with self._lock:
            remaining = [item.mechanism_id for item in self.remaining_mechanisms()]
            if self.succeeded:
                code = "postcondition_verified"
                next_action = "complete"
            elif self.frozen_reason:
                code = "ambiguous_effect_requires_reconciliation"
                next_action = "reconcile_existing_attempt"
            elif remaining:
                code = "safe_strategies_remain"
                next_action = "continue_with_next_strategy"
            else:
                code = "all_semantically_valid_strategies_exhausted"
                next_action = "precise_failure_or_clarification"
            return {
                "code": code,
                "goal_fingerprint": self.goal.fingerprint,
                "remaining": remaining,
                "failure_reasons": [
                    item.failure_or_stagnation_reason
                    for item in self.attempts
                    if item.failure_or_stagnation_reason
                ][-8:],
                "postcondition_evidence": list(self.postcondition_evidence[-8:]),
                "next_action": next_action,
            }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "goal": self.goal.public_dict(),
                "current_mechanism": self.current_mechanism,
                "current_layer": self.current_layer,
                "attempts": [item.public_dict() for item in self.attempts[-16:]],
                "mechanisms": [
                    {
                        **asdict(item),
                        "layer": int(item.layer),
                    }
                    for item in sorted(
                        self._mechanisms.values(),
                        key=lambda value: (int(value.layer), value.mechanism_id),
                    )
                ],
                "alternatives_still_applicable": [
                    item.mechanism_id for item in self.remaining_mechanisms()
                ],
                "postcondition_evidence": list(self.postcondition_evidence[-8:]),
                "proven_postconditions": dict(self.proven_postconditions),
                "frozen_reason": self.frozen_reason,
                "succeeded": self.succeeded,
                "evidence_revision": self._evidence_revision,
                "evidence_tokens": sorted(self._evidence_tokens)[-64:],
                "failed_signatures": dict(self._failed_signatures),
                "signature_attempt_counts": dict(self._signature_attempt_counts),
                "stale_mechanisms": dict(self._stale_mechanisms),
                "terminal_prompt_tokens": sorted(self._terminal_prompt_tokens)[-32:],
            }

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "ExecutionStrategy":
        """Restore the exact frontier for another provider segment."""

        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("goal"), dict):
            raise ValueError("invalid execution strategy snapshot")
        goal_data = dict(snapshot["goal"])
        goal_data["required_postconditions"] = tuple(
            goal_data.get("required_postconditions") or ()
        )
        goal = SemanticGoal(**goal_data)
        mechanisms = []
        for raw in snapshot.get("mechanisms") or []:
            if not isinstance(raw, dict):
                continue
            data = dict(raw)
            data["layer"] = StrategyLayer(int(data["layer"]))
            mechanisms.append(StrategyMechanism(**data))
        strategy = cls(goal, mechanisms)
        strategy.attempts = [
            StrategyAttempt(**dict(item))
            for item in snapshot.get("attempts") or []
            if isinstance(item, dict)
        ]
        strategy.current_mechanism = str(snapshot.get("current_mechanism") or "")
        layer = snapshot.get("current_layer")
        strategy.current_layer = int(layer) if layer is not None else None
        strategy.postcondition_evidence = list(snapshot.get("postcondition_evidence") or [])
        strategy.proven_postconditions = dict(snapshot.get("proven_postconditions") or {})
        strategy.frozen_reason = str(snapshot.get("frozen_reason") or "")
        strategy.succeeded = bool(snapshot.get("succeeded"))
        strategy._evidence_revision = int(snapshot.get("evidence_revision") or 0)
        strategy._evidence_tokens = set(snapshot.get("evidence_tokens") or [])
        strategy._failed_signatures = {
            str(key): int(value)
            for key, value in (snapshot.get("failed_signatures") or {}).items()
        }
        strategy._signature_attempt_counts = {
            str(key): int(value)
            for key, value in (snapshot.get("signature_attempt_counts") or {}).items()
        }
        strategy._stale_mechanisms = {
            str(key): int(value)
            for key, value in (snapshot.get("stale_mechanisms") or {}).items()
        }
        strategy._terminal_prompt_tokens = set(snapshot.get("terminal_prompt_tokens") or [])
        return strategy

    def model_instruction(self) -> str:
        available = [
            {
                "id": item.mechanism_id,
                "layer": int(item.layer),
                "mechanism": item.label,
                "reason": item.reason_selected,
            }
            for item in self.remaining_mechanisms()
        ]
        payload = {
            "semantic_goal": self.goal.public_dict(),
            "available_strategy_layers": available,
        }
        return (
            "[Internal deterministic execution contract. Preserve target, required "
            "application/surface/account, action, and observable postcondition across "
            "fallbacks. Inspect capabilities first and use the earliest applicable "
            "layer in this order: 0 exact direct provider/API/MCP/OSIA capability; "
            "1 deep link or deterministic route; 2 DOM/Playwright; 3 native "
            "accessibility; 4 keyboard with observable focus; 5 local/zoomed vision; "
            "6 global coordinates. Do not replay a failed action "
            "without new evidence; re-resolve stale targets; reconcile any possible "
            "effect before another mutation. Structured state: "
            + json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            + "]"
        )

    def model_feedback(self) -> str:
        return (
            "[Internal execution_strategy: "
            + json.dumps(
                self.final_diagnostic(), sort_keys=True, ensure_ascii=True,
                separators=(",", ":"),
            )
            + "]"
        )

    def premature_final_instruction(self) -> str:
        """Return one bounded correction for each unchanged strategy state."""

        with self._lock:
            if not self.goal.requires_execution or self.succeeded or self.frozen_reason:
                return ""
            remaining = self.remaining_mechanisms()
            if not remaining:
                return ""
            token = hashlib.sha256(
                json.dumps(
                    {
                        "attempts": [
                            (item.mechanism_id, item.status, item.failure_or_stagnation_reason)
                            for item in self.attempts
                        ],
                        "remaining": [item.mechanism_id for item in remaining],
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()[:20]
            if token in self._terminal_prompt_tokens:
                return ""
            self._terminal_prompt_tokens.add(token)
            return (
                "The exact goal is not yet supported by postcondition evidence and "
                "safe semantically equivalent strategies remain: "
                + ", ".join(item.mechanism_id for item in remaining[:8])
                + ". Continue with the earliest applicable strategy. Do not claim "
                "success, change the required surface/account, or ask the user for "
                "help yet."
            )


_OBSERVATION_TOOLS = frozenset({
    "take_screenshot", "is_running", "is_installed", "has_window",
    "steam_game_state", "web_state", "get_current_url", "read_page",
    "get_page_text", "inspect_browser_page", "inspect_form_fields",
    "list_browser_tabs", "get_browser_download", "read_file", "list_files",
    "search_files",
})

_DOM_ACTION_TOOLS = frozenset({
    "navigate", "click", "find_and_click", "fill", "type_text", "press_key",
    "navigate_browser", "search_web", "open_search_result",
    "activate_browser_element", "fill_form_field", "handle_browser_dialog",
    "upload_browser_file", "download_browser_file", "go_back", "go_forward",
    "open_new_tab", "close_tab", "switch_tab", "go_back_browser",
    "go_forward_browser", "open_browser_tab", "close_browser_tab",
    "switch_browser_tab", "paginate_browser", "scroll_infinite",
})


def _uri_identity(uri: str) -> tuple[str, str, str]:
    value = _normalized(uri)
    if value.startswith("steam:"):
        surface = ""
        if "open/games" in value:
            surface = "library"
        elif "store" in value:
            surface = "store"
        elif "settings" in value:
            surface = "settings"
        elif "downloads" in value:
            surface = "downloads"
        elif "friends" in value:
            surface = "friends"
        return "steam", surface, "user_account"
    if value.startswith("spotify:"):
        return "spotify", "search" if "search:" in value else "", "user_account"
    if value.startswith("vscode:"):
        return "vscode", "", "user_account"
    return "", "", ""


def _url_identity(url: str) -> tuple[str, str, str]:
    try:
        parsed = urlparse(str(url or ""))
        host = _normalized(parsed.hostname or "")
        path = _normalized(parsed.path or "")
    except Exception:
        return "", "", ""
    if "steampowered.com" in host:
        surface = "store" if host.startswith("store.") else ""
        if "/library" in path:
            surface = "library"
        return "steam", surface, "public" if surface == "store" else ""
    if "spotify.com" in host:
        return "spotify", "", ""
    return "", "", ""


def _recommended_deep_link(goal: SemanticGoal) -> StrategyMechanism | None:
    if goal.required_application == "steam" and goal.required_surface:
        routes = {
            "library": "steam://open/games",
            "friends": "steam://open/friends",
            "downloads": "steam://open/downloads",
            "settings": "steam://settings",
        }
        uri = routes.get(goal.required_surface)
        if uri:
            covers = goal.requested_action in {"open", "navigate"}
            return StrategyMechanism(
                "deep_link:steam:" + goal.required_surface,
                StrategyLayer.DEEP_LINK,
                f"known deterministic route {uri}",
                application="steam",
                surface=goal.required_surface,
                account=goal.required_account,
                covers_goal=covers,
                mutation=True,
            )
    if goal.required_application == "spotify" and goal.requested_action == "search":
        return StrategyMechanism(
            "deep_link:spotify:search", StrategyLayer.DEEP_LINK,
            "known deterministic Spotify search route",
            application="spotify", surface=goal.required_surface,
            account=goal.required_account, covers_goal=True, mutation=True,
        )
    return None


def build_execution_strategy(
    request: str,
    available_tools: Iterable[str] = (),
    *,
    postcondition: str = "",
    postconditions: Iterable[str] = (),
) -> ExecutionStrategy:
    goal = derive_semantic_goal(
        request, postcondition=postcondition, postconditions=postconditions,
    )
    tools = frozenset(str(item) for item in available_tools)
    mechanisms: list[StrategyMechanism] = []
    deep_link = _recommended_deep_link(goal)
    if deep_link is not None and "open_uri" in tools:
        mechanisms.append(deep_link)
    if "desktop_click" in tools:
        common = {
            "application": goal.required_application,
            "surface": goal.required_surface,
            "account": goal.required_account,
            "covers_goal": False,
            "mutation": True,
        }
        mechanisms.extend((
            StrategyMechanism(
                "native_accessibility", StrategyLayer.NATIVE_ACCESSIBILITY,
                "fresh native accessibility target", **common,
            ),
            StrategyMechanism(
                "keyboard_focus", StrategyLayer.KEYBOARD,
                "observable focus after accessibility direct action stagnates", **common,
            ),
            StrategyMechanism(
                "local_vision", StrategyLayer.LOCAL_VISION,
                "zoomed local target after deterministic native paths stagnate", **common,
            ),
            StrategyMechanism(
                "global_vision", StrategyLayer.GLOBAL_VISION,
                "global coordinates only after every higher applicable layer", **common,
            ),
        ))
    return ExecutionStrategy(goal, mechanisms)


def mechanism_for_tool(
    strategy: ExecutionStrategy,
    tool_name: str,
    arguments: dict,
) -> StrategyMechanism | None:
    goal = strategy.goal
    if tool_name in _OBSERVATION_TOOLS:
        return None
    if tool_name == "open_application":
        app = _normalized(arguments.get("name")) or goal.required_application
        covers = goal.requested_action == "open" and not goal.required_surface
        return StrategyMechanism(
            f"direct:open_application:{app}", StrategyLayer.DIRECT_CAPABILITY,
            "exact native application launch capability", application=app,
            surface="", account=goal.required_account, covers_goal=covers,
            mutation=True,
        )
    if tool_name == "open_uri":
        app, surface, account = _uri_identity(arguments.get("uri", ""))
        return StrategyMechanism(
            f"deep_link:{app or 'unknown'}:{surface or 'route'}",
            StrategyLayer.DEEP_LINK,
            "allowlisted deterministic URI route",
            application=app, surface=surface, account=account,
            covers_goal=(
                goal.requested_action in {"open", "navigate"}
                or (goal.requested_action == "search" and "search" in _normalized(arguments.get("uri")))
            ),
            mutation=True,
        )
    if tool_name in _DOM_ACTION_TOOLS:
        url = arguments.get("url") or arguments.get("target_url") or ""
        app, surface, account = _url_identity(url)
        if url:
            if not app and goal.required_application:
                app = "unresolved_web_application"
            if not surface and goal.required_surface:
                surface = "unresolved_web_surface"
            if not account and goal.required_account:
                account = "unresolved_web_account"
        elif not app and goal.required_application:
            app = goal.required_application
        if not surface and not url:
            # A pre-existing browser page is not evidence that a required native
            # surface survived the fallback.  Keep it explicit and fail closed.
            surface = "current_web" if goal.required_surface else ""
        return StrategyMechanism(
            f"dom:{app or 'web'}:{surface or 'current'}",
            StrategyLayer.DOM, "structured DOM/Playwright action",
            application=app, surface=surface, account=account,
            covers_goal=True, mutation=True,
        )
    if tool_name == "desktop_click":
        # The composite owns DOM/UIA/keyboard/local/global selection internally.
        return None
    if tool_name in {"desktop_press", "desktop_type"}:
        return StrategyMechanism(
            "keyboard_focus", StrategyLayer.KEYBOARD,
            "keyboard action requiring observable focus",
            application=goal.required_application, surface=goal.required_surface,
            account=goal.required_account, covers_goal=False, mutation=True,
        )
    return StrategyMechanism(
        f"direct:{_bounded(tool_name, 80)}", StrategyLayer.DIRECT_CAPABILITY,
        "provider/API/MCP/OSIA structured direct capability",
        application=goal.required_application, surface=goal.required_surface,
        account=goal.required_account, covers_goal=True, mutation=True,
    )


def tool_preflight(tool_name: str, arguments: dict) -> StrategyDecision | None:
    strategy = current_execution_strategy()
    if strategy is None:
        return None
    if tool_name == "desktop_click":
        # Internal selection begins with DOM when applicable, then layer 3.
        preview = strategy.preview(StrategyMechanism(
            "native_accessibility", StrategyLayer.NATIVE_ACCESSIBILITY,
            "desktop composite starts at the earliest applicable internal layer",
            application=strategy.goal.required_application,
            surface=strategy.goal.required_surface,
            account=strategy.goal.required_account,
            covers_goal=False,
            mutation=True,
        ))
        return preview
    mechanism = mechanism_for_tool(strategy, tool_name, arguments)
    if mechanism is None:
        return None
    return strategy.select(mechanism, {"tool": tool_name, "arguments": arguments})


def record_tool_observation(
    tool_name: str,
    result: object,
    *,
    is_error: bool,
    strategy: ExecutionStrategy | None = None,
) -> bool:
    strategy = strategy or current_execution_strategy()
    if strategy is None or tool_name not in _OBSERVATION_TOOLS or is_error:
        return False
    digest = hashlib.sha256(_bounded(result, 2000).encode("utf-8", errors="replace")).hexdigest()[:16]
    strategy.observe(f"read-only observation from {tool_name}", evidence_token=f"{tool_name}:{digest}")
    text = _normalized(result)
    negative = re.search(
        r"\b(no|false|unknown|unavailable|not found|not installed|cannot verify)\b",
        text,
    )
    target_tokens = [
        token for token in _normalized(strategy.goal.target_object).split()
        if len(token) >= 3
    ]
    target_visible = bool(target_tokens) and all(token in text for token in target_tokens[:6])
    affirmative = bool(re.search(r"\b(yes|true|verified|visible|running)\b", text))
    goal = strategy.goal
    page_proves_goal = (
        tool_name in {"read_page", "get_page_text", "inspect_browser_page"}
        and target_visible
    )
    file_search_proves_goal = (
        tool_name in {"search_files", "list_files"}
        and goal.requested_action == "search"
        and target_visible
    )
    app_state_proves_goal = (
        tool_name in {"is_running", "has_window"}
        and affirmative
        and goal.requested_action == "open"
        and not goal.required_surface
    )
    steam_state_proves_goal = (
        tool_name == "steam_game_state"
        and affirmative
        and goal.requested_action == "open"
        and target_visible
        and not goal.required_surface
    )
    web_state_proves_goal = (
        tool_name == "web_state"
        and affirmative
        and target_visible
    )
    proves_goal = not negative and (
        page_proves_goal
        or file_search_proves_goal
        or app_state_proves_goal
        or steam_state_proves_goal
        or web_state_proves_goal
    )
    if strategy.frozen_reason:
        if proves_goal:
            strategy.reconcile(
                succeeded=True, evidence=f"{tool_name}:{digest}",
            )
            return True
        elif negative and tool_name in {
            "search_files", "list_files", "is_running", "has_window",
            "steam_game_state", "web_state",
        }:
            strategy.reconcile(succeeded=False, evidence=f"{tool_name}:{digest}")
        return False
    if proves_goal:
        return strategy.verify_postcondition(f"{tool_name}:{digest}")
    return False


def record_resumed_tool_outcome(
    strategy: ExecutionStrategy,
    tool_name: str,
    execution_record,
) -> None:
    """Reconcile a durable pending receipt against the preserved strategy."""

    outcome_value = getattr(getattr(execution_record, "outcome", None), "value", None)
    outcome = _normalized(
        outcome_value or getattr(execution_record, "outcome", "")
    ).replace(" ", "_")
    is_error = bool(getattr(execution_record, "is_error", False))
    result = getattr(execution_record, "result", "")
    observation_proven = record_tool_observation(
        tool_name,
        result,
        is_error=is_error,
        strategy=strategy,
    )
    if not strategy.frozen_reason:
        return
    terminal_receipt = outcome in {
        "success_verified",
        "success_unverified",
        "confirmed_failure_before_effect",
        "failed_before_effect",
        "cancelled_proven",
        "rejected_before_execution",
    }
    if not terminal_receipt:
        return
    evidence = hashlib.sha256(
        _bounded(result, 4000).encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    current = strategy._mechanisms.get(strategy.current_mechanism)
    covers_goal = bool(
        outcome == "success_verified"
        and not is_error
        and current is not None
        and current.covers_goal
        and (
            tool_name not in _OBSERVATION_TOOLS
            or observation_proven
        )
    )
    strategy.reconcile(
        succeeded=covers_goal,
        evidence=f"{tool_name}:{evidence}",
    )


def record_tool_outcome(
    tool_name: str,
    decision: StrategyDecision | None,
    *,
    outcome: str,
    is_error: bool,
    reason_code: str,
    result: object,
) -> None:
    strategy = current_execution_strategy()
    if strategy is None:
        return
    record_tool_observation(tool_name, result, is_error=is_error)
    if decision is None or not decision.allowed or not decision.attempt_id:
        if _normalized(outcome) in {"pending", "outcome_unknown", "unknown_after_start"}:
            synthetic = StrategyMechanism(
                f"tool:{_bounded(tool_name, 80)}", StrategyLayer.DIRECT_CAPABILITY,
                "dispatched action has a possible effect", mutation=True,
            )
            pending = strategy.select(synthetic, {"tool": tool_name})
            if pending.allowed:
                strategy.record(
                    pending, "ambiguous", failure_reason=reason_code or "effect_possible",
                )
        return
    normalized_outcome = _normalized(outcome).replace(" ", "_")
    if normalized_outcome in {"pending", "outcome_unknown", "unknown_after_start"}:
        strategy.record(decision, "ambiguous", failure_reason=reason_code)
        return
    if reason_code in {"stale_semantic_reference", "stale_reference"}:
        strategy.record(decision, "stale", failure_reason=reason_code)
        return
    if is_error:
        strategy.record(decision, "failed", failure_reason=reason_code or "tool_failed")
        return
    evidence = hashlib.sha256(
        _bounded(result, 4000).encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    if normalized_outcome in {"success_verified", "succeeded", "success"}:
        strategy.record(
            decision, "succeeded", observation=f"verified result from {tool_name}",
            postcondition_evidence=f"{tool_name}:{evidence}",
        )
    else:
        strategy.record(
            decision, "progress", observation=f"unverified progress from {tool_name}",
            postcondition_evidence=f"{tool_name}:{evidence}",
        )


_ACTIVE_STRATEGY: ContextVar[ExecutionStrategy | None] = ContextVar(
    "osia_execution_strategy", default=None,
)


def current_execution_strategy() -> ExecutionStrategy | None:
    return _ACTIVE_STRATEGY.get()


@contextmanager
def execution_strategy_scope(
    request: str,
    available_tools: Iterable[str] = (),
    *,
    postcondition: str = "",
    postconditions: Iterable[str] = (),
    strategy: ExecutionStrategy | None = None,
):
    active = strategy or build_execution_strategy(
        request,
        available_tools,
        postcondition=postcondition,
        postconditions=postconditions,
    )
    token = _ACTIVE_STRATEGY.set(active)
    try:
        yield active
    finally:
        _ACTIVE_STRATEGY.reset(token)
