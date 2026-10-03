"""The selected model decides when a canonical Chat turn is finished."""

from __future__ import annotations

import types

from osia.agent import OSIAAgent
from osia.outcome_contract import new_outcome_contract
from osia.turn_terminality import TurnTerminalityController

ANDROID_REQUEST = "Create an Android app, build it and install it on my Pixel"


def test_selected_model_final_does_not_require_a_static_outcome_reprompt(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path))
    agent = OSIAAgent()
    calls = []
    final = "I have not verified the APK installation."

    def fake_run(self, text, **kwargs):
        calls.append(text)
        assert self._terminality_controller is not None
        self.messages.append({"role": "assistant", "content": final})
        return final

    agent.run = types.MethodType(fake_run, agent)
    result = agent.run_terminal_turn(
        ANDROID_REQUEST,
        turn_id="selected-model-final",
        execution_required=False,
        required_postconditions=("APK installed on Pixel",),
    )

    assert calls == [ANDROID_REQUEST]
    assert result == final
    terminal = agent.terminality_result("selected-model-final")
    assert terminal is not None
    assert terminal["terminal_state"] == "success_verified"


def test_model_final_and_optional_outcome_notes_do_not_create_tool_facts(tmp_path):
    controller = TurnTerminalityController(
        turn_id="facts-stay-factual",
        objective=ANDROID_REQUEST,
        required_postconditions=("APK installed on Pixel",),
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.bind_outcome_contract(("APK installed on Pixel",))

    final = "I have not verified the APK installation."
    assert controller.accept_completion(final) == final

    state = controller.snapshot()
    assert state["completion_text"] == final
    assert state["typed_evidence"] == []
    assert state["proven_postconditions"] == {}
    assert controller.evaluate().terminal_state == "success_verified"


def test_optional_result_notes_do_not_veto_the_selected_model():
    contract = new_outcome_contract(
        ANDROID_REQUEST, ("APK installed on Pixel",),
    )
    assert contract.objective == ANDROID_REQUEST
    assert contract.requirements[0].result == "APK installed on Pixel"
