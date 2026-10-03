"""Canonical outcome/fact integration without language compilation."""
import json
import types
import pytest
from osia.agent import OSIAAgent
from osia.turn_terminality import TurnTerminalityController


def final(text, outcomes=None):
    return text + '<osia_completion>' + json.dumps({'outcomes': outcomes or [
        {'id':'answer','result':'requested answer','kind':'assistant_content','receipt_ids':[]}
    ]}) + '</osia_completion>'

@pytest.fixture(autouse=True)
def stores(monkeypatch,tmp_path):
    import osia.tool_attempts as attempts
    monkeypatch.setattr('osia.turn_terminality.CONFIG_DIR',str(tmp_path))
    monkeypatch.setattr(attempts,'CONFIG_DIR',str(tmp_path))
    monkeypatch.setattr(attempts,'_DEFAULT_STORE',None)
    monkeypatch.setattr(attempts,'_DEFAULT_COORDINATOR',None)

@pytest.mark.parametrize('objective,answer',[
    ('What is the capital of France?','Paris.'),
    ('Write a poem about spring','Flowers wake in morning light.'),
    ('Write a short invitation to dinner','Please join us for dinner on Friday.'),
])
def test_output_only_one_provider_flow(monkeypatch,objective,answer):
    import osia.execution_strategy as strategy
    monkeypatch.setattr(strategy,'derive_semantic_mission',lambda *a,**k: pytest.fail('compiler called'))
    agent=OSIAAgent(); calls=[]
    def run(self,text,**kwargs):
        calls.append(text)
        assert kwargs['on_sentence'] is None
        output=final(answer)
        self.messages.append({'role':'assistant','content':output})
        return output
    agent.run=types.MethodType(run,agent)
    spoken=[]
    assert agent.run_terminal_turn(objective,turn_id='output',on_sentence=spoken.append)==answer
    assert calls==[objective]
    assert spoken==[answer]
    public=[m for m in agent.messages if m.get('role')=='assistant' and not m.get('_internal')]
    assert public==[{'role':'assistant','content':answer}]

@pytest.mark.parametrize("objective", [
    "Install the APK on my Pixel.",
    "Compile the Android project and install the APK on my Pixel.",
    "Organize this project completely.",
])
def test_selected_model_final_does_not_trigger_static_objective_reprompt(objective):
    agent = OSIAAgent()
    calls = []
    answer = "I have not verified completion."

    def run(self, text, **kwargs):
        calls.append(text)
        return answer

    agent.run = types.MethodType(run, agent)
    result = agent.run_terminal_turn(
        objective, turn_id="model-final", execution_required=False,
    )

    assert calls == [objective]
    assert result == answer
    assert agent.terminality_result("model-final")["terminal_state"] == "success_verified"

def test_optional_outcome_notes_survive_reopen_without_gating_model_final(tmp_path):
    controller = TurnTerminalityController(
        turn_id="retain", objective="build and install",
        execution_required=False, storage_dir=tmp_path,
    )
    controller.bind_outcome_contract(["APK built", "APK installed on Pixel"])
    final_text = "The selected model has not verified the APK installation."
    assert controller.accept_completion(final_text) == final_text
    assert controller.evaluate().terminal_state == "success_verified"

    restored = TurnTerminalityController(
        turn_id="retain", objective="build and install",
        execution_required=False, storage_dir=tmp_path,
    )
    assert restored.snapshot()["outcome_contract"] == controller.snapshot()["outcome_contract"]
    assert restored.snapshot()["typed_evidence"] == []
    assert restored.snapshot()["proven_postconditions"] == {}
    assert restored.evaluate().terminal_state == "success_verified"

def test_unknown_outcome_stays_factual_and_does_not_veto_selected_model_final(tmp_path):
    controller = TurnTerminalityController(
        turn_id="facts", objective="install APK",
        execution_required=False, storage_dir=tmp_path,
    )
    controller.bind_outcome_contract()
    outcome = {
        "id": "install", "result": "APK installed", "kind": "observation",
        "target": {"resource": "org.example"},
        "expected": {"property": "present", "equals": True},
        "receipt_ids": [],
    }
    controller.declare_outcomes([{k: v for k, v in outcome.items() if k != "receipt_ids"}])
    controller.record_typed_evidence(
        outcome="outcome_unknown", evidence="pending mutation",
        attempt_id="unknown",
    )
    final_text = "Installation remains unverified."
    assert controller.accept_completion(final(final_text, [outcome])) == final_text
    assert "unknown" in controller.snapshot()["unknown_effects"]
    assert controller.evaluate().terminal_state == "success_verified"
    assert [item["outcome"] for item in controller.snapshot()["typed_evidence"]] == [
        "outcome_unknown",
    ]

def test_optional_new_result_note_does_not_revoke_selected_model_completion(tmp_path):
    controller = TurnTerminalityController(
        turn_id="append", objective="write and build",
        execution_required=False, storage_dir=tmp_path,
    )
    controller.bind_outcome_contract()
    assert controller.accept_completion(final("Text.")) == "Text."
    assert controller.evaluate().terminal_state == "success_verified"

    outcomes = [
        {"id": "answer", "result": "requested answer",
         "kind": "assistant_content", "receipt_ids": []},
        {"id": "file", "result": "built file",
         "kind": "external", "receipt_ids": []},
    ]
    assert controller.accept_completion(final("Still unverified.", outcomes)) == "Still unverified."
    assert controller.evaluate().terminal_state == "success_verified"
    assert controller.snapshot()["typed_evidence"] == []
    assert controller.snapshot()["proven_postconditions"] == {}

def test_resume_retains_new_explicit_requirement(tmp_path):
    controller=TurnTerminalityController(turn_id='append-required',objective='build install',execution_required=False,storage_dir=tmp_path)
    controller.bind_outcome_contract(['APK built'])
    controller.bind_outcome_contract(['APK installed'])
    assert [r['result'] for r in controller.snapshot()['outcome_contract']['requirements']]==['APK built','APK installed']

def test_long_turn_receipts_distinguish_attempts_and_survive(tmp_path):
    controller=TurnTerminalityController(turn_id='many',objective='many results',execution_required=False,storage_dir=tmp_path)
    controller.bind_outcome_contract()
    refs=[]
    for i in range(130):
        controller.record_typed_evidence(outcome='success_verified',evidence='same stdout',attempt_id=str(i),facts=[{'kind':'observation','target':{'resource':'same'},'properties':{'value':'same stdout'}}])
        refs.append(controller.receipt_reference('same stdout',str(i)))
    assert len(set(refs))==130
    assert controller.receipt_reference('same stdout','0')==refs[0]
    controller.mark_structural_wait('WAIT_STRUCTURAL', evidence={'wait':'known'})
    assert controller.receipt_reference('same stdout','0')==refs[0]

def test_original_objective_is_not_truncated(tmp_path):
    objective = 'context ' * 5000 + 'install the APK on my Pixel'
    controller=TurnTerminalityController(turn_id='long-objective',objective=objective,execution_required=False,storage_dir=tmp_path)
    controller.bind_outcome_contract()
    assert controller.objective==objective
    assert controller.snapshot()['outcome_contract']['objective']==objective
    with pytest.raises(ValueError,match='another objective'):
        TurnTerminalityController(turn_id='long-objective',objective=objective+' and verify',execution_required=False,storage_dir=tmp_path)


def test_continuation_exposes_usable_receipt_identifiers(tmp_path):
    controller=TurnTerminalityController(turn_id='receipt-context',objective='produce result',execution_required=False,storage_dir=tmp_path)
    controller.bind_outcome_contract()
    requirement={'id':'result','result':'requested observed result','kind':'observation','target':{'resource':'test-resource'},'expected':{'property':'value','equals':'observed'}}
    controller.declare_outcomes([requirement])
    controller.record_typed_evidence(outcome='success_verified',evidence='actual observed result',attempt_id='read',facts=[{'kind':'observation','target':{'resource':'test-resource'},'properties':{'value':'observed'}}])
    context=controller.continuation_context(controller.evaluate())
    payload=json.loads(context[context.index('{'):-1])
    reference=payload['receipts'][0]['receipt_id']
    assert reference==controller.receipt_reference('actual observed result','read')
    proposal={**requirement,'receipt_ids':[reference]}
    controller.accept_completion(final('Observed.',[proposal]))
    assert controller.evaluate().terminal_state=='success_verified'


def test_canonical_fact_report_is_not_replaced_by_blanket_claim():
    agent=OSIAAgent()
    report='Observed artifact: postcondition.txt exists.'
    assert agent._terminal_final_text('success_verified','unsupported compiled claim',snapshot={'outcome_contract':{'objective':'write file'},'completion_text':report})==report
