"""Small fail-closed client for the stable Codex App Server JSONL protocol.

The process is deliberately isolated from the normal Codex installation and
from OSIA's repository/configuration. Turn-scoped local images are input-only.
Provider-hosted read capabilities and validated image views remain native;
physical local effects are handled only by OSIA's canonical dynamic-tool path.
"""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from osia.runtime import resource_path, is_frozen
from osia.tool_contracts import BROWSER_FAMILY_TOOL_NAMES, BROWSER_VERIFY_TOOL_NAMES
from osia.version import PRODUCT_VERSION
from osia.utils.subprocess_policy import (
    ProcessMode,
    process_kwargs,
    run_background_probe,
)

from .rate_limits import (
    merge_rate_limit_payload,
    sanitize_rate_limit_payload,
)


SUPPORTED_MIN_VERSION = (0, 144, 1)
SUPPORTED_MAX_VERSION = (0, 145, 0)
_DYNAMIC_IMAGE_BRIDGE_RUNTIME_RANGE = ((0, 144, 1), (0, 145, 0))
# Unmanaged executables must match a reviewed version. The OSIA updater verifies
# the App Server contract before publishing newer, OSIA-owned packages.
CURRENT_RUNTIME_VERSION = (0, 166, 0)
_ADDITIONAL_SUPPORTED_RUNTIME_VERSIONS = frozenset({(0, 156, 1), CURRENT_RUNTIME_VERSION})
_UPDATE_VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_UPDATE_READY_FILE = ".osia-verified-v1"
CLIENT_VERSION = PRODUCT_VERSION
_IMAGE_LOG_REDACTION_POLL_SECONDS = 0.5

_SAFE_CONFIG = """# Managed by OSIA for the isolated ChatGPT-account provider.
check_for_update_on_startup = false
web_search = "live"
approval_policy = "on-request"
sandbox_mode = "read-only"

[features]
apps = false
plugins = false

[tools]
web_search = true

[shell_environment_policy]
inherit = "core"
"""

_NO_ENV_SAFE_CONFIG = _SAFE_CONFIG.replace('inherit = "core"', 'inherit = "none"')

_LEGACY_SAFE_CONFIG = """# Managed by OSIA for the isolated ChatGPT-account provider.
check_for_update_on_startup = false
web_search = "disabled"
approval_policy = "never"
sandbox_mode = "read-only"

[features]
apps = false
plugins = false

[tools]
web_search = false

[shell_environment_policy]
inherit = "none"
"""

_SAFE_ITEM_TYPES = frozenset({
    "agentMessage", "userMessage", "reasoning", "plan",
    "contextCompaction", "webSearch", "imageView",
})
_FORBIDDEN_ITEM_TYPES = frozenset(
    {
        "collabAgentToolCall",
        "dynamicToolCall",
        "hookPrompt",
        "imageGeneration",
        "mcpToolCall",
        "sleep",
        "subAgentActivity",
    }
)
_SAFE_NOTIFICATIONS = frozenset(
    {
        "account/login/completed",
        "account/rateLimits/updated",
        "account/updated",
        "error",
        "item/agentMessage/delta",
        "item/commandExecution/outputDelta",
        "item/commandExecution/terminalInteraction",
        "item/completed",
        "item/fileChange/outputDelta",
        "item/fileChange/patchUpdated",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
        "item/started",
        "model/rerouted",
        "model/safetyBuffering/updated",
        "model/verification",
        "thread/closed",
        "thread/compacted",
        "thread/settings/updated",
        "thread/started",
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "turn/completed",
        "turn/diff/updated",
        "turn/moderationMetadata",
        "turn/plan/updated",
        "turn/started",
        "warning",
    }
)

_CANONICAL_NATIVE_TOOL_NAMES = (
    "declare_outcomes",
    "create_chat_skill",
    "read_file",
    "run_command",
    "open_application",
    "open_uri",
    "is_installed",
    "is_running",
    "has_window",
    "steam_game_state",
    "search_public_web",
    "read_public_web_page",
    "write_file",
    "navigate",
    "open_browser",
    "scroll",
    *sorted(BROWSER_FAMILY_TOOL_NAMES),
    "read_document",
    "query_document",
    "inspect_desktop_surfaces",
    "activate_desktop_surface",
    "computer_screenshot",
    "computer_zoom",
    "computer_observation",
    "computer_click",
    "computer_double_click",
    "computer_type",
    "computer_keypress",
    "computer_scroll",
    "computer_actions",
)

_DIRECT_READ_ONLY_NATIVE_TOOLS = frozenset({
    *BROWSER_VERIFY_TOOL_NAMES,
    "steam_game_state",
    "search_public_web",
    "read_public_web_page",
    "inspect_desktop_surfaces",
    "request_visual_observation",
    "computer_screenshot",
    "computer_zoom",
    "computer_observation",
})

# Managed browser effects use the same correlated canonical executor as reads.
# Returning their receipts in place keeps the provider's observations alive.
_INLINE_BROWSER_TOOLS = BROWSER_FAMILY_TOOL_NAMES | {"navigate", "open_browser", "scroll"}
_INLINE_NATIVE_TOOLS = _DIRECT_READ_ONLY_NATIVE_TOOLS | _INLINE_BROWSER_TOOLS

_COMPACT_PROMPT = (
    "Summarize this OSIA conversation so the same task can continue. Keep the "
    "user's current request and constraints, completed work and how it was "
    "verified, uncertain effects, the most recent tool results and targets, "
    "and the next unfinished step. Give more detail to recent work in progress. "
    "Use fresh browser or window references before acting again."
)

_DIRECT_IMAGE_NATIVE_TOOLS = frozenset({
    "request_visual_observation", "computer_screenshot", "computer_zoom",
})


def _normalized_model_id(value: Any) -> str:
    """Normalize harmless surrounding whitespace; model IDs stay exact otherwise."""

    return str(value or "").strip()


_NATIVE_MCP_ALIAS_PREFIX = "osia_"


def _native_mcp_name(name: object) -> bool:
    # Leave room for the alias prefix within the native name's 64 characters.
    return isinstance(name, str) and bool(re.fullmatch(r"mcp__[A-Za-z0-9_-]{1,54}", name))


def _canonical_dynamic_tool_name(name: Any) -> Any:
    if isinstance(name, str) and name.startswith(_NATIVE_MCP_ALIAS_PREFIX):
        candidate = name.removeprefix(_NATIVE_MCP_ALIAS_PREFIX)
        if _native_mcp_name(candidate):
            return candidate
    return name


def _is_canonical_native_handoff(name: object) -> bool:
    if name in _CANONICAL_NATIVE_TOOL_NAMES:
        return True
    if not _native_mcp_name(name):
        return False
    from osia.tool_contracts import get_tool_spec

    spec = get_tool_spec(name)
    return bool(
        spec and spec.dynamic and spec.connector_id
        and (not spec.providers or "chatgpt_account" in spec.providers)
    )


def _canonical_native_tools(execution_strategy=None) -> list[dict]:
    """Expose every available native capability for this provider.

    WOR-300: the native frontier is composed from genuine runtime availability
    alone — canonical wire schemas this app-server can host, membership in the
    complete OSIA inventory, and win32/V4/vision capability for ``computer_*``.
    Request wording, GoalContract/ExecutionStrategy state, frozen effects,
    target conflicts and frontier progress never change it; those remain
    execution-time guards (``tool_preflight``, V4 capture/action gates) that
    can still refuse a visible call.
    """

    del execution_strategy  # kept for call compatibility; never consulted
    import sys

    from osia.capability_federation import federated_capabilities
    from osia.tool_contracts import dynamic_tool_specs, get_tool_spec

    available_osia = {
        item.identity
        for item in federated_capabilities("chatgpt_account")
        if item.available and item.source == "osia"
    }
    tools = []
    for name in _CANONICAL_NATIVE_TOOL_NAMES:
        if name.startswith("computer_"):
            if sys.platform != "win32":
                # V4 exact-window capture/actuation is win32-only.
                continue
            try:
                from osia.computer_use_v4_integration import (
                    COMPUTER_USE_V4_TOOL_NAMES,
                    computer_use_v4_tools_available,
                )
            except Exception:
                continue
            if (
                name not in COMPUTER_USE_V4_TOOL_NAMES
                or not computer_use_v4_tools_available()
            ):
                continue
        if name not in available_osia:
            continue
        spec = get_tool_spec(name)
        if spec is None:
            continue
        input_schema = deepcopy(spec.input_schema)
        if name == "activate_desktop_surface":
            properties = input_schema.get("properties") or {}
            input_schema["properties"] = {
                key: deepcopy(properties[key])
                for key in ("surface_ref", "objective", "action_pattern")
                if key in properties
            }
            input_schema["properties"]["action_pattern"] = {
                "type": "string",
                "const": "Window",
            }
            input_schema["required"] = [
                "surface_ref", "objective", "action_pattern",
            ]
            input_schema["additionalProperties"] = False
        tools.append(
            {
                "type": "function",
                "name": spec.name,
                "description": spec.capability,
                "inputSchema": input_schema,
            }
        )
    # MCP requests use the same canonical handoff as other OSIA tools.
    # Names outside the provider's native format remain available via text.
    tools.extend(
        {
            "type": "function",
            "name": _NATIVE_MCP_ALIAS_PREFIX + spec.name,
            "description": spec.capability,
            "inputSchema": deepcopy(spec.input_schema),
        }
        for spec in dynamic_tool_specs()
        if spec.connector_id and _native_mcp_name(spec.name)
        and (not spec.providers or "chatgpt_account" in spec.providers)
    )
    return tools


def _exact_canonical_native_call(params: dict) -> dict | None:
    """Return one value-only exact handoff; never infer from native commands."""

    namespace = params.get("namespace")
    tool = _canonical_dynamic_tool_name(params.get("tool"))
    arguments = params.get("arguments")
    call_id = params.get("callId") or params.get("call_id")
    if namespace not in {None, ""}:
        return None
    if not _is_canonical_native_handoff(tool):
        return None
    if not isinstance(arguments, dict) or "tool" in arguments:
        return None
    if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
        return None
    return {
        "tool": str(tool),
        "arguments": deepcopy(arguments),
        "call_id": call_id,
    }


class AppServerError(RuntimeError):
    """Sanitized App Server failure safe to cross the provider boundary."""

    def __init__(
        self,
        message: str,
        *,
        output_started: bool = False,
        native_action: bool = False,
        controlled_refusal: bool = False,
        native_tool_call: dict | None = None,
        reformulatable_native_action: bool = False,
        allow_fallback: bool = True,
        category: str = "app_server",
        method: str | None = None,
        server_code: int | None = None,
        capability_events=(),
        native_turn_id: str | None = None,
    ):
        super().__init__(message)
        self.output_started = bool(output_started)
        self.native_action = bool(native_action)
        self.controlled_refusal = bool(controlled_refusal)
        self.native_tool_call = (
            deepcopy(native_tool_call) if isinstance(native_tool_call, dict) else None
        )
        self.reformulatable_native_action = bool(reformulatable_native_action)
        self.allow_fallback = bool(allow_fallback)
        self.category = category
        self.method = str(method) if method else None
        self.server_code = (
            server_code
            if isinstance(server_code, int) and not isinstance(server_code, bool)
            else None
        )
        self.capability_events = tuple(capability_events or ())
        self.native_turn_id = str(native_turn_id) if native_turn_id else None


class RuntimeUnavailableError(AppServerError):
    """The required native Codex runtime is missing or incompatible."""


_NATIVE_ACTION_REFUSAL_KEY = "chatgpt_account.native_action_refused"


def _latest_user_text(turn_content: str | list[dict]) -> str:
    """Return the latest user text from a direct or conversation turn input."""
    if isinstance(turn_content, list):
        text = "\n\n".join(
            str(part.get("text") or "")
            for part in turn_content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text = str(turn_content or "")

    user_markers = list(re.finditer(r"(?m)^\[USER\][ \t]*$", text))
    if not user_markers:
        return text
    latest = text[user_markers[-1].end():].lstrip("\r\n")
    next_role = re.search(
        r"(?m)^\[(?:ASSISTANT|SYSTEM|TOOL|USER)\][ \t]*$",
        latest,
    )
    return latest[:next_role.start()].rstrip() if next_role else latest.rstrip()


def _native_action_refusal(turn_content: str | list[dict]) -> str:
    """Return the user-language refusal for blocked native actions."""
    from osia import i18n
    from osia.turn_context import normalize_locale, resolve_turn_context

    saved_language = normalize_locale(i18n.get_language())
    language = resolve_turn_context(
        _latest_user_text(turn_content),
        saved_language=saved_language,
    )
    language = (
        saved_language
        if language.source == "natural_model" and saved_language else
        language.language
    )
    translations = i18n.TRANSLATIONS.get(_NATIVE_ACTION_REFUSAL_KEY, {})
    return translations.get(language) or i18n.t(_NATIVE_ACTION_REFUSAL_KEY)


@dataclass(frozen=True)
class CapabilityEvent:
    source: str
    identity: str
    phase: str
    outcome: str
    item_id: str
    tool_attempt_id: str = ""
    tool_name: str = ""
    arguments: dict | None = None
    semantic_node_id: str = ""
    semantic_postcondition: str = ""
    reason_code: str = ""
    process_facts: dict | None = None


@dataclass(frozen=True)
class TurnResult:
    text: str
    thread_id: str
    turn_id: str | None
    status: str
    output_started: bool = False
    capability_events: tuple[CapabilityEvent, ...] = ()
    public_reasoning_summary: str = ""
    response_items: tuple[dict, ...] = ()
    final_output_item_id: str = ""
    model_rerouted: bool = False
    effective_model_id: str = ""
    image_input_present: bool = False
    native_tool_names: tuple[str, ...] = ()
    native_tool_schema_fingerprint: str = ""
    strategy_revision: int = 0
    world_revision: int = 0
    native_tool_call: dict | None = None


@dataclass
class _PendingRequest:
    response: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))


@dataclass
class _CompactionRequest:
    event: threading.Event = field(default_factory=threading.Event)
    turn_id: str = ""
    compacted: bool = False
    succeeded: bool = False


@dataclass
class _TurnAttempt:
    thread_id: str
    conversation_id: str = ""
    osia_turn_id: str = ""
    client_request_id: str = ""
    final_response_message_id: str = ""
    model_id: str = ""
    user_request: str = ""
    events: queue.Queue = field(default_factory=queue.Queue)
    turn_id: str | None = None
    text_parts: list[str] = field(default_factory=list)
    output_started: bool = False
    native_action: bool = False
    native_tool_call: dict | None = None
    unsafe_category: str | None = None
    interrupt_sent: bool = False
    capability_events: list[CapabilityEvent] = field(default_factory=list)
    native_image_views: dict[str, dict] = field(default_factory=dict)
    terminal_native_items: set[str] = field(default_factory=set)
    native_effect_started: bool = False
    outcome_unknown: bool = False
    terminal_permission_outcome: str = ""
    execution_strategy: Any = None
    public_reasoning_summary_parts: dict[str, dict] = field(default_factory=dict)
    public_reasoning_summary_order: list[str] = field(default_factory=list)
    public_reasoning_summary: str = ""
    public_reasoning_summary_emitted: str = ""
    dynamic_items: dict[str, dict] = field(default_factory=dict)
    dynamic_tool_responses: dict[str, dict] = field(default_factory=dict)
    dynamic_response_lock: Any = field(
        default_factory=threading.RLock,
        repr=False,
    )
    dynamic_response_sent: bool = False
    first_causal_failure: str = ""
    visual_frame_calls: dict[str, dict] = field(default_factory=dict)
    visual_consumed_calls: set[str] = field(default_factory=set)
    post_visual_agent_items: set[str] = field(default_factory=set)
    local_image_delivery_ids: set[str] = field(default_factory=set)
    bridged_visual_delivery_ids: set[str] = field(default_factory=set)
    bridged_visual_paths: list[Path] = field(default_factory=list)
    visual_delivery_frozen: bool = False
    visual_bridge_failure_state: str = ""
    visual_delivery_required_failed: bool = False
    model_successor_observed: bool = False
    agent_delta_sequence: int = 0
    pending_turn_notifications: list[tuple[str, dict]] = field(default_factory=list)
    pending_server_requests: list[dict] = field(default_factory=list)
    model_rerouted: bool = False
    effective_model_id: str = ""
    model_identity_changed: bool = False
    image_input_present: bool = False
    agent_message_items: dict[str, dict] = field(default_factory=dict)
    pending_agent_message_items: set[str] = field(default_factory=set)
    response_item_diagnostics: list[dict] = field(default_factory=list)
    deferred_done: dict | None = None
    final_output_item_id: str = ""


def _selected_model_identity_changed(attempt: _TurnAttempt) -> bool:
    effective_model_id = attempt.effective_model_id or attempt.model_id
    return bool(
        attempt.model_identity_changed
        or _normalized_model_id(effective_model_id)
        != _normalized_model_id(attempt.model_id)
    )


def _runtime_base_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / "OSIA" / "Codex"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OSIA" / "Codex"
    root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return root / "OSIA" / "Codex"


_DATA_IMAGE_PATTERN = re.compile(
    r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)",
    re.IGNORECASE,
)
_RAW_IMAGE_LOG_SQL_WHERE = "(" + " OR ".join(
    f"instr(lower(feedback_log_body), 'data:image/{mime};base64,') > 0"
    for mime in ("png", "jpeg", "webp", "gif")
) + ")"


def redact_app_server_log_body(value: object) -> str:
    """Replace private pixels with bounded mechanical delivery metadata."""

    body = str(value or "")

    def replace(match: re.Match) -> str:
        mime = str(match.group(1) or "image/unknown").lower()
        encoded = str(match.group(2) or "")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
            byte_count = len(image_bytes)
            digest = hashlib.sha256(image_bytes).hexdigest()[:16]
        except (ValueError, TypeError, binascii.Error):
            byte_count = 0
            digest = "invalid"
        return (
            "osia:image_redacted:"
            f"mime={mime};bytes={byte_count};sha256={digest}"
        )

    return _DATA_IMAGE_PATTERN.sub(replace, body)


def _version_tuple(raw: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?:codex-cli\s+)?(\d+)\.(\d+)\.(\d+)", raw or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _is_supported_runtime_version(version: tuple[int, int, int] | None) -> bool:
    if version is None:
        return False
    return (
        SUPPORTED_MIN_VERSION <= version < SUPPORTED_MAX_VERSION
        or version in _ADDITIONAL_SUPPORTED_RUNTIME_VERSIONS
    )


def _updated_runtime_dir(base_dir: Path) -> Path:
    return base_dir / "runtime" / "updates"


def _updated_runtime_candidates(base_dir: Path) -> dict[Path, str]:
    executable = "codex.exe" if sys.platform == "win32" else "codex"
    candidates = []
    try:
        root = _updated_runtime_dir(base_dir)
        if root.resolve() != base_dir.resolve() / "runtime" / "updates":
            return {}
        for package in root.iterdir():
            match = _UPDATE_VERSION_PATTERN.fullmatch(package.name)
            if (
                match and (package / _UPDATE_READY_FILE).is_file()
                and package.resolve() == root.resolve() / package.name
            ):
                version = tuple(map(int, match.groups()))
                if version >= CURRENT_RUNTIME_VERSION:
                    candidates.append((version, package / "bin" / executable, package.name))
    except OSError:
        pass
    return {path: f"codex-cli {version}" for _, path, version in sorted(candidates, reverse=True)}


def _managed_runtime_candidates(base_dir: Path | None = None) -> list[Path]:
    """Return OSIA-owned runtimes before any machine-global installation."""
    executable = "codex.exe" if sys.platform == "win32" else "codex"
    candidates: list[Path] = []
    if base_dir is not None:
        candidates.extend(
            (
                Path(base_dir) / "runtime" / "current" / "bin" / executable,
                Path(base_dir) / "runtime" / "bin" / executable,
            )
        )
    candidates.append(resource_path("runtimes", "codex", "bin", executable))
    return candidates


def _native_candidates(base_dir: Path | None = None) -> list[Path]:
    candidates: list[Path] = _managed_runtime_candidates(base_dir)
    # A packaged install must prove its own dependency is usable. A developer's
    # global CLI/override must never mask a missing runtime in the distributable.
    if is_frozen():
        return candidates
    override = os.environ.get("OSIA_CODEX_EXECUTABLE")
    if override:
        candidates.append(Path(override))

    if sys.platform == "win32":
        version = ".".join(map(str, CURRENT_RUNTIME_VERSION))
        candidates.append(resource_path("build", "codex-runtime", version, "bin", "codex.exe"))
        wrapper = shutil.which("codex")
        if wrapper:
            npm_root = Path(wrapper).resolve().parent / "node_modules" / "@openai" / "codex"
            patterns = (
                "node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe",
                "node_modules/@openai/codex-win32-*/vendor/*/codex/codex.exe",
            )
            for pattern in patterns:
                candidates.extend(sorted(npm_root.glob(pattern)))
        direct = shutil.which("codex.exe")
        if direct:
            candidates.append(Path(direct))
    else:
        direct = shutil.which("codex")
        if direct:
            candidates.append(Path(direct))

    out: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _probe_executable(candidate: Path) -> str | None:
    if not candidate.is_file():
        return None
    try:
        result = run_background_probe(
            [str(candidate), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (result.stdout or result.stderr or "").strip()
    return raw if result.returncode == 0 and _version_tuple(raw) else None


class AppServerClient:
    """Single-process, thread-safe owner of a stable stdio App Server."""

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        process_factory: Callable | None = None,
        runtime_executable: Path | None = None,
        runtime_version_override: str | None = None,
        disable_code_mode_host: bool = False,
        runtime_partition: str = "",
        compact_logs_on_startup: bool = True,
        startup_timeout: float = 15.0,
        request_timeout: float = 30.0,
        tool_attempt_store=None,
    ):
        self.base_dir = Path(base_dir) if base_dir else _runtime_base_dir()
        partition = str(runtime_partition or "").strip().lower()
        if partition and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", partition):
            raise ValueError("invalid_app_server_runtime_partition")
        directory_suffix = f"-{partition}" if partition else ""
        self.home_dir = self.base_dir / "home"
        self.workspace_dir = self.base_dir / f"workspace{directory_suffix}"
        self.temp_dir = self.base_dir / f"temp{directory_suffix}"
        self._process_factory = process_factory or subprocess.Popen
        self._runtime_executable = Path(runtime_executable) if runtime_executable else None
        self._runtime_version_override = runtime_version_override
        self._disable_code_mode_host = bool(disable_code_mode_host)
        self._compact_logs_on_startup = bool(compact_logs_on_startup)
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self._tool_attempt_store = tool_attempt_store

        self._process = None
        self._start_lock = threading.RLock()
        self._runtime_lock = threading.Lock()
        self._resolved_runtime: tuple[Path, str] | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._next_id = 1
        self._pending: dict[str, _PendingRequest] = {}
        self._attempts: dict[str, _TurnAttempt] = {}
        self._context_usage: dict[str, tuple[int, int]] = {}
        self._compacting_threads: set[str] = set()
        self._compaction_requests: dict[str, _CompactionRequest] = {}
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._wait_thread: threading.Thread | None = None
        self._intentional_processes: set[int] = set()
        self._generation = 0
        self._account_generation = 0
        self._stderr_categories: dict[str, int] = {}
        self._crash_times: list[float] = []
        self._rate_limits_refresh_lock = threading.Lock()
        self._rate_limits_cache: dict | None = None
        self._rate_limits_pending_update: dict = {}
        self._image_log_redaction_pending = False
        self._image_log_checkpoint_pending = False
        self._log_redaction_lock = threading.Lock()
        self._log_redaction_stop = threading.Event()
        self._log_redaction_thread: threading.Thread | None = None

        self._login_lock = threading.Lock()
        self._active_login: dict | None = None
        self._login_events: dict[str, tuple[threading.Event, dict]] = {}

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def account_generation(self) -> int:
        with self._state_lock:
            return self._account_generation

    def is_runtime_available(self) -> bool:
        try:
            self._resolve_runtime()
            return True
        except RuntimeUnavailableError:
            return False

    def get_runtime_version(self) -> str | None:
        try:
            _, version = self._resolve_runtime()
            return version
        except RuntimeUnavailableError:
            return None

    def _resolve_runtime(self) -> tuple[Path, str]:
        with self._runtime_lock:
            if self._resolved_runtime is None:
                self._resolved_runtime = self._select_runtime()
            return self._resolved_runtime

    def _select_runtime(self) -> tuple[Path, str]:
        if self._runtime_executable is not None:
            version = self._runtime_version_override or _probe_executable(self._runtime_executable)
            if not version:
                raise RuntimeUnavailableError(
                    "ERROR: the native Codex runtime could not be started.",
                    category="runtime_missing",
                )
            parsed = _version_tuple(version)
            if not _is_supported_runtime_version(parsed):
                raise RuntimeUnavailableError(
                    "ERROR: the installed Codex runtime is incompatible with ChatGPT account support.",
                    category="runtime_incompatible",
                )
            return self._runtime_executable, version

        incompatible = False
        updates = (
            _updated_runtime_candidates(self.base_dir)
            if is_frozen() or not os.environ.get("OSIA_CODEX_EXECUTABLE") else {}
        )
        for candidate in dict.fromkeys((*updates, *_native_candidates(self.base_dir))):
            version = _probe_executable(candidate)
            if not version:
                continue
            expected = updates.get(candidate)
            supported = (
                version == expected if expected is not None
                else _is_supported_runtime_version(_version_tuple(version))
            )
            if supported:
                self._runtime_executable = candidate
                self._runtime_version_override = version
                return candidate, version
            incompatible = True
        category = "runtime_incompatible" if incompatible else "runtime_missing"
        message = (
            "ERROR: the installed Codex runtime is incompatible with ChatGPT account support."
            if incompatible
            else "ERROR: the native Codex runtime is not installed. APIs remain available."
        )
        raise RuntimeUnavailableError(message, category=category)

    def _ensure_layout(self) -> None:
        for path in (self.home_dir, self.workspace_dir, self.temp_dir):
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise RuntimeUnavailableError(
                    "ERROR: OSIA's isolated Codex directories cannot be symbolic links.",
                    category="isolation_failed",
                )

        forbidden = (
            "plugins", "hooks", "rules", "profiles", "AGENTS.md",
            ".codex", ".codex-plugin",
        )
        for name in forbidden:
            if (self.home_dir / name).exists():
                raise RuntimeUnavailableError(
                    "ERROR: the isolated Codex configuration is not clean; ChatGPT account support was disabled.",
                    category="isolation_failed",
                )
        skills = self.home_dir / "skills"
        if skills.exists():
            custom = [p for p in skills.iterdir() if p.name != ".system"]
            if custom:
                raise RuntimeUnavailableError(
                    "ERROR: custom Codex skills were found in OSIA's isolated runtime.",
                    category="isolation_failed",
                )
            system_skills = skills / ".system"
            if system_skills.exists():
                allowed_system = {
                    ".codex-system-skills.marker", "imagegen", "openai-docs",
                    "plugin-creator", "review-agent", "skill-creator",
                    "skill-installer",
                }
                unexpected = [
                    path for path in system_skills.iterdir()
                    if path.name not in allowed_system
                ]
                if unexpected:
                    raise RuntimeUnavailableError(
                        "ERROR: unexpected skills were found in OSIA's isolated Codex runtime.",
                        category="isolation_failed",
                    )

        config = self.home_dir / "config.toml"
        if config.exists():
            try:
                current_config = config.read_text(encoding="utf-8")
                if current_config in {_LEGACY_SAFE_CONFIG, _NO_ENV_SAFE_CONFIG}:
                    config.write_text(_SAFE_CONFIG, encoding="utf-8", newline="\n")
                elif current_config != _SAFE_CONFIG:
                    raise RuntimeUnavailableError(
                        "ERROR: OSIA's isolated Codex configuration was modified.",
                        category="isolation_failed",
                    )
            except OSError as exc:
                raise RuntimeUnavailableError(
                    "ERROR: OSIA could not verify its isolated Codex configuration.",
                    category="isolation_failed",
                ) from exc
        else:
            config.write_text(_SAFE_CONFIG, encoding="utf-8", newline="\n")
        from osia.image_inputs import (
            cleanup_media_store,
            cleanup_owned_temp_images,
        )

        cleanup_media_store()
        cleanup_owned_temp_images(directories=(self.temp_dir,))
        log_cleanup_proven = self._drain_operational_image_logs(
            compact=self._compact_logs_on_startup,
        )
        if self._compact_logs_on_startup and not log_cleanup_proven:
            raise RuntimeUnavailableError(
                "ERROR: OSIA could not prove private image log cleanup.",
                category="isolation_failed",
            )

    def _app_server_command(self, executable: Path) -> list[str]:
        command = [
            str(executable), "app-server", "--stdio", "--strict-config",
        ]
        # `unified_exec` selects the PTY-backed implementation; `shell_tool`
        # is the separate feature gate that registers the native command tool.
        command.extend((
            "--enable", "code_mode",
            "--enable", "code_mode_host",
            "--disable", "code_mode_only",
            "--disable", "shell_tool",
            "--disable", "unified_exec",
            "--disable", "browser_use",
            "--disable", "browser_use_external",
            "--disable", "browser_use_full_cdp_access",
            "--disable", "computer_use",
            "--disable", "apps",
            "--disable", "plugins",
            "--disable", "standalone_web_search",
            "--disable", "in_app_browser",
            "--config", 'web_search="disabled"',
            "--config", "tools.web_search=false",
        ))
        # The legacy 0.144 runtime predates this gate and rejects unknown
        # feature names before JSONL initialization.
        version = _version_tuple(self._runtime_version_override or _probe_executable(executable) or "")
        if version in _ADDITIONAL_SUPPORTED_RUNTIME_VERSIONS or (
            version is not None and version >= CURRENT_RUNTIME_VERSION
        ):
            command.extend(("--disable", "in_app_local_automation"))
        if self._disable_code_mode_host:
            # Keep ordinary code-mode-only account turns unchanged.  This
            # alternate client is selected only for a turn whose exact
            # frontier contains the correlated visual dynamic tool.
            command.extend(("--disable", "code_mode_host"))
        return command

    def redact_operational_image_logs(
        self,
        *,
        limit: int = 64,
        compact: bool = False,
    ) -> int:
        """Schema-aware bounded redaction of the isolated App Server log DB."""

        database = self.home_dir / "logs_2.sqlite"
        if not database.is_file() or database.is_symlink():
            return 0
        try:
            import sqlite3

            connection = sqlite3.connect(str(database), timeout=0.25)
            try:
                connection.execute("PRAGMA secure_delete = ON")
                columns = {
                    str(row[1]) for row in connection.execute(
                        "PRAGMA table_info(logs)"
                    ).fetchall()
                }
                if not {"id", "feedback_log_body", "estimated_bytes"}.issubset(
                    columns
                ):
                    return -1
                remaining = max(1, min(int(limit), 16_384))
                changed = 0
                while remaining > 0:
                    rows = connection.execute(
                        "SELECT id, feedback_log_body FROM logs "
                        f"WHERE {_RAW_IMAGE_LOG_SQL_WHERE} "
                        "ORDER BY id ASC LIMIT ?",
                        (min(64, remaining),),
                    ).fetchall()
                    if not rows:
                        break
                    batch_changed = 0
                    for row_id, body in rows:
                        redacted = redact_app_server_log_body(body)
                        if redacted == str(body or ""):
                            continue
                        connection.execute(
                            "UPDATE logs SET feedback_log_body = ?, "
                            "estimated_bytes = ? WHERE id = ?",
                            (redacted, len(redacted.encode("utf-8")), int(row_id)),
                        )
                        batch_changed += 1
                    connection.commit()
                    changed += batch_changed
                    remaining -= len(rows)
                    if batch_changed == 0:
                        break
                # VACUUM rebuilds the database even when a previous pass already
                # removed every logical match, purging old cells/freelist pages.
                if compact:
                    connection.execute("VACUUM")
                if compact or changed or self._image_log_checkpoint_pending:
                    self._image_log_checkpoint_pending = True
                    checkpoint = connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    checkpoint_busy = bool(
                        checkpoint and int(checkpoint[0] or 0) != 0
                    )
                    self._image_log_checkpoint_pending = checkpoint_busy
                    if compact and checkpoint_busy:
                        return -1
                return changed
            finally:
                connection.close()
        except Exception:
            return -1

    def _drain_operational_image_logs(self, *, compact: bool) -> bool:
        """Drain every logical match, then physically rebuild and verify."""

        residual = self.operational_image_log_residual_count()
        while residual > 0:
            changed = self.redact_operational_image_logs(
                limit=min(residual, 16_384),
                compact=False,
            )
            current = self.operational_image_log_residual_count()
            if changed <= 0 or current < 0 or current >= residual:
                return False
            residual = current
        if residual < 0:
            return False
        if compact and self.redact_operational_image_logs(
            limit=1,
            compact=True,
        ) < 0:
            return False
        return self.operational_image_log_residual_count() == 0

    def operational_image_log_residual_count(self) -> int:
        """Return 0 only when the canonical log schema proves no raw image row."""

        database = self.home_dir / "logs_2.sqlite"
        if not database.is_file() or database.is_symlink():
            return 0
        try:
            import sqlite3

            connection = sqlite3.connect(str(database), timeout=0.25)
            try:
                columns = {
                    str(row[1]) for row in connection.execute(
                        "PRAGMA table_info(logs)"
                    ).fetchall()
                }
                if not {"id", "feedback_log_body"}.issubset(columns):
                    return -1
                row = connection.execute(
                    "SELECT count(*) FROM logs WHERE "
                    + _RAW_IMAGE_LOG_SQL_WHERE
                ).fetchone()
                return int(row[0]) if row else -1
            finally:
                connection.close()
        except Exception:
            return -1

    def _schedule_operational_log_redaction(self) -> None:
        if not self._image_log_redaction_pending:
            return
        if self._log_redaction_stop.is_set():
            return
        if not self._log_redaction_lock.acquire(blocking=False):
            return

        def redact() -> None:
            try:
                # The native SQLite logger batches asynchronously. Keep polling
                # after an initially empty pass so a delayed insert is scrubbed.
                while (
                    self._image_log_redaction_pending
                    and not self._log_redaction_stop.is_set()
                ):
                    self.redact_operational_image_logs(limit=1024)
                    self._log_redaction_stop.wait(
                        _IMAGE_LOG_REDACTION_POLL_SECONDS
                    )
            finally:
                self._log_redaction_thread = None
                self._log_redaction_lock.release()

        worker = threading.Thread(
            target=redact,
            name="osia-app-server-log-redaction",
            daemon=True,
        )
        self._log_redaction_thread = worker
        worker.start()

    def _stop_operational_log_redaction(self) -> None:
        self._log_redaction_stop.set()
        worker = self._log_redaction_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)

    def isolated_environment(self) -> dict[str, str]:
        """Return the exact secret-minimized child environment (testable)."""
        allow = (
            "SystemRoot",
            "WINDIR",
            "ComSpec",
            "PATHEXT",
            "PROCESSOR_ARCHITECTURE",
            "PROCESSOR_IDENTIFIER",
            "NUMBER_OF_PROCESSORS",
            "OS",
            "USERNAME",
            "LANG",
            "LC_ALL",
        )
        env = {key: os.environ[key] for key in allow if key in os.environ}
        runtime_dir = (
            str(self._runtime_executable.parent)
            if self._runtime_executable is not None else ""
        )
        if sys.platform == "win32":
            system_root = Path(env.get("SystemRoot") or env.get("WINDIR") or r"C:\Windows")
            path_parts = [runtime_dir, str(system_root / "System32"), str(system_root)]
        else:
            path_parts = [runtime_dir, *os.defpath.split(os.pathsep)]
        env["PATH"] = os.pathsep.join(part for part in path_parts if part)
        env.update(
            {
                "CODEX_HOME": str(self.home_dir),
                "HOME": str(self.home_dir),
                "USERPROFILE": str(self.home_dir),
                "TEMP": str(self.temp_dir),
                "TMP": str(self.temp_dir),
                "APPDATA": str(self.home_dir / "appdata"),
                "LOCALAPPDATA": str(self.home_dir / "localappdata"),
                # Reduce ordinary stderr body logging. The independent SQLite
                # sink is still scrubbed and physically compacted by OSIA.
                "RUST_LOG": "warn,codex_core::session::handlers=off",
            }
        )
        return env

    def start(self) -> None:
        with self._start_lock:
            if self._process is not None and self._process.poll() is None:
                return
            self._log_redaction_stop.clear()
            now = time.monotonic()
            self._crash_times = [stamp for stamp in self._crash_times if now - stamp < 60.0]
            if len(self._crash_times) >= 2:
                raise RuntimeUnavailableError(
                    "ERROR: the Codex App Server repeatedly crashed; the account provider is disabled for this session.",
                    category="terminal_session_error",
                )
            executable, _version = self._resolve_runtime()
            self._ensure_layout()
            env = self.isolated_environment()
            Path(env["APPDATA"]).mkdir(parents=True, exist_ok=True)
            Path(env["LOCALAPPDATA"]).mkdir(parents=True, exist_ok=True)
            command = self._app_server_command(executable)
            kwargs = {
                "cwd": str(self.workspace_dir),
                "env": env,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            kwargs = process_kwargs(ProcessMode.BACKGROUND_SUPERVISED, kwargs)
            try:
                self._process = self._process_factory(command, **kwargs)
            except (OSError, subprocess.SubprocessError) as exc:
                self._process = None
                raise RuntimeUnavailableError(
                    "ERROR: the Codex App Server could not be started. APIs remain available.",
                    category="startup_failed",
                ) from exc

            self._generation += 1
            process = self._process
            self._reader_thread = threading.Thread(
                target=self._reader_loop, args=(process,),
                name="osia-codex-stdout", daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._stderr_loop, args=(process,),
                name="osia-codex-stderr", daemon=True,
            )
            self._wait_thread = threading.Thread(
                target=self._wait_loop, args=(process,),
                name="osia-codex-wait", daemon=True,
            )
            self._reader_thread.start()
            self._stderr_thread.start()
            self._wait_thread.start()

            try:
                self._request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "osia_pc",
                            "title": "OSIA",
                            "version": CLIENT_VERSION,
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                    timeout=self.startup_timeout,
                    ensure_started=False,
                )
                self._send({"method": "initialized", "params": {}})
            except Exception:
                self.shutdown()
                raise

    def health_check(self) -> dict:
        self.start()
        return {
            "healthy": self._process is not None and self._process.poll() is None,
            "runtimeVersion": self.get_runtime_version(),
            "generation": self._generation,
        }

    def _next_request_id(self) -> int:
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _send(self, message: dict) -> None:
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        contains_private_image = "data:image" in raw
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                raise AppServerError(
                    "ERROR: the Codex App Server is unavailable.",
                    category="process_stopped",
                )
            try:
                process.stdin.write(raw)
                process.stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                raise AppServerError(
                    "ERROR: the Codex App Server connection closed.",
                    category="process_crash",
                ) from exc
        if contains_private_image:
            self._image_log_redaction_pending = True
            self._schedule_operational_log_redaction()

    def _request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
        ensure_started: bool = True,
    ) -> dict:
        if ensure_started:
            self.start()
        request_id = self._next_request_id()
        pending = _PendingRequest()
        with self._state_lock:
            self._pending[str(request_id)] = pending
        try:
            self._send({"method": method, "id": request_id, "params": params or {}})
            try:
                response = pending.response.get(timeout=timeout or self.request_timeout)
            except queue.Empty as exc:
                raise AppServerError(
                    "ERROR: the Codex App Server did not respond in time.",
                    category="request_timeout",
                ) from exc
            if isinstance(response, AppServerError):
                raise response
            if not isinstance(response, dict) or "error" in response:
                error = (
                    response.get("error")
                    if isinstance(response, dict)
                    and isinstance(response.get("error"), dict)
                    else {}
                )
                code = error.get("code")
                if not isinstance(code, int) or isinstance(code, bool):
                    code = None
                if (
                    method == "thread/resume" and code == -32600
                    and isinstance(params, dict) and params.get("threadId")
                    and str(error.get("message") or "").strip()
                    == f"no rollout found for thread id {params['threadId']}"
                ):
                    raise AppServerError(
                        "ERROR: the saved ChatGPT thread is missing.",
                        allow_fallback=False, category="thread_missing",
                        method=method, server_code=code,
                    )
                from osia import i18n

                raise AppServerError(
                    i18n.t("chatgpt_account.request_rejected"),
                    category="request_rejected",
                    method=method,
                    server_code=code,
                )
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        finally:
            with self._state_lock:
                self._pending.pop(str(request_id), None)

    def _send_request_no_wait(self, method: str, params: dict) -> None:
        request_id = self._next_request_id()
        try:
            self._send({"method": method, "id": request_id, "params": params})
        except AppServerError:
            pass

    def _reader_loop(self, process) -> None:
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            if self._process is not process:
                return
            if self._image_log_redaction_pending:
                self._schedule_operational_log_redaction()
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                if self._process is process:
                    self._fail_all("protocol_invalid")
                continue
            if not isinstance(message, dict):
                if self._process is process:
                    self._fail_all("protocol_invalid")
                continue
            if "id" in message and "method" in message:
                self._handle_server_request(message)
            elif "id" in message:
                with self._state_lock:
                    pending = self._pending.get(str(message.get("id")))
                if pending is not None:
                    try:
                        pending.response.put_nowait(message)
                    except queue.Full:
                        pass
            elif "method" in message:
                self._dispatch_notification(
                    str(message.get("method") or ""),
                    message.get("params") if isinstance(message.get("params"), dict) else {},
                )

    def _stderr_loop(self, process) -> None:
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            if self._process is not process:
                return
            lower = line.lower()
            if "warning" in lower:
                category = "warning"
            elif "error" in lower or "failed" in lower:
                category = "error"
            else:
                category = "diagnostic"
            with self._state_lock:
                self._stderr_categories[category] = self._stderr_categories.get(category, 0) + 1

    def _wait_loop(self, process) -> None:
        if process is None:
            return
        try:
            process.wait()
        except Exception:
            return
        with self._state_lock:
            intentional = id(process) in self._intentional_processes
            self._intentional_processes.discard(id(process))
        if not intentional and self._process is process:
            with self._state_lock:
                self._crash_times.append(time.monotonic())
            if self._process is process:
                self._fail_all("process_crash")

    def _fail_all(self, category: str) -> None:
        error = AppServerError(
            "ERROR: the Codex App Server stopped unexpectedly.",
            category=category,
        )
        with self._state_lock:
            pending = list(self._pending.values())
            attempts = list(self._attempts.values())
            for request in self._compaction_requests.values():
                request.event.set()
            self._compacting_threads.clear()
        for item in pending:
            try:
                item.response.put_nowait(error)
            except queue.Full:
                pass
        for attempt in attempts:
            attempt.events.put(("error", error))
        # Login is also an in-flight RPC lifecycle. Wake its owner now instead
        # of leaving the UI connecting until its unrelated login deadline.
        with self._login_lock:
            waiters = list(self._login_events.values())
            self._login_events.clear()
            self._active_login = None
            for event, state in waiters:
                state.update({
                    "success": False,
                    "cancelled": category == "shutdown",
                    "failure_category": category,
                })
                event.set()

    @staticmethod
    def _thread_id(params: dict) -> str | None:
        value = params.get("threadId")
        return str(value) if value else None

    def _attempt_for(self, params: dict) -> _TurnAttempt | None:
        thread_id = self._thread_id(params)
        with self._state_lock:
            if thread_id:
                return self._attempts.get(thread_id)
            if len(self._attempts) == 1:
                return next(iter(self._attempts.values()))
        return None

    def _turn_attempt_for(self, params: dict) -> _TurnAttempt | None:
        """Return only the exact native thread+turn for streamed output."""

        thread_id = self._thread_id(params)
        if not thread_id:
            return None
        native_turn_id = str(
            params.get("turnId")
            or (
                (params.get("turn") or {}).get("id")
                if isinstance(params.get("turn"), dict) else ""
            )
            or ""
        )
        if not native_turn_id:
            return None
        with self._state_lock:
            attempt = self._attempts.get(thread_id)
            if attempt is None:
                return None
            if not attempt.turn_id:
                return None
            if str(attempt.turn_id) != native_turn_id:
                return None
            return attempt

    def _reasoning_attempt_for(self, params: dict) -> _TurnAttempt | None:
        """Return only an explicitly thread-bound matching public-summary turn."""

        thread_id = self._thread_id(params)
        if not thread_id:
            return None
        with self._state_lock:
            attempt = self._attempts.get(thread_id)
        if attempt is None:
            return None
        native_turn_id = str(params.get("turnId") or "")
        if (
            not native_turn_id
            or not attempt.turn_id
            or str(attempt.turn_id) != native_turn_id
        ):
            return None
        return attempt

    @staticmethod
    def _reasoning_summary_part_key(params: dict) -> str:
        item_id = str(params.get("itemId") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", item_id):
            return ""
        index = params.get("summaryIndex")
        if index is None:
            index = params.get("partIndex", 0)
        if isinstance(index, bool):
            return ""
        try:
            part_index = int(index)
        except (TypeError, ValueError):
            return ""
        if part_index < 0:
            return ""
        return f"{item_id}:{part_index}"

    @staticmethod
    def _sanitize_public_reasoning_summary(value: object) -> str:
        from osia.execution_trace import sanitize_public_reasoning_summary

        return sanitize_public_reasoning_summary(value)

    @staticmethod
    def _public_reasoning_summary_payload(
        attempt: _TurnAttempt,
        summary: str,
    ) -> dict:
        """Carry the provider summary with its already-bound OSIA/native turn."""

        return {
            "summary": summary,
            "conversation_id": attempt.conversation_id,
            "request_id": attempt.client_request_id,
            "turn_id": attempt.osia_turn_id,
            "run_id": attempt.osia_turn_id,
            "provider_id": "chatgpt_account",
            "model_id": attempt.model_id,
            "native_thread_id": attempt.thread_id,
            "native_turn_id": str(attempt.turn_id or ""),
        }

    def _public_reasoning_summary(self, attempt: _TurnAttempt) -> str:
        parts = []
        for key in attempt.public_reasoning_summary_order:
            part = attempt.public_reasoning_summary_parts.get(key) or {}
            text = str(part.get("delta") or part.get("seed") or "")
            if text:
                parts.append(text)
        return self._sanitize_public_reasoning_summary("\n".join(parts))

    def _capture_public_reasoning_summary(
        self,
        attempt: _TurnAttempt,
        method: str,
        params: dict,
    ) -> None:
        key = self._reasoning_summary_part_key(params)
        if not key:
            return
        part = attempt.public_reasoning_summary_parts.get(key)
        if part is None:
            part = {"seed": "", "raw_delta": "", "delta": ""}
            attempt.public_reasoning_summary_parts[key] = part
            attempt.public_reasoning_summary_order.append(key)

        if method == "item/reasoning/summaryPartAdded":
            raw_part = params.get("part")
            if (
                not isinstance(raw_part, dict)
                or raw_part.get("type") != "summary_text"
            ):
                return
            raw_part = raw_part.get("text")
            seed = self._sanitize_public_reasoning_summary(raw_part)
            if seed and not part.get("delta"):
                part["seed"] = seed
                part["raw_seed"] = raw_part
                self._publish_public_reasoning_summary(attempt)
            return

        delta = params.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        raw_delta = str(part.get("raw_delta") or part.get("raw_seed") or "") + delta
        part["raw_delta"] = raw_delta
        combined = self._sanitize_public_reasoning_summary(raw_delta)
        if not combined or combined == part.get("delta"):
            return
        part["delta"] = combined
        self._publish_public_reasoning_summary(attempt)

    def _publish_public_reasoning_summary(self, attempt: _TurnAttempt) -> None:
        """Forward each genuine public update at receipt, without a size gate."""
        aggregate = self._public_reasoning_summary(attempt)
        emitted = attempt.public_reasoning_summary_emitted
        if aggregate and aggregate != emitted:
            attempt.public_reasoning_summary = aggregate
            attempt.public_reasoning_summary_emitted = aggregate
            attempt.events.put((
                "public_reasoning_summary",
                self._public_reasoning_summary_payload(attempt, aggregate),
            ))

    def _mark_unsafe(
        self,
        params: dict,
        category: str,
        *,
        native_tool_call: dict | None = None,
    ) -> None:
        attempt = self._attempt_for(params)
        if attempt is None or attempt.native_action:
            return
        attempt.native_action = True
        attempt.native_tool_call = (
            deepcopy(native_tool_call)
            if isinstance(native_tool_call, dict)
            else None
        )
        attempt.unsafe_category = category
        if params.get("turnId"):
            attempt.turn_id = str(params["turnId"])
        attempt.events.put(("unsafe", category))
        if not attempt.interrupt_sent and attempt.turn_id:
            attempt.interrupt_sent = True
            self._send_request_no_wait(
                "turn/interrupt", {"threadId": attempt.thread_id, "turnId": attempt.turn_id}
            )

    @staticmethod
    def _safe_receipt_ref(value: Any, prefix: str) -> str:
        candidate = str(value or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", candidate):
            return candidate
        digest = hashlib.sha256(candidate.encode("utf-8", errors="replace")).hexdigest()
        return f"{prefix}:{digest}"

    def _attempt_store(self):
        if self._tool_attempt_store is None:
            from osia.tool_attempts import get_tool_attempt_store

            self._tool_attempt_store = get_tool_attempt_store()
        return self._tool_attempt_store

    @staticmethod
    def _native_item_id(params: Mapping[str, Any]) -> str:
        return str(params.get("itemId") or params.get("item_id") or "")

    @staticmethod
    def _agent_message_text(item: Mapping[str, Any]) -> str:
        """Extract only public assistant text from stable App Server item shapes."""

        direct = item.get("text")
        if isinstance(direct, str):
            return direct
        content = item.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            part_type = str(part.get("type") or "")
            if part_type not in {
                "outputText", "output_text", "text", "messageText",
            }:
                continue
            value = part.get("text")
            if isinstance(value, str):
                parts.append(value)
        return "".join(parts)

    @staticmethod
    def _native_image_view_metadata(path_value: str) -> dict:
        """Validate a native image read with OSIA's existing image bounds."""
        from osia.image_inputs import (
            ImageInputError,
            MAX_SOURCE_IMAGE_BYTES,
            _magic_mime,
            _validated_image_geometry,
        )

        path = Path(path_value)
        if not path.is_absolute():
            raise ValueError("image_view_path_not_absolute")
        path = path.resolve(strict=True)
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_IMAGE_BYTES:
            raise ValueError("image_view_path_unavailable")
        data = path.read_bytes()
        mime = _magic_mime(data)
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise ValueError("image_view_format_invalid")
        try:
            width, height = _validated_image_geometry(data, mime)
        except ImageInputError as exc:
            raise ValueError("image_view_image_invalid") from exc
        return {
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
            "mime": mime,
            "width": width,
            "height": height,
        }

    @staticmethod
    def _record_response_item(
        attempt: _TurnAttempt,
        *,
        item_id: str,
        item_type: str,
        phase: str,
        status: str = "",
        text_chars: int = 0,
    ) -> None:
        attempt.response_item_diagnostics.append({
            "item_id": str(item_id or "")[:160],
            "item_type": str(item_type or "unknown")[:80],
            "phase": str(phase or "")[:40],
            "status": str(status or "")[:40],
            "text_present": bool(text_chars),
            "text_chars": max(0, min(int(text_chars or 0), 1_000_000)),
        })
        del attempt.response_item_diagnostics[:-64]

    @staticmethod
    def _append_agent_text(
        attempt: _TurnAttempt,
        text: str,
        *,
        item_id: str = "",
    ) -> bool:
        if not isinstance(text, str) or not text:
            return False
        state = attempt.agent_message_items.setdefault(
            str(item_id or ""), {
                "delta_chars": 0,
                "delta_text": "",
                "completed": False,
            },
        )
        state["delta_chars"] = int(state.get("delta_chars") or 0) + len(text)
        state["delta_text"] = str(state.get("delta_text") or "") + text
        attempt.output_started = True
        if state.get("message_phase") == "commentary":
            wording = AppServerClient._sanitize_public_reasoning_summary(state["delta_text"])
            if wording:
                payload = AppServerClient._public_reasoning_summary_payload(attempt, wording)
                payload["native_item_id"] = str(item_id)
                attempt.events.put(("public_reasoning_summary", payload))
            return True
        attempt.text_parts.append(text)
        if item_id:
            attempt.final_output_item_id = str(item_id)[:160]
        attempt.events.put(("delta", text))
        return True

    def _mark_visual_input_consumed(
        self,
        attempt: _TurnAttempt,
        *,
        successor_item_id: str = "",
    ) -> bool:
        """Record one exact post-image continuation without exposing pixels."""

        successor = str(successor_item_id or "")
        if not successor:
            return False
        correlated_successor = False
        with attempt.dynamic_response_lock:
            for entry in tuple(attempt.visual_frame_calls.values()):
                call_id = str(entry.get("call_id") or "")
                if (
                    not call_id
                    or not entry.get("transport_started")
                    or successor == call_id
                    or successor in set(entry.get("prior_item_ids") or ())
                ):
                    continue
                if not entry.get("response_committed"):
                    pending = entry.setdefault("pending_successor_item_ids", [])
                    if successor not in pending:
                        pending.append(successor)
                        del pending[:-16]
                    continue
                correlated_successor = True
                if call_id in attempt.visual_consumed_calls:
                    continue
                attempt.visual_consumed_calls.add(call_id)
                delivery_id = str(entry.get("delivery_id") or "")
                if delivery_id:
                    try:
                        from osia.image_inputs import acknowledge_media_delivery

                        delivery = acknowledge_media_delivery(delivery_id)
                        if delivery is not None:
                            entry["delivery_state"] = delivery.state
                    except Exception:
                        pass
                if delivery_id and entry.get("delivery_state") != "ACKNOWLEDGED":
                    entry["successor_observed"] = True
                self._acknowledge_bridged_visual_deliveries(attempt)
                if entry.get("tool_name") == "request_visual_observation":
                    try:
                        from osia.desktop_scene import (
                            acknowledge_visual_actuation_frame,
                        )

                        lease = acknowledge_visual_actuation_frame(
                            str(entry.get("frame_id") or ""),
                            provider=attempt.model_id and "chatgpt_account" or "",
                            model=attempt.model_id,
                            owner_id=attempt.conversation_id,
                            turn_id=attempt.osia_turn_id,
                        )
                        entry["actuation_lease_state"] = str(
                            lease.get("lease_state") or ""
                        )
                        entry["actuation_lease_expires_at"] = float(
                            lease.get("lease_expires_at") or 0.0
                        )
                    except Exception:
                        entry["actuation_lease_state"] = "unavailable"
                        entry["actuation_lease_expires_at"] = 0.0
                else:
                    entry["actuation_lease_state"] = "not_required_v4"
                    entry["actuation_lease_expires_at"] = 0.0
                self._append_capability_event(
                    attempt,
                    source="provider_dynamic",
                    identity="imageInput",
                    phase="continued",
                    outcome="success_verified",
                    item_id=call_id,
                    tool_attempt_id=str(
                        entry.get("tool_attempt_id") or call_id
                    ),
                    tool_name=str(entry.get("tool_name") or ""),
                    arguments=entry.get("arguments"),
                    reason_code="IMAGE_INPUT_CONSUMED",
                )
        return correlated_successor

    @staticmethod
    def _acknowledge_local_image_deliveries(
        attempt: _TurnAttempt,
    ) -> None:
        if not attempt.local_image_delivery_ids:
            return
        from osia.image_inputs import acknowledge_media_delivery

        remaining = set()
        for delivery_id in attempt.local_image_delivery_ids:
            try:
                delivery = acknowledge_media_delivery(delivery_id)
            except Exception:
                delivery = None
            if delivery is None or delivery.state != "ACKNOWLEDGED":
                remaining.add(delivery_id)
        attempt.local_image_delivery_ids = remaining

    @staticmethod
    def _acknowledge_bridged_visual_deliveries(
        attempt: _TurnAttempt,
    ) -> None:
        if not attempt.bridged_visual_delivery_ids:
            return
        from osia.image_inputs import acknowledge_media_delivery

        remaining = set()
        for delivery_id in attempt.bridged_visual_delivery_ids:
            try:
                delivery = acknowledge_media_delivery(delivery_id)
            except Exception:
                delivery = None
            if delivery is None or delivery.state != "ACKNOWLEDGED":
                remaining.add(delivery_id)
        attempt.bridged_visual_delivery_ids = remaining

    @staticmethod
    def _release_deferred_done(attempt: _TurnAttempt) -> None:
        if (
            attempt.deferred_done is not None
            and not attempt.pending_agent_message_items
        ):
            payload = attempt.deferred_done
            attempt.deferred_done = None
            attempt.events.put(("done", payload))

    @staticmethod
    def _append_capability_event(
        attempt: _TurnAttempt,
        *,
        source: str,
        identity: str,
        phase: str,
        outcome: str,
        item_id: str,
        tool_attempt_id: str = "",
        tool_name: str = "",
        arguments: Mapping[str, Any] | None = None,
        semantic_node_id: str = "",
        semantic_postcondition: str = "",
        reason_code: str = "",
        process_facts: Mapping[str, Any] | None = None,
    ) -> None:
        from osia.tool_contracts import normalize_process_facts

        attempt.capability_events.append(CapabilityEvent(
            source=source,
            identity=identity,
            phase=phase,
            outcome=outcome,
            item_id=str(item_id or "")[:256],
            tool_attempt_id=str(tool_attempt_id or "")[:256],
            tool_name=str(tool_name or "")[:128],
            arguments=dict(arguments) if isinstance(arguments, Mapping) else None,
            semantic_node_id=str(semantic_node_id or "")[:128],
            semantic_postcondition=str(semantic_postcondition or "")[:512],
            reason_code=str(reason_code or "")[:160],
            process_facts=normalize_process_facts(process_facts),
        ))

    def _deny_native_approval(
        self, attempt: _TurnAttempt, request_id: Any, method: str, item_id: str,
        *, outcome: str = "rejected_before_execution",
        tool_name: str = "",
        arguments: Mapping[str, Any] | None = None,
        semantic_node_id: str = "",
        semantic_postcondition: str = "",
        reason_code: str = "",
    ) -> None:
        try:
            self._send({"id": request_id, "result": {"decision": "decline"}})
        except AppServerError:
            pass
        self._append_capability_event(
            attempt,
            source="provider_native",
            identity=(
                "commandExecution"
                if "commandExecution" in method else "fileChange"
            ),
            phase="denied",
            outcome=outcome,
            item_id=item_id,
            tool_name=tool_name,
            arguments=arguments,
            semantic_node_id=semantic_node_id,
            semantic_postcondition=semantic_postcondition,
            reason_code=reason_code,
        )
        if outcome in {
            "permission_user_denied",
            "permission_presentation_failed",
            "permission_timeout",
            "permission_disconnected",
            "permission_internal_failure",
        }:
            if not attempt.terminal_permission_outcome:
                attempt.terminal_permission_outcome = outcome

    def _resolve_native_approval(
        self, attempt: _TurnAttempt, payload: dict,
    ) -> None:
        """Decline provider-native physical execution; OSIA dynamic tools own effects."""

        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        self._deny_native_approval(
            attempt,
            payload.get("request_id"),
            method,
            self._native_item_id(params),
            reason_code="CANONICAL_OSIA_TOOL_REQUIRED",
        )

    def _complete_native_item(self, attempt: _TurnAttempt, item: dict) -> None:
        """Record an unexpected provider-native physical item without adoption."""

        item_id = str(item.get("id") or "")
        item_type = str(item.get("type") or "")
        status = str(item.get("status") or "")
        if item_type not in {"commandExecution", "fileChange"}:
            return
        if status == "inProgress":
            return
        if item_id in attempt.terminal_native_items:
            return
        attempt.terminal_native_items.add(item_id)
        if status == "declined":
            self._append_capability_event(
                attempt,
                source="provider_native",
                identity=item_type,
                phase="completed",
                outcome="cancelled_proven",
                item_id=item_id,
                reason_code="PROVIDER_NATIVE_EXECUTION_DECLINED",
            )
            return

        # A provider completion is only an observation. It does not establish
        # OSIA dispatch, policy approval, or a canonical ActionTruth receipt.
        attempt.outcome_unknown = True
        attempt.native_effect_started = True
        self._append_capability_event(
            attempt,
            source="provider_native",
            identity=item_type,
            phase="completed",
            outcome="outcome_unknown",
            item_id=item_id,
            reason_code="UNEXPECTED_PROVIDER_NATIVE_PHYSICAL_EFFECT",
        )
        self._mark_unsafe(
            {"threadId": attempt.thread_id, "turnId": attempt.turn_id},
            f"unbrokered_native_item:{item_type}",
        )

    @staticmethod
    def _dynamic_call_from_params(params: Mapping[str, Any]) -> tuple[dict | None, str]:
        """Parse one provider dynamic call without treating unknown tools as holes."""

        namespace = params.get("namespace")
        tool = _canonical_dynamic_tool_name(params.get("tool"))
        arguments = params.get("arguments")
        call_id = params.get("callId") or params.get("call_id")
        if namespace not in {None, ""}:
            return None, "CAPABILITY_UNAVAILABLE"
        if not isinstance(tool, str) or not tool or len(tool) > 128:
            return None, "CAPABILITY_UNAVAILABLE"
        if not isinstance(arguments, dict) or "tool" in arguments:
            return None, "INVALID_TOOL_ARGUMENTS"
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
            return None, "TOOL_RESPONSE_ID_MISMATCH"
        return {
            "tool": tool,
            "arguments": deepcopy(arguments),
            "call_id": call_id,
        }, ""

    @staticmethod
    def _dynamic_call_signature(call: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            {
                "tool": str(call.get("tool") or ""),
                "arguments": (
                    call.get("arguments")
                    if isinstance(call.get("arguments"), dict) else {}
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda _value: "<unsupported>",
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _visual_provider_result(
        result: Any,
        *,
        attached_now: bool = True,
        reconciliation_postcondition: str = "",
    ) -> Any:
        """Expose pixels plus mechanical metadata, not a duplicate UIA scene."""

        if not isinstance(result, dict):
            return result
        observation = result.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        receipt = result.get("receipt")
        receipt = receipt if isinstance(receipt, dict) else {}
        allowed_receipt = {
            "schema_version", "dispatch_state", "outcome", "reason_code",
            "surface_ref", "window_surface_ref", "application",
            "requested_application", "resolved_target_candidates",
            "target_conflict", "window_generation", "structural_epoch",
            "observed_at", "captured_at", "expires_at", "image_sha256", "image_id",
            "image_mime", "image_width", "image_height",
            "native_window_token", "native_window_id", "capture_method",
            "capture_driver", "capture_attempts", "fallback_used",
            "exact_surface_pixels", "target_foreground", "frame_id",
            "surface_generation", "provider_id", "model_id",
            "v4_loop_id",
            "image_part_count", "image_input_sent", "image_input_resent",
            "dimensions", "image_part_dimensions", "source_dimensions",
            "mime", "crop_rectangle", "source_frame_id", "refine_count",
            "crop_chain", "target_identity", "source_bounds",
            "content_coverage", "source_pixel_format",
            "pixel_format_conversion", "alpha_handling",
            "source_color_space", "color_space_conversion",
        }
        mechanical_receipt = {
            key: deepcopy(value)
            for key, value in receipt.items()
            if key in allowed_receipt
        }
        reconciliation_instruction = (
            " This frame is exclusively for read-only reconciliation of this "
            "intermediate postcondition: "
            + str(reconciliation_postcondition)[:512]
            + ". Inspect the pixels, then call computer_observation exactly "
            "once with this frame_id and status satisfied, not_satisfied, or "
            "inconclusive. Do not advance to a later mutation, and do not "
            "substitute provider prose for the typed observation."
            if reconciliation_postcondition else ""
        )
        text = json.dumps({
            "contract": (
                "Exact read-only pixels from the reported target are attached "
                "to this tool result. Use the image for visible appearance; "
                if attached_now else
                "The same exact pixels were attached to an earlier correlated "
                "tool result in this model turn; they are not duplicated here. "
            ) + (
                "mechanical metadata below is not a semantic description. "
                "If desktop_click is independently applicable and chosen, "
                "copy visual_frame_id exactly and supply the normalized bbox "
                "derived from these pixels. When computer_observation is "
                "available, use it after inspecting the image to ground any "
                "already-satisfied or post-action visual claim; prose alone "
                "is not verification evidence."
            ) + reconciliation_instruction,
            "visual_frame_id": mechanical_receipt.get("frame_id", ""),
            "observation": observation,
            "target_identity": mechanical_receipt.get("target_identity", {}),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "text": text,
            "visual_frame_id": mechanical_receipt.get("frame_id", ""),
            "observation": deepcopy(observation),
            "receipt": mechanical_receipt,
        }

    @staticmethod
    def _dynamic_result_envelope(
        call: Mapping[str, Any] | None,
        *,
        success: bool,
        reason_code: str,
        status: str = "",
        result: Any = None,
        detail_reason_code: str = "",
        image_data_url: str = "",
    ) -> dict:
        tool = str((call or {}).get("tool") or "")
        call_id = str((call or {}).get("call_id") or "")
        payload = {
            "schema_version": 1,
            "call_id": call_id,
            "tool": tool,
            "status": str(status or (
                "success_verified" if success else "failed_before_effect"
            ))[:80],
            "reason_code": str(reason_code or "")[:160],
            "result": result,
        }
        if detail_reason_code:
            payload["detail_reason_code"] = str(detail_reason_code)[:160]
        content_items = [{
            "type": "inputText",
            "text": json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=lambda _value: "<unsupported>",
            ),
        }]
        if image_data_url:
            repeat_instruction = (
                "request_visual_observation again for this same frame."
                if tool == "request_visual_observation" else
                "request this same computer frame again."
            )
            content_items.append({
                "type": "inputText",
                "text": (
                    "The next content item is the exact fresh target image. "
                    "Inspect those pixels now and answer the current visual "
                    "request from them. Do not " + repeat_instruction
                ),
            })
            content_items.append({
                "type": "inputImage",
                "imageUrl": image_data_url,
            })
        return {"contentItems": content_items, "success": bool(success)}

    def _bridge_dynamic_visual_image_for_runtime(
        self,
        attempt: _TurnAttempt,
        *,
        call_id: str,
        request_id: Any,
        image_reference: dict,
        image_artifact,
        owner_id: str,
        tool_name: str = "request_visual_observation",
        frame_id: str = "",
    ) -> bool:
        """Bridge Codex 0.144 dynamic-image visibility on the same native turn.

        The pinned App Server correctly transports ``inputImage`` in the
        correlated tool response, but the controlled Terra probe proves that
        its current model backend does not expose those pixels for reasoning.
        Until the supported runtime range advances past 0.144, queue the same
        canonical artifact as ``localImage`` before releasing the dynamic
        response.  The duplicate is explicit, version-bounded compatibility
        transport—not a secondary model or semantic image helper.
        """

        if (
            SUPPORTED_MIN_VERSION,
            SUPPORTED_MAX_VERSION,
        ) != _DYNAMIC_IMAGE_BRIDGE_RUNTIME_RANGE:
            attempt.visual_bridge_failure_state = "RUNTIME_RANGE_UNREVIEWED"
            attempt.visual_delivery_frozen = True
            return False

        from osia.image_inputs import (
            MediaEnvelopeType,
            acknowledge_media_delivery,
            begin_media_delivery,
            commit_media_delivery,
            fail_media_delivery,
            mark_media_delivery_commit_unknown,
            prepare_media_delivery,
        )

        suffix = {
            "image/gif": ".gif",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }.get(str(image_artifact.mime_type or ""))
        if not suffix or not attempt.turn_id:
            attempt.visual_bridge_failure_state = "FAILED"
            return False
        delivery = prepare_media_delivery(
            image_reference,
            owner_id=owner_id,
            provider="chatgpt_account",
            model=attempt.model_id,
            conversation_id=attempt.conversation_id,
            request_id=(attempt.client_request_id or attempt.thread_id),
            turn_id=(attempt.osia_turn_id or attempt.turn_id),
            tool_call_id=call_id,
            rpc_id=f"turn-steer:{request_id}",
            transport_id=(
                f"generation:{self.generation}:thread:{attempt.thread_id}:"
                "codex-0.144-dynamic-image-bridge"
            ),
            envelope_type=MediaEnvelopeType.TURN_STEER_IMAGE,
        )

        def remember_failure_state() -> None:
            try:
                from osia.image_inputs import media_delivery_record

                current = media_delivery_record(delivery.delivery_id)
                state = str(current.state if current is not None else "UNKNOWN")
            except Exception:
                state = "UNKNOWN"
            attempt.visual_bridge_failure_state = state
            if state in {"DISPATCHING", "COMMIT_UNKNOWN", "UNKNOWN"}:
                attempt.visual_delivery_frozen = True

        if delivery.state == "ACKNOWLEDGED":
            attempt.visual_bridge_failure_state = ""
            return True
        if delivery.state == "COMMITTED":
            attempt.bridged_visual_delivery_ids.add(delivery.delivery_id)
            attempt.visual_bridge_failure_state = ""
            return True
        if not delivery.send_authorized:
            remember_failure_state()
            return False

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        path = None
        transport_started = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix="vision-bridge-",
                suffix=suffix,
                dir=self.temp_dir,
                delete=False,
            ) as handle:
                handle.write(image_artifact.image_bytes)
                path = Path(handle.name)
            dispatching = begin_media_delivery(delivery.delivery_id)
            if dispatching is None or dispatching.state != "DISPATCHING":
                raise RuntimeError(
                    "image_bridge_dispatch_state_unavailable"
                )
            transport_started = True
            self._request(
                "turn/steer",
                {
                    "threadId": attempt.thread_id,
                    "expectedTurnId": str(attempt.turn_id),
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "Codex 0.144 compatibility image for the exact "
                                "correlated visual tool result. Inspect these "
                                "pixels and answer the current visual request "
                                "without requesting this frame again."
                                + (
                                    " The required Computer Use frame_id is "
                                    + str(frame_id)[:160]
                                    + "; copy that exact frame_id into any "
                                    "computer action. The local image filename "
                                    "is transport-only and is not a frame_id."
                                    if tool_name.startswith("computer_")
                                    and frame_id else ""
                                )
                            ),
                        },
                        {
                            "type": "localImage",
                            "path": str(path),
                            "detail": "high",
                        },
                    ],
                },
                timeout=min(15.0, self.request_timeout),
            )
            committed = commit_media_delivery(delivery.delivery_id)
            if committed is None or committed.state not in {
                "COMMITTED", "ACKNOWLEDGED",
            }:
                committed = mark_media_delivery_commit_unknown(
                    delivery.delivery_id,
                    reason_code="IMAGE_DELIVERY_COMMIT_UNKNOWN",
                )
            if committed is None or committed.state not in {
                "COMMITTED", "ACKNOWLEDGED",
            }:
                remember_failure_state()
                return False
            attempt.bridged_visual_paths.append(path)
            path = None
            if committed.state == "COMMITTED":
                attempt.bridged_visual_delivery_ids.add(delivery.delivery_id)
            else:
                acknowledge_media_delivery(delivery.delivery_id)
            attempt.visual_bridge_failure_state = ""
            self._append_capability_event(
                attempt,
                source="provider_dynamic",
                identity="codex0144DynamicImageBridge",
                phase="committed",
                outcome="success_verified",
                item_id=call_id,
                tool_attempt_id=call_id,
                tool_name=tool_name,
                reason_code="DYNAMIC_IMAGE_COMPATIBILITY_BRIDGED",
            )
            return True
        except AppServerError as exc:
            try:
                if exc.category in {"request_rejected", "process_stopped"}:
                    fail_media_delivery(
                        delivery.delivery_id,
                        reason_code="IMAGE_DELIVERY_FAILED_BEFORE_COMMIT",
                    )
                else:
                    mark_media_delivery_commit_unknown(
                        delivery.delivery_id,
                        reason_code="IMAGE_DELIVERY_COMMIT_UNKNOWN",
                    )
            except Exception:
                pass
            remember_failure_state()
            return False
        except Exception:
            try:
                if transport_started:
                    mark_media_delivery_commit_unknown(
                        delivery.delivery_id,
                        reason_code="IMAGE_DELIVERY_COMMIT_UNKNOWN",
                    )
                else:
                    fail_media_delivery(
                        delivery.delivery_id,
                        reason_code="IMAGE_DELIVERY_FAILED_BEFORE_COMMIT",
                    )
            except Exception:
                pass
            remember_failure_state()
            return False
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _send_dynamic_failure(
        self,
        request_id: Any,
        call: Mapping[str, Any] | None,
        reason_code: str,
        *,
        detail_reason_code: str = "",
    ) -> None:
        result = self._dynamic_result_envelope(
            call,
            success=False,
            reason_code=reason_code,
            result={
                "available": False,
                "retryable_in_same_model_turn": True,
            },
            detail_reason_code=detail_reason_code,
        )
        self._send({"id": request_id, "result": result})

    def _send_uncached_dynamic_failure(
        self,
        attempt: _TurnAttempt,
        request_id: Any,
        call: Mapping[str, Any] | None,
        reason_code: str,
    ) -> None:
        try:
            self._send_dynamic_failure(request_id, call, reason_code)
        except AppServerError as exc:
            if not attempt.first_causal_failure:
                attempt.first_causal_failure = "TOOL_RESPONSE_MISSING"
            raise AppServerError(
                "ERROR: TOOL_RESPONSE_MISSING",
                allow_fallback=False,
                category="TOOL_RESPONSE_MISSING",
            ) from exc

    def _reject_dynamic_after_model_change(
        self,
        attempt: _TurnAttempt,
        request_id: Any,
        call: Mapping[str, Any] | None,
    ) -> None:
        call = call if isinstance(call, Mapping) else {}
        call_id = str(call.get("call_id") or "")
        tool_name = str(call.get("tool") or "")
        arguments = call.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        self._append_capability_event(
            attempt,
            source="provider_dynamic",
            identity="dynamicToolCall",
            phase="completed",
            outcome="failed_before_effect",
            item_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            reason_code="MODEL_IDENTITY_CHANGED",
        )
        if not attempt.first_causal_failure:
            attempt.first_causal_failure = "MODEL_IDENTITY_CHANGED"
        try:
            self._send_dynamic_failure(
                request_id, call or None, "MODEL_IDENTITY_CHANGED",
            )
        except AppServerError:
            attempt.events.put((
                "error",
                AppServerError(
                    "ERROR: TOOL_RESPONSE_MISSING",
                    allow_fallback=False,
                    category="TOOL_RESPONSE_MISSING",
                ),
            ))
            return
        attempt.events.put((
            "error",
            AppServerError(
                "ERROR: MODEL_IDENTITY_CHANGED",
                allow_fallback=False,
                category="MODEL_IDENTITY_CHANGED",
            ),
        ))

    def _commit_dynamic_response(
        self,
        attempt: _TurnAttempt,
        *,
        request_id: Any,
        call_id: str,
        signature: str,
        result: Mapping[str, Any],
    ) -> None:
        pending_successors: list[str] = []
        with attempt.dynamic_response_lock:
            visual_entry = next((
                entry for entry in attempt.visual_frame_calls.values()
                if str(entry.get("call_id") or "") == str(call_id or "")
            ), None)
            if isinstance(visual_entry, dict):
                visual_entry["transport_started"] = True
            try:
                self._send({"id": request_id, "result": dict(result)})
            except AppServerError as exc:
                if isinstance(visual_entry, dict):
                    visual_entry["transport_started"] = False
                    visual_entry["pending_successor_item_ids"] = []
                if not attempt.first_causal_failure:
                    attempt.first_causal_failure = "TOOL_RESPONSE_MISSING"
                raise AppServerError(
                    "ERROR: TOOL_RESPONSE_MISSING",
                    allow_fallback=False,
                    category="TOOL_RESPONSE_MISSING",
                ) from exc
            if isinstance(visual_entry, dict):
                visual_entry["response_committed"] = True
                pending_successors = list(
                    visual_entry.pop("pending_successor_item_ids", ()) or ()
                )
            attempt.dynamic_tool_responses[call_id] = {
                "signature": signature,
                "result": deepcopy(dict(result)),
            }
            attempt.dynamic_response_sent = True
        for successor_item_id in pending_successors:
            correlated = self._mark_visual_input_consumed(
                attempt,
                successor_item_id=successor_item_id,
            )
            if correlated and successor_item_id in attempt.agent_message_items:
                attempt.post_visual_agent_items.add(successor_item_id)

    def _close_dynamic_failure(
        self,
        attempt: _TurnAttempt,
        *,
        request_id: Any,
        call: Mapping[str, Any],
        reason_code: str,
        result: Any,
        arguments: Mapping[str, Any] | None = None,
        detail_reason_code: str = "",
        semantic_node_id: str = "",
    ) -> None:
        call_id = str(call.get("call_id") or "")
        tool_name = str(call.get("tool") or "")
        clean_arguments = (
            dict(arguments)
            if isinstance(arguments, Mapping) else
            dict(call.get("arguments") or {})
        )
        response = self._dynamic_result_envelope(
            call,
            success=False,
            reason_code=reason_code,
            result=result,
            detail_reason_code=detail_reason_code,
        )
        self._commit_dynamic_response(
            attempt,
            request_id=request_id,
            call_id=call_id,
            signature=self._dynamic_call_signature(call),
            result=response,
        )
        if not attempt.first_causal_failure:
            attempt.first_causal_failure = reason_code
        self._append_capability_event(
            attempt,
            source="provider_dynamic",
            identity="dynamicToolCall",
            phase="completed",
            outcome="failed_before_effect",
            item_id=call_id,
            tool_attempt_id=call_id,
            tool_name=tool_name,
            arguments=clean_arguments,
            semantic_node_id=semantic_node_id,
            reason_code=reason_code,
        )

    @staticmethod
    def _dynamic_lifecycle_mismatch(
        attempt: _TurnAttempt,
        call: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> bool:
        call_id = str(call.get("call_id") or "")
        item_id = str(params.get("itemId") or params.get("item_id") or "")
        if item_id and item_id != call_id:
            return True
        if not attempt.dynamic_items:
            return False
        signature = AppServerClient._dynamic_call_signature(call)
        known = attempt.dynamic_items.get(call_id)
        if known is not None:
            return AppServerClient._dynamic_call_signature(known) != signature
        # A new App Server call ID is a new request even when its tool and
        # arguments match a prior dynamic call. Only a conflict for this exact
        # call ID (or an explicit itemId mismatch) is a transport mismatch.
        return False

    def _resolve_dynamic_tool_call(
        self,
        attempt: _TurnAttempt,
        payload: Mapping[str, Any],
    ) -> None:
        """Close one read-only App Server call on its original model turn."""

        request_id = payload.get("request_id")
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}
        call = payload.get("call")
        call = call if isinstance(call, dict) else None
        parse_failure = str(payload.get("parse_failure") or "")
        if call is None:
            reason_code = parse_failure or "TOOL_RESPONSE_ID_MISMATCH"
            self._send_uncached_dynamic_failure(
                attempt, request_id, None, reason_code,
            )
            if not attempt.first_causal_failure:
                attempt.first_causal_failure = reason_code
            self._append_capability_event(
                attempt,
                source="provider_dynamic",
                identity="dynamicToolCall",
                phase="completed",
                outcome="failed_before_effect",
                item_id="",
                reason_code=reason_code,
            )
            return

        call_id = str(call.get("call_id") or "")
        tool_name = str(call.get("tool") or "")
        arguments = call.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        signature = self._dynamic_call_signature(call)
        cached = attempt.dynamic_tool_responses.get(call_id)
        if cached is not None:
            if cached.get("signature") != signature:
                self._send_uncached_dynamic_failure(
                    attempt, request_id, call, "TOOL_RESPONSE_ID_MISMATCH",
                )
                if not attempt.first_causal_failure:
                    attempt.first_causal_failure = "TOOL_RESPONSE_ID_MISMATCH"
                self._append_capability_event(
                    attempt,
                    source="provider_dynamic",
                    identity="dynamicToolCall",
                    phase="completed",
                    outcome="failed_before_effect",
                    item_id=call_id,
                    tool_attempt_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    reason_code="TOOL_RESPONSE_ID_MISMATCH",
                )
                return
            try:
                self._send({
                    "id": request_id,
                    "result": deepcopy(cached["result"]),
                })
            except AppServerError as exc:
                if not attempt.first_causal_failure:
                    attempt.first_causal_failure = "TOOL_RESPONSE_MISSING"
                raise AppServerError(
                    "ERROR: TOOL_RESPONSE_MISSING",
                    allow_fallback=False,
                    category="TOOL_RESPONSE_MISSING",
                ) from exc
            return

        if _selected_model_identity_changed(attempt):
            self._reject_dynamic_after_model_change(
                attempt, request_id, call,
            )
            return

        if self._dynamic_lifecycle_mismatch(attempt, call, params):
            self._send_uncached_dynamic_failure(
                attempt, request_id, call, "TOOL_RESPONSE_ID_MISMATCH",
            )
            if not attempt.first_causal_failure:
                attempt.first_causal_failure = "TOOL_RESPONSE_ID_MISMATCH"
            self._append_capability_event(
                attempt,
                source="provider_dynamic",
                identity="dynamicToolCall",
                phase="completed",
                outcome="failed_before_effect",
                item_id=call_id,
                tool_attempt_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                reason_code="TOOL_RESPONSE_ID_MISMATCH",
            )
            return

        if tool_name not in _INLINE_NATIVE_TOOLS:
            from osia.tool_contracts import tool_is_runtime_available

            self._close_dynamic_failure(
                attempt,
                request_id=request_id,
                call=call,
                reason_code="CAPABILITY_UNAVAILABLE",
                result={
                    "available": False,
                    "requested_capability": tool_name,
                    "available_capabilities": sorted(
                        name for name in _INLINE_NATIVE_TOOLS
                        if tool_is_runtime_available(name)
                    ),
                    "retryable_in_same_model_turn": True,
                },
                arguments=arguments,
            )
            return

        try:
            from osia.execution_strategy import (
                record_tool_outcome,
                tool_preflight,
            )
            from osia.tool_contracts import (
                SideEffectClass,
                ToolValidationError,
                get_tool_spec,
                validate_tool_arguments,
            )
        except ImportError:
            self._close_dynamic_failure(
                attempt,
                request_id=request_id,
                call=call,
                reason_code="CAPABILITY_UNAVAILABLE",
                result={"available": False, "executed": False},
                arguments=arguments,
            )
            return
        try:
            spec = get_tool_spec(tool_name)
            if (
                spec is None
                or (tool_name not in _INLINE_BROWSER_TOOLS
                    and spec.side_effect is not SideEffectClass.READ_ONLY)
                or not callable(spec.dispatcher)
            ):
                raise LookupError("canonical_inline_dispatcher_unavailable")
            clean_arguments = validate_tool_arguments(spec, arguments)
        except ToolValidationError as exc:
            from osia.tool_contracts import validation_error_details

            self._close_dynamic_failure(
                attempt,
                request_id=request_id,
                call=call,
                reason_code="INVALID_TOOL_ARGUMENTS",
                result={
                    "available": True, "executed": False,
                    "validation": validation_error_details(exc),
                    "input_schema": spec.input_schema,
                    "retryable_in_same_model_turn": True,
                },
                arguments=arguments,
            )
            return
        except LookupError:
            self._close_dynamic_failure(
                attempt,
                request_id=request_id,
                call=call,
                reason_code="CAPABILITY_UNAVAILABLE",
                result={"available": False, "executed": False},
                arguments=arguments,
            )
            return

        decision = None
        preflight_error = None
        preflight_reason_code = ""
        try:
            decision = tool_preflight(
                tool_name,
                clean_arguments,
                strategy=attempt.execution_strategy,
            ) if attempt.execution_strategy is not None else None
        except Exception:
            preflight_error = (
                "ERROR: capability preflight failed safely"
            )
            preflight_reason_code = "CAPABILITY_PREFLIGHT_UNAVAILABLE"
        if decision is not None and not decision.allowed:
            preflight_error = "ERROR: capability rejected before execution"
            preflight_reason_code = str(
                decision.reason_code or "CAPABILITY_PREFLIGHT_REJECTED"
            )[:160]
        if (
            tool_name in _DIRECT_IMAGE_NATIVE_TOOLS
            and attempt.visual_delivery_frozen
        ):
            preflight_error = "ERROR: image delivery state is unresolved"
            preflight_reason_code = "IMAGE_DELIVERY_COMMIT_UNKNOWN"

        from osia.agent import _execute_tool_with_policy, _model_result_text
        from osia.tool_contracts import ExecutionOutcome

        durable_turn_id = str(attempt.osia_turn_id or attempt.turn_id or "")
        provider_request_id = str(request_id or "")

        def _capability_outcome(record) -> str:
            value = str(
                getattr(getattr(record, "outcome", None), "value", "")
                or getattr(record, "outcome", "")
            )
            return {
                ExecutionOutcome.SUCCESS_VERIFIED.value: "success_verified",
                ExecutionOutcome.SUCCESS_UNVERIFIED.value: "success_unverified",
                ExecutionOutcome.PENDING.value: "pending",
                ExecutionOutcome.NOT_STARTED.value: "failed_before_effect",
                ExecutionOutcome.REJECTED_BEFORE_EXECUTION.value:
                    "rejected_before_execution",
                ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT.value:
                    "confirmed_failure_before_effect",
                ExecutionOutcome.FAILED_AFTER_EFFECT.value: "failed_after_effect",
                ExecutionOutcome.OUTCOME_UNKNOWN.value: "outcome_unknown",
                ExecutionOutcome.CANCELLED_PROVEN.value: "cancelled_proven",
            }.get(value, "outcome_unknown")

        def _record_capability_event(
            phase,
            outcome,
            tool_attempt_id,
            reason="",
            semantic_postcondition="",
        ):
            self._append_capability_event(
                attempt,
                source="provider_dynamic",
                identity="dynamicToolCall",
                phase=phase,
                outcome=outcome,
                item_id=call_id,
                tool_attempt_id=tool_attempt_id,
                tool_name=tool_name,
                arguments=clean_arguments,
                semantic_node_id=str(
                    decision.node_id if decision is not None else ""
                ),
                semantic_postcondition=semantic_postcondition,
                reason_code=reason,
            )

        def _status_callback(status, tool_attempt_id):
            if status == "action_starting":
                _record_capability_event(
                    "accepted",
                    "read_only" if spec.side_effect is SideEffectClass.READ_ONLY else "accepted",
                    tool_attempt_id,
                )
            elif status == "action_in_progress":
                _record_capability_event(
                    "started", "dispatch_started", tool_attempt_id,
                )

        def _terminal_callback(tool_attempt_id, record):
            outcome = _capability_outcome(record)
            if outcome != "pending":
                _record_capability_event(
                    "completed", outcome, tool_attempt_id,
                    str(getattr(record, "reason_code", "") or ""),
                )

        attempt_context = {
            "enabled": True,
            "store": self._attempt_store(),
            "conversation_id": str(attempt.conversation_id or attempt.thread_id),
            "turn_id": durable_turn_id,
            "request_id": str(attempt.client_request_id or provider_request_id),
            "tool_call_id": call_id,
            "provider_id": "chatgpt_account",
            "model_id": str(attempt.model_id or "unknown"),
            "owner_scope": "chat",
            "provider_references": {
                "tool_call_id": call_id,
                "provider_native_call_id": call_id,
                "provider_request_id": provider_request_id,
                "native_turn_id": str(attempt.turn_id or ""),
                "native_thread_id": str(attempt.thread_id or ""),
            },
            "status_callback": _status_callback,
            "terminal_callback": _terminal_callback,
        }
        execution_record = _execute_tool_with_policy(
            tool_name,
            clean_arguments,
            allow_retry=False,
            lang="en",
            attempt_context=attempt_context,
            preflight_error=preflight_error,
            preflight_reason_code=preflight_reason_code,
        )
        durable_attempt_id = str(
            getattr(execution_record, "tool_attempt_id", "")
            or attempt_context.get("tool_attempt_id")
            or ""
        )
        execution_outcome = _capability_outcome(execution_record)
        model_result = getattr(execution_record, "result", None)
        text_result = (
            str(model_result.get("text") or "")
            if isinstance(model_result, dict) else str(model_result or "")
        )
        receipt = (
            model_result.get("receipt")
            if isinstance(model_result, dict) else {}
        )
        receipt = receipt if isinstance(receipt, dict) else {}
        receipt_outcome = str(receipt.get("outcome") or "")
        is_pending = execution_outcome == "pending"
        is_error = bool(
            getattr(execution_record, "is_error", False)
            or is_pending
            or text_result.lstrip().startswith("ERROR:")
            or receipt_outcome in {
                "confirmed_failure_before_effect",
                "failed_before_effect",
                "rejected_before_execution",
            }
        )
        reason_code = str(
            getattr(execution_record, "reason_code", "")
            or receipt.get("reason_code")
            or (
                "READ_ONLY_OBSERVATION_COMPLETED"
                if not is_error else "CAPABILITY_EXECUTION_FAILED"
            )
        )[:160]
        if is_pending:
            reason_code = "TOOL_RESULT_PENDING"
            model_result = {
                "text": "PENDING: the tool is still running.",
                "receipt": {
                    "schema_version": 1,
                    "dispatch_state": "worker_may_be_active",
                    "outcome": "pending",
                    "reason_code": reason_code,
                },
            }
        image_data_url = ""
        image_artifact = None
        media_delivery = None
        image_reference = (
            model_result.get("image_ref")
            if isinstance(model_result, dict) else None
        )
        if isinstance(model_result, dict):
            model_result = deepcopy(model_result)
            model_result.pop("screenshot", None)
            model_result.pop("image_ref", None)
        if not is_error and isinstance(image_reference, dict):
            try:
                from osia.image_inputs import (
                    artifact_data_url,
                    ImageInputError,
                    resolve_image_reference,
                    turn_scoped_media_owner,
                )

                media_owner_id = turn_scoped_media_owner(
                    attempt.conversation_id,
                    attempt.osia_turn_id or attempt.turn_id or attempt.thread_id,
                )
                image_artifact = resolve_image_reference(
                    image_reference,
                    owner_id=media_owner_id,
                )
                receipt_expectations = {
                    "image_id": image_artifact.image_id,
                    "image_sha256": image_artifact.content_hash,
                    "image_mime": image_artifact.mime_type,
                    "image_width": image_artifact.width,
                    "image_height": image_artifact.height,
                }
                required_receipt_fields = {
                    "image_id", "image_sha256", "image_mime",
                    "image_width", "image_height", "surface_ref",
                    "window_generation", "application", "frame_id",
                }
                if any(
                    receipt.get(key) in (None, "")
                    for key in required_receipt_fields
                ) or any(
                    receipt.get(key) != expected
                    for key, expected in receipt_expectations.items()
                ) or (
                    str(receipt.get("surface_ref"))
                    != str(image_artifact.surface_id)
                    or str(receipt.get("window_generation"))
                    != str(image_artifact.generation)
                    or str(receipt.get("application"))
                    != str(image_artifact.application_identity)
                ):
                    raise ImageInputError(
                        "ERROR: the visual receipt does not match its exact "
                        "image artifact.",
                        reason_code="IMAGE_REF_METADATA_MISMATCH",
                    )
                image_data_url = artifact_data_url(
                    image_artifact, "chatgpt_account",
                )
            except Exception as exc:
                is_error = True
                reason_code = str(
                    getattr(exc, "reason_code", "IMAGE_INPUT_UNAVAILABLE")
                    or "IMAGE_INPUT_UNAVAILABLE"
                )[:160]
                model_result = {
                    "text": "ERROR: the prepared image input was unavailable.",
                    "receipt": {
                        "schema_version": 1,
                        "dispatch_state": "not_dispatched",
                        "outcome": "confirmed_failure_before_effect",
                        "reason_code": reason_code,
                    },
                }
        if (
            tool_name in _DIRECT_IMAGE_NATIVE_TOOLS
            and not is_error
            and reason_code in {
                "ACTIVE_MODEL_IMAGE_PART_BUILT",
                "V4_ACTIVE_MODEL_IMAGE_PART_BUILT",
                "V4_ACTIVE_MODEL_CROP_BUILT",
            }
            and not image_data_url
        ):
            is_error = True
            reason_code = "IMAGE_INPUT_UNAVAILABLE"
            model_result = {
                "text": "ERROR: the active-model image input was unavailable.",
                "receipt": {
                    "schema_version": 1,
                    "dispatch_state": "not_dispatched",
                    "outcome": "confirmed_failure_before_effect",
                    "reason_code": reason_code,
                },
            }
        frame_key = ""
        prior_visual_call = None
        if image_data_url and isinstance(model_result, dict):
            sent_receipt = model_result.get("receipt")
            if isinstance(sent_receipt, dict):
                frame_id = str(sent_receipt.get("frame_id") or "")
                image_sha256 = str(sent_receipt.get("image_sha256") or "")
                target_identity = sent_receipt.get("target_identity")
                target_identity = (
                    target_identity if isinstance(target_identity, dict) else {}
                )
                frame_key = hashlib.sha256(json.dumps(
                    {
                        "image_sha256": image_sha256,
                        "frame_id": frame_id,
                        "application": sent_receipt.get("application"),
                        "surface_ref": sent_receipt.get("surface_ref"),
                        "window_generation": sent_receipt.get(
                            "window_generation"
                        ),
                        "target_identity": target_identity,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                prior_visual_call = (
                    attempt.visual_frame_calls.get(frame_key)
                    if frame_key else None
                )
                if (
                    prior_visual_call is not None
                    and prior_visual_call.get("delivery_state")
                    not in {"COMMITTED", "ACKNOWLEDGED"}
                ):
                    prior_visual_call = None
                sent_receipt["tool_call_id"] = call_id
                if prior_visual_call is not None:
                    # The first response already placed these exact pixels in
                    # this same native model turn. Close the repeated call once,
                    # but do not duplicate the image content item.
                    image_data_url = ""
                    reason_code = "IMAGE_INPUT_ALREADY_SENT"
                    sent_receipt["reason_code"] = reason_code
                    sent_receipt["image_input_sent"] = True
                    sent_receipt["image_input_resent"] = False
                    sent_receipt["original_tool_call_id"] = str(
                        prior_visual_call.get("call_id") or ""
                    )[:160]
                else:
                    sent_receipt["reason_code"] = "IMAGE_INPUT_SENT"
                    sent_receipt["image_input_sent"] = True
            diagnostics = model_result.get("diagnostics")
            if isinstance(diagnostics, dict):
                diagnostics["image_input_sent"] = True
                diagnostics["image_input_resent"] = prior_visual_call is None
            if prior_visual_call is None:
                reason_code = "IMAGE_INPUT_SENT"

        success = not is_error
        reconciliation_postcondition = ""
        strategy = attempt.execution_strategy
        if strategy is not None and bool(
            getattr(strategy, "frozen_reason", "")
        ):
            frozen_node = strategy.node_by_id(
                str(getattr(strategy, "_frozen_node_id", "") or "")
            )
            if frozen_node is not None:
                reconciliation_postcondition = str(
                    frozen_node.observable_success_postcondition or ""
                )
        provider_model_result = (
            self._visual_provider_result(
                model_result,
                attached_now=bool(image_data_url),
                reconciliation_postcondition=reconciliation_postcondition,
            )
            if (
                tool_name in _DIRECT_IMAGE_NATIVE_TOOLS
                and success
                and bool(image_data_url or prior_visual_call is not None)
            ) else model_result
        )
        if (
            tool_name in _INLINE_BROWSER_TOOLS
            and isinstance(model_result, dict)
            and isinstance(model_result.get("receipt"), dict)
            and model_result["receipt"].get("schema_version") == 1
            and not is_pending
        ):
            provider_model_result = _model_result_text(model_result, tool_name)
            if provider_model_result.startswith("{"):
                provider_model_result = json.loads(provider_model_result)
        try:
            result = self._dynamic_result_envelope(
                call,
                success=success,
                reason_code=reason_code,
                status=execution_outcome if tool_name in _INLINE_BROWSER_TOOLS else (
                    "pending" if is_pending else ""
                ),
                result=provider_model_result,
                image_data_url=image_data_url,
            )
        except (TypeError, ValueError, RecursionError):
            success = False
            reason_code = "CAPABILITY_EXECUTION_FAILED"
            image_data_url = ""
            model_result = {
                "text": "ERROR: the capability result was not serializable.",
                "receipt": {
                    "schema_version": 1,
                    "dispatch_state": "not_dispatched",
                    "outcome": "confirmed_failure_before_effect",
                    "reason_code": reason_code,
                },
            }
            provider_model_result = model_result
            result = self._dynamic_result_envelope(
                call,
                success=False,
                reason_code=reason_code,
                result=model_result,
            )
        if (
            success
            and frame_key
            and prior_visual_call is None
            and reason_code == "IMAGE_INPUT_SENT"
            and isinstance(image_reference, dict)
            and image_artifact is not None
        ):
            try:
                from osia.image_inputs import (
                    MediaEnvelopeType,
                    prepare_media_delivery,
                )

                media_delivery = prepare_media_delivery(
                    image_reference,
                    owner_id=media_owner_id,
                    provider="chatgpt_account",
                    model=attempt.model_id,
                    conversation_id=attempt.conversation_id,
                    request_id=(attempt.client_request_id or attempt.thread_id),
                    turn_id=(attempt.osia_turn_id or attempt.thread_id),
                    tool_call_id=call_id,
                    rpc_id=str(request_id or ""),
                    transport_id=(
                        f"generation:{self.generation}:"
                        f"thread:{attempt.thread_id}"
                    ),
                    envelope_type=MediaEnvelopeType.INPUT_IMAGE,
                )
                if media_delivery.state in {"COMMITTED", "ACKNOWLEDGED"}:
                    image_data_url = ""
                    reason_code = "IMAGE_INPUT_ALREADY_SENT"
                    result = self._dynamic_result_envelope(
                        call,
                        success=True,
                        reason_code=reason_code,
                        result=provider_model_result,
                    )
                    media_delivery = None
                elif not media_delivery.send_authorized:
                    success = False
                    image_data_url = ""
                    reason_code = (
                        "IMAGE_DELIVERY_COMMIT_UNKNOWN"
                        if media_delivery.state == "COMMIT_UNKNOWN" else
                        "IMAGE_DELIVERY_IN_PROGRESS"
                    )
                    model_result = {
                        "text": "ERROR: the exact image delivery is already claimed.",
                        "receipt": {
                            "schema_version": 1,
                            "dispatch_state": "not_dispatched",
                            "outcome": "confirmed_failure_before_effect",
                            "reason_code": reason_code,
                            "image_input_sent": False,
                        },
                    }
                    result = self._dynamic_result_envelope(
                        call,
                        success=False,
                        reason_code=reason_code,
                        result=model_result,
                    )
                    media_delivery = None
                else:
                    attempt.visual_frame_calls[frame_key] = {
                        "call_id": call_id,
                        "tool_attempt_id": durable_attempt_id,
                        "tool_name": tool_name,
                        "frame_id": str(receipt.get("frame_id") or "")[:160],
                        "provider_id": str(receipt.get("provider_id") or "")[:80],
                        "model_id": str(receipt.get("model_id") or "")[:160],
                        "arguments": dict(clean_arguments),
                        "delivery_id": media_delivery.delivery_id,
                        "delivery_state": "PREPARED",
                        "transport_started": False,
                        "response_committed": False,
                        "pending_successor_item_ids": [],
                        "prior_item_ids": sorted({
                            str(item.get("item_id") or "")
                            for item in attempt.response_item_diagnostics
                            if isinstance(item, dict) and item.get("item_id")
                        } | set(attempt.dynamic_items) | set(
                            attempt.agent_message_items
                        )),
                    }
                    attempt.image_input_present = True
            except Exception:
                success = False
                reason_code = "IMAGE_DELIVERY_FAILED"
                image_data_url = ""
                model_result = {
                    "text": "ERROR: the image delivery could not be prepared.",
                    "receipt": {
                        "schema_version": 1,
                        "dispatch_state": "not_dispatched",
                        "outcome": "confirmed_failure_before_effect",
                        "reason_code": reason_code,
                        "image_input_sent": False,
                    },
                }
                result = self._dynamic_result_envelope(
                    call,
                    success=False,
                    reason_code=reason_code,
                    result=model_result,
                )
        if media_delivery is not None:
            bridge_committed = self._bridge_dynamic_visual_image_for_runtime(
                attempt,
                call_id=call_id,
                request_id=request_id,
                image_reference=image_reference,
                image_artifact=image_artifact,
                owner_id=media_owner_id,
                tool_name=tool_name,
                frame_id=str(receipt.get("frame_id") or ""),
            )
            if not bridge_committed:
                self._append_capability_event(
                    attempt,
                    source="provider_dynamic",
                    identity="codex0144DynamicImageBridge",
                    phase="completed",
                    outcome="failed_before_effect",
                    item_id=call_id,
                    tool_attempt_id=call_id,
                    tool_name=tool_name,
                    reason_code="DYNAMIC_IMAGE_COMPATIBILITY_BRIDGE_FAILED",
                )
                try:
                    from osia.image_inputs import fail_media_delivery

                    fail_media_delivery(
                        media_delivery.delivery_id,
                        reason_code="IMAGE_DELIVERY_FAILED_BEFORE_COMMIT",
                    )
                except Exception:
                    pass
                attempt.visual_frame_calls.pop(frame_key, None)
                media_delivery = None
                success = False
                attempt.visual_delivery_required_failed = True
                image_data_url = ""
                reason_code = "IMAGE_DELIVERY_FAILED"
                if not attempt.first_causal_failure:
                    attempt.first_causal_failure = reason_code
                model_result = {
                    "text": (
                        "ERROR: the pinned App Server image compatibility "
                        "delivery failed before visual continuation."
                    ),
                    "receipt": {
                        "schema_version": 1,
                        "dispatch_state": "not_dispatched",
                        "outcome": "confirmed_failure_before_effect",
                        "reason_code": reason_code,
                        "detail_reason_code": (
                            "DYNAMIC_IMAGE_COMPATIBILITY_BRIDGE_FAILED"
                        ),
                        "image_input_sent": False,
                    },
                }
                result = self._dynamic_result_envelope(
                    call,
                    success=False,
                    reason_code=reason_code,
                    result=model_result,
                    detail_reason_code=(
                        "DYNAMIC_IMAGE_COMPATIBILITY_BRIDGE_FAILED"
                    ),
                )
            else:
                try:
                    from osia.image_inputs import (
                        begin_media_delivery,
                        fail_media_delivery,
                    )

                    dispatching = begin_media_delivery(
                        media_delivery.delivery_id
                    )
                    if (
                        dispatching is None
                        or dispatching.state != "DISPATCHING"
                    ):
                        raise RuntimeError(
                            "image_delivery_dispatch_state_unavailable"
                        )
                except Exception:
                    try:
                        fail_media_delivery(
                            media_delivery.delivery_id,
                            reason_code="IMAGE_DELIVERY_FAILED_BEFORE_COMMIT",
                        )
                    except Exception:
                        pass
                    attempt.visual_frame_calls.pop(frame_key, None)
                    media_delivery = None
                    success = False
                    attempt.visual_delivery_required_failed = True
                    image_data_url = ""
                    reason_code = "IMAGE_DELIVERY_FAILED"
                    model_result = {
                        "text": "ERROR: the image delivery could not be started.",
                        "receipt": {
                            "schema_version": 1,
                            "dispatch_state": "not_dispatched",
                            "outcome": "confirmed_failure_before_effect",
                            "reason_code": reason_code,
                            "image_input_sent": False,
                        },
                    }
                    result = self._dynamic_result_envelope(
                        call,
                        success=False,
                        reason_code=reason_code,
                        result=model_result,
                    )
        semantic_postcondition = ""
        if (
            tool_name in {"computer_screenshot", "computer_zoom"}
            and receipt.get("authoritative_postcondition_verified") is True
            and attempt.execution_strategy is not None
        ):
            node = attempt.execution_strategy.goal
            semantic_postcondition = str(
                getattr(node, "observable_success_postcondition", "") or ""
            )[:512]
        if execution_outcome != "pending":
            _record_capability_event(
                "completed",
                execution_outcome,
                durable_attempt_id,
                str(getattr(execution_record, "reason_code", "") or ""),
                semantic_postcondition,
            )
        try:
            self._commit_dynamic_response(
                attempt,
                request_id=request_id,
                call_id=call_id,
                signature=signature,
                result=result,
            )
        except AppServerError:
            if media_delivery is not None:
                try:
                    from osia.image_inputs import (
                        mark_media_delivery_commit_unknown,
                    )

                    mark_media_delivery_commit_unknown(
                        media_delivery.delivery_id,
                        reason_code="IMAGE_DELIVERY_COMMIT_UNKNOWN",
                    )
                except Exception:
                    pass
                attempt.visual_frame_calls.pop(frame_key, None)
            raise
        if media_delivery is not None:
            registered = attempt.visual_frame_calls.get(frame_key)
            try:
                from osia.image_inputs import (
                    acknowledge_media_delivery,
                    commit_media_delivery,
                    mark_media_delivery_commit_unknown,
                )

                committed = commit_media_delivery(media_delivery.delivery_id)
                if committed is None or committed.state not in {
                    "COMMITTED", "ACKNOWLEDGED",
                }:
                    committed = mark_media_delivery_commit_unknown(
                        media_delivery.delivery_id,
                        reason_code="IMAGE_DELIVERY_COMMIT_UNKNOWN",
                    )
                    if not attempt.first_causal_failure:
                        attempt.first_causal_failure = (
                            "IMAGE_DELIVERY_COMMIT_UNKNOWN"
                        )
                if isinstance(registered, dict):
                    registered["delivery_state"] = (
                        committed.state if committed is not None else
                        "COMMIT_UNKNOWN"
                    )
                if call_id in attempt.visual_consumed_calls:
                    acknowledged = acknowledge_media_delivery(
                        media_delivery.delivery_id
                    )
                    if isinstance(registered, dict):
                        registered["delivery_state"] = (
                            acknowledged.state if acknowledged is not None else
                            registered.get("delivery_state", "COMMIT_UNKNOWN")
                        )
            except Exception:
                # The provider response is already committed. Completion-time
                # repair can acknowledge it from the correlated successor.
                try:
                    from osia.image_inputs import (
                        mark_media_delivery_commit_unknown,
                    )

                    mark_media_delivery_commit_unknown(
                        media_delivery.delivery_id,
                        reason_code="IMAGE_DELIVERY_COMMIT_UNKNOWN",
                    )
                except Exception:
                    pass
                if isinstance(registered, dict):
                    registered["delivery_state"] = "COMMIT_UNKNOWN"
                if not attempt.first_causal_failure:
                    attempt.first_causal_failure = (
                        "IMAGE_DELIVERY_COMMIT_UNKNOWN"
                    )
        if not success and not attempt.first_causal_failure:
            attempt.first_causal_failure = reason_code
        if attempt.execution_strategy is not None:
            try:
                record_tool_outcome(
                    tool_name,
                    decision,
                    outcome=(
                        "success_verified" if success else "failed_before_effect"
                    ),
                    is_error=not success,
                    reason_code=reason_code,
                    result=model_result,
                    strategy=attempt.execution_strategy,
                )
            except Exception:
                pass
        # Image transport is mechanical modality evidence only. The terminal
        # turn requires this consumed same-model frame, but never re-scores the
        # provider's natural-language interpretation.
        if semantic_postcondition and attempt.execution_strategy is not None:
            try:
                attempt.execution_strategy.verify_postcondition(
                    "computer_use_v4_authoritative_post_action_observation",
                    postcondition=semantic_postcondition,
                )
            except Exception:
                semantic_postcondition = ""

    def _handle_server_request(self, message: dict) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        request_id = message.get("id")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            attempt = self._attempt_for(params)
            if attempt is None:
                try:
                    self._send({"id": request_id, "result": {"decision": "cancel"}})
                except AppServerError:
                    pass
                return
            self._resolve_native_approval(attempt, {
                "method": method,
                "params": deepcopy(params),
                "request_id": request_id,
            })
            return
        elif method in {"execCommandApproval", "applyPatchApproval"}:
            result = {"decision": "abort"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "cancel", "content": None}
        elif method == "item/tool/call":
            dynamic_call, parse_failure = self._dynamic_call_from_params(params)
            attempt = self._attempt_for(params)
            thread_id = str(params.get("threadId") or "")
            turn_id = str(params.get("turnId") or "")
            if (
                attempt is not None
                and thread_id == attempt.thread_id
                and turn_id
                and not attempt.turn_id
            ):
                attempt.pending_server_requests.append(deepcopy(message))
                del attempt.pending_server_requests[:-32]
                return
            exact_attempt = bool(
                attempt is not None
                and thread_id
                and thread_id == attempt.thread_id
                and turn_id
                and attempt.turn_id
                and turn_id == str(attempt.turn_id)
            )
            if not exact_attempt:
                try:
                    self._send_dynamic_failure(
                        request_id,
                        dynamic_call,
                        "TOOL_RESPONSE_ID_MISMATCH",
                    )
                except AppServerError:
                    pass
                return
            if _selected_model_identity_changed(attempt):
                self._reject_dynamic_after_model_change(
                    attempt, request_id, dynamic_call,
                )
                return
            if dynamic_call is None:
                attempt.events.put(("dynamic_tool_call", {
                    "call": None,
                    "parse_failure": parse_failure,
                    "params": deepcopy(params),
                    "request_id": request_id,
                }))
                return
            self._mark_visual_input_consumed(
                attempt,
                successor_item_id=str(dynamic_call.get("call_id") or ""),
            )
            if dynamic_call["tool"] == "desktop_click":
                click_arguments = dynamic_call.get("arguments")
                click_arguments = (
                    click_arguments
                    if isinstance(click_arguments, dict) else {}
                )
                frame_entries = [
                    entry
                    for entry in attempt.visual_frame_calls.values()
                    if isinstance(entry, dict)
                    and entry.get("frame_id")
                    and entry.get("delivery_state") in {
                        "COMMITTED", "ACKNOWLEDGED",
                    }
                ]
                valid_frame_ids = {
                    str(entry.get("frame_id") or "")
                    for entry in frame_entries
                }
                expected_frame_id = (
                    str(frame_entries[-1].get("frame_id") or "")
                    if frame_entries else ""
                )
                supplied_frame_id = str(
                    click_arguments.get("visual_frame_id") or ""
                )
                bbox = click_arguments.get("visual_bbox")
                bbox = bbox if isinstance(bbox, dict) else {}
                normalized_bbox = {}
                for key in ("left", "top", "right", "bottom"):
                    value = bbox.get(key)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not 0.0 <= float(value) <= 1.0
                    ):
                        normalized_bbox = {}
                        break
                    normalized_bbox[key] = float(value)
                bbox_valid = bool(
                    len(normalized_bbox) == 4
                    and normalized_bbox["left"] < normalized_bbox["right"]
                    and normalized_bbox["top"] < normalized_bbox["bottom"]
                )
                missing = []
                if not str(click_arguments.get("description") or "").strip():
                    missing.append("description")
                if not supplied_frame_id or supplied_frame_id not in valid_frame_ids:
                    missing.append("visual_frame_id")
                if not bbox_valid:
                    missing.append("visual_bbox")
                if missing:
                    self._close_dynamic_failure(
                        attempt,
                        request_id=request_id,
                        call=dynamic_call,
                        reason_code="INVALID_ARGUMENTS",
                        result={
                            "available": True,
                            "executed": False,
                            "retryable_in_same_model_turn": True,
                            "missing_or_invalid_fields": missing,
                            "required_visual_frame_id": expected_frame_id,
                            "visual_frame_id": expected_frame_id,
                            "visual_bbox_contract": (
                                "left, top, right, bottom must be normalized "
                                "numbers in [0,1] with left < right and top < bottom"
                            ),
                            "instruction": (
                                "Call desktop_click again with visual_frame_id "
                                "copied exactly and one normalized visual_bbox."
                                if expected_frame_id else
                                "Call request_visual_observation before desktop_click."
                            ),
                        },
                        arguments=click_arguments,
                    )
                    return
            if (
                dynamic_call["tool"] in _INLINE_NATIVE_TOOLS
                or not _is_canonical_native_handoff(dynamic_call["tool"])
            ):
                attempt.events.put(("dynamic_tool_call", {
                    "call": dynamic_call,
                    "parse_failure": "",
                    "params": deepcopy(params),
                    "request_id": request_id,
                }))
                return
            native_tool_call = _exact_canonical_native_call(params)
            result = {
                "contentItems": [{
                    "type": "inputText",
                    "text": (
                        "The request was not executed by Codex. OSIA will "
                        "evaluate it through its canonical tool pipeline."
                    ),
                }],
                "success": False,
            }
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        else:
            try:
                self._send(
                    {
                        "id": request_id,
                        "error": {"code": -32000, "message": "Declined by OSIA policy"},
                    }
                )
            except AppServerError:
                pass
            self._mark_unsafe(params, "native_request")
            return
        try:
            self._send({"id": request_id, "result": result})
        except AppServerError:
            pass
        if method == "item/tool/call" and native_tool_call is not None:
            if str(native_tool_call.get("tool") or "").startswith("computer_"):
                try:
                    from osia.computer_use_v4_integration import (
                        prepare_controlled_computer_use_v4_handoff,
                    )

                    prepare_controlled_computer_use_v4_handoff(
                        str(native_tool_call.get("tool") or ""),
                        native_tool_call.get("arguments") or {},
                    )
                except Exception:
                    pass
            self._mark_unsafe(
                params,
                f"canonical_tool:{native_tool_call['tool']}",
                native_tool_call=native_tool_call,
            )
        elif method != "item/tool/call" or native_tool_call is None:
            self._mark_unsafe(params, "native_request")

    def _dispatch_notification(self, method: str, params: dict) -> None:
        if method == "account/updated":
            with self._state_lock:
                self._account_generation += 1
                self._rate_limits_cache = None
                self._rate_limits_pending_update = {}
                self._context_usage.clear()
            return
        if method == "account/login/completed":
            login_id = params.get("loginId")
            if login_id:
                with self._login_lock:
                    waiter = self._login_events.get(str(login_id))
                if waiter:
                    waiter[1].update(
                        {
                            "success": bool(params.get("success")),
                            "error": bool(params.get("error")),
                        }
                    )
                    waiter[0].set()
            if params.get("success"):
                with self._state_lock:
                    self._rate_limits_cache = None
                    self._rate_limits_pending_update = {}
            return

        if method == "account/rateLimits/updated":
            update = sanitize_rate_limit_payload(params)
            if update:
                with self._state_lock:
                    if self._rate_limits_cache is not None:
                        self._rate_limits_cache = merge_rate_limit_payload(
                            self._rate_limits_cache, update
                        )
                    self._rate_limits_pending_update = merge_rate_limit_payload(
                        self._rate_limits_pending_update, update
                    )
            return

        if method == "item/reasoning/textDelta":
            # Raw chain-of-thought is never a display or persistence source.
            return
        if method in {
            "item/reasoning/summaryPartAdded",
            "item/reasoning/summaryTextDelta",
        }:
            reasoning_attempt = self._reasoning_attempt_for(params)
            if reasoning_attempt is not None:
                self._capture_public_reasoning_summary(
                    reasoning_attempt, method, params,
                )
            return

        thread_id = self._thread_id(params)
        if thread_id:
            pending_turn_id = str(
                params.get("turnId")
                or (
                    (params.get("turn") or {}).get("id")
                    if isinstance(params.get("turn"), dict) else ""
                )
                or ""
            )
            with self._state_lock:
                pending_attempt = self._attempts.get(thread_id)
                if (
                    pending_attempt is not None
                    and not pending_attempt.turn_id
                    and pending_turn_id
                ):
                    pending_attempt.pending_turn_notifications.append((
                        str(method), deepcopy(params),
                    ))
                    del pending_attempt.pending_turn_notifications[:-64]
                    return

        attempt = self._turn_attempt_for(params)
        if thread_id:
            self._record_context_notification(thread_id, method, params, attempt)
        if method == "model/rerouted":
            if attempt is not None:
                attempt.model_rerouted = True
                attempt.effective_model_id = str(
                    params.get("model")
                    or params.get("toModel")
                    or params.get("modelId")
                    or ""
                )[:160]
                if (
                    _normalized_model_id(attempt.effective_model_id)
                    != _normalized_model_id(attempt.model_id)
                ):
                    attempt.model_identity_changed = True
            return
        if method == "item/agentMessage/delta":
            if attempt is not None and not attempt.native_action:
                delta = params.get("delta")
                if isinstance(delta, str) and delta:
                    attempt.model_successor_observed = True
                    self._acknowledge_local_image_deliveries(attempt)
                    item_id = self._native_item_id(params)
                    if (
                        not item_id
                        and len(attempt.pending_agent_message_items) == 1
                    ):
                        item_id = next(iter(
                            attempt.pending_agent_message_items
                        ))
                    if not item_id:
                        attempt.agent_delta_sequence += 1
                        item_id = (
                            f"agent-delta:{attempt.turn_id}:"
                            f"{attempt.agent_delta_sequence}"
                        )[:160]
                    correlated_visual = self._mark_visual_input_consumed(
                        attempt,
                        successor_item_id=item_id,
                    )
                    if correlated_visual and item_id:
                        attempt.post_visual_agent_items.add(item_id)
                    self._append_agent_text(
                        attempt, delta, item_id=item_id,
                    )
            return

        if method in {"item/started", "item/completed"}:
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            item_type = str(item.get("type") or "")
            item_id = str(item.get("id") or "")
            if (
                attempt is not None
                and item_type
                and item_type != "userMessage"
            ):
                attempt.model_successor_observed = True
                self._acknowledge_local_image_deliveries(attempt)
            if attempt is not None:
                self._record_response_item(
                    attempt,
                    item_id=item_id,
                    item_type=item_type,
                    phase=(
                        "started" if method == "item/started" else "completed"
                    ),
                    status=str(item.get("status") or ""),
                    text_chars=(
                        len(self._agent_message_text(item))
                        if item_type == "agentMessage" else 0
                    ),
                )
            correlated_visual = False
            if (
                attempt is not None
                and item_type in {"agentMessage", "dynamicToolCall"}
                and item_id
            ):
                correlated_visual = self._mark_visual_input_consumed(
                    attempt,
                    successor_item_id=item_id,
                )
            if item_type == "agentMessage":
                if attempt is not None:
                    state = attempt.agent_message_items.setdefault(
                        item_id, {
                            "delta_chars": 0,
                            "delta_text": "",
                            "completed": False,
                        },
                    )
                    # Use the provider's explicit public phase, never inferred
                    # intent or private reasoning, to distinguish commentary.
                    phase = item.get("phase")
                    if phase in {"commentary", "final_answer"}:
                        state["message_phase"] = phase
                    if method == "item/started":
                        if item_id:
                            attempt.pending_agent_message_items.add(item_id)
                    else:
                        completed_text = self._agent_message_text(item)
                        if correlated_visual and completed_text:
                            attempt.post_visual_agent_items.add(item_id)
                        assembled_text = (
                            str(state.get("delta_text") or "")
                            if state.get("message_phase") == "commentary"
                            else "".join(attempt.text_parts)
                        )
                        streamed_text = str(state.get("delta_text") or "")
                        missing_text = ""
                        if completed_text and completed_text not in assembled_text:
                            if (
                                streamed_text
                                and completed_text.startswith(streamed_text)
                            ):
                                missing_text = completed_text[len(streamed_text):]
                            elif not streamed_text:
                                overlap = min(
                                    len(assembled_text), len(completed_text),
                                )
                                while (
                                    overlap
                                    and not assembled_text.endswith(
                                        completed_text[:overlap]
                                    )
                                ):
                                    overlap -= 1
                                missing_text = completed_text[overlap:]
                        if missing_text:
                            self._append_agent_text(
                                attempt, missing_text, item_id=item_id,
                            )
                        state["completed"] = True
                        if item_id:
                            attempt.pending_agent_message_items.discard(item_id)
                            if state.get("message_phase") != "commentary":
                                attempt.final_output_item_id = item_id[:160]
                    self._release_deferred_done(attempt)
                return
            if item_type == "webSearch":
                if attempt is not None:
                    self._append_capability_event(
                        attempt,
                        source="provider_hosted",
                        identity="webSearch",
                        phase="started" if method == "item/started" else "completed",
                        outcome=(
                            "pending" if method == "item/started"
                            else "success_verified"
                        ),
                        item_id=item_id,
                    )
                return
            if item_type == "imageView":
                if attempt is None or self._turn_attempt_for(params) is not attempt:
                    return
                path_value = item.get("path")
                valid_shape = bool(
                    set(item) == {"type", "id", "path"}
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", item_id)
                    and isinstance(path_value, str)
                    and path_value
                )
                if not valid_shape:
                    self._mark_unsafe(params, "invalid_native_item:imageView")
                    return
                try:
                    metadata = self._native_image_view_metadata(path_value)
                except (OSError, ValueError, TypeError):
                    self._mark_unsafe(params, "invalid_native_item:imageView")
                    return
                if method == "item/started":
                    if item_id in attempt.native_image_views:
                        self._mark_unsafe(params, "invalid_native_item:imageView")
                        return
                    attempt.native_image_views[item_id] = {
                        "original_path": path_value, "metadata": metadata,
                    }
                    self._append_capability_event(
                        attempt, source="provider_native", identity="imageView",
                        phase="selected", outcome="running_read_only",
                        item_id=item_id, arguments=metadata,
                    )
                else:
                    selected = attempt.native_image_views.pop(item_id, None)
                    if (
                        not isinstance(selected, dict)
                        or selected.get("original_path") != path_value
                        or selected.get("metadata") != metadata
                    ):
                        self._mark_unsafe(params, "invalid_native_item:imageView")
                        return
                    self._append_capability_event(
                        attempt, source="provider_native", identity="imageView",
                        phase="completed", outcome="success_verified",
                        item_id=item_id, arguments=metadata,
                    )
                return
            if item_type in {"commandExecution", "fileChange"}:
                if attempt is not None and item_id:
                    if self._turn_attempt_for(params) is not attempt:
                        return
                    valid_item = bool(
                        item.get("status") in {
                            "inProgress", "completed", "failed", "declined",
                        }
                        and (
                            (
                                item_type == "commandExecution"
                                and isinstance(item.get("command"), str)
                                and isinstance(item.get("commandActions"), list)
                            )
                            or (
                                item_type == "fileChange"
                                and isinstance(item.get("changes"), list)
                            )
                        )
                    )
                    if not valid_item:
                        self._mark_unsafe(
                            params, f"invalid_native_item:{item_type}",
                        )
                        return
                    if method == "item/started":
                        self._append_capability_event(
                            attempt,
                            source="provider_native",
                            identity=item_type,
                            phase="started",
                            outcome="outcome_unknown",
                            item_id=item_id,
                            reason_code="UNEXPECTED_PROVIDER_NATIVE_PHYSICAL_EFFECT",
                        )
                        attempt.outcome_unknown = True
                        attempt.native_effect_started = True
                        self._mark_unsafe(
                            params,
                            f"unbrokered_native_item:{item_type}",
                        )
                    else:
                        self._complete_native_item(attempt, item)
                return
            if item_type == "dynamicToolCall":
                lifecycle_params = {**params, **item}
                if not lifecycle_params.get("callId") and isinstance(item.get("id"), str):
                    lifecycle_params["callId"] = item["id"]
                dynamic_call, _parse_failure = self._dynamic_call_from_params(
                    lifecycle_params,
                )
                if dynamic_call is not None and attempt is not None:
                    call_id = str(dynamic_call.get("call_id") or "")
                    if method == "item/started":
                        attempt.dynamic_items[call_id] = deepcopy(dynamic_call)
                    if (
                        dynamic_call["tool"] in _INLINE_NATIVE_TOOLS
                        or not _is_canonical_native_handoff(dynamic_call["tool"])
                    ):
                        return
                    if call_id in attempt.dynamic_tool_responses:
                        # A pre-handoff typed rejection already closed this
                        # exact item. Its later lifecycle notification must not
                        # resurrect it as an effectful compatibility handoff.
                        return
                native_tool_call = _exact_canonical_native_call(lifecycle_params)
                if native_tool_call is not None:
                    if method == "item/completed":
                        self._mark_unsafe(
                            params,
                            f"canonical_tool:{native_tool_call['tool']}",
                            native_tool_call=native_tool_call,
                        )
                    return
            if item_type in _FORBIDDEN_ITEM_TYPES or item_type not in _SAFE_ITEM_TYPES:
                self._mark_unsafe(params, f"native_item:{item_type or 'unknown'}")
            return

        if method == "turn/started":
            if attempt is not None:
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if turn.get("id"):
                    attempt.turn_id = str(turn["id"])
            return

        if method == "turn/completed":
            if attempt is not None:
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                completion = {
                    "status": str(turn.get("status") or "failed"),
                    "turnId": (
                        str(turn.get("id")) if turn.get("id") else attempt.turn_id
                    ),
                    "errorCategory": self._turn_error_category(turn),
                }
                if attempt.pending_agent_message_items:
                    attempt.deferred_done = completion
                else:
                    attempt.events.put(("done", completion))
            return

        if method == "error":
            if attempt is not None:
                attempt.events.put(("provider_error", self._notification_error_category(params)))
            return

        if method == "turn/diff/updated":
            return

        if method not in _SAFE_NOTIFICATIONS:
            if attempt is not None and (
                method.startswith((
                    "item/", "hook/", "process/", "fs/", "mcpServer/",
                    "command/", "plugin/", "app/", "skills/",
                ))
            ):
                self._mark_unsafe(params, "unknown_action_event")

    @staticmethod
    def _notification_error_category(params: dict) -> str:
        error = params.get("error") if isinstance(params.get("error"), dict) else {}
        info = error.get("codexErrorInfo")
        if isinstance(info, dict):
            return str(info.get("type") or next(iter(info), "provider_error"))
        return "provider_error"

    @staticmethod
    def _turn_error_category(turn: dict) -> str:
        error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
        info = error.get("codexErrorInfo")
        if isinstance(info, dict):
            return str(info.get("type") or next(iter(info), "turn_failed"))
        return "turn_failed"

    def get_account_status(self) -> dict:
        result = self._request("account/read", {"refreshToken": False})
        account = result.get("account") if isinstance(result.get("account"), dict) else None
        safe_account = None
        if account and account.get("type") == "chatgpt":
            safe_account = {
                "type": "chatgpt",
                "email": account.get("email") if isinstance(account.get("email"), str) else None,
                "planType": str(account.get("planType") or "unknown"),
            }
        if safe_account is None:
            with self._state_lock:
                self._rate_limits_cache = None
                self._rate_limits_pending_update = {}
        return {
            "connected": bool(safe_account),
            "account": safe_account,
            "requiresOpenaiAuth": bool(result.get("requiresOpenaiAuth")),
        }

    @staticmethod
    def _validate_auth_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            host == "chatgpt.com"
            or host.endswith(".chatgpt.com")
            or host == "openai.com"
            or host.endswith(".openai.com")
        )

    def begin_login(self) -> dict:
        self.start()
        with self._login_lock:
            if self._active_login is not None:
                return dict(self._active_login)
            result = self._request(
                "account/login/start",
                {"type": "chatgpt", "useHostedLoginSuccessPage": True, "appBrand": "chatgpt"},
            )
            login_id = str(result.get("loginId") or "")
            auth_url = str(result.get("authUrl") or "")
            if result.get("type") != "chatgpt" or not login_id or not self._validate_auth_url(auth_url):
                raise AppServerError(
                    "ERROR: Codex did not return a valid managed ChatGPT login flow.",
                    category="login_invalid",
                )
            state: dict = {}
            self._login_events[login_id] = (threading.Event(), state)
            self._active_login = {"loginId": login_id, "authUrl": auth_url}
            return dict(self._active_login)

    def begin_device_code_login(self) -> dict:
        """Start the stable App Server device-code login flow."""
        self.start()
        with self._login_lock:
            if self._active_login is not None:
                return dict(self._active_login)
            result = self._request(
                "account/login/start", {"type": "chatgptDeviceCode"}
            )
            login_id = str(result.get("loginId") or "")
            verification_url = str(result.get("verificationUrl") or "")
            user_code = str(result.get("userCode") or "")
            if (
                result.get("type") != "chatgptDeviceCode"
                or not login_id
                or not user_code
                or not self._validate_auth_url(verification_url)
            ):
                raise AppServerError(
                    "ERROR: Codex did not return a valid ChatGPT device-code flow.",
                    category="login_invalid",
                )
            state: dict = {}
            self._login_events[login_id] = (threading.Event(), state)
            self._active_login = {
                "loginId": login_id,
                "verificationUrl": verification_url,
                "userCode": user_code,
            }
            return dict(self._active_login)

    def wait_for_login(self, login_id: str, timeout: float = 300.0) -> dict:
        login_id = str(login_id)
        with self._login_lock:
            waiter = self._login_events.get(login_id)
        if waiter is None:
            raise AppServerError("ERROR: no ChatGPT login is active.", category="login_missing")
        if not waiter[0].wait(timeout):
            try:
                self._request(
                    "account/login/cancel", {"loginId": login_id}, timeout=10.0,
                )
            except AppServerError:
                pass
            self._clear_login_state(login_id, cancelled=True)
            raise AppServerError("ERROR: ChatGPT login timed out.", category="login_timeout")
        success = bool(waiter[1].get("success"))
        cancelled = bool(waiter[1].get("cancelled"))
        failure_category = waiter[1].get("failure_category")
        self._clear_login_state(login_id)
        if cancelled:
            raise AppServerError("ERROR: ChatGPT login was cancelled.", category="login_cancelled")
        if failure_category:
            raise AppServerError(
                "ERROR: the Codex App Server stopped during ChatGPT login.",
                category=str(failure_category),
            )
        if not success:
            raise AppServerError("ERROR: ChatGPT login was not completed.", category="login_failed")
        return self.get_account_status()

    def cancel_login(self) -> None:
        with self._login_lock:
            active = dict(self._active_login) if self._active_login else None
        if not active:
            return
        login_id = str(active["loginId"])
        error = None
        try:
            self._request("account/login/cancel", {"loginId": login_id})
        except AppServerError as exc:
            error = exc
        finally:
            self._clear_login_state(login_id, cancelled=True)
        if error is not None:
            raise error

    def reset_login(self) -> None:
        """Best-effort cancellation used before retrying a stale login."""
        with self._login_lock:
            active = dict(self._active_login) if self._active_login else None
        if not active:
            return
        login_id = str(active["loginId"])
        try:
            self._request(
                "account/login/cancel", {"loginId": login_id}, timeout=10.0,
            )
        except AppServerError:
            pass
        self._clear_login_state(login_id, cancelled=True)

    def _clear_login_state(self, login_id: str, *, cancelled: bool = False) -> None:
        with self._login_lock:
            waiter = self._login_events.pop(str(login_id), None)
            if self._active_login and self._active_login.get("loginId") == str(login_id):
                self._active_login = None
        if waiter:
            if cancelled:
                waiter[1].update({"success": False, "cancelled": True})
            waiter[0].set()

    def logout(self) -> None:
        # Stop a pending OAuth flow before logging out, so it cannot complete
        # afterwards and reconnect the account the operator just disconnected.
        self.reset_login()
        self._request("account/logout", {})
        with self._state_lock:
            self._rate_limits_cache = None
            self._rate_limits_pending_update = {}
        with self._login_lock:
            self._active_login = None
            self._login_events.clear()

    def list_models(self) -> list[dict]:
        models: list[dict] = []
        cursor = None
        while True:
            params = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = self._request("model/list", params)
            for item in result.get("data") or []:
                if not isinstance(item, dict) or item.get("hidden"):
                    continue
                model_id = str(item.get("id") or item.get("model") or "").strip()
                modalities = item.get("inputModalities") or ["text"]
                if not model_id or "text" not in modalities:
                    continue
                efforts = []
                for effort in item.get("supportedReasoningEfforts") or []:
                    if isinstance(effort, dict) and effort.get("reasoningEffort"):
                        efforts.append(
                            {
                                "id": str(effort["reasoningEffort"]),
                                "description": str(effort.get("description") or ""),
                            }
                        )
                models.append(
                    {
                        "id": model_id,
                        "displayName": str(item.get("displayName") or model_id),
                        "description": str(item.get("description") or ""),
                        "isDefault": bool(item.get("isDefault")),
                        "defaultReasoningEffort": str(item.get("defaultReasoningEffort") or ""),
                        "supportedReasoningEfforts": efforts,
                        "inputModalities": list(modalities),
                    }
                )
            cursor = result.get("nextCursor")
            if not cursor:
                return models

    def get_rate_limits(self) -> dict:
        with self._rate_limits_refresh_lock:
            with self._state_lock:
                account_generation = self._account_generation
                self._rate_limits_pending_update = {}
            try:
                result = self._request("account/rateLimits/read", {})
            except AppServerError:
                with self._state_lock:
                    if self._rate_limits_cache is not None:
                        return deepcopy(self._rate_limits_cache)
                raise
            safe = sanitize_rate_limit_payload(result)
            with self._state_lock:
                if account_generation != self._account_generation:
                    safe = {}
                if self._rate_limits_pending_update:
                    safe = merge_rate_limit_payload(
                        safe, self._rate_limits_pending_update
                    )
                self._rate_limits_cache = safe
                self._rate_limits_pending_update = {}
                return deepcopy(safe)

    def _record_context_notification(self, thread_id, method, params, attempt) -> None:
        turn = params.get("turn") or {}
        turn_id = str(params.get("turnId") or (turn.get("id") if isinstance(turn, dict) else "") or "")
        if not turn_id:
            return
        with self._state_lock:
            request = self._compaction_requests.get(thread_id)
            if request is not None and method == "turn/started" and not request.turn_id:
                request.turn_id = turn_id
            manual = request is not None and request.turn_id == turn_id
            if not manual and attempt is None:
                return
            if method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage")
                if isinstance(usage, dict):
                    last = usage.get("last")
                    used = last.get("totalTokens") if isinstance(last, dict) else None
                    maximum = usage.get("modelContextWindow")
                    if type(used) is int and used >= 0 and type(maximum) is int and maximum > 0:
                        self._context_usage[thread_id] = (used, maximum)
                return
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "contextCompaction":
                if method == "item/started":
                    self._compacting_threads.add(thread_id)
                elif method == "item/completed":
                    self._compacting_threads.discard(thread_id)
                    self._context_usage.pop(thread_id, None)
                    if manual:
                        request.compacted = True
            if method == "turn/completed":
                self._compacting_threads.discard(thread_id)
                if manual:
                    request.succeeded = request.compacted and isinstance(turn, dict) and turn.get("status") == "completed"
                    request.event.set()

    def thread_context_usage(self, thread_id: str) -> dict | None:
        with self._state_lock:
            usage = self._context_usage.get(thread_id)
            if usage is None:
                return None
            return {
                "used": usage[0], "maximum": usage[1], "estimated": False,
                "compacting": thread_id in self._compacting_threads,
            }

    def compact_thread(self, thread_id: str) -> None:
        """Compact an idle thread and wait for its correlated completion."""
        request = _CompactionRequest()
        with self._state_lock:
            if thread_id in self._attempts or thread_id in self._compaction_requests:
                raise AppServerError("ERROR: the ChatGPT conversation is busy.", category="turn_busy")
            self._compaction_requests[thread_id] = request
        try:
            self._request("thread/compact/start", {"threadId": thread_id})
            if not request.event.wait(timeout=120.0) or not request.succeeded:
                raise AppServerError(
                    "ERROR: ChatGPT context compaction was not confirmed.",
                    allow_fallback=False, category="compaction_unconfirmed",
                )
        finally:
            with self._state_lock:
                self._compaction_requests.pop(thread_id, None)
                self._compacting_threads.discard(thread_id)

    def delete_thread(self, thread_id: str) -> None:
        self._request("thread/delete", {"threadId": thread_id})
        with self._state_lock:
            self._context_usage.pop(thread_id, None)
            self._compacting_threads.discard(thread_id)

    def start_or_resume_conversation(
        self,
        *,
        thread_id: str | None,
        model: str,
        base_instructions: str,
        developer_instructions: str,
        dynamic_tools=None,
        persistent: bool = False,
    ) -> str:
        if thread_id:
            result = self._request("thread/resume", {
                "threadId": thread_id,
                "model": model,
                "cwd": str(self.workspace_dir),
                "approvalPolicy": "on-request",
                "sandbox": "read-only",
                "excludeTurns": True,
                **({"config": {"compact_prompt": _COMPACT_PROMPT}} if persistent else {}),
            })
            thread = result.get("thread") or {}
            if result.get("instructionSources"):
                raise AppServerError(
                    "ERROR: Codex inherited external instruction files.",
                    category="isolation_failed",
                )
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                raise AppServerError(
                    "ERROR: Codex did not resume the requested conversation.",
                    allow_fallback=False, category="thread_failed",
                )
            return thread_id

        result = self._request(
            "thread/start",
            {
                "model": model,
                "cwd": str(self.workspace_dir),
                "approvalPolicy": "on-request",
                "sandbox": "read-only",
                "ephemeral": not persistent,
                "serviceName": "osia_pc",
                "baseInstructions": base_instructions,
                "developerInstructions": developer_instructions,
                "dynamicTools": (
                    _canonical_native_tools()
                    if dynamic_tools is None else list(dynamic_tools)
                ),
                **({"config": {"compact_prompt": _COMPACT_PROMPT}} if persistent else {}),
            },
        )
        if result.get("instructionSources"):
            raise AppServerError(
                "ERROR: Codex inherited external instruction files.",
                category="isolation_failed",
            )
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        new_id = str(thread.get("id") or "")
        if not new_id:
            raise AppServerError("ERROR: Codex did not create a conversation.", category="thread_failed")
        return new_id

    def run_turn(
        self,
        *,
        thread_id: str,
        model: str,
        text: str | None = None,
        content: str | list[dict] | None = None,
        conversation_id: str | None = None,
        osia_turn_id: str | None = None,
        client_request_id: str | None = None,
        final_response_message_id: str | None = None,
        effort: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_summary: Callable[[dict], None] | None = None,
        on_attempt_registered: Callable[[], None] | None = None,
        timeout: float = 300.0,
        output_schema: dict | None = None,
        application_context: str = "",
    ) -> TurnResult:
        turn_content = content if content is not None else text
        if turn_content is None:
            raise AppServerError(
                "ERROR: no ChatGPT account turn input was provided.",
                category="empty_input",
            )
        staged_images: list[Path] = []
        staged_deliveries: list[str] = []
        attempt = _TurnAttempt(
            thread_id=thread_id,
            conversation_id=str(conversation_id or thread_id),
            osia_turn_id=str(osia_turn_id or ""),
            client_request_id=str(client_request_id or ""),
            final_response_message_id=str(final_response_message_id or ""),
            model_id=str(model or "unknown"),
            effective_model_id=str(model or "unknown"),
            user_request=_latest_user_text(turn_content),
        )
        try:
            from osia.execution_strategy import current_execution_strategy

            attempt.execution_strategy = current_execution_strategy()
        except Exception:
            attempt.execution_strategy = None
        with self._state_lock:
            if thread_id in self._attempts or thread_id in self._compaction_requests:
                raise AppServerError(
                    "ERROR: a ChatGPT account turn is already active for this conversation.",
                    allow_fallback=False,
                    category="turn_busy",
                )
            self._attempts[thread_id] = attempt
        try:
            app_input, staged_images, staged_deliveries = self._stage_turn_input(
                turn_content,
                attempt,
            )
            attempt.local_image_delivery_ids.update(staged_deliveries)
            if staged_deliveries:
                attempt.image_input_present = True
            params = {
                "threadId": thread_id,
                "input": app_input,
                "model": model,
                "approvalPolicy": "on-request",
                "cwd": str(self.workspace_dir),
            }
            if effort:
                params["effort"] = effort
            if application_context:
                params["additionalContext"] = {
                    "osia": {"kind": "application", "value": application_context},
                }
            if on_reasoning_summary is not None:
                params["summary"] = "concise"
            if output_schema is not None:
                if not isinstance(output_schema, dict):
                    raise AppServerError(
                        "ERROR: the structured output schema is invalid.",
                        category="invalid_input",
                    )
                params["outputSchema"] = deepcopy(output_schema)
            if staged_deliveries:
                from osia.image_inputs import (
                    begin_media_delivery,
                    fail_media_delivery,
                )

                try:
                    for delivery_id in staged_deliveries:
                        dispatching = begin_media_delivery(delivery_id)
                        if (
                            dispatching is None
                            or dispatching.state != "DISPATCHING"
                        ):
                            raise RuntimeError(
                                "image_delivery_dispatch_state_unavailable"
                            )
                except Exception as exc:
                    for delivery_id in staged_deliveries:
                        try:
                            fail_media_delivery(
                                delivery_id,
                                reason_code="IMAGE_DELIVERY_FAILED_BEFORE_COMMIT",
                            )
                        except Exception:
                            pass
                    raise AppServerError(
                        "ERROR: IMAGE_DELIVERY_FAILED",
                        allow_fallback=False,
                        category="IMAGE_DELIVERY_FAILED",
                    ) from exc
            try:
                response = self._request("turn/start", params)
            except Exception as exc:
                from osia.image_inputs import (
                    fail_media_delivery,
                    mark_media_delivery_commit_unknown,
                )

                for delivery_id in staged_deliveries:
                    if (
                        isinstance(exc, AppServerError)
                        and exc.category in {
                            "request_rejected", "process_stopped",
                        }
                    ):
                        fail_media_delivery(
                            delivery_id,
                            reason_code=(
                                "PROVIDER_REQUEST_REJECTED"
                                if exc.category == "request_rejected" else
                                "IMAGE_DELIVERY_FAILED_BEFORE_COMMIT"
                            ),
                        )
                    else:
                        mark_media_delivery_commit_unknown(
                            delivery_id,
                            reason_code="IMAGE_DELIVERY_COMMIT_UNKNOWN",
                        )
                raise
            if staged_deliveries:
                from osia.image_inputs import (
                    commit_media_delivery,
                    mark_media_delivery_commit_unknown,
                )

                for delivery_id in staged_deliveries:
                    try:
                        committed = commit_media_delivery(delivery_id)
                    except Exception:
                        committed = None
                    if committed is None or committed.state not in {
                        "COMMITTED", "ACKNOWLEDGED",
                    }:
                        try:
                            committed = mark_media_delivery_commit_unknown(
                                delivery_id,
                                reason_code="IMAGE_DELIVERY_COMMIT_UNKNOWN",
                            )
                        except Exception:
                            committed = None
                        if not attempt.first_causal_failure:
                            attempt.first_causal_failure = (
                                "IMAGE_DELIVERY_COMMIT_UNKNOWN"
                            )
            turn = response.get("turn") if isinstance(response.get("turn"), dict) else {}
            if turn.get("id"):
                response_turn_id = str(turn["id"])
                if attempt.turn_id and attempt.turn_id != response_turn_id:
                    raise AppServerError(
                        "ERROR: the App Server returned a mismatched turn identity.",
                        allow_fallback=False,
                        category="TOOL_RESPONSE_ID_MISMATCH",
                )
                attempt.turn_id = response_turn_id
                if on_attempt_registered is not None:
                    on_attempt_registered()
                pending_notifications = list(
                    attempt.pending_turn_notifications
                )
                attempt.pending_turn_notifications.clear()
                pending_requests = list(attempt.pending_server_requests)
                attempt.pending_server_requests.clear()
                for pending_request in pending_requests:
                    self._handle_server_request(pending_request)
                for pending_method, pending_params in pending_notifications:
                    pending_turn_id = str(
                        pending_params.get("turnId")
                        or (
                            (pending_params.get("turn") or {}).get("id")
                            if isinstance(pending_params.get("turn"), dict)
                            else ""
                        )
                        or ""
                    )
                    if pending_turn_id == response_turn_id:
                        self._dispatch_notification(
                            pending_method, pending_params,
                        )
            if staged_deliveries and attempt.model_successor_observed:
                self._acknowledge_local_image_deliveries(attempt)

            deadline = time.monotonic() + timeout
            provider_error = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.cancel_turn(thread_id=thread_id, turn_id=attempt.turn_id)
                    output_waiting = bool(
                        attempt.deferred_done is not None
                        and attempt.pending_agent_message_items
                    )
                    raise AppServerError(
                        (
                            "ERROR: OUTPUT_AVAILABILITY_TIMEOUT"
                            if output_waiting else
                            "ERROR: the ChatGPT account turn timed out."
                        ),
                        output_started=attempt.output_started,
                        native_action=attempt.native_effect_started,
                        allow_fallback=(
                            not attempt.output_started
                            and not attempt.native_effect_started
                        ),
                        category=(
                            "OUTPUT_AVAILABILITY_TIMEOUT"
                            if output_waiting else "turn_timeout"
                        ),
                    )
                try:
                    kind, payload = attempt.events.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    process = self._process
                    if process is None or process.poll() is not None:
                        raise AppServerError(
                            "ERROR: the Codex App Server stopped during the turn.",
                            output_started=attempt.output_started,
                            native_action=attempt.native_effect_started,
                            allow_fallback=(
                                not attempt.output_started
                                and not attempt.native_effect_started
                            ),
                            category="process_crash",
                        )
                    continue
                if kind == "delta":
                    if on_delta is not None:
                        on_delta(str(payload))
                elif kind == "public_reasoning_summary":
                    if on_reasoning_summary is not None:
                        on_reasoning_summary(payload)
                elif kind == "provider_error":
                    provider_error = str(payload)
                elif kind == "dynamic_tool_call" and isinstance(payload, dict):
                    tool_started = time.monotonic()
                    self._resolve_dynamic_tool_call(attempt, payload)
                    deadline += max(0.0, time.monotonic() - tool_started)
                elif kind == "error" and isinstance(payload, AppServerError):
                    payload.output_started = attempt.output_started
                    payload.native_action = (
                        payload.native_action or attempt.native_effect_started
                    )
                    payload.allow_fallback = bool(
                        payload.category != "MODEL_IDENTITY_CHANGED"
                        and not attempt.output_started
                        and not attempt.native_effect_started
                    )
                    raise payload
                elif kind == "unsafe":
                    native_tool_call = attempt.native_tool_call
                    raise AppServerError(
                        (
                            "ERROR: canonical OSIA tool handoff required."
                            if native_tool_call is not None
                            else _native_action_refusal(turn_content)
                        ),
                        output_started=attempt.output_started,
                        native_action=True,
                        controlled_refusal=native_tool_call is None,
                        native_tool_call=native_tool_call,
                        reformulatable_native_action=native_tool_call is None,
                        allow_fallback=False,
                        category=str(payload),
                    )
                elif kind == "done":
                    status = str(payload.get("status") or "failed")
                    turn_id = payload.get("turnId") or attempt.turn_id
                    public_summary = self._public_reasoning_summary(attempt)
                    if (
                        public_summary
                        and public_summary
                        != attempt.public_reasoning_summary_emitted
                    ):
                        attempt.public_reasoning_summary = public_summary
                        attempt.public_reasoning_summary_emitted = public_summary
                        if on_reasoning_summary is not None:
                            on_reasoning_summary(
                                self._public_reasoning_summary_payload(
                                    attempt, public_summary,
                                )
                            )
                    if attempt.native_action:
                        native_tool_call = attempt.native_tool_call
                        raise AppServerError(
                            (
                                "ERROR: canonical OSIA tool handoff required."
                                if native_tool_call is not None
                                else _native_action_refusal(turn_content)
                            ),
                            output_started=attempt.output_started,
                            native_action=True,
                            controlled_refusal=native_tool_call is None,
                            native_tool_call=native_tool_call,
                            reformulatable_native_action=native_tool_call is None,
                            allow_fallback=False,
                            category=attempt.unsafe_category or "native_action",
                        )
                    if attempt.outcome_unknown:
                        raise AppServerError(
                            "ERROR: native action outcome is unknown; equivalent execution is frozen.",
                            output_started=attempt.output_started,
                            native_action=True,
                            controlled_refusal=True,
                            allow_fallback=False,
                            category="native_outcome_unknown",
                        )
                    if (
                        _selected_model_identity_changed(attempt)
                        or (
                            attempt.image_input_present
                            and attempt.model_rerouted
                        )
                    ):
                        raise AppServerError(
                            "ERROR: MODEL_IDENTITY_CHANGED",
                            output_started=attempt.output_started,
                            native_action=attempt.native_effect_started,
                            allow_fallback=False,
                            category="MODEL_IDENTITY_CHANGED",
                        )
                    if status != "completed":
                        cancelled = status == "interrupted"
                        causal_failure = str(
                            attempt.first_causal_failure
                            or (
                                "PROVIDER_CONTINUATION_REJECTED"
                                if attempt.dynamic_response_sent else ""
                            )
                        )
                        raise AppServerError(
                            (
                                "ERROR: " + causal_failure
                                if causal_failure else
                                "ERROR: the ChatGPT account turn was cancelled."
                                if cancelled else
                                "ERROR: the ChatGPT account turn failed before completion."
                            ),
                            output_started=attempt.output_started,
                            native_action=attempt.native_effect_started,
                            allow_fallback=(
                                False
                                if causal_failure else
                                not attempt.output_started
                                and not cancelled
                                and not attempt.native_effect_started
                            ),
                            category=(
                                causal_failure
                                or provider_error
                                or str(payload.get("errorCategory") or status)
                            ),
                        )
                    if attempt.visual_delivery_required_failed:
                        raise AppServerError(
                            "ERROR: IMAGE_DELIVERY_FAILED",
                            output_started=attempt.output_started,
                            native_action=attempt.native_effect_started,
                            allow_fallback=False,
                            category=(
                                attempt.first_causal_failure
                                or "IMAGE_DELIVERY_FAILED"
                            ),
                        )
                    committed_visual = any(
                        entry.get("delivery_state") in {
                            "COMMITTED", "ACKNOWLEDGED",
                        }
                        for entry in attempt.visual_frame_calls.values()
                    )
                    unresolved_visual_delivery = any(
                        entry.get("delivery_state") in {
                            "PREPARED", "DISPATCHING", "COMMIT_UNKNOWN",
                        }
                        for entry in attempt.visual_frame_calls.values()
                    )
                    if unresolved_visual_delivery:
                        raise AppServerError(
                            "ERROR: IMAGE_DELIVERY_FAILED",
                            output_started=attempt.output_started,
                            native_action=attempt.native_effect_started,
                            allow_fallback=False,
                            category=(
                                attempt.first_causal_failure
                                or "IMAGE_DELIVERY_FAILED"
                            ),
                        )
                    if attempt.local_image_delivery_ids:
                        raise AppServerError(
                            "ERROR: MODEL_OUTPUT_MISSING",
                            output_started=attempt.output_started,
                            native_action=attempt.native_effect_started,
                            allow_fallback=False,
                            category=(
                                attempt.first_causal_failure
                                or "MODEL_OUTPUT_MISSING"
                            ),
                        )
                    if committed_visual and not attempt.post_visual_agent_items:
                        raise AppServerError(
                            "ERROR: MODEL_VISUAL_OUTPUT_MISSING",
                            output_started=attempt.output_started,
                            native_action=attempt.native_effect_started,
                            allow_fallback=False,
                            category="MODEL_VISUAL_OUTPUT_MISSING",
                        )
                    if (
                        not attempt.text_parts
                        and attempt.visual_consumed_calls
                    ):
                        raise AppServerError(
                            "ERROR: MODEL_VISUAL_OUTPUT_MISSING",
                            output_started=False,
                            native_action=attempt.native_effect_started,
                            allow_fallback=False,
                            category="MODEL_VISUAL_OUTPUT_MISSING",
                        )
                    result = TurnResult(
                        text="".join(attempt.text_parts),
                        thread_id=thread_id,
                        turn_id=str(turn_id) if turn_id else None,
                        status=status,
                        output_started=attempt.output_started,
                        capability_events=tuple(attempt.capability_events),
                        public_reasoning_summary=public_summary,
                        response_items=tuple(
                            dict(item)
                            for item in attempt.response_item_diagnostics
                        ),
                        final_output_item_id=attempt.final_output_item_id,
                        model_rerouted=attempt.model_rerouted,
                        effective_model_id=attempt.effective_model_id,
                    )
                    return result
        except AppServerError as exc:
            exc.capability_events = tuple(attempt.capability_events)
            exc.native_turn_id = attempt.turn_id
            exc.public_reasoning_summary = self._public_reasoning_summary(attempt)
            exc.response_items = tuple(
                dict(item) for item in attempt.response_item_diagnostics
            )
            exc.final_output_item_id = attempt.final_output_item_id
            raise
        finally:
            with self._state_lock:
                self._attempts.pop(thread_id, None)
            for path in [*staged_images, *attempt.bridged_visual_paths]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _stage_turn_input(
        self,
        content: str | list[dict],
        attempt: _TurnAttempt,
    ) -> tuple[list[dict], list[Path], list[str]]:
        """Convert verified inline images to native, turn-scoped localImage items."""

        if isinstance(content, str):
            return [{"type": "text", "text": content}], [], []
        if not isinstance(content, list):
            raise AppServerError(
                "ERROR: the ChatGPT account turn input is invalid.",
                category="invalid_input",
            )

        from osia.attachments import MAX_ATTACHMENT_BYTES
        from osia.image_inputs import (
            ImageInputError,
            ImageSourceType,
            MediaEnvelopeType,
            content_with_images,
            create_image_artifact,
            decode_image_data_url,
            fail_media_delivery,
            prepare_media_delivery,
            reference_owner_id,
            resolve_image_reference,
        )

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        staged: list[Path] = []
        deliveries: list[str] = []
        items: list[dict] = []
        suffixes = {
            "image/gif": ".gif",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        try:
            for part in content:
                if not isinstance(part, dict):
                    raise AppServerError(
                        "ERROR: the ChatGPT account image input is invalid.",
                        category="invalid_input",
                    )
                if part.get("type") == "text":
                    items.append({"type": "text", "text": str(part.get("text") or "")})
                    continue
                if part.get("type") != "image_url":
                    raise AppServerError(
                        "ERROR: the ChatGPT account input contains an unsupported part.",
                        category="invalid_input",
                    )
                try:
                    decoded = decode_image_data_url(
                        part.get("image_url"),
                        maximum_bytes=MAX_ATTACHMENT_BYTES,
                    )
                except ImageInputError as exc:
                    raise AppServerError(str(exc), category="invalid_image") from exc
                media_reference = part.get("_osia_media")
                if not isinstance(media_reference, dict):
                    artifact = create_image_artifact(
                        decoded.data,
                        decoded.mime,
                        ImageSourceType.USER_UPLOAD,
                    )
                    media_reference = content_with_images(
                        "",
                        (artifact,),
                        owner_id=attempt.conversation_id,
                    )[0]
                else:
                    media_owner_id = reference_owner_id(
                        media_reference,
                        conversation_id=attempt.conversation_id,
                        turn_id=attempt.osia_turn_id,
                    )
                    artifact = resolve_image_reference(
                        media_reference,
                        owner_id=media_owner_id,
                    )
                    if (
                        decoded.mime != artifact.mime_type
                        or decoded.content_hash != artifact.content_hash
                        or (decoded.width, decoded.height)
                        != (artifact.width, artifact.height)
                    ):
                        raise AppServerError(
                            "ERROR: canonical image metadata does not match the "
                            "provider payload.",
                            category="IMAGE_REF_METADATA_MISMATCH",
                        )
                delivery = prepare_media_delivery(
                    media_reference,
                    owner_id=(
                        reference_owner_id(
                            media_reference,
                            conversation_id=attempt.conversation_id,
                            turn_id=attempt.osia_turn_id,
                        )
                    ),
                    provider="chatgpt_account",
                    model=attempt.model_id,
                    conversation_id=attempt.conversation_id,
                    request_id=(attempt.client_request_id or attempt.thread_id),
                    turn_id=(attempt.osia_turn_id or attempt.thread_id),
                    envelope_type=MediaEnvelopeType.LOCAL_IMAGE,
                    transport_id=(
                        f"generation:{self.generation}:"
                        f"thread:{attempt.thread_id}"
                    ),
                )
                if delivery.state in {"COMMITTED", "ACKNOWLEDGED"}:
                    continue
                if not delivery.send_authorized:
                    raise AppServerError(
                        "ERROR: an identical image delivery is already in "
                        "progress or has an unknown commit outcome.",
                        category=(
                            "IMAGE_DELIVERY_COMMIT_UNKNOWN"
                            if delivery.state == "COMMIT_UNKNOWN" else
                            "IMAGE_DELIVERY_IN_PROGRESS"
                        ),
                    )
                deliveries.append(delivery.delivery_id)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix="vision-",
                    suffix=suffixes[artifact.mime_type],
                    dir=self.temp_dir,
                    delete=False,
                ) as handle:
                    handle.write(artifact.image_bytes)
                    path = Path(handle.name)
                staged.append(path)
                items.append({"type": "localImage", "path": str(path)})
            if not items:
                raise AppServerError(
                    "ERROR: the ChatGPT account turn input is empty.",
                    category="empty_input",
                )
            return items, staged, deliveries
        except Exception:
            for delivery_id in deliveries:
                try:
                    fail_media_delivery(
                        delivery_id,
                        reason_code="IMAGE_DELIVERY_FAILED",
                    )
                except Exception:
                    pass
            for path in staged:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def cancel_turn(self, *, thread_id: str, turn_id: str | None = None) -> None:
        if not turn_id:
            with self._state_lock:
                attempt = self._attempts.get(thread_id)
                turn_id = attempt.turn_id if attempt else None
        if not turn_id:
            return
        params = {"threadId": thread_id, "turnId": turn_id}
        try:
            self._request("turn/interrupt", params, timeout=10.0)
        except AppServerError:
            pass

    def shutdown(self) -> None:
        with self._start_lock:
            self._stop_operational_log_redaction()
            process = self._process
            if process is None:
                cleaned = self._drain_operational_image_logs(compact=True)
                self._image_log_redaction_pending = not cleaned
                if not cleaned:
                    print("[ERROR] private App Server image log cleanup failed")
                try:
                    from osia.image_inputs import cleanup_owned_temp_images

                    cleanup_owned_temp_images(
                        directories=(self.temp_dir,),
                        minimum_age_seconds=0,
                    )
                except Exception:
                    pass
                return
            with self._state_lock:
                self._intentional_processes.add(id(process))
            self._process = None
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass
            self._fail_all("shutdown")
            cleaned = self._drain_operational_image_logs(compact=True)
            self._image_log_redaction_pending = not cleaned
            if not cleaned:
                print("[ERROR] private App Server image log cleanup failed")
            try:
                from osia.image_inputs import cleanup_owned_temp_images

                cleanup_owned_temp_images(
                    directories=(self.temp_dir,),
                    minimum_age_seconds=0,
                )
            except Exception:
                pass
