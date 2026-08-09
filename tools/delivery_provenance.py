#!/usr/bin/env python3
"""Read-only delivery provenance gate for the primary OSIA runtimes.

The gate never fetches, pulls, resets, installs, stops, or starts anything.  It
only inspects the two primary Git checkouts, active OSIA source processes, the
primary Mobile release artifact, and the official Android package when a
physical device is connected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


OFFICIAL_ANDROID_PACKAGE = "space.osia.app"
FORBIDDEN_ANDROID_PACKAGES = (
    "space.osia.app.test",
    "space.osia.app.test.test",
)
_CLOCK_SKEW_SECONDS = 2.0
_ALLOWED_DIRTY_EXACT = {
    ".claude/settings.local.json",
    "AGENTS.md",
    "CLAUDE.md",
    "osia/mcp_client/reconnect.py",
    "tools/test_autonomous_ws.py",
}
_ALLOWED_DIRTY_PREFIXES = ("config/",)
_RUNTIME_SCRIPTS = {
    "desktop": Path("osia/main.py"),
    "server": Path("osia/server/run_server.py"),
    "server_supervisor": Path("windows/run_service.py"),
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "error"


@dataclass(frozen=True)
class RuntimeRecord:
    kind: str
    pid: int
    source_path: str
    started_at: float


class ProbeError(RuntimeError):
    pass


def _run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [str(value) for value in args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"command unavailable: {args[0]}") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise ProbeError(message or f"command failed: {args[0]}")
    return completed


def _git(repo: Path, *args: str) -> str:
    return _run(("git", "-C", str(repo), *args)).stdout.strip()


def _status_paths(raw: str) -> list[str]:
    """Extract paths from ``git status --porcelain=v1 -z`` output."""

    fields = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            continue
        status = entry[:2]
        paths.append(entry[3:].replace("\\", "/"))
        if "R" in status or "C" in status:
            if index < len(fields) and fields[index]:
                paths.append(fields[index].replace("\\", "/"))
                index += 1
    return sorted(set(paths))


def _dirty_is_allowed(path: str) -> bool:
    return path in _ALLOWED_DIRTY_EXACT or path.startswith(
        _ALLOWED_DIRTY_PREFIXES
    )


def inspect_repo(
    repo: Path,
    *,
    label: str,
    expected_sha: str | None = None,
) -> tuple[dict, list[Check]]:
    repo = repo.resolve()
    checks: list[Check] = []
    result: dict = {"path": str(repo)}
    if not repo.is_dir():
        return result, [Check(f"{label}.checkout", False, "checkout is missing")]
    try:
        top = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
        head = _git(repo, "rev-parse", "HEAD")
        origin = _git(repo, "rev-parse", "origin/main")
        branch = _git(repo, "branch", "--show-current")
        reflog_epoch = float(_git(repo, "reflog", "-1", "--format=%ct", "HEAD"))
        status_raw = _run(
            ("git", "-C", str(repo), "status", "--porcelain=v1", "-z")
        ).stdout
    except (ProbeError, ValueError) as exc:
        result["error"] = str(exc)
        return result, [Check(f"{label}.git", False, "Git provenance unavailable")]

    dirty = _status_paths(status_raw)
    protected_dirty = [path for path in dirty if _dirty_is_allowed(path)]
    unapproved_dirty = [path for path in dirty if not _dirty_is_allowed(path)]
    result.update(
        {
            "head": head,
            "origin_main": origin,
            "branch": branch,
            "head_updated_at": reflog_epoch,
            "dirty_paths": dirty,
            "protected_dirty_paths": protected_dirty,
            "unapproved_dirty_paths": unapproved_dirty,
        }
    )
    checks.extend(
        (
            Check(f"{label}.primary_path", top == repo, f"top-level={top}"),
            Check(f"{label}.branch", branch == "main", f"branch={branch or '(detached)'}"),
            Check(
                f"{label}.head_matches_origin",
                head == origin,
                f"HEAD={head} origin/main={origin}",
            ),
            Check(
                f"{label}.unapproved_dirty",
                not unapproved_dirty,
                "none" if not unapproved_dirty else ", ".join(unapproved_dirty),
            ),
        )
    )
    checks.append(
        Check(
            f"{label}.protected_dirty",
            True,
            "none" if not protected_dirty else ", ".join(protected_dirty),
            severity="warning",
        )
    )
    if expected_sha:
        try:
            expected = _git(repo, "rev-parse", f"{expected_sha}^{{commit}}")
        except ProbeError:
            expected = "unresolved"
        result["expected_sha"] = expected
        checks.append(
            Check(
                f"{label}.expected_sha",
                expected == head,
                f"expected={expected} HEAD={head}",
            )
        )
    return result, checks


def _normal_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value.strip().strip('"')))


def collect_runtime_records() -> list[RuntimeRecord]:
    try:
        import psutil
    except ImportError as exc:
        raise ProbeError("psutil is unavailable") from exc

    records: list[RuntimeRecord] = []
    for process in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            arguments = [str(value) for value in (process.info["cmdline"] or [])]
            created = float(process.info["create_time"] or 0.0)
            pid = int(process.info["pid"])
        except (psutil.Error, TypeError, ValueError):
            continue
        for kind, relative in _RUNTIME_SCRIPTS.items():
            suffix = _normal_path(str(relative))
            source = next(
                (
                    argument.strip().strip('"')
                    for argument in arguments
                    if _normal_path(argument).endswith(suffix)
                ),
                None,
            )
            if source is not None:
                records.append(
                    RuntimeRecord(
                        kind=kind,
                        pid=pid,
                        source_path=str(Path(source).resolve()),
                        started_at=created,
                    )
                )
                break
    return records


def assess_runtime(
    records: Iterable[RuntimeRecord],
    *,
    pc_repo: Path,
    head_updated_at: float,
) -> list[Check]:
    records = list(records)
    expected = {
        kind: (pc_repo / relative).resolve()
        for kind, relative in _RUNTIME_SCRIPTS.items()
    }
    checks = [
        Check(
            "pc.runtime.desktop_present",
            any(record.kind == "desktop" for record in records),
            "active source desktop required",
        ),
        Check(
            "pc.runtime.server_present",
            any(record.kind in {"server", "server_supervisor"} for record in records),
            "active source mobile server required",
        ),
    ]
    for record in records:
        source = Path(record.source_path).resolve()
        checks.append(
            Check(
                f"pc.runtime.{record.kind}.{record.pid}.source",
                source == expected[record.kind],
                f"source={source}",
            )
        )
        checks.append(
            Check(
                f"pc.runtime.{record.kind}.{record.pid}.fresh",
                record.started_at + _CLOCK_SKEW_SECONDS >= head_updated_at,
                (
                    f"started_at={record.started_at:.3f} "
                    f"HEAD_updated_at={head_updated_at:.3f}"
                ),
            )
        )
    return checks


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_artifact(
    artifact: Path,
    *,
    mobile_repo: Path,
    head_updated_at: float,
) -> tuple[dict, list[Check]]:
    artifact = artifact.resolve()
    result = {"path": str(artifact)}
    if not artifact.is_file():
        return result, [Check("mobile.artifact.exists", False, "release APK is missing")]
    try:
        artifact.relative_to(mobile_repo.resolve())
        in_primary = True
    except ValueError:
        in_primary = False
    stat = artifact.stat()
    digest = _sha256(artifact)
    result.update(
        {
            "sha256": digest,
            "size": stat.st_size,
            "modified_at": stat.st_mtime,
        }
    )
    return result, [
        Check("mobile.artifact.exists", True, str(artifact)),
        Check(
            "mobile.artifact.primary_path",
            in_primary,
            f"artifact={artifact}",
        ),
        Check(
            "mobile.artifact.fresh",
            stat.st_mtime + _CLOCK_SKEW_SECONDS >= head_updated_at,
            (
                f"artifact_mtime={stat.st_mtime:.3f} "
                f"HEAD_updated_at={head_updated_at:.3f}"
            ),
        ),
    ]


def parse_adb_devices(output: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        match = re.match(r"^(\S+)\s+(\S+)(?:\s+(.*))?$", line)
        if match:
            devices.append(
                {
                    "serial": match.group(1),
                    "state": match.group(2),
                    "details": match.group(3) or "",
                }
            )
    return devices


def select_device(
    devices: Sequence[dict[str, str]], serial: str | None = None
) -> tuple[dict[str, str] | None, Check]:
    ready = [device for device in devices if device.get("state") == "device"]
    if serial:
        match = next((device for device in ready if device.get("serial") == serial), None)
        return match, Check(
            "mobile.device",
            match is not None,
            "selected device is connected" if match else "selected device is unavailable",
        )
    physical = [
        device
        for device in ready
        if not device.get("serial", "").startswith("emulator-")
        and "model:sdk_" not in device.get("details", "").lower()
    ]
    selected = physical[0] if len(physical) == 1 else None
    detail = (
        "one physical Android device connected"
        if selected
        else f"expected one physical Android device, found {len(physical)}"
    )
    return selected, Check("mobile.device", selected is not None, detail)


def compare_apk_hashes(
    artifact_sha256: str,
    artifact_size: int,
    installed_sha256: str,
    installed_size: int,
) -> Check:
    matches = (
        artifact_sha256 == installed_sha256 and artifact_size == installed_size
    )
    return Check(
        "mobile.installed_apk_matches_artifact",
        matches,
        (
            f"artifact={artifact_sha256}/{artifact_size} "
            f"installed={installed_sha256}/{installed_size}"
        ),
    )


def _find_adb(explicit: Path | None) -> Path | None:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        return candidate if candidate.is_file() else None
    located = shutil.which("adb")
    if located:
        return Path(located).resolve()
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidate = Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
            if candidate.is_file():
                return candidate.resolve()
    return None


def _adb_args(adb: Path, serial: str | None, *arguments: str) -> list[str]:
    result = [str(adb)]
    if serial:
        result.extend(("-s", serial))
    result.extend(arguments)
    return result


def _adb_text(adb: Path, serial: str | None, *arguments: str) -> str:
    return _run(_adb_args(adb, serial, *arguments), timeout=45.0).stdout.strip()


def _adb_package_paths(adb: Path, serial: str, package: str) -> list[str]:
    try:
        completed = subprocess.run(
            _adb_args(adb, serial, "shell", "pm", "path", package),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError("Android package probe unavailable") from exc
    output = completed.stdout.strip()
    error = completed.stderr.strip()
    if completed.returncode != 0 and (output or error):
        raise ProbeError(error or output)
    return [
        line.removeprefix("package:").strip()
        for line in output.splitlines()
        if line.strip()
    ]


def _hash_remote_apk(
    adb: Path, serial: str, remote_path: str
) -> tuple[str, int]:
    try:
        process = subprocess.Popen(
            _adb_args(adb, serial, "exec-out", "cat", remote_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ProbeError("adb APK stream unavailable") from exc
    assert process.stdout is not None
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    return_code = process.wait(timeout=120.0)
    if return_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ProbeError(detail or "installed APK could not be read")
    return digest.hexdigest(), size


def inspect_android(
    *,
    adb: Path | None,
    serial: str | None,
    artifact: dict,
) -> tuple[dict, list[Check]]:
    result: dict = {"package": OFFICIAL_ANDROID_PACKAGE}
    if adb is None:
        return result, [Check("mobile.adb", False, "adb is unavailable")]
    result["adb"] = str(adb)
    try:
        devices_output = _run((str(adb), "devices", "-l")).stdout
    except ProbeError as exc:
        return result, [Check("mobile.adb", False, str(exc))]
    device, device_check = select_device(parse_adb_devices(devices_output), serial)
    checks = [Check("mobile.adb", True, str(adb)), device_check]
    if device is None:
        return result, checks
    selected_serial = device["serial"]
    result["device_serial"] = selected_serial
    try:
        package_paths = _adb_package_paths(
            adb, selected_serial, OFFICIAL_ANDROID_PACKAGE
        )
        forbidden = {
            package: bool(_adb_package_paths(adb, selected_serial, package))
            for package in FORBIDDEN_ANDROID_PACKAGES
        }
    except ProbeError as exc:
        checks.append(Check("mobile.package_probe", False, str(exc)))
        return result, checks
    checks.append(
        Check(
            "mobile.official_package",
            bool(package_paths),
            "installed" if package_paths else "space.osia.app is not installed",
        )
    )
    checks.append(
        Check(
            "mobile.forbidden_packages_absent",
            not any(forbidden.values()),
            "none" if not any(forbidden.values()) else "debug/test package present",
        )
    )
    if not package_paths or "sha256" not in artifact:
        return result, checks
    base_apks = [path for path in package_paths if path.endswith("/base.apk")]
    if len(base_apks) != 1:
        checks.append(
            Check(
                "mobile.installed_base_apk",
                False,
                f"expected one base.apk, found {len(base_apks)}",
            )
        )
        return result, checks
    try:
        installed_sha, installed_size = _hash_remote_apk(
            adb, selected_serial, base_apks[0]
        )
    except (ProbeError, subprocess.TimeoutExpired) as exc:
        checks.append(Check("mobile.installed_apk_read", False, str(exc)))
        return result, checks
    result.update(
        {"installed_apk_sha256": installed_sha, "installed_apk_size": installed_size}
    )
    checks.append(
        compare_apk_hashes(
            str(artifact["sha256"]),
            int(artifact["size"]),
            installed_sha,
            installed_size,
        )
    )
    try:
        package_dump = _adb_text(
            adb, selected_serial, "shell", "dumpsys", "package", OFFICIAL_ANDROID_PACKAGE
        )
        version_name = re.search(r"\bversionName=([^\s]+)", package_dump)
        version_code = re.search(r"\bversionCode=(\d+)", package_dump)
        result["version_name"] = version_name.group(1) if version_name else ""
        result["version_code"] = version_code.group(1) if version_code else ""
    except ProbeError:
        pass
    return result, checks


def _report(checks: Sequence[Check], payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    **payload,
                    "checks": [asdict(check) for check in checks],
                    "ok": all(check.ok for check in checks if check.severity == "error"),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    for check in checks:
        marker = "PASS" if check.ok else "FAIL"
        if check.severity == "warning":
            marker = "WARN"
        print(f"[{marker}] {check.name}: {check.detail}")


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pc-repo", type=Path, default=root)
    parser.add_argument("--mobile-repo", type=Path, default=root.parent / "osia-mobile")
    parser.add_argument("--expected-pc-sha")
    parser.add_argument("--expected-mobile-sha")
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--adb", type=Path)
    parser.add_argument("--serial")
    parser.add_argument("--json", action="store_true")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    pc_repo = args.pc_repo.resolve()
    mobile_repo = args.mobile_repo.resolve()
    artifact_path = (
        args.apk.resolve()
        if args.apk is not None
        else mobile_repo / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
    )

    pc, pc_checks = inspect_repo(
        pc_repo, label="pc", expected_sha=args.expected_pc_sha
    )
    mobile, mobile_checks = inspect_repo(
        mobile_repo, label="mobile", expected_sha=args.expected_mobile_sha
    )
    checks = [*pc_checks, *mobile_checks]

    runtime_records: list[RuntimeRecord] = []
    if "head_updated_at" in pc:
        try:
            runtime_records = collect_runtime_records()
            checks.extend(
                assess_runtime(
                    runtime_records,
                    pc_repo=pc_repo,
                    head_updated_at=float(pc["head_updated_at"]),
                )
            )
        except ProbeError as exc:
            checks.append(Check("pc.runtime_probe", False, str(exc)))

    artifact: dict = {"path": str(artifact_path)}
    if "head_updated_at" in mobile:
        artifact, artifact_checks = inspect_artifact(
            artifact_path,
            mobile_repo=mobile_repo,
            head_updated_at=float(mobile["head_updated_at"]),
        )
        checks.extend(artifact_checks)

    android, android_checks = inspect_android(
        adb=_find_adb(args.adb), serial=args.serial, artifact=artifact
    )
    checks.extend(android_checks)
    payload = {
        "pc": pc,
        "mobile": mobile,
        "pc_runtime": [asdict(record) for record in runtime_records],
        "mobile_artifact": artifact,
        "android": android,
    }
    _report(checks, payload, as_json=args.json)
    return 0 if all(check.ok for check in checks if check.severity == "error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
