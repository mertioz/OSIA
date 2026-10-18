"""Desktop disconnect must project observed auth state even when RPC fails."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osia.chatgpt_account.app_server_client import AppServerError


@pytest.mark.parametrize("failed", [True, False])
def test_logout_projects_actual_provider_status(monkeypatch, failed):
    from osia.gui import main_popup

    provider = Mock()
    provider.logout.side_effect = AppServerError("fixture-private", category="request_rejected") if failed else None
    observed = {
        "available": failed, "runtimeAvailable": True, "connected": failed,
        "models": [{"id": "exact-model"}] if failed else [],
        "error": "" if failed else "disconnected",
    }
    monkeypatch.setattr("osia.chatgpt_account.provider.get_account_provider", lambda: provider)
    received = []
    panel = SimpleNamespace(
        _chatgpt_action_busy=False, _chatgpt_request_generation=10,
        _set_chatgpt_ui_state=lambda state: None,
        _chatgpt_status_ready=SimpleNamespace(emit=received.append),
        _chatgpt_status_with_usage=lambda selected: dict(observed),
    )
    monkeypatch.setattr(main_popup.threading, "Thread", lambda *, target, **kwargs: SimpleNamespace(start=target))
    main_popup._SettingsPanel._on_chatgpt_disconnect(panel)
    assert len(received) == 1
    assert received[0]["connected"] is failed
    assert received[0]["models"] == observed["models"]
    assert received[0].get("_osiaLogoutFailed", False) is failed
    assert "fixture-private" not in str(received)
    assert received[0]["_osiaRequestGeneration"] == 11
