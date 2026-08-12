"""Build the deterministic Chrome/Edge Personal Browser store ZIP."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "extension" / "personal-browser"
APPROVED_PERMISSIONS = ["activeTab", "scripting", "nativeMessaging", "storage"]
RUNTIME_FILES = (
    "service_worker.js",
    "content_agent.js",
    "icons/icon16.png",
    "icons/icon32.png",
    "icons/icon48.png",
    "icons/icon128.png",
    "_locales/en/messages.json",
    "_locales/fr/messages.json",
)
FORBIDDEN_TEXT = (
    "http://",
    "https://",
    "ws://",
    "wss://",
    "localhost",
    "127.0.0.1",
    "eval(",
    "new function",
    "import(",
    "/home/",
    "/tmp/",
)
FORBIDDEN_PERMISSIONS = {
    "cookies",
    "history",
    "tabs",
    "identity",
    "debugger",
    "webNavigation",
    "<all_urls>",
}
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _development_id(key: str) -> str:
    digest = hashlib.sha256(base64.b64decode(key, validate=True)).digest()[:16]
    return "".join(
        chr(ord("a") + nibble)
        for byte in digest
        for nibble in (byte >> 4, byte & 0x0F)
    )


def _validated_source() -> tuple[dict, dict[str, bytes]]:
    manifest = json.loads((SOURCE / "manifest.json").read_text("utf-8"))
    metadata = json.loads((SOURCE / "metadata.json").read_text("utf-8"))
    if manifest.get("manifest_version") != 3:
        raise RuntimeError("Personal Browser store package must use Manifest V3")
    if manifest.get("permissions") != APPROVED_PERMISSIONS:
        raise RuntimeError("Personal Browser permissions differ from the approved set")
    if manifest.get("incognito") != "not_allowed":
        raise RuntimeError("Personal Browser must remain unavailable in incognito")
    if "host_permissions" in manifest or "content_scripts" in manifest:
        raise RuntimeError("Personal Browser must not request ambient page access")
    if FORBIDDEN_PERMISSIONS.intersection(manifest.get("permissions", [])):
        raise RuntimeError("Personal Browser contains a forbidden permission")
    if manifest.get("default_locale") != "en":
        raise RuntimeError("Personal Browser default locale must be English")
    if _development_id(str(manifest.get("key") or "")) != metadata.get(
        "development_extension_id"
    ):
        raise RuntimeError("development extension identity is inconsistent")

    version = str(manifest.get("version") or "")
    if not re.fullmatch(r"\d+(?:\.\d+){0,3}", version):
        raise RuntimeError("extension version is not store compatible")
    if any(int(part) > 65535 for part in version.split(".")):
        raise RuntimeError("extension version component exceeds 65535")

    files: dict[str, bytes] = {}
    production_manifest = dict(manifest)
    production_manifest.pop("key", None)
    files["manifest.json"] = (
        json.dumps(production_manifest, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    for name in RUNTIME_FILES:
        path = (SOURCE / name).resolve()
        if SOURCE.resolve() not in path.parents or not path.is_file():
            raise RuntimeError(f"required runtime file is missing: {name}")
        files[name] = path.read_bytes()

    for name, payload in files.items():
        lowered = name.lower()
        if any(
            marker in lowered
            for marker in (".git", "__pycache__", ".pyc", ".map", ".log", ".tmp")
        ):
            raise RuntimeError(f"forbidden package entry: {name}")
        if Path(name).suffix in {".js", ".json"}:
            text = payload.decode("utf-8").lower()
            found = [value for value in FORBIDDEN_TEXT if value in text]
            if found:
                raise RuntimeError(f"forbidden runtime dependency in {name}: {found[0]}")
            if re.search(r"[a-z]:\\", text):
                raise RuntimeError(f"absolute Windows path found in {name}")
    return production_manifest, files


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build(output: Path | None = None) -> dict:
    manifest, files = _validated_source()
    version = manifest["version"]
    output = output or (
        ROOT / "dist" / "store" / f"osia-personal-browser-{version}.zip"
    )
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name in sorted(files):
            archive.writestr(_zip_info(name), files[name], compresslevel=9)

    payload = output.read_bytes()
    summary = {
        "schema_version": 1,
        "artifact": output.name,
        "browser_targets": ["chrome", "edge", "brave-via-chrome-web-store"],
        "extension_version": version,
        "size_bytes": len(payload),
        "sha256": _sha256_bytes(payload),
        "files": [
            {
                "path": name,
                "size_bytes": len(files[name]),
                "sha256": _sha256_bytes(files[name]),
            }
            for name in sorted(files)
        ],
    }
    contents_path = output.with_suffix(".contents.json")
    contents_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary["output"] = str(output)
    summary["contents_manifest"] = str(contents_path)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
