"""Background updates for the private Codex runtime used by OSIA."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

from osia.config import get_setting
from osia.runtime import is_frozen
from .app_server_client import (
    AppServerClient,
    CURRENT_RUNTIME_VERSION,
    _SAFE_CONFIG,
    _UPDATE_READY_FILE,
    _UPDATE_VERSION_PATTERN,
    _probe_executable,
    _runtime_base_dir,
    _updated_runtime_dir,
    _version_tuple,
)


_REPOSITORY = "https://github.com/openai/codex"
_LATEST_RELEASE = "https://api.github.com/repos/openai/codex/releases/latest"
_DAY = 24 * 60 * 60
_MAX_ARCHIVE_SIZE = 512 * 1024**2
_MAX_EXTRACTED_SIZE = 2 * 1024**3
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Release:
    version: str
    target: str
    url: str
    digest: str
    size: int


def _platform_target() -> str | None:
    arch = {
        "amd64": "x86_64", "x86_64": "x86_64",
        "arm64": "aarch64", "aarch64": "aarch64",
    }.get(platform.machine().lower())
    system = {
        "win32": "pc-windows-msvc",
        "darwin": "apple-darwin",
        "linux": "unknown-linux-musl",
    }.get(sys.platform)
    return f"{arch}-{system}" if arch and system else None


class _HTTPSOnlyRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise ValueError("Insecure Codex release redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_release_url(url: str):
    opener = build_opener(_HTTPSOnlyRedirect())
    return opener.open(Request(url, headers={"User-Agent": "OSIA-Codex-Update"}), timeout=20)


def _latest_release(target: str) -> _Release | None:
    with _open_release_url(_LATEST_RELEASE) as response:
        payload = json.loads(response.read(2 * 1024**2))
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag = str(payload.get("tag_name") or "")
    if not tag.startswith("rust-v") or not _UPDATE_VERSION_PATTERN.fullmatch(tag[6:]):
        return None
    archive = f"codex-package-{target}.tar.gz"
    for asset in payload.get("assets", []):
        if asset.get("name") != archive or asset.get("state") != "uploaded":
            continue
        digest = str(asset.get("digest") or "")
        size = asset.get("size")
        url = f"{_REPOSITORY}/releases/download/{tag}/{archive}"
        if (
            asset.get("browser_download_url") == url
            and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            and type(size) is int and 0 < size <= _MAX_ARCHIVE_SIZE
        ):
            return _Release(tag[6:], target, url, digest[7:], size)
    return None


def _download(release: _Release, archive: Path, stop: threading.Event) -> bool:
    size = 0
    digest = hashlib.sha256()
    with _open_release_url(release.url) as response, archive.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            if stop.is_set():
                return False
            size += len(chunk)
            if size > release.size:
                raise ValueError("Codex archive exceeds its published size")
            digest.update(chunk)
            output.write(chunk)
    if size != release.size or digest.hexdigest() != release.digest:
        raise ValueError("Codex archive does not match its published digest")
    return True


def _extract(archive: Path, package: Path, stop: threading.Event) -> bool:
    package.mkdir()
    size = 0
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            if stop.is_set():
                return False
            name = PurePosixPath(member.name)
            if (
                name.is_absolute() or not name.parts or ".." in name.parts
                or ":" in member.name or "\\" in member.name
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("Unsafe entry in Codex archive")
            size += member.size
            if size > _MAX_EXTRACTED_SIZE:
                raise ValueError("Codex archive exceeds the extraction limit")
            path = package.joinpath(*name.parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as data, path.open("xb") as output:
                    shutil.copyfileobj(data, output, length=1024 * 1024)
                if member.mode & 0o111:
                    path.chmod(0o755)
    return True


def _package_executable(package: Path, release: _Release) -> Path:
    metadata = json.loads((package / "codex-package.json").read_text(encoding="utf-8"))
    suffix = ".exe" if release.target.endswith("-windows-msvc") else ""
    entrypoint = f"bin/codex{suffix}"
    exec_server = f"bin/codex-exec-server{suffix}"
    expected = {
        "layoutVersion": 1, "version": release.version, "target": release.target,
        "variant": "codex", "entrypoint": entrypoint, "execServer": exec_server,
        "resourcesDir": "codex-resources", "pathDir": "codex-path",
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError("Unexpected Codex package layout")
    required = [entrypoint, exec_server, f"bin/codex-code-mode-host{suffix}", f"codex-path/rg{suffix}"]
    if suffix:
        required.extend((
            "codex-resources/codex-command-runner.exe",
            "codex-resources/codex-windows-sandbox-setup.exe",
        ))
    elif release.target.endswith("-linux-musl"):
        required.append("codex-resources/bwrap")
    for name in required:
        path = package / name
        if not path.is_file() or (not suffix and not os.access(path, os.X_OK)):
            raise ValueError("Incomplete Codex package")
    if not (package / "codex-resources").is_dir():
        raise ValueError("Missing Codex resources")
    return package / entrypoint


class _CompatibilityProbe(AppServerClient):
    """Run the production protocol against a disposable, signed-out home."""

    def _resolve_runtime(self) -> tuple[Path, str]:
        assert self._runtime_executable is not None and self._runtime_version_override is not None
        return self._runtime_executable, self._runtime_version_override

    def _ensure_layout(self) -> None:
        for directory in (self.home_dir, self.workspace_dir, self.temp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        (self.home_dir / "config.toml").write_text(_SAFE_CONFIG, encoding="utf-8")


def _verify_runtime(executable: Path, version: str, scratch: Path, stop: threading.Event) -> bool:
    expected = f"codex-cli {version}"
    if _probe_executable(executable) != expected:
        raise ValueError("Unexpected Codex executable version")
    for direct_visual in (False, True):
        if stop.is_set():
            return False
        client = _CompatibilityProbe(
            base_dir=scratch / ("visual" if direct_visual else "chat"),
            runtime_executable=executable,
            runtime_version_override=expected,
            disable_code_mode_host=direct_visual,
            request_timeout=15,
        )
        try:
            status = client.get_account_status()
            models = client.list_models()
            if status["connected"] or not models:
                raise ValueError("Unexpected isolated Codex account or model list")
            client.start_or_resume_conversation(
                thread_id=None, model=models[0]["id"],
                base_instructions="", developer_instructions="",
            )
        finally:
            client.shutdown()
    return True


def _install_release(root: Path, release: _Release, stop: threading.Event) -> bool:
    destination = root / release.version
    if destination.exists() or stop.is_set():
        return False
    # Same filesystem as the final directory, so a partial update is never visible.
    with tempfile.TemporaryDirectory(prefix=".download-", dir=root) as temp:
        scratch = Path(temp)
        archive = scratch / "package.tar.gz"
        package = scratch / "package"
        if not _download(release, archive, stop) or not _extract(archive, package, stop):
            return False
        executable = _package_executable(package, release)
        if not _verify_runtime(executable, release.version, scratch, stop) or stop.is_set():
            return False
        (package / _UPDATE_READY_FILE).touch(exist_ok=False)
        try:
            package.rename(destination)
        except OSError:
            if (destination / _UPDATE_READY_FILE).is_file():
                return False
            raise
    return True


def _check_when_due(base: Path, target: str, stop: threading.Event) -> float:
    root = _updated_runtime_dir(base)
    if root.resolve() != base.resolve() / "runtime" / "updates":
        raise ValueError("Codex update directory points outside its managed location")
    root.mkdir(parents=True, exist_ok=True)
    stamp = root / ".last-check"
    now = time.time()
    try:
        last_check = float(stamp.read_text(encoding="ascii"))
    except (FileNotFoundError, ValueError):
        last_check = 0.0
    if math.isfinite(last_check) and 0 <= now - last_check < _DAY:
        return _DAY - (now - last_check)

    with tempfile.NamedTemporaryFile(dir=root, delete=False, mode="w", encoding="ascii") as tmp:
        tmp.write(str(now))
    try:
        Path(tmp.name).replace(stamp)
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    release = _latest_release(target)
    if release is None or stop.is_set():
        return _DAY
    candidate = _version_tuple(release.version)
    installed = _version_tuple(AppServerClient(base_dir=base).get_runtime_version() or "")
    if candidate >= CURRENT_RUNTIME_VERSION and (installed is None or candidate > installed):
        if _install_release(root, release, stop):
            _LOG.info("Codex %s verified and ready for the next OSIA launch", release.version)
    return _DAY


def start_runtime_update_worker() -> threading.Event:
    """Start the desktop-owned daily check and return its nonblocking stop signal."""
    stop = threading.Event()
    target = _platform_target()
    if target is None or (not is_frozen() and os.environ.get("OSIA_CODEX_EXECUTABLE")):
        return stop

    def run() -> None:
        delay = 15.0
        while not stop.wait(delay):
            delay = _DAY
            try:
                if get_setting("chatgpt_account_feature_enabled", True):
                    delay = _check_when_due(_runtime_base_dir(), target, stop)
            except Exception as exc:
                _LOG.warning("Codex background update failed (%s)", type(exc).__name__)

    try:
        threading.Thread(target=run, name="osia-codex-update", daemon=True).start()
    except RuntimeError as exc:
        _LOG.warning("Codex background updater could not start (%s)", type(exc).__name__)
    return stop
