#!/usr/bin/env python3
"""Run the isolated WOR-216 physical PC-to-Mobile canonical-history gate.

The normal mode creates a unique temporary root, copies the current PC and
Mobile sources into it, launches a loopback-only real FastAPI/WebSocket server,
maps Android tcp:8765 to an ephemeral PC port, and runs the Flutter integration
test on one explicitly selected physical device. The internal ``--serve`` mode
is guarded by a sentinel and child-only environment marker.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


GATE_PREFIX = "OSIA-WOR216-REAL-GATE-"
GATE_SENTINEL = ".wor216-gate-root.json"
DEVICE_PORT = 8765
PUBLIC_PACKAGE = "space.osia.app"
FORBIDDEN_PACKAGES = ("space.osia.app.test", "space.osia.app.test.test")
MAX_SCREEN_TIMEOUT_MS = "2147483647"
POWER_WATCHDOG_INTERVAL_SECONDS = 1.0
GATE_TITLE = "WOR216 isolated canonical gate"
DESKTOP_MESSAGE = "wor216 synthetic desktop origin"
MOBILE_MESSAGE = "wor216 synthetic mobile origin"
CONTROL_HEADER = "X-WOR216-Gate-Control"
INTEGRATION_REGISTRANT_BLOCK = """    try {
      flutterEngine.getPlugins().add(new dev.flutter.plugins.integration_test.IntegrationTestPlugin());
    } catch (Exception e) {
      Log.e(TAG, "Error registering plugin integration_test, dev.flutter.plugins.integration_test.IntegrationTestPlugin", e);
    }
"""


class GateError(RuntimeError):
    pass


class KeyguardGateError(GateError):
    pass


@dataclass(frozen=True)
class VerifiedReleaseApk:
    path: Path
    sha256: str
    version_name: str
    version_code: str
    certificate_sha256: str


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _validate_gate_root(root: Path, parent_temp: Path, run_id: str) -> Path:
    resolved = root.resolve()
    if resolved.parent != parent_temp.resolve():
        raise GateError("gate root is not an immediate child of the approved temp dir")
    if not resolved.name.startswith(GATE_PREFIX):
        raise GateError("gate root has an invalid prefix")
    sentinel = resolved / GATE_SENTINEL
    if not sentinel.is_file():
        raise GateError("gate root sentinel is missing")
    try:
        payload = json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError("gate root sentinel is unreadable") from exc
    if payload != {"schema": 1, "run_id": run_id}:
        raise GateError("gate root sentinel does not match this run")
    return resolved


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_fingerprint(path: Path) -> dict:
    if not path.is_file():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _sha256(path),
    }


def _conversation_inventory(path: Path) -> dict:
    if not path.is_dir():
        return {"exists": False, "files": []}
    files = []
    for item in sorted(path.glob("*.json"), key=lambda candidate: candidate.name):
        stat = item.stat()
        files.append(
            {
                "name": item.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": _sha256(item),
            }
        )
    return {"exists": True, "files": files}


def _run_checked(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise GateError(f"command failed with exit code {completed.returncode}")
    return completed.stdout


def _adb_output(adb: Path, *args: str) -> str:
    return _run_checked([str(adb), *args])


def _package_path(adb: Path, package: str) -> tuple[str, ...]:
    completed = subprocess.run(
        [str(adb), "shell", "pm", "path", package],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = completed.stdout.strip()
    if completed.returncode == 1 and not output:
        return ()
    if completed.returncode != 0:
        raise GateError(
            f"Android package probe failed with exit code {completed.returncode}"
        )
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _installed_release_version(adb: Path) -> tuple[str, str] | None:
    if not _package_path(adb, PUBLIC_PACKAGE):
        return None
    output = _adb_output(adb, "shell", "dumpsys", "package", PUBLIC_PACKAGE)
    version_code = re.search(r"\bversionCode=(\d+)\b", output)
    version_name = re.search(r"(?m)^\s*versionName=([^\s]+)\s*$", output)
    if not version_code or not version_name:
        raise GateError("installed official release version is unreadable")
    return version_name.group(1), version_code.group(1)


def _android_setting(adb: Path, namespace: str, name: str) -> str:
    return _adb_output(adb, "shell", "settings", "get", namespace, name).strip()


def _restore_android_setting(
    adb: Path, namespace: str, name: str, previous: str
) -> None:
    if previous in {"", "null"}:
        _adb_output(adb, "shell", "settings", "delete", namespace, name)
        expected = "null"
    else:
        _adb_output(adb, "shell", "settings", "put", namespace, name, previous)
        expected = previous
    if _android_setting(adb, namespace, name) != expected:
        raise GateError(f"Android {name} setting was not restored")


def _android_power_state(adb: Path) -> dict[str, bool]:
    power = _adb_output(adb, "shell", "dumpsys", "power")
    policy = _adb_output(adb, "shell", "dumpsys", "window", "policy")
    display = _adb_output(adb, "shell", "dumpsys", "display")
    wakefulness = re.search(r"(?m)^\s*mWakefulness=(\w+)\s*$", power)
    powered = re.search(r"(?m)^\s*mIsPowered=(true|false)\s*$", power)
    plug_type = re.search(r"(?m)^\s*mPlugType=(\d+)\s*$", power)
    showing = re.search(r"(?m)^\s*showing=(true|false)\s*$", policy)
    policy_screen = re.search(
        r"(?m)^\s*screenState=(?:SCREEN_STATE_)?(ON|OFF|DOZE|DOZE_SUSPEND)\s*$",
        policy,
    )
    display_screen = re.search(
        r"(?m)^\s*(?:mScreenState|mGlobalDisplayState)=(ON|OFF|DOZE|DOZE_SUSPEND)\s*$",
        display,
    )
    if not all((wakefulness, powered, plug_type, showing)) or not (
        policy_screen or display_screen
    ):
        raise GateError("Android power/keyguard state is unreadable")
    screen_state = (
        policy_screen.group(1) if policy_screen else display_screen.group(1)
    )
    return {
        "interactive": wakefulness.group(1).lower() == "awake",
        "screen_on": screen_state == "ON",
        "secure_keyguard_showing": showing.group(1) == "true",
        "usb_powered": powered.group(1) == "true"
        and bool(int(plug_type.group(1)) & 2),
    }


class _AndroidPowerGuard:
    def __init__(self, adb: Path) -> None:
        self.adb = adb
        self.previous_stay_awake: str | None = None
        self.previous_screen_timeout: str | None = None
        self.before: dict[str, object] | None = None
        self.guarded: dict[str, object] | None = None
        self.restored: dict[str, object] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: Exception | None = None
        self._restore_complete = False

    def enable(self) -> None:
        state = _android_power_state(self.adb)
        self.previous_stay_awake = _android_setting(
            self.adb, "global", "stay_on_while_plugged_in"
        )
        self.previous_screen_timeout = _android_setting(
            self.adb, "system", "screen_off_timeout"
        )
        self.before = {
            **state,
            "stay_on_while_plugged_in": self.previous_stay_awake,
            "screen_off_timeout": self.previous_screen_timeout,
        }
        try:
            current = (
                0
                if self.previous_stay_awake in {"", "null"}
                else int(self.previous_stay_awake)
            )
        except ValueError as exc:
            raise GateError("Android stay-awake setting is unreadable") from exc
        requested_stay_awake = str(current | 2)
        _adb_output(
            self.adb,
            "shell",
            "settings",
            "put",
            "global",
            "stay_on_while_plugged_in",
            requested_stay_awake,
        )
        _adb_output(
            self.adb,
            "shell",
            "settings",
            "put",
            "system",
            "screen_off_timeout",
            MAX_SCREEN_TIMEOUT_MS,
        )
        if _android_setting(
            self.adb, "global", "stay_on_while_plugged_in"
        ) != requested_stay_awake:
            raise GateError("Android USB stay-awake guard was not applied")
        if _android_setting(
            self.adb, "system", "screen_off_timeout"
        ) != MAX_SCREEN_TIMEOUT_MS:
            raise GateError("Android maximum screen timeout was not applied")
        state = _android_power_state(self.adb)
        if state["secure_keyguard_showing"]:
            raise KeyguardGateError("secure Android keyguard appeared")
        if not state["usb_powered"]:
            raise GateError("Android device is not connected to USB power")
        if not state["interactive"] or not state["screen_on"]:
            _adb_output(self.adb, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
            time.sleep(0.2)
            state = _android_power_state(self.adb)
        if state["secure_keyguard_showing"]:
            raise KeyguardGateError("secure Android keyguard appeared")
        if not state["interactive"] or not state["screen_on"]:
            raise GateError("Android device did not remain interactive with its screen on")
        self.guarded = {
            **state,
            "stay_on_while_plugged_in": requested_stay_awake,
            "screen_off_timeout": MAX_SCREEN_TIMEOUT_MS,
        }
        self._thread = threading.Thread(
            target=self._monitor,
            name="wor216-android-power-watchdog",
            daemon=True,
        )
        self._thread.start()

    def _monitor(self) -> None:
        while not self._stop.wait(POWER_WATCHDOG_INTERVAL_SECONDS):
            try:
                state = _android_power_state(self.adb)
                if state["secure_keyguard_showing"]:
                    raise KeyguardGateError("secure Android keyguard appeared")
                if not state["usb_powered"]:
                    raise GateError("Android USB power was disconnected")
                if not state["interactive"] or not state["screen_on"]:
                    _adb_output(
                        self.adb,
                        "shell",
                        "input",
                        "keyevent",
                        "KEYCODE_WAKEUP",
                    )
                    time.sleep(0.2)
                    state = _android_power_state(self.adb)
                    if state["secure_keyguard_showing"]:
                        raise KeyguardGateError("secure Android keyguard appeared")
                    if not state["interactive"] or not state["screen_on"]:
                        raise GateError("Android display could not be kept awake")
            except Exception as exc:
                self._failure = exc
                return

    def check(self) -> None:
        if self._failure is not None:
            raise self._failure

    def restore(self) -> None:
        if self._restore_complete:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        errors: list[Exception] = []
        if self.previous_screen_timeout is not None:
            try:
                _restore_android_setting(
                    self.adb,
                    "system",
                    "screen_off_timeout",
                    self.previous_screen_timeout,
                )
            except Exception as exc:
                errors.append(exc)
        if self.previous_stay_awake is not None:
            try:
                _restore_android_setting(
                    self.adb,
                    "global",
                    "stay_on_while_plugged_in",
                    self.previous_stay_awake,
                )
            except Exception as exc:
                errors.append(exc)
        if not errors and self.before is not None:
            self.restored = {
                "stay_on_while_plugged_in": _android_setting(
                    self.adb, "global", "stay_on_while_plugged_in"
                ),
                "screen_off_timeout": _android_setting(
                    self.adb, "system", "screen_off_timeout"
                ),
            }
            self._restore_complete = True
        if errors:
            raise errors[0]
        self.check()


def _wait_for_process_with_power_guard(
    process: subprocess.Popen[str],
    *,
    timeout: float,
    power_guard: _AndroidPowerGuard,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        power_guard.check()
        code = process.poll()
        if code is not None:
            return code
        time.sleep(0.2)
    raise subprocess.TimeoutExpired(process.args, timeout)


def _verify_release_apk(
    apk: Path,
    *,
    aapt2: Path,
    apksigner: Path,
    expected_sha256: str,
    expected_version_name: str,
    expected_version_code: str,
    expected_certificate_sha256: str,
) -> VerifiedReleaseApk:
    resolved = apk.resolve()
    if not resolved.is_file() or resolved.name != "app-release.apk":
        raise GateError("official release APK is missing or has an unexpected name")
    if not aapt2.is_file() or not apksigner.is_file():
        raise GateError("explicit Android APK inspection tools are required")
    actual_sha256 = _sha256(resolved)
    if not hmac.compare_digest(actual_sha256, expected_sha256.lower()):
        raise GateError("official release APK SHA-256 changed")
    badging = _run_checked([str(aapt2), "dump", "badging", str(resolved)])
    package = re.search(
        r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']*)'",
        badging,
        flags=re.MULTILINE,
    )
    if not package or package.group(1) != PUBLIC_PACKAGE:
        raise GateError("release APK package id is not space.osia.app")
    if package.group(2) != expected_version_code:
        raise GateError("release APK version code changed")
    if package.group(3) != expected_version_name:
        raise GateError("release APK version name changed")
    if re.search(r"(?m)^application-debuggable\b", badging):
        raise GateError("official release APK is debuggable")
    signature = _run_checked(
        [str(apksigner), "verify", "--verbose", "--print-certs", str(resolved)]
    )
    certificate = re.search(
        r"(?mi)^(?:Signer #1|V\d+ Signer): certificate SHA-256 digest:"
        r"\s*([0-9a-f]+)\s*$",
        signature,
    )
    if not certificate:
        raise GateError("official release APK certificate is unreadable")
    certificate_sha256 = certificate.group(1).lower()
    if not hmac.compare_digest(
        certificate_sha256, expected_certificate_sha256.lower()
    ):
        raise GateError("official release APK signing certificate changed")
    return VerifiedReleaseApk(
        path=resolved,
        sha256=actual_sha256,
        version_name=package.group(3),
        version_code=package.group(2),
        certificate_sha256=certificate_sha256,
    )


def _release_install_args(release: VerifiedReleaseApk) -> tuple[str, str, str]:
    if not release.path.is_file() or not hmac.compare_digest(
        _sha256(release.path), release.sha256
    ):
        raise GateError("verified official release APK bytes changed before install")
    return "install", "-r", str(release.path)


def _reinstall_verified_release(adb: Path, release: VerifiedReleaseApk) -> None:
    _adb_output(adb, *_release_install_args(release))
    installed = _installed_release_version(adb)
    if installed != (release.version_name, release.version_code):
        raise GateError("official release package/version was not restored")


def _production_snapshot(pc_repo: Path, worker_repo: Path, adb: Path) -> dict:
    private_dir = Path.home() / ".osia"
    private_stat = private_dir.stat() if private_dir.is_dir() else None
    config = pc_repo / "config"
    protected = {
        "pc_reconnect": pc_repo / "osia" / "mcp_client" / "reconnect.py",
        "pc_autonomous_ws": pc_repo / "tools" / "test_autonomous_ws.py",
        "pc_agents": pc_repo / "AGENTS.md",
        "pc_onboarding": config / "onboarding.json",
    }
    config_files = {
        name: config / name
        for name in (
            "settings.json",
            "mobile_token.txt",
            "api_keys.json",
            "oauth_tokens.json",
            "onboarding.json",
        )
    }
    return {
        "private_data": {
            "exists": private_stat is not None,
            "mtime_ns": private_stat.st_mtime_ns if private_stat else None,
        },
        "conversations": _conversation_inventory(private_dir / "conversations"),
        "config": {
            name: _file_fingerprint(path) for name, path in config_files.items()
        },
        "protected": {
            name: _file_fingerprint(path) for name, path in protected.items()
        },
        "mobile_websocket": _file_fingerprint(
            pc_repo.parent / "osia-mobile" / "lib" / "services" / "websocket_service.dart"
        ),
        "worker_branch": _run_checked(
            ["git", "-C", str(worker_repo), "branch", "--show-current"]
        ).strip(),
        "worker_head": _run_checked(
            ["git", "-C", str(worker_repo), "rev-parse", "HEAD"]
        ).strip(),
        "worker_status": _run_checked(
            ["git", "-C", str(worker_repo), "status", "--porcelain=v1", "--untracked-files=all"]
        ),
    }


def _copy_pc_source(pc_repo: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name == "__pycache__" or name.endswith(".pyc")}

    shutil.copytree(pc_repo / "osia", destination / "osia", ignore=ignore)


def _physical_android_devices(adb: Path) -> list[dict]:
    output = _adb_output(adb, "devices", "-l")
    devices = []
    for raw in output.splitlines()[1:]:
        line = raw.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2 or fields[1] != "device":
            continue
        attributes = dict(
            field.split(":", 1) for field in fields[2:] if ":" in field
        )
        if fields[0].startswith("emulator-") or attributes.get("model", "").lower().startswith("sdk"):
            continue
        devices.append({"id": fields[0], "model": attributes.get("model", "unknown")})
    return devices


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _control_request(port: int, secret: str, path: str, *, method: str = "GET") -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        headers={CONTROL_HEADER: secret},
    )
    with urllib.request.urlopen(request, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_server(port: int, secret: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            state = _control_request(port, secret, "/__wor216_gate__/state")
            if state.get("ready") is True:
                return
            last_error = "unexpected readiness payload"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.1)
    raise GateError(f"isolated server did not become ready ({last_error})")


def _redactor(*sensitive: str):
    raw_values = {value for value in sensitive if value}
    values = sorted(
        raw_values
        | {value.replace("\\", "/") for value in raw_values}
        | {value.replace("/", "\\") for value in raw_values},
        key=len,
        reverse=True,
    )
    private_path = re.compile(
        r"[A-Za-z]:[\\/]Users[\\/][^\s\"']+", re.IGNORECASE
    )

    def redact(line: str) -> str:
        result = line
        for value in values:
            result = result.replace(value, "<redacted>")
        return private_path.sub("<private-path>", result)

    return redact


def _capture_process_output(
    process: subprocess.Popen[str],
    log_path: Path,
    redact,
) -> tuple[list[str], threading.Thread]:
    lines: list[str] = []

    def consume() -> None:
        assert process.stdout is not None
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            for raw in process.stdout:
                safe = redact(raw.rstrip("\r\n"))
                lines.append(safe)
                log.write(safe + "\n")
                log.flush()

    thread = threading.Thread(target=consume, name="wor216-log-capture", daemon=True)
    thread.start()
    return lines, thread


def _stop_process_tree(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _safe_remove_root(root: Path, parent_temp: Path, run_id: str) -> None:
    validated = _validate_gate_root(root, parent_temp, run_id)
    removal_target = (
        "\\\\?\\" + str(validated) if os.name == "nt" else str(validated)
    )
    last_error: OSError | None = None
    for _attempt in range(61):
        try:
            shutil.rmtree(removal_target)
            last_error = None
            break
        except FileNotFoundError as exc:
            if not validated.exists():
                last_error = None
                break
            last_error = exc
            time.sleep(0.5)
        except OSError as exc:
            last_error = exc
            time.sleep(0.5)
    if last_error is not None:
        raise last_error
    if validated.exists():
        raise GateError("isolated temporary root remains after cleanup")


def _receipt_state(receipts: Path, name: str) -> dict:
    path = receipts / name
    if not path.is_file():
        raise GateError(f"missing sanitized gate receipt: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _changed_snapshot_paths(before: object, after: object, prefix: str = "") -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        changed: list[str] = []
        for key in sorted(set(before) | set(after)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                changed.append(child)
            else:
                changed.extend(
                    _changed_snapshot_paths(before[key], after[key], child)
                )
        return changed
    if before != after:
        return [prefix or "root"]
    return []


def _assert_gate_receipts(receipts: Path) -> dict:
    seed = _receipt_state(receipts, "seed.json")
    mobile = _receipt_state(receipts, "mobile_commit.json")
    retry = _receipt_state(receipts, "retry.json")
    deletion = _receipt_state(receipts, "deletion.json")
    if seed.get("revision") != 2 or len(seed.get("messages", [])) != 1:
        raise GateError("desktop seed receipt is not revision 2 with one message")
    if mobile.get("revision") != 3 or len(mobile.get("messages", [])) != 2:
        raise GateError("Mobile commit receipt is not revision 3 with two messages")
    if [item.get("role") for item in mobile["messages"]] != ["user", "user"]:
        raise GateError("canonical role ordering is not deterministic")
    if [item.get("sequence") for item in mobile["messages"]] != [2, 3]:
        raise GateError("canonical commit sequence is not 2 then 3")
    message_ids = [item.get("message_id") for item in mobile["messages"]]
    if not all(message_ids) or len(set(message_ids)) != 2:
        raise GateError("canonical message ids are missing or duplicated")
    if retry.get("before") != retry.get("after") or retry["after"] != mobile:
        raise GateError("idempotent retry changed canonical state")
    if retry.get("broadcasts_before") != retry.get("broadcasts_after"):
        raise GateError("idempotent retry emitted an extra canonical broadcast")
    if deletion.get("before") != mobile or deletion.get("after") is not None:
        raise GateError("canonical deletion receipt is inconsistent")
    return {
        "conversation_id": mobile.get("id"),
        "desktop_revision": seed.get("revision"),
        "mobile_revision": mobile.get("revision"),
        "message_sequences": [2, 3],
        "idempotent_revision_unchanged": True,
        "idempotent_broadcast_unchanged": True,
        "deleted": True,
    }


def _flutter_release_test_build_command(
    flutter: Path,
    *,
    defines: Path,
) -> list[str]:
    return [
        str(flutter),
        "--suppress-analytics",
        "build",
        "apk",
        "--release",
        "--no-pub",
        "--target=integration_test/wor216_isolated_canonical_sync_test.dart",
        f"--dart-define-from-file={defines}",
    ]


def _strip_dev_plugin_from_release_registrant(mobile_repo: Path) -> Path:
    registrant = (
        mobile_repo
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "io"
        / "flutter"
        / "plugins"
        / "GeneratedPluginRegistrant.java"
    )
    if not registrant.is_file():
        raise GateError("generated Android plugin registrant is missing")
    source = registrant.read_text(encoding="utf-8")
    if source.count(INTEGRATION_REGISTRANT_BLOCK) != 1:
        raise GateError("integration_test registrant block is not exact")
    registrant.write_text(
        source.replace(INTEGRATION_REGISTRANT_BLOCK, "", 1),
        encoding="utf-8",
        newline="\n",
    )
    return registrant


def _run_dry_checks(pc_repo: Path, mobile_repo: Path) -> None:
    required = (
        pc_repo / "tools" / "wor216_isolated_mobile_gate.py",
        mobile_repo / "integration_test" / "wor216_isolated_canonical_sync_test.dart",
        mobile_repo / "lib" / "services" / "websocket_service.dart",
    )
    if not all(path.is_file() for path in required):
        raise GateError("required gate files are missing")
    build = (mobile_repo / "android" / "app" / "build.gradle.kts").read_text(
        encoding="utf-8"
    )
    if 'applicationId = "space.osia.app"' not in build:
        raise GateError("public application id changed")
    pubspec = (mobile_repo / "pubspec.yaml").read_text(encoding="utf-8")
    if "integration_test:" not in pubspec:
        raise GateError("integration_test SDK dependency is missing")
    command = _flutter_release_test_build_command(
        Path("flutter"),
        defines=Path("temporary-defines.json"),
    )
    if "--release" not in command or command[2:4] != ["build", "apk"]:
        raise GateError("physical Flutter command is not a release APK build")
    if "--flavor" in command or "--debug" in command:
        raise GateError("physical Flutter command uses an isolated application id")
    rejected = Path(tempfile.gettempdir()).resolve() / "not-a-wor216-gate-root"
    try:
        _validate_gate_root(rejected, Path(tempfile.gettempdir()), "dry-run")
    except GateError:
        pass
    else:
        raise GateError("unsafe non-gate root was accepted")
    print("WOR216_GATE_DRY_RUN_PASS")


def _serve(args: argparse.Namespace) -> int:
    if os.environ.get("OSIA_WOR216_GATE_CHILD") != "1":
        raise GateError("internal server mode requires the child-only marker")
    parent_temp = Path(os.environ["OSIA_WOR216_PARENT_TEMP"])
    root = _validate_gate_root(Path(args.root), parent_temp, args.run_id)
    source = (root / "pc-source").resolve()
    if not _is_within(source, root) or not (source / "osia").is_dir():
        raise GateError("temporary PC source is missing")
    sys.path.insert(0, str(source))

    from fastapi import HTTPException, Request
    import uvicorn
    from osia import conversations
    from osia.server import server

    # With postponed annotations, FastAPI resolves endpoint annotations from
    # module globals even though the import above is intentionally child-local.
    globals()["_GateRequest"] = Request

    imported_server = Path(server.__file__).resolve()
    if not _is_within(imported_server, source):
        raise GateError("server did not import from the isolated source copy")
    guarded_paths = (
        Path(conversations.CONVERSATIONS_DIR),
        Path(server.CONFIG_DIR),
        Path(server.TOKEN_FILE),
        Path(server.UPLOAD_DIR),
        Path(server.ATTACHMENT_UPLOAD_DIR),
    )
    if not all(_is_within(path, root) for path in guarded_paths):
        raise GateError("a server data path escaped the isolated root")

    # The gate owns no scheduler, connector, browser, agent, provider or tool.
    # WebSocket routes and their production mutation/broadcast helpers remain
    # unchanged; only unsafe lifecycle owners and provider execution are absent.
    server.app.router.on_startup.clear()
    server.app.router.on_shutdown.clear()
    server._pending_cron_notifications = lambda: []

    async def idle_heartbeat(_channel):
        await server.asyncio.Event().wait()

    server._heartbeat_push_loop = idle_heartbeat
    receipts = root / "receipts"
    canonical_broadcasts = 0
    original_send_conversation_frame = server._send_conversation_frame

    async def audited_send_conversation_frame(frame, **kwargs):
        nonlocal canonical_broadcasts
        if frame.get("type") in {
            "conversation_history",
            "conversations",
            "conversation_deleted",
        }:
            canonical_broadcasts += 1
        return await original_send_conversation_frame(frame, **kwargs)

    server._send_conversation_frame = audited_send_conversation_frame

    def snapshot(conv_id: str) -> dict | None:
        conv = conversations.get_conversation(conv_id)
        if conv is None:
            return None
        return {
            "id": str(conv.get("id") or ""),
            "revision": int(conv.get("revision") or 0),
            "operation_id": str(conv.get("last_operation_id") or ""),
            "messages": [
                {
                    "role": str(item.get("role") or ""),
                    "message_id": str(item.get("_message_id") or ""),
                    "operation_id": str(item.get("_operation_id") or ""),
                    "request_id": str(
                        item.get("_request_id")
                        or item.get("_client_request_id")
                        or ""
                    ),
                    "turn_id": str(item.get("_turn_id") or ""),
                    "sequence": int(item.get("_sequence") or 0),
                }
                for item in conv.get("messages", [])
                if isinstance(item, dict)
            ],
        }

    async def no_provider_turn(
        channel,
        text,
        history,
        active_conv_id,
        *,
        turn_meta=None,
        **_kwargs,
    ):
        persisted = server._persist_accepted_user_message(
            active_conv_id,
            (turn_meta or {}).get("persisted_user_content", text),
            turn_meta,
        )
        if not persisted:
            raise GateError("Mobile user commit was rejected")
        await server._broadcast_committed_receipt(
            active_conv_id, (turn_meta or {}).get("_user_commit")
        )
        turn_meta["persisted"] = True
        terminal = server._turn_status_frame(turn_meta, "done")
        terminal["persisted"] = True
        await channel.send_json(terminal)
        canonical = snapshot(active_conv_id)
        _write_json(receipts / "mobile_commit.json", canonical)
        stored = conversations.get_conversation(active_conv_id) or {}
        return list(stored.get("messages") or []), active_conv_id

    server._run_agent_and_respond = no_provider_turn

    original_replay = server._send_persisted_turn_for_request

    async def audited_replay(channel, conv_id, request_id, **kwargs):
        before = snapshot(conv_id)
        broadcasts_before = canonical_broadcasts
        handled = await original_replay(
            channel, conv_id, request_id, **kwargs
        )
        if handled and before is not None:
            _write_json(
                receipts / "retry.json",
                {
                    "before": before,
                    "after": snapshot(conv_id),
                    "broadcasts_before": broadcasts_before,
                    "broadcasts_after": canonical_broadcasts,
                },
            )
        return handled

    server._send_persisted_turn_for_request = audited_replay
    original_delete = conversations.delete_conversation

    def audited_delete(conv_id: str) -> bool:
        before = snapshot(conv_id)
        deleted = original_delete(conv_id)
        if before is not None:
            _write_json(
                receipts / "deletion.json",
                {"before": before, "after": snapshot(conv_id)},
            )
        return deleted

    conversations.delete_conversation = audited_delete
    control_secret = (root / "control.secret").read_text(encoding="utf-8").strip()
    gate_state: dict[str, str] = {"conversation_id": ""}

    def authorize(request: _GateRequest) -> None:
        host = request.client.host if request.client else ""
        supplied = request.headers.get(CONTROL_HEADER, "")
        if host not in {"127.0.0.1", "::1"} or not hmac.compare_digest(
            supplied, control_secret
        ):
            raise HTTPException(status_code=404)

    @server.app.get("/__wor216_gate__/state")
    async def gate_state_route(request: _GateRequest):
        authorize(request)
        return {
            "ready": True,
            "authenticated_clients": len(server._WS_CLIENTS),
            "seeded": bool(gate_state["conversation_id"]),
        }

    @server.app.post("/__wor216_gate__/seed")
    async def gate_seed_route(request: _GateRequest):
        authorize(request)
        if len(server._WS_CLIENTS) != 1:
            raise HTTPException(status_code=409, detail="one client required")
        if gate_state["conversation_id"]:
            raise HTTPException(status_code=409, detail="already seeded")
        conv = conversations.create_conversation(title=GATE_TITLE)
        gate_state["conversation_id"] = conv["id"]
        await server._broadcast_canonical_conversation_change(
            conv["id"],
            revision=int(conv.get("revision") or 0),
            operation_id=str(conv.get("last_operation_id") or ""),
        )
        receipt = conversations.commit_message(
            conv["id"],
            "user",
            DESKTOP_MESSAGE,
            metadata={
                "_conversation_id": conv["id"],
                "_request_id": "req_wor216_gate_desktop",
                "_client_request_id": "req_wor216_gate_desktop",
                "_turn_id": "turn_wor216_gate_desktop",
                "_message_id": "msg_wor216_gate_desktop",
                "_operation_id": "op_wor216_gate_desktop",
            },
        )
        if not receipt or not receipt.get("committed"):
            raise GateError("desktop gate commit failed")
        await server._broadcast_committed_receipt(conv["id"], receipt)
        canonical = snapshot(conv["id"])
        _write_json(receipts / "seed.json", canonical)
        return {
            "seeded": True,
            "revision": canonical["revision"],
            "message_count": len(canonical["messages"]),
        }

    uvicorn.run(
        server.app,
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


def _run_gate(args: argparse.Namespace) -> int:
    started = time.monotonic()
    pc_repo = Path(args.pc_repo).resolve()
    mobile_repo = Path(args.mobile_repo).resolve()
    worker_repo = Path(args.worker_repo).resolve()
    adb = Path(args.adb).resolve()
    flutter = Path(args.flutter).resolve()
    aapt2 = Path(args.aapt2).resolve()
    apksigner = Path(args.apksigner).resolve()
    if not adb.is_file() or adb.name.lower() != "adb.exe":
        raise GateError("an explicit full adb.exe path is required")
    if not flutter.is_file():
        raise GateError("an explicit Flutter executable path is required")
    devices = _physical_android_devices(adb)
    if len(devices) != 1 or devices[0]["id"] != args.device_id:
        raise GateError("exactly one matching authorized physical Android device is required")
    power_guard = _AndroidPowerGuard(adb)
    parent_temp = Path(tempfile.gettempdir()).resolve()
    root: Path | None = None
    run_id = ""
    try:
        power_guard.enable()
        expected_release_path = (
            mobile_repo
            / "build"
            / "app"
            / "outputs"
            / "flutter-apk"
            / "app-release.apk"
        ).resolve()
        requested_release_path = Path(args.release_apk).resolve()
        if requested_release_path != expected_release_path:
            raise GateError("release APK is not the current Mobile worktree output")
        original_release = _verify_release_apk(
            requested_release_path,
            aapt2=aapt2,
            apksigner=apksigner,
            expected_sha256=args.release_sha256,
            expected_version_name=args.release_version_name,
            expected_version_code=args.release_version_code,
            expected_certificate_sha256=args.release_cert_sha256,
        )
        if _installed_release_version(adb) != (
            original_release.version_name,
            original_release.version_code,
        ):
            raise GateError("the verified official release is not installed")
        if any(_package_path(adb, package) for package in FORBIDDEN_PACKAGES):
            raise GateError("a forbidden debug/test application package is installed")
        power_guard.check()
        reverse_before = _adb_output(adb, "reverse", "--list").strip()
        if f"tcp:{DEVICE_PORT}" in reverse_before:
            raise GateError("device tcp:8765 already has a reverse mapping")

        root = Path(tempfile.mkdtemp(prefix=GATE_PREFIX, dir=parent_temp)).resolve()
        run_id = secrets.token_hex(16)
        _write_json(root / GATE_SENTINEL, {"schema": 1, "run_id": run_id})
        token = secrets.token_hex(10)
        control_secret = secrets.token_urlsafe(32)
        port = _reserve_port()
        production_before = _production_snapshot(pc_repo, worker_repo, adb)
    except Exception:
        if root is not None and run_id:
            _safe_remove_root(root, parent_temp, run_id)
        power_guard.restore()
        raise
    assert root is not None
    server_process: subprocess.Popen[str] | None = None
    flutter_process: subprocess.Popen[str] | None = None
    logcat_process: subprocess.Popen[str] | None = None
    server_thread: threading.Thread | None = None
    flutter_thread: threading.Thread | None = None
    logcat_thread: threading.Thread | None = None
    server_lines: list[str] = []
    flutter_lines: list[str] = []
    logcat_lines: list[str] = []
    reverse_added = False
    retained_release: VerifiedReleaseApk | None = None
    result: dict | None = None
    cleanup_errors: list[str] = []
    primary_error: Exception | None = None
    try:
        power_guard.check()
        (root / "home" / ".osia").mkdir(parents=True)
        (root / "scratch").mkdir()
        (root / "logs").mkdir()
        (root / "receipts").mkdir()
        retained_path = root / "official-release" / "app-release.apk"
        retained_path.parent.mkdir()
        shutil.copy2(original_release.path, retained_path)
        retained_release = _verify_release_apk(
            retained_path,
            aapt2=aapt2,
            apksigner=apksigner,
            expected_sha256=original_release.sha256,
            expected_version_name=original_release.version_name,
            expected_version_code=original_release.version_code,
            expected_certificate_sha256=original_release.certificate_sha256,
        )
        (root / "control.secret").write_text(control_secret, encoding="utf-8")
        (root / "home" / ".osia" / "mobile_token.txt").write_text(
            token, encoding="utf-8"
        )
        _copy_pc_source(pc_repo, root / "pc-source")
        _write_json(
            root / "mobile-defines.json",
            {
                "WOR216_GATE_TOKEN": token,
                "WOR216_GATE_TITLE": GATE_TITLE,
                "WOR216_GATE_DESKTOP_MESSAGE": DESKTOP_MESSAGE,
                "WOR216_GATE_MOBILE_MESSAGE": MOBILE_MESSAGE,
            },
        )

        child_env = os.environ.copy()
        for name in tuple(child_env):
            if re.search(
                r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)",
                name,
                flags=re.IGNORECASE,
            ):
                child_env.pop(name, None)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            child_env.pop(name, None)
        child_env.update(
            {
                "OSIA_WOR216_GATE_CHILD": "1",
                "OSIA_WOR216_PARENT_TEMP": str(parent_temp),
                "PYTHONPATH": str(root / "pc-source"),
                "USERPROFILE": str(root / "home"),
                "APPDATA": str(root / "appdata"),
                "LOCALAPPDATA": str(root / "localappdata"),
                "TEMP": str(root / "scratch"),
                "TMP": str(root / "scratch"),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            }
        )
        server_process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve",
                "--root",
                str(root),
                "--run-id",
                run_id,
                "--port",
                str(port),
            ],
            cwd=str(root / "pc-source"),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        redact = _redactor(
            token,
            control_secret,
            str(root),
            str(pc_repo),
            str(mobile_repo),
        )
        server_lines, server_thread = _capture_process_output(
            server_process, root / "logs" / "server.log", redact
        )
        try:
            _wait_for_server(port, control_secret)
        except GateError as exc:
            time.sleep(0.2)
            server_tail = " | ".join(server_lines[-12:]) or (
                f"child_exit={server_process.poll()}"
            )
            raise GateError(f"{exc}: {server_tail}") from exc
        power_guard.check()
        _adb_output(adb, "reverse", f"tcp:{DEVICE_PORT}", f"tcp:{port}")
        reverse_added = True
        reverse_live = _adb_output(adb, "reverse", "--list")
        if f"tcp:{DEVICE_PORT} tcp:{port}" not in reverse_live:
            raise GateError("Android reverse mapping was not installed")

        flutter_env = os.environ.copy()
        for name in tuple(flutter_env):
            keep_release_signing = name in {
                "OSIA_ANDROID_KEYSTORE_PATH",
                "OSIA_ANDROID_KEYSTORE_PASSWORD",
                "OSIA_ANDROID_KEY_ALIAS",
                "OSIA_ANDROID_KEY_PASSWORD",
            }
            if not keep_release_signing and re.search(
                r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)",
                name,
                flags=re.IGNORECASE,
            ):
                flutter_env.pop(name, None)
        flutter_env["PUB_ENVIRONMENT"] = "wor216_isolated_gate"
        flutter_env["GRADLE_OPTS"] = (
            flutter_env.get("GRADLE_OPTS", "")
            + " -Dorg.gradle.offline=true -Dorg.gradle.daemon=false"
        ).strip()
        _run_checked(
            [str(flutter), "--suppress-analytics", "pub", "get", "--offline"],
            cwd=mobile_repo,
        )
        power_guard.check()
        _strip_dev_plugin_from_release_registrant(mobile_repo)
        flutter_process = subprocess.Popen(
            _flutter_release_test_build_command(
                flutter,
                defines=root / "mobile-defines.json",
            ),
            cwd=str(mobile_repo),
            env=flutter_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        flutter_lines, flutter_thread = _capture_process_output(
            flutter_process, root / "logs" / "flutter.log", redact
        )
        try:
            flutter_code = _wait_for_process_with_power_guard(
                flutter_process,
                timeout=360,
                power_guard=power_guard,
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError("release integration APK build exceeded six minutes") from exc
        if flutter_thread:
            flutter_thread.join(timeout=10)
        if flutter_code != 0:
            tail = " | ".join(flutter_lines[-80:])
            raise GateError(f"release integration APK build failed: {tail}")

        integration_path = expected_release_path
        integration_release = _verify_release_apk(
            integration_path,
            aapt2=aapt2,
            apksigner=apksigner,
            expected_sha256=_sha256(integration_path),
            expected_version_name=original_release.version_name,
            expected_version_code=original_release.version_code,
            expected_certificate_sha256=original_release.certificate_sha256,
        )
        logcat_process = subprocess.Popen(
            [str(adb), "logcat", "-v", "brief", "flutter:I", "*:S"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        logcat_lines, logcat_thread = _capture_process_output(
            logcat_process, root / "logs" / "device.log", redact
        )
        _adb_output(adb, *_release_install_args(integration_release))
        power_guard.check()
        if _installed_release_version(adb) != (
            integration_release.version_name,
            integration_release.version_code,
        ):
            raise GateError("release integration APK was not installed")
        _adb_output(adb, "shell", "am", "force-stop", PUBLIC_PACKAGE)
        _adb_output(
            adb,
            "shell",
            "am",
            "start",
            "-W",
            "-n",
            f"{PUBLIC_PACKAGE}/.MainActivity",
        )

        auth_deadline = time.monotonic() + 90
        while time.monotonic() < auth_deadline:
            power_guard.check()
            state = _control_request(port, control_secret, "/__wor216_gate__/state")
            if state.get("authenticated_clients") == 1:
                break
            time.sleep(0.2)
        else:
            device_tail = " | ".join(logcat_lines[-30:]) or "no device output"
            raise GateError(
                "physical release client did not authenticate within 90 seconds: "
                + device_tail
            )
        if not _package_path(adb, PUBLIC_PACKAGE):
            raise GateError("official release package was not observed")
        if any(_package_path(adb, package) for package in FORBIDDEN_PACKAGES):
            raise GateError("Flutter created a forbidden debug/test application package")
        seeded = _control_request(
            port, control_secret, "/__wor216_gate__/seed", method="POST"
        )
        if seeded != {"seeded": True, "revision": 2, "message_count": 1}:
            raise GateError("desktop seed acknowledgement was unexpected")

        assertion_deadline = time.monotonic() + 300
        while time.monotonic() < assertion_deadline:
            power_guard.check()
            if any(
                "WOR216_PHYSICAL_ASSERTIONS_PASS" in line
                for line in logcat_lines
            ):
                break
            if any("Some tests failed." in line for line in logcat_lines):
                device_tail = " | ".join(logcat_lines[-80:])
                raise GateError(
                    "physical release integration test failed: " + device_tail
                )
            time.sleep(0.2)
        else:
            device_tail = " | ".join(logcat_lines[-80:])
            raise GateError(
                "physical release assertion marker was not observed: " + device_tail
            )
        receipt_summary = _assert_gate_receipts(root / "receipts")
        auth_count = sum(
            "Mobile client authenticated." in line for line in server_lines
        )
        if auth_count < 2:
            raise GateError("server did not observe the physical reconnect")
        result = {
            "device_model": devices[0]["model"],
            "server_bind": "127.0.0.1",
            "pc_port_ephemeral": port != DEVICE_PORT,
            "official_release_package_observed": True,
            "physical_assertions": True,
            "authenticated_sessions": auth_count,
            **receipt_summary,
        }
    except Exception as exc:
        primary_error = exc
    finally:
        _stop_process_tree(flutter_process)
        if flutter_thread:
            flutter_thread.join(timeout=10)
        _stop_process_tree(logcat_process)
        if logcat_thread:
            logcat_thread.join(timeout=10)
        _stop_process_tree(server_process)
        if server_thread:
            server_thread.join(timeout=10)
        if reverse_added:
            try:
                _adb_output(adb, "reverse", "--remove", f"tcp:{DEVICE_PORT}")
            except Exception as exc:
                cleanup_errors.append(f"reverse removal: {type(exc).__name__}")
        try:
            if f"tcp:{DEVICE_PORT}" in _adb_output(adb, "reverse", "--list"):
                cleanup_errors.append("reverse mapping remains")
        except Exception as exc:
            cleanup_errors.append(f"reverse verification: {type(exc).__name__}")
        try:
            _adb_output(adb, "shell", "am", "force-stop", PUBLIC_PACKAGE)
            release_to_restore = retained_release or original_release
            if retained_release is not None:
                shutil.copy2(retained_release.path, original_release.path)
                release_to_restore = VerifiedReleaseApk(
                    path=original_release.path,
                    sha256=retained_release.sha256,
                    version_name=retained_release.version_name,
                    version_code=retained_release.version_code,
                    certificate_sha256=retained_release.certificate_sha256,
                )
            _reinstall_verified_release(adb, release_to_restore)
        except Exception as exc:
            cleanup_errors.append(f"official release reinstall: {type(exc).__name__}")
        try:
            _run_checked(
                [str(flutter), "--suppress-analytics", "pub", "get", "--offline"],
                cwd=mobile_repo,
            )
        except Exception as exc:
            cleanup_errors.append(f"generated registrant restore: {type(exc).__name__}")
        try:
            power_guard.restore()
        except Exception as exc:
            cleanup_errors.append(f"Android power guard restore: {type(exc).__name__}")
        try:
            if any(_package_path(adb, package) for package in FORBIDDEN_PACKAGES):
                cleanup_errors.append("forbidden debug/test application package remains")
        except Exception as exc:
            cleanup_errors.append(f"package isolation verification: {type(exc).__name__}")
        try:
            production_after = _production_snapshot(pc_repo, worker_repo, adb)
            if production_after != production_before:
                changed_paths = _changed_snapshot_paths(
                    production_before, production_after
                )
                cleanup_errors.append(
                    "production snapshot changed: " + ",".join(changed_paths[:20])
                )
        except Exception as exc:
            cleanup_errors.append(f"production verification: {type(exc).__name__}")
        try:
            _safe_remove_root(root, parent_temp, run_id)
        except Exception as exc:
            cleanup_errors.append(f"temporary root cleanup: {type(exc).__name__}")
    if cleanup_errors:
        detail = "; ".join(cleanup_errors)
        if primary_error is not None:
            raise GateError(f"{primary_error}; cleanup failure: {detail}") from primary_error
        raise GateError(detail)
    if primary_error is not None:
        raise primary_error
    if result is None:
        raise GateError("gate completed without a result")
    result.update(
        {
            "production_snapshot_unchanged": True,
            "official_release_reinstalled": True,
            "official_release_package": PUBLIC_PACKAGE,
            "official_release_sha256": original_release.sha256,
            "official_release_version_name": original_release.version_name,
            "official_release_version_code": original_release.version_code,
            "official_release_certificate_sha256": original_release.certificate_sha256,
            "reverse_removed": True,
            "usb_stay_awake_restored": True,
            "screen_timeout_restored": True,
            "android_power_before": power_guard.before,
            "android_power_guarded": power_guard.guarded,
            "android_power_restored": power_guard.restored,
            "forbidden_packages_absent": True,
            "server_process_stopped": server_process is None or server_process.poll() is not None,
            "flutter_process_stopped": flutter_process is None or flutter_process.poll() is not None,
            "device_log_process_stopped": logcat_process is None or logcat_process.poll() is not None,
            "temporary_root_removed": not root.exists(),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    )
    print("WOR216_GATE_RESULT=" + json.dumps(result, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root")
    parser.add_argument("--run-id")
    parser.add_argument("--port", type=int)
    parser.add_argument("--device-id")
    parser.add_argument("--adb")
    parser.add_argument("--flutter")
    parser.add_argument("--aapt2")
    parser.add_argument("--apksigner")
    parser.add_argument("--release-apk")
    parser.add_argument("--release-sha256")
    parser.add_argument("--release-version-name")
    parser.add_argument("--release-version-code")
    parser.add_argument("--release-cert-sha256")
    parser.add_argument(
        "--pc-repo", default=str(Path(__file__).resolve().parent.parent)
    )
    parser.add_argument("--mobile-repo", default=r"C:\Users\vertu\osia-mobile")
    parser.add_argument("--worker-repo", default=r"C:\Users\vertu\osia-worker")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.serve:
            if not args.root or not args.run_id or not args.port:
                raise GateError("internal server arguments are incomplete")
            return _serve(args)
        pc_repo = Path(args.pc_repo).resolve()
        mobile_repo = Path(args.mobile_repo).resolve()
        if args.dry_run:
            _run_dry_checks(pc_repo, mobile_repo)
            return 0
        required = (
            args.device_id,
            args.adb,
            args.flutter,
            args.aapt2,
            args.apksigner,
            args.release_apk,
            args.release_sha256,
            args.release_version_name,
            args.release_version_code,
            args.release_cert_sha256,
        )
        if not all(required):
            raise GateError("device, Android tools and verified release metadata are required")
        return _run_gate(args)
    except GateError as exc:
        print(f"WOR216_GATE_ERROR={type(exc).__name__}:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
