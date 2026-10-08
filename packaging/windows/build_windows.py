"""Build the self-contained OSIA Windows bundle and Inno Setup installer."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.version import PRODUCT_VERSION, parse_product_version
from osia.release_notes import (
    installer_release_notes,
    is_major_update,
    manifest_release_notes,
)
from osia.windows_release_policy import scan_release_payload


STAGE = ROOT / "build" / "windows"
GENERATED = STAGE / "generated"
MODEL_STAGE = STAGE / "models"
LEGAL_STAGE = STAGE / "legal"
DIST = ROOT / "dist" / "OSIA"
RELEASE = ROOT / "release" / "windows"
CODEX_RUNTIME_STAGE = STAGE / "runtimes" / "codex"
CODEX_RUNTIME_VERSION = "0.144.1"
CODEX_RUNTIME_ARCHIVE = "codex-package-x86_64-pc-windows-msvc.tar.gz"
CODEX_RUNTIME_SHA256 = "ce94e1fb84693d3f7332ceeab5be73e93de38725da4b82dff863cd2c795b4730"
CODEX_RUNTIME_URL = (
    "https://github.com/openai/codex/releases/download/"
    f"rust-v{CODEX_RUNTIME_VERSION}/{CODEX_RUNTIME_ARCHIVE}"
)
SOURCE_VERSION = PRODUCT_VERSION
DEFAULT_BUILD_CHANNEL = "internal-preview"
ALLOWED_BUILD_CHANNELS = {"internal-preview", "stable"}
# Build-time release authorization only. This is not embedded as updater trust;
# runtime acceptance is governed by TEMPORARY_UNSIGNED_STABLE_POLICY.  WOR-183
# keeps this temporary switch enabled until WOR-220 supplies recognized signing;
# do not replace it with a version allowlist (WOR-66 C13).
ALLOW_UNSIGNED_STABLE_BUILD = True
UPDATER_SCHEMA_VERSION = 1

_APPROVED_INNO_RUNTIME_CONSTANTS = frozenset({
    "app",
    "autodesktop",
    "group",
    "localappdata",
    "src",
    "sys",
    "tmp",
    "uninstallexe",
})
_APPROVED_INNO_RUNTIME_PREFIXES = ("cm:", "param:")
_INNO_BRACE_TOKEN_RE = re.compile(r"\{([^{}\r\n]+)\}")
_INNO_PASCAL_STRING_RE = re.compile(r"'(?:''|[^'])*'")
_INNO_INCLUDE_RE = re.compile(r'^\s*#include\s+"([^"]+)"', re.IGNORECASE)

MODELS = {
    "kokoro": {
        "repo": "hexgrad/Kokoro-82M",
        "revision": "f3ff3571791e39611d31c381e3a41a3af07b4987",
        "files": {
            "config.json": "5abb01e2403b072bf03d04fde160443e209d7a0dad49a423be15196b9b43c17f",
            "kokoro-v1_0.pth": "496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4",
            "voices/af_sky.pt": "c799548aed06e0cb0d655a85a01b48e7f10484d71663f9a3045a5b9362e8512c",
            "voices/ff_siwis.pt": "8073bf2d2c4b9543a90f2f0fd2144de4ed157e2d4b79ddeb0d5123066171fbc9",
        },
        "card_sha256": "91dcabced89db6f109b8786642f50402d3ee87450e8189589b6f85520e7f4d78",
    },
    "faster-whisper-small": {
        "repo": "Systran/faster-whisper-small",
        "revision": "536b0662742c02347bc0e980a01041f333bce120",
        "files": {
            "config.json": "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
            "model.bin": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
            "tokenizer.json": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
            "vocabulary.txt": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
        },
        "card_sha256": "329373481008c7c38654aff8ecdcf0163c211557cc7ba8e2ef6f2f84b4f75ec8",
    },
}


def _run(args: list[str], **kwargs) -> None:
    print("+", subprocess.list2cmdline(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def _pyinstaller_environment() -> dict[str, str]:
    """Keep Codex-host native runtimes out of the packaged dependency search."""

    environment = os.environ.copy()
    path_entries = environment.get("PATH", "").split(os.pathsep)
    environment["PATH"] = os.pathsep.join(
        entry
        for entry in path_entries
        if not (
            "codex-runtimes" in entry.replace("/", "\\").casefold()
            and "\\dependencies\\native\\"
            in entry.replace("/", "\\").casefold()
        )
    )
    return environment


def _run_package_smoke(executable: Path) -> None:
    """Run the windowed executable with one sanitized temporary report."""

    report = Path(tempfile.gettempdir()) / (
        f"osia-package-smoke-result-{os.urandom(8).hex()}.txt"
    )
    environment = os.environ.copy()
    environment["OSIA_PACKAGE_SMOKE_REPORT"] = str(report)
    try:
        _run([str(executable), "--osia-package-smoke"], env=environment)
    except subprocess.CalledProcessError:
        try:
            diagnostic = report.read_text("utf-8").strip()
        except OSError:
            diagnostic = "status=failed\nstep=unknown\nerror_type=Unavailable"
        print("Package smoke diagnostic:\n" + diagnostic, flush=True)
        raise
    finally:
        report.unlink(missing_ok=True)


def _iter_inno_source_lines(
    script_path: Path,
    *,
    in_code: bool = False,
    seen: set[Path] | None = None,
):
    path = script_path.resolve()
    visited = seen if seen is not None else set()
    if path in visited:
        return
    visited.add(path)
    code_section = in_code
    for line_number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        section = re.fullmatch(r"\s*\[([^]]+)]\s*", line)
        if section is not None:
            code_section = section.group(1).casefold() == "code"
        include = _INNO_INCLUDE_RE.match(line)
        if include is not None:
            include_path = (path.parent / include.group(1)).resolve()
            yield from _iter_inno_source_lines(
                include_path, in_code=code_section, seen=visited,
            )
        yield path, line_number, line, code_section


def _classify_inno_brace_token(body: str, *, escaped: bool) -> str:
    lowered = body.casefold()
    if body.startswith("#"):
        return "compile_time_preprocessor"
    if escaped or re.fullmatch(r"[A-Za-z%\\][^{}]*", body) is None:
        return "literal_brace_text"
    if lowered.startswith("code:"):
        return "custom_code"
    if lowered in _APPROVED_INNO_RUNTIME_CONSTANTS or lowered.startswith(
        _APPROVED_INNO_RUNTIME_PREFIXES
    ):
        return "valid_runtime"
    return "invalid_runtime"


def _inno_constant_inventory(
    script_path: Path | None = None,
) -> dict[str, tuple[str, ...]]:
    source = script_path or (ROOT / "packaging" / "windows" / "OSIA.iss")
    inventory: dict[str, set[str]] = {
        "compile_time_preprocessor": set(),
        "valid_runtime": set(),
        "custom_code": set(),
        "literal_brace_text": set(),
        "invalid_runtime": set(),
    }
    for _path, _line_number, line, in_code in _iter_inno_source_lines(source):
        for token in _INNO_BRACE_TOKEN_RE.finditer(line):
            if token.group(1).startswith("#"):
                inventory["compile_time_preprocessor"].add(token.group(0))
        fragments = (
            (match.group(0)[1:-1], match.start(0) + 1)
            for match in _INNO_PASCAL_STRING_RE.finditer(line)
        ) if in_code else ((line, 0),)
        for fragment, offset in fragments:
            for token in _INNO_BRACE_TOKEN_RE.finditer(fragment):
                start = offset + token.start(0)
                escaped = start > 0 and line[start - 1] == "{"
                category = _classify_inno_brace_token(
                    token.group(1), escaped=escaped,
                )
                inventory[category].add(token.group(0))
    return {
        category: tuple(sorted(tokens, key=str.casefold))
        for category, tokens in inventory.items()
    }


def _validate_inno_runtime_constants(script_path: Path | None = None) -> dict:
    inventory = _inno_constant_inventory(script_path)
    invalid = inventory["invalid_runtime"]
    if invalid:
        raise RuntimeError(
            "unsupported Inno runtime constant(s): " + ", ".join(invalid)
        )
    return inventory


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_dir(path: Path) -> None:
    resolved = path.resolve()
    allowed = (
        (ROOT / "build").resolve(),
        (ROOT / "dist").resolve(),
        (ROOT / "release").resolve(),
    )
    if not any(resolved == base or base in resolved.parents for base in allowed):
        raise RuntimeError(f"refusing to clean path outside build outputs: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _stage_models() -> list[dict]:
    from huggingface_hub import hf_hub_download

    manifests = []
    model_cards = LEGAL_STAGE / "model_cards"
    model_cards.mkdir(parents=True, exist_ok=True)
    for name, spec in MODELS.items():
        destination = MODEL_STAGE / name
        for relative, expected in spec["files"].items():
            cached = Path(hf_hub_download(
                repo_id=spec["repo"],
                filename=relative,
                revision=spec["revision"],
            ))
            actual = _sha256(cached)
            if actual != expected:
                raise RuntimeError(f"hash mismatch for {spec['repo']}/{relative}: {actual}")
            target = destination / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, target, follow_symlinks=True)

        card = Path(hf_hub_download(
            repo_id=spec["repo"], filename="README.md", revision=spec["revision"],
        ))
        if _sha256(card) != spec["card_sha256"]:
            raise RuntimeError(f"hash mismatch for {spec['repo']}/README.md")
        shutil.copy2(card, model_cards / f"{name}.md", follow_symlinks=True)
        manifests.append({
            "name": name,
            "repository": spec["repo"],
            "revision": spec["revision"],
            "files": spec["files"],
        })
    return manifests


def _stage_codex_runtime() -> dict:
    """Stage OSIA's pinned native Codex runtime without consulting PATH."""
    cache_root = Path(
        os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache")
    ) / "OSIA" / "build-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    archive = cache_root / CODEX_RUNTIME_ARCHIVE
    if not archive.is_file() or _sha256(archive) != CODEX_RUNTIME_SHA256:
        archive.unlink(missing_ok=True)
        partial = archive.with_suffix(archive.suffix + ".partial")
        partial.unlink(missing_ok=True)
        request = urllib.request.Request(
            CODEX_RUNTIME_URL,
            headers={"User-Agent": "OSIA-Windows-Builder/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output)
            if _sha256(partial) != CODEX_RUNTIME_SHA256:
                raise RuntimeError("Codex runtime archive checksum mismatch")
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)

    _clean_dir(CODEX_RUNTIME_STAGE)
    CODEX_RUNTIME_STAGE.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as package:
        package.extractall(CODEX_RUNTIME_STAGE, filter="data")

    required = (
        "codex-package.json",
        "bin/codex.exe",
        "bin/codex-code-mode-host.exe",
        "codex-path/rg.exe",
        "codex-resources/codex-command-runner.exe",
        "codex-resources/codex-windows-sandbox-setup.exe",
    )
    missing = [name for name in required if not (CODEX_RUNTIME_STAGE / name).is_file()]
    if missing:
        raise RuntimeError("Codex runtime package is incomplete: " + ", ".join(missing))
    probe = subprocess.run(
        [str(CODEX_RUNTIME_STAGE / "bin" / "codex.exe"), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if probe.returncode != 0 or CODEX_RUNTIME_VERSION not in (probe.stdout or probe.stderr):
        raise RuntimeError("staged Codex runtime failed version verification")
    return {
        "version": CODEX_RUNTIME_VERSION,
        "archive": CODEX_RUNTIME_ARCHIVE,
        "sha256": CODEX_RUNTIME_SHA256,
    }


def _stage_legal() -> list[dict]:
    license_root = LEGAL_STAGE / "licenses"
    license_root.mkdir(parents=True, exist_ok=True)
    dependencies = []
    for dist in sorted(
        importlib.metadata.distributions(),
        key=lambda item: (item.metadata.get("Name") or "").lower(),
    ):
        name = dist.metadata.get("Name") or "unknown"
        entry = {
            "name": name,
            "version": dist.version,
            "license": dist.metadata.get("License-Expression") or dist.metadata.get("License") or "",
            "homepage": dist.metadata.get("Home-page") or "",
        }
        dependencies.append(entry)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{name}-{dist.version}")
        for item in dist.files or ():
            basename = Path(str(item)).name.upper()
            if not basename.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
                continue
            source = Path(dist.locate_file(item))
            if not source.is_file() or source.stat().st_size > 2 * 1024 * 1024:
                continue
            target = license_root / safe / Path(str(item)).name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
    (LEGAL_STAGE / "DEPENDENCIES.json").write_text(
        json.dumps(dependencies, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    return dependencies


def _generate_branding(version: str) -> None:
    from PIL import Image, ImageDraw

    GENERATED.mkdir(parents=True, exist_ok=True)
    icon = Image.open(ROOT / "assets" / "osia_icon.png").convert("RGBA")
    icon.save(
        GENERATED / "osia.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    logo = Image.open(ROOT / "assets" / "osia_logo.png").convert("RGBA")
    canvas = Image.new("RGBA", (410, 790), "#110b15")
    draw = ImageDraw.Draw(canvas)
    for y in range(canvas.height):
        mix = y / max(1, canvas.height - 1)
        colour = (17 + int(9 * mix), 11 + int(6 * mix), 21 + int(15 * mix), 255)
        draw.line((0, y, canvas.width, y), fill=colour)
    mark = logo.copy()
    mark.thumbnail((280, 280), Image.Resampling.LANCZOS)
    canvas.alpha_composite(
        mark, ((canvas.width - mark.width) // 2, (canvas.height - mark.height) // 2),
    )
    canvas.convert("RGB").save(GENERATED / "wizard-large.png", optimize=True)

    # The small wizard image is reused as the visible logo throughout Setup.
    # Keep it genuinely transparent so it never renders as a square colour block.
    mark = icon.copy()
    mark.thumbnail((132, 132), Image.Resampling.LANCZOS)
    transparent_logo = Image.new("RGBA", (164, 164), (0, 0, 0, 0))
    transparent_logo.alpha_composite(
        mark,
        (
            (transparent_logo.width - mark.width) // 2,
            (transparent_logo.height - mark.height) // 2,
        ),
    )
    transparent_logo.save(GENERATED / "wizard-small.png", optimize=True)

    # Updater-only major release token. The alpha mask keeps the treatment
    # inside one rounded card instead of recolouring the full installer.
    band_size = (960, 132)
    band = Image.new("RGBA", band_size, (0, 0, 0, 0))
    gradient = Image.new("RGBA", band_size, (0, 0, 0, 0))
    gradient_draw = ImageDraw.Draw(gradient)
    left = (139, 92, 255)
    right = (52, 216, 255)
    for x in range(band_size[0]):
        mix = x / max(1, band_size[0] - 1)
        colour = tuple(
            round(left[index] * (1.0 - mix) + right[index] * mix)
            for index in range(3)
        )
        gradient_draw.line((x, 0, x, band_size[1]), fill=(*colour, 72))
    mask = Image.new("L", band_size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (2, 2, band_size[0] - 3, band_size[1] - 3), radius=28, fill=255
    )
    gradient.putalpha(Image.eval(mask, lambda value: round(value * 0.34)))
    band.alpha_composite(gradient)
    band_draw = ImageDraw.Draw(band)
    band_draw.rounded_rectangle(
        (2, 2, band_size[0] - 3, band_size[1] - 3),
        radius=28,
        outline=(102, 205, 255, 150),
        width=3,
    )
    band.save(GENERATED / "major-update-band.png", optimize=True)

    version_parts = list(parse_product_version(version))
    version_parts.extend([0] * (4 - len(version_parts)))
    version_tuple = tuple(version_parts)
    version_info = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={version_tuple}, prodvers={version_tuple},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'OSIA'),
    StringStruct('FileDescription', 'OSIA Personal AI Assistant'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('InternalName', 'OSIA'),
    StringStruct('LegalCopyright', 'Copyright OSIA'),
    StringStruct('OriginalFilename', 'OSIA.exe'),
    StringStruct('ProductName', 'OSIA'),
    StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"""
    (GENERATED / "version_info.txt").write_text(version_info, encoding="utf-8")


def _build_timestamp() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            value = datetime.fromtimestamp(int(epoch), timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            raise RuntimeError("SOURCE_DATE_EPOCH must be a valid Unix timestamp") from exc
    else:
        value = datetime.now(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_commit() -> str | None:
    configured = os.environ.get("OSIA_BUILD_COMMIT", "").strip().lower()
    if configured:
        if re.fullmatch(r"[0-9a-f]{40}", configured) is None:
            raise RuntimeError("OSIA_BUILD_COMMIT must be a 40-character commit SHA")
        return configured
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    commit = completed.stdout.strip().lower()
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None


def _resolve_build_identity(
    channel: str = DEFAULT_BUILD_CHANNEL,
    version_override: str | None = None,
) -> dict:
    if channel not in ALLOWED_BUILD_CHANNELS:
        raise ValueError(f"unsupported update channel: {channel}")
    version = SOURCE_VERSION
    override = None
    if version_override is not None:
        parse_product_version(version_override)
        if channel != "internal-preview":
            raise ValueError("version overrides are forbidden for stable builds")
        if version_override != "1.0.1":
            raise ValueError("the only supported internal override is 1.0.1")
        version = version_override
        override = {
            "sourceVersion": SOURCE_VERSION,
            "version": version,
            "channel": channel,
        }
    return {
        "version": version,
        "channel": channel,
        "timestamp": _build_timestamp(),
        "commit": _build_commit(),
        "override": override,
    }


def _write_app_metadata(identity: dict) -> Path:
    metadata = {
        "product": "OSIA",
        "version": identity["version"],
        "updateChannel": identity["channel"],
        "platform": "windows",
        "architecture": "x86_64",
        "buildTimestamp": identity["timestamp"],
        "commitSha": identity["commit"],
        "updaterSchemaVersion": UPDATER_SCHEMA_VERSION,
        "buildOverride": identity["override"],
    }
    GENERATED.mkdir(parents=True, exist_ok=True)
    path = GENERATED / "app_metadata.json"
    path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    return path


def _write_installer_release_notes(version: str) -> tuple[Path, Path]:
    """Create complete localized changelogs for the visible update wizard."""

    GENERATED.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for language in ("en", "fr"):
        path = GENERATED / f"release-notes-{language}.txt"
        path.write_text(
            installer_release_notes(version, language), encoding="utf-8-sig"
        )
        paths.append(path)
    return paths[0], paths[1]


def _write_manifest(
    models: list[dict], dependencies: list[dict], codex_runtime: dict, identity: dict,
) -> Path:
    manifest = {
        "product": "OSIA",
        "version": identity["version"],
        "update_channel": identity["channel"],
        "architecture": "x86_64",
        "build_utc": identity["timestamp"],
        "git_commit": identity["commit"],
        "build_override": identity["override"],
        "python": sys.version,
        "pyinstaller": importlib.metadata.version("pyinstaller"),
        "pyinstaller_hooks_contrib": importlib.metadata.version("pyinstaller-hooks-contrib"),
        "models": models,
        "codex_runtime": codex_runtime,
        "build_environment_dependencies": dependencies,
    }
    path = LEGAL_STAGE / "BUILD_MANIFEST.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _write_bundle_file_manifest() -> Path:
    """Record every frozen payload file except the manifest itself."""
    path = DIST / "_internal" / "legal" / "BUNDLE_FILES.json"
    entries = []
    for item in sorted(DIST.rglob("*")):
        if not item.is_file() or item == path:
            continue
        entries.append({
            "path": item.relative_to(DIST).as_posix(),
            "bytes": item.stat().st_size,
            "sha256": _sha256(item),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return path


def _find_iscc() -> Path:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which("ISCC.exe") or shutil.which("iscc")
    if found:
        return Path(found)
    raise FileNotFoundError("Inno Setup 6 ISCC.exe was not found")


def _find_signtool() -> Path | None:
    direct = shutil.which("signtool.exe")
    if direct:
        return Path(direct)
    kits = Path(os.environ.get("ProgramFiles(x86)", "")) / "Windows Kits" / "10" / "bin"
    matches = sorted(kits.glob("*/x64/signtool.exe"), reverse=True) if kits.is_dir() else []
    return matches[0] if matches else None


def _sign(path: Path) -> bool:
    thumbprint = os.environ.get("OSIA_SIGN_SHA1", "").replace(" ", "")
    if not thumbprint:
        return False
    signtool = _find_signtool()
    if signtool is None:
        raise FileNotFoundError("OSIA_SIGN_SHA1 is set but signtool.exe was not found")
    _run([
        str(signtool), "sign", "/sha1", thumbprint, "/fd", "sha256",
        "/tr", "http://timestamp.digicert.com", "/td", "sha256", str(path),
    ])
    _run([str(signtool), "verify", "/pa", "/v", str(path)])
    return True


def _local_secret_values() -> tuple[bytes, ...]:
    """Read local credentials for exact-match scanning without logging them."""
    values: set[bytes] = set()
    sensitive_fields = {
        "access_token", "refresh_token", "id_token", "api_key", "client_secret",
        "password", "code_verifier", "credential", "secret",
    }

    def add(value) -> None:
        if isinstance(value, str) and len(value.strip()) >= 16:
            values.add(value.strip().encode("utf-8"))

    def collect_sensitive_fields(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in sensitive_fields:
                    add(item)
                collect_sensitive_fields(item)
        elif isinstance(value, list):
            for item in value:
                collect_sensitive_fields(item)

    api_keys = ROOT / "config" / "api_keys.json"
    if api_keys.is_file():
        try:
            for value in json.loads(api_keys.read_text("utf-8")).values():
                add(value)
        except (OSError, ValueError, AttributeError):
            pass

    settings = ROOT / "config" / "settings.json"
    if settings.is_file():
        try:
            fallbacks = json.loads(settings.read_text("utf-8")).get("api_key_fallbacks", {})
            for chain in fallbacks.values():
                for value in chain if isinstance(chain, list) else ():
                    add(value)
        except (OSError, ValueError, AttributeError):
            pass

    oauth = ROOT / "config" / "oauth_tokens.json"
    if oauth.is_file():
        try:
            collect_sensitive_fields(json.loads(oauth.read_text("utf-8")))
        except (OSError, ValueError):
            pass
    token_file = ROOT / "config" / "mobile_token.txt"
    if token_file.is_file():
        token = token_file.read_text("utf-8", errors="ignore").strip()
        if len(token) >= 16:
            values.add(token.encode("utf-8"))
    return tuple(values)


def _secret_scan(root: Path) -> None:
    forbidden_names = {
        ".fingerprint.key", "api_keys.json", "oauth_tokens.json", "mobile_token.txt", "settings.json",
        "memory.json", "onboarding.json", "task_state.json", "task_state_mobile.json",
        "lan_direct_key.pem", "lan_direct_cert.pem",
    }
    generic_patterns = (
        b"sk-proj-",
        b"AIzaSy",
        b"-----BEGIN " b"PRIVATE KEY-----",
    )
    text_suffixes = {
        ".cfg", ".html", ".ini", ".iss", ".json", ".md", ".ps1", ".py",
        ".toml", ".txt", ".xml", ".yaml", ".yml",
    }
    exact_secrets = _local_secret_values()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() in forbidden_names:
            raise RuntimeError(f"forbidden user-data file in package: {path}")
        size = path.stat().st_size
        if path.suffix.lower() in text_suffixes and size <= 10 * 1024 * 1024:
            data = path.read_bytes()
            if any(pattern in data for pattern in generic_patterns):
                raise RuntimeError(f"possible credential in package: {path}")
        if exact_secrets:
            overlap = max(map(len, exact_secrets)) - 1
            tail = b""
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                    data = tail + chunk
                    if any(secret in data for secret in exact_secrets):
                        raise RuntimeError(f"local credential value found in package: {path}")
                    tail = data[-overlap:] if overlap > 0 else b""


def _validate_release_payload(root: Path) -> None:
    files, total_bytes = scan_release_payload(
        root,
        forbidden_roots=(Path.home(), ROOT, STAGE, DIST, RELEASE),
    )
    print(
        f"Validated release payload safety: {files:,} files, "
        f"{total_bytes:,} bytes under {root}",
        flush=True,
    )


def build(
    skip_installer: bool = False,
    *,
    channel: str = DEFAULT_BUILD_CHANNEL,
    version_override: str | None = None,
) -> Path | None:
    if sys.platform != "win32":
        raise RuntimeError("the Windows package must be built on Windows")
    if importlib.metadata.version("pyinstaller") != "6.21.0":
        raise RuntimeError("PyInstaller 6.21.0 is required")
    if importlib.metadata.version("pyinstaller-hooks-contrib") != "2026.6":
        raise RuntimeError("pyinstaller-hooks-contrib 2026.6 is required")

    _validate_inno_runtime_constants()
    identity = _resolve_build_identity(channel, version_override)
    version = identity["version"]

    _clean_dir(STAGE)
    _clean_dir(DIST)
    _clean_dir(RELEASE)
    RELEASE.mkdir(parents=True, exist_ok=True)
    LEGAL_STAGE.mkdir(parents=True, exist_ok=True)
    models = _stage_models()
    codex_runtime = _stage_codex_runtime()
    dependencies = _stage_legal()
    _generate_branding(version)
    _write_app_metadata(identity)
    _write_manifest(models, dependencies, codex_runtime, identity)

    _run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
          str(ROOT / "packaging" / "windows" / "osia.spec")],
         env=_pyinstaller_environment())
    _write_bundle_file_manifest()
    _validate_release_payload(DIST)
    _secret_scan(DIST)
    _run_package_smoke(DIST / "OSIA.exe")
    signed_app = _sign(DIST / "OSIA.exe")
    if skip_installer:
        if (
            channel == "stable"
            and not signed_app
            and not ALLOW_UNSIGNED_STABLE_BUILD
        ):
            raise RuntimeError("stable builds require Authenticode signing")
        return None

    release_notes_en, release_notes_fr = _write_installer_release_notes(version)
    iscc = _find_iscc()
    _run([
        str(iscc),
        f"/DAppVersion={version}",
        f"/DAppVersionMajor={parse_product_version(version)[0]}",
        f"/DAppVersionMinor={parse_product_version(version)[1]}",
        f"/DAppVersionPatch={parse_product_version(version)[2]}",
        f"/DBuildChannel={channel}",
        f"/DInternalBuild={1 if channel == 'internal-preview' else 0}",
        f"/DSignedBuild={1 if signed_app else 0}",
        f"/DMajorUpdate={1 if is_major_update(version) else 0}",
        f"/DReleaseNotesEn={release_notes_en}",
        f"/DReleaseNotesFr={release_notes_fr}",
        f"/DRepoRoot={ROOT}",
        f"/DBuildRoot={STAGE}",
        f"/DReleaseRoot={RELEASE}",
        str(ROOT / "packaging" / "windows" / "OSIA.iss"),
    ])
    installer = RELEASE / "OSIA-Setup.exe"
    if not installer.is_file():
        raise FileNotFoundError(installer)
    signed_installer = _sign(installer)
    if (
        channel == "stable"
        and not (signed_app and signed_installer)
        and not ALLOW_UNSIGNED_STABLE_BUILD
    ):
        installer.unlink(missing_ok=True)
        raise RuntimeError("stable builds require signed application and installer artifacts")
    checksum = _sha256(installer)
    (installer.with_suffix(installer.suffix + ".sha256")).write_text(
        f"{checksum}  {installer.name}\n", encoding="ascii",
    )
    shutil.copy2(release_notes_en, RELEASE / "release-notes-en.txt")
    shutil.copy2(release_notes_fr, RELEASE / "release-notes-fr.txt")
    release_manifest = {
        "schemaVersion": UPDATER_SCHEMA_VERSION,
        "product": "OSIA",
        "platform": "windows",
        "architecture": "x86_64",
        "channel": channel,
        "version": version,
        "publishedAt": identity["timestamp"],
        "installer": {
            "url": f"https://download.osia.space/windows/{version}/OSIA-Setup.exe",
            "sizeBytes": installer.stat().st_size,
            "sha256": checksum,
            "signed": signed_installer,
            "signaturePublisher": "OSIA" if signed_installer else None,
        },
        "mandatory": False,
        "minimumSupportedVersion": "1.0.1",
        "minimumUpdaterVersion": "1.0.1",
        "releaseNotes": manifest_release_notes(version),
        "releaseNotesUrl": None,
        "rollout": {"state": "paused"},
    }
    (RELEASE / "release-manifest.json").write_text(
        json.dumps(release_manifest, indent=2) + "\n", encoding="utf-8",
    )
    _validate_release_payload(RELEASE)
    print(f"Built {installer} ({installer.stat().st_size:,} bytes)")
    if not signed_installer:
        print("WARNING: installer is unsigned (set OSIA_SIGN_SHA1 for release signing)")
    return installer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-installer", action="store_true")
    parser.add_argument("--channel", choices=sorted(ALLOWED_BUILD_CHANNELS),
                        default=DEFAULT_BUILD_CHANNEL)
    parser.add_argument("--version-override")
    args = parser.parse_args()
    build(
        skip_installer=args.skip_installer,
        channel=args.channel,
        version_override=args.version_override,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
