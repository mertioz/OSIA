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
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
STAGE = ROOT / "build" / "windows"
GENERATED = STAGE / "generated"
MODEL_STAGE = STAGE / "models"
LEGAL_STAGE = STAGE / "legal"
DIST = ROOT / "dist" / "OSIA"
RELEASE = ROOT / "release" / "windows"
_VERSION_SOURCE = (ROOT / "osia" / "lan_discovery.py").read_text("utf-8")
_VERSION_MATCH = re.search(r'^PRODUCT_VERSION\s*=\s*["\']([^"\']+)["\']', _VERSION_SOURCE, re.M)
if _VERSION_MATCH is None:
    raise RuntimeError("osia.lan_discovery.PRODUCT_VERSION was not found")
VERSION = _VERSION_MATCH.group(1)

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


def _generate_branding() -> None:
    from PIL import Image, ImageDraw

    GENERATED.mkdir(parents=True, exist_ok=True)
    icon = Image.open(ROOT / "assets" / "osia_icon.png").convert("RGBA")
    icon.save(
        GENERATED / "osia.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    logo = Image.open(ROOT / "assets" / "osia_logo.png").convert("RGBA")
    for filename, size, logo_size in (
        ("wizard-large.png", (410, 790), 280),
        ("wizard-small.png", (164, 164), 120),
    ):
        canvas = Image.new("RGBA", size, "#0d0b12")
        draw = ImageDraw.Draw(canvas)
        for y in range(size[1]):
            mix = y / max(1, size[1] - 1)
            colour = (
                int(13 + 18 * mix), int(11 + 10 * mix), int(18 + 28 * mix), 255,
            )
            draw.line((0, y, size[0], y), fill=colour)
        mark = logo.copy()
        mark.thumbnail((logo_size, logo_size), Image.Resampling.LANCZOS)
        canvas.alpha_composite(mark, ((size[0] - mark.width) // 2, (size[1] - mark.height) // 2))
        canvas.convert("RGB").save(GENERATED / filename, optimize=True)

    version_parts = [int(part) for part in VERSION.split(".")]
    if len(version_parts) > 4:
        raise ValueError(f"Windows version has too many components: {VERSION}")
    version_parts.extend([0] * (4 - len(version_parts)))
    version_tuple = tuple(version_parts)
    version_info = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={version_tuple}, prodvers={version_tuple},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'OSIA'),
    StringStruct('FileDescription', 'OSIA Personal AI Assistant'),
    StringStruct('FileVersion', '{VERSION}'),
    StringStruct('InternalName', 'OSIA'),
    StringStruct('LegalCopyright', 'Copyright OSIA'),
    StringStruct('OriginalFilename', 'OSIA.exe'),
    StringStruct('ProductName', 'OSIA'),
    StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"""
    (GENERATED / "version_info.txt").write_text(version_info, encoding="utf-8")


def _write_manifest(models: list[dict], dependencies: list[dict]) -> Path:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    manifest = {
        "product": "OSIA",
        "version": VERSION,
        "architecture": "x86_64",
        "build_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit,
        "git_dirty": bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()),
        "python": sys.version,
        "pyinstaller": importlib.metadata.version("pyinstaller"),
        "pyinstaller_hooks_contrib": importlib.metadata.version("pyinstaller-hooks-contrib"),
        "models": models,
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
        "api_keys.json", "oauth_tokens.json", "mobile_token.txt", "settings.json",
        "memory.json", "task_state.json", "task_state_mobile.json",
        "lan_direct_key.pem", "lan_direct_cert.pem",
    }
    generic_patterns = (b"sk-proj-", b"AIzaSy", b"-----BEGIN PRIVATE KEY-----")
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


def build(skip_installer: bool = False) -> Path | None:
    if sys.platform != "win32":
        raise RuntimeError("the Windows package must be built on Windows")
    if importlib.metadata.version("pyinstaller") != "6.21.0":
        raise RuntimeError("PyInstaller 6.21.0 is required")
    if importlib.metadata.version("pyinstaller-hooks-contrib") != "2026.6":
        raise RuntimeError("pyinstaller-hooks-contrib 2026.6 is required")

    _clean_dir(STAGE)
    _clean_dir(DIST)
    _clean_dir(RELEASE)
    RELEASE.mkdir(parents=True, exist_ok=True)
    LEGAL_STAGE.mkdir(parents=True, exist_ok=True)
    models = _stage_models()
    dependencies = _stage_legal()
    _generate_branding()
    _write_manifest(models, dependencies)

    _run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
          str(ROOT / "packaging" / "windows" / "osia.spec")])
    _write_bundle_file_manifest()
    _secret_scan(DIST)
    _run([str(DIST / "OSIA.exe"), "--osia-package-smoke"])
    signed_app = _sign(DIST / "OSIA.exe")
    if skip_installer:
        return None

    iscc = _find_iscc()
    _run([
        str(iscc),
        f"/DAppVersion={VERSION}",
        f"/DRepoRoot={ROOT}",
        f"/DBuildRoot={STAGE}",
        f"/DReleaseRoot={RELEASE}",
        str(ROOT / "packaging" / "windows" / "OSIA.iss"),
    ])
    installer = RELEASE / "OSIA-Setup.exe"
    if not installer.is_file():
        raise FileNotFoundError(installer)
    signed_installer = _sign(installer)
    checksum = _sha256(installer)
    (installer.with_suffix(installer.suffix + ".sha256")).write_text(
        f"{checksum}  {installer.name}\n", encoding="ascii",
    )
    release_manifest = {
        "artifact": installer.name,
        "version": VERSION,
        "architecture": "x86_64",
        "sha256": checksum,
        "bytes": installer.stat().st_size,
        "app_signed": signed_app,
        "installer_signed": signed_installer,
    }
    (RELEASE / "release-manifest.json").write_text(
        json.dumps(release_manifest, indent=2) + "\n", encoding="utf-8",
    )
    print(f"Built {installer} ({installer.stat().st_size:,} bytes)")
    if not signed_installer:
        print("WARNING: installer is unsigned (set OSIA_SIGN_SHA1 for release signing)")
    return installer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-installer", action="store_true")
    args = parser.parse_args()
    build(skip_installer=args.skip_installer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
