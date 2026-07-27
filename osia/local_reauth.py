"""Small, injectable local user-presence verification abstraction (WOR-196)."""

from __future__ import annotations

import ctypes
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class ReauthStatus(str, Enum):
    SUCCESS = "success"
    DENIED = "denied"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class ReauthResult:
    status: ReauthStatus
    method: str
    reason_code: str = ""

    @property
    def allows_activation(self) -> bool:
        return self.status is ReauthStatus.SUCCESS


_ACTIVATION_SEAL = object()
_ACTIVATION_PROOFS: set[str] = set()
_ACTIVATION_PROOF_LOCK = threading.Lock()


@dataclass(frozen=True)
class FullAccessActivationProof:
    method: str
    reauth_status: ReauthStatus
    issued_at: float
    _nonce: str = field(repr=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ACTIVATION_SEAL:
            raise ValueError("untrusted_activation_proof")


def issue_full_access_activation_proof(
    result: ReauthResult,
) -> FullAccessActivationProof:
    if not isinstance(result, ReauthResult) or not result.allows_activation:
        raise ValueError("reauth_does_not_authorize_activation")
    if result.method not in {"windows_hello", "deterministic_test"}:
        raise ValueError("invalid_activation_method")
    nonce = secrets.token_urlsafe(32)
    with _ACTIVATION_PROOF_LOCK:
        _ACTIVATION_PROOFS.add(nonce)
    return FullAccessActivationProof(
        method=result.method,
        reauth_status=result.status,
        issued_at=time.time(),
        _nonce=nonce,
        _seal=_ACTIVATION_SEAL,
    )


def consume_full_access_activation_proof(
    proof: FullAccessActivationProof,
) -> str:
    if not isinstance(proof, FullAccessActivationProof):
        raise ValueError("activation_proof_required")
    if proof._seal is not _ACTIVATION_SEAL:
        raise ValueError("untrusted_activation_proof")
    if proof.reauth_status is not ReauthStatus.SUCCESS:
        raise ValueError("invalid_activation_proof_status")
    if time.time() - proof.issued_at > 120.0:
        with _ACTIVATION_PROOF_LOCK:
            _ACTIVATION_PROOFS.discard(proof._nonce)
        raise ValueError("activation_proof_expired")
    with _ACTIVATION_PROOF_LOCK:
        if proof._nonce not in _ACTIVATION_PROOFS:
            raise ValueError("activation_proof_used")
        _ACTIVATION_PROOFS.remove(proof._nonce)
    return proof.method


class LocalReauthProvider(Protocol):
    def reauthenticate(self, prompt: str) -> ReauthResult:
        """Request local user presence without receiving user credentials."""


class UnavailableLocalReauthProvider:
    def __init__(self, reason_code: str = "platform_reauth_unavailable"):
        self.reason_code = reason_code

    def reauthenticate(self, prompt: str) -> ReauthResult:
        del prompt
        return ReauthResult(
            ReauthStatus.UNAVAILABLE, "unavailable", self.reason_code,
        )


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_text(cls, value: str) -> "_Guid":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


_USER_CONSENT_VERIFIER_INTEROP_IID = _Guid.from_text(
    "39e050c3-4e74-441a-8dc0-b81104df949c"
)
_ASYNC_USER_CONSENT_RESULT_IID = _Guid.from_text(
    "fd596ffd-2318-558f-9dbe-d21df43764a5"
)
_ASYNC_INFO_IID = _Guid.from_text(
    "00000036-0000-0000-c000-000000000046"
)
_USER_CONSENT_RESULTS = {
    0: "Verified",
    1: "DeviceNotPresent",
    2: "NotConfiguredForUser",
    3: "DisabledByPolicy",
    4: "DeviceBusy",
    5: "RetriesExhausted",
    6: "Canceled",
}


def _com_method(pointer, index: int, result_type, *argument_types):
    vtable = ctypes.cast(
        pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
    ).contents
    prototype = ctypes.WINFUNCTYPE(
        result_type, ctypes.c_void_p, *argument_types,
    )
    return prototype(vtable[index])


def _release_com_pointer(pointer) -> None:
    if pointer:
        _com_method(pointer, 2, ctypes.c_ulong)(pointer)


def _verify_windows_hello_for_window(
    window_handle: int,
    prompt: str,
    timeout: float = 120.0,
    *,
    cancellation_event: threading.Event | None = None,
) -> str:
    """Call the desktop UserConsentVerifier interop API for an HWND."""
    from ctypes import wintypes

    combase = ctypes.WinDLL("combase", use_last_error=True)
    combase.RoInitialize.argtypes = [ctypes.c_uint32]
    combase.RoInitialize.restype = ctypes.c_long
    combase.RoUninitialize.argtypes = []
    combase.RoUninitialize.restype = None
    combase.WindowsCreateString.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
    ]
    combase.WindowsCreateString.restype = ctypes.c_long
    combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    combase.WindowsDeleteString.restype = ctypes.c_long
    combase.RoGetActivationFactory.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_Guid),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    combase.RoGetActivationFactory.restype = ctypes.c_long

    runtime_name = ctypes.c_void_p()
    factory = ctypes.c_void_p()
    operation = ctypes.c_void_p()
    async_info = ctypes.c_void_p()
    initialized = False
    try:
        initialize_result = combase.RoInitialize(1)  # RO_INIT_MULTITHREADED
        initialized = initialize_result >= 0
        if initialize_result < 0:
            return "InteropInitializationFailed"
        runtime_class = "Windows.Security.Credentials.UI.UserConsentVerifier"
        if combase.WindowsCreateString(
            runtime_class, len(runtime_class), ctypes.byref(runtime_name),
        ) < 0:
            return "InteropStringFailed"
        if combase.RoGetActivationFactory(
            runtime_name,
            ctypes.byref(_USER_CONSENT_VERIFIER_INTEROP_IID),
            ctypes.byref(factory),
        ) < 0:
            return "InteropFactoryFailed"

        message = ctypes.c_void_p()
        try:
            if combase.WindowsCreateString(
                prompt, len(prompt), ctypes.byref(message),
            ) < 0:
                return "InteropStringFailed"
            request = _com_method(
                factory, 6, ctypes.c_long, wintypes.HWND, ctypes.c_void_p,
                ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p),
            )
            request_result = request(
                factory, wintypes.HWND(window_handle), message,
                ctypes.byref(_ASYNC_USER_CONSENT_RESULT_IID),
                ctypes.byref(operation),
            )
            if request_result < 0:
                return f"InteropRequestFailed:{request_result & 0xffffffff:08x}"
        finally:
            if message:
                combase.WindowsDeleteString(message)

        query_interface = _com_method(
            operation, 0, ctypes.c_long, ctypes.POINTER(_Guid),
            ctypes.POINTER(ctypes.c_void_p),
        )
        if query_interface(
            operation, ctypes.byref(_ASYNC_INFO_IID), ctypes.byref(async_info),
        ) < 0:
            return "InteropAsyncInfoFailed"
        get_status = _com_method(
            async_info, 7, ctypes.c_long, ctypes.POINTER(ctypes.c_int32),
        )
        cancel = _com_method(async_info, 9, ctypes.c_long)
        deadline = time.monotonic() + timeout
        status = ctypes.c_int32()
        while True:
            if get_status(async_info, ctypes.byref(status)) < 0:
                return "InteropStatusFailed"
            if status.value != 0:  # AsyncStatus.Started
                break
            if cancellation_event is not None and cancellation_event.is_set():
                if cancel(async_info) < 0:
                    return "InteropCancelFailed"
                cancel_deadline = time.monotonic() + 5.0
                while status.value == 0 and time.monotonic() < cancel_deadline:
                    time.sleep(0.01)
                    if get_status(async_info, ctypes.byref(status)) < 0:
                        return "InteropStatusFailed"
                return "Canceled"
            if time.monotonic() >= deadline:
                if cancel(async_info) < 0:
                    return "InteropCancelFailed"
                cancel_deadline = time.monotonic() + 5.0
                while status.value == 0 and time.monotonic() < cancel_deadline:
                    time.sleep(0.01)
                    if get_status(async_info, ctypes.byref(status)) < 0:
                        return "InteropStatusFailed"
                return "TimedOut"
            time.sleep(0.05)
        if status.value == 2:
            return "Canceled"
        if status.value != 1:
            return "InteropOperationFailed"
        result = ctypes.c_int32()
        get_results = _com_method(
            operation, 8, ctypes.c_long, ctypes.POINTER(ctypes.c_int32),
        )
        if get_results(operation, ctypes.byref(result)) < 0:
            return "InteropResultFailed"
        return _USER_CONSENT_RESULTS.get(result.value, "Unknown")
    finally:
        if async_info:
            try:
                close = _com_method(async_info, 10, ctypes.c_long)
                close(async_info)
            except Exception:
                pass
        _release_com_pointer(async_info)
        _release_com_pointer(operation)
        _release_com_pointer(factory)
        if runtime_name:
            combase.WindowsDeleteString(runtime_name)
        if initialized:
            combase.RoUninitialize()


class WindowsHelloReauthProvider:
    """Use Windows UserConsentVerifier (Hello/PIN/biometric user presence)."""

    _UNAVAILABLE = {
        "DeviceNotPresent", "NotConfiguredForUser", "DisabledByPolicy",
        "DeviceBusy",
    }

    def reauthenticate(
        self,
        prompt: str,
        *,
        window_handle: int | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> ReauthResult:
        if sys.platform != "win32":
            return ReauthResult(
                ReauthStatus.UNAVAILABLE, "unavailable", "not_windows",
            )
        if sys.getwindowsversion().build < 22000:
            return ReauthResult(
                ReauthStatus.UNAVAILABLE, "unavailable",
                "windows_desktop_reauth_unavailable",
            )
        if not window_handle:
            return ReauthResult(
                ReauthStatus.ERROR, "windows_hello", "window_handle_required",
            )
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.IsWindow.argtypes = [ctypes.c_void_p]
            user32.IsWindow.restype = ctypes.c_bool
            valid_window = user32.IsWindow(ctypes.c_void_p(window_handle))
        except Exception:
            return ReauthResult(
                ReauthStatus.ERROR, "windows_hello", "reauth_backend_error",
            )
        if not valid_window:
            return ReauthResult(
                ReauthStatus.ERROR, "windows_hello", "invalid_window_handle",
            )

        try:
            verification = _verify_windows_hello_for_window(
                window_handle,
                str(prompt or "Confirm local access"),
                115.0,
                cancellation_event=cancellation_event,
            )
        except Exception:
            return ReauthResult(
                ReauthStatus.ERROR, "windows_hello", "reauth_backend_error",
            )
        if verification == "TimedOut":
            return ReauthResult(
                ReauthStatus.ERROR, "windows_hello", "reauth_timeout",
            )
        if verification == "Verified":
            return ReauthResult(ReauthStatus.SUCCESS, "windows_hello")
        if verification == "Canceled":
            return ReauthResult(
                ReauthStatus.CANCELLED, "windows_hello", "user_cancelled",
            )
        if verification == "RetriesExhausted":
            return ReauthResult(
                ReauthStatus.DENIED, "windows_hello", "verification_denied",
            )
        if verification in self._UNAVAILABLE:
            return ReauthResult(
                ReauthStatus.UNAVAILABLE, "unavailable",
                f"windows_hello_{verification.lower()}",
            )
        return ReauthResult(
            ReauthStatus.ERROR, "windows_hello",
            "verification_failed" if verification == "Unknown"
            else "reauth_backend_error",
        )


def default_local_reauth_provider() -> LocalReauthProvider:
    if sys.platform == "win32":
        return WindowsHelloReauthProvider()
    return UnavailableLocalReauthProvider()


__all__ = [
    "FullAccessActivationProof", "LocalReauthProvider", "ReauthResult",
    "ReauthStatus", "consume_full_access_activation_proof",
    "UnavailableLocalReauthProvider", "WindowsHelloReauthProvider",
    "default_local_reauth_provider", "issue_full_access_activation_proof",
]
