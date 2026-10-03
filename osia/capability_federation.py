"""Canonical planner-facing inventory of capabilities available to OSIA.

Availability and execution authorization are deliberately separate here.  The
inventory depends on genuine runtime availability (platform, provider,
connected connectors) but never on the request, the execution surface, or
Standard/delegated/Full Access permission state.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping

from osia.tool_contracts import (
    ConfirmationPolicy,
    SideEffectClass,
    dynamic_tool_specs,
    effective_static_tool_specs,
)


@dataclass(frozen=True)
class CapabilityDescriptor:
    source: str
    provider: str
    identity: str
    available: bool
    side_effect_class: str
    idempotence: str
    deterministic_strength: str
    mechanism: str
    permission_requirement: str
    observability: str
    reconciliation: str
    equivalence_class: str = ""
    latency_hint: str = ""
    cost_hint: str = ""


_CHATGPT_ACCOUNT_NATIVE = (
    CapabilityDescriptor(
        source="provider_hosted",
        provider="chatgpt_account",
        identity="webSearch",
        available=True,
        side_effect_class="read_only",
        idempotence="idempotent",
        deterministic_strength="provider_hosted",
        mechanism="direct",
        permission_requirement="none",
        observability="typed_item_lifecycle",
        reconciliation="completed_item",
        equivalence_class="web_search",
        latency_hint="network",
        cost_hint="provider_managed",
    ),
    CapabilityDescriptor(
        source="provider_native",
        provider="chatgpt_account",
        identity="commandExecution",
        available=True,
        side_effect_class="dynamically_classified",
        idempotence="dynamic",
        deterministic_strength="native_command",
        mechanism="direct_brokered",
        permission_requirement="canonical_policy",
        observability="approval_and_typed_item_lifecycle",
        reconciliation="durable_receipt",
        equivalence_class="local_command",
        latency_hint="local_process",
    ),
    CapabilityDescriptor(
        source="provider_native",
        provider="chatgpt_account",
        identity="fileChange",
        available=True,
        side_effect_class="persistent_local",
        idempotence="conditionally_idempotent",
        deterministic_strength="native_patch",
        mechanism="direct_brokered",
        permission_requirement="canonical_policy",
        observability="approval_and_typed_item_lifecycle",
        reconciliation="durable_receipt",
        equivalence_class="local_file_change",
        latency_hint="local_filesystem",
    ),
)


def _permission_requirement(spec) -> str:
    if spec.permission_category and spec.permission_category != "none":
        return spec.permission_category
    if spec.confirmation is not ConfirmationPolicy.NO_CONFIRMATION:
        return spec.confirmation.value
    return "none"


def _equivalence_class(name: str) -> str:
    if name in {"search_web", "web_search"}:
        return "web_search"
    if name in {"bash", "run_command"}:
        return "local_command"
    if name in {
        "write_file", "edit_file_lines", "copy_file", "rename_file",
        "create_directory", "delete_file", "delete_directory",
    }:
        return "local_file_change"
    return name


def _tool_descriptor(spec, *, provider: str, source: str) -> CapabilityDescriptor:
    effectful = spec.side_effect is not SideEffectClass.READ_ONLY
    return CapabilityDescriptor(
        source=source,
        provider=provider,
        identity=spec.name,
        available=True,
        side_effect_class=spec.side_effect.value,
        idempotence=spec.idempotency.value,
        deterministic_strength=spec.deterministic_test_status.value,
        mechanism="direct",
        permission_requirement=_permission_requirement(spec),
        observability=spec.observability.value,
        reconciliation="durable_receipt" if effectful else "typed_result",
        equivalence_class=_equivalence_class(spec.name),
    )


def federated_capabilities(provider: str) -> tuple[CapabilityDescriptor, ...]:
    """Return the actual capability view of the current runtime.

    WOR-300: the view is composed from genuine runtime availability alone
    (provider support, platform capability, connector/dynamic registration).
    Request wording, GoalContract/ExecutionStrategy state and the engine
    surface never change it.

    This function intentionally has no permission-mode argument and does not
    read the permission store.  Standard, grants, and Full Access therefore
    receive the same identities whenever platform/provider availability is the
    same; the permission broker acts only after one capability is selected.
    """

    provider_id = str(provider or "")
    capabilities = [
        _tool_descriptor(spec, provider="osia", source="osia")
        for spec in effective_static_tool_specs()
        if not spec.providers or provider_id in spec.providers
    ]
    capabilities.extend(
        _tool_descriptor(spec, provider=spec.connector_id, source="mcp")
        for spec in dynamic_tool_specs()
        if not spec.providers or provider_id in spec.providers
    )
    if provider_id == "chatgpt_account":
        capabilities.extend(_CHATGPT_ACCOUNT_NATIVE)
    return tuple(sorted(
        capabilities,
        key=lambda item: (item.source, item.provider, item.identity),
    ))


def capability_available(provider: str, source: str, identity: str) -> bool:
    return any(
        item.available
        and item.source == str(source or "")
        and item.identity == str(identity or "")
        for item in federated_capabilities(provider)
    )


def mutation_equivalence_arguments(
    tool_name: str, arguments: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return a private-input equivalence key for anti-double-dispatch.

    Callers persist only a keyed fingerprint of this mapping.  It is purposely
    conservative for local files: after a native patch starts, a second local
    mutation of the same path is frozen even if represented by another tool.
    """

    name = str(tool_name or "")
    values = arguments if isinstance(arguments, Mapping) else {}
    if name in {"bash", "run_command"}:
        # Shell whitespace and quoting are executable data: collapsing spaces
        # can conflate distinct quoted arguments. The runtime's fixed default
        # working directory also belongs to the operation, while a timeout is
        # only an observation budget and cannot authorize replay.
        command = str(values.get("command") or "")
        if not command.strip():
            return None
        cwd = values.get("cwd") or str(Path(__file__).resolve().parent.parent)
        return {
            "class": "local_command",
            "command": command,
            "cwd": os.path.normcase(os.path.abspath(os.fspath(cwd))),
            "arguments": json.dumps(
                {key: value for key, value in values.items()
                 if key not in {"command", "cwd", "timeout"}},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                default=lambda _value: "<unsupported>",
            ),
        }
    path_field = {
        "write_file": "path",
        "edit_file_lines": "path",
        "delete_file": "path",
        "delete_directory": "path",
        "create_directory": "path",
        "rename_file": "new_path",
        "copy_file": "dst",
    }.get(name)
    if path_field:
        path = str(values.get(path_field) or "").strip()
        if path:
            canonical = os.path.normcase(os.path.abspath(os.path.normpath(path)))
            return {"class": "local_file_change", "path": canonical}
    return None


__all__ = [
    "CapabilityDescriptor",
    "capability_available",
    "federated_capabilities",
    "mutation_equivalence_arguments",
]
