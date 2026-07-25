"""Privacy and artifact-boundary coverage for WOR-176 browser forms."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from osia import agent as agent_module
from osia import document_artifacts, document_reader


PRIVATE_VALUE = "WOR176_FORM_VALUE_PRIVATE_DO_NOT_LEAK"
DOCUMENT_TAIL = "WOR176_DOCUMENT_TAIL_PRIVATE_DO_NOT_LEAK"
FIELD_REF = "fld_AAAAAAAAAAAAAAAAAAAAAAAA"


def _tool_call(tool: str, **kwargs) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, **kwargs}) + "\n</tool_call>"


class _Replies:
    def __init__(self, replies):
        self.replies = list(replies)

    def create(self, **_kwargs):
        content = self.replies.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
            )]
        )


def _agent_with(replies):
    instance = agent_module.OSIAAgent(lang="en")
    instance.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Replies(replies)),
    )
    return instance


def _safe_fill_result(*, changed=True) -> str:
    return json.dumps({
        "ok": True,
        "field_ref": FIELD_REF,
        "operation": "fill",
        "verified": True,
        "value_present": True,
        "normalized_length": len(PRIVATE_VALUE),
        "changed": changed,
        "state_token": "a" * 32,
        "validation": {"status": "valid"},
    }, sort_keys=True, separators=(",", ":"))


def test_semantic_fill_value_is_absent_from_results_logs_and_history(
    monkeypatch, capsys,
):
    received = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "fill_form_field",
        lambda field_ref, value: received.append((field_ref, value))
        or _safe_fill_result(),
    )
    osia = _agent_with([
        _tool_call("fill_form_field", field_ref=FIELD_REF, value=PRIVATE_VALUE),
        f"The field was prepared with {PRIVATE_VALUE} and verified.",
    ])

    response = osia.run("Prepare the inspected synthetic field.", history=[])
    output = capsys.readouterr().out

    assert received == [(FIELD_REF, PRIVATE_VALUE)]
    assert PRIVATE_VALUE not in response
    assert "[REDACTED FORM VALUE]" in response
    assert PRIVATE_VALUE not in output
    assert PRIVATE_VALUE not in repr(osia.last_run_diagnostic)
    assert PRIVATE_VALUE not in repr(osia.exported_history())
    assert osia.exported_history() == []
    assert osia._semantic_form_values == []


def test_semantic_fill_is_never_automatically_retried(monkeypatch):
    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "fill_form_field",
        lambda field_ref, value: calls.append((field_ref, value))
        or 'ERROR: {"category":"unverified_result","ok":false}',
    )
    result, is_error = agent_module._execute_tool_with_retry(
        "fill_form_field", {"field_ref": FIELD_REF, "value": PRIVATE_VALUE},
    )
    assert is_error is True
    assert result.startswith("ERROR:")
    assert calls == [(FIELD_REF, PRIVATE_VALUE)]
    assert PRIVATE_VALUE not in result


def test_numeric_semantic_fill_value_is_redacted_from_final_output():
    osia = agent_module.OSIAAgent(lang="en")
    osia._remember_semantic_form_value(8675309)

    redacted = osia._redact_semantic_form_values(
        "The verified numeric field contains 8675309."
    )

    assert "8675309" not in redacted
    assert "[REDACTED FORM VALUE]" in redacted


class _ArtifactFormReplies:
    def __init__(self, artifact_id: str):
        self.artifact_id = artifact_id
        self.round = 0

    def create(self, **kwargs):
        self.round += 1
        if self.round == 1:
            content = _tool_call(
                "read_document_artifact",
                artifact_id=self.artifact_id,
                offset=300,
                chunk_chars=800,
            )
        elif self.round == 2:
            model_history = repr(kwargs.get("messages"))
            assert DOCUMENT_TAIL in model_history
            content = _tool_call(
                "fill_form_field", field_ref=FIELD_REF, value=DOCUMENT_TAIL,
            )
        else:
            content = "The bounded document field was prepared and verified."
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
            )]
        )


def test_wor175_artifact_tail_maps_field_by_field_without_persistence(
    tmp_path, monkeypatch, capsys,
):
    store = document_artifacts.ArtifactStore(tmp_path / "artifacts")
    previous = document_artifacts.set_store_for_testing(store)
    received = []
    try:
        with document_artifacts.artifact_access_scope("wor176-artifact-owner"):
            reference = store.create(
                "x" * 350 + DOCUMENT_TAIL,
                {
                    "document_type": "pdf",
                    "page_count": 1,
                    "page_methods": ["native"],
                    "confidence": None,
                    "warnings": [],
                },
            )
        monkeypatch.setitem(
            agent_module.TOOL_MAP,
            "fill_form_field",
            lambda field_ref, value: received.append((field_ref, value))
            or _safe_fill_result(),
        )
        osia = agent_module.OSIAAgent(lang="en")
        osia.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=_ArtifactFormReplies(reference["artifact_id"]),
            )
        )
        with document_artifacts.artifact_access_scope(
            "wor176-artifact-owner", [reference["artifact_id"]],
        ):
            response = osia.run(
                "Use the authorized synthetic document artifact for one inspected field.",
                history=[],
            )
            chunk = json.loads(document_reader.read_document_artifact(
                reference["artifact_id"], offset=300, chunk_chars=800,
            ))
            assert DOCUMENT_TAIL in chunk["content"]
            assert store.delete(reference["artifact_id"]) is True

        rendered = "\n".join([
            response,
            capsys.readouterr().out,
            repr(osia.exported_history()),
            repr(osia.last_run_diagnostic),
        ])
        assert received == [(FIELD_REF, DOCUMENT_TAIL)]
        assert DOCUMENT_TAIL not in rendered
        assert osia.exported_history() == []
        assert not (store.root / reference["artifact_id"]).exists()
    finally:
        document_artifacts.set_store_for_testing(previous)


def test_failed_or_command_accepted_only_results_never_count_as_progress():
    accepted_only = json.dumps({
        "ok": True,
        "field_ref": FIELD_REF,
        "operation": "fill",
        "verified": False,
        "changed": True,
        "state_token": "b" * 32,
    })
    failed = 'ERROR: {"category":"postcondition_mismatch","ok":false}'
    assert agent_module._semantic_form_progress_fingerprint(
        "fill_form_field", accepted_only,
    ) is None
    assert agent_module._semantic_form_progress_fingerprint(
        "fill_form_field", failed,
    ) is None


def test_safe_metadata_shape_contains_no_value_or_screenshot_payload():
    result = json.loads(_safe_fill_result())
    assert result["verified"] is True
    assert result["value_present"] is True
    assert "value" not in result
    assert "screenshot" not in result
    assert PRIVATE_VALUE not in json.dumps(result)
