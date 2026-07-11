"""Small fail-closed client for the stable Codex App Server JSONL protocol.

The process is deliberately isolated from the normal Codex installation and
from OSIA's repository/configuration.  Only agent-message text is accepted as
provider output; every native action item or approval request cancels the turn.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


SUPPORTED_MIN_VERSION = (0, 144, 1)
SUPPORTED_MAX_VERSION = (0, 145, 0)
CLIENT_VERSION = "1.0.0"

_SAFE_CONFIG = """# Managed by OSIA for the isolated ChatGPT-account provider.
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

_SAFE_ITEM_TYPES = frozenset(
    {"agentMessage", "userMessage", "reasoning", "plan", "contextCompaction"}
)
_FORBIDDEN_ITEM_TYPES = frozenset(
    {
        "collabAgentToolCall",
        "commandExecution",
        "dynamicToolCall",
        "fileChange",
        "hookPrompt",
        "imageGeneration",
        "imageView",
        "mcpToolCall",
        "sleep",
        "subAgentActivity",
        "webSearch",
    }
)
_SAFE_NOTIFICATIONS = frozenset(
    {
        "account/login/completed",
        "account/rateLimits/updated",
        "account/updated",
        "error",
        "item/agentMessage/delta",
        "item/completed",
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
        "turn/moderationMetadata",
        "turn/plan/updated",
        "turn/started",
        "warning",
    }
)


class AppServerError(RuntimeError):
    """Sanitized App Server failure safe to cross the provider boundary."""

    def __init__(
        self,
        message: str,
        *,
        output_started: bool = False,
        native_action: bool = False,
        allow_fallback: bool = True,
        category: str = "app_server",
    ):
        super().__init__(message)
        self.output_started = bool(output_started)
        self.native_action = bool(native_action)
        self.allow_fallback = bool(allow_fallback)
        self.category = category


class RuntimeUnavailableError(AppServerError):
    """The required native Codex runtime is missing or incompatible."""


@dataclass(frozen=True)
class TurnResult:
    text: str
    thread_id: str
    turn_id: str | None
    status: str
    output_started: bool = False


@dataclass
class _PendingRequest:
    response: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))


@dataclass
class _TurnAttempt:
    thread_id: str
    events: queue.Queue = field(default_factory=queue.Queue)
    turn_id: str | None = None
    text_parts: list[str] = field(default_factory=list)
    output_started: bool = False
    native_action: bool = False
    unsafe_category: str | None = None
    interrupt_sent: bool = False


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


def _native_candidates() -> list[Path]:
    candidates: list[Path] = []
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
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            **kwargs,
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
        for candidate in _native_candidates():
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
                if config.read_text(encoding="utf-8") != _SAFE_CONFIG:
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
                kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                kwargs["start_new_session"] = True
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
                        }
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
                raise AppServerError(
                    "ERROR: the Codex App Server rejected the request.",
                    category="request_rejected",
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

    def _mark_unsafe(self, params: dict, category: str) -> None:
        attempt = self._attempt_for(params)
        if attempt is None or attempt.native_action:
            return
        attempt.native_action = True
        attempt.unsafe_category = category
        if params.get("turnId"):
            attempt.turn_id = str(params["turnId"])
        attempt.events.put(("unsafe", category))
        if not attempt.interrupt_sent and attempt.turn_id:
            attempt.interrupt_sent = True
            self._send_request_no_wait(
                "turn/interrupt", {"threadId": attempt.thread_id, "turnId": attempt.turn_id}
            )

    def _handle_server_request(self, message: dict) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        request_id = message.get("id")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            result = {"decision": "cancel"}
        elif method in {"execCommandApproval", "applyPatchApproval"}:
            result = {"decision": "abort"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "cancel", "content": None}
        elif method == "item/tool/call":
            result = {"contentItems": [], "success": False}
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
            self._mark_unsafe(params, "native_diff")
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
        with self._login_lock:
            waiter = self._login_events.get(str(login_id))
        if waiter is None:
            raise AppServerError("ERROR: no ChatGPT login is active.", category="login_missing")
        if not waiter[0].wait(timeout):
            raise AppServerError("ERROR: ChatGPT login timed out.", category="login_timeout")
        success = bool(waiter[1].get("success"))
        cancelled = bool(waiter[1].get("cancelled"))
        with self._login_lock:
            self._login_events.pop(str(login_id), None)
            if self._active_login and self._active_login.get("loginId") == str(login_id):
                self._active_login = None
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
        self._request("account/login/cancel", {"loginId": login_id})
        with self._login_lock:
            waiter = self._login_events.pop(login_id, None)
            self._active_login = None
        if waiter:
            waiter[1].update({"success": False, "cancelled": True})
            waiter[0].set()

    def logout(self) -> None:
        self._request("account/logout", {})
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
                modalities = item.get("inputModalities") or ["text", "image"]
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
        result = self._request("account/rateLimits/read", {})
        # Rate-limit payloads contain no credentials, but keep the interface
        # deliberately narrow and let the UI summarize only stable fields.
        return {"rateLimits": result.get("rateLimits") or result.get("data") or result}

    def start_or_resume_conversation(
        self,
        *,
        thread_id: str | None,
        model: str,
        base_instructions: str,
        developer_instructions: str,
    ) -> str:
        if thread_id:
            try:
                result = self._request(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "model": model,
                        "cwd": str(self.workspace_dir),
                        "approvalPolicy": "never",
                        "sandbox": "readOnly",
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
                "approvalPolicy": "never",
                "sandbox": "readOnly",
                "ephemeral": True,
                "serviceName": "osia_pc",
                "baseInstructions": base_instructions,
                "developerInstructions": developer_instructions,
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
        text: str,
        model: str,
        effort: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        timeout: float = 300.0,
    ) -> TurnResult:
        attempt = _TurnAttempt(thread_id=thread_id)
        with self._state_lock:
            if thread_id in self._attempts:
                raise AppServerError(
                    "ERROR: a ChatGPT account turn is already active for this conversation.",
                    allow_fallback=False,
                    category="turn_busy",
                )
            self._attempts[thread_id] = attempt
        try:
            params = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "model": model,
                "approvalPolicy": "never",
                "cwd": str(self.workspace_dir),
            }
            if effort:
                params["effort"] = effort
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
                        allow_fallback=not attempt.output_started,
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
                            allow_fallback=not attempt.output_started,
                            category="process_crash",
                        )
                    continue
                if kind == "delta":
                    if on_delta is not None:
                        on_delta(str(payload))
                elif kind == "provider_error":
                    provider_error = str(payload)
                elif kind == "error" and isinstance(payload, AppServerError):
                    payload.output_started = attempt.output_started
                    payload.allow_fallback = not attempt.output_started
                    raise payload
                elif kind == "unsafe":
                    raise AppServerError(
                        "ERROR: ChatGPT attempted a native Codex action; OSIA cancelled the turn.",
                        output_started=attempt.output_started,
                        native_action=True,
                        allow_fallback=False,
                        category=str(payload),
                    )
                elif kind == "done":
                    status = str(payload.get("status") or "failed")
                    turn_id = payload.get("turnId") or attempt.turn_id
                    if attempt.native_action:
                        raise AppServerError(
                            "ERROR: ChatGPT attempted a native Codex action; OSIA cancelled the turn.",
                            output_started=attempt.output_started,
                            native_action=True,
                            allow_fallback=False,
                            category=attempt.unsafe_category or "native_action",
                        )
                    if status != "completed":
                        cancelled = status == "interrupted"
                        raise AppServerError(
                            "ERROR: the ChatGPT account turn was cancelled."
                            if cancelled
                            else "ERROR: the ChatGPT account turn failed before completion.",
                            output_started=attempt.output_started,
                            allow_fallback=not attempt.output_started and not cancelled,
                            category=provider_error or str(payload.get("errorCategory") or status),
                        )
                    return TurnResult(
                        text="".join(attempt.text_parts),
                        thread_id=thread_id,
                        turn_id=str(turn_id) if turn_id else None,
                        status=status,
                        output_started=attempt.output_started,
                    )
        finally:
            with self._state_lock:
                self._attempts.pop(thread_id, None)

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
