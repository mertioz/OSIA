"""Same-user local broker for Personal Browser Native Messaging clients."""

from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import struct
import sys
import threading
import time
from typing import Any, Mapping

from osia.personal_browser.protocol import (
    MAX_ENVELOPE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_message,
    encode_message,
    request_message,
    validate_target,
)
from osia.tools.browser_state import (
    BrowserStateRef,
    DispatchState,
    ReceiptOutcome,
    action_result,
    make_receipt,
)


@dataclass
class _PendingRequest:
    connection_id: str
    target_handle: str
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    dispatched: bool = False
    progress_callback: Any = None


@dataclass
class _Client:
    connection_id: str
    install_instance_id: str
    browser_session_id: str
    browser_family: str
    caller_origin: str
    capabilities: tuple[str, ...]
    handle: Any
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)

    def send(self, message: Mapping[str, Any]) -> None:
        payload = encode_message(message)
        frame = struct.pack("<I", len(payload)) + payload
        import win32file

        with self.write_lock:
            win32file.WriteFile(self.handle, frame)


class PersonalBrowserBroker:
    """One process-local broker; it never selects Personal Browser implicitly."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: dict[str, _Client] = {}
        self._pending: dict[str, _PendingRequest] = {}
        self._progress_callback = None
        self._closed = False
        self._server_thread: threading.Thread | None = None
        self._control_thread: threading.Thread | None = None
        self._owner_semaphore = None
        self._owns_listener = False
        if sys.platform == "win32":
            self._initialize_windows_owner()

    def _initialize_windows_owner(self) -> None:
        import win32event

        semaphore = win32event.CreateSemaphore(None, 1, 1, _owner_mutex_name())
        if win32event.WaitForSingleObject(semaphore, 0) != win32event.WAIT_OBJECT_0:
            semaphore.Close()
            return
        self._owner_semaphore = semaphore
        self._owns_listener = True
        self._start_windows_server()

    def _try_promote_to_owner(self) -> bool:
        if self._owns_listener:
            return True
        if self._closed:
            return False
        import win32event

        semaphore = win32event.CreateSemaphore(None, 1, 1, _owner_mutex_name())
        if win32event.WaitForSingleObject(semaphore, 0) != win32event.WAIT_OBJECT_0:
            semaphore.Close()
            return False
        self._owner_semaphore = semaphore
        self._owns_listener = True
        self._start_windows_server()
        return True

    def _start_windows_server(self) -> None:
        if self._server_thread is not None:
            return
        self._server_thread = threading.Thread(
            target=self._windows_accept_loop,
            name="osia-personal-browser-broker",
            daemon=True,
        )
        self._server_thread.start()
        self._control_thread = threading.Thread(
            target=self._windows_control_accept_loop,
            name="osia-personal-browser-control",
            daemon=True,
        )
        self._control_thread.start()

    def _windows_accept_loop(self) -> None:
        import pywintypes
        import win32pipe

        while not self._closed:
            handle = None
            try:
                handle = win32pipe.CreateNamedPipe(
                    _pipe_name(),
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    MAX_ENVELOPE_BYTES + 4,
                    MAX_ENVELOPE_BYTES + 4,
                    0,
                    _same_user_security_attributes(),
                )
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as exc:
                    if exc.winerror != 535:  # ERROR_PIPE_CONNECTED
                        raise
                threading.Thread(
                    target=self._serve_windows_client,
                    args=(handle,),
                    name="osia-personal-browser-client",
                    daemon=True,
                ).start()
                handle = None
            except Exception:
                if handle is not None:
                    _close_pipe(handle)
                if not self._closed:
                    time.sleep(0.1)

    def _windows_control_accept_loop(self) -> None:
        import pywintypes
        import win32pipe

        while not self._closed and self._owns_listener:
            handle = None
            try:
                handle = win32pipe.CreateNamedPipe(
                    _control_pipe_name(),
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_BYTE
                    | win32pipe.PIPE_READMODE_BYTE
                    | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    MAX_ENVELOPE_BYTES + 4,
                    MAX_ENVELOPE_BYTES + 4,
                    0,
                    _same_user_security_attributes(),
                )
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as exc:
                    if exc.winerror != 535:  # ERROR_PIPE_CONNECTED
                        raise
                threading.Thread(
                    target=self._serve_control_client,
                    args=(handle,),
                    name="osia-personal-browser-control-client",
                    daemon=True,
                ).start()
                handle = None
            except Exception:
                if handle is not None:
                    _close_pipe(handle)
                if not self._closed:
                    time.sleep(0.1)

    def _serve_control_client(self, handle) -> None:
        import pywintypes

        try:
            message = _read_pipe_message(handle)
            if message is None or message.get("message_type") != "control_request":
                return
            if not _client_is_current_user(handle):
                return
            def progress(payload: Mapping[str, Any]) -> None:
                if payload.get("phase") != "waiting":
                    return
                _write_pipe_message(handle, {
                    "protocol_version": PROTOCOL_VERSION,
                    "message_type": "control_progress",
                    "request_id": message["request_id"],
                    "phase": "waiting",
                })

            result = self._dispatch_control_request(message, progress_callback=progress)
            _write_pipe_message(handle, {
                "protocol_version": PROTOCOL_VERSION,
                "message_type": "control_response",
                "request_id": message["request_id"],
                "status": "ok",
                "error": None,
                "result": result,
            })
        except (OSError, pywintypes.error, ProtocolError, KeyError, ValueError):
            pass
        finally:
            _close_pipe(handle)

    def _dispatch_control_request(
        self, message: Mapping[str, Any], *, progress_callback=None,
    ) -> dict[str, Any]:
        control_operation = message.get("control_operation")
        if control_operation == "status":
            return self._status_local()
        if control_operation == "revoke":
            return self._revoke_all_local()
        if control_operation == "invoke":
            from osia.tools.browser_controller import browser_turn_scope

            with browser_turn_scope("personal") as binding:
                _apply_turn_binding(binding, message.get("turn_binding"))
                result = self._invoke_local(
                    str(message.get("browser_operation") or ""),
                    dict(message.get("arguments") or {}),
                    progress_callback=progress_callback,
                )
                return {
                    "control_result": result,
                    "turn_binding": _turn_binding_payload(binding),
                }
        return _error("unknown_operation")

    def _serve_windows_client(self, handle) -> None:
        import pywintypes

        connection_id = ""
        client = None
        try:
            hello = _read_pipe_message(handle)
            if hello is None or hello.get("message_type") != "hello":
                return
            if not _client_is_current_user(handle):
                return
            from osia.personal_browser.registration import origin_allowed

            if not origin_allowed(str(hello.get("caller_origin") or "")):
                return
            connection_id = str(hello["connection_id"])
            client = _Client(
                connection_id=connection_id,
                install_instance_id=str(hello["install_instance_id"]),
                browser_session_id=str(hello["browser_session_id"]),
                browser_family=str(hello["browser_family"]),
                caller_origin=str(hello["caller_origin"]),
                capabilities=tuple(map(str, hello["capabilities"])),
                handle=handle,
            )
            with self._lock:
                if connection_id in self._clients:
                    return
                self._clients[connection_id] = client
            while not self._closed:
                if not _pipe_has_data(handle):
                    time.sleep(0.01)
                    continue
                message = _read_pipe_message(handle)
                if message is None:
                    break
                if str(message.get("connection_id") or "") != connection_id:
                    break
                self._handle_client_message(client, message)
        except (OSError, pywintypes.error, ProtocolError, KeyError, ValueError):
            pass
        finally:
            with self._lock:
                if self._clients.get(connection_id) is client:
                    self._clients.pop(connection_id, None)
                for pending in self._pending.values():
                    if pending.connection_id == connection_id:
                        pending.event.set()
            _close_pipe(handle)

    def _handle_client_message(self, client: _Client, message: dict[str, Any]) -> None:
        kind = message.get("message_type")
        if kind == "target_update":
            target = validate_target(message.get("target"))
            if (
                target["install_instance_id"] != client.install_instance_id
                or target["browser_session_id"] != client.browser_session_id
                or target["browser_family"] != client.browser_family
            ):
                raise ProtocolError("target_identity_mismatch")
            with self._lock:
                if target["authorized"]:
                    client.targets[target["target_handle"]] = target
                else:
                    client.targets.pop(target["target_handle"], None)
            return
        request_id = str(message.get("request_id") or "")
        with self._lock:
            pending = self._pending.get(request_id)
            if (
                pending is None
                or pending.connection_id != client.connection_id
                or (
                    kind == "dispatch_started"
                    and message.get("target_handle") != pending.target_handle
                )
            ):
                return
            if kind == "dispatch_started":
                pending.dispatched = True
                callback = pending.progress_callback or self._progress_callback
                if callable(callback):
                    try:
                        callback({"phase": "waiting"})
                    except Exception:
                        pass
            elif kind == "response":
                pending.response = message
                pending.event.set()

    def set_progress_callback(self, callback) -> None:
        self._progress_callback = callback

    def _authorized_targets(self) -> list[tuple[_Client, dict[str, Any]]]:
        with self._lock:
            values = [
                (client, dict(target))
                for client in self._clients.values()
                for target in client.targets.values()
                if target.get("authorized") is True
            ]
        return values

    def _select_target(self) -> tuple[_Client | None, dict[str, Any] | None, str]:
        from osia.config import load_settings
        from osia.tools.browser_controller import current_browser_turn_binding

        settings = load_settings()
        if settings.get("personal_browser_enabled") is not True:
            return None, None, "setup_required"
        binding = current_browser_turn_binding()
        targets = self._authorized_targets()
        if binding.personal_connection_id:
            for client, target in targets:
                if (
                    client.connection_id == binding.personal_connection_id
                    and client.browser_session_id == binding.personal_session_id
                    and target["target_handle"] == binding.personal_target_handle
                ):
                    return client, target, ""
            return None, None, "authorization_revoked"
        preference = str(settings.get("personal_browser_preference") or "automatic").lower()
        if preference in {"chrome", "edge", "brave"}:
            targets = [item for item in targets if item[0].browser_family == preference]
        hint = str(settings.get("personal_browser_instance_hint") or "")
        if hint:
            hinted = [
                item for item in targets
                if item[0].install_instance_id == hint or item[1]["target_handle"] == hint
            ]
            if not hinted:
                return None, None, "authorization_revoked"
            targets = hinted
        if not targets:
            return None, None, "no_authorized_tab"
        if len(targets) != 1:
            return None, None, "multiple_sessions_selection_required"
        client, target = targets[0]
        binding.personal_connection_id = client.connection_id
        binding.personal_session_id = client.browser_session_id
        binding.personal_target_handle = str(target["target_handle"])
        return client, target, ""

    def _control_request(
        self, control_operation: str, *, browser_operation: str | None = None,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        turn_binding = None
        if control_operation == "invoke":
            from osia.tools.browser_controller import current_browser_turn_binding

            turn_binding = _turn_binding_payload(current_browser_turn_binding())
        if self._try_promote_to_owner():
            result = self._dispatch_control_request({
                "control_operation": control_operation,
                "browser_operation": browser_operation,
                "arguments": dict(arguments or {}),
                "turn_binding": turn_binding,
            })
            return _consume_control_result(control_operation, result)
        import win32con
        import win32file
        import win32pipe
        import pywintypes

        deadline = time.monotonic() + 5.0
        handle = None
        while handle is None:
            try:
                handle = win32file.CreateFile(
                    _control_pipe_name(),
                    win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                    0, None, win32con.OPEN_EXISTING, 0, None,
                )
            except Exception:
                if time.monotonic() >= deadline:
                    return _error("disconnected")
                try:
                    win32pipe.WaitNamedPipe(_control_pipe_name(), 250)
                except Exception:
                    time.sleep(0.05)
        request_id = "control:" + secrets.token_urlsafe(24)
        try:
            _write_pipe_message(handle, {
                "protocol_version": PROTOCOL_VERSION,
                "message_type": "control_request",
                "request_id": request_id,
                "control_operation": control_operation,
                "browser_operation": browser_operation,
                "arguments": dict(arguments or {}),
                "turn_binding": turn_binding,
            })
            while True:
                response = _read_pipe_message(handle)
                if (
                    isinstance(response, Mapping)
                    and response.get("message_type") == "control_progress"
                    and response.get("request_id") == request_id
                ):
                    callback = self._progress_callback
                    if callable(callback):
                        try:
                            callback({"phase": "waiting"})
                        except Exception:
                            pass
                    continue
                break
        except (OSError, pywintypes.error, ProtocolError):
            return _error("disconnected")
        finally:
            _close_pipe(handle)
        if (
            not isinstance(response, Mapping)
            or response.get("message_type") != "control_response"
            or response.get("request_id") != request_id
        ):
            return _error("malformed_message")
        result = response.get("result")
        if not isinstance(result, Mapping):
            return _error("malformed_message")
        return _consume_control_result(control_operation, dict(result))

    def invoke(self, operation: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if sys.platform == "win32" and not self._owns_listener:
            return self._control_request(
                "invoke", browser_operation=operation, arguments=arguments,
            )
        return self._invoke_local(operation, arguments)

    def _invoke_local(
        self, operation: str, arguments: Mapping[str, Any], *, progress_callback=None,
    ) -> dict[str, Any]:
        client, target, error = self._select_target()
        if client is None or target is None:
            return _error(error or "disconnected")
        if operation != "revoke" and operation not in client.capabilities:
            return _error("capability_unavailable")
        if _wrong_personal_reference(arguments, target):
            return _error("cross_backend_reference")
        timeout = _operation_timeout(operation, arguments)
        request_id = "request:" + secrets.token_urlsafe(24)
        pending = _PendingRequest(
            connection_id=client.connection_id,
            target_handle=str(target["target_handle"]),
            progress_callback=progress_callback,
        )
        message = request_message(
            request_id=request_id,
            connection_id=client.connection_id,
            target_handle=str(target["target_handle"]),
            operation=operation,
            parameters=dict(arguments),
            timeout_seconds=timeout,
        )
        with self._lock:
            self._pending[request_id] = pending
        try:
            client.send(message)
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            return _effect_error("disconnected", arguments, dispatched=False)
        completed = pending.event.wait(timeout)
        with self._lock:
            self._pending.pop(request_id, None)
        if not completed or pending.response is None:
            try:
                client.send({
                    "protocol_version": PROTOCOL_VERSION,
                    "message_type": "cancel",
                    "request_id": request_id,
                    "connection_id": client.connection_id,
                })
            except Exception:
                pass
            return _effect_error(
                "outcome_unknown" if pending.dispatched else "browser_timeout",
                arguments,
                dispatched=pending.dispatched,
            )
        response = pending.response
        if response.get("status") == "ok":
            result = response.get("result")
            return dict(result) if isinstance(result, Mapping) else _error("malformed_message")
        error_obj = response.get("error")
        code = str(error_obj.get("code") or "browser_operation_failed") if isinstance(error_obj, Mapping) else "browser_operation_failed"
        return _error(code)

    def status(self) -> dict[str, Any]:
        if sys.platform == "win32" and not self._owns_listener:
            result = self._control_request("status")
            if result.get("error"):
                return {"backend": "personal", "state": "disconnected", "targets": []}
            return result
        return self._status_local()

    def _status_local(self) -> dict[str, Any]:
        from osia.config import load_settings
        from osia.personal_browser.registration import registration_status

        registration = registration_status()
        if registration.get("registered") is not True:
            return {"backend": "personal", "state": registration.get("state", "setup_required"), "targets": []}
        if load_settings().get("personal_browser_enabled") is not True:
            return {"backend": "personal", "state": "setup_required", "targets": []}
        targets = self._authorized_targets()
        with self._lock:
            connected = list(self._clients.values())
        if targets:
            state = (
                "sensitive_page_blocked"
                if all(target.get("state") == "sensitive_page_blocked" for _, target in targets)
                else "ready"
            )
        else:
            state = "no_authorized_tab" if connected else "browser_stopped"
        safe_targets = [
            {
                "target_handle": target["target_handle"],
                "install_instance_id": client.install_instance_id,
                "browser_session_id": client.browser_session_id,
                "browser_family": client.browser_family,
                "window_id": target["window_id"],
                "tab_id": target["tab_id"],
                "safe_origin": target["safe_origin"],
                "safe_url": target["safe_url"],
                "safe_title": target["safe_title"],
                "state": target.get("state", "ready"),
                "state_ref": target.get("state_ref"),
            }
            for client, target in targets
        ]
        capabilities = sorted(set.intersection(
            *(set(client.capabilities) for client in connected)
        )) if connected else []
        return {"backend": "personal", "state": state, "targets": safe_targets, "capabilities": capabilities}

    def revoke_all(self) -> dict[str, Any]:
        if sys.platform == "win32" and not self._owns_listener:
            return self._control_request("revoke")
        return self._revoke_all_local()

    def _revoke_all_local(self) -> dict[str, Any]:
        revoked = 0
        confirmed = 0
        for client, target in self._authorized_targets():
            request_id = "request:" + secrets.token_urlsafe(24)
            pending = _PendingRequest(
                connection_id=client.connection_id,
                target_handle=str(target["target_handle"]),
            )
            with self._lock:
                client.targets.pop(target["target_handle"], None)
                self._pending[request_id] = pending
            revoked += 1
            try:
                client.send(request_message(
                    request_id=request_id,
                    connection_id=client.connection_id,
                    target_handle=target["target_handle"],
                    operation="revoke",
                    parameters={},
                    timeout_seconds=5.0,
                ))
            except Exception:
                pending.event.set()
            completed = pending.event.wait(5.0)
            with self._lock:
                self._pending.pop(request_id, None)
            response = pending.response
            if (
                completed
                and isinstance(response, Mapping)
                and response.get("status") == "ok"
                and isinstance(response.get("result"), Mapping)
                and response["result"].get("ok") is True
            ):
                confirmed += 1
            else:
                _close_pipe(client.handle)
        return {"ok": True, "revoked": revoked, "confirmed": confirmed}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        was_owner = self._owns_listener
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            for pending in self._pending.values():
                pending.event.set()
        for client in clients:
            _close_pipe(client.handle)
        if sys.platform == "win32" and was_owner:
            for pipe in (_pipe_name(), _control_pipe_name()):
                try:
                    import win32con
                    import win32file

                    handle = win32file.CreateFile(
                        pipe, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                        0, None, win32con.OPEN_EXISTING, 0, None,
                    )
                    win32file.CloseHandle(handle)
                except Exception:
                    pass
        if self._owner_semaphore is not None:
            try:
                if was_owner:
                    import win32event

                    win32event.ReleaseSemaphore(self._owner_semaphore, 1)
                self._owner_semaphore.Close()
            except Exception:
                pass
            self._owner_semaphore = None
        self._owns_listener = False


def _turn_binding_payload(binding) -> dict[str, str]:
    return {
        "personal_connection_id": str(binding.personal_connection_id or ""),
        "personal_session_id": str(binding.personal_session_id or ""),
        "personal_target_handle": str(binding.personal_target_handle or ""),
    }


def _apply_turn_binding(binding, raw: Any) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError("malformed_turn_binding")
    values = [
        raw.get("personal_connection_id"),
        raw.get("personal_session_id"),
        raw.get("personal_target_handle"),
    ]
    if any(not isinstance(value, str) for value in values):
        raise ValueError("malformed_turn_binding")
    if any(values) and not all(values):
        raise ValueError("malformed_turn_binding")
    (
        binding.personal_connection_id,
        binding.personal_session_id,
        binding.personal_target_handle,
    ) = values


def _consume_control_result(
    control_operation: str, result: Mapping[str, Any],
) -> dict[str, Any]:
    if control_operation != "invoke":
        return dict(result)
    control_result = result.get("control_result")
    returned_binding = result.get("turn_binding")
    if not isinstance(control_result, Mapping) or not isinstance(returned_binding, Mapping):
        return _error("malformed_message")
    from osia.tools.browser_controller import current_browser_turn_binding

    binding = current_browser_turn_binding()
    if binding.backend == "personal":
        try:
            _apply_turn_binding(binding, returned_binding)
        except ValueError:
            return _error("malformed_message")
    return dict(control_result)


def _operation_timeout(operation: str, arguments: Mapping[str, Any]) -> float:
    if operation == "wait_for_browser_condition":
        wait = arguments.get("wait_spec")
        if isinstance(wait, Mapping):
            try:
                return min(120.0, max(1.0, float(wait.get("timeout_seconds", 60.0)) + 5.0))
            except (TypeError, ValueError, OverflowError):
                pass
        return 65.0
    if operation in {"navigate_browser", "reload_browser_page", "browser_back", "browser_forward"}:
        return 45.0
    return 20.0


def _wrong_personal_reference(arguments: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    expected = str(target.get("browser_session_id") or "")

    def inspect(value: Any) -> bool:
        if isinstance(value, Mapping):
            session = value.get("browser_session_id")
            if isinstance(session, str) and session != expected:
                return True
            return any(inspect(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(inspect(item) for item in value)
        return False

    return inspect(arguments)


def _state_from_arguments(arguments: Mapping[str, Any]) -> BrowserStateRef | None:
    state = arguments.get("state_ref")
    if not isinstance(state, Mapping):
        return None
    try:
        return BrowserStateRef.parse(state)
    except Exception:
        return None


def _effect_error(code: str, arguments: Mapping[str, Any], *, dispatched: bool) -> dict[str, Any]:
    before = _state_from_arguments(arguments)
    receipt = make_receipt(
        before_state=before,
        after_state=None,
        dispatch_state=DispatchState.EFFECT_UNKNOWN if dispatched else DispatchState.NOT_DISPATCHED,
        outcome=ReceiptOutcome.OUTCOME_UNKNOWN if dispatched else ReceiptOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT,
        evidence=[{"connection_lost": True}] if dispatched else [],
    )
    return action_result(ok=False, receipt=receipt, error_code=code)


def _error(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "text": f"ERROR: personal browser operation failed ({code})",
        "error": {"code": code},
    }


def _pipe_name() -> str:
    from osia.personal_browser.registration_windows import pipe_name

    return pipe_name()


def _control_pipe_name() -> str:
    return _pipe_name() + ".control"


def _owner_mutex_name() -> str:
    from osia.personal_browser.registration_windows import current_user_sid

    return "Local\\space.osia.browser.owner." + current_user_sid()


def _same_user_security_attributes():
    import pywintypes
    import win32con
    import win32security

    from osia.personal_browser.registration_windows import current_user_sid

    sid = win32security.ConvertStringSidToSid(current_user_sid())
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        sid,
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _client_is_current_user(handle) -> bool:
    import win32api
    import win32con
    import win32security

    token = None
    try:
        win32security.ImpersonateNamedPipeClient(handle)
        token = win32security.OpenThreadToken(
            win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True,
        )
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        actual = win32security.ConvertSidToStringSid(sid)
    except Exception:
        return False
    finally:
        if token is not None:
            try:
                token.Close()
            except Exception:
                pass
        try:
            win32security.RevertToSelf()
        except Exception:
            pass
    from osia.personal_browser.registration_windows import current_user_sid

    return actual == current_user_sid()


def _read_exact(handle, size: int) -> bytes | None:
    import win32file

    chunks = []
    remaining = size
    while remaining:
        _code, data = win32file.ReadFile(handle, remaining)
        if not data:
            return None
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _pipe_has_data(handle) -> bool:
    import win32pipe

    _data, available, _remaining = win32pipe.PeekNamedPipe(handle, 0)
    return bool(available)


def _read_pipe_message(handle) -> dict[str, Any] | None:
    header = _read_exact(handle, 4)
    if header is None:
        return None
    size = struct.unpack("<I", header)[0]
    if size <= 0 or size > MAX_ENVELOPE_BYTES:
        raise ProtocolError("message_too_large")
    payload = _read_exact(handle, size)
    return decode_message(payload) if payload is not None else None


def _write_pipe_message(handle, message: Mapping[str, Any]) -> None:
    import win32file

    payload = encode_message(message)
    win32file.WriteFile(handle, struct.pack("<I", len(payload)) + payload)


def _close_pipe(handle) -> None:
    if handle is None:
        return
    try:
        import win32file
        win32file.CloseHandle(handle)
    except Exception:
        pass


_BROKER: PersonalBrowserBroker | None = None
_BROKER_LOCK = threading.Lock()


def get_personal_browser_broker() -> PersonalBrowserBroker:
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is None or _BROKER._closed:
            _BROKER = PersonalBrowserBroker()
        return _BROKER


__all__ = ["PersonalBrowserBroker", "get_personal_browser_broker"]
