"""Feature-gated WSS LAN gateway for the OSIA mobile application.

The gateway uses the existing OSIA X25519/HKDF/ChaCha20-Poly1305 handshake on
top of certificate-pinned TLS, then bridges decrypted JSON only to the existing
loopback WebSocket server. The mobile bearer token never crosses the LAN.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import tempfile
import threading
from typing import Any, Awaitable, Callable

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from osia.config import CONFIG_DIR, load_mobile_token, load_settings
from osia.utils.subprocess_policy import run_background_probe
from osia.relay_crypto import (
    ALG_CHACHA20_POLY1305,
    E2E_VERSION,
    MOBILE_TO_PC,
    PC_TO_MOBILE,
    OutboundSequence,
    RelayCryptoError,
    RelaySessionContext,
    SequenceReplayGuard,
    b64url_decode,
    b64url_encode,
    decrypt_frame,
    derive_pair_secret,
    derive_session_keys,
    encrypt_frame,
    generate_nonce_base,
    generate_x25519_private_key,
    handshake_confirmation_tag,
    relay_frame_is_final,
    relay_frame_priority,
    verify_handshake_confirmation,
    x25519_public_key,
)
from osia.transport_coordinator import (
    APPLICATION_TRANSPORT_COORDINATOR,
    ApplicationTransportCoordinator,
)


LAN_PROTOCOL_VERSION = 1
LAN_PORT = 8766
LAN_PATH = "/lan"
MAX_APP_FRAME_BYTES = 16 * 1024 * 1024
MAX_WIRE_FRAME_BYTES = 24 * 1024 * 1024
HANDSHAKE_TIMEOUT_SECONDS = 5.0
LOCAL_WS_URL = "ws://127.0.0.1:8765/ws"

_CONTROL_FIELDS = {"v", "type", "role", "pair_id", "payload"}
_HELLO_FIELDS = {
    "e2e_version",
    "alg",
    "public_key",
    "nonce_base",
    "e2e_chunk_version",
}
_READY_FIELDS = {"e2e_version", "alg", "kid", "tag"}
_E2E_FRAME_FIELDS = {
    "v",
    "type",
    "role",
    "pair_id",
    "seq",
    "e2e_version",
    "alg",
    "kid",
    "priority",
    "final",
    "nonce",
    "ciphertext",
}

_CERT_FILE = Path(CONFIG_DIR) / "lan_direct_cert.pem"
_KEY_FILE = Path(CONFIG_DIR) / "lan_direct_key.pem"
_PROVISIONING_LOCK = threading.Lock()
_PROVISIONING: dict[str, Any] | None = None


def _set_lan_provisioning(value: dict[str, Any] | None) -> None:
    global _PROVISIONING
    with _PROVISIONING_LOCK:
        _PROVISIONING = dict(value) if value is not None else None


def lan_provisioning_snapshot() -> dict[str, Any] | None:
    """Return the E2E-relay provisioning payload, never an mDNS payload."""

    with _PROVISIONING_LOCK:
        return dict(_PROVISIONING) if _PROVISIONING is not None else None


class LanProtocolError(Exception):
    """Fail-closed LAN handshake or frame validation error."""


class LanTransportBusy(LanProtocolError):
    """The single authoritative application bridge is already owned."""


def lan_direct_enabled() -> bool:
    raw = os.getenv("OSIA_LAN_DIRECT_V1")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return load_settings().get("lan_direct_v1") is True


def configured_lan_host() -> str:
    raw = os.getenv("OSIA_LAN_DIRECT_HOST")
    if raw is None:
        raw = str(load_settings().get("lan_direct_host") or "")
    if raw.strip():
        return validate_private_lan_host(raw)
    from osia.lan_discovery import select_private_ipv4

    return select_private_ipv4()


def validate_private_lan_host(raw: str) -> str:
    value = raw.strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("lan_direct_host must be a numeric private IP address") from exc
    private_ranges = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in network for network in private_ranges
    ):
        raise ValueError("lan_direct_host must be a private IPv4 address")
    return str(address)


def ensure_lan_certificate(
    host: str,
    *,
    cert_file: Path = _CERT_FILE,
    key_file: Path = _KEY_FILE,
) -> tuple[Path, Path, str]:
    """Load or atomically create a valid, protected P-256 certificate pair."""

    host = validate_private_lan_host(host)
    cert_file = Path(cert_file)
    key_file = Path(key_file)
    try:
        certificate, _ = _load_and_validate_certificate_pair(
            host,
            cert_file,
            key_file,
        )
    except Exception:
        _regenerate_certificate_pair(host, cert_file, key_file)
        certificate, _ = _load_and_validate_certificate_pair(
            host,
            cert_file,
            key_file,
        )

    _protect_private_key(key_file)
    if not _private_key_permissions_are_safe(key_file):
        raise PermissionError("LAN private-key permissions are not restricted")

    # Exercise the platform TLS loader before exposing the pin. A pair that the
    # runtime cannot load is unusable and must fail LAN startup closed.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_file), str(key_file))
    fingerprint = hashlib.sha256(
        certificate.public_bytes(serialization.Encoding.DER)
    ).hexdigest()
    return cert_file, key_file, fingerprint


def _certificate_time(certificate: x509.Certificate, name: str) -> datetime:
    value = getattr(certificate, f"{name}_utc", None)
    if value is None:
        value = getattr(certificate, name).replace(tzinfo=timezone.utc)
    return value


def _load_and_validate_certificate_pair(
    host: str,
    cert_file: Path,
    key_file: Path,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    certificate = x509.load_pem_x509_certificate(cert_file.read_bytes())
    private_key = serialization.load_pem_private_key(
        key_file.read_bytes(),
        password=None,
    )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve,
        ec.SECP256R1,
    ):
        raise ValueError("LAN private key must be ECDSA P-256")
    public_key = certificate.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve,
        ec.SECP256R1,
    ):
        raise ValueError("LAN certificate must use ECDSA P-256")
    if public_key.public_numbers() != private_key.public_key().public_numbers():
        raise ValueError("LAN certificate and private key do not match")

    now = datetime.now(timezone.utc)
    if not (
        _certificate_time(certificate, "not_valid_before")
        <= now
        <= _certificate_time(certificate, "not_valid_after")
    ):
        raise ValueError("LAN certificate is outside its validity period")
    san = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    general_names = list(san)
    expected_address = ipaddress.ip_address(host)
    if (
        len(general_names) != 1
        or not isinstance(general_names[0], x509.IPAddress)
        or general_names[0].value != expected_address
    ):
        raise ValueError("LAN certificate SAN does not match the private IPv4 host")
    validate_private_lan_host(str(general_names[0].value))
    if certificate.subject != certificate.issuer:
        raise ValueError("LAN certificate must remain self-signed")
    try:
        public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            ec.ECDSA(certificate.signature_hash_algorithm),
        )
    except InvalidSignature as exc:
        raise ValueError("LAN certificate self-signature is invalid") from exc
    return certificate, private_key


def _new_certificate_pair(host: str) -> tuple[bytes, bytes]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "OSIA LAN Direct")]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    key_bytes = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return certificate.public_bytes(serialization.Encoding.PEM), key_bytes


def _stage_bytes(path: Path, data: bytes, *, private_key: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    staged = Path(raw_path)
    try:
        if os.name != "nt":
            os.fchmod(
                descriptor,
                stat.S_IRUSR | stat.S_IWUSR if private_key else 0o644,
            )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if private_key:
            _protect_private_key(staged)
        return staged
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            staged.unlink()
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _replace_bytes_atomically(path: Path, data: bytes, *, private_key: bool) -> None:
    staged = _stage_bytes(path, data, private_key=private_key)
    try:
        os.replace(staged, path)
        _fsync_directory(path.parent)
    finally:
        with suppress(OSError):
            staged.unlink()


def _regenerate_certificate_pair(host: str, cert_file: Path, key_file: Path) -> None:
    """Stage and validate both files, then atomically replace each member."""

    cert_bytes, key_bytes = _new_certificate_pair(host)
    old_cert = cert_file.read_bytes() if cert_file.exists() else None
    old_key = key_file.read_bytes() if key_file.exists() else None
    staged_cert: Path | None = None
    staged_key: Path | None = None
    try:
        staged_cert = _stage_bytes(cert_file, cert_bytes, private_key=False)
        staged_key = _stage_bytes(key_file, key_bytes, private_key=True)
        _load_and_validate_certificate_pair(host, staged_cert, staged_key)
        os.replace(staged_key, key_file)
        _fsync_directory(key_file.parent)
        os.replace(staged_cert, cert_file)
        _fsync_directory(cert_file.parent)
        _protect_private_key(key_file)
        _load_and_validate_certificate_pair(host, cert_file, key_file)
    except Exception:
        # Best-effort rollback keeps a previously complete pair complete if the
        # second atomic replacement fails. On the next startup, validation still
        # rejects any cross-file mismatch before TLS can bind.
        if old_key is not None and old_cert is not None:
            with suppress(Exception):
                _replace_bytes_atomically(key_file, old_key, private_key=True)
            with suppress(Exception):
                _replace_bytes_atomically(cert_file, old_cert, private_key=False)
        else:
            if key_file.exists():
                with suppress(OSError):
                    key_file.unlink()
            if cert_file.exists():
                with suppress(OSError):
                    cert_file.unlink()
        raise
    finally:
        if staged_key is not None:
            with suppress(OSError):
                staged_key.unlink()
        if staged_cert is not None:
            with suppress(OSError):
                staged_cert.unlink()


def _protect_private_key(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return
    if _private_key_permissions_are_safe(path):
        return
    script = r"""
$ErrorActionPreference = 'Stop'
$path = $env:OSIA_LAN_ACL_TARGET
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$required = @(
  $current,
  [System.Security.Principal.SecurityIdentifier]'S-1-5-18',
  [System.Security.Principal.SecurityIdentifier]'S-1-5-32-544'
) | Sort-Object Value -Unique
$acl = Get-Acl -LiteralPath $path
$acl.SetAccessRuleProtection($true, $false)
@($acl.Access | ForEach-Object { $_.IdentityReference }) |
  Select-Object -Unique |
  ForEach-Object { $acl.PurgeAccessRules($_) }
foreach ($sid in $required) {
  $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.AccessControlType]::Allow
  )
  [void]$acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $path -AclObject $acl
"""
    result = run_background_probe(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        env={**os.environ, "OSIA_LAN_ACL_TARGET": str(path)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0 or not _private_key_permissions_are_safe(path):
        raise PermissionError("could not restrict LAN private-key ACL")


def _private_key_permissions_are_safe(path: Path) -> bool:
    if os.name != "nt":
        return stat.S_IMODE(path.stat().st_mode) == (
            stat.S_IRUSR | stat.S_IWUSR
        )
    script = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:OSIA_LAN_ACL_TARGET
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($current, 'S-1-5-18', 'S-1-5-32-544') |
  Sort-Object -Unique
$rules = $acl.GetAccessRules(
  $true,
  $true,
  [System.Security.Principal.SecurityIdentifier]
)
if (-not $acl.AreAccessRulesProtected) { exit 2 }
if ($rules.Count -ne $allowed.Count) { exit 3 }
foreach ($rule in $rules) {
  if ($rule.IsInherited) { exit 4 }
  if ($rule.AccessControlType -ne 'Allow') { exit 5 }
  if ($allowed -notcontains $rule.IdentityReference.Value) { exit 6 }
  if (($rule.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -ne [System.Security.AccessControl.FileSystemRights]::FullControl) { exit 7 }
}
foreach ($sid in $allowed) {
  if (-not ($rules | Where-Object { $_.IdentityReference.Value -eq $sid })) { exit 8 }
}
exit 0
"""
    try:
        result = run_background_probe(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            env={**os.environ, "OSIA_LAN_ACL_TARGET": str(path)},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _pair_id(token: str) -> str:
    return hashlib.sha256(derive_pair_secret(token).hex().encode("utf-8")).hexdigest()


def _exact_object(value: Any, fields: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == fields


def _decode_sized(value: Any, expected: int, field: str) -> bytes:
    if not isinstance(value, str):
        raise LanProtocolError(f"{field} must be a string")
    try:
        decoded = b64url_decode(value)
    except Exception as exc:
        raise LanProtocolError(f"{field} is not valid base64url") from exc
    if len(decoded) != expected:
        raise LanProtocolError(f"{field} has the wrong length")
    return decoded


class LanSessionState(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    READY = "ready"
    ACTIVE_TURN = "active_turn"
    CLOSING = "closing"
    DISCONNECTED = "disconnected"


class LanGatewaySession:
    """One authenticated LAN socket and its single loopback bridge."""

    def __init__(
        self,
        websocket,
        *,
        token: str,
        coordinator: ApplicationTransportCoordinator = APPLICATION_TRANSPORT_COORDINATOR,
        connect_local: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self.websocket = websocket
        self.token = token
        self.pair_id = _pair_id(token)
        self.coordinator = coordinator
        self.connect_local = connect_local or self._connect_local
        self.private_key = generate_x25519_private_key()
        self.public_key = x25519_public_key(self.private_key)
        self.nonce_base = generate_nonce_base()
        self.context: RelaySessionContext | None = None
        self.keys = None
        self.replay_guard = SequenceReplayGuard()
        self.outbound_sequence = OutboundSequence()
        self._send_lock = asyncio.Lock()
        self.state = LanSessionState.IDLE
        self._active_request_id = ""
        self._active_turn_id = ""

    def _set_state(self, state: LanSessionState) -> None:
        allowed = {
            LanSessionState.IDLE: {LanSessionState.CONNECTING},
            LanSessionState.CONNECTING: {
                LanSessionState.AUTHENTICATING,
                LanSessionState.CLOSING,
            },
            LanSessionState.AUTHENTICATING: {
                LanSessionState.READY,
                LanSessionState.CLOSING,
            },
            LanSessionState.READY: {
                LanSessionState.ACTIVE_TURN,
                LanSessionState.CLOSING,
            },
            LanSessionState.ACTIVE_TURN: {
                LanSessionState.READY,
                LanSessionState.CLOSING,
            },
            LanSessionState.CLOSING: {LanSessionState.DISCONNECTED},
            LanSessionState.DISCONNECTED: set(),
        }
        if state is self.state:
            return
        if state not in allowed[self.state]:
            raise RuntimeError(
                f"invalid LAN lifecycle transition {self.state.value}->{state.value}"
            )
        self.state = state

    async def run(self) -> None:
        lease = None
        local_ws = None
        bridge_tasks: list[asyncio.Task] = []
        close_code: int | None = None
        close_reason = ""
        self._set_state(LanSessionState.CONNECTING)
        try:
            self._set_state(LanSessionState.AUTHENTICATING)
            hello = await self._recv_control("E2E_HELLO")
            payload = hello["payload"]
            if not _exact_object(payload, _HELLO_FIELDS):
                raise LanProtocolError("malformed E2E_HELLO payload")
            if (
                payload["e2e_version"] != E2E_VERSION
                or payload["alg"] != ALG_CHACHA20_POLY1305
                or payload["e2e_chunk_version"] != 1
            ):
                raise LanProtocolError("unsupported E2E capabilities")
            mobile_public_key = _decode_sized(payload["public_key"], 32, "public_key")
            mobile_nonce_base = _decode_sized(payload["nonce_base"], 4, "nonce_base")
            self.context = RelaySessionContext(
                pair_id=self.pair_id,
                pc_public_key=self.public_key,
                mobile_public_key=mobile_public_key,
                pc_nonce_base=self.nonce_base,
                mobile_nonce_base=mobile_nonce_base,
            )
            self.keys = derive_session_keys(
                token=self.token,
                local_private_key=self.private_key,
                peer_public_key=mobile_public_key,
                context=self.context,
            )
            await self._send_control(
                "E2E_HELLO",
                {
                    "e2e_version": E2E_VERSION,
                    "alg": ALG_CHACHA20_POLY1305,
                    "public_key": b64url_encode(self.public_key),
                    "nonce_base": b64url_encode(self.nonce_base),
                    "e2e_chunk_version": 1,
                },
            )

            ready = await self._recv_control("E2E_READY")
            ready_payload = ready["payload"]
            if not _exact_object(ready_payload, _READY_FIELDS):
                raise LanProtocolError("malformed E2E_READY payload")
            if (
                ready_payload["e2e_version"] != E2E_VERSION
                or ready_payload["alg"] != ALG_CHACHA20_POLY1305
                or ready_payload["kid"] != self.keys.kid
                or not verify_handshake_confirmation(
                    token=self.token,
                    context=self.context,
                    sender_role="mobile",
                    tag=str(ready_payload["tag"]),
                )
            ):
                raise LanProtocolError("E2E identity confirmation failed")

            lease = self.coordinator.try_acquire("lan", self)
            if lease is None:
                raise LanTransportBusy("application transport busy")
            local_ws = await self.connect_local(self.token)
            await self._send_control(
                "E2E_READY",
                {
                    "e2e_version": E2E_VERSION,
                    "alg": ALG_CHACHA20_POLY1305,
                    "kid": self.keys.kid,
                    "tag": handshake_confirmation_tag(
                        token=self.token,
                        context=self.context,
                        sender_role="pc",
                    ),
                },
            )
            await self._send_control(
                "PC_READY",
                {"e2e": True, "transport": "lan", "protocol_version": 1},
            )
            self._set_state(LanSessionState.READY)

            bridge_tasks = [
                asyncio.create_task(self._lan_to_local(local_ws)),
                asyncio.create_task(self._local_to_lan(local_ws)),
            ]
            done, _pending = await asyncio.wait(
                bridge_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                await task
        except asyncio.CancelledError:
            raise
        except LanTransportBusy:
            close_code = 1013
            close_reason = "application transport busy"
        except (LanProtocolError, RelayCryptoError, ValueError, KeyError, TypeError):
            close_code = 1008
            close_reason = "LAN authentication failed"
        except Exception:
            close_code = 1011
            close_reason = "LAN bridge unavailable"
        finally:
            if self.state not in {
                LanSessionState.CLOSING,
                LanSessionState.DISCONNECTED,
            }:
                self._set_state(LanSessionState.CLOSING)
            for task in bridge_tasks:
                if not task.done():
                    task.cancel()
            if bridge_tasks:
                await asyncio.gather(*bridge_tasks, return_exceptions=True)
            if local_ws is not None:
                with suppress(Exception):
                    await local_ws.close()
            if lease is not None:
                lease.release()
            if close_code is not None:
                with suppress(Exception):
                    await self.websocket.close(
                        code=close_code,
                        reason=close_reason,
                    )
            if self.state is LanSessionState.CLOSING:
                self._set_state(LanSessionState.DISCONNECTED)

    async def _recv_control(self, expected_type: str) -> dict[str, Any]:
        try:
            raw = await asyncio.wait_for(
                self.websocket.recv(), timeout=HANDSHAKE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            raise LanProtocolError("LAN handshake timed out") from exc
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8192:
            raise LanProtocolError("invalid LAN control frame")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LanProtocolError("invalid LAN control JSON") from exc
        if (
            not _exact_object(message, _CONTROL_FIELDS)
            or message["v"] != LAN_PROTOCOL_VERSION
            or message["type"] != expected_type
            or message["role"] != "mobile"
            or message["pair_id"] != self.pair_id
        ):
            raise LanProtocolError("invalid LAN control envelope")
        return message

    async def _send_control(self, message_type: str, payload: dict[str, Any]) -> None:
        await self.websocket.send(
            json.dumps(
                {
                    "v": LAN_PROTOCOL_VERSION,
                    "type": message_type,
                    "role": "pc",
                    "pair_id": self.pair_id,
                    "payload": payload,
                },
                separators=(",", ":"),
            )
        )

    async def _lan_to_local(self, local_ws) -> None:
        async for raw in self.websocket:
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_WIRE_FRAME_BYTES:
                raise LanProtocolError("invalid encrypted LAN frame")
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LanProtocolError("invalid encrypted LAN JSON") from exc
            if (
                not _exact_object(envelope, _E2E_FRAME_FIELDS)
                or envelope["v"] != LAN_PROTOCOL_VERSION
                or envelope["type"] != "E2E_FRAME"
                or envelope["role"] != "mobile"
                or envelope["pair_id"] != self.pair_id
            ):
                raise LanProtocolError("invalid encrypted LAN envelope")
            frame = decrypt_frame(
                envelope,
                keys=self.keys,
                direction=MOBILE_TO_PC,
                replay_guard=self.replay_guard,
                replay_key="mobile",
            )
            if frame.get("type") == "auth":
                raise LanProtocolError("auth frames are not allowed over LAN")
            starts_turn = frame.get("type") in {"text", "audio", "speak"} or (
                frame.get("type") == "file" and frame.get("is_image") is True
            )
            if starts_turn:
                request_id = str(
                    frame.get("request_id")
                    or frame.get("client_request_id")
                    or ""
                )
                turn_id = str(frame.get("turn_id") or "")
                if not request_id:
                    raise LanProtocolError("application turn is missing request correlation")
                if self.state is LanSessionState.READY:
                    self._active_request_id = request_id
                    self._active_turn_id = turn_id
                    self._set_state(LanSessionState.ACTIVE_TURN)
                elif (
                    self.state is not LanSessionState.ACTIVE_TURN
                    or request_id != self._active_request_id
                    or (turn_id and turn_id != self._active_turn_id)
                ):
                    raise LanProtocolError("concurrent LAN application turn rejected")
            encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_APP_FRAME_BYTES:
                raise LanProtocolError("decrypted LAN frame is too large")
            await local_ws.send(encoded)

    async def _local_to_lan(self, local_ws) -> None:
        async for raw in local_ws:
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_APP_FRAME_BYTES:
                continue
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            if self.state is LanSessionState.ACTIVE_TURN:
                request_id = str(
                    frame.get("request_id")
                    or frame.get("client_request_id")
                    or ""
                )
                turn_id = str(frame.get("turn_id") or "")
                if (
                    request_id == self._active_request_id
                    and not self._active_turn_id
                    and turn_id
                ):
                    self._active_turn_id = turn_id
                same_turn = (
                    request_id == self._active_request_id
                    and (
                        not self._active_turn_id
                        or not turn_id
                        or turn_id == self._active_turn_id
                    )
                )
                terminal_error = (
                    frame.get("terminal") is True
                    and frame.get("status")
                    in {"error", "cancelled", "canceled", "rejected"}
                )
                media_terminal = frame.get("media_terminal") is True
                if same_turn and (terminal_error or media_terminal):
                    self._active_request_id = ""
                    self._active_turn_id = ""
                    self._set_state(LanSessionState.READY)
            envelope = encrypt_frame(
                frame,
                keys=self.keys,
                direction=PC_TO_MOBILE,
                pair_id=self.pair_id,
                role="pc",
                priority=relay_frame_priority(frame),
                final=relay_frame_is_final(frame),
                outbound_sequence=self.outbound_sequence,
            )
            message = {
                "v": LAN_PROTOCOL_VERSION,
                "type": "E2E_FRAME",
                "role": "pc",
                "pair_id": self.pair_id,
                **envelope,
            }
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_WIRE_FRAME_BYTES:
                raise LanProtocolError("encrypted LAN frame is too large")
            async with self._send_lock:
                await self.websocket.send(encoded)

    @staticmethod
    async def _connect_local(token: str):
        import websockets

        local_ws = await websockets.connect(
            LOCAL_WS_URL,
            max_size=MAX_APP_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
        )
        await local_ws.send(json.dumps({"type": "auth", "token": token}))
        return local_ws


async def _serve(
    host: str,
    cert_file: Path,
    key_file: Path,
    ready: Callable[[], None],
    stop_event: asyncio.Event,
) -> None:
    import websockets
    from osia.lan_discovery import LanDiscoveryAdvertiser

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_context.load_cert_chain(str(cert_file), str(key_file))

    async def handler(websocket) -> None:
        request = getattr(websocket, "request", None)
        if request is None or request.path != LAN_PATH:
            await websocket.close(code=1008, reason="invalid LAN path")
            return
        token = load_mobile_token()
        if not token:
            await websocket.close(code=1011, reason="mobile pairing unavailable")
            return
        await LanGatewaySession(websocket, token=token).run()

    async with websockets.serve(
        handler,
        host,
        LAN_PORT,
        ssl=ssl_context,
        max_size=MAX_WIRE_FRAME_BYTES,
        ping_interval=20,
        ping_timeout=20,
    ):
        provisioning = lan_provisioning_snapshot()
        alias = str((provisioning or {}).get("instance_alias") or "")
        advertiser = LanDiscoveryAdvertiser(host, LAN_PORT, alias)
        await asyncio.to_thread(advertiser.start)
        try:
            # Ready means both WSS bind and DNS-SD registration succeeded.
            ready()
            await stop_event.wait()
        finally:
            await asyncio.to_thread(advertiser.close)


class LanGatewayLifecycle:
    """Threaded gateway with explicit readiness, stop, and bounded cleanup."""

    def __init__(self, serve: Callable[..., Awaitable[None]] = _serve) -> None:
        self._serve = serve
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._ready_event: threading.Event | None = None
        self._stop_requested = False
        self._started = False
        self._error: BaseException | None = None

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started and self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> BaseException | None:
        with self._lock:
            return self._error

    def start(
        self,
        host: str,
        cert_file: Path,
        key_file: Path,
        *,
        timeout: float = 5.0,
    ) -> bool:
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                ready_event = self._ready_event
            else:
                self._thread = None
                self._loop = None
                self._async_stop = None
                self._stop_requested = False
                self._started = False
                self._error = None
                ready_event = threading.Event()
                self._ready_event = ready_event
                thread = threading.Thread(
                    target=self._thread_main,
                    args=(host, cert_file, key_file),
                    name="osia-lan-gateway",
                    daemon=True,
                )
                self._thread = thread
                thread.start()

        if ready_event is None or not ready_event.wait(timeout):
            self.stop(timeout=timeout)
            with self._lock:
                if self._error is None:
                    self._error = TimeoutError("LAN gateway readiness timed out")
            return False
        with self._lock:
            ready = self._started and self._error is None
        if not ready:
            thread.join(timeout)
            self._reset_finished_thread(thread)
        return ready

    def stop(self, *, timeout: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
            self._stop_requested = True
            loop = self._loop
            async_stop = self._async_stop
        if loop is not None and async_stop is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(async_stop.set)
        if thread is None:
            with self._lock:
                self._started = False
            return True
        if thread is not threading.current_thread():
            thread.join(timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._reset_finished_thread(thread)
        return stopped

    def _mark_ready(self) -> None:
        with self._lock:
            self._started = True
            ready_event = self._ready_event
        if ready_event is not None:
            ready_event.set()

    def _thread_main(self, host: str, cert_file: Path, key_file: Path) -> None:
        async def run() -> None:
            loop = asyncio.get_running_loop()
            stop_event = asyncio.Event()
            with self._lock:
                self._loop = loop
                self._async_stop = stop_event
                if self._stop_requested:
                    stop_event.set()
            await self._serve(
                host,
                cert_file,
                key_file,
                self._mark_ready,
                stop_event,
            )

        try:
            asyncio.run(run())
        except BaseException as exc:
            with self._lock:
                self._error = exc
                self._started = False
                ready_event = self._ready_event
            if ready_event is not None:
                ready_event.set()
        finally:
            with self._lock:
                # A serve implementation that returned before readiness is a
                # startup failure, not a successful fire-and-forget start.
                if not self._started and self._error is None:
                    self._error = RuntimeError("LAN gateway stopped before readiness")
                if self._started:
                    self._started = False
                ready_event = self._ready_event
            if ready_event is not None:
                ready_event.set()

    def _reset_finished_thread(self, thread: threading.Thread) -> None:
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                self._loop = None
                self._async_stop = None
                self._ready_event = None
                self._stop_requested = False
                self._started = False


_GATEWAY_LIFECYCLE = LanGatewayLifecycle()


def start_background_gateway() -> bool:
    """Start the default-off LAN gateway and wait for bind readiness."""

    if not lan_direct_enabled():
        return False
    if _GATEWAY_LIFECYCLE.started:
        return True
    try:
        host = configured_lan_host()
        cert_file, key_file, fingerprint = ensure_lan_certificate(host)
        from osia.lan_discovery import PRODUCT_VERSION, load_or_create_instance_alias

        alias = load_or_create_instance_alias()
        _set_lan_provisioning({
            "protocol_version": LAN_PROTOCOL_VERSION,
            "product_version": PRODUCT_VERSION,
            "instance_alias": alias,
            "certificate_sha256": fingerprint,
            "port": LAN_PORT,
            "path": LAN_PATH,
        })
    except Exception as exc:
        _set_lan_provisioning(None)
        print(f"[OSIA LAN] gateway disabled: {exc}")
        return False
    if not _GATEWAY_LIFECYCLE.start(host, cert_file, key_file):
        error = _GATEWAY_LIFECYCLE.error
        _set_lan_provisioning(None)
        print(f"[OSIA LAN] gateway disabled: {error or 'startup failed'}")
        return False
    print(f"[OSIA LAN] WSS gateway ready on private IPv4 port {LAN_PORT}")
    return True


def stop_background_gateway(*, timeout: float = 5.0) -> bool:
    """Stop the LAN gateway and join its thread within a bounded interval."""

    stopped = _GATEWAY_LIFECYCLE.stop(timeout=timeout)
    if stopped:
        _set_lan_provisioning(None)
    return stopped
