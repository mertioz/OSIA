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
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
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
    ("calculate", r"\b(calculate|compute|calculer|calcule)\b"),
    ("send", r"\b(send|submit|envoie|envoyer|soumet|soumettre)\b"),
    ("create", r"\b(create|add|cree|creer|ajoute|ajouter)\b"),
    ("delete", r"\b(delete|remove|supprime|supprimer|efface|effacer)\b"),
)

_NEGATIVE_PREFIX_RE = re.compile(
    r"\b(?:without|sans|do\s+not|don['’]?t|dont|never)\b"
    r"(?:[^\r\n.;!?]|\.(?=[A-Za-z0-9_-])){0,240}",
    re.IGNORECASE,
)
_FRENCH_NEGATIVE_RE = re.compile(
    r"\bne\b(?:[^\r\n.;!?]|\.(?=[A-Za-z0-9_-])){0,120}"
    r"\b(?:pas|jamais)\b"
    r"(?:[^\r\n.;!?]|\.(?=[A-Za-z0-9_-])){0,120}",
    re.IGNORECASE,
)
_NEGATED_ACTION_ALIASES = (
    (
        "activate",
        r"\b(?:click(?:s|ed|ing)?|select(?:s|ed|ing)?|"
        r"press(?:es|ed|ing)?|"
        r"activat(?:e|es|ed|ing)|cliqu(?:e|es|er|ez|ant)|"
        r"appui(?:e|es|er|ez|ant)|selectionn(?:e|es|er|ez|ant)|"
        r"activ(?:e|es|er|ez|ant))\b",
    ),
    (
        "type",
        r"\b(?:typ(?:e|es|ed|ing)|enter(?:s|ed|ing)?|"
        r"fill(?:s|ed|ing)?|saisi(?:s|r|ssant)?|"
        r"tap(?:e|es|er|ez|ant))\b",
    ),
    (
        "write",
        r"\b(?:writ(?:e|es|ing|ten)|overwrit(?:e|es|ing|ten)|"
        r"edit(?:s|ed|ing)?|updat(?:e|es|ed|ing)|sav(?:e|es|ed|ing)|"
        r"modif(?:y|ies|ied|ying)|chang(?:e|es|ed|ing)|"
        r"replac(?:e|es|ed|ing)|ecri(?:s|re|vant)|"
        r"modifi(?:e|es|er|ez|ant)|remplac(?:e|es|er|ez|ant)|"
        r"enregistr(?:e|es|er|ez|ant)|mets?|mettre\s+a\s+jour)\b",
    ),
    (
        "move",
        r"\b(?:mov(?:e|es|ed|ing)|renam(?:e|es|ed|ing)|"
        r"deplac(?:e|es|er|ez|ant)|renomm(?:e|es|er|ez|ant))\b",
    ),
    (
        "send",
        r"\b(?:send(?:s|ing)?|sent|submit(?:s|ted|ting)?|"
        r"publish(?:es|ed|ing)?|shar(?:e|es|ed|ing)|"
        r"envoi(?:e|es|er|ez|ant)|soumet(?:s|tre|tez|tant)|"
        r"publi(?:e|es|er|ez|ant)|partag(?:e|es|er|ez|ant))\b",
    ),
    (
        "create",
        r"\b(?:creat(?:e|es|ed|ing)|add(?:s|ed|ing)?|"
        r"cre(?:e|es|er|ez|ant)|ajout(?:e|es|er|ez|ant))\b",
    ),
    (
        "delete",
        r"\b(?:delet(?:e|es|ed|ing)|remov(?:e|es|ed|ing)|"
        r"clos(?:e|es|ed|ing)|kill(?:s|ed|ing)?|"
        r"terminat(?:e|es|ed|ing)|supprim(?:e|es|er|ez|ant)|"
        r"effac(?:e|es|er|ez|ant)|ferm(?:e|es|er|ez|ant))\b",
    ),
    (
        "open",
        r"\b(?:open(?:s|ed|ing)?|launch(?:es|ed|ing)?|"
        r"ouvr(?:e|es|ir|ez|ant)|lanc(?:e|es|er|ez|ant))\b",
    ),
    (
        "navigate",
        r"\b(?:navigat(?:e|es|ed|ing)|visit(?:s|ed|ing)?|"
        r"aller|navigu(?:e|es|er|ez|ant)|visit(?:e|es|er|ez|ant))\b",
    ),
    (
        "search",
        r"\b(?:search(?:es|ed|ing)?|find(?:s|ing)?|found|"
        r"cherch(?:e|es|er|ez|ant)|recherch(?:e|es|er|ez|ant))\b",
    ),
)

_OPAQUE_NEGATIVE_TARGET_KEYS = frozenset({
    "it", "this", "that", "these", "those", "them", "itself",
    "ca", "cela", "ceci", "celui", "celle", "ceux", "celles",
    "lui", "eux", "elles",
    "any", "all", "every", "each", "anyfile", "anyfiles",
    "allfiles", "everyfile", "everyfiles", "aucun", "aucune",
    "aucunfichier", "aucunefichier", "aucunsfichiers",
    "aucunesfichiers", "tout", "tous", "toute", "toutes",
    "toutfichier", "touslesfichiers", "nimportequel", "nimportequoi",
})


def _explicit_negative_clauses(value: object) -> tuple[tuple[int, int, str], ...]:
    """Return bounded explicit user-negation spans without inventing intent."""

    normalized = _normalized(_bounded(value, 32_000))
    found: list[tuple[int, int, str]] = []
    for pattern in (_NEGATIVE_PREFIX_RE, _FRENCH_NEGATIVE_RE):
        for match in pattern.finditer(normalized):
            clause = _bounded(match.group(0), 240)
            if clause:
                found.append((match.start(), match.end(), clause))
    found.sort(key=lambda item: (item[0], item[1]))
    deduped: list[tuple[int, int, str]] = []
    for item in found:
        if any(
            item[0] >= existing[0]
            and item[1] <= existing[1]
            for existing in deduped
        ):
            continue
        deduped.append(item)
    return tuple(deduped)


def explicit_negative_constraints(value: object) -> tuple[dict[str, str], ...]:
    """Project literal negative clauses and only unambiguous action bans.

    A target-specific clause remains preserved as authoritative model context,
    but is not widened into a global capability ban.  Clauses such as
    ``without clicking or sending anything`` have no target residue and can be
    enforced structurally before dispatch.
    """

    constraints: list[dict[str, str]] = []
    for _start, _end, clause in _explicit_negative_clauses(value):
        constraints.append({
            "name": "explicit_negative_constraint",
            "value": clause,
            "source": "user_objective",
        })
        content = re.sub(
            r"^\s*(?:without|sans|do\s+not|don['’]?t|dont|never)\b",
            "",
            clause,
            flags=re.IGNORECASE,
        ).strip()
        mentioned: list[str] = []
        residual = content
        for action, pattern in _NEGATED_ACTION_ALIASES:
            if re.search(pattern, content, flags=re.IGNORECASE):
                mentioned.append(action)
                residual = re.sub(
                    pattern, " ", residual, flags=re.IGNORECASE,
                )
        if not mentioned:
            constraints.append({
                "name": "unresolved_negative_constraint",
                "value": clause,
                "source": "user_objective",
            })
        residual = re.sub(
            r"\b(?:and|or|nor|any|all|every|each|anything\s+else|"
            r"anything|nothing|"
            r"et|ou|ni|rien|quoi\s+que\s+ce\s+soit|pas|jamais|"
            r"aucun(?:e|s|es)?|tout(?:e|s|es)?|n['’]?importe\s+quel(?:le|s|les)?|"
            r"ne|n|de|du|des|la|le|les|un|une)\b",
            " ",
            residual,
            flags=re.IGNORECASE,
        )
        target = _bounded(residual.strip(" \t\r\n,.:;!?"), 240)
        residual_key = re.sub(r"[^a-z0-9]+", "", target)
        if residual_key in _OPAQUE_NEGATIVE_TARGET_KEYS:
            # A pronoun/deictic target cannot be proven different from the
            # concrete object later supplied to a tool. Preserve the explicit
            # user ban as global for that action class instead of letting the
            # unresolved reference weaken it.
            target = ""
            residual_key = ""
        if residual_key:
            for action in dict.fromkeys(mentioned):
                constraints.append({
                    "name": "forbidden_action_target",
                    "value": json.dumps(
                        {"action": action, "target": target},
                        sort_keys=True,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                    "source": "user_objective",
                })
            continue
        for action in dict.fromkeys(mentioned):
            constraints.append({
                "name": "forbidden_action",
                "value": action,
                "source": "user_objective",
            })
    return tuple(constraints)

_KNOWN_APPLICATIONS = (
    "steam", "spotify", "vscode", "visual studio code", "discord",
    "brave", "chrome", "firefox", "edge", "outlook", "gmail",
    "notepad", "bloc-notes", "calculator", "calculatrice", "paint",
    "character map", "xbox",
)

_APPLICATION_ALIASES = {
    "visual studio code": "vscode",
    "calculatrice": "calculator",
}


def _canonical_application(value: object) -> str:
    normalized = _normalized(value)
    return _APPLICATION_ALIASES.get(normalized, normalized)


def _simple_calculation_result(value: object) -> str:
    """Return the exact result of one bounded decimal arithmetic expression."""

    match = re.fullmatch(
        r"\s*([+-]?\d+(?:[.,]\d+)?)\s*([+\-*x\u00d7])\s*"
        r"([+-]?\d+(?:[.,]\d+)?)\s*=?\s*",
        str(value or ""),
        re.IGNORECASE,
    )
    if match is None:
        return ""
    try:
        left = Decimal(match.group(1).replace(",", "."))
        right = Decimal(match.group(3).replace(",", "."))
        operator = match.group(2).lower()
        result = (
            left + right
            if operator == "+" else
            left - right
            if operator == "-" else
            left * right
        )
    except (InvalidOperation, ValueError):
        return ""
    rendered = format(result, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _primitive_proves_calculation_result(
    node: SemanticActionNode | None,
    attempt: StrategyAttempt | None,
) -> bool:
    if (
        node is None
        or attempt is None
        or node.requested_action != "calculate"
    ):
        return False
    target = (
        (attempt.primitive_receipt.get("before") or {}).get("target") or {}
    )
    target_label = _normalized(target.get("label"))
    if target_label != "=" and not any(
        token in target_label for token in ("equal", "egal")
    ):
        return False
    expected = _simple_calculation_result(node.requested_content)
    effect = normalize_desktop_observable_effect(attempt.observable_effect)
    if not expected or not effect:
        return False
    observed = re.sub(
        r"(?<=\d)[\s\u00a0\u202f'](?=\d)",
        "",
        _normalized(effect.get("expected_text")).replace(",", "."),
    )
    return bool(re.search(
        rf"(?<![\d.]){re.escape(expected)}(?![\d.])",
        observed,
    ))


def _semantic_batch_proves_calculation_result(
    node: SemanticActionNode | None,
    batch_plan: object,
) -> bool:
    """Return whether a typed batch ends in the node's exact calculation result."""

    if node is None or node.requested_action != "calculate":
        return True
    plan = batch_plan if isinstance(batch_plan, dict) else {}
    items = plan.get("items") if isinstance(plan.get("items"), list) else []
    if not items:
        return False
    final_item = items[-1] if isinstance(items[-1], dict) else {}
    target_label = _normalized(final_item.get("objective"))
    if target_label != "=" and not any(
        token in target_label for token in ("equal", "egal")
    ):
        return False
    expected = _simple_calculation_result(node.requested_content)
    effect = normalize_desktop_observable_effect(
        final_item.get("observable_effect")
    )
    if not expected or not effect:
        return False
    observed = re.sub(
        r"(?<=\d)[\s\u00a0\u202f'](?=\d)",
        "",
        _normalized(effect.get("expected_text")).replace(",", "."),
    )
    return bool(re.search(
        rf"(?<![\d.]){re.escape(expected)}(?![\d.])",
        observed,
    ))


@dataclass
class SemanticActionNode:
    target_object: str
    required_application: str
    required_surface: str
    required_account: str
    requested_action: str
    observable_success_postcondition: str
    fingerprint: str
    requires_execution: bool = False
    required_postconditions: tuple[str, ...] = ()
    node_id: str = ""
    order: int = 0
    source_clause: str = ""
    requested_content: str = ""
    status: str = "pending"
    evidence_links: tuple[str, ...] = ()

    def public_dict(self) -> dict:
        return asdict(self)


@dataclass
class SemanticMission:
    objective: str
    action_nodes: tuple[SemanticActionNode, ...]
    fingerprint: str
    execution_required_override: bool = False

    @property
    def requires_execution(self) -> bool:
        return self.execution_required_override or any(
            node.requires_execution for node in self.action_nodes
        )

    @property
    def required_postconditions(self) -> tuple[str, ...]:
        return tuple(
            postcondition
            for node in self.action_nodes
            for postcondition in node.required_postconditions
        )

    def public_dict(self) -> dict:
        return {
            "objective": self.objective,
            "action_nodes": [node.public_dict() for node in self.action_nodes],
            "fingerprint": self.fingerprint,
            "execution_required_override": self.execution_required_override,
        }


# Compatibility name for focused WOR-248 callers which construct one node.
SemanticGoal = SemanticActionNode


def _derive_semantic_node(
    request: str,
    *,
    postcondition: str = "",
    postconditions: Iterable[str] = (),
) -> SemanticActionNode:
    """Derive bounded goal invariants without an extra model call.

    The parser intentionally extracts only explicit constraints.  Missing
    surface/account values stay empty, which permits broader equivalents.  It
    never invents a native-vs-web requirement merely from an application name.
    """

    source = _bounded(request, 32_000)
    norm = _normalized(source)
    negative_spans = tuple(
        (start, end)
        for start, end, _clause in _explicit_negative_clauses(norm)
    )

    application = ""
    for candidate in _KNOWN_APPLICATIONS:
        if re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", norm):
            application = _canonical_application(candidate)
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
                or any(
                    start <= match.start() < end
                    for start, end in negative_spans
                )
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
        candidate = re.split(
            r"(?:[;!?]|\.(?:\s+|$))", candidate, maxsplit=1,
        )[0].strip()
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
            r"\b(click|type|calculate|compute|calculer|calcule|send|submit|"
            r"create|delete|remove|open|launch|"
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
    return SemanticActionNode(
        **payload,
        fingerprint=fingerprint,
        requires_execution=requires_execution,
        required_postconditions=required,
        source_clause=source,
    )


def derive_semantic_goal(
    request: str,
    *,
    postcondition: str = "",
    postconditions: Iterable[str] = (),
) -> SemanticActionNode:
    """Compatibility parser for one semantic action chain.

    Production turn orchestration uses :func:`derive_semantic_mission`.  This
    helper intentionally preserves the WOR-248 single-goal contract for tests
    and callers which explicitly ask for one compound action chain.
    """

    return _derive_semantic_node(
        request,
        postcondition=postcondition,
        postconditions=postconditions,
    )


_ACTION_WORDS = (
    r"search|find|look\s+for|cherche|chercher|recherche|rechercher|"
    r"open|launch|start|ouvre|ouvrir|lance|lancer|demarre|demarrer|"
    r"go\s+to|navigate|visit|va\s+a|aller\s+a|rends\s+toi|accede|"
    r"click|select|activate|clique|selectionne|active|"
    r"type|enter|write|saisis|ecris|tape|calculate|compute|calculer|calcule|"
    r"send|submit|envoie|envoyer|"
    r"create|add|cree|creer|ajoute|ajouter|delete|remove|supprime|efface|"
    r"do|perform|execute|run|check|verify|confirm|report|"
    r"faire|effectuer|executer|verifier|confirmer|rapporter"
)
_ACTION_SPLIT_PATTERN = re.compile(
    rf"(?:\s*;\s*|\s*\.(?:\s+|$)\s*|"
    rf"\s*,?\s*\b(?:and\s+then|then|puis|ensuite)\b\s*|"
    rf"\s*,\s*(?=(?:{_ACTION_WORDS})\b)|"
    rf"\s+\b(?:and|et)\b\s+(?=(?:{_ACTION_WORDS})\b))",
    re.IGNORECASE,
)


def _explicit_application(clause: str, node: SemanticActionNode) -> str:
    if node.required_application:
        return node.required_application
    norm = _normalized(clause)
    if node.requested_action == "open":
        target = _normalized(node.target_object)
        target = re.sub(r"^(?:the|a|an|le|la|les|un|une)\s+", "", target)
        return _bounded(_canonical_application(target), 80)
    if node.requested_action in {"type", "send", "create"}:
        match = re.search(
            r"\b(?:in|into|inside|dans|sur)\s+([^,.;]{1,80})$", norm,
        )
        if match:
            return _bounded(match.group(1).strip(), 80)
    return ""


def _open_list_targets(clause: str) -> tuple[str, ...]:
    norm = _normalized(clause)
    match = re.search(
        r"\b(?:open|launch|start|ouvre|ouvrir|lance|lancer|demarre|demarrer)\b\s+(.+)$",
        norm,
    )
    if not match:
        return ()
    tail = match.group(1).strip(" .?!,;:")
    if re.search(r"\b(?:at|in|on|to|dans|sur|vers)\b", tail):
        return ()
    parts = [
        re.sub(r"^(?:the|a|an|le|la|les|un|une)\s+", "", item).strip()
        for item in re.split(r"\s*(?:,|\band\b|\bet\b)\s*", tail)
        if item.strip()
    ]
    return tuple(_bounded(item, 80) for item in parts) if len(parts) > 1 else ()


def _looks_like_bare_application(clause: str) -> bool:
    norm = _normalized(clause).strip(" .?!,;:")
    if not norm or len(norm.split()) > 5:
        return False
    return not re.search(
        r"\b(?:do|perform|execute|run|verify|check|confirm|report|use|with|it|this|that|"
        r"verifie|verifier|confirme|confirmer|rapporte|utilise|utiliser|cela|ceci)\b",
        norm,
    )


def _node_with_identity(
    node: SemanticActionNode,
    *,
    order: int,
    clause: str,
    application: str = "",
    postcondition: str = "",
) -> SemanticActionNode:
    if application:
        node.required_application = _canonical_application(application)
        if node.requested_action == "open":
            node.target_object = _bounded(application, 160)
    if postcondition:
        node.observable_success_postcondition = _bounded(postcondition, 320)
        node.required_postconditions = (node.observable_success_postcondition,)
    node.order = order
    node.source_clause = _bounded(clause, 500)
    node.node_id = "node_" + hashlib.sha256(
        json.dumps(
            {
                "order": order,
                "clause": node.source_clause,
                "application": node.required_application,
                "action": node.requested_action,
                "target": node.target_object,
            },
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    if node.requested_action in {"type", "send", "create"}:
        quoted = re.findall(
            r"[\"'\u201c\u201d\u00ab\u00bb]([^\"'\u201c\u201d\u00ab\u00bb]{1,320})[\"'\u201c\u201d\u00ab\u00bb]",
            clause,
        )
        if quoted:
            node.requested_content = _bounded(quoted[-1], 320)
    elif node.requested_action == "calculate":
        node.requested_content = _bounded(node.target_object, 320)
        exact_result = _simple_calculation_result(node.requested_content)
        if exact_result and not postcondition:
            node.observable_success_postcondition = _bounded(
                f"{node.required_application or 'calculator'} visibly displays "
                f"the exact result {exact_result}",
                320,
            )
            node.required_postconditions = (
                node.observable_success_postcondition,
            )
    return node


def _verification_postcondition(
    clause: str,
    previous: SemanticActionNode | None,
) -> str:
    """Bind an explicit verification clause to the effect it verifies."""

    if previous is None or not previous.requires_execution:
        return ""
    normalized = _normalized(clause)
    match = re.match(
        r"^(?:verify|check|confirm|verifie|verifier|confirme|confirmer)\b\s*(.*)$",
        normalized,
    )
    if match is None:
        return ""
    detail = _bounded(match.group(1).strip(" .?!,;:"), 240)
    if not detail:
        return ""
    application = previous.required_application or previous.target_object
    return _bounded(
        f"{application} visibly shows {detail}",
        320,
    )


def derive_semantic_mission(
    request: str,
    *,
    postcondition: str = "",
    postconditions: Iterable[str] = (),
) -> SemanticMission:
    """Build an ordered mission without merging independent requested work."""

    source = _bounded(request, 32_000)
    explicit_postconditions = tuple(
        _bounded(item, 320) for item in postconditions if _bounded(item, 320)
    )
    clauses = [
        item.strip(" ,") for item in _ACTION_SPLIT_PATTERN.split(source)
        if item.strip(" ,")
    ] or [source]
    nodes: list[SemanticActionNode] = []
    current_application = ""
    current_action = ""
    for clause in clauses:
        verification = _verification_postcondition(
            clause,
            nodes[-1] if nodes else None,
        )
        if verification:
            previous = nodes[-1]
            previous.source_clause = _bounded(
                previous.source_clause + ". " + clause,
                500,
            )
            nodes[-1] = _node_with_identity(
                previous,
                order=previous.order,
                clause=previous.source_clause,
                application=previous.required_application,
                postcondition=verification,
            )
            continue
        open_targets = _open_list_targets(clause)
        if open_targets:
            for target in open_targets:
                node = _derive_semantic_node("Open " + target)
                node = _node_with_identity(
                    node,
                    order=len(nodes),
                    clause=target,
                    application=target,
                )
                nodes.append(node)
                current_application = node.required_application
                current_action = "open"
            continue

        node = _derive_semantic_node(clause)
        if (
            not node.requested_action
            and current_action == "open"
            and _looks_like_bare_application(clause)
        ):
            node = _derive_semantic_node("Open " + clause)
        explicit_application = _explicit_application(clause, node)
        if explicit_application:
            node.required_application = explicit_application
        elif current_application and node.requested_action:
            node.required_application = current_application

        # Navigation/search/activation/typing with no new application is a
        # dependent refinement of the preceding node, not a second effect.
        if (
            nodes
            and node.requested_action in {"navigate", "search", "activate", "type"}
            and not explicit_application
            and nodes[-1].required_application == node.required_application
        ):
            combined = _derive_semantic_node(
                nodes[-1].source_clause + " and " + clause,
            )
            nodes[-1] = _node_with_identity(
                combined,
                order=nodes[-1].order,
                clause=nodes[-1].source_clause + " and " + clause,
                application=current_application,
            )
            continue

        node = _node_with_identity(
            node,
            order=len(nodes),
            clause=clause,
            application=explicit_application,
        )
        nodes.append(node)
        if node.required_application:
            current_application = node.required_application
        if node.requested_action:
            current_action = node.requested_action

    if not nodes:
        nodes.append(_node_with_identity(
            _derive_semantic_node(source), order=0, clause=source,
        ))

    if postcondition and len(nodes) == 1:
        nodes[0] = _node_with_identity(
            nodes[0], order=0, clause=nodes[0].source_clause,
            postcondition=postcondition,
        )
    elif explicit_postconditions:
        for index, value in enumerate(explicit_postconditions):
            if index >= len(nodes):
                break
            nodes[index] = _node_with_identity(
                nodes[index], order=index, clause=nodes[index].source_clause,
                postcondition=value,
            )

    payload = [node.public_dict() for node in nodes]
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:24]
    return SemanticMission(
        objective=source,
        action_nodes=tuple(nodes),
        fingerprint=fingerprint,
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
    node_id: str = ""
    attempted_effect: str = ""

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
    INAPPLICABLE = "inapplicable"


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
    node_id: str = ""
    attempted_effect: str = ""
    target_application: str = ""
    target_surface: str = ""
    target_surface_identity: dict = field(default_factory=dict)
    dispatched_at: float = 0.0
    dispatch_evidence_revision: int = 0
    composite_id: str = ""
    substep_id: str = ""
    substep_index: int = 0
    primitive_postcondition: str = ""
    observable_effect: dict = field(default_factory=dict)
    primitive_receipt: dict = field(default_factory=dict)
    action_pattern: str = ""
    actionability_evidence: dict = field(default_factory=dict)
    semantic_batch_id: str = ""
    semantic_batch_effects: list[dict] = field(default_factory=list)
    semantic_checkpoint_count: int = 0
    semantic_batch_resume_from_index: int | None = None

    def public_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StrategyDecision:
    allowed: bool
    reason_code: str
    attempt_id: str = ""
    mechanism_id: str = ""
    node_id: str = ""
    attempted_effect: str = ""


_TERMINAL_FOR_ESCALATION = frozenset({
    AttemptStatus.PROGRESSED.value,
    AttemptStatus.SUCCEEDED.value,
    AttemptStatus.FAILED.value,
    AttemptStatus.STAGNATED.value,
    AttemptStatus.STALE.value,
    AttemptStatus.AMBIGUOUS.value,
    AttemptStatus.INAPPLICABLE.value,
})


class ExecutionStrategy:
    """Bounded strategy state for one ordered semantic mission."""

    _MAX_IDENTICAL_ACTION_ATTEMPTS = 2
    _MAX_PROGRESS_ATTEMPTS_PER_DESKTOP_MECHANISM = 16

    def __init__(
        self,
        mission: SemanticMission | SemanticActionNode,
        mechanisms: Iterable[StrategyMechanism] = (),
        *,
        available_tools: Iterable[str] = (),
    ):
        if isinstance(mission, SemanticActionNode):
            node = mission
            if not node.node_id:
                node = _node_with_identity(
                    node, order=0, clause=node.source_clause or node.target_object,
                )
            mission = SemanticMission(
                objective=node.source_clause or node.target_object,
                action_nodes=(node,),
                fingerprint=node.fingerprint,
            )
        self.mission = mission
        self.available_tools = frozenset(
            str(item) for item in available_tools if str(item)
        )
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
        self._retired_effect_mechanisms: dict[str, str] = {}
        self._completed_effects: list[dict[str, object]] = []
        self._reconciliation_events: list[dict[str, str]] = []
        self._terminal_prompt_tokens: set[str] = set()
        self._substep_counters: dict[str, int] = {}
        self._completed_substeps: list[dict] = []
        self._completed_primitive_signatures: set[str] = set()
        self._retry_substeps: dict[str, dict] = {}
        self._latest_desktop_observation: dict[str, object] = {}
        self._goal_contract: dict = {}
        self._frozen_node_id = ""
        self._frozen_mechanism_id = ""
        self._frozen_attempt_id = ""
        self.terminal_blocker = ""
        self._lock = threading.RLock()
        for mechanism in mechanisms:
            self.register(mechanism)

    @property
    def goal(self) -> SemanticActionNode:
        """Compatibility view of the currently relevant action node."""

        pending = self.pending_nodes()
        if pending:
            return pending[0]
        return self.mission.action_nodes[-1]

    @goal.setter
    def goal(self, value: SemanticActionNode) -> None:
        if len(self.mission.action_nodes) != 1:
            raise ValueError("cannot replace one node in a multi-action mission")
        self.mission.action_nodes = (value,)

    @property
    def required_postconditions(self) -> tuple[str, ...]:
        return self.mission.required_postconditions

    @property
    def requires_execution(self) -> bool:
        return self.mission.requires_execution

    def require_execution(self) -> None:
        self.mission.execution_required_override = True
        if not any(node.requires_execution for node in self.mission.action_nodes):
            self.mission.action_nodes[0].requires_execution = True

    def bind_goal_contract(self, contract) -> None:
        """Bind one validated canonical authority for every later decision."""

        from osia.goal_contract import GoalContract, validate_goal_contract

        if not isinstance(contract, GoalContract):
            raise TypeError("goal contract is required")
        validated = validate_goal_contract(contract)
        if _bounded(validated.root_objective, 32_000) != _bounded(
            self.mission.objective, 32_000,
        ):
            raise ValueError("goal contract objective does not match mission")
        safe = json.loads(json.dumps(
            validated.to_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ))
        if self._goal_contract and self._goal_contract != safe:
            raise ValueError("execution strategy goal contract is immutable")
        self._goal_contract = safe

    def goal_contract_dict(self) -> dict:
        return json.loads(json.dumps(self._goal_contract)) if self._goal_contract else {}

    def pending_nodes(self) -> list[SemanticActionNode]:
        return [
            node for node in self.mission.action_nodes
            if not all(
                item in self.proven_postconditions
                for item in node.required_postconditions
            )
        ]

    def node_by_id(self, node_id: str) -> SemanticActionNode | None:
        return next(
            (node for node in self.mission.action_nodes if node.node_id == node_id),
            None,
        )

    def attempt_by_id(self, attempt_id: str) -> StrategyAttempt | None:
        return next(
            (
                item for item in reversed(self.attempts)
                if item.attempt_id == str(attempt_id or "")
            ),
            None,
        )

    def preparatory_effect_is_frozen(self) -> bool:
        attempt = self.attempt_by_id(self._frozen_attempt_id)
        return bool(
            self.frozen_reason
            and attempt is not None
            and attempt.attempted_effect in {
                "application_foreground", "window_focus", "surface_rebound",
            }
        )

    def primitive_postcondition_for_attempt(self, attempt_id: str) -> str:
        attempt = self.attempt_by_id(attempt_id)
        return attempt.primitive_postcondition if attempt is not None else ""

    def attempted_effect_for_attempt(self, attempt_id: str) -> str:
        attempt = self.attempt_by_id(attempt_id)
        return attempt.attempted_effect if attempt is not None else ""

    def remember_completed_effect(
        self,
        attempted_effect: str,
        *,
        target_application: str = "",
        target_surface: str = "",
        evidence: str = "",
        observed_at: float = 0.0,
    ) -> bool:
        """Retain one strongly verified semantic effect independently of a node."""

        effect = _bounded(attempted_effect, 160)
        application = _normalized(target_application)
        surface = _normalized(target_surface)
        if not effect:
            return False
        try:
            observed = max(0.0, float(observed_at or 0.0))
        except (TypeError, ValueError, OverflowError):
            observed = 0.0
        fact = {
            "attempted_effect": effect,
            "target_application": application,
            "target_surface": surface,
            "evidence": _bounded(evidence, 180),
            "observed_at": observed,
        }
        identity = (effect, application, surface)
        with self._lock:
            prior = next((
                item for item in self._completed_effects
                if (
                    item.get("attempted_effect"),
                    item.get("target_application"),
                    item.get("target_surface"),
                ) == identity
            ), None)
            if prior is not None:
                if observed >= float(prior.get("observed_at") or 0.0):
                    prior.update(fact)
                return True
            self._completed_effects.append(fact)
            self._completed_effects = self._completed_effects[-32:]
        return True

    def invalidate_completed_effect(
        self,
        attempted_effect: str,
        *,
        target_application: str = "",
        target_surface: str = "",
    ) -> bool:
        """Forget an effect only after stronger current evidence disproves it."""

        effect = _bounded(attempted_effect, 160)
        application = _normalized(target_application)
        surface = _normalized(target_surface)
        with self._lock:
            before = len(self._completed_effects)
            self._completed_effects = [
                item for item in self._completed_effects
                if not (
                    item.get("attempted_effect") == effect
                    and (
                        not application
                        or item.get("target_application") == application
                    )
                    and (
                        not surface or item.get("target_surface") == surface
                    )
                )
            ]
            return len(self._completed_effects) != before

    def completed_effect_facts(self) -> tuple[dict[str, object], ...]:
        """Return bounded structured prerequisite/world facts for this strategy."""

        with self._lock:
            for node in self.mission.action_nodes:
                if not node.required_postconditions or not all(
                    item in self.proven_postconditions
                    for item in node.required_postconditions
                ):
                    continue
                attempt = next((
                    item for item in reversed(self.attempts)
                    if item.node_id == node.node_id and item.attempted_effect
                ), None)
                if attempt is not None:
                    self.remember_completed_effect(
                        attempt.attempted_effect,
                        target_application=(
                            attempt.target_application
                            or node.required_application
                        ),
                        target_surface=attempt.target_surface or node.required_surface,
                        evidence=next(iter(node.evidence_links), ""),
                        observed_at=attempt.dispatched_at,
                    )
            observation = self._latest_desktop_observation
            if (
                observation.get("target_resolution") == "exact_target"
                and observation.get("window_surface_ref")
            ):
                self.remember_completed_effect(
                    "application_open",
                    target_application=(
                        observation.get("requested_application")
                        or observation.get("application")
                    ),
                    target_surface=observation.get("window_surface_ref"),
                    evidence="fresh_exact_desktop_target",
                    observed_at=float(observation.get("observed_at") or 0.0),
                )
            return tuple(dict(item) for item in self._completed_effects)

    def effect_is_completed(
        self,
        attempted_effect: str,
        *,
        target_application: str = "",
        target_surface: str = "",
    ) -> bool:
        effect = _bounded(attempted_effect, 160)
        application = _normalized(target_application)
        surface = _normalized(target_surface)
        observation = self._latest_desktop_observation
        observed_application = _normalized(
            observation.get("requested_application")
            or observation.get("application")
        )
        if effect == "application_foreground":
            try:
                observation_age = time.time() - float(
                    observation.get("observed_at") or 0.0
                )
            except (TypeError, ValueError, OverflowError):
                observation_age = float("inf")
            if (
                observation.get("target_resolution") != "exact_target"
                or observation.get("target_foreground") is not True
                or not observation.get("window_surface_ref")
                or not application
                or observed_application != application
                or (surface and _normalized(
                    observation.get("window_surface_ref")
                ) != surface)
                or not -5.0 <= observation_age <= 30.0
            ):
                return False
        if (
            effect == "application_open"
            and application
            and observed_application == application
            and not observation.get("window_surface_ref")
            and observation.get("target_resolution") in {
                "target_missing", "target_not_found",
                "semantic_target_surface_not_found",
            }
        ):
            return False
        return any(
            item.get("attempted_effect") == effect
            and (
                not application
                or item.get("target_application") == application
            )
            and (
                not surface
                or not item.get("target_surface")
                or item.get("target_surface") == surface
            )
            for item in self.completed_effect_facts()
        )

    @staticmethod
    def _tool_action(tool_name: str, arguments: dict) -> str:
        if tool_name == "open_application":
            return "open"
        if tool_name in {"desktop_type", "type_text", "fill", "fill_form_field"}:
            return "type"
        if tool_name in {"open_uri", "navigate", "navigate_browser", "open_browser_tab"}:
            return "navigate"
        if tool_name in {
            "click", "find_and_click", "desktop_click",
            "activate_desktop_surface", "activate_browser_element",
        }:
            return "activate"
        if tool_name in {"search_web", "search_files"}:
            return "search"
        if tool_name == "run_command":
            command = _normalized(arguments.get("command"))
            if re.search(r"\b(start|start-process|open|explorer)\b", command):
                return "open"
        return ""

    def _tool_application(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "open_application":
            return _canonical_application(arguments.get("name"))
        if tool_name == "open_uri":
            return _uri_identity(arguments.get("uri", ""))[0]
        if tool_name in _DOM_ACTION_TOOLS:
            url = arguments.get("url") or arguments.get("target_url") or ""
            return _url_identity(url)[0]
        if tool_name == "run_command":
            command = _normalized(arguments.get("command"))
            mission_applications = [
                _normalized(node.required_application)
                for node in self.mission.action_nodes
                if node.required_application
            ]
            command_aliases = {
                "calculator": ("calc",),
                "character map": ("charmap",),
                "file explorer": ("explorer",),
            }
            for node_name in (*mission_applications, *_KNOWN_APPLICATIONS):
                normalized = _normalized(node_name)
                aliases = (normalized, *command_aliases.get(normalized, ()))
                if any(
                    re.search(
                        rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                        command,
                    )
                    for alias in aliases
                ):
                    return "vscode" if normalized == "visual studio code" else normalized
        return ""

    def match_action_node(
        self,
        tool_name: str,
        arguments: dict | None = None,
        *,
        include_completed: bool = False,
    ) -> SemanticActionNode | None:
        arguments = arguments if isinstance(arguments, dict) else {}
        candidates = (
            list(self.mission.action_nodes) if include_completed else self.pending_nodes()
        )
        candidates = [node for node in candidates if node.status != "blocked"]
        executable = [node for node in candidates if node.requires_execution]
        if executable:
            candidates = executable
        if not candidates:
            return None
        tool_app = self._tool_application(tool_name, arguments)
        tool_action = self._tool_action(tool_name, arguments)
        serialized = _normalized(json.dumps(arguments, sort_keys=True, default=str))
        scored: list[tuple[int, int, SemanticActionNode]] = []
        for node in candidates:
            if (
                tool_app
                and node.required_application
                and tool_app != _normalized(node.required_application)
            ):
                continue
            if (
                tool_action
                and node.requested_action
                and tool_action != node.requested_action
            ):
                continue
            score = 0
            if tool_app and tool_app == _normalized(node.required_application):
                score += 20
            if tool_action and tool_action == node.requested_action:
                score += 8
            target_tokens = [
                token for token in _normalized(node.target_object).split()
                if len(token) >= 3
            ]
            score += sum(1 for token in target_tokens[:6] if token in serialized)
            if node.status in {"active", "unknown"}:
                score += 4
            scored.append((score, -node.order, node))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return scored[0][2]

    def postcondition_for_tool(self, tool_name: str, arguments: dict | None = None) -> str:
        node = self.match_action_node(tool_name, arguments)
        return node.observable_success_postcondition if node is not None else ""

    def prove_informational_nodes(self, evidence: str) -> tuple[str, ...]:
        proven = []
        for node in self.pending_nodes():
            if node.requires_execution:
                continue
            for postcondition in node.required_postconditions:
                if self.verify_postcondition(evidence, postcondition=postcondition):
                    proven.append(postcondition)
        return tuple(proven)

    def record_external_event(
        self,
        tool_name: str,
        arguments: dict | None,
        *,
        outcome: str,
        evidence: str,
        semantic_node_id: str = "",
    ) -> SemanticActionNode | None:
        if semantic_node_id:
            node = self.node_by_id(str(semantic_node_id))
        else:
            node = self.match_action_node(tool_name, arguments)
        if node is None:
            return None
        normalized = _normalized(outcome).replace(" ", "_")
        link = _bounded(evidence, 180)
        if link and link not in node.evidence_links:
            node.evidence_links = tuple((*node.evidence_links, link)[-16:])
        if (
            tool_name == "run_command"
            and node.required_application
            and normalized == "success_verified"
        ):
            # A process/command receipt is not an observation of desktop state.
            # Preserve correlation without supplying terminality a postcondition.
            return replace(
                node,
                observable_success_postcondition="",
                required_postconditions=(),
            )
        if normalized == "success_verified":
            for postcondition in node.required_postconditions:
                self.verify_postcondition(link or outcome, postcondition=postcondition)
            node.status = "succeeded"
        elif normalized in {"outcome_unknown", "unknown_after_start"}:
            node.status = "unknown"
            self._frozen_node_id = node.node_id
            self.frozen_reason = "effect_possible_postcondition_unknown"
            selected = next(
                (
                    item for item in reversed(self.attempts)
                    if item.node_id == node.node_id
                    and item.status in {
                        AttemptStatus.SELECTED.value,
                        AttemptStatus.AMBIGUOUS.value,
                    }
                ),
                None,
            )
            if selected is not None:
                self._frozen_mechanism_id = selected.mechanism_id
                self._frozen_attempt_id = selected.attempt_id
                self._retired_effect_mechanisms[
                    self._effect_scope(selected.mechanism_id, node.node_id)
                ] = "outcome_unknown"
        elif normalized in {"dispatch_started", "read_only", "permission_pending"}:
            node.status = "active"
        elif normalized in {
            "permission_user_denied",
        }:
            node.status = "blocked"
            self.terminal_blocker = normalized
        elif normalized in {
            "failed_before_effect", "confirmed_failure_before_effect",
            "rejected_before_execution",
            "permission_presentation_failed", "permission_timeout",
            "permission_disconnected", "permission_internal_failure",
        }:
            node.status = "pending"
        return node

    def register(self, mechanism: StrategyMechanism) -> None:
        with self._lock:
            self._mechanisms.setdefault(mechanism.mechanism_id, mechanism)

    def mechanism_for_layer(
        self,
        layer: StrategyLayer,
        *,
        node_id: str = "",
    ) -> StrategyMechanism | None:
        with self._lock:
            matches = [
                item for item in self._mechanisms.values()
                if item.layer is layer
                and (not node_id or item.node_id == node_id)
            ]
            matches.sort(key=lambda item: item.mechanism_id)
            return matches[0] if matches else None

    def mark_inapplicable(
        self,
        mechanism: StrategyMechanism,
        *,
        reason_code: str,
    ) -> StrategyAttempt | None:
        """Retire one mechanism only with explicit applicability evidence."""

        with self._lock:
            self.register(mechanism)
            if self._exhausted(mechanism.mechanism_id):
                return next(
                    (
                        item for item in reversed(self.attempts)
                        if item.mechanism_id == mechanism.mechanism_id
                        and item.status == AttemptStatus.INAPPLICABLE.value
                    ),
                    None,
                )
            node = self._node_for_mechanism(mechanism)
            attempt = StrategyAttempt(
                attempt_id=f"strategy_{len(self.attempts) + 1:03d}",
                mechanism_id=mechanism.mechanism_id,
                layer=int(mechanism.layer),
                mechanism=mechanism.label,
                reason_selected=mechanism.reason_selected,
                action_signature=self.action_signature(
                    mechanism.mechanism_id,
                    {"inapplicable": _bounded(reason_code, 120)},
                ),
                status=AttemptStatus.INAPPLICABLE.value,
                failure_or_stagnation_reason=(
                    _bounded(reason_code, 120) or "mechanism_inapplicable"
                ),
                node_id=node.node_id if node is not None else mechanism.node_id,
                attempted_effect=mechanism.attempted_effect,
                target_application=mechanism.application,
                target_surface=mechanism.surface,
            )
            self.attempts.append(attempt)
            attempt.alternatives_still_applicable = [
                item.mechanism_id for item in self.remaining_mechanisms()
            ]
            return attempt

    def _node_for_mechanism(
        self,
        mechanism: StrategyMechanism,
        action: object | None = None,
    ) -> SemanticActionNode | None:
        if mechanism.node_id:
            node = self.node_by_id(mechanism.node_id)
            return (
                node
                if node in self.pending_nodes() and node.status != "blocked"
                else None
            )
        action_payload = action if isinstance(action, dict) else {}
        tool_name = str(action_payload.get("tool") or "")
        arguments = action_payload.get("arguments")
        if tool_name:
            matched = self.match_action_node(
                tool_name,
                arguments if isinstance(arguments, dict) else action_payload,
            )
            if matched is not None:
                return matched
        candidates = self.pending_nodes()
        if mechanism.application:
            normalized = _normalized(mechanism.application)
            for node in candidates:
                if _normalized(node.required_application) == normalized:
                    return node
        return candidates[0] if candidates else None

    def _semantic_error(
        self,
        mechanism: StrategyMechanism,
        node: SemanticActionNode | None = None,
    ) -> str:
        goal = node or self._node_for_mechanism(mechanism)
        if goal is None:
            return "no_pending_action_node"
        pending = [node for node in self.pending_nodes() if node.requires_execution]
        if pending and goal.requires_execution and goal.order != pending[0].order:
            return "earlier_action_node_pending"
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
        matching = [
            attempt for attempt in self.attempts
            if attempt.mechanism_id == mechanism_id
        ]
        mechanism = self._mechanisms.get(mechanism_id)
        if (
            mechanism is not None
            and mechanism.layer is StrategyLayer.NATIVE_ACCESSIBILITY
        ):
            if any(
                attempt.status == AttemptStatus.INAPPLICABLE.value
                and not attempt.target_surface
                for attempt in matching
            ):
                return True
            if any(
                attempt.status == AttemptStatus.AMBIGUOUS.value
                for attempt in matching
            ):
                return True
            candidate_failures = [
                attempt for attempt in matching
                if attempt.target_surface
                and attempt.substep_id
                and attempt.status in {
                    AttemptStatus.FAILED.value,
                    AttemptStatus.STAGNATED.value,
                    AttemptStatus.STALE.value,
                    AttemptStatus.BLOCKED.value,
                }
            ]
            if candidate_failures:
                node = self.node_by_id(mechanism.node_id) or self.goal
                return _structured_uia_frontier_state(
                    self, node,
                ) == "exhausted"
            if any(
                attempt.status in {
                    AttemptStatus.FAILED.value,
                    AttemptStatus.STAGNATED.value,
                    AttemptStatus.STALE.value,
                }
                for attempt in matching
            ):
                return True
        progressive_desktop = bool(
            mechanism is not None
            and mechanism.layer in {
                StrategyLayer.NATIVE_ACCESSIBILITY,
                StrategyLayer.KEYBOARD,
            }
        )
        progress_count = sum(
            attempt.status == AttemptStatus.PROGRESSED.value
            for attempt in matching
        )
        for attempt in matching:
            if attempt.status not in _TERMINAL_FOR_ESCALATION:
                continue
            if (
                progressive_desktop
                and attempt.status == AttemptStatus.PROGRESSED.value
                and progress_count
                < self._MAX_PROGRESS_ATTEMPTS_PER_DESKTOP_MECHANISM
            ):
                continue
            return True
        return False

    def remaining_mechanisms(self) -> list[StrategyMechanism]:
        with self._lock:
            return sorted(
                (
                    mechanism for mechanism in self._mechanisms.values()
                    if mechanism.applicable
                    and self._node_for_mechanism(mechanism) is not None
                    and not self._semantic_error(
                        mechanism, self._node_for_mechanism(mechanism),
                    )
                    and not self._scope_retirement_reason(
                        mechanism.mechanism_id,
                        self._node_for_mechanism(mechanism).node_id,
                    )
                    and not self._scope_is_in_flight(
                        mechanism.mechanism_id,
                        self._node_for_mechanism(mechanism).node_id,
                    )
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
    def _effect_scope(mechanism_id: str, node_id: str) -> str:
        """Return the durable replay scope for one mechanism on one goal node."""

        return f"{_bounded(node_id, 96)}|{_bounded(mechanism_id, 120)}"

    def _scope_is_in_flight(self, mechanism_id: str, node_id: str) -> bool:
        scope = self._effect_scope(mechanism_id, node_id)
        return any(
            item.status == AttemptStatus.SELECTED.value
            and self._effect_scope(item.mechanism_id, item.node_id) == scope
            for item in self.attempts
        )

    def _scope_retirement_reason(self, mechanism_id: str, node_id: str) -> str:
        return self._retired_effect_mechanisms.get(
            self._effect_scope(mechanism_id, node_id),
            "",
        )

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

    def preview(
        self,
        mechanism: StrategyMechanism,
        action: object | None = None,
    ) -> StrategyDecision:
        with self._lock:
            if self.succeeded:
                return StrategyDecision(False, "postcondition_already_satisfied")
            if any(
                self._evidence_revision <= revision
                for revision in self._stale_mechanisms.values()
            ):
                return StrategyDecision(False, "stale_target_requires_reresolution")
            if not mechanism.applicable:
                return StrategyDecision(False, "mechanism_not_applicable")
            node = self._node_for_mechanism(mechanism, action)
            semantic_error = self._semantic_error(mechanism, node)
            if semantic_error:
                return StrategyDecision(
                    False, semantic_error, node_id=(node.node_id if node else ""),
                )
            node_id = node.node_id if node is not None else mechanism.node_id
            if (
                self.frozen_reason
                and mechanism.mutation
                and not (
                    self.preparatory_effect_is_frozen()
                    and mechanism.attempted_effect
                    not in {
                        "application_foreground", "window_focus",
                        "surface_rebound",
                    }
                )
            ):
                return StrategyDecision(
                    False,
                    "ambiguous_effect_requires_reconciliation",
                    node_id=node_id,
                )
            retirement = self._scope_retirement_reason(
                mechanism.mechanism_id, node_id,
            )
            if retirement:
                return StrategyDecision(
                    False,
                    "mechanism_retired_after_" + retirement,
                    node_id=node_id,
                )
            if mechanism.mutation and self._scope_is_in_flight(
                mechanism.mechanism_id, node_id,
            ):
                return StrategyDecision(
                    False, "mechanism_dispatch_in_flight", node_id=node_id,
                )
            higher = self._higher_unexhausted(mechanism.layer)
            if higher:
                return StrategyDecision(
                    False,
                    "higher_strategy_still_applicable:" + ",".join(
                        item.mechanism_id for item in higher[:8]
                    ),
                )
            return StrategyDecision(
                True,
                "strategy_selected",
                mechanism_id=mechanism.mechanism_id,
                node_id=(node.node_id if node else ""),
                attempted_effect=mechanism.attempted_effect,
            )

    def select(self, mechanism: StrategyMechanism, action: object) -> StrategyDecision:
        with self._lock:
            self.register(mechanism)
            signature = self.action_signature(mechanism.mechanism_id, action)
            preview = self.preview(mechanism, action)
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
                    node_id=preview.node_id,
                    attempted_effect=mechanism.attempted_effect,
                    target_application=mechanism.application,
                    target_surface=mechanism.surface,
                )
                self.attempts.append(attempt)
                return StrategyDecision(
                    False, preview.reason_code, attempt.attempt_id,
                    mechanism.mechanism_id, preview.node_id,
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
            action_payload = action if isinstance(action, dict) else {}
            primitive_tool = str(action_payload.get("tool") or "")
            primitive_arguments = action_payload.get("arguments")
            primitive_arguments = (
                primitive_arguments
                if isinstance(primitive_arguments, dict) else action_payload
            )
            preparatory_activation = bool(
                primitive_tool == "activate_desktop_surface"
                and _normalized(primitive_arguments.get("action_pattern"))
                in {"", "window"}
                and _bounded(primitive_arguments.get("surface_ref"), 160)
                == _bounded(
                    self._latest_desktop_observation.get(
                        "window_surface_ref"
                    ),
                    160,
                )
            )
            primitive_key = f"{preview.node_id}|{signature}"
            if (
                primitive_tool == "activate_desktop_surface"
                and not preparatory_activation
                and primitive_key in self._completed_primitive_signatures
            ):
                return StrategyDecision(
                    False,
                    "primitive_already_completed",
                    mechanism_id=mechanism.mechanism_id,
                    node_id=preview.node_id,
                )
            observable_effect = {}
            if (
                primitive_tool == "activate_desktop_surface"
                and not preparatory_activation
            ):
                observable_effect = normalize_desktop_observable_effect(
                    primitive_arguments.get("observable_effect")
                )
                completed_effects = [
                    normalize_desktop_observable_effect(
                        item.observable_effect,
                    )
                    for item in self.attempts
                    if item.composite_id == preview.node_id
                    and item.status in {
                        AttemptStatus.PROGRESSED.value,
                        AttemptStatus.SUCCEEDED.value,
                    }
                ]
                same_surface_effects = [
                    item for item in completed_effects
                    if item
                    and observable_effect
                    and item["surface_ref"] == observable_effect["surface_ref"]
                ]
                if (
                    len(same_surface_effects) >= 2
                    and observable_effect != same_surface_effects[-1]
                    and observable_effect in same_surface_effects[:-1]
                ):
                    return StrategyDecision(
                        False,
                        "primitive_observable_witness_regression",
                        mechanism_id=mechanism.mechanism_id,
                        node_id=preview.node_id,
                    )
            retry_identity = self._retry_substeps.pop(primitive_key, None)
            pending_substep = _pending_uia_substep(self, preview.node_id)
            if (
                primitive_tool == "activate_desktop_surface"
                and not preparatory_activation
                and pending_substep
                and _uia_effects_match_substep(
                    pending_substep.get("observable_effect"),
                    observable_effect,
                )
            ):
                retry_identity = pending_substep
            composite_id = ""
            substep_id = ""
            substep_index = 0
            primitive_postcondition = ""
            if (
                primitive_tool == "activate_desktop_surface"
                and not preparatory_activation
            ):
                composite_id = preview.node_id
                if isinstance(retry_identity, dict):
                    substep_id = str(retry_identity.get("substep_id") or "")
                    substep_index = int(retry_identity.get("substep_index") or 0)
                if not substep_id or substep_index <= 0:
                    substep_index = self._substep_counters.get(composite_id, 0) + 1
                    self._substep_counters[composite_id] = substep_index
                    material = f"{composite_id}|{substep_index}|{signature}"
                    substep_id = "substep_" + hashlib.sha256(
                        material.encode("utf-8")
                    ).hexdigest()[:20]
                primitive_postcondition = observable_effect_postcondition(
                    observable_effect,
                )
            elif (
                primitive_tool in {"desktop_press", "desktop_type"}
                and pending_substep
            ):
                composite_id = str(
                    pending_substep.get("composite_id") or preview.node_id
                )
                substep_id = str(pending_substep.get("substep_id") or "")
                substep_index = int(
                    pending_substep.get("substep_index") or 0
                )
                primitive_postcondition = str(
                    pending_substep.get("primitive_postcondition")
                    or pending_substep.get("attempted_effect")
                    or ""
                )
                observable_effect = normalize_desktop_observable_effect(
                    pending_substep.get("observable_effect"),
                )
            target_surface_identity = {}
            action_pattern = ""
            if primitive_tool == "activate_desktop_surface":
                action_pattern = _canonical_uia_action_pattern(
                    primitive_arguments.get("action_pattern"),
                )
                target_surface_identity = _capture_desktop_surface_identity(
                    primitive_arguments.get("surface_ref"),
                )
                observed_identity = _observed_uia_candidate_identity(
                    self,
                    primitive_arguments.get("surface_ref"),
                    action_pattern,
                )
                if observed_identity:
                    merged_identity = dict(observed_identity)
                    merged_identity.update({
                        key: value
                        for key, value in target_surface_identity.items()
                        if value not in (None, "", [], 0, 0.0)
                        or key == "schema_version"
                    })
                    target_surface_identity = (
                        _normalize_desktop_surface_identity(merged_identity)
                    )
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
                node_id=preview.node_id,
                attempted_effect=(
                    mechanism.attempted_effect or primitive_postcondition
                ),
                target_application=mechanism.application,
                target_surface=(
                    _bounded(primitive_arguments.get("surface_ref"), 160)
                    if primitive_tool == "activate_desktop_surface" else
                    _bounded(observable_effect.get("surface_ref"), 160)
                    if primitive_tool in {"desktop_press", "desktop_type"}
                    and pending_substep else
                    mechanism.surface
                ),
                target_surface_identity=target_surface_identity,
                dispatched_at=time.time(),
                dispatch_evidence_revision=self._evidence_revision,
                composite_id=composite_id,
                substep_id=substep_id,
                substep_index=substep_index,
                primitive_postcondition=primitive_postcondition,
                observable_effect=observable_effect,
                action_pattern=action_pattern,
            )
            self.attempts.append(attempt)
            self._signature_attempt_counts[signature] = (
                self._signature_attempt_counts.get(signature, 0) + 1
            )
            self.current_mechanism = mechanism.mechanism_id
            self.current_layer = int(mechanism.layer)
            node = self.node_by_id(preview.node_id)
            if node is not None:
                node.status = "active"
            return StrategyDecision(
                True, "strategy_selected", attempt.attempt_id,
                mechanism.mechanism_id, preview.node_id,
                attempt.attempted_effect,
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
        primitive_receipt: dict | None = None,
        semantic_barrier: bool = True,
    ) -> StrategyAttempt | None:
        with self._lock:
            attempt = next(
                (item for item in reversed(self.attempts) if item.attempt_id == decision.attempt_id),
                None,
            )
            if attempt is None or not decision.allowed:
                return attempt
            mechanism = self._mechanisms[attempt.mechanism_id]
            node = self.node_by_id(attempt.node_id) or self.goal
            normalized_outcome = _normalized(outcome).replace(" ", "_")
            if isinstance(primitive_receipt, dict):
                attempt.primitive_receipt = json.loads(json.dumps(
                    primitive_receipt,
                    ensure_ascii=True,
                    sort_keys=True,
                    default=lambda _value: "<unsupported>",
                ))
            if observation:
                attempt.observations_gained.append(_bounded(observation, 160))
                self._evidence_revision += 1
            if normalized_outcome in {"ambiguous", "pending", "unknown_after_start"}:
                attempt.status = AttemptStatus.AMBIGUOUS.value
                attempt.failure_or_stagnation_reason = (
                    _bounded(failure_reason, 120) or "effect_possible_postcondition_unknown"
                )
                self._retired_effect_mechanisms[
                    self._effect_scope(attempt.mechanism_id, node.node_id)
                ] = "outcome_unknown"
                if semantic_barrier:
                    self.frozen_reason = attempt.failure_or_stagnation_reason
                    self._frozen_node_id = node.node_id
                    self._frozen_mechanism_id = attempt.mechanism_id
                    self._frozen_attempt_id = attempt.attempt_id
                    node.status = "unknown"
                else:
                    # Preparatory activation/focus has its own effect identity.
                    # Its uncertainty retires only that exact preparatory
                    # mechanism; it cannot freeze the pending semantic mutation.
                    self.frozen_reason = attempt.failure_or_stagnation_reason
                    self._frozen_node_id = node.node_id
                    self._frozen_mechanism_id = attempt.mechanism_id
                    self._frozen_attempt_id = attempt.attempt_id
                    node.status = "pending"
            elif normalized_outcome == "failed_before_effect":
                attempt.status = AttemptStatus.FAILED.value
                attempt.failure_or_stagnation_reason = (
                    _bounded(failure_reason, 120) or "failed_before_effect"
                )
                # No semantic actuation began, so the exact semantic candidate
                # remains eligible. Do not consume its evidence-gated retry.
                node.status = "pending"
            elif normalized_outcome == "stale":
                attempt.status = AttemptStatus.STALE.value
                attempt.failure_or_stagnation_reason = (
                    _bounded(failure_reason, 120) or "stale_target"
                )
                self._stale_mechanisms[attempt.mechanism_id] = self._evidence_revision
                self._failed_signatures[attempt.action_signature] = self._evidence_revision
                node.status = "pending"
            elif normalized_outcome in {"stagnated", "stagnation"}:
                attempt.status = AttemptStatus.STAGNATED.value
                attempt.failure_or_stagnation_reason = (
                    _bounded(failure_reason, 120) or "strategy_stagnated"
                )
                self._failed_signatures[attempt.action_signature] = self._evidence_revision
                node.status = "pending"
            elif normalized_outcome in {"failed", "failure", "rejected"}:
                attempt.status = AttemptStatus.FAILED.value
                attempt.failure_or_stagnation_reason = (
                    _bounded(failure_reason, 120) or "strategy_failed"
                )
                self._failed_signatures[attempt.action_signature] = self._evidence_revision
                node.status = "pending"
            elif normalized_outcome in {"progress", "progressed"}:
                attempt.status = AttemptStatus.PROGRESSED.value
                node.status = "pending"
                if attempt.mechanism_id == "direct:run_command":
                    self._retired_effect_mechanisms[
                        self._effect_scope(attempt.mechanism_id, node.node_id)
                    ] = "unverified_progress"
            elif normalized_outcome in {"succeeded", "success"}:
                evidence = _bounded(postcondition_evidence, 180)
                if not evidence:
                    attempt.status = AttemptStatus.FAILED.value
                    attempt.failure_or_stagnation_reason = "postcondition_unverified"
                    self._failed_signatures[attempt.action_signature] = self._evidence_revision
                elif mechanism.covers_goal:
                    attempt.status = AttemptStatus.SUCCEEDED.value
                    attempt.postcondition_evidence = evidence
                    self.postcondition_evidence.append(evidence)
                    for postcondition in node.required_postconditions:
                        self.proven_postconditions[postcondition] = evidence
                    node.status = "succeeded"
                    node.evidence_links = tuple(
                        (*node.evidence_links, evidence)[-16:]
                    )
                    self.succeeded = all(
                        item in self.proven_postconditions
                        for item in self.required_postconditions
                    )
                else:
                    attempt.status = AttemptStatus.PROGRESSED.value
                    attempt.postcondition_evidence = evidence
                    self.postcondition_evidence.append(evidence)
                    node.status = "pending"
                    if attempt.mechanism_id == "direct:run_command":
                        self._retired_effect_mechanisms[
                            self._effect_scope(attempt.mechanism_id, node.node_id)
                        ] = "unverified_progress"
            else:
                attempt.status = AttemptStatus.FAILED.value
                attempt.failure_or_stagnation_reason = "invalid_strategy_outcome"
                self._failed_signatures[attempt.action_signature] = self._evidence_revision
                node.status = "pending"
            attempt.alternatives_still_applicable = [
                item.mechanism_id for item in self.remaining_mechanisms()
            ]
            return attempt

    def reconcile(
        self,
        *,
        succeeded: bool,
        evidence: str = "",
        evidence_details: dict | None = None,
        primitive_only: bool = False,
    ) -> bool:
        with self._lock:
            if not self.frozen_reason:
                return False
            reconciled_node_id = self._frozen_node_id
            reconciled_mechanism_id = self._frozen_mechanism_id
            reconciled_attempt_id = self._frozen_attempt_id
            attempt = next(
                (
                    item for item in reversed(self.attempts)
                    if item.attempt_id == reconciled_attempt_id
                ),
                None,
            )
            details = (
                dict(evidence_details)
                if isinstance(evidence_details, dict) else {}
            )
            observed_at = details.get("observed_at")
            try:
                observed_epoch = float(observed_at or 0.0)
            except (TypeError, ValueError):
                observed_epoch = 0.0
            if (
                details.get("evidence_kind")
                == "exact_independent_desktop_state"
                and (
                    attempt is None
                    or attempt.dispatched_at <= 0.0
                    or observed_epoch < attempt.dispatched_at
                )
            ):
                return False
            node = self.node_by_id(reconciled_node_id)
            mechanism = self._mechanisms.get(reconciled_mechanism_id)
            attempted_effect = (
                attempt.attempted_effect if attempt is not None else ""
            )
            preparatory_effect = bool(
                not primitive_only
                and attempt is not None
                and mechanism is not None
                and not mechanism.covers_goal
                and attempted_effect
                and _bounded(details.get("postcondition"), 320)
                == attempted_effect
            )
            reconciliation_postcondition = _bounded(
                details.get("postcondition"), 320,
            )
            if (
                attempted_effect
                and mechanism is not None
                and not primitive_only
                and (
                    reconciliation_postcondition == attempted_effect
                    or (
                        mechanism.covers_goal
                        and node is not None
                        and reconciliation_postcondition
                        in node.required_postconditions
                    )
                )
            ):
                reconciliation_postcondition = attempted_effect
            if primitive_only and attempt is None:
                return False
            if primitive_only and (
                str(details.get("substep_id") or "") != attempt.substep_id
                or str(details.get("composite_id") or "") != attempt.composite_id
            ):
                return False
            if primitive_only:
                primitive = (
                    attempt.primitive_receipt
                    if isinstance(attempt.primitive_receipt, dict) else {}
                )
                before = primitive.get("before")
                before = before if isinstance(before, dict) else {}
                declared_effect = normalize_desktop_observable_effect(
                    attempt.observable_effect,
                )
                observed_effect = normalize_desktop_observable_effect(
                    details.get("observable_effect"),
                )
                receipt_effect = normalize_desktop_observable_effect(
                    primitive.get("observable_effect"),
                )
                if declared_effect != observed_effect:
                    return False
                if receipt_effect and receipt_effect != declared_effect:
                    return False
                exact_matches = (
                    ("strategy_attempt_id", attempt.attempt_id),
                    ("semantic_node_id", attempt.node_id),
                    ("mechanism_id", attempt.mechanism_id),
                    ("attempted_effect", attempt.attempted_effect),
                    ("primitive_postcondition", attempt.primitive_postcondition),
                    ("postcondition", attempt.primitive_postcondition),
                    ("observed_application", attempt.target_application),
                )
                if any(
                    details.get(key) not in (None, "")
                    and _normalized(details.get(key)) != _normalized(expected)
                    for key, expected in exact_matches
                    if expected not in (None, "")
                ):
                    return False
                expected_surface = str(
                    before.get("window_surface_ref") or ""
                )
                expected_generation = str(
                    before.get("window_generation") or ""
                )
                observed_surface = str(
                    details.get("observed_surface") or ""
                )
                observed_generation = str(
                    details.get("window_generation") or ""
                )
                continuity = str(
                    details.get("surface_continuity") or ""
                )
                if succeeded:
                    if continuity == "exact_same_generation":
                        if (
                            not expected_surface
                            or not expected_generation
                            or observed_surface != expected_surface
                            or observed_generation != expected_generation
                            or str(details.get(
                                "previous_window_surface_ref"
                            ) or "") != expected_surface
                            or str(details.get(
                                "previous_window_generation"
                            ) or "") != expected_generation
                        ):
                            return False
                    elif continuity == "semantic_reresolution_proven":
                        identity = (
                            attempt.target_surface_identity
                            if isinstance(
                                attempt.target_surface_identity, dict,
                            ) else {}
                        )
                        primitive_kind = str(primitive.get("kind") or "")
                        if (
                            primitive_kind != "desktop_uia_primitive"
                            or not identity.get("runtime_signature")
                            or str(details.get(
                                "previous_window_surface_ref"
                            ) or "") != expected_surface
                            or str(details.get(
                                "previous_window_generation"
                            ) or "") != expected_generation
                            or str(details.get(
                                "rebound_window_surface_ref"
                            ) or "") != observed_surface
                            or str(details.get(
                                "rebound_window_generation"
                            ) or "") != observed_generation
                            or not str(details.get(
                                "rebound_target_surface_ref"
                            ) or "")
                            or not observed_surface
                            or not observed_generation
                        ):
                            return False
                    else:
                        return False
                elif observed_surface or observed_generation:
                    if (
                        observed_surface != expected_surface
                        or observed_generation != expected_generation
                    ):
                        return False
                if details.get("substep_index") not in (None, ""):
                    try:
                        if int(details["substep_index"]) != attempt.substep_index:
                            return False
                    except (TypeError, ValueError, OverflowError):
                        return False
            if primitive_only and succeeded and evidence:
                bounded = _bounded(evidence, 180)
                completes_parent = _primitive_proves_calculation_result(
                    node,
                    attempt,
                )
                attempt.status = (
                    AttemptStatus.SUCCEEDED.value
                    if completes_parent else
                    AttemptStatus.PROGRESSED.value
                )
                attempt.postcondition_evidence = bounded
                attempt.observations_gained.append(bounded)
                if node is not None:
                    if completes_parent:
                        for postcondition in node.required_postconditions:
                            self.proven_postconditions[postcondition] = bounded
                        self.postcondition_evidence.append(bounded)
                        node.status = "succeeded"
                        self.succeeded = all(
                            item in self.proven_postconditions
                            for item in self.required_postconditions
                        )
                    else:
                        node.status = "pending"
                    node.evidence_links = tuple(
                        (*node.evidence_links, bounded)[-16:]
                    )
                primitive_key = f"{attempt.node_id}|{attempt.action_signature}"
                self._completed_primitive_signatures.add(primitive_key)
                self._completed_substeps.append({
                    "composite_id": attempt.composite_id,
                    "substep_id": attempt.substep_id,
                    "substep_index": attempt.substep_index,
                    "attempt_id": attempt.attempt_id,
                    "action_signature": attempt.action_signature,
                    "attempted_effect": attempt.attempted_effect,
                    "evidence": bounded,
                })
                self._completed_substeps = self._completed_substeps[-64:]
            elif primitive_only:
                attempt.status = AttemptStatus.BLOCKED.value
                attempt.failure_or_stagnation_reason = (
                    "primitive_reconciled_not_applied"
                )
                self._failed_signatures[
                    attempt.action_signature
                ] = self._evidence_revision
                primitive_key = f"{attempt.node_id}|{attempt.action_signature}"
                self._retry_substeps[primitive_key] = {
                    "substep_id": attempt.substep_id,
                    "substep_index": attempt.substep_index,
                }
                if node is not None:
                    node.status = "pending"
            elif succeeded and evidence and preparatory_effect:
                bounded = _bounded(evidence, 180)
                attempt.status = AttemptStatus.PROGRESSED.value
                attempt.postcondition_evidence = bounded
                attempt.observations_gained.append(bounded)
                self.postcondition_evidence.append(bounded)
                self.remember_completed_effect(
                    attempted_effect,
                    target_application=(
                        attempt.target_application
                        or (node.required_application if node is not None else "")
                    ),
                    target_surface=(
                        attempt.target_surface
                        or (node.required_surface if node is not None else "")
                    ),
                    evidence=bounded,
                    observed_at=observed_epoch,
                )
                if node is not None:
                    node.status = "pending"
                    node.evidence_links = tuple(
                        (*node.evidence_links, bounded)[-16:]
                    )
            elif succeeded and evidence:
                bounded = _bounded(evidence, 180)
                self.postcondition_evidence.append(bounded)
                if node is not None:
                    for postcondition in node.required_postconditions:
                        self.proven_postconditions[postcondition] = bounded
                    node.status = "succeeded"
                    node.evidence_links = tuple(
                        (*node.evidence_links, bounded)[-16:]
                    )
                    self.succeeded = all(
                        item in self.proven_postconditions
                        for item in self.required_postconditions
                    )
                if attempt is not None and attempt.substep_id:
                    attempt.status = AttemptStatus.SUCCEEDED.value
                    primitive_key = f"{attempt.node_id}|{attempt.action_signature}"
                    self._completed_primitive_signatures.add(primitive_key)
                    self._completed_substeps.append({
                        "composite_id": attempt.composite_id,
                        "substep_id": attempt.substep_id,
                        "substep_index": attempt.substep_index,
                        "attempt_id": attempt.attempt_id,
                        "action_signature": attempt.action_signature,
                        "attempted_effect": attempt.attempted_effect,
                        "evidence": bounded,
                    })
                    self._completed_substeps = self._completed_substeps[-64:]
            elif self._frozen_node_id:
                node = self.node_by_id(self._frozen_node_id)
                if node is not None:
                    node.status = "pending"
            self._reconciliation_events.append({
                "attempt_id": _bounded(reconciled_attempt_id, 120),
                "node_id": _bounded(reconciled_node_id, 120),
                "mechanism_id": _bounded(reconciled_mechanism_id, 120),
                "resolution": (
                    "primitive_completed"
                    if primitive_only and succeeded and evidence else
                    "primitive_not_applied"
                    if primitive_only else
                    "completed" if succeeded and evidence else "absent"
                ),
                "evidence": _bounded(evidence, 180),
                "schema_version": "1" if details else "",
                "evidence_kind": _bounded(
                    details.get("evidence_kind"), 80,
                ),
                "evidence_source": _bounded(
                    details.get("evidence_source"), 80,
                ),
                "evidence_outcome": _bounded(
                    details.get("evidence_outcome"), 80,
                ),
                "reason_code": _bounded(details.get("reason_code"), 160),
                "postcondition": _bounded(
                    reconciliation_postcondition or (
                        attempt.primitive_postcondition
                        if primitive_only and attempt is not None else
                        attempt.attempted_effect
                        if attempt is not None else ""
                    ), 320,
                ),
                "observable_effect": (
                    dict(attempt.observable_effect)
                    if attempt is not None
                    and isinstance(attempt.observable_effect, dict)
                    else {}
                ),
                "composite_id": _bounded(
                    attempt.composite_id if attempt is not None else "", 160,
                ),
                "substep_id": _bounded(
                    attempt.substep_id if attempt is not None else "", 160,
                ),
                "substep_index": _bounded(
                    attempt.substep_index if attempt is not None else 0, 20,
                ),
                "observed_target": _bounded(
                    details.get("observed_target"), 160,
                ),
                "target_application": _bounded(
                    attempt.target_application
                    if attempt is not None and attempt.target_application else
                    node.required_application if node is not None else "", 160,
                ),
                "target_surface": _bounded(
                    attempt.target_surface
                    if attempt is not None and attempt.target_surface else
                    node.required_surface if node is not None else "", 160,
                ),
                "observed_application": _bounded(
                    details.get("observed_application"), 160,
                ),
                "observed_surface": _bounded(
                    details.get("observed_surface"), 160,
                ),
                "window_generation": _bounded(
                    details.get("window_generation"), 160,
                ),
                "surface_continuity": _bounded(
                    details.get("surface_continuity"), 80,
                ),
                "previous_window_surface_ref": _bounded(
                    details.get("previous_window_surface_ref"), 160,
                ),
                "previous_window_generation": _bounded(
                    details.get("previous_window_generation"), 160,
                ),
                "rebound_window_surface_ref": _bounded(
                    details.get("rebound_window_surface_ref"), 160,
                ),
                "rebound_window_generation": _bounded(
                    details.get("rebound_window_generation"), 160,
                ),
                "rebound_target_surface_ref": _bounded(
                    details.get("rebound_target_surface_ref"), 160,
                ),
                "structural_epoch": _bounded(
                    details.get("structural_epoch"), 40,
                ),
                "observed_at": _bounded(observed_epoch, 40),
                "dispatched_at": _bounded(
                    attempt.dispatched_at if attempt is not None else 0.0, 40,
                ),
                "dispatch_evidence_revision": _bounded(
                    (
                        attempt.dispatch_evidence_revision
                        if attempt is not None else 0
                    ),
                    40,
                ),
                "evidence_revision": _bounded(
                    self._evidence_revision + 1, 40,
                ),
                "no_partial_effect": (
                    "true" if details.get("no_partial_effect") is True else "false"
                ),
            })
            self._reconciliation_events = self._reconciliation_events[-16:]
            if primitive_only or (
                succeeded
                and evidence
                and preparatory_effect
                and attempted_effect == "application_foreground"
            ):
                self._retired_effect_mechanisms.pop(
                    self._effect_scope(
                        reconciled_mechanism_id, reconciled_node_id,
                    ),
                    None,
                )
            self.frozen_reason = ""
            self._frozen_node_id = ""
            self._frozen_mechanism_id = ""
            self._frozen_attempt_id = ""
            self._evidence_revision += 1
            return True

    def reconcile_semantic_batch_prefix(
        self,
        *,
        attempt_id: str,
        semantic_batch_id: str,
        applied_count: int,
        evidence: str,
    ) -> bool:
        """Advance only to an exactly observed, non-replayable batch prefix."""

        with self._lock:
            attempt = self.attempt_by_id(attempt_id)
            bounded = _bounded(evidence, 180)
            try:
                prefix_size = int(applied_count)
            except (TypeError, ValueError, OverflowError):
                return False
            if (
                attempt is None
                or attempt.attempt_id != self._frozen_attempt_id
                or not self.frozen_reason
                or not bounded
                or not semantic_batch_id
                or attempt.semantic_batch_id != semantic_batch_id
                or prefix_size <= 0
                or prefix_size >= len(attempt.semantic_batch_effects)
            ):
                return False
            attempt.status = AttemptStatus.PROGRESSED.value
            attempt.postcondition_evidence = bounded
            attempt.observations_gained.append(bounded)
            attempt.semantic_batch_resume_from_index = prefix_size
            node = self.node_by_id(attempt.node_id)
            if node is not None:
                node.status = "pending"
                node.evidence_links = tuple(
                    (*node.evidence_links, bounded)[-16:]
                )
            self._reconciliation_events.append({
                "attempt_id": attempt.attempt_id,
                "node_id": attempt.node_id,
                "mechanism_id": attempt.mechanism_id,
                "resolution": "semantic_batch_exact_prefix",
                "evidence": bounded,
                "reason_code": "semantic_batch_exact_prefix_reconciled",
                "semantic_batch_id": semantic_batch_id,
                "applied_count": prefix_size,
                "resume_from_index": prefix_size,
                "postcondition": attempt.attempted_effect,
                "composite_id": attempt.composite_id,
                "substep_id": attempt.substep_id,
                "substep_index": attempt.substep_index,
                "target_application": attempt.target_application,
                "target_surface": attempt.target_surface,
            })
            self._reconciliation_events = self._reconciliation_events[-16:]
            self._retired_effect_mechanisms.pop(
                self._effect_scope(attempt.mechanism_id, attempt.node_id),
                None,
            )
            self.frozen_reason = ""
            self._frozen_node_id = ""
            self._frozen_mechanism_id = ""
            self._frozen_attempt_id = ""
            self._evidence_revision += 1
            return True

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
            required = self.required_postconditions
            target = _bounded(postcondition, 320)
            if not target and len(required) == 1:
                target = required[0]
            if target not in required:
                return False
            self.proven_postconditions[target] = bounded
            self.succeeded = all(item in self.proven_postconditions for item in required)
            for node in self.mission.action_nodes:
                if target in node.required_postconditions:
                    node.status = "succeeded" if all(
                        item in self.proven_postconditions
                        for item in node.required_postconditions
                    ) else node.status
                    node.evidence_links = tuple(
                        (*node.evidence_links, bounded)[-16:]
                    )
                    break
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
                "goal_fingerprint": self.mission.fingerprint,
                "remaining": remaining,
                "uia_candidate_exhaustion": _uia_candidate_diagnostic(
                    self, self.goal,
                ) if _semantic_domain(self.goal) == "desktop" else {},
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
                "mission": self.mission.public_dict(),
                "goal_contract": self.goal_contract_dict(),
                # Kept for restoring pre-WOR-256 ledgers and focused callers.
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
                "retired_effect_mechanisms": dict(
                    self._retired_effect_mechanisms
                ),
                "completed_effects": list(self.completed_effect_facts()),
                "reconciliation_events": list(self._reconciliation_events),
                "terminal_prompt_tokens": sorted(self._terminal_prompt_tokens)[-32:],
                "frozen_node_id": self._frozen_node_id,
                "frozen_mechanism_id": self._frozen_mechanism_id,
                "frozen_attempt_id": self._frozen_attempt_id,
                "terminal_blocker": self.terminal_blocker,
                "substep_counters": dict(self._substep_counters),
                "completed_substeps": list(self._completed_substeps),
                "completed_primitive_signatures": sorted(
                    self._completed_primitive_signatures
                ),
                "retry_substeps": dict(self._retry_substeps),
                "latest_desktop_observation": dict(
                    self._latest_desktop_observation
                ),
                "available_tools": sorted(self.available_tools),
            }

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "ExecutionStrategy":
        """Restore the exact frontier for another provider segment."""

        if not isinstance(snapshot, dict):
            raise ValueError("invalid execution strategy snapshot")
        mission_data = snapshot.get("mission")
        if isinstance(mission_data, dict):
            nodes = []
            for raw_node in mission_data.get("action_nodes") or []:
                if not isinstance(raw_node, dict):
                    continue
                node_data = dict(raw_node)
                node_data["required_postconditions"] = tuple(
                    node_data.get("required_postconditions") or ()
                )
                node_data["evidence_links"] = tuple(
                    node_data.get("evidence_links") or ()
                )
                nodes.append(SemanticActionNode(**node_data))
            if not nodes:
                raise ValueError("invalid execution mission snapshot")
            mission = SemanticMission(
                objective=str(mission_data.get("objective") or ""),
                action_nodes=tuple(nodes),
                fingerprint=str(mission_data.get("fingerprint") or ""),
                execution_required_override=bool(
                    mission_data.get("execution_required_override")
                ),
            )
        elif isinstance(snapshot.get("goal"), dict):
            goal_data = dict(snapshot["goal"])
            goal_data["required_postconditions"] = tuple(
                goal_data.get("required_postconditions") or ()
            )
            goal_data["evidence_links"] = tuple(
                goal_data.get("evidence_links") or ()
            )
            mission = SemanticGoal(**goal_data)
        else:
            raise ValueError("invalid execution strategy snapshot")
        mechanisms = []
        for raw in snapshot.get("mechanisms") or []:
            if not isinstance(raw, dict):
                continue
            data = dict(raw)
            data["layer"] = StrategyLayer(int(data["layer"]))
            mechanisms.append(StrategyMechanism(**data))
        strategy = cls(
            mission,
            mechanisms,
            available_tools=snapshot.get("available_tools") or (),
        )
        serialized_goal_contract = snapshot.get("goal_contract")
        if isinstance(serialized_goal_contract, dict) and serialized_goal_contract:
            from osia.goal_contract import goal_contract_from_dict

            strategy.bind_goal_contract(
                goal_contract_from_dict(serialized_goal_contract)
            )
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
        strategy._retired_effect_mechanisms = {
            str(key): str(value)
            for key, value in (
                snapshot.get("retired_effect_mechanisms") or {}
            ).items()
        }
        for item in snapshot.get("completed_effects") or []:
            if isinstance(item, dict):
                strategy.remember_completed_effect(
                    str(item.get("attempted_effect") or ""),
                    target_application=str(
                        item.get("target_application") or ""
                    ),
                    target_surface=str(item.get("target_surface") or ""),
                    evidence=str(item.get("evidence") or ""),
                    observed_at=float(item.get("observed_at") or 0.0),
                )
        strategy._reconciliation_events = [
            {
                str(key): _bounded(value, 180)
                for key, value in item.items()
            }
            for item in snapshot.get("reconciliation_events") or []
            if isinstance(item, dict)
        ][-16:]
        strategy._terminal_prompt_tokens = set(snapshot.get("terminal_prompt_tokens") or [])
        strategy._frozen_node_id = str(snapshot.get("frozen_node_id") or "")
        strategy._frozen_mechanism_id = str(
            snapshot.get("frozen_mechanism_id") or ""
        )
        strategy._frozen_attempt_id = str(
            snapshot.get("frozen_attempt_id") or ""
        )
        strategy.terminal_blocker = str(snapshot.get("terminal_blocker") or "")
        strategy._substep_counters = {
            str(key): int(value)
            for key, value in (snapshot.get("substep_counters") or {}).items()
        }
        strategy._completed_substeps = [
            dict(item) for item in snapshot.get("completed_substeps") or []
            if isinstance(item, dict)
        ][-64:]
        strategy._completed_primitive_signatures = set(
            snapshot.get("completed_primitive_signatures") or []
        )
        strategy._retry_substeps = {
            str(key): dict(value)
            for key, value in (snapshot.get("retry_substeps") or {}).items()
            if isinstance(value, dict)
        }
        strategy._latest_desktop_observation = dict(
            snapshot.get("latest_desktop_observation") or {}
        )
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
        goal_contract = self.goal_contract_dict()
        if not goal_contract:
            try:
                from osia.goal_contract import goal_contract_from_semantic_mission

                goal_contract = goal_contract_from_semantic_mission(
                    self.mission
                ).to_dict()
            except (AttributeError, TypeError, ValueError):
                goal_contract = {}
        payload = {
            "goal_contract": goal_contract,
            "semantic_mission": self.mission.public_dict(),
            "available_strategy_layers": available,
            "completed_verified_effects": [
                {
                    key: fact.get(key)
                    for key in (
                        "attempted_effect", "target_application",
                        "target_surface", "observed_at",
                    )
                }
                for fact in self.completed_effect_facts()
            ],
            "completed_verified_substeps": list(self._completed_substeps[-16:]),
            "latest_desktop_observation": dict(
                self._latest_desktop_observation
            ),
            "uia_candidate_exhaustion": _uia_candidate_diagnostic(
                self, self.goal,
            ) if _semantic_domain(self.goal) == "desktop" else {},
        }
        return (
            "[Internal deterministic execution contract. Preserve every ordered "
            "action node, its application/surface/account, requested content, and "
            "observable postcondition across fallbacks. Never replay completed nodes "
            "or merge unresolved work into another node. Inspect capabilities first "
            "and use the earliest applicable "
            "layer in this order: 0 exact direct provider/API/MCP/OSIA capability; "
            "1 deep link or deterministic route; 2 DOM/Playwright; 3 native "
            "accessibility; 4 keyboard with observable focus; 5 local/zoomed vision; "
            "6 global coordinates. Do not replay a failed action "
            "without new evidence; re-resolve stale targets; reconcile any possible "
            "effect before another mutation. For a native Desktop composite, observe "
            "the requested application surface first. Keep an unrelated foreground "
            "separate from that target. If the exact target is background, activate "
            "its opaque window ref and re-observe target focus before any control or "
            "keyboard action. Then dispatch only one UIA primitive, observe, and "
            "reconcile that exact primitive before choosing the next. For a stateless "
            "Invoke control, declare its independent exact surface-text effect using "
            "the observed effect surface ref and expected post-dispatch text; control "
            "invocation itself is never proof. Repeated native literal controls in "
            "one composite must keep the same previously verified text witness; if "
            "a witness-family conflict is rejected, re-observe and use that proven "
            "current witness. A failed-before-effect UIA candidate "
            "retires only that exact identity/generation. Re-observe read-only and "
            "exhaust the fresh candidate set for the same pending substep before "
            "keyboard; a keyboard fallback may perform only that substep. Document tools "
            "cannot execute or verify a Desktop node. Vision is eligible only after "
            "structured UIA evidence proves it is necessary. Structured state: "
            + json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            + "]"
        )

    def model_feedback(self) -> str:
        payload = self.final_diagnostic()
        payload["completed_verified_substeps"] = list(
            self._completed_substeps[-16:]
        )
        return (
            "[Internal execution_strategy: "
            + json.dumps(
                payload, sort_keys=True, ensure_ascii=True,
                separators=(",", ":"),
            )
            + "]"
        )

    def premature_final_instruction(self) -> str:
        """Return one bounded correction for each unchanged strategy state."""

        with self._lock:
            if not self.requires_execution or self.succeeded or self.frozen_reason:
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
    "take_screenshot", "inspect_desktop_surfaces", "request_visual_observation",
    "is_running", "is_installed", "has_window",
    "steam_game_state", "web_state", "get_current_url", "read_page",
    "get_page_text", "get_browser_state", "inspect_browser_page", "inspect_form_fields",
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

_VISUAL_DESKTOP_TOOLS = frozenset({
    "take_screenshot", "request_visual_observation",
})

# Exact reconciliation observers are selected from the semantic effect that is
# already frozen, never from the mutating tool name.  The current tool registry
# has no machine-readable evidence-requirement field, so this narrow typed
# relation is the canonical bridge for exact Desktop application state.
_RECONCILIATION_OBSERVERS = {
    "application_open": ("inspect_desktop_surfaces",),
    "application_foreground": ("inspect_desktop_surfaces",),
    "desktop_uia_primitive": ("inspect_desktop_surfaces",),
}


def normalize_desktop_observable_effect(value: object) -> dict:
    """Return one bounded typed surface-level postcondition."""

    raw = value if isinstance(value, dict) else {}
    kind = _bounded(raw.get("kind"), 48)
    surface_ref = _bounded(raw.get("surface_ref"), 160)
    expected_text = _bounded(raw.get("expected_text"), 240)
    if (
        kind != "exact_surface_text"
        or not surface_ref
        or not expected_text
    ):
        return {}
    return {
        "kind": kind,
        "surface_ref": surface_ref,
        "expected_text": expected_text,
    }


def observable_effect_postcondition(value: object) -> str:
    """Encode the typed effect as a stable primitive postcondition."""

    effect = normalize_desktop_observable_effect(value)
    if not effect:
        return ""
    return "exact surface text on {surface} is {text}".format(
        surface=effect["surface_ref"],
        text=effect["expected_text"],
    )


def _reconciliation_observer_relation(attempt: StrategyAttempt) -> str:
    """Return an observer relation only for a state-observable frozen effect."""

    semantic_effect = str(attempt.attempted_effect or "")
    if semantic_effect in _RECONCILIATION_OBSERVERS:
        return semantic_effect
    primitive = (
        attempt.primitive_receipt
        if isinstance(attempt.primitive_receipt, dict) else {}
    )
    before = primitive.get("before")
    before = before if isinstance(before, dict) else {}
    target = before.get("target")
    target = target if isinstance(target, dict) else {}
    state_rows = before.get("state_rows")
    state_rows = state_rows if isinstance(state_rows, list) else []
    mechanism = _normalized(primitive.get("mechanism"))
    declared_effect = normalize_desktop_observable_effect(
        attempt.observable_effect,
    )
    receipt_effect = normalize_desktop_observable_effect(
        primitive.get("observable_effect"),
    )
    batch_effects = [
        effect for raw in attempt.semantic_batch_effects
        if (effect := normalize_desktop_observable_effect(raw))
    ]
    receipt_is_batch_prefix = bool(
        attempt.semantic_batch_id
        and receipt_effect
        and receipt_effect in batch_effects
    )
    dispatch_effect = (
        receipt_effect if receipt_is_batch_prefix else declared_effect
    )
    try:
        attempt_dispatched_at = float(attempt.dispatched_at or 0.0)
        primitive_dispatched_at = float(primitive.get("dispatched_at") or 0.0)
        before_observed_at = float(before.get("observed_at") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return ""
    declared_effect_observable = any(
        isinstance(item, dict)
        and item.get("surface_ref") == dispatch_effect.get("surface_ref")
        and bool(item.get("identity"))
        and _normalized(item.get("label"))
        != _normalized(dispatch_effect.get("expected_text"))
        for item in state_rows
    )
    target_state_observable = bool(
        mechanism in {"selectionitem", "toggle", "expandcollapse"}
        and target.get("runtime_signature")
    )
    if (
        primitive.get("kind") == "desktop_uia_primitive"
        and attempt.primitive_postcondition
        and semantic_effect == attempt.primitive_postcondition
        and attempt.composite_id
        and attempt.substep_id
        and attempt.substep_index > 0
        and attempt.target_application
        and _normalized(before.get("application"))
        == _normalized(attempt.target_application)
        and before.get("window_surface_ref")
        and before.get("window_generation")
        and primitive_dispatched_at > 0.0
        and primitive_dispatched_at >= attempt_dispatched_at
        and before_observed_at > 0.0
        and before_observed_at <= primitive_dispatched_at
        and (
            (
                mechanism in {
                    "invoke", "selectionitem", "toggle", "expandcollapse",
                }
                and declared_effect
                and (
                    receipt_effect == declared_effect
                    or receipt_is_batch_prefix
                )
                and declared_effect_observable
            )
            or (
                mechanism in {"selectionitem", "toggle", "expandcollapse"}
                and not declared_effect
                and target_state_observable
            )
        )
    ):
        return "desktop_uia_primitive"
    return ""


def _semantic_domain(node: SemanticActionNode) -> str:
    if (
        _normalized(node.required_application)
        in {"brave", "chrome", "firefox", "edge"}
        and node.requested_action in {
            "navigate", "search", "activate", "type", "send",
        }
    ):
        return "web"
    if node.required_application or node.required_surface:
        return "desktop"
    source = _normalized(
        " ".join((node.source_clause, node.target_object))
    )
    if re.search(
        r"\b(document|pdf|spreadsheet|workbook|worksheet|sheet|slides?|deck|"
        r"transcript|document|feuille|tableur|diapositives?)\b",
        source,
    ):
        return "document"
    if re.search(r"\b(web|website|browser|page|site|url)\b", source):
        return "web"
    return ""


def semantic_prerequisite_effects(
    node: SemanticActionNode,
) -> tuple[dict[str, str], ...]:
    """Return structured prerequisite effects distinct from the node's goal."""

    if (
        _semantic_domain(node) == "desktop"
        and node.required_application
        and node.requested_action != "open"
    ):
        return ({
            "attempted_effect": "application_open",
            "target_application": _normalized(node.required_application),
            "target_surface": "",
        },)
    return ()


def _tool_attempted_effect(tool_name: str) -> str:
    try:
        from osia.tool_contracts import get_tool_spec

        spec = get_tool_spec(tool_name)
    except Exception:
        spec = None
    return _bounded(getattr(spec, "attempted_effect", ""), 160)


def _effect_applicability(
    strategy: ExecutionStrategy,
    node: SemanticActionNode,
    attempted_effect: str,
    target_application: str,
    target_surface: str = "",
) -> tuple[bool, str]:
    """Decide whether one typed effect can advance the current semantic node."""

    effect = _bounded(attempted_effect, 160)
    application = _normalized(target_application or node.required_application)
    if effect == "application_open" and node.requested_action == "open":
        if (
            node.required_application
            and application
            and application != _normalized(node.required_application)
        ):
            return False, "attempted_effect_target_mismatch"
        if strategy.effect_is_completed(
            effect,
            target_application=application,
            target_surface=target_surface,
        ):
            return False, "goal_effect_already_completed"
        return True, "goal_effect_pending"
    prerequisite = next((
        item for item in semantic_prerequisite_effects(node)
        if item["attempted_effect"] == effect
        and (
            not item["target_application"]
            or item["target_application"] == application
        )
        and (
            not item["target_surface"]
            or item["target_surface"] == _normalized(target_surface)
        )
    ), None)
    if prerequisite is None:
        return False, "attempted_effect_not_applicable_to_semantic_node"
    if strategy.effect_is_completed(
        effect,
        target_application=application,
        target_surface=target_surface,
    ):
        return False, "prerequisite_effect_already_completed"
    return True, "prerequisite_effect_pending"


def _tool_domain(tool_name: str) -> str:
    try:
        from osia.tool_contracts import get_tool_spec

        spec = get_tool_spec(tool_name)
    except Exception:
        spec = None
    if spec is None:
        return ""
    preconditions = set(spec.preconditions)
    capability = _normalized(spec.capability)
    properties = set(
        (spec.input_schema.get("properties") or {}).keys()
        if isinstance(spec.input_schema, dict) else ()
    )
    if (
        preconditions.intersection({
            "authorized_document_source", "authorized_live_artifact",
        })
        or re.search(
            r"\b(document|pdf|spreadsheet|workbook|worksheet|slides?|deck)\b",
            capability,
        )
    ):
        return "document"
    if (
        preconditions.intersection({
            "foreground_desktop_surface_available", "fresh_desktop_surface_ref",
            "screen_capture_available", "application_resolvable",
        })
        or {"surface_ref", "objective"}.issubset(properties)
        or re.search(
            r"\b(desktop|native app|native application|screen capture)\b",
            capability,
        )
    ):
        return "desktop"
    if (
        "managed_browser" in preconditions
        or re.search(r"\b(browser|web page|website|url)\b", capability)
    ):
        return "web"
    return ""


_STATEFUL_UIA_PATTERNS = frozenset({
    "SelectionItem", "Toggle", "ExpandCollapse",
})

_CANONICAL_UIA_ACTION_PATTERNS = (
    "Invoke", "SelectionItem", "Toggle", "ExpandCollapse",
)


def _canonical_uia_action_pattern(value: object) -> str:
    normalized = _normalized(value).replace(" ", "")
    return next((
        item for item in _CANONICAL_UIA_ACTION_PATTERNS
        if _normalized(item).replace(" ", "") == normalized
    ), "")


def _normalize_desktop_uia_frontier(value: object) -> list[dict]:
    """Return bounded exact actionable UIA candidate/pattern pairs."""

    normalized = []
    for raw in (value if isinstance(value, list) else [])[:64]:
        if not isinstance(raw, dict):
            continue
        surface_ref = _bounded(raw.get("surface_ref"), 160)
        verifier = _bounded(raw.get("verifier"), 48)
        if (
            not surface_ref
            or verifier not in {
                "exact_surface_text", "exact_target_state_transition",
            }
        ):
            continue
        patterns = [
            canonical
            for item in (raw.get("patterns") or [])[:8]
            if (canonical := _canonical_uia_action_pattern(item))
        ]
        patterns = list(dict.fromkeys(patterns))
        if not patterns:
            continue
        requested_pattern = _canonical_uia_action_pattern(
            raw.get("action_pattern"),
        )
        if requested_pattern and requested_pattern not in patterns:
            continue
        try:
            structural_epoch = max(
                0, int(raw.get("structural_epoch") or 0),
            )
        except (TypeError, ValueError, OverflowError):
            structural_epoch = 0
        for action_pattern in (
            (requested_pattern,) if requested_pattern else patterns
        ):
            normalized.append({
                "surface_ref": surface_ref,
                "window_surface_ref": _bounded(
                    raw.get("window_surface_ref"), 160,
                ),
                "window_generation": _bounded(
                    raw.get("window_generation"), 160,
                ),
                "structural_epoch": structural_epoch,
                "runtime_signature": _bounded(
                    raw.get("runtime_signature"), 160,
                ),
                "automation_id": _bounded(
                    raw.get("automation_id"), 160,
                ),
                "surface_kind": _bounded(raw.get("surface_kind"), 40),
                "role": _bounded(raw.get("role"), 80),
                "label": _bounded(raw.get("label"), 240),
                "parent_surface_ref": _bounded(
                    raw.get("parent_surface_ref"), 160,
                ),
                "patterns": patterns,
                "action_pattern": action_pattern,
                "preconditions": [
                    _bounded(item, 80)
                    for item in (raw.get("preconditions") or [])[:8]
                    if _bounded(item, 80)
                ],
                "background_safe": raw.get("background_safe") is True,
                "foreground_required": raw.get("foreground_required") is True,
                "verifier": verifier,
                "observable_effect_surface_refs": [
                    _bounded(item, 160)
                    for item in (
                        raw.get("observable_effect_surface_refs") or []
                    )[:8]
                    if _bounded(item, 160) and _bounded(item, 160) != surface_ref
                ],
            })
    return normalized


def _desktop_uia_frontier_from_scene(scene: object) -> list[dict]:
    """Project exact, independently verifiable UIA actions from one scene."""

    scene = scene if isinstance(scene, dict) else {}
    surfaces = [
        item for item in (scene.get("surfaces") or [])[:128]
        if isinstance(item, dict)
    ]
    effect_surface_refs = [
        _bounded(item.get("surface_ref"), 160)
        for item in surfaces
        if _bounded(item.get("surface_ref"), 160)
        and _bounded(item.get("label"), 240)
        and "text" in _normalized(item.get("role"))
    ][:16]
    frontier = []
    for item in surfaces:
        actuation = item.get("actuation")
        actuation = actuation if isinstance(actuation, dict) else {}
        if (
            actuation.get("available") is not True
            or actuation.get("attempted_effect") != "desktop_uia_primitive"
        ):
            continue
        surface_ref = _bounded(item.get("surface_ref"), 160)
        patterns = [
            _bounded(pattern, 80)
            for pattern in (
                actuation.get("patterns") or item.get("patterns") or []
            )[:8]
            if _bounded(pattern, 80)
        ]
        pattern_set = set(patterns)
        observable_refs = [
            candidate for candidate in effect_surface_refs
            if candidate != surface_ref
        ][:8]
        if pattern_set.intersection(_STATEFUL_UIA_PATTERNS):
            verifier = "exact_target_state_transition"
        elif "Invoke" in pattern_set and observable_refs:
            verifier = "exact_surface_text"
        else:
            continue
        frontier.append({
            "surface_ref": surface_ref,
            "window_surface_ref": _bounded(
                scene.get("window_surface_ref"), 160,
            ),
            "window_generation": _bounded(
                item.get("window_generation")
                or scene.get("window_generation"), 160,
            ),
            "structural_epoch": item.get(
                "structural_epoch", scene.get("structural_epoch", 0),
            ),
            "runtime_signature": _bounded(
                item.get("runtime_signature"), 160,
            ),
            "automation_id": _bounded(item.get("automation_id"), 160),
            "surface_kind": _bounded(item.get("surface_kind"), 40),
            "role": _bounded(item.get("role"), 80),
            "label": _bounded(item.get("label"), 240),
            "parent_surface_ref": _bounded(
                item.get("parent_surface_ref"), 160,
            ),
            "patterns": patterns,
            "preconditions": list(actuation.get("preconditions") or [])[:8],
            "background_safe": actuation.get("background_safe") is True,
            "foreground_required": actuation.get("foreground_required") is True,
            "verifier": verifier,
            "observable_effect_surface_refs": observable_refs,
        })
    return _normalize_desktop_uia_frontier(frontier)


def _normalize_desktop_text_surfaces(value: object) -> list[dict]:
    """Retain bounded exact text witnesses needed by a pending substep."""

    normalized = []
    for raw in (value if isinstance(value, list) else [])[:32]:
        if not isinstance(raw, dict):
            continue
        surface_ref = _bounded(raw.get("surface_ref"), 160)
        label = _bounded(raw.get("label"), 240)
        if not surface_ref or not label:
            continue
        normalized.append({
            "surface_ref": surface_ref,
            "role": _bounded(raw.get("role"), 80),
            "label": label,
            "runtime_signature": _bounded(
                raw.get("runtime_signature"), 160,
            ),
            "window_generation": _bounded(
                raw.get("window_generation"), 160,
            ),
        })
    return normalized


def _desktop_text_surfaces_from_scene(scene: object) -> list[dict]:
    scene = scene if isinstance(scene, dict) else {}
    rows = []
    for item in (scene.get("surfaces") or [])[:128]:
        if not isinstance(item, dict):
            continue
        if "text" not in _normalized(item.get("role")):
            continue
        rows.append({
            "surface_ref": item.get("surface_ref"),
            "role": item.get("role"),
            "label": item.get("label"),
            "runtime_signature": item.get("runtime_signature"),
            "window_generation": (
                item.get("window_generation")
                or scene.get("window_generation")
            ),
        })
    return _normalize_desktop_text_surfaces(rows)


_UIA_CANDIDATE_FAILED_BEFORE_EFFECT = frozenset({
    AttemptStatus.FAILED.value,
    AttemptStatus.STAGNATED.value,
    AttemptStatus.STALE.value,
    AttemptStatus.BLOCKED.value,
})


def _pending_uia_substep(
    strategy: ExecutionStrategy,
    node_id: str = "",
) -> dict:
    """Return the latest safely unresolved exact UIA semantic substep."""

    completed_ids = {
        str(item.get("substep_id") or "")
        for item in strategy._completed_substeps
        if isinstance(item, dict)
    }
    for attempt in reversed(strategy.attempts):
        if (
            attempt.layer != int(StrategyLayer.NATIVE_ACCESSIBILITY)
            or not attempt.substep_id
            or (node_id and attempt.node_id != node_id)
            or attempt.substep_id in completed_ids
        ):
            continue
        later = next((
            candidate for candidate in reversed(strategy.attempts)
            if candidate.substep_id == attempt.substep_id
            and candidate.attempt_id != attempt.attempt_id
            and candidate.layer == int(StrategyLayer.NATIVE_ACCESSIBILITY)
            and candidate.status in {
                AttemptStatus.SELECTED.value,
                AttemptStatus.PROGRESSED.value,
                AttemptStatus.SUCCEEDED.value,
                AttemptStatus.AMBIGUOUS.value,
            }
        ), None)
        if later is not None:
            continue
        if attempt.status not in _UIA_CANDIDATE_FAILED_BEFORE_EFFECT:
            continue
        return {
            "attempt_id": attempt.attempt_id,
            "node_id": attempt.node_id,
            "composite_id": attempt.composite_id,
            "substep_id": attempt.substep_id,
            "substep_index": attempt.substep_index,
            "attempted_effect": attempt.attempted_effect,
            "primitive_postcondition": attempt.primitive_postcondition,
            "observable_effect": dict(attempt.observable_effect),
            "action_pattern": attempt.action_pattern,
            "dispatch_evidence_revision": attempt.dispatch_evidence_revision,
            "dispatched_at": attempt.dispatched_at,
        }
    return {}


def _uia_effects_match_substep(first: object, second: object) -> bool:
    left = normalize_desktop_observable_effect(first)
    right = normalize_desktop_observable_effect(second)
    if left == right and left:
        return True
    return bool(
        left.get("kind") == right.get("kind") == "exact_surface_text"
        and _bounded(left.get("expected_text"), 240)
        == _bounded(right.get("expected_text"), 240)
    )


def _observed_uia_candidate_identity(
    strategy: ExecutionStrategy,
    surface_ref: object,
    action_pattern: object = "",
) -> dict:
    candidate = _structured_uia_surface(
        strategy, surface_ref, action_pattern=action_pattern,
    )
    if not candidate:
        return {}
    observation = strategy._latest_desktop_observation
    return _normalize_desktop_surface_identity({
        "surface_ref": candidate.get("surface_ref"),
        "application": (
            observation.get("requested_application")
            or observation.get("application")
        ),
        "window_surface_ref": (
            candidate.get("window_surface_ref")
            or observation.get("window_surface_ref")
        ),
        "window_generation": (
            candidate.get("window_generation")
            or observation.get("window_generation")
        ),
        "structural_epoch": candidate.get("structural_epoch"),
        "surface_kind": candidate.get("surface_kind"),
        "role": candidate.get("role"),
        "runtime_signature": candidate.get("runtime_signature"),
        "automation_id": candidate.get("automation_id"),
        "parent_surface_ref": candidate.get("parent_surface_ref"),
        "patterns": candidate.get("patterns"),
        "observed_at": observation.get("observed_at"),
    })


def _uia_candidate_matches_attempt(
    candidate: dict,
    attempt: StrategyAttempt,
    observation: dict,
) -> bool:
    identity = (
        attempt.target_surface_identity
        if isinstance(attempt.target_surface_identity, dict) else {}
    )
    candidate_pattern = _canonical_uia_action_pattern(
        candidate.get("action_pattern"),
    )
    attempt_pattern = _canonical_uia_action_pattern(
        attempt.action_pattern,
    )
    if (
        attempt_pattern
        and candidate_pattern
        and attempt_pattern != candidate_pattern
    ):
        return False
    candidate_ref = _bounded(candidate.get("surface_ref"), 160)
    attempt_ref = _bounded(
        identity.get("surface_ref") or attempt.target_surface, 160,
    )
    candidate_generation = _bounded(
        candidate.get("window_generation")
        or observation.get("window_generation"), 160,
    )
    attempt_generation = _bounded(
        identity.get("window_generation"), 160,
    )
    candidate_signature = _bounded(
        candidate.get("runtime_signature"), 160,
    )
    attempt_signature = _bounded(
        identity.get("runtime_signature"), 160,
    )
    try:
        candidate_epoch = max(
            0, int(candidate.get("structural_epoch") or 0),
        )
        attempt_epoch = max(
            0, int(identity.get("structural_epoch") or 0),
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        candidate_generation
        and attempt_generation
        and candidate_generation != attempt_generation
    ):
        return False
    if attempt_epoch > 0 and candidate_epoch > attempt_epoch:
        return False
    if (
        candidate_signature
        and attempt_signature
        and candidate_signature == attempt_signature
    ):
        return True
    if candidate_ref != attempt_ref:
        return False
    return not (
        candidate_signature
        and attempt_signature
        and candidate_signature != attempt_signature
    )


def _retired_uia_candidates(
    strategy: ExecutionStrategy,
    pending: dict,
) -> list[StrategyAttempt]:
    return [
        attempt for attempt in strategy.attempts
        if attempt.node_id == pending.get("node_id")
        and attempt.substep_id == pending.get("substep_id")
        and attempt.target_surface
        and attempt.status in _UIA_CANDIDATE_FAILED_BEFORE_EFFECT
        and f"{attempt.node_id}|{attempt.action_signature}"
        not in strategy._retry_substeps
    ]


def _uia_candidate_is_retired(
    strategy: ExecutionStrategy,
    candidate: dict,
    pending: dict,
) -> bool:
    observation = strategy._latest_desktop_observation
    return any(
        _uia_candidate_matches_attempt(candidate, attempt, observation)
        for attempt in _retired_uia_candidates(strategy, pending)
    )


def _uia_candidate_diagnostic(
    strategy: ExecutionStrategy,
    node: SemanticActionNode,
) -> dict:
    pending = _pending_uia_substep(strategy, node.node_id)
    frontier = _normalize_desktop_uia_frontier(
        strategy._latest_desktop_observation.get("uia_actionable_frontier"),
    )
    executable = [
        item for item in frontier
        if item["background_safe"]
        or strategy._latest_desktop_observation.get("target_foreground") is True
    ]
    remaining = (
        [
            item for item in executable
            if not _uia_candidate_is_retired(strategy, item, pending)
        ]
        if pending else executable
    )
    return {
        "tier_state": _structured_uia_frontier_state(strategy, node),
        "pending_substep": dict(pending),
        "candidate_count": len(executable),
        "candidate_pattern_pair_count": len(executable),
        "retired_candidate_count": (
            len(_retired_uia_candidates(strategy, pending)) if pending else 0
        ),
        "remaining_candidate_count": len(remaining),
        "remaining_candidates": [
            {
                key: item.get(key)
                for key in (
                    "surface_ref", "window_generation", "structural_epoch",
                    "runtime_signature", "surface_kind", "role", "label",
                    "patterns", "action_pattern", "verifier",
                )
            }
            for item in remaining[:16]
        ],
    }


def _explicit_coordinate_goal(node: SemanticActionNode) -> bool:
    """Return true only when the semantic target explicitly requires pixels."""

    source = _normalized(" ".join((
        node.source_clause,
        node.target_object,
        node.requested_content,
    )))
    if not re.search(
        r"\b(coordinates?|coordonnees?|pixels?|"
        r"x\s*coordinate|y\s*coordinate)\b",
        source,
    ):
        return False
    return bool(
        re.search(r"\(\s*\d{1,6}\s*[,;]\s*\d{1,6}\s*\)", source)
        or re.search(
            r"\bx\s*[=:]?\s*\d{1,6}\b.*\by\s*[=:]?\s*\d{1,6}\b",
            source,
        )
    )


def _structured_uia_frontier_state(
    strategy: ExecutionStrategy,
    node: SemanticActionNode,
) -> str:
    """Return candidate-, substep-, and tier-aware exact UIA state."""

    observation = strategy._latest_desktop_observation
    if observation.get("uia_frontier_observed") is not True:
        return "unknown"
    if (
        observation.get("target_resolution") != "exact_target"
        or not observation.get("window_surface_ref")
        or observation.get("target_minimized") is True
    ):
        return "unavailable"
    observed_application = _normalized(
        observation.get("requested_application")
        or observation.get("application")
    )
    if (
        node.required_application
        and observed_application != _normalized(node.required_application)
    ):
        return "unavailable"
    mechanism = next((
        item
        for item in strategy._mechanisms.values()
        if item.layer == StrategyLayer.NATIVE_ACCESSIBILITY
        and item.node_id == node.node_id
        and item.attempted_effect != "application_foreground"
        and item.mechanism_id
        != "native_accessibility:preparatory_focus"
    ), None)
    if mechanism is not None and strategy._scope_retirement_reason(
        mechanism.mechanism_id, node.node_id,
    ):
        return "exhausted"
    if mechanism is not None and any(
        attempt.mechanism_id == mechanism.mechanism_id
        and attempt.status == AttemptStatus.INAPPLICABLE.value
        and not attempt.target_surface
        for attempt in strategy.attempts
    ):
        return "exhausted"
    frontier = _normalize_desktop_uia_frontier(
        observation.get("uia_actionable_frontier"),
    )
    executable = [
        item for item in frontier
        if item["background_safe"]
        or observation.get("target_foreground") is True
    ]
    if not executable:
        return "waiting_activation" if frontier else "unavailable"
    pending = _pending_uia_substep(strategy, node.node_id)
    if pending:
        try:
            observation_revision = max(
                0, int(observation.get("evidence_revision") or 0),
            )
            dispatch_revision = max(
                0, int(pending.get("dispatch_evidence_revision") or 0),
            )
            observed_at = max(0.0, float(observation.get("observed_at") or 0.0))
            dispatched_at = max(0.0, float(pending.get("dispatched_at") or 0.0))
        except (TypeError, ValueError, OverflowError):
            return "unknown"
        if (
            observed_at <= dispatched_at
            and observation_revision <= dispatch_revision
        ):
            return "refresh_required"
        if not any(
            not _uia_candidate_is_retired(strategy, item, pending)
            for item in executable
        ):
            return "exhausted"
    return "actionable"


def _structured_uia_surface(
    strategy: ExecutionStrategy,
    surface_ref: object,
    *,
    action_pattern: object = "",
) -> dict:
    exact_ref = _bounded(surface_ref, 160)
    if not exact_ref:
        return {}
    exact_pattern = _canonical_uia_action_pattern(action_pattern)
    return next((
        item for item in _normalize_desktop_uia_frontier(
            strategy._latest_desktop_observation.get(
                "uia_actionable_frontier",
            ),
        )
        if item["surface_ref"] == exact_ref
        and (
            not exact_pattern
            or item.get("action_pattern") == exact_pattern
        )
    ), {})


def _fresh_exact_keyboard_focus(
    strategy: ExecutionStrategy,
    node: SemanticActionNode,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Require one fresh, exact control-focus observation for keyboard input."""

    observation = strategy._latest_desktop_observation
    if (
        observation.get("target_resolution") != "exact_target"
        or not observation.get("window_surface_ref")
    ):
        return False, "exact_desktop_target_observation_required"
    if observation.get("target_minimized") is True:
        return False, "exact_target_activation_required"
    if observation.get("target_foreground") is not True:
        return False, "fresh_target_foreground_required"
    expected_application = _normalized(node.required_application)
    requested_application = _normalized(
        observation.get("requested_application")
    )
    observed_application = _normalized(
        observation.get("application")
    )
    foreground_application = _normalized(
        observation.get("foreground_application")
    )
    if (
        not observed_application
        or not foreground_application
        or foreground_application != observed_application
        or (
            expected_application
            and requested_application
            and requested_application != expected_application
        )
    ):
        return False, "exact_keyboard_focus_target_mismatch"
    if (
        observation.get("focus_observable") is not True
        or not _bounded(observation.get("focused_surface_ref"), 160)
    ):
        return False, "fresh_exact_keyboard_focus_required"
    try:
        observed_at = float(observation.get("observed_at") or 0.0)
        age = (time.time() if now is None else float(now)) - observed_at
    except (TypeError, ValueError, OverflowError):
        observed_at = 0.0
        age = float("inf")
    if not observed_at or not -5.0 <= age <= 30.0:
        return False, "stale_keyboard_focus_observation"
    return True, ""


def _structured_keyboard_frontier_state(
    strategy: ExecutionStrategy,
    node: SemanticActionNode,
) -> str:
    """Return blocked/actionable/waiting_focus/exhausted for keyboard input."""

    if _structured_uia_frontier_state(strategy, node) in {
        "actionable", "refresh_required", "waiting_activation", "unknown",
    }:
        return "blocked_by_uia"
    mechanism = strategy.mechanism_for_layer(
        StrategyLayer.KEYBOARD,
        node_id=node.node_id,
    )
    if mechanism is not None and (
        strategy._exhausted(mechanism.mechanism_id)
        or strategy._scope_retirement_reason(
            mechanism.mechanism_id, node.node_id,
        )
    ):
        return "exhausted"
    focused, _reason = _fresh_exact_keyboard_focus(strategy, node)
    return "actionable" if focused else "waiting_focus"


def _uia_action_preserves_pending_substep(
    strategy: ExecutionStrategy,
    node: SemanticActionNode,
    arguments: dict,
) -> tuple[bool, str]:
    pending = _pending_uia_substep(strategy, node.node_id)
    if not pending:
        return True, ""
    if not _bounded(arguments.get("surface_ref"), 160):
        return True, ""
    candidate = _structured_uia_surface(
        strategy,
        arguments.get("surface_ref"),
        action_pattern=arguments.get("action_pattern"),
    )
    if not candidate:
        return False, "fresh_exact_uia_candidate_required"
    if _uia_candidate_is_retired(strategy, candidate, pending):
        return False, "uia_candidate_retired_before_effect"
    pending_effect = pending.get("observable_effect")
    if pending_effect and not _uia_effects_match_substep(
        pending_effect, arguments.get("observable_effect"),
    ):
        return False, "uia_action_skips_pending_substep"
    return True, ""


def _single_character_text_delta(before: object, after: object) -> str:
    """Return one exact changed/appended character, otherwise empty."""

    left = _bounded(before, 240)
    right = _bounded(after, 240)
    if not left or not right or left == right:
        return ""
    if right.startswith(left):
        delta = right[len(left):].strip()
    else:
        prefix = 0
        maximum = min(len(left), len(right))
        while prefix < maximum and left[prefix] == right[prefix]:
            prefix += 1
        delta = right[prefix:].strip()
    return delta if len(delta) == 1 else ""


def _single_digit_automation_family(value: object) -> tuple[str, str]:
    """Return one native literal-control family and its exact digit."""

    automation_id = _normalized(value).replace(" ", "")
    digits = list(re.finditer(r"\d", automation_id))
    if len(digits) != 1:
        return "", ""
    match = digits[0]
    return (
        automation_id[:match.start()] + "#" + automation_id[match.end():],
        match.group(0),
    )


def _effect_witness_identity(
    effect: object,
    rows: object,
) -> tuple[str, str]:
    """Return one exact witness identity and its currently observed text."""

    normalized = normalize_desktop_observable_effect(effect)
    surface_ref = normalized.get("surface_ref")
    if not surface_ref:
        return "", ""
    for item in rows if isinstance(rows, list) else []:
        if (
            isinstance(item, dict)
            and _bounded(item.get("surface_ref"), 160) == surface_ref
        ):
            return (
                _bounded(
                    item.get("runtime_signature") or item.get("identity"),
                    160,
                ),
                _bounded(item.get("label"), 240),
            )
    return "", ""


def _uia_literal_family_preserves_verified_witness(
    strategy: ExecutionStrategy,
    node: SemanticActionNode,
    candidate: dict,
    arguments: dict,
) -> tuple[bool, str]:
    """Keep repeated native literal controls on their proven text witness."""

    family, literal = _single_digit_automation_family(
        candidate.get("automation_id"),
    )
    effect = normalize_desktop_observable_effect(
        arguments.get("observable_effect"),
    )
    current_identity, current_text = _effect_witness_identity(
        effect,
        _normalize_desktop_text_surfaces(
            strategy._latest_desktop_observation.get("uia_text_surfaces"),
        ),
    )
    if (
        not family
        or not current_identity
        or _single_character_text_delta(
            current_text,
            effect.get("expected_text"),
        ) != literal
    ):
        return True, ""
    for attempt in strategy.attempts:
        if (
            attempt.node_id != node.node_id
            or attempt.layer != int(StrategyLayer.NATIVE_ACCESSIBILITY)
            or attempt.status not in {
                AttemptStatus.PROGRESSED.value,
                AttemptStatus.SUCCEEDED.value,
            }
        ):
            continue
        prior_automation_id = (
            attempt.target_surface_identity.get("automation_id")
            if isinstance(attempt.target_surface_identity, dict) else ""
        ) or (
            attempt.actionability_evidence.get("automation_id")
            if isinstance(attempt.actionability_evidence, dict) else ""
        )
        prior_family, prior_literal = _single_digit_automation_family(
            prior_automation_id,
        )
        if prior_family != family:
            continue
        receipt = (
            attempt.primitive_receipt
            if isinstance(attempt.primitive_receipt, dict) else {}
        )
        before = receipt.get("before")
        before = before if isinstance(before, dict) else {}
        prior_effect = normalize_desktop_observable_effect(
            attempt.observable_effect,
        )
        prior_identity, prior_text = _effect_witness_identity(
            prior_effect,
            before.get("state_rows"),
        )
        if (
            prior_identity
            and _single_character_text_delta(
                prior_text,
                prior_effect.get("expected_text"),
            ) == prior_literal
            and prior_identity != current_identity
        ):
            return False, "uia_literal_family_witness_conflict"
    return True, ""


def _uia_candidate_preserves_verified_text_delta(
    strategy: ExecutionStrategy,
    node: SemanticActionNode,
    candidate: dict,
    arguments: dict,
) -> tuple[bool, str]:
    """Keep a verified one-character UIA control bound to that character."""

    effect = normalize_desktop_observable_effect(
        arguments.get("observable_effect"),
    )
    if effect.get("kind") != "exact_surface_text":
        return True, ""
    current_text = next((
        _bounded(item.get("label"), 240)
        for item in _normalize_desktop_text_surfaces(
            strategy._latest_desktop_observation.get("uia_text_surfaces"),
        )
        if item.get("surface_ref") == effect.get("surface_ref")
    ), "")
    expected_text = _bounded(effect.get("expected_text"), 240)
    if not current_text or not expected_text.startswith(current_text):
        return True, ""
    current_delta = _single_character_text_delta(
        current_text, expected_text,
    )
    if not current_delta:
        return True, ""
    observation = strategy._latest_desktop_observation
    for attempt in strategy.attempts:
        if (
            attempt.node_id != node.node_id
            or attempt.layer != int(StrategyLayer.NATIVE_ACCESSIBILITY)
            or attempt.status not in {
                AttemptStatus.PROGRESSED.value,
                AttemptStatus.SUCCEEDED.value,
            }
            or not _uia_candidate_matches_attempt(
                candidate, attempt, observation,
            )
        ):
            continue
        prior_effect = normalize_desktop_observable_effect(
            attempt.observable_effect,
        )
        if prior_effect.get("kind") != "exact_surface_text":
            continue
        receipt = (
            attempt.primitive_receipt
            if isinstance(attempt.primitive_receipt, dict) else {}
        )
        before = receipt.get("before")
        before = before if isinstance(before, dict) else {}
        prior_text = next((
            _bounded(item.get("label"), 240)
            for item in (before.get("state_rows") or [])[:32]
            if isinstance(item, dict)
            and _bounded(item.get("surface_ref"), 160)
            == prior_effect.get("surface_ref")
        ), "")
        prior_delta = _single_character_text_delta(
            prior_text, prior_effect.get("expected_text"),
        )
        if prior_delta and prior_delta != current_delta:
            return False, "uia_candidate_conflicts_with_verified_text_delta"
    return True, ""


def _longest_suffix_prefix(left: str, right: str) -> str:
    maximum = min(len(left), len(right))
    for size in range(maximum, 0, -1):
        if left[-size:] == right[:size]:
            return right[size:].strip()
    return ""


def _pending_uia_expected_delta(
    strategy: ExecutionStrategy,
    pending: dict,
) -> str:
    effect = normalize_desktop_observable_effect(
        pending.get("observable_effect"),
    )
    if effect.get("kind") != "exact_surface_text":
        return ""
    expected = _bounded(effect.get("expected_text"), 240)
    surface_ref = _bounded(effect.get("surface_ref"), 160)
    current = next((
        _bounded(item.get("label"), 240)
        for item in _normalize_desktop_text_surfaces(
            strategy._latest_desktop_observation.get("uia_text_surfaces"),
        )
        if item.get("surface_ref") == surface_ref
    ), "")
    if current and expected.startswith(current):
        return expected[len(current):].strip()
    candidates = []
    for attempt in strategy.attempts:
        if (
            attempt.node_id != pending.get("node_id")
            or attempt.substep_index >= int(pending.get("substep_index") or 0)
            or attempt.status not in {
                AttemptStatus.PROGRESSED.value,
                AttemptStatus.SUCCEEDED.value,
            }
        ):
            continue
        prior = normalize_desktop_observable_effect(
            attempt.observable_effect,
        )
        if prior.get("kind") == "exact_surface_text":
            candidates.append(_bounded(prior.get("expected_text"), 240))
    deltas = [
        _longest_suffix_prefix(prior, expected)
        for prior in candidates
        if prior
    ]
    deltas = [item for item in deltas if item]
    return min(deltas, key=len) if deltas else ""


def _keyboard_action_preserves_pending_substep(
    strategy: ExecutionStrategy,
    node: SemanticActionNode,
    tool_name: str,
    arguments: dict,
) -> tuple[bool, str]:
    pending = _pending_uia_substep(strategy, node.node_id)
    if not pending:
        return True, ""
    if any(
        attempt.layer == int(StrategyLayer.KEYBOARD)
        and attempt.substep_id == pending.get("substep_id")
        and attempt.status in {
            AttemptStatus.SELECTED.value,
            AttemptStatus.PROGRESSED.value,
            AttemptStatus.AMBIGUOUS.value,
        }
        for attempt in strategy.attempts
    ):
        return False, "keyboard_substep_observation_required"
    if tool_name == "desktop_press":
        if "key" not in arguments:
            return True, ""
        key = _bounded(arguments.get("key"), 80)
        if re.search(
            r"(^|\+)(ctrl|control|alt|shift|cmd|command|meta|win|windows)(\+|$)",
            _normalized(key),
        ):
            return False, "keyboard_action_skips_pending_substep"
        return (
            (True, "")
            if key
            else (False, "keyboard_pending_substep_input_required")
        )
    if "text" not in arguments:
        return True, ""
    text = str(arguments.get("text") or "")
    if not text:
        return False, "keyboard_pending_substep_input_required"
    delta = _pending_uia_expected_delta(strategy, pending)
    if delta:
        if text == delta or (len(text) == 1 and len(delta) == 1):
            return True, ""
        return False, "keyboard_action_skips_pending_substep"
    if len(text) == 1:
        return True, ""
    return False, "keyboard_action_skips_pending_substep"


def _reconcile_observed_keyboard_substep(
    strategy: ExecutionStrategy,
    scene: dict,
) -> bool:
    """Commit one keyboard fallback only from its exact post-dispatch witness."""

    pending = _pending_uia_substep(strategy, strategy.goal.node_id)
    if not pending:
        return False
    attempt = next((
        item for item in reversed(strategy.attempts)
        if item.layer == int(StrategyLayer.KEYBOARD)
        and item.substep_id == pending.get("substep_id")
        and item.status == AttemptStatus.PROGRESSED.value
    ), None)
    if attempt is None or attempt.dispatched_at <= 0.0:
        return False
    effect = normalize_desktop_observable_effect(attempt.observable_effect)
    if not effect:
        return False
    try:
        observed_at = float(scene.get("observed_at") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        observed_at <= attempt.dispatched_at
        or scene.get("target_resolution") != "exact_target"
        or not scene.get("window_surface_ref")
        or _normalized(
            scene.get("requested_application") or scene.get("application")
        ) != _normalized(attempt.target_application)
    ):
        return False
    observed_text = next((
        _bounded(item.get("label"), 240)
        for item in (scene.get("surfaces") or [])[:128]
        if isinstance(item, dict)
        and _bounded(item.get("surface_ref"), 160) == effect["surface_ref"]
        and "text" in _normalized(item.get("role"))
    ), "")
    if observed_text != _bounded(effect.get("expected_text"), 240):
        return False
    evidence = "exact_keyboard_substep_witness:" + hashlib.sha256(
        json.dumps(
            {
                "substep_id": attempt.substep_id,
                "surface_ref": effect["surface_ref"],
                "expected_text": effect["expected_text"],
                "observed_at": observed_at,
            },
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()[:20]
    attempt.postcondition_evidence = evidence
    attempt.observations_gained.append(evidence)
    strategy._completed_primitive_signatures.add(
        f"{attempt.node_id}|{attempt.action_signature}"
    )
    strategy._completed_substeps.append({
        "composite_id": attempt.composite_id,
        "substep_id": attempt.substep_id,
        "substep_index": attempt.substep_index,
        "attempt_id": attempt.attempt_id,
        "action_signature": attempt.action_signature,
        "attempted_effect": attempt.attempted_effect,
        "evidence": evidence,
    })
    strategy._completed_substeps = strategy._completed_substeps[-64:]
    node = strategy.node_by_id(attempt.node_id)
    if node is not None:
        node.status = "pending"
        node.evidence_links = tuple((*node.evidence_links, evidence)[-16:])
    return True


def _nodes_provably_independent(
    first: SemanticActionNode | None,
    second: SemanticActionNode | None,
) -> bool:
    """Return only an explicit, disjoint application-level independence."""

    if first is None or second is None or first.node_id == second.node_id:
        return False
    first_application = _canonical_application(first.required_application)
    second_application = _canonical_application(second.required_application)
    return bool(
        first_application
        and second_application
        and first_application != second_application
    )


def _tool_is_structurally_applicable(
    strategy: ExecutionStrategy,
    tool_name: str,
    arguments: dict,
) -> tuple[bool, str]:
    matched_node = strategy.match_action_node(tool_name, arguments)
    if strategy.frozen_reason and tool_name not in _OBSERVATION_TOOLS:
        frozen_surface = _bounded(arguments.get("surface_ref"), 160)
        frozen_attempt = strategy.attempt_by_id(
            strategy._frozen_attempt_id
        )
        frozen_window = _bounded(
            strategy._latest_desktop_observation.get("window_surface_ref")
            or (
                frozen_attempt.target_surface
                if frozen_attempt is not None else ""
            ),
            160,
        )
        preparatory_bypass = bool(
            strategy.preparatory_effect_is_frozen()
            and tool_name == "activate_desktop_surface"
            and frozen_surface
            and frozen_surface != frozen_window
        )
        frozen_node = strategy.node_by_id(strategy._frozen_node_id)
        independent_branch = _nodes_provably_independent(
            frozen_node, matched_node,
        )
        if not preparatory_bypass and not independent_branch:
            return False, "ambiguous_effect_requires_reconciliation"
    if matched_node is not None:
        pending_nodes = [
            item for item in strategy.pending_nodes() if item.requires_execution
        ]
        if (
            pending_nodes
            and matched_node.order != pending_nodes[0].order
            and not _nodes_provably_independent(
                pending_nodes[0], matched_node,
            )
        ):
            return False, "earlier_action_node_pending"
    node = matched_node or strategy.goal
    goal_domain = _semantic_domain(node)
    attempted_effect = _tool_attempted_effect(tool_name)
    if attempted_effect:
        target_application = strategy._tool_application(tool_name, arguments)
        if (
            matched_node is None
            and attempted_effect == "application_open"
            and node.required_application
            and target_application
            and target_application != _normalized(node.required_application)
        ):
            return False, "required_application_changed"
        applicable, reason = _effect_applicability(
            strategy,
            node,
            attempted_effect,
            target_application,
        )
        if not applicable:
            return False, reason
    if tool_name == "run_command":
        command = _normalized(arguments.get("command"))
        explicit_command_goal = bool(re.search(
            r"\b(command|command line|shell|terminal|powershell|bash|cmd)\b",
            _normalized(" ".join((
                strategy.mission.objective,
                node.source_clause,
                node.target_object,
            ))),
        ))
        if not command and not (
            explicit_command_goal and goal_domain not in {"document", "web"}
        ):
            return False, "generic_command_semantic_applicability_unknown"
        if goal_domain == "desktop" and not (
            strategy._tool_action(tool_name, arguments)
            == node.requested_action
            and strategy._tool_application(tool_name, arguments)
            == _normalized(node.required_application)
        ):
            # Preserve the stronger anti-replay/in-flight decision for an
            # already-bound direct effect.  A first generic command still
            # fails closed before it can create a mutating attempt.
            if not (
                strategy._scope_retirement_reason(
                    "direct:run_command", node.node_id,
                )
                or strategy._scope_is_in_flight(
                    "direct:run_command", node.node_id,
                )
            ):
                return False, (
                    "mechanism_not_applicable"
                    if node.requested_action == "calculate"
                    else "generic_command_not_applicable_to_desktop_node"
                )
        if goal_domain in {"document", "web"}:
            return False, "tool_capability_not_applicable_to_semantic_node"
        if (
            not goal_domain
            and strategy.available_tools
            and not explicit_command_goal
        ):
            return False, "generic_command_semantic_applicability_unknown"
        return True, ""
    if (
        tool_name in _DOM_ACTION_TOOLS
        and goal_domain not in {"", "web"}
        and (goal_domain == "document" or not arguments)
    ):
        return False, "tool_capability_not_applicable_to_semantic_node"
    if tool_name == "open_uri" and node.requested_action not in {
        "open", "navigate", "search",
    }:
        return False, "route_effect_not_applicable_to_semantic_node"
    if (
        tool_name == "activate_desktop_surface"
        and bool(_bounded(arguments.get("surface_ref"), 160))
        and _normalized(arguments.get("surface_ref")) == _normalized(
            strategy._latest_desktop_observation.get("window_surface_ref")
        )
        and strategy.effect_is_completed(
            "application_foreground",
            target_application=node.required_application,
            target_surface=str(arguments.get("surface_ref") or ""),
        )
    ):
        return False, "prerequisite_effect_already_completed"
    if tool_name == "activate_desktop_surface":
        resume_effects: list[dict] = []
        resume_from_index: int | None = None
        for prior_index in range(len(strategy.attempts) - 1, -1, -1):
            prior_attempt = strategy.attempts[prior_index]
            if (
                prior_attempt.node_id != node.node_id
                or prior_attempt.status != AttemptStatus.PROGRESSED.value
                or prior_attempt.semantic_batch_resume_from_index is None
                or not prior_attempt.semantic_batch_effects
            ):
                continue
            resume_effects = [
                effect for raw in prior_attempt.semantic_batch_effects
                if (effect := normalize_desktop_observable_effect(raw))
            ]
            resume_from_index = max(
                0, int(prior_attempt.semantic_batch_resume_from_index),
            )
            for later_attempt in strategy.attempts[prior_index + 1:]:
                if (
                    later_attempt.node_id != node.node_id
                    or later_attempt.status not in {
                        AttemptStatus.PROGRESSED.value,
                        AttemptStatus.SUCCEEDED.value,
                    }
                ):
                    continue
                later_effects = [
                    effect for raw in later_attempt.semantic_batch_effects
                    if (effect := normalize_desktop_observable_effect(raw))
                ] or [normalize_desktop_observable_effect(
                    later_attempt.observable_effect
                )]
                later_effects = [effect for effect in later_effects if effect]
                if later_effects == resume_effects[
                    resume_from_index:resume_from_index + len(later_effects)
                ]:
                    resume_from_index += len(later_effects)
            break
        if resume_from_index is not None and resume_from_index >= len(
            resume_effects
        ):
            return False, "semantic_batch_resume_already_completed"
        semantic_batch = arguments.get("semantic_batch")
        if semantic_batch is not None:
            try:
                from osia.semantic_batch import normalize_semantic_batch

                batch_plan, batch_reason = normalize_semantic_batch(
                    surface_ref=arguments.get("surface_ref"),
                    objective=arguments.get("objective"),
                    action_pattern=arguments.get("action_pattern"),
                    observable_effect=arguments.get("observable_effect"),
                    semantic_batch=semantic_batch,
                )
            except Exception:
                batch_plan, batch_reason = (
                    {}, "semantic_batch_preflight_unavailable",
                )
            if not batch_plan:
                return False, batch_reason
            batch_effects = [
                dict(item.get("observable_effect") or {})
                for item in batch_plan.get("items") or []
            ]
            if resume_from_index is not None and batch_effects != resume_effects[
                resume_from_index:
            ]:
                return False, "semantic_batch_resume_frontier_mismatch"
            if resume_from_index is None and not _semantic_batch_proves_calculation_result(
                node, batch_plan,
            ):
                return False, "semantic_batch_incomplete_semantic_goal"
            expected_window = _bounded(
                strategy._latest_desktop_observation.get(
                    "window_surface_ref"
                ), 160,
            )
            expected_generation = _bounded(
                strategy._latest_desktop_observation.get(
                    "window_generation"
                ), 160,
            )
            expected_epoch = None
            current_text_by_ref = {
                item["surface_ref"]: item["label"]
                for item in _normalize_desktop_text_surfaces(
                    strategy._latest_desktop_observation.get(
                        "uia_text_surfaces"
                    )
                )
            }
            for item in batch_plan["items"]:
                candidate = _structured_uia_surface(
                    strategy,
                    item["surface_ref"],
                    action_pattern=item["action_pattern"],
                )
                if not candidate:
                    return False, "semantic_batch_stale_primitive"
                if _normalized(candidate.get("label")) != _normalized(
                    item["objective"]
                ):
                    return False, "semantic_batch_target_identity_invalid"
                if (
                    _bounded(candidate.get("window_surface_ref"), 160)
                    != expected_window
                    or _bounded(candidate.get("window_generation"), 160)
                    != expected_generation
                ):
                    return False, "semantic_batch_target_identity_invalid"
                try:
                    candidate_epoch = int(
                        candidate.get("structural_epoch") or 0
                    )
                except (TypeError, ValueError, OverflowError):
                    return False, "semantic_batch_target_identity_invalid"
                if expected_epoch is None:
                    expected_epoch = candidate_epoch
                elif candidate_epoch != expected_epoch:
                    return False, "semantic_batch_structural_transition"
                effect_ref = item["observable_effect"]["surface_ref"]
                if (
                    item["action_pattern"] == "Invoke"
                    and effect_ref not in candidate.get(
                        "observable_effect_surface_refs", []
                    )
                ):
                    return False, "semantic_batch_final_checkpoint_unobservable"
                if _normalized(current_text_by_ref.get(effect_ref)) == _normalized(
                    item["observable_effect"]["expected_text"]
                ):
                    return False, "semantic_batch_effect_already_observed"
                if (
                    candidate.get("foreground_required") is True
                    and candidate.get("background_safe") is not True
                    and strategy._latest_desktop_observation.get(
                        "target_foreground"
                    ) is not True
                ):
                    return False, "semantic_batch_unresolved_focus_requirement"
        elif resume_from_index is not None and normalize_desktop_observable_effect(
            arguments.get("observable_effect")
        ) != resume_effects[resume_from_index]:
            return False, "semantic_batch_resume_frontier_mismatch"
        surface_ref = _bounded(arguments.get("surface_ref"), 160)
        window_ref = _bounded(
            strategy._latest_desktop_observation.get("window_surface_ref"),
            160,
        )
        requested_pattern = _canonical_uia_action_pattern(
            arguments.get("action_pattern"),
        )
        window_pattern = (
            surface_ref == window_ref
            and _normalized(arguments.get("action_pattern")) in {"", "window"}
        )
        if (
            window_pattern
            and strategy._latest_desktop_observation.get(
                "target_foreground"
            ) is True
        ):
            return False, "prerequisite_effect_already_completed"
        if (
            arguments.get("action_pattern")
            and not requested_pattern
            and not window_pattern
        ):
            return False, "uia_action_pattern_unsupported"
        surface = _structured_uia_surface(
            strategy,
            surface_ref,
            action_pattern=requested_pattern,
        )
        if (
            surface_ref
            and surface_ref != window_ref
            and strategy._latest_desktop_observation.get(
                "uia_frontier_observed"
            ) is True
        ):
            if not surface and requested_pattern:
                return False, (
                    "uia_action_pattern_unavailable"
                )
            if surface:
                selected_pattern = _canonical_uia_action_pattern(
                    surface.get("action_pattern"),
                )
                if not selected_pattern:
                    return False, "uia_action_pattern_unavailable"
                # Bind legacy/provider-omitted calls to the deterministic exact
                # pair from the fresh structured frontier before signature.
                arguments["action_pattern"] = selected_pattern
                if (
                    selected_pattern == "Invoke"
                    and not normalize_desktop_observable_effect(
                        arguments.get("observable_effect"),
                    )
                ):
                    return False, "uia_invoke_observable_effect_required"
        preserves_substep, substep_reason = (
            _uia_action_preserves_pending_substep(
                strategy, node, arguments,
            )
        )
        if not preserves_substep:
            return False, substep_reason
        preserves_delta, delta_reason = (
            _uia_candidate_preserves_verified_text_delta(
                strategy, node, surface, arguments,
            )
        )
        if not preserves_delta:
            return False, delta_reason
        preserves_witness, witness_reason = (
            _uia_literal_family_preserves_verified_witness(
                strategy, node, surface, arguments,
            )
        )
        if not preserves_witness:
            return False, witness_reason
        if (
            surface.get("foreground_required") is True
            and surface.get("background_safe") is not True
            and strategy._latest_desktop_observation.get(
                "target_foreground"
            ) is not True
        ):
            return False, "exact_target_activation_required"
        frontier_state = _structured_uia_frontier_state(strategy, node)
        focused, _focus_reason = _fresh_exact_keyboard_focus(
            strategy, node,
        )
        if (
            frontier_state in {"unavailable", "exhausted"}
            and focused
        ):
            return False, "uia_frontier_exhausted_keyboard_ready"
    if tool_name in {
        "desktop_click", "desktop_press", "desktop_type", "desktop_scroll",
    }:
        observation = strategy._latest_desktop_observation
        if (
            observation.get("target_resolution") != "exact_target"
            or not observation.get("window_surface_ref")
        ):
            return False, "exact_desktop_target_observation_required"
        if observation.get("target_minimized") is True:
            return False, "exact_target_activation_required"
        if observation.get("target_foreground") is not True:
            return False, (
                "background_safe_uia_required"
                if tool_name == "desktop_click" else
                "fresh_target_foreground_required"
            )
        if (
            tool_name == "desktop_click"
            and not _explicit_coordinate_goal(node)
            and _structured_uia_frontier_state(strategy, node) == "actionable"
        ):
            return False, "stronger_exact_uia_frontier_available"
        if tool_name in {"desktop_press", "desktop_type"}:
            frontier_state = _structured_uia_frontier_state(strategy, node)
            if frontier_state == "actionable":
                return False, "stronger_exact_uia_frontier_available"
            if frontier_state in {"unknown", "refresh_required"}:
                return False, "uia_exhaustion_evidence_required"
            if _structured_keyboard_frontier_state(
                strategy, node,
            ) == "exhausted":
                return False, "keyboard_frontier_exhausted"
            focused, focus_reason = _fresh_exact_keyboard_focus(
                strategy, node,
            )
            if not focused:
                return False, focus_reason
            preserves_substep, substep_reason = (
                _keyboard_action_preserves_pending_substep(
                    strategy, node, tool_name, arguments,
                )
            )
            if not preserves_substep:
                return False, substep_reason
        if (
            tool_name == "desktop_click"
            and not _explicit_coordinate_goal(node)
            and _structured_keyboard_frontier_state(strategy, node)
            != "exhausted"
        ):
            return False, "stronger_exact_keyboard_frontier_available"
    if tool_name in {
        "open_application", "open_uri", "desktop_click",
        "activate_desktop_surface", "desktop_press", "desktop_type",
        "desktop_scroll",
    } or tool_name in _DOM_ACTION_TOOLS:
        return True, ""
    tool_domain = _tool_domain(tool_name)
    if goal_domain and tool_domain != goal_domain:
        return False, "tool_capability_not_applicable_to_semantic_node"
    if goal_domain == "desktop" and tool_name in _VISUAL_DESKTOP_TOOLS:
        observation = strategy._latest_desktop_observation
        quality = str(observation.get("accessibility_quality") or "")
        frontier_state = _structured_uia_frontier_state(strategy, node)
        if not quality:
            return False, "uia_observation_required_before_vision"
        if str(observation.get("target_resolution") or "") != "exact_target":
            return False, "exact_target_surface_required_before_vision"
        if observation.get("target_foreground") is not True:
            return False, "target_focus_verification_required_before_vision"
        if (
            frontier_state in {"actionable", "refresh_required"}
            or (
                frontier_state == "unknown"
                and quality == "rich_target_resolved"
            )
        ):
            return False, "structured_uia_evidence_sufficient"
        if _structured_keyboard_frontier_state(strategy, node) != "exhausted":
            return False, "keyboard_frontier_not_exhausted"
    return True, ""


def tool_applicability(
    strategy: ExecutionStrategy,
    tool_name: str,
    arguments: dict | None = None,
) -> tuple[bool, str]:
    """Public, side-effect-free applicability check for provider frontiers."""

    if not isinstance(strategy, ExecutionStrategy):
        return False, "execution_strategy_required"
    return _tool_is_structurally_applicable(
        strategy,
        str(tool_name or ""),
        arguments if isinstance(arguments, dict) else {},
    )


_MEMORY_ATTEMPT_STATUSES = frozenset(item.value for item in AttemptStatus)


def _memory_evidence_token(value: object) -> str:
    """Return an opaque bounded proof token, never checkpoint raw evidence."""

    bounded = _bounded(value, 2000)
    if not bounded:
        return ""
    return "evidence_sha256:" + hashlib.sha256(
        bounded.encode("utf-8", errors="replace")
    ).hexdigest()[:24]


def semantic_effect_key(node: SemanticActionNode) -> str:
    """Stable key for rebinding one equivalent action across a replan."""

    source = _normalized(" ".join((
        node.source_clause,
        node.target_object,
        node.observable_success_postcondition,
        node.requested_content,
    )))
    numeric_facts = sorted(set(
        re.findall(r"\d+(?:[.,]\d+)?", source)
    ))[:12]
    action = node.requested_action
    if action in {"type", "calculate"} and len(numeric_facts) >= 2:
        action = "calculate"
    payload = {
        "domain": _semantic_domain(node),
        "application": _normalized(node.required_application),
        "surface": _normalized(node.required_surface),
        "account": _normalized(node.required_account),
        "action": action,
        "numeric_facts": numeric_facts,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:24]


def _normalize_desktop_surface_identity(value: object) -> dict:
    """Bound one checkpoint-safe structured Windows surface identity."""

    raw = value if isinstance(value, dict) else {}
    try:
        structural_epoch = max(0, int(raw.get("structural_epoch") or 0))
    except (TypeError, ValueError, OverflowError):
        structural_epoch = 0
    try:
        observed_at = max(0.0, float(raw.get("observed_at") or 0.0))
    except (TypeError, ValueError, OverflowError):
        observed_at = 0.0
    try:
        dispatched_at = max(0.0, float(raw.get("dispatched_at") or 0.0))
    except (TypeError, ValueError, OverflowError):
        dispatched_at = 0.0
    identity = {
        "schema_version": 1,
        "surface_ref": _bounded(raw.get("surface_ref"), 160),
        "application": _bounded(raw.get("application"), 160),
        "process_ref": _bounded(raw.get("process_ref"), 160),
        "window_surface_ref": _bounded(
            raw.get("window_surface_ref"), 160,
        ),
        # HWND is chronology/correlation evidence only.  It is never used to
        # dispatch after reconstruction; re-resolution remains mandatory.
        "native_window_id": _bounded(raw.get("native_window_id"), 64),
        "window_generation": _bounded(
            raw.get("window_generation"), 160,
        ),
        "structural_epoch": structural_epoch,
        "surface_kind": _bounded(raw.get("surface_kind"), 40),
        "role": _bounded(raw.get("role"), 80),
        "runtime_signature": _bounded(
            raw.get("runtime_signature"), 160,
        ),
        "parent_surface_ref": _bounded(
            raw.get("parent_surface_ref"), 160,
        ),
        "patterns": [
            _bounded(item, 80)
            for item in (raw.get("patterns") or [])[:16]
            if _bounded(item, 80)
        ],
        "observed_at": observed_at,
        "dispatched_at": dispatched_at,
    }
    automation_id = _bounded(raw.get("automation_id"), 160)
    if automation_id:
        identity["automation_id"] = automation_id
    if not identity["surface_ref"]:
        return {}
    return identity


def _normalize_primitive_receipt(value: object) -> dict:
    """Retain only bounded identity/state needed for exact reconciliation."""

    raw = value if isinstance(value, dict) else {}
    before = raw.get("before")
    before = before if isinstance(before, dict) else {}
    target = before.get("target")
    target = target if isinstance(target, dict) else {}
    try:
        dispatched_at = max(0.0, float(raw.get("dispatched_at") or 0.0))
        observed_at = max(0.0, float(before.get("observed_at") or 0.0))
        structural_epoch = max(0, int(before.get("structural_epoch") or 0))
    except (TypeError, ValueError, OverflowError):
        return {}
    state_rows = []
    for item in (before.get("state_rows") or [])[:32]:
        if not isinstance(item, dict):
            continue
        identity = _bounded(item.get("identity"), 160)
        role = _bounded(item.get("role"), 80)
        if not identity or not role:
            continue
        state_rows.append({
            "surface_ref": _bounded(item.get("surface_ref"), 160),
            "identity": identity,
            "role": role,
            "label": _bounded(item.get("label"), 240),
            "selected": item.get("selected") is True,
            "focused": item.get("focused") is True,
        })
    normalized_target = {
        "surface_ref": _bounded(target.get("surface_ref"), 160),
        "application": _bounded(target.get("application"), 160),
        "process_ref": _bounded(target.get("process_ref"), 160),
        "window_generation": _bounded(
            target.get("window_generation"), 160,
        ),
        "surface_kind": _bounded(target.get("surface_kind"), 40),
        "runtime_signature": _bounded(
            target.get("runtime_signature"), 160,
        ),
        "role": _bounded(target.get("role"), 80),
        "label": _bounded(target.get("label"), 240),
        "parent_surface_ref": _bounded(
            target.get("parent_surface_ref"), 160,
        ),
        "selected": target.get("selected") is True,
        "focused": target.get("focused") is True,
        "enabled": target.get("enabled") is not False,
        "patterns": [
            _bounded(item, 80)
            for item in (target.get("patterns") or [])[:16]
            if _bounded(item, 80)
        ],
    }
    target_automation_id = _bounded(target.get("automation_id"), 160)
    if target_automation_id:
        normalized_target["automation_id"] = target_automation_id
    normalized = {
        "kind": _bounded(raw.get("kind"), 80),
        "mechanism": _bounded(raw.get("mechanism"), 80),
        "observable_effect": normalize_desktop_observable_effect(
            raw.get("observable_effect"),
        ),
        "dispatched_at": dispatched_at,
        "before": {
            "application": _bounded(before.get("application"), 160),
            "window_surface_ref": _bounded(
                before.get("window_surface_ref"), 160,
            ),
            "window_generation": _bounded(
                before.get("window_generation"), 160,
            ),
            "target_foreground": before.get("target_foreground") is True,
            "structural_epoch": structural_epoch,
            "observed_at": observed_at,
            "state_signature": _bounded(
                before.get("state_signature"), 80,
            ),
            "state_rows": state_rows,
            "target": normalized_target,
        },
    }
    if (
        not normalized["kind"]
        or dispatched_at <= 0.0
        or not normalized["before"]["application"]
        or not normalized["before"]["window_surface_ref"]
        or not normalized["before"]["window_generation"]
    ):
        return {}
    return normalized


def _capture_desktop_surface_identity(
    surface_ref: object,
    *,
    primitive_receipt: object = None,
) -> dict:
    """Capture dispatch identity without making a stale ref actionable."""

    ref_id = _bounded(surface_ref, 160)
    identity = {"surface_ref": ref_id}
    if ref_id:
        try:
            from osia.desktop_surface import (
                DESKTOP_SURFACE_REGISTRY,
                SurfaceKind,
            )

            resolution = DESKTOP_SURFACE_REGISTRY.resolve(ref_id)
            surface = resolution.surface
            native = resolution.native
            if surface is not None:
                identity.update({
                    "application": surface.application_id,
                    "process_ref": surface.process_ref,
                    "window_generation": surface.window_generation,
                    "structural_epoch": surface.structural_epoch,
                    "surface_kind": surface.surface_kind.value,
                    "role": surface.role,
                    "runtime_signature": surface.runtime_signature,
                    "automation_id": surface.automation_id,
                    "parent_surface_ref": surface.parent_ref or "",
                    "patterns": list(surface.patterns),
                    "observed_at": surface.observed_at,
                })
                current = surface
                for _depth in range(16):
                    if current.surface_kind in {
                        SurfaceKind.WINDOW, SurfaceKind.DIALOG,
                    }:
                        identity["window_surface_ref"] = current.ref_id
                        break
                    if not current.parent_ref:
                        break
                    parent = DESKTOP_SURFACE_REGISTRY.resolve(
                        current.parent_ref,
                    ).surface
                    if parent is None:
                        break
                    current = parent
            if native is not None:
                identity["native_window_id"] = native.native_window_id
        except Exception:
            pass
    primitive = _normalize_primitive_receipt(primitive_receipt)
    if primitive:
        before = primitive["before"]
        target = before.get("target") or {}
        identity.update({
            "surface_ref": target.get("surface_ref") or ref_id,
            "application": before.get("application") or identity.get(
                "application", "",
            ),
            "process_ref": target.get("process_ref") or identity.get(
                "process_ref", "",
            ),
            "window_surface_ref": before.get("window_surface_ref") or identity.get(
                "window_surface_ref", "",
            ),
            "window_generation": before.get("window_generation") or identity.get(
                "window_generation", "",
            ),
            "structural_epoch": before.get("structural_epoch") or identity.get(
                "structural_epoch", 0,
            ),
            "surface_kind": target.get("surface_kind") or identity.get(
                "surface_kind", "",
            ),
            "role": target.get("role") or identity.get("role", ""),
            "runtime_signature": target.get("runtime_signature") or identity.get(
                "runtime_signature", "",
            ),
            "automation_id": target.get("automation_id") or identity.get(
                "automation_id", "",
            ),
            "parent_surface_ref": target.get("parent_surface_ref") or identity.get(
                "parent_surface_ref", "",
            ),
            "patterns": target.get("patterns") or identity.get("patterns", []),
            "observed_at": before.get("observed_at") or identity.get(
                "observed_at", 0.0,
            ),
            "dispatched_at": primitive.get("dispatched_at") or 0.0,
        })
    return _normalize_desktop_surface_identity(identity)


def normalize_execution_memory(value: object) -> dict:
    """Return the bounded, path-free execution memory allowed in checkpoints."""

    raw = value if isinstance(value, dict) else {}
    effects = []
    for item in (raw.get("effects") or [])[:12]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("effect_key") or "")
        if not re.fullmatch(r"[0-9a-f]{24}", key):
            continue
        retired = []
        for retired_item in (item.get("retired_mechanisms") or [])[:16]:
            if not isinstance(retired_item, dict):
                continue
            mechanism_id = _bounded(retired_item.get("mechanism_id"), 120)
            reason = _bounded(retired_item.get("reason"), 80)
            if mechanism_id:
                retired.append({
                    "mechanism_id": mechanism_id,
                    "reason": reason or "retired_before_replan",
                })
        attempts = []
        for attempt in (item.get("attempts") or [])[-16:]:
            if not isinstance(attempt, dict):
                continue
            status = str(attempt.get("status") or "")
            mechanism_id = _bounded(attempt.get("mechanism_id"), 120)
            attempt_id = _bounded(attempt.get("attempt_id"), 120)
            if (
                status not in _MEMORY_ATTEMPT_STATUSES
                or not mechanism_id
                or not attempt_id
            ):
                continue
            try:
                layer = int(attempt.get("layer", 0))
                dispatched_at = float(attempt.get("dispatched_at", 0.0) or 0.0)
                dispatch_revision = max(
                    0, int(attempt.get("dispatch_evidence_revision", 0) or 0),
                )
                substep_index = max(
                    0, int(attempt.get("substep_index", 0) or 0),
                )
                semantic_checkpoint_count = min(
                    1, max(0, int(
                        attempt.get("semantic_checkpoint_count", 0) or 0
                    )),
                )
                raw_batch_resume = attempt.get(
                    "semantic_batch_resume_from_index"
                )
                semantic_batch_resume = (
                    None if raw_batch_resume is None else
                    max(0, int(raw_batch_resume))
                )
            except (TypeError, ValueError, OverflowError):
                continue
            if layer < int(StrategyLayer.DIRECT_CAPABILITY) or layer > int(
                StrategyLayer.GLOBAL_VISION
            ):
                continue
            primitive_receipt = _normalize_primitive_receipt(
                attempt.get("primitive_receipt"),
            )
            target_surface_identity = _normalize_desktop_surface_identity(
                attempt.get("target_surface_identity"),
            )
            if primitive_receipt and not target_surface_identity:
                target_surface_identity = _capture_desktop_surface_identity(
                    attempt.get("target_surface"),
                    primitive_receipt=primitive_receipt,
                )
            actionability = attempt.get("actionability_evidence")
            actionability = (
                actionability if isinstance(actionability, dict) else {}
            )
            attempts.append({
                "attempt_id": attempt_id,
                "mechanism_id": mechanism_id,
                "layer": layer,
                "mechanism": _bounded(attempt.get("mechanism"), 80),
                "reason_selected": _bounded(
                    attempt.get("reason_selected"), 180,
                ),
                "action_signature": _bounded(
                    attempt.get("action_signature"), 64,
                ),
                "status": status,
                "observations_gained": [
                    _memory_evidence_token(observation)
                    for observation in (
                        attempt.get("observations_gained") or []
                    )[-8:]
                    if _memory_evidence_token(observation)
                ],
                "failure_or_stagnation_reason": _bounded(
                    attempt.get("failure_or_stagnation_reason"), 120,
                ),
                "postcondition_evidence": _memory_evidence_token(
                    attempt.get("postcondition_evidence"),
                ),
                "attempted_effect": _bounded(
                    attempt.get("attempted_effect"), 160,
                ),
                "target_application": _bounded(
                    attempt.get("target_application"), 80,
                ),
                "target_surface": _bounded(
                    attempt.get("target_surface"), 160,
                ),
                "target_surface_identity": target_surface_identity,
                "dispatched_at": max(0.0, dispatched_at),
                "dispatch_evidence_revision": dispatch_revision,
                "composite_id": _bounded(attempt.get("composite_id"), 120),
                "substep_id": _bounded(attempt.get("substep_id"), 120),
                "substep_index": substep_index,
                "primitive_postcondition": _bounded(
                    attempt.get("primitive_postcondition"), 320,
                ),
                "observable_effect": normalize_desktop_observable_effect(
                    attempt.get("observable_effect"),
                ),
                "primitive_receipt": primitive_receipt,
                "action_pattern": _canonical_uia_action_pattern(
                    attempt.get("action_pattern"),
                ),
                "actionability_evidence": {
                    "semantic_match": actionability.get(
                        "semantic_match"
                    ) is True,
                    "actionable_match": actionability.get(
                        "actionable_match"
                    ) is True,
                    "selected_pattern_available": actionability.get(
                        "selected_pattern_available"
                    ) is True,
                    "dispatch_preflight_result": _bounded(
                        actionability.get("dispatch_preflight_result"), 80,
                    ),
                    "failed_before_effect_reason": _bounded(
                        actionability.get("failed_before_effect_reason"), 120,
                    ),
                    "stale_rebind_status": _bounded(
                        actionability.get("stale_rebind_status"), 80,
                    ),
                    "retirement_state": _bounded(
                        actionability.get("retirement_state"), 80,
                    ),
                    "automation_id": _bounded(
                        actionability.get("automation_id"), 160,
                    ),
                    "fresh_supported_patterns": [
                        pattern
                        for item in (
                            actionability.get("fresh_supported_patterns") or []
                        )[:8]
                        if (pattern := _canonical_uia_action_pattern(item))
                    ],
                },
                "semantic_batch_id": _bounded(
                    attempt.get("semantic_batch_id"), 160,
                ),
                "semantic_batch_effects": [
                    effect
                    for raw_effect in (
                        attempt.get("semantic_batch_effects") or []
                    )[:8]
                    if (effect := normalize_desktop_observable_effect(
                        raw_effect
                    ))
                ],
                "semantic_checkpoint_count": (
                    semantic_checkpoint_count
                ),
                "semantic_batch_resume_from_index": semantic_batch_resume,
            })
        mechanisms = []
        for mechanism in (item.get("mechanisms") or [])[:16]:
            if not isinstance(mechanism, dict):
                continue
            try:
                layer = int(mechanism.get("layer", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            mechanism_id = _bounded(mechanism.get("mechanism_id"), 120)
            if (
                not mechanism_id
                or layer < int(StrategyLayer.DIRECT_CAPABILITY)
                or layer > int(StrategyLayer.GLOBAL_VISION)
            ):
                continue
            mechanisms.append({
                "mechanism_id": mechanism_id,
                "layer": layer,
                "reason_selected": _bounded(
                    mechanism.get("reason_selected"), 180,
                ),
                "application": _bounded(mechanism.get("application"), 80),
                "surface": _bounded(mechanism.get("surface"), 80),
                "account": _bounded(mechanism.get("account"), 80),
                "applicable": mechanism.get("applicable") is not False,
                "covers_goal": mechanism.get("covers_goal") is not False,
                "mutation": mechanism.get("mutation") is True,
                "attempted_effect": _bounded(
                    mechanism.get("attempted_effect"), 160,
                ),
            })
        effects.append({
            "effect_key": key,
            "semantic_domain": _bounded(item.get("semantic_domain"), 32),
            "target_object": _bounded(item.get("target_object"), 160),
            "target_application": _bounded(
                item.get("target_application"), 80,
            ),
            "target_surface": _bounded(item.get("target_surface"), 80),
            "requested_action": _bounded(item.get("requested_action"), 40),
            "requested_content": _bounded(item.get("requested_content"), 320),
            "requires_execution": item.get("requires_execution") is True,
            "required_postconditions": [
                _bounded(postcondition, 320)
                for postcondition in (
                    item.get("required_postconditions") or []
                )[:8]
                if _bounded(postcondition, 320)
            ],
            "completed": item.get("completed") is True,
            "completion_evidence": [
                _memory_evidence_token(evidence)
                for evidence in (item.get("completion_evidence") or [])[:8]
                if _memory_evidence_token(evidence)
            ],
            "retired_mechanisms": retired,
            "attempts": attempts,
            "mechanisms": mechanisms,
            "unresolved_reason": _bounded(
                item.get("unresolved_reason"), 120,
            ),
            "unresolved_mechanism_id": _bounded(
                item.get("unresolved_mechanism_id"), 120,
            ),
            "unresolved_attempt_id": _bounded(
                item.get("unresolved_attempt_id"), 120,
            ),
            "completed_primitive_signatures": [
                _bounded(signature, 80)
                for signature in (
                    item.get("completed_primitive_signatures") or []
                )[-64:]
                if _bounded(signature, 80)
            ],
            "completed_substeps": [
                {
                    str(key_name)[:80]: (
                        value_item
                        if isinstance(value_item, (bool, int, float))
                        else _memory_evidence_token(value_item)
                        if str(key_name) in {"evidence", "observation"}
                        else _bounded(value_item, 180)
                    )
                    for key_name, value_item in substep.items()
                    if isinstance(value_item, (bool, int, float, str))
                }
                for substep in (item.get("completed_substeps") or [])[-64:]
                if isinstance(substep, dict)
            ],
        })
    completed_effects = []
    for item in (raw.get("completed_effects") or [])[-32:]:
        if not isinstance(item, dict):
            continue
        attempted_effect = _bounded(item.get("attempted_effect"), 160)
        if not attempted_effect:
            continue
        try:
            observed_at = max(0.0, float(item.get("observed_at") or 0.0))
        except (TypeError, ValueError, OverflowError):
            observed_at = 0.0
        completed_effects.append({
            "attempted_effect": attempted_effect,
            "target_application": _bounded(
                item.get("target_application"), 80,
            ),
            "target_surface": _bounded(item.get("target_surface"), 80),
            "evidence": _memory_evidence_token(item.get("evidence")),
            "observed_at": observed_at,
        })
    observation = raw.get("desktop_observation")
    observation = observation if isinstance(observation, dict) else {}
    try:
        observed_at = float(observation.get("observed_at", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        observed_at = 0.0
    desktop_observation = {
        "application": _bounded(observation.get("application"), 160),
        "requested_application": _bounded(
            observation.get("requested_application"), 160,
        ),
        "window_surface_ref": _bounded(
            observation.get("window_surface_ref"), 160,
        ),
        "window_generation": _bounded(
            observation.get("window_generation"), 160,
        ),
        "accessibility_quality": _bounded(
            observation.get("accessibility_quality"), 80,
        ),
        "target_resolution": _bounded(
            observation.get("target_resolution"), 80,
        ),
        "target_foreground": observation.get("target_foreground") is True,
        "target_minimized": observation.get("target_minimized") is True,
        "target_identity_evidence": _bounded(
            observation.get("target_identity_evidence"), 240,
        ),
        "foreground_application": _bounded(
            observation.get("foreground_application"), 160,
        ),
        "focus_observable": observation.get("focus_observable") is True,
        "focused_surface_ref": _bounded(
            observation.get("focused_surface_ref"), 160,
        ),
        "uia_frontier_observed": (
            observation.get("uia_frontier_observed") is True
        ),
        "uia_actionable_frontier": _normalize_desktop_uia_frontier(
            observation.get("uia_actionable_frontier"),
        ),
        "uia_text_surfaces": _normalize_desktop_text_surfaces(
            observation.get("uia_text_surfaces"),
        ),
        "evidence_revision": max(
            0, int(observation.get("evidence_revision") or 0),
        ) if str(observation.get("evidence_revision") or "0").isdigit() else 0,
        "observed_at": max(0.0, observed_at),
    }
    if not any(desktop_observation.values()):
        desktop_observation = {}
    try:
        evidence_revision = max(0, int(raw.get("evidence_revision", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        evidence_revision = 0
    return {
        "schema_version": 1,
        "effects": effects,
        "completed_effects": completed_effects,
        "desktop_observation": desktop_observation,
        "evidence_revision": evidence_revision,
    }


def export_execution_memory(strategy: ExecutionStrategy) -> dict:
    """Project only semantic/replay/evidence facts needed by a later replan."""

    effects = []
    one_node = len(strategy.mission.action_nodes) == 1
    for node in strategy.mission.action_nodes[:12]:
        scope_prefix = _bounded(node.node_id, 96) + "|"
        retired = [
            {
                "mechanism_id": scope[len(scope_prefix):],
                "reason": reason,
            }
            for scope, reason in strategy._retired_effect_mechanisms.items()
            if scope.startswith(scope_prefix)
        ]
        attempts = [
            attempt.public_dict() for attempt in strategy.attempts[-16:]
            if attempt.node_id == node.node_id
        ]
        attempt_mechanisms = {item["mechanism_id"] for item in attempts}
        mechanisms = [
            {**asdict(mechanism), "layer": int(mechanism.layer)}
            for mechanism in strategy._mechanisms.values()
            if mechanism.node_id == node.node_id
            or mechanism.mechanism_id in attempt_mechanisms
        ]
        completed = all(
            postcondition in strategy.proven_postconditions
            for postcondition in node.required_postconditions
        )
        effects.append({
            "effect_key": semantic_effect_key(node),
            "semantic_domain": _semantic_domain(node),
            "target_object": node.target_object,
            "target_application": node.required_application,
            "target_surface": node.required_surface,
            "requested_action": node.requested_action,
            "requested_content": node.requested_content,
            "requires_execution": node.requires_execution,
            "required_postconditions": list(node.required_postconditions),
            "completed": completed,
            "completion_evidence": [
                strategy.proven_postconditions[postcondition]
                for postcondition in node.required_postconditions
                if postcondition in strategy.proven_postconditions
            ],
            "retired_mechanisms": retired,
            "attempts": attempts,
            "mechanisms": mechanisms,
            "unresolved_reason": (
                strategy.frozen_reason
                if strategy._frozen_node_id == node.node_id else ""
            ),
            "unresolved_mechanism_id": (
                strategy._frozen_mechanism_id
                if strategy._frozen_node_id == node.node_id else ""
            ),
            "unresolved_attempt_id": (
                strategy._frozen_attempt_id
                if strategy._frozen_node_id == node.node_id else ""
            ),
            "completed_primitive_signatures": (
                sorted(strategy._completed_primitive_signatures)
                if one_node else []
            ),
            "completed_substeps": (
                list(strategy._completed_substeps) if one_node else []
            ),
        })
    return normalize_execution_memory({
        "effects": effects,
        "completed_effects": list(strategy.completed_effect_facts()),
        "desktop_observation": strategy._latest_desktop_observation,
        "evidence_revision": strategy._evidence_revision,
    })


def _rebound_mechanism_id(
    mechanism_id: str,
    old_node_id: str,
    new_node_id: str,
) -> str:
    suffix = ":" + old_node_id
    if old_node_id and mechanism_id.endswith(suffix):
        return mechanism_id[:-len(suffix)] + ":" + new_node_id
    if re.search(r":node_[0-9a-f]{16}$", mechanism_id):
        return re.sub(
            r":node_[0-9a-f]{16}$", ":" + new_node_id, mechanism_id,
        )
    return mechanism_id


def _effect_matches_replanned_node(
    node: SemanticActionNode,
    effect: dict,
) -> bool:
    """Match an exact effect or its read-only result-verification successor."""

    if effect.get("effect_key") == semantic_effect_key(node):
        return True
    if (
        _semantic_domain(node) != "desktop"
        or effect.get("semantic_domain") != "desktop"
        or _normalized(effect.get("target_application"))
        != _normalized(node.required_application)
        or str(effect.get("requested_action") or "")
        not in {"calculate", "type"}
        or node.requested_action
    ):
        return False
    current_facts = set(re.findall(
        r"\d+(?:[.,]\d+)?",
        " ".join(node.required_postconditions),
    ))
    prior_facts = set(re.findall(
        r"\d+(?:[.,]\d+)?",
        " ".join(str(item) for item in (
            effect.get("required_postconditions") or []
        )),
    ))
    return bool(current_facts and current_facts == prior_facts)


def _restore_pending_effect_contract(
    node: SemanticActionNode,
    effect: dict,
) -> bool:
    """Keep a pending mutation executable when a replan emits only prose."""

    action = str(effect.get("requested_action") or "")
    requires_execution = bool(
        effect.get("requires_execution") is True
        or (
            action
            and effect.get("completed") is not True
            and effect.get("semantic_domain") in {"desktop", "browser"}
        )
    )
    if (
        effect.get("completed") is True
        or not requires_execution
        or not action
        or node.requires_execution
        or (node.requested_action and node.requested_action != action)
    ):
        return False

    node.target_object = str(
        effect.get("target_object") or node.target_object
    )
    node.required_application = str(
        effect.get("target_application") or node.required_application
    )
    node.required_surface = str(
        effect.get("target_surface") or node.required_surface
    )
    node.requested_action = action
    node.requested_content = str(
        effect.get("requested_content") or node.requested_content
    )
    node.requires_execution = True
    payload = {
        "target_object": node.target_object,
        "required_application": node.required_application,
        "required_surface": node.required_surface,
        "required_account": node.required_account,
        "requested_action": node.requested_action,
        "observable_success_postcondition": node.observable_success_postcondition,
        "required_postconditions": node.required_postconditions,
    }
    node.fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:24]
    _node_with_identity(
        node,
        order=node.order,
        clause=node.source_clause,
        application=node.required_application,
    )
    return True


def rebind_execution_memory(
    strategy: ExecutionStrategy,
    memories: Iterable[dict],
    *,
    now: float | None = None,
    fresh_desktop_seconds: float = 30.0,
) -> ExecutionStrategy:
    """Rebind verified facts to equivalent new nodes without reusing stale refs."""

    normalized = [
        normalize_execution_memory(memory)
        for memory in memories
        if isinstance(memory, dict)
    ]
    for memory in normalized:
        for fact in memory.get("completed_effects") or []:
            strategy.remember_completed_effect(
                str(fact.get("attempted_effect") or ""),
                target_application=str(fact.get("target_application") or ""),
                target_surface=str(fact.get("target_surface") or ""),
                evidence=str(fact.get("evidence") or ""),
                observed_at=float(fact.get("observed_at") or 0.0),
            )
        for effect in memory.get("effects") or []:
            if effect.get("completed") is not True:
                continue
            attempted_effect = next((
                str(item.get("attempted_effect") or "")
                for item in reversed(effect.get("attempts") or [])
                if item.get("attempted_effect")
            ), "") or next((
                str(item.get("attempted_effect") or "")
                for item in effect.get("mechanisms") or []
                if item.get("attempted_effect")
            ), "")
            if attempted_effect:
                strategy.remember_completed_effect(
                    attempted_effect,
                    target_application=str(
                        effect.get("target_application") or ""
                    ),
                    target_surface=str(effect.get("target_surface") or ""),
                    evidence=next(iter(
                        effect.get("completion_evidence") or []
                    ), ""),
                    observed_at=max(
                        [0.0]
                        + [
                            float(item.get("dispatched_at") or 0.0)
                            for item in effect.get("attempts") or []
                        ]
                    ),
                )
    restored_contract = False
    for node in strategy.mission.action_nodes:
        matches = [
            effect
            for memory in normalized
            for effect in memory.get("effects") or []
            if _effect_matches_replanned_node(node, effect)
        ]
        for effect in matches:
            if _restore_pending_effect_contract(node, effect):
                restored_contract = True
                for mechanism in _default_mechanisms_for_node(
                    node,
                    strategy.available_tools,
                    multiple_nodes=len(strategy.mission.action_nodes) > 1,
                ):
                    strategy.register(mechanism)
            for raw_attempt in effect.get("attempts") or []:
                mechanism_id = str(raw_attempt.get("mechanism_id") or "")
                rebound_id = _rebound_mechanism_id(
                    mechanism_id, "", node.node_id,
                )
                mechanism_data = next((
                    item for item in effect.get("mechanisms") or []
                    if item.get("mechanism_id") == mechanism_id
                ), None)
                if rebound_id not in strategy._mechanisms and mechanism_data:
                    strategy.register(StrategyMechanism(
                        mechanism_id=rebound_id,
                        layer=StrategyLayer(int(mechanism_data["layer"])),
                        reason_selected=str(
                            mechanism_data.get("reason_selected") or ""
                        ),
                        application=str(mechanism_data.get("application") or ""),
                        surface=str(mechanism_data.get("surface") or ""),
                        account=str(mechanism_data.get("account") or ""),
                        applicable=mechanism_data.get("applicable") is not False,
                        covers_goal=mechanism_data.get("covers_goal") is not False,
                        mutation=mechanism_data.get("mutation") is True,
                        node_id=node.node_id,
                        attempted_effect=str(
                            mechanism_data.get("attempted_effect") or ""
                        ),
                    ))
                if any(
                    existing.attempt_id == raw_attempt["attempt_id"]
                    for existing in strategy.attempts
                ):
                    continue
                restored_attempt = StrategyAttempt(
                    attempt_id=raw_attempt["attempt_id"],
                    mechanism_id=rebound_id,
                    layer=int(raw_attempt["layer"]),
                    mechanism=str(raw_attempt.get("mechanism") or ""),
                    reason_selected=str(
                        raw_attempt.get("reason_selected") or ""
                    ),
                    action_signature=str(
                        raw_attempt.get("action_signature") or ""
                    ),
                    status=str(raw_attempt.get("status") or ""),
                    observations_gained=list(
                        raw_attempt.get("observations_gained") or []
                    ),
                    failure_or_stagnation_reason=str(
                        raw_attempt.get("failure_or_stagnation_reason") or ""
                    ),
                    postcondition_evidence=str(
                        raw_attempt.get("postcondition_evidence") or ""
                    ),
                    node_id=node.node_id,
                    attempted_effect=str(
                        raw_attempt.get("attempted_effect") or ""
                    ),
                    target_application=str(
                        raw_attempt.get("target_application") or ""
                    ),
                    target_surface=str(
                        raw_attempt.get("target_surface") or ""
                    ),
                    target_surface_identity=dict(
                        raw_attempt.get("target_surface_identity") or {}
                    ),
                    dispatched_at=float(raw_attempt.get("dispatched_at") or 0.0),
                    dispatch_evidence_revision=int(
                        raw_attempt.get("dispatch_evidence_revision") or 0
                    ),
                    composite_id=str(raw_attempt.get("composite_id") or ""),
                    substep_id=str(raw_attempt.get("substep_id") or ""),
                    substep_index=int(raw_attempt.get("substep_index") or 0),
                    primitive_postcondition=str(
                        raw_attempt.get("primitive_postcondition") or ""
                    ),
                    observable_effect=dict(
                        raw_attempt.get("observable_effect") or {}
                    ),
                    primitive_receipt=dict(
                        raw_attempt.get("primitive_receipt") or {}
                    ),
                    action_pattern=str(
                        raw_attempt.get("action_pattern") or ""
                    ),
                    actionability_evidence=dict(
                        raw_attempt.get("actionability_evidence") or {}
                    ),
                    semantic_batch_id=str(
                        raw_attempt.get("semantic_batch_id") or ""
                    ),
                    semantic_batch_effects=[
                        dict(item)
                        for item in (
                            raw_attempt.get("semantic_batch_effects") or []
                        )
                        if isinstance(item, dict)
                    ],
                    semantic_checkpoint_count=int(
                        raw_attempt.get("semantic_checkpoint_count") or 0
                    ),
                    semantic_batch_resume_from_index=(
                        int(raw_attempt["semantic_batch_resume_from_index"])
                        if raw_attempt.get(
                            "semantic_batch_resume_from_index"
                        ) is not None else None
                    ),
                )
                strategy.attempts.append(restored_attempt)
                if (
                    restored_attempt.composite_id
                    and restored_attempt.substep_index > 0
                ):
                    strategy._substep_counters[
                        restored_attempt.composite_id
                    ] = max(
                        strategy._substep_counters.get(
                            restored_attempt.composite_id, 0,
                        ),
                        restored_attempt.substep_index,
                    )
            for retired in effect.get("retired_mechanisms") or []:
                mechanism_id = _rebound_mechanism_id(
                    str(retired.get("mechanism_id") or ""), "", node.node_id,
                )
                strategy._retired_effect_mechanisms[
                    strategy._effect_scope(mechanism_id, node.node_id)
                ] = str(retired.get("reason") or "retired_before_replan")
            if effect.get("completed") is True:
                evidence = next(iter(effect.get("completion_evidence") or []), "")
                evidence = evidence or (
                    "replan_rebound_verified_effect:"
                    + str(effect.get("effect_key") or "")
                )
                for postcondition in node.required_postconditions:
                    strategy.proven_postconditions[postcondition] = evidence
                node.status = "succeeded"
            unresolved_reason = str(effect.get("unresolved_reason") or "")
            if unresolved_reason and not effect.get("completed"):
                unresolved_mechanism = str(
                    effect.get("unresolved_mechanism_id") or ""
                )
                unresolved_attempt = next((
                    attempt for attempt in reversed(strategy.attempts)
                    if attempt.node_id == node.node_id
                    and (
                        not unresolved_mechanism
                        or attempt.mechanism_id == unresolved_mechanism
                    )
                ), None)
                if unresolved_attempt is not None:
                    node.status = "unknown"
                    strategy.frozen_reason = unresolved_reason
                    strategy._frozen_node_id = node.node_id
                    strategy._frozen_mechanism_id = unresolved_attempt.mechanism_id
                    strategy._frozen_attempt_id = unresolved_attempt.attempt_id
            strategy._completed_primitive_signatures.update(
                effect.get("completed_primitive_signatures") or []
            )
            for substep in effect.get("completed_substeps") or []:
                if substep not in strategy._completed_substeps:
                    strategy._completed_substeps.append(dict(substep))
    if restored_contract:
        payload = [node.public_dict() for node in strategy.mission.action_nodes]
        strategy.mission.fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:24]
    strategy.succeeded = all(
        postcondition in strategy.proven_postconditions
        for postcondition in strategy.required_postconditions
    )
    target_apps = {
        _normalized(node.required_application)
        for node in strategy.mission.action_nodes
        if node.required_application
    }
    observations = [
        memory.get("desktop_observation") or {}
        for memory in normalized
        if memory.get("desktop_observation")
    ]
    observations.sort(
        key=lambda item: float(item.get("observed_at") or 0.0),
        reverse=True,
    )
    observation = next((
        item for item in observations
        if _normalized(
            item.get("requested_application") or item.get("application")
        ) in target_apps
    ), None)
    if observation is not None:
        current = time.time() if now is None else float(now)
        observed_at = float(observation.get("observed_at") or 0.0)
        age = current - observed_at
        fresh = bool(
            observation.get("target_resolution") == "exact_target"
            and observed_at > 0.0
            and -5.0 <= age <= max(0.0, fresh_desktop_seconds)
        )
        if fresh:
            strategy._latest_desktop_observation = dict(observation)
        else:
            strategy._latest_desktop_observation = {
                "application": str(observation.get("application") or ""),
                "requested_application": str(
                    observation.get("requested_application") or ""
                ),
                "window_surface_ref": "",
                "window_generation": "",
                "accessibility_quality": "",
                "target_resolution": "stale_target_requires_refresh",
                "target_foreground": False,
                "target_minimized": False,
                "target_identity_evidence": str(
                    observation.get("target_identity_evidence") or ""
                ),
                "foreground_application": str(
                    observation.get("foreground_application") or ""
                ),
                "focus_observable": False,
                "focused_surface_ref": "",
                "uia_frontier_observed": False,
                "uia_actionable_frontier": [],
                "uia_text_surfaces": [],
                "evidence_revision": int(
                    observation.get("evidence_revision") or 0
                ),
                "observed_at": observed_at,
            }
    strategy._evidence_revision = max(
        [strategy._evidence_revision]
        + [int(memory.get("evidence_revision") or 0) for memory in normalized]
    )
    return strategy


def select_local_reconciliation_probe(
    strategy: ExecutionStrategy,
    available_tools: Iterable[str],
    *,
    user_request: str = "",
) -> dict | None:
    """Select one canonical read-only observer for the exact frozen effect.

    This is an evidence-gathering decision only.  It cannot select or dispatch
    a new semantic mutation, and unknown/dynamic effect classifications fail
    closed even when a tool name resembles an observer.
    """

    if not strategy.frozen_reason or not strategy._frozen_attempt_id:
        return None
    node = strategy.node_by_id(strategy._frozen_node_id)
    attempt = strategy.attempt_by_id(strategy._frozen_attempt_id)
    if node is None or attempt is None:
        return None
    semantic_effect = str(attempt.attempted_effect or "")
    observer_relation = _reconciliation_observer_relation(attempt)
    target_application = str(attempt.target_application or "")
    target_surface = str(attempt.target_surface or "")
    available = {str(item or "") for item in available_tools}
    if _semantic_domain(node) == "web" and semantic_effect:
        tool_name = "get_browser_state"
        if tool_name not in available:
            return None
        arguments: dict = {}
        try:
            from osia.tool_contracts import (
                SideEffectClass,
                effective_contract,
                get_tool_spec,
            )

            spec = get_tool_spec(tool_name)
            if spec is None:
                return None
            contract = effective_contract(
                spec,
                arguments,
                user_request or strategy.mission.objective,
            )
        except Exception:
            return None
        if (
            spec.side_effect is not SideEffectClass.READ_ONLY
            or contract.side_effect is not SideEffectClass.READ_ONLY
        ):
            return None
        return {
            "tool": tool_name,
            "arguments": arguments,
            "semantic_effect": semantic_effect,
            "postcondition": semantic_effect,
            "strategy_attempt_id": attempt.attempt_id,
            "semantic_node_id": node.node_id,
            "mechanism_id": attempt.mechanism_id,
            "target_application": target_application,
            "target_surface": target_surface,
            "observer_relation": "browser_state_after_action",
            "composite_id": attempt.composite_id,
            "substep_id": attempt.substep_id,
            "substep_index": attempt.substep_index,
            "primitive_postcondition": attempt.primitive_postcondition,
            "observable_effect": dict(attempt.observable_effect),
        }
    if (
        _semantic_domain(node) != "desktop"
        or not semantic_effect
        or not target_application
        or _normalized(target_application)
        != _normalized(node.required_application)
    ):
        return None
    candidates = _RECONCILIATION_OBSERVERS.get(observer_relation, ())
    for tool_name in candidates:
        if tool_name not in available:
            continue
        arguments = {"target": target_application}
        try:
            from osia.tool_contracts import (
                SideEffectClass,
                effective_contract,
                get_tool_spec,
            )

            spec = get_tool_spec(tool_name)
            if spec is None:
                continue
            contract = effective_contract(
                spec,
                arguments,
                user_request or strategy.mission.objective,
            )
        except Exception:
            continue
        if (
            spec.side_effect is not SideEffectClass.READ_ONLY
            or contract.side_effect is not SideEffectClass.READ_ONLY
        ):
            continue
        applicable, _reason = _tool_is_structurally_applicable(
            strategy, tool_name, arguments,
        )
        if not applicable or _tool_domain(tool_name) != "desktop":
            continue
        return {
            "tool": tool_name,
            "arguments": arguments,
            "semantic_effect": semantic_effect,
            "postcondition": semantic_effect,
            "strategy_attempt_id": attempt.attempt_id,
            "semantic_node_id": node.node_id,
            "mechanism_id": attempt.mechanism_id,
            "target_application": target_application,
            "target_surface": target_surface,
            "observer_relation": observer_relation,
            "composite_id": attempt.composite_id,
            "substep_id": attempt.substep_id,
            "substep_index": attempt.substep_index,
            "primitive_postcondition": attempt.primitive_postcondition,
            "observable_effect": dict(attempt.observable_effect),
        }
    return None


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


def _single_application_hint(*values: object) -> str:
    """Return one explicit application shared by trusted task metadata."""

    found: list[str] = []
    for value in values:
        normalized = _normalized(value)
        for candidate in _KNOWN_APPLICATIONS:
            if not re.search(
                rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])",
                normalized,
            ):
                continue
            canonical = _canonical_application(candidate)
            if canonical not in found:
                found.append(canonical)
    return found[0] if len(found) == 1 else ""


def _mission_with_application_hint(
    mission: SemanticMission,
    application: str,
) -> SemanticMission:
    if not application or all(
        node.required_application for node in mission.action_nodes
    ):
        return mission
    nodes = []
    for node in mission.action_nodes:
        if not node.required_application:
            node = _node_with_identity(
                node,
                order=node.order,
                clause=node.source_clause,
                application=application,
            )
            if node.requested_action:
                node.requires_execution = True
        nodes.append(node)
    payload = [node.public_dict() for node in nodes]
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:24]
    return SemanticMission(
        objective=mission.objective,
        action_nodes=tuple(nodes),
        fingerprint=fingerprint,
        execution_required_override=mission.execution_required_override,
    )


def _default_mechanisms_for_node(
    node: SemanticActionNode,
    tools: frozenset[str],
    *,
    multiple_nodes: bool,
) -> tuple[StrategyMechanism, ...]:
    mechanisms: list[StrategyMechanism] = []
    deep_link = _recommended_deep_link(node)
    if deep_link is not None and "open_uri" in tools:
        mechanisms.append(StrategyMechanism(
            **{**asdict(deep_link), "node_id": node.node_id},
        ))
    if "desktop_click" in tools:
        suffix = ":" + node.node_id if multiple_nodes else ""
        common = {
            "application": node.required_application,
            "surface": node.required_surface,
            "account": node.required_account,
            "covers_goal": False,
            "mutation": True,
            "node_id": node.node_id,
        }
        mechanisms.extend((
            StrategyMechanism(
                "native_accessibility" + suffix,
                StrategyLayer.NATIVE_ACCESSIBILITY,
                "fresh native accessibility target", **common,
            ),
            StrategyMechanism(
                "keyboard_focus" + suffix, StrategyLayer.KEYBOARD,
                "observable focus after accessibility direct action stagnates", **common,
            ),
            StrategyMechanism(
                "local_vision" + suffix, StrategyLayer.LOCAL_VISION,
                "zoomed local target after deterministic native paths stagnate", **common,
            ),
            StrategyMechanism(
                "global_vision" + suffix, StrategyLayer.GLOBAL_VISION,
                "global coordinates only after every higher applicable layer", **common,
            ),
        ))
    return tuple(mechanisms)


def build_execution_strategy(
    request: str,
    available_tools: Iterable[str] = (),
    *,
    postcondition: str = "",
    postconditions: Iterable[str] = (),
    semantic_context: str = "",
) -> ExecutionStrategy:
    explicit_postconditions = tuple(postconditions)
    # Autonomous supplies a trusted, labelled step contract inside a larger
    # provider instruction. Only that structured objective/criterion is
    # semantic work; the surrounding instructions are not action nodes.
    objective_marker = "\nSTEP OBJECTIVE:\n"
    criterion_marker = (
        "\nSUCCESS CRITERION (you are done only when this is OBSERVABLY true):\n"
    )
    if (
        "You are executing ONE step of a larger autonomous task." in request
        and objective_marker in request
        and criterion_marker in request
    ):
        before_criterion, criterion_tail = request.split(criterion_marker, 1)
        _prefix, structured_objective = before_criterion.rsplit(
            objective_marker, 1,
        )
        structured_objective = structured_objective.strip()
        structured_criterion = criterion_tail.split("\n\n", 1)[0].strip()
        if structured_objective and structured_criterion:
            request = structured_objective
            semantic_context = " ".join(
                item for item in (semantic_context, structured_criterion) if item
            )
            if not postcondition and not explicit_postconditions:
                postcondition = structured_criterion
    mission = derive_semantic_mission(
        request,
        postcondition=postcondition,
        postconditions=explicit_postconditions,
    )
    mission = _mission_with_application_hint(
        mission,
        _single_application_hint(request, semantic_context, postcondition),
    )
    tools = frozenset(str(item) for item in available_tools)
    mechanisms: list[StrategyMechanism] = []
    for node in mission.action_nodes:
        if not node.requires_execution:
            continue
        mechanisms.extend(_default_mechanisms_for_node(
            node,
            tools,
            multiple_nodes=len(mission.action_nodes) > 1,
        ))
    return ExecutionStrategy(mission, mechanisms, available_tools=tools)


def mechanism_for_tool(
    strategy: ExecutionStrategy,
    tool_name: str,
    arguments: dict,
) -> StrategyMechanism | None:
    matched_goal = strategy.match_action_node(tool_name, arguments)
    goal = matched_goal or strategy.goal
    node_id = goal.node_id
    if tool_name in _OBSERVATION_TOOLS:
        return None
    if tool_name == "open_application":
        app = _canonical_application(
            arguments.get("name")
        ) or goal.required_application
        covers = goal.requested_action == "open" and not goal.required_surface
        try:
            from osia.tool_contracts import get_tool_spec

            spec = get_tool_spec(tool_name)
            attempted_effect = str(
                getattr(spec, "attempted_effect", "") or ""
            )
        except Exception:
            attempted_effect = ""
        return StrategyMechanism(
            f"direct:open_application:{app}", StrategyLayer.DIRECT_CAPABILITY,
            "exact native application launch capability", application=app,
            surface="", account=goal.required_account, covers_goal=covers,
            mutation=True, node_id=node_id,
            attempted_effect=attempted_effect,
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
            mutation=True, node_id=node_id,
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
            covers_goal=True, mutation=True, node_id=node_id,
        )
    if tool_name == "desktop_click":
        # The composite owns DOM/UIA/keyboard/local/global selection internally.
        return None
    if tool_name == "activate_desktop_surface":
        surface_ref = _bounded(arguments.get("surface_ref"), 160)
        window_ref = _bounded(
            strategy._latest_desktop_observation.get("window_surface_ref"),
            160,
        )
        if (
            surface_ref
            and surface_ref == window_ref
            and _normalized(arguments.get("action_pattern")) in {"", "window"}
        ):
            return StrategyMechanism(
                "native_accessibility:preparatory_focus",
                StrategyLayer.NATIVE_ACCESSIBILITY,
                "exact target foreground acquisition",
                application=goal.required_application,
                surface=surface_ref,
                account=goal.required_account,
                covers_goal=False,
                mutation=True,
                node_id=node_id,
                attempted_effect="application_foreground",
            )
        semantic_native = next((
            item
            for item in strategy._mechanisms.values()
            if item.layer == StrategyLayer.NATIVE_ACCESSIBILITY
            and item.node_id == node_id
            and item.attempted_effect != "application_foreground"
            and item.mechanism_id
            != "native_accessibility:preparatory_focus"
        ), None)
        return semantic_native or StrategyMechanism(
            "native_accessibility", StrategyLayer.NATIVE_ACCESSIBILITY,
            "fresh opaque native accessibility target",
            application=goal.required_application, surface=goal.required_surface,
            account=goal.required_account, covers_goal=False, mutation=True,
            node_id=node_id,
        )
    if tool_name in {"desktop_press", "desktop_type"}:
        return strategy.mechanism_for_layer(
            StrategyLayer.KEYBOARD,
            node_id=node_id,
        ) or StrategyMechanism(
            "keyboard_focus", StrategyLayer.KEYBOARD,
            "keyboard action requiring observable focus",
            application=goal.required_application, surface=goal.required_surface,
            account=goal.required_account, covers_goal=False, mutation=True,
            node_id=node_id,
        )
    if tool_name == "run_command":
        application = strategy._tool_application(tool_name, arguments)
        action = strategy._tool_action(tool_name, arguments)
        desktop_goal = bool(goal.required_application)
        desktop_calculation = goal.requested_action == "calculate"
        return StrategyMechanism(
            "direct:run_command", StrategyLayer.DIRECT_CAPABILITY,
            "exact local command capability",
            application=application,
            surface=goal.required_surface,
            account=goal.required_account,
            covers_goal=bool(
                not desktop_goal
                and
                matched_goal is not None
                and (not action or action == goal.requested_action)
            ),
            applicable=not desktop_calculation,
            mutation=True,
            node_id=node_id,
        )
    return StrategyMechanism(
        f"direct:{_bounded(tool_name, 80)}", StrategyLayer.DIRECT_CAPABILITY,
        "provider/API/MCP/OSIA structured direct capability",
        application=goal.required_application, surface=goal.required_surface,
        account=goal.required_account, covers_goal=True, mutation=True,
        node_id=node_id,
    )


def _tool_semantic_actions(tool_name: object, arguments: dict) -> frozenset[str]:
    """Return only coarse action classes needed by explicit user prohibitions."""

    name = str(tool_name or "").strip().lower()
    actions: set[str] = set()
    if name in {
        "click", "find_and_click", "desktop_click",
        "activate_browser_element", "open_search_result", "paginate_browser",
    }:
        actions.add("activate")
    if name == "desktop_uia_action" and str(
        (arguments or {}).get("action_pattern") or ""
    ).strip().lower() in {
        "invoke", "selectionitem", "toggle", "expandcollapse",
    }:
        actions.add("activate")
    if name == "activate_desktop_surface":
        values = arguments if isinstance(arguments, dict) else {}
        semantic_batch = values.get("semantic_batch")
        batch_items = (
            semantic_batch if isinstance(semantic_batch, list) else []
        )
        if (
            _canonical_uia_action_pattern(values.get("action_pattern"))
            or batch_items
        ):
            actions.add("activate")
        objectives = [values.get("objective")]
        objectives.extend(
            item.get("objective")
            for item in batch_items
            if isinstance(item, dict)
        )
        for objective in objectives:
            normalized_objective = _normalized(objective)
            for action, pattern in _ACTION_PATTERNS:
                if re.search(pattern, normalized_objective):
                    actions.add(action)
    if name in {"fill", "fill_form_field", "type_text", "desktop_type"}:
        actions.update({"type", "write"})
    if name in {"press_key", "desktop_press"}:
        key = _normalized((arguments or {}).get("key")).replace(" ", "")
        if key in {
            "enter", "return", "ctrl+enter", "control+enter",
            "cmd+enter", "command+enter", "meta+enter",
        }:
            # Enter is a commit/activation boundary and commonly sends forms
            # or messages. An explicit no-send/no-activate constraint wins.
            actions.update({"activate", "send"})
        if key in {"delete", "del"}:
            actions.add("delete")
    if re.search(r"(?:^|_)(?:send|submit|publish|share)(?:_|$)", name):
        actions.add("send")
    if re.search(r"(?:^|_)(?:create|add)(?:_|$)", name) or name in {
        "write_file", "save_memory", "gmail_create_draft",
    }:
        actions.add("create")
    if (
        re.search(
            r"(?:^|_)(?:write|save|edit|update|modify|replace|overwrite)"
            r"(?:_|$)",
            name,
        )
        or name in {"gmail_create_draft"}
    ):
        actions.add("write")
    if re.search(r"(?:^|_)(?:move|rename)(?:_|$)", name):
        actions.add("move")
    if re.search(r"(?:^|_)(?:delete|remove)(?:_|$)", name) or name in {
        "kill_process", "close_browser_tab",
    }:
        actions.add("delete")
    if name in {
        "open_application", "open_uri", "open_in_editor", "open_browser",
        "open_new_tab", "open_new_browser_tab", "open_search_result",
    }:
        actions.add("open")
    if name in {
        "navigate", "navigate_browser", "go_back", "go_forward",
        "browser_back", "browser_forward", "select_browser_tab",
        "return_to_browser_opener",
    }:
        actions.add("navigate")
    if "search" in name or name in {"find_and_click"}:
        actions.add("search")
    return frozenset(actions)


def _constraint_target_key(value: object) -> str:
    normalized = _normalized(value)
    for _action, pattern in _NEGATED_ACTION_ALIASES:
        normalized = re.sub(pattern, " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"\b(?:the|a|an|this|that|these|those|le|la|les|un|une|des|"
        r"ce|cet|cette|ces|file|files|folder|folders|fichier|fichiers|"
        r"dossier|dossiers)\b",
        " ",
        normalized,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[^a-z0-9._:/\\-]+", "", normalized)


def _tool_semantic_target_values(tool_name: object, arguments: dict) -> tuple[str, ...]:
    name = str(tool_name or "").strip().lower()
    values = arguments if isinstance(arguments, dict) else {}
    preferred_keys = {
        "path", "paths", "file", "filename", "directory", "target",
        "target_path", "selector", "description", "objective", "label",
        "name", "url", "uri", "query", "recipient", "destination",
    }
    targets: list[str] = []

    def append(value) -> None:
        if isinstance(value, str) and value.strip():
            targets.append(value)
        elif isinstance(value, list):
            for item in value[:32]:
                append(item)

    for key in preferred_keys:
        append(values.get(key))
    if name == "activate_desktop_surface":
        for item in (
            values.get("semantic_batch")
            if isinstance(values.get("semantic_batch"), list) else []
        )[:8]:
            if isinstance(item, dict):
                append(item.get("objective"))
    return tuple(dict.fromkeys(
        target for target in targets if _constraint_target_key(target)
    ))


def _explicitly_forbidden_tool_action(
    strategy: ExecutionStrategy,
    tool_name: object,
    arguments: dict,
) -> str:
    contract = strategy.goal_contract_dict()
    constraints = (
        contract.get("global_constraints")
        if isinstance(contract, dict) else None
    )
    if not isinstance(constraints, list):
        constraints = list(
            explicit_negative_constraints(strategy.mission.objective)
        )
    forbidden = {
        str(item.get("value") or "")
        for item in constraints
        if isinstance(item, dict) and item.get("name") == "forbidden_action"
    }
    tool_actions = _tool_semantic_actions(tool_name, arguments)
    if any(
        isinstance(item, dict)
        and item.get("name") == "unresolved_negative_constraint"
        for item in constraints
    ):
        mechanism = mechanism_for_tool(strategy, str(tool_name or ""), arguments)
        if (
            str(tool_name or "").strip().lower() not in _OBSERVATION_TOOLS
            and (mechanism is None or mechanism.mutation)
        ):
            return "unresolved"
    global_match = next(iter(sorted(
        forbidden.intersection(tool_actions)
    )), "")
    if global_match:
        return global_match
    targeted = []
    for item in constraints:
        if not isinstance(item, dict) or item.get("name") != "forbidden_action_target":
            continue
        try:
            value = json.loads(str(item.get("value") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        action = str((value or {}).get("action") or "")
        target = str((value or {}).get("target") or "")
        if action in tool_actions and _constraint_target_key(target):
            targeted.append((action, target))
    if not targeted:
        return ""
    candidate_keys = tuple(
        _constraint_target_key(value)
        for value in _tool_semantic_target_values(tool_name, arguments)
        if _constraint_target_key(value)
    )
    if not candidate_keys:
        # An opaque keyboard/ref action cannot prove that it excludes a
        # specifically forbidden target, so explicit user intent wins.
        return sorted(action for action, _target in targeted)[0]
    for action, target in sorted(targeted):
        target_key = _constraint_target_key(target)
        if any(
            target_key == candidate
            or (
                min(len(target_key), len(candidate)) >= 3
                and (target_key in candidate or candidate in target_key)
            )
            for candidate in candidate_keys
        ):
            return action
    return ""


def tool_preflight(
    tool_name: str,
    arguments: dict,
    *,
    strategy: ExecutionStrategy | None = None,
) -> StrategyDecision | None:
    strategy = strategy or current_execution_strategy()
    if strategy is None:
        return None
    forbidden_action = _explicitly_forbidden_tool_action(
        strategy, tool_name, arguments,
    )
    if forbidden_action:
        return StrategyDecision(
            False,
            "explicit_negative_constraint_forbids_" + forbidden_action,
            node_id=strategy.goal.node_id,
        )
    applicable, reason_code = _tool_is_structurally_applicable(
        strategy, tool_name, arguments,
    )
    if not applicable:
        if reason_code in {
            "goal_effect_already_completed",
            "prerequisite_effect_already_completed",
        }:
            mechanism = mechanism_for_tool(strategy, tool_name, arguments)
            node = (
                strategy.node_by_id(mechanism.node_id)
                if mechanism is not None and mechanism.node_id else
                strategy._node_for_mechanism(mechanism, {
                    "tool": tool_name,
                    "arguments": arguments,
                })
                if mechanism is not None else None
            )
            retirement = (
                strategy._scope_retirement_reason(
                    mechanism.mechanism_id,
                    node.node_id,
                )
                if mechanism is not None and node is not None else ""
            )
            if retirement:
                reason_code = "mechanism_retired_after_" + retirement
        return StrategyDecision(
            False,
            reason_code,
            node_id=strategy.goal.node_id,
        )
    if tool_name in {"desktop_press", "desktop_type"}:
        node = strategy.goal
        if _structured_uia_frontier_state(strategy, node) == "unavailable":
            native = strategy.mechanism_for_layer(
                StrategyLayer.NATIVE_ACCESSIBILITY,
                node_id=node.node_id,
            )
            if native is not None and not (
                strategy._exhausted(native.mechanism_id)
                or strategy._scope_retirement_reason(
                    native.mechanism_id, node.node_id,
                )
            ):
                strategy.mark_inapplicable(
                    native,
                    reason_code="exact_uia_unavailable_before_effect",
                )
    try:
        from osia.tool_contracts import SideEffectClass, get_tool_spec

        spec = get_tool_spec(tool_name)
    except Exception:
        spec = None
    if spec is not None and spec.side_effect is SideEffectClass.READ_ONLY:
        return None
    if tool_name == "desktop_click":
        observation = strategy._latest_desktop_observation
        if (
            observation.get("target_resolution") == "exact_target"
            and bool(observation.get("window_surface_ref"))
            and observation.get("target_foreground") is not True
        ):
            return StrategyDecision(
                False,
                "exact_target_activation_required",
                node_id=strategy.goal.node_id,
            )
        # Internal selection begins with DOM when applicable, then layer 3.
        preview = strategy.preview(StrategyMechanism(
            "native_accessibility", StrategyLayer.NATIVE_ACCESSIBILITY,
            "desktop composite starts at the earliest applicable internal layer",
            application=strategy.goal.required_application,
            surface=strategy.goal.required_surface,
            account=strategy.goal.required_account,
            covers_goal=False,
            mutation=True,
            node_id=strategy.goal.node_id,
        ))
        return preview
    mechanism = mechanism_for_tool(strategy, tool_name, arguments)
    if mechanism is None:
        return None
    decision = strategy.select(
        mechanism, {"tool": tool_name, "arguments": arguments},
    )
    if (
        tool_name == "activate_desktop_surface"
        and arguments.get("semantic_batch") is not None
        and decision.allowed
        and decision.attempt_id
    ):
        try:
            from osia.semantic_batch import normalize_semantic_batch

            batch_plan, _reason = normalize_semantic_batch(
                surface_ref=arguments.get("surface_ref"),
                objective=arguments.get("objective"),
                action_pattern=arguments.get("action_pattern"),
                observable_effect=arguments.get("observable_effect"),
                semantic_batch=arguments.get("semantic_batch"),
            )
        except Exception:
            batch_plan = {}
        attempt = strategy.attempt_by_id(decision.attempt_id)
        if attempt is not None and batch_plan:
            attempt.semantic_batch_id = str(
                batch_plan.get("semantic_batch_id") or ""
            )
            attempt.semantic_batch_effects = [
                dict(item.get("observable_effect") or {})
                for item in batch_plan.get("items") or []
            ]
            attempt.semantic_checkpoint_count = 1
    return decision


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
    receipt = result.get("receipt") if isinstance(result, dict) else None
    typed_app_open = bool(
        tool_name == "is_running"
        and isinstance(receipt, dict)
        and receipt.get("evidence_kind")
        == "exact_independent_desktop_state"
        and receipt.get("evidence_source") == "inspect_desktop_surfaces"
        and receipt.get("semantic_postcondition") == "application_open"
    )
    if (
        tool_name in {
            "inspect_desktop_surfaces", "request_visual_observation",
        }
        or typed_app_open
    ):
        receipt = result.get("receipt") if isinstance(result, dict) else None
        receipt = receipt if isinstance(receipt, dict) else {}
        scene = result.get("scene") if isinstance(result, dict) else None
        scene = scene if isinstance(scene, dict) else {}
        if tool_name == "inspect_desktop_surfaces" and scene:
            strategy._latest_desktop_observation = {
                "application": _bounded(scene.get("application"), 160),
                "requested_application": _bounded(
                    scene.get("requested_application"), 160,
                ),
                "window_surface_ref": _bounded(
                    scene.get("window_surface_ref"), 160,
                ),
                "window_generation": _bounded(
                    scene.get("window_generation"), 160,
                ),
                "accessibility_quality": _bounded(
                    scene.get("accessibility_quality"), 80,
                ),
                "target_resolution": _bounded(
                    scene.get("target_resolution"), 80,
                ),
                "target_foreground": scene.get("target_foreground") is True,
                "target_minimized": scene.get("target_minimized") is True,
                "target_identity_evidence": _bounded(
                    scene.get("target_identity_evidence"), 240,
                ),
                "foreground_application": _bounded(
                    scene.get("foreground_application"), 160,
                ),
                "focus_observable": scene.get("focus_observable") is True,
                "focused_surface_ref": _bounded(
                    scene.get("focused_surface_ref"), 160,
                ),
                "uia_frontier_observed": True,
                "uia_actionable_frontier": (
                    _desktop_uia_frontier_from_scene(scene)
                ),
                "uia_text_surfaces": _desktop_text_surfaces_from_scene(scene),
                "observed_at": (
                    float(scene.get("observed_at"))
                    if isinstance(scene.get("observed_at"), (int, float))
                    and not isinstance(scene.get("observed_at"), bool)
                    else time.time()
                ),
            }
            _reconcile_observed_keyboard_substep(strategy, scene)
            requested_application = str(
                scene.get("requested_application")
                or scene.get("application")
                or ""
            )
            resolution = str(scene.get("target_resolution") or "")
            if (
                resolution == "exact_target"
                and scene.get("window_surface_ref")
            ):
                strategy.remember_completed_effect(
                    "application_open",
                    target_application=requested_application,
                    target_surface=str(scene.get("window_surface_ref") or ""),
                    evidence="fresh_exact_desktop_target",
                    observed_at=float(
                        strategy._latest_desktop_observation.get("observed_at")
                        or 0.0
                    ),
                )
            elif (
                requested_application
                and not scene.get("window_surface_ref")
                and resolution in {
                    "target_missing", "target_not_found",
                    "semantic_target_surface_not_found",
                }
                and int(scene.get("target_candidate_count") or 0) == 0
            ):
                strategy.invalidate_completed_effect(
                    "application_open",
                    target_application=requested_application,
                )
        safe_reason = _bounded(receipt.get("reason_code"), 64)
        safe_outcome = _bounded(receipt.get("outcome"), 32)
        safe_surface = _bounded(
            receipt.get("surface_ref") or receipt.get("window_surface_ref"), 80,
        )
        safe_epoch = receipt.get("structural_epoch")
        pending_refresh = (
            _pending_uia_substep(strategy, strategy.goal.node_id)
            if tool_name == "inspect_desktop_surfaces" else {}
        )
        token = ":".join(str(item or "") for item in (
            tool_name,
            safe_reason,
            safe_outcome,
            safe_surface,
            safe_epoch if isinstance(safe_epoch, int) else "",
            pending_refresh.get("attempt_id") if pending_refresh else "",
        ))
        strategy.observe(
            "bounded semantic desktop observation",
            evidence_token=token,
        )
        if tool_name == "inspect_desktop_surfaces" and scene:
            strategy._latest_desktop_observation["evidence_revision"] = (
                strategy._evidence_revision
            )
        exact_postcondition = _bounded(receipt.get("postcondition"), 320)
        if (
            tool_name == "inspect_desktop_surfaces"
            and safe_outcome == "confirmed_success"
            and safe_reason in {
                "exact_desktop_primitive_state_change",
                "exact_desktop_observable_effect_change",
                "exact_desktop_target_focus_observed",
            }
            and exact_postcondition
            and strategy.frozen_reason
        ):
            return strategy.reconcile(
                succeeded=True,
                evidence=token,
                primitive_only=(
                    safe_reason in {
                        "exact_desktop_primitive_state_change",
                        "exact_desktop_observable_effect_change",
                    }
                ),
                evidence_details={
                    "evidence_kind": "exact_independent_desktop_state",
                    "evidence_source": tool_name,
                    "evidence_outcome": safe_outcome,
                    "reason_code": safe_reason,
                    "postcondition": exact_postcondition,
                    "strategy_attempt_id": receipt.get("strategy_attempt_id"),
                    "semantic_node_id": receipt.get("semantic_node_id"),
                    "mechanism_id": receipt.get("mechanism_id"),
                    "attempted_effect": receipt.get("attempted_effect"),
                    "primitive_postcondition": receipt.get(
                        "primitive_postcondition"
                    ),
                    "observable_effect": receipt.get("observable_effect"),
                    "composite_id": receipt.get("composite_id"),
                    "substep_id": receipt.get("substep_id"),
                    "substep_index": receipt.get("substep_index"),
                    "observed_application": scene.get("application"),
                    "observed_surface": scene.get("window_surface_ref"),
                    "window_generation": scene.get("window_generation"),
                    "structural_epoch": scene.get("structural_epoch"),
                    "observed_at": receipt.get("observed_at"),
                    "surface_continuity": receipt.get(
                        "surface_continuity"
                    ),
                    "previous_window_surface_ref": receipt.get(
                        "previous_window_surface_ref"
                    ),
                    "previous_window_generation": receipt.get(
                        "previous_window_generation"
                    ),
                    "rebound_window_surface_ref": receipt.get(
                        "rebound_window_surface_ref"
                    ),
                    "rebound_window_generation": receipt.get(
                        "rebound_window_generation"
                    ),
                    "rebound_target_surface_ref": receipt.get(
                        "rebound_target_surface_ref"
                    ),
                    "no_partial_effect": False,
                },
            )
        if (
            tool_name == "inspect_desktop_surfaces"
            and safe_outcome == "confirmed_absence"
            and safe_reason
            == "exact_desktop_primitive_absent_no_partial_effect"
            and exact_postcondition
            and receipt.get("no_partial_effect") is True
            and strategy.frozen_reason
        ):
            return strategy.reconcile(
                succeeded=False,
                evidence=token,
                primitive_only=True,
                evidence_details={
                    "evidence_kind": "exact_independent_desktop_state",
                    "evidence_source": tool_name,
                    "evidence_outcome": safe_outcome,
                    "reason_code": safe_reason,
                    "postcondition": exact_postcondition,
                    "strategy_attempt_id": receipt.get(
                        "strategy_attempt_id"
                    ),
                    "semantic_node_id": receipt.get("semantic_node_id"),
                    "mechanism_id": receipt.get("mechanism_id"),
                    "attempted_effect": receipt.get("attempted_effect"),
                    "primitive_postcondition": receipt.get(
                        "primitive_postcondition"
                    ),
                    "observable_effect": receipt.get("observable_effect"),
                    "composite_id": receipt.get("composite_id"),
                    "substep_id": receipt.get("substep_id"),
                    "substep_index": receipt.get("substep_index"),
                    "observed_application": scene.get("application"),
                    "observed_surface": scene.get("window_surface_ref"),
                    "window_generation": scene.get("window_generation"),
                    "structural_epoch": scene.get("structural_epoch"),
                    "observed_at": receipt.get("observed_at"),
                    "surface_continuity": receipt.get(
                        "surface_continuity"
                    ),
                    "previous_window_surface_ref": receipt.get(
                        "previous_window_surface_ref"
                    ),
                    "previous_window_generation": receipt.get(
                        "previous_window_generation"
                    ),
                    "rebound_window_surface_ref": receipt.get(
                        "rebound_window_surface_ref"
                    ),
                    "rebound_window_generation": receipt.get(
                        "rebound_window_generation"
                    ),
                    "rebound_target_surface_ref": receipt.get(
                        "rebound_target_surface_ref"
                    ),
                    "no_partial_effect": True,
                },
            )
        if (
            (tool_name == "inspect_desktop_surfaces" or typed_app_open)
            and safe_outcome == "confirmed_success"
            and safe_reason == "exact_final_state_observation"
            and exact_postcondition
        ):
            if strategy.frozen_reason:
                frozen = strategy.node_by_id(strategy._frozen_node_id)
                frozen_attempt = strategy.attempt_by_id(
                    strategy._frozen_attempt_id
                )
                if (
                    frozen is not None
                    and (
                        exact_postcondition in frozen.required_postconditions
                        or (
                            frozen_attempt is not None
                            and exact_postcondition
                            == frozen_attempt.attempted_effect
                        )
                    )
                ):
                    return strategy.reconcile(
                        succeeded=True,
                        evidence=token,
                        evidence_details={
                            "evidence_kind": "exact_independent_desktop_state",
                            "evidence_source": (
                                receipt.get("evidence_source") or tool_name
                            ),
                            "evidence_outcome": safe_outcome,
                            "reason_code": safe_reason,
                            "postcondition": exact_postcondition,
                            "observed_target": receipt.get("observed_target"),
                            "observed_application": scene.get("application"),
                            "observed_surface": scene.get(
                                "window_surface_ref",
                            ),
                            "window_generation": scene.get(
                                "window_generation",
                            ),
                            "structural_epoch": scene.get(
                                "structural_epoch",
                            ),
                            "observed_at": receipt.get("observed_at"),
                            "no_partial_effect": False,
                        },
                    )
            return strategy.verify_postcondition(
                token,
                postcondition=exact_postcondition,
            )
        if (
            tool_name == "inspect_desktop_surfaces"
            and safe_outcome == "confirmed_absence"
            and safe_reason == "exact_postcondition_absent_no_partial_effect"
            and exact_postcondition
            and receipt.get("no_partial_effect") is True
            and strategy.frozen_reason
        ):
            frozen = strategy.node_by_id(strategy._frozen_node_id)
            frozen_attempt = strategy.attempt_by_id(
                strategy._frozen_attempt_id
            )
            if (
                frozen is not None
                and (
                    exact_postcondition in frozen.required_postconditions
                    or (
                        frozen_attempt is not None
                        and exact_postcondition
                        == frozen_attempt.attempted_effect
                    )
                )
            ):
                strategy.reconcile(
                    succeeded=False,
                    evidence=token,
                    evidence_details={
                        "evidence_kind": "exact_independent_desktop_state",
                        "evidence_source": tool_name,
                        "evidence_outcome": safe_outcome,
                        "reason_code": safe_reason,
                        "postcondition": exact_postcondition,
                        "observed_target": receipt.get("observed_target"),
                        "observed_application": scene.get("application"),
                        "observed_surface": scene.get("window_surface_ref"),
                        "window_generation": scene.get("window_generation"),
                        "structural_epoch": scene.get("structural_epoch"),
                        "observed_at": receipt.get("observed_at"),
                        "no_partial_effect": True,
                    },
                )
        return False
    digest = hashlib.sha256(_bounded(result, 2000).encode("utf-8", errors="replace")).hexdigest()[:16]
    strategy.observe(f"read-only observation from {tool_name}", evidence_token=f"{tool_name}:{digest}")
    text = _normalized(result)
    negative = re.search(
        r"\b(no|false|unknown|unavailable|not found|not installed|cannot verify)\b",
        text,
    )
    candidate_nodes = strategy.pending_nodes()
    goal = strategy.node_by_id(strategy._frozen_node_id) if strategy.frozen_reason else None
    if goal is None:
        ranked = []
        for candidate in candidate_nodes:
            tokens = [
                token for token in _normalized(candidate.target_object).split()
                if len(token) >= 3
            ]
            ranked.append((sum(1 for token in tokens[:6] if token in text), -candidate.order, candidate))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        goal = ranked[0][2] if ranked else strategy.goal
    target_tokens = [
        token for token in _normalized(goal.target_object).split()
        if len(token) >= 3
    ]
    target_visible = bool(target_tokens) and all(token in text for token in target_tokens[:6])
    affirmative = bool(re.search(r"\b(yes|true|verified|visible|running)\b", text))
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
        tool_name == "has_window"
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
        elif negative and (
            (
                tool_name in {"search_files", "list_files"}
                and goal.requested_action == "search"
            )
            or (
                tool_name == "has_window"
                and goal.requested_action == "open"
                and not goal.required_surface
            )
            or (
                tool_name == "steam_game_state"
                and goal.requested_action == "open"
            )
            or tool_name == "web_state"
        ):
            strategy.reconcile(succeeded=False, evidence=f"{tool_name}:{digest}")
        return False
    if proves_goal:
        return strategy.verify_postcondition(
            f"{tool_name}:{digest}",
            postcondition=goal.observable_success_postcondition,
        )
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
    strategy: ExecutionStrategy | None = None,
    durable_attempt_id: str = "",
) -> None:
    strategy = strategy or current_execution_strategy()
    if strategy is None:
        return
    record_tool_observation(
        tool_name, result, is_error=is_error, strategy=strategy,
    )
    if decision is None or not decision.allowed or not decision.attempt_id:
        if tool_name in _OBSERVATION_TOOLS:
            return
        if tool_name == "activate_desktop_surface":
            # A wrapper/owner state without the already-bound strategy decision
            # is operation-level evidence only. Never synthesize a semantic
            # UIA mutation or anti-replay barrier from it.
            return
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
    receipt = result.get("receipt") if isinstance(result, dict) else None
    receipt = receipt if isinstance(receipt, dict) else {}
    if tool_name == "activate_desktop_surface":
        attempt = strategy.attempt_by_id(decision.attempt_id)
        actionability = receipt.get("actionability")
        actionability = (
            actionability if isinstance(actionability, dict) else {}
        )
        if attempt is not None:
            selected_pattern = _canonical_uia_action_pattern(
                actionability.get("selected_pattern")
                or receipt.get("mechanism")
                or attempt.action_pattern,
            )
            if selected_pattern:
                attempt.action_pattern = selected_pattern
            if actionability:
                attempt.actionability_evidence = {
                    "semantic_match": actionability.get(
                        "semantic_match"
                    ) is True,
                    "actionable_match": actionability.get(
                        "actionable_match"
                    ) is True,
                    "selected_pattern_available": actionability.get(
                        "selected_pattern_available"
                    ) is True,
                    "dispatch_preflight_result": _bounded(
                        actionability.get("dispatch_preflight_result"), 80,
                    ),
                    "failed_before_effect_reason": _bounded(
                        actionability.get("failed_before_effect_reason"), 120,
                    ),
                    "stale_rebind_status": _bounded(
                        actionability.get("stale_rebind_status"), 80,
                    ),
                    "retirement_state": (
                        "retired_failed_before_effect"
                        if normalized_outcome in {
                            "confirmed_failure_before_effect",
                            "failed_before_effect",
                            "rejected_before_execution",
                        }
                        else _bounded(
                            actionability.get("retirement_state"), 80,
                        )
                    ),
                    "automation_id": _bounded(
                        actionability.get("automation_id"), 160,
                    ),
                    "fresh_supported_patterns": [
                        pattern
                        for item in (
                            actionability.get("fresh_supported_patterns") or []
                        )[:8]
                        if (pattern := _canonical_uia_action_pattern(item))
                    ],
                }
    if normalized_outcome in {
        "pending", "outcome_unknown", "unknown_after_start",
    } or (
        normalized_outcome == "success_unverified"
        and tool_name == "activate_desktop_surface"
    ):
        primitive_receipt = (
            receipt.get("primitive")
            if receipt else None
        )
        attempt = strategy.attempt_by_id(decision.attempt_id)
        if (
            tool_name == "activate_desktop_surface"
            and not isinstance(primitive_receipt, dict)
            and durable_attempt_id
            and attempt is not None
        ):
            try:
                from osia.tool_attempts import get_tool_attempt_store

                durable = (
                    get_tool_attempt_store().get_attempt(durable_attempt_id)
                    or {}
                )
                journal_entry = next((
                    item
                    for item in reversed(
                        durable.get("primitive_journal") or []
                    )
                    if item.get("effect_role") == "semantic"
                    and item.get("lifecycle") in {
                        "dispatch_started", "dispatch_returned",
                        "receipt_bound", "verified", "unknown",
                    }
                ), None)
            except Exception:
                journal_entry = None
            if isinstance(journal_entry, dict):
                identity = (
                    attempt.target_surface_identity
                    if isinstance(attempt.target_surface_identity, dict)
                    else {}
                )
                primitive_receipt = {
                    "kind": "desktop_uia_primitive",
                    "mechanism": str(
                        journal_entry.get("uia_pattern") or ""
                    ).casefold(),
                    "observable_effect": dict(attempt.observable_effect),
                    "dispatched_at": float(
                        journal_entry.get("dispatch_started_at") or 0.0
                    ),
                    "before": {
                        "application": (
                            identity.get("application")
                            or attempt.target_application
                        ),
                        "window_surface_ref": identity.get(
                            "window_surface_ref"
                        ),
                        "window_generation": identity.get(
                            "window_generation"
                        ),
                        "structural_epoch": identity.get(
                            "structural_epoch", 0
                        ),
                        "observed_at": identity.get("observed_at", 0.0),
                        "target": dict(identity),
                    },
                }
        if (
            tool_name == "activate_desktop_surface"
            and isinstance(primitive_receipt, dict)
        ):
            if attempt is not None:
                try:
                    dispatched_at = float(
                        primitive_receipt.get("dispatched_at") or 0.0
                    )
                except (TypeError, ValueError, OverflowError):
                    dispatched_at = 0.0
                if dispatched_at > 0.0:
                    attempt.dispatched_at = dispatched_at
                primitive_mechanism = _normalized(
                    primitive_receipt.get("mechanism"),
                )
                if (
                    primitive_mechanism in {
                        "selectionitem", "toggle", "expandcollapse",
                    }
                    and not attempt.primitive_postcondition
                ):
                    attempt.primitive_postcondition = (
                        "exact target control state changed for "
                        + attempt.substep_id
                    )
                    attempt.attempted_effect = attempt.primitive_postcondition
                dispatched_surface = _bounded(
                    receipt.get("surface_ref"), 160,
                )
                if dispatched_surface:
                    attempt.target_surface = dispatched_surface
                captured_identity = _capture_desktop_surface_identity(
                    attempt.target_surface,
                    primitive_receipt=primitive_receipt,
                )
                if captured_identity:
                    merged_identity = dict(attempt.target_surface_identity)
                    merged_identity.update({
                        key: value
                        for key, value in captured_identity.items()
                        if value not in (None, "", [], 0, 0.0)
                        or key == "schema_version"
                    })
                    attempt.target_surface_identity = (
                        _normalize_desktop_surface_identity(merged_identity)
                    )
                receipt_effect = normalize_desktop_observable_effect(
                    primitive_receipt.get("observable_effect"),
                )
                if receipt_effect == normalize_desktop_observable_effect(
                    attempt.observable_effect,
                ):
                    attempt.observable_effect = receipt_effect
        if (
            tool_name == "activate_desktop_surface"
            and isinstance(primitive_receipt, dict)
            and primitive_receipt.get("kind") == "desktop_window_focus"
        ):
            attempt = strategy.attempt_by_id(decision.attempt_id)
            if attempt is not None:
                # Window activation is a typed preparatory effect, not proof of
                # the enclosing Desktop goal or a generic UIA control substep.
                attempt.attempted_effect = "application_foreground"
                attempt.target_surface = _bounded(
                    receipt.get("surface_ref"), 160,
                ) or attempt.target_surface
            strategy.record(
                decision,
                "ambiguous",
                failure_reason=(reason_code or "preparatory_effect_possible"),
                primitive_receipt=primitive_receipt,
                semantic_barrier=False,
            )
            return
        if (
            tool_name == "activate_desktop_surface"
            and not (
                isinstance(primitive_receipt, dict)
                and primitive_receipt.get("kind") == "desktop_uia_primitive"
            )
        ):
            strategy.record(
                decision,
                "failed_before_effect",
                failure_reason="semantic_dispatch_witness_missing",
            )
            return
        strategy.record(
            decision,
            "ambiguous",
            failure_reason=(
                reason_code
                or "desktop_primitive_requires_exact_reconciliation"
            ),
            primitive_receipt=(
                primitive_receipt
                if isinstance(primitive_receipt, dict) else None
            ),
        )
        return
    if reason_code in {"stale_semantic_reference", "stale_reference"}:
        strategy.record(decision, "stale", failure_reason=reason_code)
        return
    if is_error:
        failure_reason = _bounded(reason_code, 120)
        receipt_reason = _bounded(receipt.get("reason_code"), 120)
        if not failure_reason or failure_reason == "tool_failed":
            failure_reason = receipt_reason or "tool_failed"
        strategy.record(decision, "failed", failure_reason=failure_reason)
        if reason_code == "permission_user_denied":
            node = strategy.node_by_id(decision.node_id)
            if node is not None:
                node.status = "blocked"
            strategy.terminal_blocker = "permission_user_denied"
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
