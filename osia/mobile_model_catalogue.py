"""Sanitized PC-owned model and ChatGPT-account mirror for mobile."""

from __future__ import annotations

import threading
from collections.abc import Callable

from osia.config import (
    get_reasoning_settings,
    load_api_keys,
    load_settings,
    set_reasoning_settings,
    set_setting,
)
from osia.chatgpt_account.rate_limits import quota_windows
from osia.models import (
    API_PROVIDERS,
    PROVIDERS,
    discover_models_async,
    get_all_models,
    invalidate_ollama_models_cache,
)
from osia.reasoning import get_reasoning_options, resolve_reasoning_option_id
from osia.utils.key_chain import has_key

ACCOUNT_PROVIDER_ID = "chatgpt_account"
PROVIDER_ORDER = (
    ACCOUNT_PROVIDER_ID,
    "anthropic",
    "openai",
    "deepseek",
    "google",
    "xai",
    "groq",
    "mistral",
    "ollama",
)
_SAFE_ACCOUNT_ERRORS = {
    "",
    "disconnected",
    "feature_disabled",
    "runtime_missing",
    "runtime_incompatible",
    "isolation_failed",
    "login_cancelled",
    "login_failed",
    "login_invalid",
    "login_timeout",
    "model_unavailable",
    "provider_error",
}

_remote_lock = threading.Lock()
_remote_models: list[dict] = []
_remote_refreshing = False
_remote_callbacks: list[Callable[[], None]] = []

_login_lock = threading.RLock()
_login_connecting = False
_login_ephemeral: dict[str, str] = {}


def _safe_error(value) -> str:
    category = str(value or "").strip().lower()
    return category if category in _SAFE_ACCOUNT_ERRORS else "provider_error"


def _safe_quota_windows(raw) -> list[dict]:
    safe = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or "").split())[:80]
        remaining = item.get("remainingPercent")
        if (
            label
            and isinstance(remaining, int)
            and not isinstance(remaining, bool)
            and 0 <= remaining <= 100
        ):
            safe.append({"label": label, "remainingPercent": remaining})
    return safe


def _reasoning_metadata(provider: str, model_id: str) -> dict:
    options = []
    for raw in get_reasoning_options(provider, model_id):
        option_id = str(raw.get("id") or "").strip().lower()
        if not option_id:
            continue
        options.append({
            "id": option_id,
            "label": str(raw.get("label") or option_id),
            "default": bool(raw.get("default")),
        })
    selected = resolve_reasoning_option_id(provider, model_id)
    return {
        "supported": bool(options),
        "options": options,
        "selected": selected if any(o["id"] == selected for o in options) else "auto",
    }


def _safe_model(raw: dict, provider: str | None = None) -> dict | None:
    provider_id = str(provider or raw.get("provider") or "").strip().lower()
    model_id = str(raw.get("id") or raw.get("model") or "").strip()
    if not provider_id or not model_id:
        return None
    modalities = raw.get("inputModalities") or ["text"]
    if not isinstance(modalities, list):
        modalities = ["text"]
    safe_modalities = [
        value for value in (str(item).strip().lower() for item in modalities)
        if value in {"text", "image", "audio"}
    ]
    if "text" not in safe_modalities:
        return None
    label = str(raw.get("displayName") or raw.get("label") or model_id).strip()
    return {
        "id": model_id,
        "displayName": label or model_id,
        "providerId": provider_id,
        "provider": provider_id,
        "inputModalities": safe_modalities,
        "hidden": False,
        "usable": True,
        "selectable": True,
        "vision": "image" in safe_modalities,
        "reasoning": _reasoning_metadata(provider_id, model_id),
    }


def _account_status_with_usage(*, refresh_models: bool) -> dict:
    try:
        from osia.chatgpt_account.provider import get_account_provider

        provider = get_account_provider()
        status = provider.status(refresh_models=refresh_models)
        if status.get("connected"):
            try:
                usage = provider.rate_limits()
                status = dict(status)
                status["quotaWindows"] = quota_windows(usage)
            except Exception:
                pass
        return status
    except Exception:
        return {
            "available": False,
            "runtimeAvailable": False,
            "connected": False,
            "models": [],
            "error": "runtime_missing",
        }


def account_snapshot(*, refresh_models: bool = True, include_ephemeral: bool = False) -> dict:
    status = _account_status_with_usage(refresh_models=refresh_models)
    account = status.get("account") if isinstance(status.get("account"), dict) else {}
    models = []
    seen = set()
    for raw in status.get("models") or []:
        if not isinstance(raw, dict) or raw.get("hidden"):
            continue
        model = _safe_model(raw, ACCOUNT_PROVIDER_ID)
        if model is None or model["id"] in seen:
            continue
        seen.add(model["id"])
        models.append(model)
    with _login_lock:
        connecting = _login_connecting
        ephemeral = dict(_login_ephemeral) if include_ephemeral else {}
    error = _safe_error(status.get("error"))
    connected = bool(status.get("connected"))
    available = bool(connected and status.get("available") and not error and models)
    snapshot = {
        "providerId": ACCOUNT_PROVIDER_ID,
        "runtimeAvailable": bool(status.get("runtimeAvailable")),
        "connected": connected,
        "connecting": connecting,
        "loginCancellable": connecting,
        "available": available,
        "error": error,
        "email": str(account.get("email") or ""),
        "plan": str(account.get("planType") or ""),
        "quotaWindows": _safe_quota_windows(status.get("quotaWindows")),
        "selectedModel": str(status.get("selectedModel") or ""),
        "models": models,
    }
    if ephemeral:
        snapshot["login"] = ephemeral
    return snapshot


def _mobile_model(entry: dict) -> dict:
    """Serialize one canonical inventory entry for the mobile protocol."""
    provider_id = entry["provider_id"]
    model_id = entry["model_id"]
    options = [dict(option) for option in entry.get("reasoning_options") or []]
    try:
        selected = resolve_reasoning_option_id(provider_id, model_id)
    except Exception:
        selected = "auto"
    if not any(option.get("id") == selected for option in options):
        selected = "auto"
    modalities = list(entry.get("input_modalities") or ["text"])
    return {
        "id": model_id,
        "displayName": entry["model_display_name"],
        "providerId": provider_id,
        "provider": provider_id,
        "inputModalities": modalities,
        "hidden": not bool(entry.get("picker_visible")),
        "usable": bool(entry.get("available")),
        "selectable": bool(entry.get("picker_visible") and entry.get("available")),
        "vision": bool(entry.get("capabilities", {}).get("vision")),
        "availability": entry.get("availability", "listed"),
        "source": entry.get("catalogue_source", entry.get("source_type", "")),
        "reasoning": {
            "supported": bool(options),
            "options": options,
            "selected": selected,
        },
    }


def _provider_entry(provider_id: str, entries: list[dict]) -> dict:
    kind = (
        "chatgpt_account" if provider_id == ACCOUNT_PROVIDER_ID
        else "local" if provider_id == "ollama"
        else "api"
    )
    name = (
        "ChatGPT" if provider_id == ACCOUNT_PROVIDER_ID
        else str(PROVIDERS.get(provider_id, {}).get("name") or provider_id)
    )
    return {
        "providerId": provider_id,
        "displayName": name,
        "kind": kind,
        "available": any(entry.get("available") for entry in entries),
        "modelCount": len(entries),
        "models": [_mobile_model(entry) for entry in entries],
    }


def cached_remote_models() -> list[dict]:
    with _remote_lock:
        return [dict(item) for item in _remote_models]


def _api_provider_is_configured(keys: dict, provider_id: str) -> bool:
    """Use the key-chain owner, including fallback-only configuration."""
    if provider_id not in API_PROVIDERS:
        return False
    value = keys.get(provider_id)
    if value is not None and bool(str(value).strip()):
        return True
    try:
        return bool(has_key(provider_id))
    except Exception:
        return False


def configured_api_providers(keys: dict | None = None) -> set[str]:
    """Return provider ids only; key material never leaves the config owner."""
    safe_keys = keys if isinstance(keys, dict) else {}
    return {
        provider_id for provider_id in API_PROVIDERS
        if _api_provider_is_configured(safe_keys, provider_id)
    }


def stable_model_key(provider_id: str, model_id: str) -> str:
    """Stable wire identity for one exact provider/model pair."""
    return f"{str(provider_id or '').strip().lower()}:{str(model_id or '').strip()}"


def _safe_reasoning_options(raw: dict, provider_id: str, model_id: str) -> list[dict]:
    reasoning = raw.get("reasoning") if isinstance(raw.get("reasoning"), dict) else {}
    candidates = raw.get("reasoning_options") or reasoning.get("options") or []
    if not candidates and provider_id == ACCOUNT_PROVIDER_ID:
        efforts = raw.get("supportedReasoningEfforts") or []
        if efforts:
            candidates = [
                {"id": "auto", "label": "Default", "default": True},
                *efforts,
            ]
    if not candidates and provider_id != ACCOUNT_PROVIDER_ID:
        try:
            candidates = get_reasoning_options(provider_id, model_id)
        except Exception:
            candidates = []
    options = []
    seen = set()
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict):
            continue
        option_id = str(item.get("id") or "").strip().lower()
        if not option_id or option_id in seen:
            continue
        seen.add(option_id)
        label = str(item.get("label") or option_id).strip() or option_id
        option = {
            "id": option_id,
            "label": label,
            "default": bool(item.get("default")),
        }
        label_key = str(item.get("label_key") or "").strip()
        if label_key:
            option["label_key"] = label_key
        options.append(option)
    if (
        provider_id == ACCOUNT_PROVIDER_ID
        and len(options) == 1
        and options[0]["id"] == "auto"
    ):
        return []
    return options


def _canonical_entry(raw: dict, provider_id: str, source_type: str) -> dict | None:
    model_id = str(raw.get("id") or raw.get("model") or "").strip()
    if not model_id:
        return None
    if provider_id == "deepseek" and model_id != "deepseek-flash":
        return None
    modalities = raw.get("inputModalities") or raw.get("input_modalities") or ["text"]
    if not isinstance(modalities, list):
        modalities = ["text"]
    safe_modalities = [
        value for value in (str(item).strip().lower() for item in modalities)
        if value in {"text", "image", "audio"}
    ]
    if "text" not in safe_modalities:
        return None
    display_name = str(
        raw.get("displayName") or raw.get("label") or model_id
    ).strip() or model_id
    provider_name = (
        "ChatGPT" if provider_id == ACCOUNT_PROVIDER_ID
        else str(PROVIDERS.get(provider_id, {}).get("name") or provider_id)
    )
    from osia.model_capabilities import effective_route_capabilities

    capabilities = effective_route_capabilities(
        provider_id,
        model_id,
        model_metadata=raw,
    )
    if (
        raw.get("inputModalities") is None
        and raw.get("input_modalities") is None
        and capabilities.input_modalities
    ):
        safe_modalities = list(capabilities.input_modalities)
    vision = capabilities.vision_supported is True
    return {
        "provider_id": provider_id,
        "provider_display_name": provider_name,
        "model_id": model_id,
        "model_display_name": display_name,
        "source_type": source_type,
        "configured": True,
        "available": bool(raw.get("available", True)),
        "availability": str(raw.get("availability") or "listed"),
        "catalogue_source": str(raw.get("source") or source_type),
        "picker_visible": True,
        "input_modalities": safe_modalities,
        "capabilities": {"vision": bool(vision)},
        "reasoning_options": _safe_reasoning_options(raw, provider_id, model_id),
        "stable_key": stable_model_key(provider_id, model_id),
    }


def build_model_inventory(
    raw_models: list,
    configured_providers: set[str] | list[str] | tuple[str, ...],
    account_status: dict | None = None,
) -> list[dict]:
    """Purely normalize already-available sanitized state into one inventory.

    The caller owns discovery and account probing.  This function performs no
    network, App Server, local-runtime or secret-store access.
    """
    configured = {
        str(provider_id or "").strip().lower()
        for provider_id in configured_providers or ()
    }
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def append(raw: dict, provider_id: str, source_type: str) -> None:
        entry = _canonical_entry(raw, provider_id, source_type)
        if entry is None:
            return
        identity = (entry["provider_id"], entry["model_id"])
        if identity in seen:
            return
        seen.add(identity)
        entries.append(entry)

    grouped: dict[str, list[dict]] = {}
    for raw in raw_models if isinstance(raw_models, list) else []:
        if not isinstance(raw, dict):
            continue
        provider_id = str(raw.get("provider") or raw.get("providerId") or "").strip().lower()
        if provider_id in API_PROVIDERS:
            if provider_id not in configured:
                continue
            grouped.setdefault(provider_id, []).append(raw)
        elif provider_id == "ollama":
            grouped.setdefault(provider_id, []).append(raw)

    status = account_status if isinstance(account_status, dict) else {}
    runtime_available = bool(
        status.get("runtimeAvailable", status.get("available", False))
    )
    account_models = status.get("models") if isinstance(status.get("models"), list) else []
    account_visible = bool(
        runtime_available
        and status.get("available")
        and status.get("connected")
        and not status.get("error")
        and account_models
    )

    for provider_id in PROVIDER_ORDER:
        if provider_id == ACCOUNT_PROVIDER_ID:
            if account_visible:
                for raw in account_models:
                    if isinstance(raw, dict) and not raw.get("hidden"):
                        append(raw, provider_id, "chatgpt_account")
            continue
        source_type = "local" if provider_id == "ollama" else "api"
        for raw in grouped.get(provider_id, []):
            append(raw, provider_id, source_type)
    return entries










def build_catalogue(*, refresh_local: bool = True, refresh_account: bool = True) -> dict:
    """Build the deterministic safe catalogue from current PC-owned state."""
    if refresh_local:
        invalidate_ollama_models_cache()
    keys = load_api_keys()
    raw_models = get_all_models(api_keys=keys, remote_models=cached_remote_models())
    account = account_snapshot(refresh_models=refresh_account)
    inventory = build_model_inventory(
        raw_models,
        configured_api_providers(keys),
        account,
    )
    grouped: dict[str, list[dict]] = {}
    for entry in inventory:
        grouped.setdefault(entry["provider_id"], []).append(entry)

    providers = [
        _provider_entry(provider_id, grouped[provider_id])
        for provider_id in PROVIDER_ORDER
        if grouped.get(provider_id)
    ]
    flat_models = [model for provider in providers for model in provider["models"]]
    settings = load_settings()
    from osia.models import discovery_status
    return {
        "discovery": discovery_status(),
        "version": 1,
        "providers": providers,
        "models": flat_models,
        "current": str(settings.get("selected_model") or ""),
        "currentProvider": str(settings.get("selected_model_provider") or ""),
        "account": account,
        "inventory": inventory,
    }


def request_remote_refresh(callback: Callable[[], None]) -> None:
    """Coalesce provider discovery and notify listeners when the cache changes."""
    global _remote_refreshing
    with _remote_lock:
        _remote_callbacks.append(callback)
        if _remote_refreshing:
            return
        _remote_refreshing = True

    def done(models: list) -> None:
        global _remote_models, _remote_refreshing
        with _remote_lock:
            _remote_models = [dict(item) for item in models if isinstance(item, dict)]
            callbacks = list(_remote_callbacks)
            _remote_callbacks.clear()
            _remote_refreshing = False
        for notify in callbacks:
            try:
                notify()
            except Exception:
                pass

    discover_models_async(done, api_keys=load_api_keys())


def validate_and_select(provider_id: str, model_id: str) -> bool:
    provider_id = str(provider_id or "").strip().lower()
    model_id = str(model_id or "").strip()
    if not provider_id or not model_id:
        return False
    catalogue = build_catalogue(refresh_local=True, refresh_account=True)
    valid = any(
        model["providerId"] == provider_id and model["id"] == model_id
        for model in catalogue["models"]
    )
    if not valid:
        return False
    if provider_id == ACCOUNT_PROVIDER_ID:
        from osia.chatgpt_account.provider import get_account_provider

        get_account_provider().set_selected_model(model_id)
    set_setting("selected_model", model_id)
    set_setting("selected_model_provider", provider_id)
    return True


def validate_and_set_reasoning(provider_id: str, model_id: str, option_id: str) -> bool:
    provider_id = str(provider_id or "").strip().lower()
    model_id = str(model_id or "").strip()
    option_id = str(option_id or "").strip().lower()
    if not provider_id or not model_id or not option_id:
        return False
    catalogue = build_catalogue(refresh_local=False, refresh_account=False)
    model = next(
        (item for item in catalogue["models"]
         if item["providerId"] == provider_id and item["id"] == model_id),
        None,
    )
    if model is None:
        return False
    valid_options = {item["id"] for item in model["reasoning"]["options"]}
    if option_id not in valid_options:
        return False
    settings = get_reasoning_settings()
    levels = dict(settings.get("models") or {})
    key = f"chatgpt_account:{model_id}" if provider_id == ACCOUNT_PROVIDER_ID else model_id
    levels[key] = option_id
    settings["models"] = levels
    set_reasoning_settings(settings)
    return True


def start_device_login(callback: Callable[[dict], None]) -> dict:
    """Start one official device-code login; never persist or log its code."""
    global _login_connecting, _login_ephemeral
    with _login_lock:
        if _login_connecting:
            return account_snapshot(refresh_models=False, include_ephemeral=True)
        _login_connecting = True
        _login_ephemeral = {}

    def worker() -> None:
        global _login_connecting, _login_ephemeral
        try:
            from osia.chatgpt_account.provider import get_account_provider

            provider = get_account_provider()
            login = provider.begin_device_code_login()
            ephemeral = {
                "verificationUrl": str(login.get("verificationUrl") or ""),
                "userCode": str(login.get("userCode") or ""),
            }
            with _login_lock:
                _login_ephemeral = ephemeral
            callback(account_snapshot(refresh_models=False, include_ephemeral=True))
            provider.wait_for_login(str(login.get("loginId") or ""), timeout=300.0)
        except Exception as exc:
            category = _safe_error(getattr(exc, "category", "login_failed"))
            with _login_lock:
                cancelled = not _login_connecting
            if category in {"login_cancelled", "provider_error"} and cancelled:
                category = "disconnected"
            with _login_lock:
                _login_connecting = False
                _login_ephemeral = {}
            state = account_snapshot(refresh_models=False)
            if not state["connected"]:
                state["error"] = category or "login_failed"
            callback(state)
            return
        with _login_lock:
            _login_connecting = False
            _login_ephemeral = {}
        callback(account_snapshot(refresh_models=True))

    threading.Thread(target=worker, name="osia-mobile-chatgpt-login", daemon=True).start()
    return account_snapshot(refresh_models=False)


def cancel_device_login() -> dict:
    global _login_connecting, _login_ephemeral
    try:
        from osia.chatgpt_account.provider import get_account_provider

        get_account_provider().cancel_login()
    except Exception:
        pass
    with _login_lock:
        _login_connecting = False
        _login_ephemeral = {}
    return account_snapshot(refresh_models=False)


def logout_account() -> dict:
    global _login_connecting, _login_ephemeral
    try:
        from osia.chatgpt_account.provider import get_account_provider

        get_account_provider().logout()
    except Exception:
        pass
    with _login_lock:
        _login_connecting = False
        _login_ephemeral = {}
    return account_snapshot(refresh_models=False)
