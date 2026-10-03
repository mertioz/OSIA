from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

from dataclasses import fields

from osia.capability_federation import CapabilityDescriptor, federated_capabilities
from osia.execution_strategy import (
    ExecutionStrategy,
    StrategyLayer,
    StrategyMechanism,
    build_execution_strategy,
    derive_semantic_goal,
    tool_preflight,
)
from osia.tool_contracts import static_tool_prompt_block
from osia.turn_terminality import TurnTerminalityController


_ROOT = Path(__file__).resolve().parents[1]
_PHASE0_PROMPT_SHA256 = (
    "d5c512d9b4ff0a86356d57ae3209efe480c4819c70d8a9ffdfcc773a7f740271"
)


def test_importing_phase1_sidecars_is_strictly_observational_in_fresh_process():
    script = r'''
import hashlib
import json
from dataclasses import asdict, fields
from osia.capability_federation import CapabilityDescriptor, federated_capabilities
from osia.mcp_client.registry import registered_names
from osia.tool_contracts import (
    STATIC_TOOL_REGISTRY, dynamic_tool_specs, static_tool_prompt_block,
)

def snapshot():
    openai = federated_capabilities("openai")
    account = federated_capabilities("chatgpt_account")
    prompt = static_tool_prompt_block().encode("utf-8")
    return {
        "static_names": STATIC_TOOL_REGISTRY.names(),
        "dynamic_names": tuple(item.name for item in dynamic_tool_specs()),
        "mcp_names": tuple(registered_names()),
        "openai": tuple(asdict(item) for item in openai),
        "account": tuple(asdict(item) for item in account),
        "descriptor_fields": tuple(item.name for item in fields(CapabilityDescriptor)),
        "descriptor_repr": repr(openai[0]),
        "descriptor_hash": hash(openai[0]),
        "prompt": prompt.decode("utf-8"),
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
    }

before = snapshot()
import osia.goal_contract
import osia.capability_adapters
import benchmarks.wor260.schema
after = snapshot()
print(json.dumps({
    "equal": before == after,
    "chat_count": len(after["openai"]),
    "account_count": len(after["account"]),
    "dynamic_count": len(after["dynamic_names"]),
    "prompt_sha256": after["prompt_sha256"],
}))
'''
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = json.loads(result.stdout)

    assert evidence == {
        "equal": True,
        "chat_count": 92,
        "account_count": 95,
        "dynamic_count": 0,
        "prompt_sha256": _PHASE0_PROMPT_SHA256,
    }


def test_current_chat_and_chatgpt_account_identity_universes_are_unchanged():
    chat = federated_capabilities("openai")
    account = federated_capabilities("chatgpt_account")
    chat_ids = tuple(item.identity for item in chat)
    account_osia_ids = tuple(
        item.identity for item in account if item.source == "osia"
    )
    account_native_ids = tuple(
        item.identity
        for item in account
        if item.source in {"provider_native", "provider_hosted"}
    )

    assert len(chat_ids) == 92
    assert account_osia_ids == chat_ids
    assert account_native_ids == ("webSearch", "commandExecution", "fileChange")
    assert len(account) == 95
    assert chat_ids == tuple(sorted(chat_ids))


def test_phase0_prompt_tool_block_is_byte_for_byte_identical():
    prompt = static_tool_prompt_block().encode("utf-8")

    assert len(prompt) == 13_607
    assert len(prompt.decode("utf-8").splitlines()) == 94
    assert hashlib.sha256(prompt).hexdigest() == _PHASE0_PROMPT_SHA256


def test_legacy_descriptor_dataclass_serialization_shape_is_unchanged():
    assert tuple(item.name for item in fields(CapabilityDescriptor)) == (
        "source",
        "provider",
        "identity",
        "available",
        "side_effect_class",
        "idempotence",
        "deterministic_strength",
        "mechanism",
        "permission_requirement",
        "observability",
        "reconciliation",
        "equivalence_class",
        "latency_hint",
        "cost_hint",
    )


def test_strategy_and_terminality_signatures_receive_no_phase1_inputs():
    assert str(inspect.signature(build_execution_strategy)) == (
        "(request: 'str', available_tools: 'Iterable[str]' = (), *, "
        "postcondition: 'str' = '', postconditions: 'Iterable[str]' = (), "
        "semantic_context: 'str' = '') "
        "-> 'ExecutionStrategy'"
    )
    assert str(inspect.signature(ExecutionStrategy)) == (
        "(mission: 'SemanticMission | SemanticActionNode', "
        "mechanisms: 'Iterable[StrategyMechanism]' = (), *, "
        "available_tools: 'Iterable[str]' = ())"
    )
    assert str(inspect.signature(TurnTerminalityController)) == (
        "(*, turn_id: 'str', objective: 'str', required_postconditions: "
        "'Iterable[str]' = (), execution_required: 'bool', storage_dir: "
        "'str | Path | None' = None) -> 'None'"
    )


def test_execution_strategy_surface_equivalence_decision_remains_legacy():
    goal = derive_semantic_goal(
        "Open Steam, go to my Library and search for Subnautica"
    )
    store_dom = StrategyMechanism(
        "dom:steam:store",
        StrategyLayer.DOM,
        "controlled store DOM",
        application="steam",
        surface="store",
        account="public",
        mutation=True,
    )
    strategy = ExecutionStrategy(goal, (store_dom,))

    decision = strategy.select(
        store_dom,
        {"url": "https://store.steampowered.com/search/?term=Subnautica"},
    )

    assert decision.allowed is False
    assert decision.reason_code == "required_surface_changed"


def test_active_runtime_modules_do_not_import_phase1_sidecars():
    active_paths = (
        _ROOT / "osia" / "agent.py",
        _ROOT / "osia" / "execution_strategy.py",
        _ROOT / "osia" / "turn_terminality.py",
        _ROOT / "osia" / "capability_federation.py",
    )
    for path in active_paths:
        source = path.read_text(encoding="utf-8")
        # WOR-291+: goal_contract is a canonical runtime module now; the
        # phase-1 benchmark sidecars below must stay non-runtime.
        assert "import osia.capability_adapters" not in source
        assert "from osia.capability_adapters" not in source
        assert "benchmarks.wor260" not in source


def test_tool_preflight_signature_and_no_strategy_path_are_unchanged():
    assert str(inspect.signature(tool_preflight)) == (
        "(tool_name: 'str', arguments: 'dict', *, strategy: "
        "'ExecutionStrategy | None' = None) -> 'StrategyDecision | None'"
    )
    assert tool_preflight("read_file", {"path": "fixture"}, strategy=None) is None
