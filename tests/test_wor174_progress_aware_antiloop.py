"""Focused deterministic coverage for WOR-174 progress-aware anti-looping."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from osia import agent as agent_module
from osia.autonomous import diagnostics
from osia.autonomous import state as autonomous_state
from osia.autonomous.orchestrator import Orchestrator
from osia.chatgpt_account.provider import _bounded_turn_timeout


SENSITIVE_MARKER = "WOR174_PRIVATE_MARKER_DO_NOT_LEAK"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def _guard(*, idle=300.0, hard=1800.0):
    clock = FakeClock()
    return clock, agent_module._AntiLoopGuard(
        lang="en", clock=clock, idle_limit=idle, hard_limit=hard,
    )


def test_verified_periodic_progress_continues_past_legacy_300_seconds():
    clock, guard = _guard(hard=1800.0)
    for index in range(1, 6):
        clock.advance(250)
        assert not guard.record(
            "read_file",
            {"path": f"synthetic-{index}"},
            f"bounded state {index}",
            False,
        )
    assert clock.now == 1250
    assert guard.started_at == 0
    assert guard.last_useful_progress_at == 1250
    assert not guard.tripped


def test_stagnation_limit_stops_without_progress():
    clock, guard = _guard()
    clock.advance(300)
    assert guard.check_time()
    assert guard.stop_category == "stagnation_timeout"


def test_subminute_override_is_reported_with_its_exact_duration():
    clock, guard = _guard(idle=12.5)
    clock.advance(12.5)
    assert guard.check_time()
    assert "12.5 seconds" in guard.stop_reason


def test_total_hard_ceiling_is_immutable_despite_continuous_progress():
    clock, guard = _guard(idle=300, hard=900)
    for index in range(3):
        clock.advance(250)
        assert not guard.record(
            "get_current_url", {},
            f"Current URL: https://safe.invalid/progress/{index}", False,
        )
    clock.advance(150)
    assert guard.check_time()
    assert guard.started_at == 0
    assert guard.last_useful_progress_at == 750
    assert guard.stop_category == "hard_total_ceiling"


def test_successful_unchanged_observation_does_not_extend_idle_timer():
    clock, guard = _guard()
    clock.advance(10)
    assert not guard.record("read_page", {}, "Page content:\nunchanged", False)
    first_progress = guard.last_useful_progress_at
    clock.advance(250)
    assert not guard.record("get_page_text", {}, "unchanged", False)
    assert guard.last_useful_progress_at == first_progress
    clock.advance(50)
    assert guard.check_time()
    assert guard.stop_category == "stagnation_timeout"


@pytest.mark.parametrize(
    ("tool_name", "result_text"),
    [
        ("take_screenshot", "Description of the same screen"),
        ("scroll", "Scrolled down by 600px"),
        ("scroll_down", "Scrolled down by 600px"),
        ("fill", "Filled #field with: synthetic"),
        ("desktop_click", "Clicked synthetic target at (1, 1)."),
        ("click", "Clicked element: #synthetic"),
        ("kill_process", "Sent SIGTERM to PID 1 (synthetic)"),
        ("set_volume", "Volume set to 42%"),
        ("set_clipboard", "Copied 9 chars to clipboard"),
    ],
)
def test_trivial_activity_does_not_extend_idle_timer(tool_name, result_text):
    clock, guard = _guard()
    original = guard.last_useful_progress_at
    for index in range(4):
        clock.advance(60)
        assert not guard.record(
            tool_name,
            {"iteration": index},
            result_text,
            False,
        )
    assert guard.last_useful_progress_at == original
    clock.advance(60)
    assert guard.check_time()


def test_wait_for_load_counts_only_when_it_discovers_a_new_url():
    clock, guard = _guard()
    clock.advance(20)
    assert not guard.record(
        "get_current_url", {}, "Current URL: https://safe.invalid/a", False,
    )
    clock.advance(200)
    assert not guard.record(
        "wait_for_load", {}, "Page loaded: https://safe.invalid/a", False,
    )
    assert guard.last_useful_progress_at == 20
    assert not guard.record(
        "wait_for_load", {}, "Page loaded: https://safe.invalid/b", False,
    )
    assert guard.last_useful_progress_at == 220


def test_changed_url_and_new_read_only_state_extend_idle_timer():
    clock, guard = _guard()
    clock.advance(100)
    guard.record("get_current_url", {}, "Current URL: https://safe.invalid/a", False)
    assert guard.last_useful_progress_at == 100
    clock.advance(100)
    guard.record("get_current_url", {}, "Current URL: https://safe.invalid/b", False)
    assert guard.last_useful_progress_at == 200
    clock.advance(100)
    guard.record("read_file", {"path": "redacted"}, "new bounded state", False)
    assert guard.last_useful_progress_at == 300


def test_recent_progress_fingerprints_are_bounded():
    _clock, guard = _guard()
    for index in range(agent_module.RECENT_PROGRESS_FINGERPRINT_LIMIT + 9):
        guard.record(
            "read_file", {"probe": index}, f"bounded state {index}", False,
        )
    assert len(guard._recent_progress_fingerprints) == (
        agent_module.RECENT_PROGRESS_FINGERPRINT_LIMIT
    )
    assert guard.useful_progress_count == (
        agent_module.RECENT_PROGRESS_FINGERPRINT_LIMIT + 9
    )


def test_identical_call_detector_remains_active():
    _clock, guard = _guard()
    for _ in range(agent_module.REPEAT_CALL_LIMIT):
        guard.record("write_file", {"path": "x", "content": "y"},
                     "Successfully wrote 1 bytes to x", False)
    assert guard.stop_category == "repeated_identical_call"


def test_repeated_cycle_detector_remains_active():
    _clock, guard = _guard()
    cycle = [
        ("read_page", {}, "Page content:\na"),
        ("get_current_url", {}, "Current URL: https://safe.invalid/a"),
    ]
    for _ in range(agent_module.STALL_CYCLE_REPEATS + 1):
        for name, kwargs, result in cycle:
            guard.record(name, kwargs, result, False)
    assert guard.stop_category == "repeated_cycle"


def test_consecutive_error_detector_remains_active():
    _clock, guard = _guard()
    for index in range(agent_module.ERROR_STREAK_LIMIT):
        guard.record(
            "read_file", {"attempt": index}, f"ERROR: failure {index}", True,
        )
    assert guard.stop_category == "consecutive_tool_errors"


def test_streaming_keepalive_retry_and_generic_success_are_not_progress():
    clock, guard = _guard()
    for _ in range(5):
        clock.advance(50)
        # Streaming tokens/keepalives never enter record(); a generic retry
        # success is deliberately classified as no evidence.
        guard.record("fill", {"selector": "#x"}, "Success", False)
    assert guard.last_useful_progress_at == guard.started_at
    clock.advance(50)
    assert guard.check_time()


@pytest.mark.parametrize("value", ["", "nan", "inf", "-inf", "0", "-1", "bad"])
def test_invalid_idle_override_uses_safe_default(monkeypatch, value):
    monkeypatch.setenv("OSIA_TURN_TIME_LIMIT_SECONDS", value)
    assert agent_module._turn_time_limit() == (
        agent_module.DEFAULT_TURN_IDLE_LIMIT_SECONDS
    )


def test_legacy_idle_override_remains_compatible(monkeypatch):
    monkeypatch.setenv("OSIA_TURN_TIME_LIMIT_SECONDS", "12.5")
    assert agent_module._turn_time_limit() == 12.5


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "86401", "bad"])
def test_invalid_or_unbounded_hard_override_uses_safe_default(monkeypatch, value):
    monkeypatch.setenv("OSIA_TURN_HARD_LIMIT_SECONDS", value)
    assert agent_module._turn_hard_time_limit() == (
        agent_module.DEFAULT_TURN_HARD_LIMIT_SECONDS
    )


def test_bounded_hard_override_is_supported(monkeypatch):
    monkeypatch.setenv("OSIA_TURN_HARD_LIMIT_SECONDS", "7200")
    assert agent_module._turn_hard_time_limit() == 7200
    assert _bounded_turn_timeout(7200) == 300
    assert _bounded_turn_timeout(math.inf) == 300


def test_typed_diagnostic_stage_category_and_localization_are_safe():
    clock, guard = _guard()
    clock.advance(300)
    guard.check_time()
    assert guard.sanitized_diagnostic() == {
        "stage": "task_execution",
        "error_category": "stagnation_timeout",
        "reason_code": "stagnation_timeout",
    }
    assert diagnostics.failure_message_key("stagnation_timeout") == (
        "autonomous.failure.stagnation_timeout"
    )


def test_autonomous_terminal_projection_preserves_typed_antiloop_leaf(monkeypatch):
    class TypedFailureAgent:
        def __init__(self):
            self.calls = 0
            self.last_run_diagnostic = None

        def reset(self):
            return None

        def run(self, _prompt):
            self.calls += 1
            if self.calls == 1:
                self.last_run_diagnostic = {
                    "stage": "task_execution",
                    "error_category": "stagnation_timeout",
                    "reason_code": "stagnation_timeout",
                }
                return "sanitized anti-loop stop"
            self.last_run_diagnostic = None
            return "NOT_DONE\nSynthetic criterion is not met."

    monkeypatch.setattr(autonomous_state, "save_task_state", lambda _st: None)
    st = autonomous_state.new_task_state(
        "synthetic objective",
        "ollama",
        "synthetic-model",
        [{
            "name": "synthetic phase",
            "status": "pending",
            "tasks": [{
                "id": "task-1",
                "objective": "synthetic step",
                "success_criterion": "Synthetic observable state is true",
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "distilled_summary": "",
            }],
        }],
    )
    st["budgets"]["max_attempts_per_task"] = 1
    st["budgets"]["max_replans"] = 0
    events = []
    Orchestrator(TypedFailureAgent(), progress_cb=events.append)._step(st)

    assert st["status"] == "failed"
    assert st["failure_diagnostic"]["stage"] == "task_execution"
    assert st["failure_diagnostic"]["error_category"] == "stagnation_timeout"
    categories = [item["error_category"] for item in st["diagnostic_history"]]
    assert "task_attempts_exhausted" in categories
    assert "replans_exhausted" in categories
    assert events[-1]["error_category"] == "stagnation_timeout"


def test_sensitive_arguments_results_and_markers_never_enter_guard_output(capsys):
    _clock, guard = _guard()
    for index in range(agent_module.ERROR_STREAK_LIMIT):
        guard.record(
            "read_file",
            {"path": f"{SENSITIVE_MARKER}/{index}"},
            f"ERROR: {SENSITIVE_MARKER}",
            True,
        )
    rendered = "\n".join(
        [
            repr(guard.calls),
            repr(guard.actions),
            repr(guard.sanitized_diagnostic()),
            guard.fallback_summary(),
            capsys.readouterr().out,
        ]
    )
    assert SENSITIVE_MARKER not in rendered


def test_model_supplied_tool_name_is_sanitized_in_logs_and_summary(capsys):
    _clock, guard = _guard()
    hostile_name = f"tool-{SENSITIVE_MARKER}/private"
    for _ in range(agent_module.REPEAT_CALL_LIMIT):
        guard.record(hostile_name, {}, "ERROR: redacted", True)
    rendered = "\n".join([
        repr(guard.calls),
        repr(guard.actions),
        guard.fallback_summary(),
        capsys.readouterr().out,
    ])
    assert SENSITIVE_MARKER not in rendered
    assert "unknown_tool" in rendered


def test_deduplicated_tool_finalization_keeps_one_sanitized_assistant_state(
    monkeypatch,
):
    monkeypatch.setitem(agent_module.TOOL_MAP, "fill", lambda **_kwargs: "Success")
    tool_call = (
        '<tool_call>{"tool":"fill","selector":"#private",'
        f'"value":"{SENSITIVE_MARKER}"}}</tool_call>'
    )
    osia = agent_module.OSIAAgent(lang="en")
    osia.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(
                [tool_call] * agent_module.REPEAT_CALL_LIMIT
            )
        )
    )

    response = osia.run("synthetic request")
    exported = osia.exported_history()
    assert osia.last_run_diagnostic["error_category"] == "task_execution_failure"
    assert [item["role"] for item in exported] == ["user", "assistant"]
    assert exported[-1]["content"] == response
    assert SENSITIVE_MARKER not in repr(exported)
    assert "<tool_result>" not in repr(exported)
    assert "PROTECTION ANTI" not in repr(exported)


def test_deepseek_native_and_groq_xml_parsers_and_parse_before_strip_unchanged():
    xml = '<tool_call>{"tool":"read_file","path":"x"}</tool_call>'
    assert agent_module._extract_tool_calls(xml, provider="groq") == [
        {"tool": "read_file", "path": "x"}
    ]
    native_call = SimpleNamespace(
        function=SimpleNamespace(name="read_file", arguments='{"path":"y"}')
    )
    message = SimpleNamespace(tool_calls=[native_call])
    assert agent_module._extract_tool_calls(
        "", message=message, provider="deepseek",
    ) == [{"tool": "read_file", "path": "y"}]
    assert not agent_module._extract_tool_calls(
        agent_module.strip_raw_tool_tags(xml), provider="groq",
    )


class _FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)

    def create(self, **_kwargs):
        content = self.replies.pop(0)
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_normal_short_text_response_and_raw_markup_contract_are_unchanged():
    osia = agent_module.OSIAAgent(lang="en")
    osia.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(["Short safe response."]))
    )
    assert osia.run("hello") == "Short safe response."
    assert osia.last_run_diagnostic is None
    assert "<tool_call>" not in osia.messages[-1]["content"]


def test_text_only_provider_chatter_cannot_bypass_stagnation(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(agent_module.time, "monotonic", clock)

    class DelayedCompletions:
        def create(self, **kwargs):
            assert 0 < kwargs["timeout"] <= 300
            clock.advance(301)
            message = SimpleNamespace(content="late provider prose", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    osia = agent_module.OSIAAgent(lang="en")
    osia.client = SimpleNamespace(
        chat=SimpleNamespace(completions=DelayedCompletions())
    )
    result = osia.run("hello")
    assert "late provider prose" not in result
    assert osia.last_run_diagnostic["error_category"] == "stagnation_timeout"


def test_exhausted_provider_timeout_is_typed_without_raw_exception_text():
    class TimedOutCompletions:
        def create(self, **_kwargs):
            raise TimeoutError(SENSITIVE_MARKER)

    osia = agent_module.OSIAAgent(lang="en")
    osia.client = SimpleNamespace(
        chat=SimpleNamespace(completions=TimedOutCompletions())
    )
    with pytest.raises(TimeoutError):
        osia.run("hello")
    assert osia.last_run_diagnostic == {
        "stage": "task_execution",
        "error_category": "provider_timeout",
        "reason_code": "provider_timeout",
    }
    assert SENSITIVE_MARKER not in repr(osia.last_run_diagnostic)


def test_structural_tool_timeout_is_typed_if_no_later_progress(monkeypatch):
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "read_file",
        lambda **_kwargs: "[ERREUR] synthetic tool timeout",
    )
    replies = [
        '<tool_call>{"tool":"read_file","path":"redacted"}</tool_call>',
        "The requested observation could not be confirmed.",
    ]
    osia = agent_module.OSIAAgent(lang="en")
    osia.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(replies))
    )
    assert osia.run("read synthetic state") == (
        "The requested observation could not be confirmed."
    )
    assert osia.last_run_diagnostic["error_category"] == "tool_timeout"


def test_each_agent_run_receives_a_fresh_guard(monkeypatch):
    created = []
    real_guard = agent_module._AntiLoopGuard

    def factory(*args, **kwargs):
        instance = real_guard(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(agent_module, "_AntiLoopGuard", factory)
    osia = agent_module.OSIAAgent(lang="en")
    osia.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(["first", "second"]),
        )
    )
    assert osia.run("one") == "first"
    assert osia.run("two") == "second"
    assert len(created) == 2
    assert created[0] is not created[1]
