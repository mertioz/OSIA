"""WOR-196 local-reauth provider follow-up under the WOR-301 Full Access model.

WOR-301 removed permission modes, grants, permits, epochs and the permission
store: OSIA executes with permanent unconditional Full Access.  The Windows
Hello reauth provider itself remains production code with no permission
authority; these tests keep its typed native-result contract intact.
"""

from __future__ import annotations

import pytest

from osia.local_reauth import (
    ReauthResult,
    ReauthStatus,
    WindowsHelloReauthProvider,
)


def test_windows_hello_requires_desktop_window_handle(monkeypatch):
    version = type("Version", (), {"build": 26100})()
    monkeypatch.setattr("osia.local_reauth.sys.platform", "win32")
    monkeypatch.setattr(
        "osia.local_reauth.sys.getwindowsversion", lambda: version,
        raising=False,
    )
    result = WindowsHelloReauthProvider().reauthenticate("Confirm")
    assert result.status is ReauthStatus.ERROR
    assert result.reason_code == "window_handle_required"


def test_native_exception_becomes_typed_error_without_state_write(
    monkeypatch,
):
    version = type("Version", (), {"build": 26100})()
    monkeypatch.setattr("osia.local_reauth.sys.platform", "win32")
    monkeypatch.setattr(
        "osia.local_reauth.sys.getwindowsversion", lambda: version,
        raising=False,
    )

    class _IsWindow:
        def __call__(self, _window_handle):
            return True

    class _User32:
        IsWindow = _IsWindow()

    monkeypatch.setattr(
        "osia.local_reauth.ctypes.WinDLL",
        lambda *_args, **_kwargs: _User32(),
    )
    monkeypatch.setattr(
        "osia.local_reauth._verify_windows_hello_for_window",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("synthetic_native_failure")
        ),
    )
    result = WindowsHelloReauthProvider().reauthenticate(
        "Confirm", window_handle=12345,
    )
    assert result == ReauthResult(
        ReauthStatus.ERROR, "windows_hello", "reauth_backend_error",
    )


@pytest.mark.parametrize(
    ("verification", "expected_status", "expected_reason"),
    [
        ("Verified", ReauthStatus.SUCCESS, ""),
        ("RetriesExhausted", ReauthStatus.DENIED, "verification_denied"),
        ("Canceled", ReauthStatus.CANCELLED, "user_cancelled"),
        (
            "DeviceNotPresent",
            ReauthStatus.UNAVAILABLE,
            "windows_hello_devicenotpresent",
        ),
        ("TimedOut", ReauthStatus.ERROR, "reauth_timeout"),
        ("InteropOperationFailed", ReauthStatus.ERROR, "reauth_backend_error"),
    ],
)
def test_windows_hello_native_results_are_typed(
    monkeypatch, verification, expected_status, expected_reason,
):
    version = type("Version", (), {"build": 26100})()
    monkeypatch.setattr("osia.local_reauth.sys.platform", "win32")
    monkeypatch.setattr(
        "osia.local_reauth.sys.getwindowsversion", lambda: version,
        raising=False,
    )

    class _IsWindow:
        def __call__(self, _window_handle):
            return True

    class _User32:
        IsWindow = _IsWindow()

    monkeypatch.setattr(
        "osia.local_reauth.ctypes.WinDLL",
        lambda *_args, **_kwargs: _User32(),
    )
    monkeypatch.setattr(
        "osia.local_reauth._verify_windows_hello_for_window",
        lambda *_args, **_kwargs: verification,
    )
    result = WindowsHelloReauthProvider().reauthenticate(
        "Confirm", window_handle=12345,
    )
    assert result.status is expected_status
    assert result.reason_code == expected_reason


def test_full_access_is_permanent_and_has_no_store(tmp_path):
    """WOR-301: no permission module, mode, grant, permit or confirmation."""

    import importlib.util

    from osia import action_policy

    for name in ("permission_store", "permission_contract", "permission_policy"):
        assert importlib.util.find_spec(f"osia.{name}") is None, name
    for removed in (
        "PermissionMode", "PermissionPolicy", "PermissionStore", "UsePolicy",
        "create_grant", "build_permission_request", "evaluate_permission",
        "trusted_deletion_classification",
    ):
        assert not hasattr(action_policy, removed), removed
    assert action_policy.action_policy_prompt_block("en") == ""
    assert action_policy.action_policy_prompt_block("fr") == ""
    assert action_policy.guard_tool_call(
        "Write an isolated note.",
        "write_file",
        {"path": str(tmp_path / "note.txt"), "content": "fixture"},
    ) is None
