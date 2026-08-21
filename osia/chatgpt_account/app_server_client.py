"""Small fail-closed client for the stable Codex App Server JSONL protocol.

The process is deliberately isolated from the normal Codex installation and
from OSIA's repository/configuration.  Turn-scoped local images are input-only.
Provider-hosted read capabilities remain native, observable local command/file
effects use OSIA's canonical permission broker, and exact OSIA dynamic tools
remain a compatibility path.
"""

from __future__ import annotations

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

from osia.runtime import resource_path
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
CLIENT_VERSION = "1.0.0"

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
    "contextCompaction", "webSearch",
})
_FORBIDDEN_ITEM_TYPES = frozenset(
    {
        "collabAgentToolCall",
        "dynamicToolCall",
        "hookPrompt",
        "imageGeneration",
        "imageView",
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
    "read_file",
    "write_file",
    "navigate",
    "open_browser",
    "navigate_browser",
    "read_document",
    "query_document",
)


def _canonical_native_tools(execution_strategy=None) -> list[dict]:
    """Expose only exact OSIA identities backed by authoritative schemas."""

    from osia.capability_federation import federated_capabilities
    from osia.tool_contracts import get_static_registry

    registry = get_static_registry()
    available_osia = {
        item.identity
        for item in federated_capabilities("chatgpt_account")
        if item.available and item.source == "osia"
    }
    tools = []
    for name in _CANONICAL_NATIVE_TOOL_NAMES:
        if name not in available_osia:
            continue
        spec = registry.get(name)
        if spec is None or not spec.chat_eligible:
            continue
        if execution_strategy is not None:
            try:
                from osia.execution_strategy import tool_applicability

                applicable, _reason = tool_applicability(
                    execution_strategy, name, {},
                )
            except Exception:
                applicable = False
            if not applicable:
                continue
        tools.append(
            {
                "type": "function",
                "name": spec.name,
                "description": spec.capability,
                "inputSchema": deepcopy(spec.input_schema),
            }
        )
    return tools


def _exact_canonical_native_call(params: dict) -> dict | None:
    """Return one value-only exact handoff; never infer from native commands."""

    namespace = params.get("namespace")
    tool = params.get("tool")
    arguments = params.get("arguments")
    call_id = params.get("callId") or params.get("call_id")
    if namespace not in {None, ""}:
        return None
    if tool not in _CANONICAL_NATIVE_TOOL_NAMES:
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
    from osia.turn_context import resolve_turn_context

    language = resolve_turn_context(
        _latest_user_text(turn_content),
        saved_language=i18n.get_language(),
    ).language
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


@dataclass(frozen=True)
class TurnResult:
    text: str
    thread_id: str
    turn_id: str | None
    status: str
    output_started: bool = False
    capability_events: tuple[CapabilityEvent, ...] = ()


@dataclass
class _PendingRequest:
    response: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))


@dataclass
class _NativeDispatch:
    item_id: str
    tool_name: str
    arguments: dict
    side_effect: str
    idempotence: str
    retry_policy: str
    equivalence_fingerprint: str = ""
    tool_attempt_id: str = ""
    started: bool = False
    terminal: bool = False
    strategy_decision: Any = None
    semantic_postcondition: str = ""


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
    native_items: dict[str, dict] = field(default_factory=dict)
    native_dispatches: dict[str, _NativeDispatch] = field(default_factory=dict)
    terminal_native_items: set[str] = field(default_factory=set)
    frozen_equivalences: set[str] = field(default_factory=set)
    native_effect_started: bool = False
    outcome_unknown: bool = False
    terminal_permission_outcome: str = ""
    execution_strategy: Any = None


def _runtime_base_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / "OSIA" / "Codex"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OSIA" / "Codex"
    root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return root / "OSIA" / "Codex"


def _version_tuple(raw: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?:codex-cli\s+)?(\d+)\.(\d+)\.(\d+)", raw or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


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
    override = os.environ.get("OSIA_CODEX_EXECUTABLE")
    if override:
        candidates.append(Path(override))

    if sys.platform == "win32":
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
        startup_timeout: float = 15.0,
        request_timeout: float = 30.0,
        tool_attempt_store=None,
    ):
        self.base_dir = Path(base_dir) if base_dir else _runtime_base_dir()
        self.home_dir = self.base_dir / "home"
        self.workspace_dir = self.base_dir / "workspace"
        self.temp_dir = self.base_dir / "temp"
        self._process_factory = process_factory or subprocess.Popen
        self._runtime_executable = Path(runtime_executable) if runtime_executable else None
        self._runtime_version_override = runtime_version_override
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self._tool_attempt_store = tool_attempt_store

        self._process = None
        self._start_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._next_id = 1
        self._pending: dict[str, _PendingRequest] = {}
        self._attempts: dict[str, _TurnAttempt] = {}
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._wait_thread: threading.Thread | None = None
        self._intentional_processes: set[int] = set()
        self._generation = 0
        self._stderr_categories: dict[str, int] = {}
        self._crash_times: list[float] = []
        self._rate_limits_refresh_lock = threading.Lock()
        self._rate_limits_cache: dict | None = None
        self._rate_limits_pending_update: dict = {}

        self._login_lock = threading.Lock()
        self._active_login: dict | None = None
        self._login_events: dict[str, tuple[threading.Event, dict]] = {}

    @property
    def generation(self) -> int:
        return self._generation

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
        if self._runtime_executable is not None:
            version = self._runtime_version_override or _probe_executable(self._runtime_executable)
            if not version:
                raise RuntimeUnavailableError(
                    "ERROR: the native Codex runtime could not be started.",
                    category="runtime_missing",
                )
            parsed = _version_tuple(version)
            if parsed is None or not (SUPPORTED_MIN_VERSION <= parsed < SUPPORTED_MAX_VERSION):
                raise RuntimeUnavailableError(
                    "ERROR: the installed Codex runtime is incompatible with ChatGPT account support.",
                    category="runtime_incompatible",
                )
            return self._runtime_executable, version

        incompatible = False
        for candidate in _native_candidates(self.base_dir):
            version = _probe_executable(candidate)
            if not version:
                continue
            parsed = _version_tuple(version)
            if parsed and SUPPORTED_MIN_VERSION <= parsed < SUPPORTED_MAX_VERSION:
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
                    "plugin-creator", "skill-creator", "skill-installer",
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
            }
        )
        return env

    def start(self) -> None:
        with self._start_lock:
            if self._process is not None and self._process.poll() is None:
                return
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
            command = [str(executable), "app-server", "--stdio", "--strict-config"]
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
        if not intentional:
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
        for item in pending:
            try:
                item.response.put_nowait(error)
            except queue.Full:
                pass
        for attempt in attempts:
            attempt.events.put(("error", error))

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
    ) -> None:
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
        ))

    def _file_change_broker_call(
        self, attempt: _TurnAttempt, item_id: str,
    ) -> tuple[str, dict] | None:
        item = attempt.native_items.get(item_id)
        changes = item.get("changes") if isinstance(item, dict) else None
        if not isinstance(changes, list) or len(changes) != 1:
            return None
        change = changes[0] if isinstance(changes[0], dict) else {}
        kind = change.get("kind")
        kind_name = str(kind.get("type") or "") if isinstance(kind, dict) else str(kind or "")
        if kind_name not in {"add", "update"}:
            return None
        if kind_name == "update" and isinstance(kind, dict) and kind.get("move_path"):
            return None
        path_value = str(change.get("path") or "").strip()
        diff = change.get("diff")
        if not path_value or not isinstance(diff, str) or len(diff) > 2_000_000:
            return None
        path = Path(path_value)
        if not path.is_absolute():
            path = self.workspace_dir / path
        return "write_file", {
            "path": str(path.resolve(strict=False)),
            # This is the exact proposed patch body used only for policy and
            # receipt binding; OSIA does not execute it as write_file content.
            "content": diff,
        }

    def _native_broker_call(
        self, attempt: _TurnAttempt, method: str, params: dict,
    ) -> tuple[str, dict] | None:
        item_id = self._native_item_id(params)
        if not item_id:
            return None
        if method == "item/fileChange/requestApproval":
            return self._file_change_broker_call(attempt, item_id)
        if method != "item/commandExecution/requestApproval":
            return None
        if any(
            params.get(field)
            for field in (
                "additionalPermissions",
                "networkApprovalContext",
            )
        ):
            # These expand authority beyond the exact local command contract;
            # the canonical broker has no sealed permit for that extra scope.
            return None
        command = params.get("command")
        if not isinstance(command, str) or not command or len(command) > 32_768:
            return None
        available_decisions = params.get("availableDecisions")
        if isinstance(available_decisions, list) and "accept" not in {
            str(item) for item in available_decisions
        }:
            return None
        cwd = params.get("cwd")
        try:
            resolved_cwd = Path(
                str(cwd) if cwd else self.workspace_dir,
            ).resolve(strict=False)
        except (OSError, ValueError):
            return None
        started_item = attempt.native_items.get(item_id)
        if (
            not isinstance(started_item, dict)
            or started_item.get("type") != "commandExecution"
            or started_item.get("command") != command
        ):
            return None
        try:
            started_cwd = Path(
                str(started_item.get("cwd") or self.workspace_dir),
            ).resolve(strict=False)
        except (OSError, ValueError):
            return None
        if started_cwd != resolved_cwd:
            return None
        arguments = {"command": command, "cwd": str(resolved_cwd)}
        try:
            from osia.deletion_plan import DeletionPreflightKind, prepare_deletion_call

            deletion = prepare_deletion_call("run_command", arguments)
            if deletion.kind is not DeletionPreflightKind.NOT_DELETION:
                return None
        except Exception:
            # Native deletion cannot consume OSIA's sealed deletion plan.
            return None
        return "run_command", arguments

    def _deny_native_approval(
        self, attempt: _TurnAttempt, request_id: Any, method: str, item_id: str,
        *, outcome: str = "rejected_before_execution",
        tool_name: str = "",
        arguments: Mapping[str, Any] | None = None,
        semantic_node_id: str = "",
        semantic_postcondition: str = "",
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
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        request_id = payload.get("request_id")
        item_id = self._native_item_id(params)
        if attempt.terminal_permission_outcome:
            try:
                self._send({
                    "id": request_id,
                    "result": {"decision": "decline"},
                })
            except AppServerError:
                pass
            return
        broker_call = self._native_broker_call(attempt, method, params)
        if broker_call is None:
            self._deny_native_approval(attempt, request_id, method, item_id)
            return
        tool_name, arguments = broker_call
        strategy_decision = None
        semantic_postcondition = ""
        try:
            from osia.action_policy import (
                consume_pending_execution_permit,
                guard_tool_call,
                pending_execution_permit_matches,
            )
            from osia.capability_federation import mutation_equivalence_arguments
            from osia.tool_contracts import (
                ConfirmationPolicy,
                SideEffectClass,
                effective_contract,
                get_tool_spec,
            )

            spec = get_tool_spec(tool_name)
            if spec is None:
                raise ValueError("native_broker_tool_unavailable")
            contract = effective_contract(spec, arguments, attempt.user_request)
            effectful = contract.side_effect is not SideEffectClass.READ_ONLY
            if effectful:
                try:
                    from osia.execution_strategy import tool_preflight

                    strategy_decision = tool_preflight(
                        tool_name,
                        arguments,
                        strategy=attempt.execution_strategy,
                    )
                except Exception:
                    if attempt.execution_strategy is not None:
                        self._deny_native_approval(
                            attempt,
                            request_id,
                            method,
                            item_id,
                            outcome="semantic_preflight_unavailable",
                            tool_name=tool_name,
                            arguments=arguments,
                        )
                        return
                    strategy_decision = None
                if strategy_decision is not None:
                    node = (
                        attempt.execution_strategy.node_by_id(
                            strategy_decision.node_id,
                        )
                        if attempt.execution_strategy is not None else None
                    )
                    semantic_postcondition = (
                        node.observable_success_postcondition
                        if node is not None else ""
                    )
                    if not strategy_decision.allowed:
                        self._deny_native_approval(
                            attempt,
                            request_id,
                            method,
                            item_id,
                            outcome=(
                                "semantic_" + strategy_decision.reason_code
                            ),
                            tool_name=tool_name,
                            arguments=arguments,
                            semantic_node_id=strategy_decision.node_id,
                            semantic_postcondition=semantic_postcondition,
                        )
                        return
            store = None
            fingerprint = ""
            equivalence_fingerprint = ""
            if effectful:
                store = self._attempt_store()
                fingerprint = store.fingerprint_arguments(arguments)
                equivalence = mutation_equivalence_arguments(tool_name, arguments)
                if equivalence is not None:
                    equivalence_fingerprint = store.fingerprint_arguments(
                        equivalence,
                    )
                if store.has_dispatch_barrier(
                    conversation_id=self._safe_receipt_ref(
                        attempt.conversation_id or attempt.thread_id,
                        "conversation",
                    ),
                    turn_id=self._safe_receipt_ref(
                        attempt.osia_turn_id or attempt.turn_id or attempt.thread_id,
                        "turn",
                    ),
                    tool_name=tool_name,
                    argument_fingerprint=fingerprint,
                    equivalence_fingerprint=equivalence_fingerprint,
                ):
                    self._deny_native_approval(
                        attempt,
                        request_id,
                        method,
                        item_id,
                        outcome="duplicate_or_unknown_frozen",
                    )
                    return
            blocked = guard_tool_call(attempt.user_request, tool_name, arguments)
            if blocked is not None:
                from osia.permission_outcome import permission_outcome_from_text

                permission_outcome = permission_outcome_from_text(blocked)
                self._deny_native_approval(
                    attempt,
                    request_id,
                    method,
                    item_id,
                    outcome=(
                        f"permission_{permission_outcome.value}"
                        if permission_outcome is not None
                        else "rejected_before_execution"
                    ),
                    tool_name=tool_name,
                    arguments=arguments,
                    semantic_node_id=(
                        strategy_decision.node_id if strategy_decision else ""
                    ),
                    semantic_postcondition=semantic_postcondition,
                )
                if strategy_decision is not None:
                    from osia.execution_strategy import record_tool_outcome

                    record_tool_outcome(
                        tool_name,
                        strategy_decision,
                        outcome="failed_before_effect",
                        is_error=True,
                        reason_code=(
                            f"permission_{permission_outcome.value}"
                            if permission_outcome is not None
                            else "rejected_before_execution"
                        ),
                        result=blocked,
                        strategy=attempt.execution_strategy,
                    )
                return
            requires_permit = contract.confirmation in {
                ConfirmationPolicy.CONFIRMATION_REQUIRED,
                ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION,
            }
            has_permit = pending_execution_permit_matches(tool_name, arguments)
            if requires_permit and not has_permit:
                self._deny_native_approval(
                    attempt, request_id, method, item_id,
                    outcome="permission_internal_failure",
                    tool_name=tool_name,
                    arguments=arguments,
                )
                return
            if has_permit and not consume_pending_execution_permit(tool_name, arguments):
                self._deny_native_approval(
                    attempt, request_id, method, item_id,
                    outcome="permission_internal_failure",
                    tool_name=tool_name,
                    arguments=arguments,
                )
                return

            dispatch = _NativeDispatch(
                item_id=item_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                side_effect=contract.side_effect.value,
                idempotence=contract.idempotency.value,
                retry_policy=contract.retry.value,
                strategy_decision=strategy_decision,
                semantic_postcondition=semantic_postcondition,
            )
            if effectful:
                dispatch.equivalence_fingerprint = equivalence_fingerprint
                receipt = store.create_attempt(
                    conversation_id=self._safe_receipt_ref(
                        attempt.conversation_id or attempt.thread_id, "conversation",
                    ),
                    turn_id=self._safe_receipt_ref(
                        attempt.osia_turn_id or attempt.turn_id or attempt.thread_id,
                        "turn",
                    ),
                    request_id=self._safe_receipt_ref(
                        attempt.client_request_id or request_id,
                        "request",
                    ),
                    tool_call_id=self._safe_receipt_ref(item_id, "native_item"),
                    tool_name=tool_name,
                    provider_id="chatgpt_account",
                    model_id=attempt.model_id or "unknown",
                    turn_context={},
                    argument_fingerprint=fingerprint,
                    equivalence_fingerprint=dispatch.equivalence_fingerprint,
                    side_effect=contract.side_effect.value,
                    idempotence=contract.idempotency.value,
                    retry_policy=contract.retry.value,
                    postcondition_type="provider_typed_item_status",
                    final_response_message_id=attempt.final_response_message_id,
                    persistence_contract={
                        "family": "system" if tool_name == "run_command" else "file",
                        "release_after_seconds": 2.0,
                        "inactivity_timeout_seconds": 30.0,
                        "hard_timeout_seconds": 300.0,
                        "check_delays_seconds": [2.0, 5.0],
                        "safe_alternatives": [],
                        "restart_reconciliation": True,
                    },
                )
                dispatch.tool_attempt_id = str(receipt["tool_attempt_id"])
                store.mark_accepted(dispatch.tool_attempt_id)
                store.mark_running(dispatch.tool_attempt_id)
                dispatch.started = True
                attempt.native_effect_started = True
                if dispatch.equivalence_fingerprint:
                    attempt.frozen_equivalences.add(
                        dispatch.equivalence_fingerprint,
                    )
            attempt.native_dispatches[item_id] = dispatch
            self._send({"id": request_id, "result": {"decision": "accept"}})
            self._append_capability_event(
                attempt,
                source="provider_native",
                identity=(
                    "commandExecution"
                    if tool_name == "run_command" else "fileChange"
                ),
                phase="accepted",
                outcome="dispatch_started" if effectful else "read_only",
                item_id=item_id,
                tool_attempt_id=dispatch.tool_attempt_id,
                tool_name=tool_name,
                arguments=arguments,
                semantic_node_id=(
                    strategy_decision.node_id if strategy_decision else ""
                ),
                semantic_postcondition=semantic_postcondition,
            )
        except Exception:
            dispatch = attempt.native_dispatches.get(item_id)
            if dispatch is not None and dispatch.tool_attempt_id:
                try:
                    from osia.tool_attempts import ToolAttemptStatus

                    self._attempt_store().record_terminal_result(
                        dispatch.tool_attempt_id,
                        status=ToolAttemptStatus.UNKNOWN_AFTER_START,
                        terminal_outcome="outcome_unknown",
                        dispatch_state="dispatch_started",
                        reason_code="native_approval_transport_failed",
                    )
                except Exception:
                    pass
                attempt.outcome_unknown = True
                attempt.native_effect_started = True
            self._deny_native_approval(
                attempt, request_id, method, item_id,
                outcome=(
                    "outcome_unknown" if attempt.outcome_unknown
                    else "rejected_before_execution"
                ),
            )

    def _native_dynamic_call_is_frozen(
        self, attempt: _TurnAttempt, native_tool_call: dict,
    ) -> bool:
        try:
            from osia.capability_federation import mutation_equivalence_arguments
            from osia.tool_contracts import SideEffectClass, effective_contract, get_tool_spec

            tool_name = str(native_tool_call.get("tool") or "")
            arguments = native_tool_call.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            spec = get_tool_spec(tool_name)
            if spec is None:
                return True
            contract = effective_contract(spec, arguments, attempt.user_request)
            if contract.side_effect is SideEffectClass.READ_ONLY:
                return False
            if attempt.outcome_unknown:
                return True
            equivalence = mutation_equivalence_arguments(tool_name, arguments)
            if equivalence is None:
                return False
            fingerprint = self._attempt_store().fingerprint_arguments(equivalence)
            return fingerprint in attempt.frozen_equivalences
        except Exception:
            return bool(attempt.native_effect_started)

    def _complete_native_item(self, attempt: _TurnAttempt, item: dict) -> None:
        item_id = str(item.get("id") or "")
        item_type = str(item.get("type") or "")
        status = str(item.get("status") or "")
        if status != "inProgress":
            if item_id in attempt.terminal_native_items:
                return
            attempt.terminal_native_items.add(item_id)
        dispatch = attempt.native_dispatches.get(item_id)
        if dispatch is None:
            if status in {"declined", "failed"}:
                self._append_capability_event(
                    attempt,
                    source="provider_native",
                    identity=item_type,
                    phase="completed",
                    outcome=(
                        "cancelled_proven" if status == "declined"
                        else "failed_before_effect"
                    ),
                    item_id=item_id,
                )
                return
            if item_type == "commandExecution" and status == "completed":
                try:
                    from osia.tool_contracts import SideEffectClass, effective_contract, get_tool_spec

                    spec = get_tool_spec("run_command")
                    contract = effective_contract(
                        spec, {"command": str(item.get("command") or "")},
                        attempt.user_request,
                    )
                    if (
                        contract.side_effect is SideEffectClass.READ_ONLY
                        and isinstance(item.get("exitCode"), int)
                        and not isinstance(item.get("exitCode"), bool)
                        and item.get("exitCode") == 0
                    ):
                        self._append_capability_event(
                            attempt,
                            source="provider_native",
                            identity=item_type,
                            phase="completed",
                            outcome="success_verified",
                            item_id=item_id,
                        )
                        return
                except Exception:
                    pass
            attempt.outcome_unknown = True
            attempt.native_effect_started = True
            self._mark_unsafe(
                {"threadId": attempt.thread_id, "turnId": attempt.turn_id},
                f"unbrokered_native_item:{item_type or 'unknown'}",
            )
            return
        if dispatch.terminal or status == "inProgress":
            return
        dispatch.terminal = True
        lifecycle_matches = True
        if dispatch.tool_name == "run_command":
            try:
                completed_cwd = Path(
                    str(item.get("cwd") or self.workspace_dir),
                ).resolve(strict=False)
            except (OSError, ValueError):
                lifecycle_matches = False
            else:
                lifecycle_matches = bool(
                    item.get("command") == dispatch.arguments.get("command")
                    and str(completed_cwd) == dispatch.arguments.get("cwd")
                )
        elif dispatch.tool_name == "write_file":
            completed_call = self._file_change_broker_call(attempt, item_id)
            lifecycle_matches = bool(
                completed_call is not None
                and completed_call[0] == dispatch.tool_name
                and completed_call[1] == dispatch.arguments
            )
        if not lifecycle_matches:
            status = "lifecycleMismatch"
        if status == "completed" and (
            dispatch.tool_name != "run_command"
            or (
                isinstance(item.get("exitCode"), int)
                and not isinstance(item.get("exitCode"), bool)
                and item.get("exitCode") == 0
            )
        ):
            terminal_status = "succeeded"
            outcome = "success_verified"
            dispatch_state = "effect_applied"
            reason = "provider_typed_completion"
        elif (
            status == "completed"
            and isinstance(item.get("exitCode"), int)
            and not isinstance(item.get("exitCode"), bool)
        ):
            terminal_status = "failed_after_effect"
            outcome = "failed_after_effect"
            dispatch_state = "effect_applied"
            reason = "provider_nonzero_exit"
        elif status == "declined":
            terminal_status = "cancelled_proven"
            outcome = "cancelled_proven"
            dispatch_state = "cancelled_before_effect"
            reason = "provider_declined_before_effect"
        else:
            terminal_status = "unknown_after_start"
            outcome = "outcome_unknown"
            dispatch_state = "dispatch_started"
            reason = "provider_failed_after_start"
            attempt.outcome_unknown = True
        if dispatch.tool_attempt_id:
            try:
                from osia.tool_attempts import ToolAttemptStatus

                self._attempt_store().record_terminal_result(
                    dispatch.tool_attempt_id,
                    status=ToolAttemptStatus(terminal_status),
                    terminal_outcome=outcome,
                    dispatch_state=dispatch_state,
                    reason_code=reason,
                )
            except Exception:
                attempt.outcome_unknown = True
                outcome = "outcome_unknown"
        if dispatch.strategy_decision is not None:
            try:
                from osia.execution_strategy import record_tool_outcome

                record_tool_outcome(
                    dispatch.tool_name,
                    dispatch.strategy_decision,
                    outcome=outcome,
                    is_error=(outcome != "success_verified"),
                    reason_code=reason,
                    result={
                        "provider_native_item": item_id,
                        "status": status,
                        "outcome": outcome,
                    },
                    strategy=attempt.execution_strategy,
                )
            except Exception:
                pass
        self._append_capability_event(
            attempt,
            source="provider_native",
            identity=item_type,
            phase="completed",
            outcome=outcome,
            item_id=item_id,
            tool_attempt_id=dispatch.tool_attempt_id,
            tool_name=dispatch.tool_name,
            arguments=dispatch.arguments,
            semantic_node_id=(
                dispatch.strategy_decision.node_id
                if dispatch.strategy_decision is not None else ""
            ),
            semantic_postcondition=dispatch.semantic_postcondition,
        )

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
            attempt.events.put(("native_approval", {
                "method": method,
                "params": deepcopy(params),
                "request_id": request_id,
            }))
            return
        elif method in {"execCommandApproval", "applyPatchApproval"}:
            result = {"decision": "abort"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "cancel", "content": None}
        elif method == "item/tool/call":
            native_tool_call = _exact_canonical_native_call(params)
            attempt = self._attempt_for(params)
            frozen = bool(
                native_tool_call is not None
                and attempt is not None
                and self._native_dynamic_call_is_frozen(
                    attempt, native_tool_call,
                )
            )
            result = {
                "contentItems": [{
                    "type": "inputText",
                    "text": (
                        "The equivalent action is already committed and must "
                        "not be repeated."
                        if frozen else
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
        if method == "item/tool/call" and native_tool_call is not None and not frozen:
            self._mark_unsafe(
                params,
                f"canonical_tool:{native_tool_call['tool']}",
                native_tool_call=native_tool_call,
            )
        elif method != "item/tool/call" or native_tool_call is None:
            self._mark_unsafe(params, "native_request")

    def _dispatch_notification(self, method: str, params: dict) -> None:
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

        attempt = self._attempt_for(params)
        if method == "item/agentMessage/delta":
            if attempt is not None and not attempt.native_action:
                delta = params.get("delta")
                if isinstance(delta, str) and delta:
                    attempt.output_started = True
                    attempt.text_parts.append(delta)
                    attempt.events.put(("delta", delta))
            return

        if method in {"item/started", "item/completed"}:
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            item_type = str(item.get("type") or "")
            item_id = str(item.get("id") or "")
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
            if item_type in {"commandExecution", "fileChange"}:
                if attempt is not None and item_id:
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
                    attempt.native_items[item_id] = deepcopy(item)
                    if method == "item/started":
                        selection_outcome = "pending_approval"
                        if item_type == "commandExecution":
                            try:
                                from osia.tool_contracts import (
                                    SideEffectClass,
                                    effective_contract,
                                    get_tool_spec,
                                )

                                command_spec = get_tool_spec("run_command")
                                command_contract = effective_contract(
                                    command_spec,
                                    {"command": str(item.get("command") or "")},
                                    attempt.user_request,
                                )
                                if (
                                    command_contract.side_effect
                                    is SideEffectClass.READ_ONLY
                                ):
                                    selection_outcome = "running_read_only"
                            except Exception:
                                pass
                        self._append_capability_event(
                            attempt,
                            source="provider_native",
                            identity=item_type,
                            phase="selected",
                            outcome=selection_outcome,
                            item_id=item_id,
                        )
                    else:
                        self._complete_native_item(attempt, item)
                return
            if item_type == "dynamicToolCall":
                lifecycle_params = {**params, **item}
                if not lifecycle_params.get("callId") and isinstance(item.get("id"), str):
                    lifecycle_params["callId"] = item["id"]
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
                attempt.events.put(
                    (
                        "done",
                        {
                            "status": str(turn.get("status") or "failed"),
                            "turnId": str(turn.get("id")) if turn.get("id") else attempt.turn_id,
                            "errorCategory": self._turn_error_category(turn),
                        },
                    )
                )
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
        self._clear_login_state(login_id)
        if cancelled:
            raise AppServerError("ERROR: ChatGPT login was cancelled.", category="login_cancelled")
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
                if self._rate_limits_pending_update:
                    safe = merge_rate_limit_payload(
                        safe, self._rate_limits_pending_update
                    )
                self._rate_limits_cache = safe
                self._rate_limits_pending_update = {}
                return deepcopy(safe)

    def start_or_resume_conversation(
        self,
        *,
        thread_id: str | None,
        model: str,
        base_instructions: str,
        developer_instructions: str,
        dynamic_tools=None,
    ) -> str:
        if thread_id:
            try:
                result = self._request(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "model": model,
                        "cwd": str(self.workspace_dir),
                        "approvalPolicy": "on-request",
                        "sandbox": "read-only",
                    },
                )
                thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
                resumed = str(thread.get("id") or "")
                if resumed:
                    if result.get("instructionSources"):
                        raise AppServerError(
                            "ERROR: Codex inherited external instruction files.",
                            category="isolation_failed",
                        )
                    return resumed
            except AppServerError:
                pass

        result = self._request(
            "thread/start",
            {
                "model": model,
                "cwd": str(self.workspace_dir),
                "approvalPolicy": "on-request",
                "sandbox": "read-only",
                "ephemeral": True,
                "serviceName": "osia_pc",
                "baseInstructions": base_instructions,
                "developerInstructions": developer_instructions,
                "dynamicTools": (
                    _canonical_native_tools()
                    if dynamic_tools is None else list(dynamic_tools)
                ),
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
        timeout: float = 300.0,
        output_schema: dict | None = None,
    ) -> TurnResult:
        turn_content = content if content is not None else text
        if turn_content is None:
            raise AppServerError(
                "ERROR: no ChatGPT account turn input was provided.",
                category="empty_input",
            )
        staged_images: list[Path] = []
        attempt = _TurnAttempt(
            thread_id=thread_id,
            conversation_id=str(conversation_id or thread_id),
            osia_turn_id=str(osia_turn_id or ""),
            client_request_id=str(client_request_id or ""),
            final_response_message_id=str(final_response_message_id or ""),
            model_id=str(model or "unknown"),
            user_request=_latest_user_text(turn_content),
        )
        try:
            from osia.execution_strategy import current_execution_strategy

            attempt.execution_strategy = current_execution_strategy()
        except Exception:
            attempt.execution_strategy = None
        with self._state_lock:
            if thread_id in self._attempts:
                raise AppServerError(
                    "ERROR: a ChatGPT account turn is already active for this conversation.",
                    allow_fallback=False,
                    category="turn_busy",
                )
            self._attempts[thread_id] = attempt
        try:
            app_input, staged_images = self._stage_turn_input(turn_content)
            params = {
                "threadId": thread_id,
                "input": app_input,
                "model": model,
                "approvalPolicy": "on-request",
                "cwd": str(self.workspace_dir),
            }
            if effort:
                params["effort"] = effort
            if output_schema is not None:
                if not isinstance(output_schema, dict):
                    raise AppServerError(
                        "ERROR: the structured output schema is invalid.",
                        category="invalid_input",
                    )
                params["outputSchema"] = deepcopy(output_schema)
            response = self._request("turn/start", params)
            turn = response.get("turn") if isinstance(response.get("turn"), dict) else {}
            if turn.get("id"):
                attempt.turn_id = str(turn["id"])

            deadline = time.monotonic() + timeout
            provider_error = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.cancel_turn(thread_id=thread_id, turn_id=attempt.turn_id)
                    raise AppServerError(
                        "ERROR: the ChatGPT account turn timed out.",
                        output_started=attempt.output_started,
                        native_action=attempt.native_effect_started,
                        allow_fallback=(
                            not attempt.output_started
                            and not attempt.native_effect_started
                        ),
                        category="turn_timeout",
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
                elif kind == "provider_error":
                    provider_error = str(payload)
                elif kind == "native_approval" and isinstance(payload, dict):
                    approval_started = time.monotonic()
                    self._resolve_native_approval(attempt, payload)
                    deadline += max(0.0, time.monotonic() - approval_started)
                elif kind == "error" and isinstance(payload, AppServerError):
                    payload.output_started = attempt.output_started
                    payload.native_action = (
                        payload.native_action or attempt.native_effect_started
                    )
                    payload.allow_fallback = bool(
                        not attempt.output_started
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
                    for dispatch in tuple(attempt.native_dispatches.values()):
                        if dispatch.started and not dispatch.terminal:
                            self._complete_native_item(attempt, {
                                "id": dispatch.item_id,
                                "type": (
                                    "commandExecution"
                                    if dispatch.tool_name == "run_command"
                                    else "fileChange"
                                ),
                                "status": "failed",
                            })
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
                    if status != "completed":
                        cancelled = status == "interrupted"
                        raise AppServerError(
                            "ERROR: the ChatGPT account turn was cancelled."
                            if cancelled
                            else "ERROR: the ChatGPT account turn failed before completion.",
                            output_started=attempt.output_started,
                            native_action=attempt.native_effect_started,
                            allow_fallback=(
                                not attempt.output_started
                                and not cancelled
                                and not attempt.native_effect_started
                            ),
                            category=provider_error or str(payload.get("errorCategory") or status),
                        )
                    result = TurnResult(
                        text="".join(attempt.text_parts),
                        thread_id=thread_id,
                        turn_id=str(turn_id) if turn_id else None,
                        status=status,
                        output_started=attempt.output_started,
                        capability_events=tuple(attempt.capability_events),
                    )
                    return result
        except AppServerError as exc:
            exc.capability_events = tuple(attempt.capability_events)
            exc.native_turn_id = attempt.turn_id
            raise
        finally:
            with self._state_lock:
                self._attempts.pop(thread_id, None)
            for path in staged_images:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _stage_turn_input(
        self,
        content: str | list[dict],
    ) -> tuple[list[dict], list[Path]]:
        """Convert verified inline images to native, turn-scoped localImage items."""

        if isinstance(content, str):
            return [{"type": "text", "text": content}], []
        if not isinstance(content, list):
            raise AppServerError(
                "ERROR: the ChatGPT account turn input is invalid.",
                category="invalid_input",
            )

        from osia.attachments import MAX_ATTACHMENT_BYTES
        from osia.image_inputs import ImageInputError, decode_image_data_url

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        staged: list[Path] = []
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
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix="vision-",
                    suffix=suffixes[decoded.mime],
                    dir=self.temp_dir,
                    delete=False,
                ) as handle:
                    handle.write(decoded.data)
                    path = Path(handle.name)
                staged.append(path)
                items.append({"type": "localImage", "path": str(path)})
            if not items:
                raise AppServerError(
                    "ERROR: the ChatGPT account turn input is empty.",
                    category="empty_input",
                )
            return items, staged
        except Exception:
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
            process = self._process
            if process is None:
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
