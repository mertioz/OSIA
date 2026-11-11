#!/usr/bin/env python3
"""Sign an unchanged Linux release using an existing protected OpenPGP key.

The key passphrase is read from standard input, never from command arguments.
Private key creation and backup belong to the operator's local signing vault.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ORIGIN = "https://download.osia.space"
INSTALLER = "OSIA-Linux-x86_64.tar.xz"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def gpg_path(path: Path, executable: Path) -> str:
    """Git for Windows GnuPG uses MSYS paths instead of native drive paths."""
    resolved = path.resolve()
    if os.name == "nt" and (executable.parent / "msys-2.0.dll").is_file():
        if re.fullmatch(r"[A-Za-z]:", resolved.drive):
            return "/" + resolved.drive[0].lower() + resolved.as_posix()[2:]
    return resolved.as_posix()


def run_gpg(executable: Path, home: Path, *args: str, password: bytes | None = None) -> bytes:
    command = [str(executable), "--homedir", gpg_path(home, executable), "--batch"]
    if password is not None:
        command += ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
    result = subprocess.run(command + list(args), input=password, capture_output=True, timeout=90)
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        if password:
            detail = detail.replace(password.strip().decode(), "<redacted>")
        raise RuntimeError(f"GnuPG failed with exit code {result.returncode}: {detail}")
    return result.stdout


def validate_release(artifact: Path, manifest: dict) -> None:
    version = manifest.get("version", "")
    installer = manifest.get("installer") or {}
    if not (
        artifact.name == INSTALLER
        and re.fullmatch(r"\d+\.\d+\.\d+", version)
        and manifest.get("platform") == "linux-x64"
        and manifest.get("installerId") == "space.osia.desktop.linux.v1"
        and re.fullmatch(r"[0-9a-f]{40}", manifest.get("sourceCommit", ""))
        and type(manifest.get("releaseSequence")) is int
        and manifest["releaseSequence"] > 0
        and installer.get("url") == f"{ORIGIN}/linux/{version}/{INSTALLER}"
        and installer.get("sizeBytes") == artifact.stat().st_size
        and installer.get("sha256") == digest(artifact)
    ):
        raise ValueError("Linux release identity, size or SHA-256 mismatch")


def verify(executable: Path, home: Path, signature: Path, artifact: Path, fingerprint: str) -> None:
    output = run_gpg(executable, home, "--no-autostart", "--status-fd", "1", "--verify",
                     gpg_path(signature, executable), gpg_path(artifact, executable)).decode()
    valid = [line.split() for line in output.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    if len(valid) != 1 or fingerprint not in (valid[0][2], valid[0][-1]):
        raise ValueError("Signature does not match the required primary key")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--gnupg-home", type=Path, required=True)
    parser.add_argument("--key", required=True, help="Full primary-key fingerprint")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gpg", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_bytes())
    validate_release(args.artifact, manifest)
    fingerprint = args.key.upper()
    if not re.fullmatch(r"[0-9A-F]{40}", fingerprint):
        raise ValueError("A full OpenPGP primary-key fingerprint is required")
    destination = args.output_dir.resolve()
    if destination.exists():
        raise ValueError("Use a new output directory to preserve existing signing evidence")
    destination.parent.mkdir(parents=True, exist_ok=True)
    password = sys.stdin.buffer.readline().rstrip(b"\r\n")
    if not password:
        raise ValueError("A protected-key passphrase is required on standard input")
    stage = Path(tempfile.mkdtemp(prefix=".osia-signing-", dir=destination.parent)).resolve()
    # GnuPG's MSYS agent socket needs a shorter home than the artifact path.
    public_home = Path(tempfile.mkdtemp(prefix="osia-gpg-")).resolve()
    try:
        public_bytes = run_gpg(args.gpg, args.gnupg_home, "--armor", "--export", fingerprint)
        key_hash = hashlib.sha256(public_bytes).hexdigest()
        public_key = stage / f"osia-linux-release-{fingerprint.lower()}-{key_hash[:16]}.asc"
        public_key.write_bytes(public_bytes)
        run_gpg(args.gpg, public_home, "--import", gpg_path(public_key, args.gpg))
        signature = stage / (INSTALLER + ".asc")
        run_gpg(args.gpg, args.gnupg_home, "--armor", "--local-user", fingerprint,
                "--output", gpg_path(signature, args.gpg), "--detach-sign",
                gpg_path(args.artifact, args.gpg), password=password + b"\n")
        verify(args.gpg, public_home, signature, args.artifact, fingerprint)
        manifest["installer"]["signed"] = True
        manifest["installer"]["signature"] = {
            "type": "openpgp-detached",
            "url": manifest["installer"]["url"] + ".asc",
            "sha256": digest(signature),
            "sizeBytes": signature.stat().st_size,
            "keyUrl": f"{ORIGIN}/keys/{public_key.name}",
            "keySha256": digest(public_key),
            "keyFingerprint": fingerprint,
        }
        manifest["signedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        manifest["originalReleaseManifestUrl"] = f"{ORIGIN}/linux/{manifest['version']}/release-manifest.json"
        signed_manifest = stage / "release-manifest.json"
        signed_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_signature = stage / "release-manifest.json.asc"
        run_gpg(args.gpg, args.gnupg_home, "--armor", "--local-user", fingerprint,
                "--output", gpg_path(manifest_signature, args.gpg), "--detach-sign",
                gpg_path(signed_manifest, args.gpg), password=password + b"\n")
        verify(args.gpg, public_home, manifest_signature, signed_manifest, fingerprint)
        stage.rename(destination)
        print(json.dumps({"version": manifest["version"], "fingerprint": fingerprint,
                          "artifactSha256": manifest["installer"]["sha256"], "output": str(destination)}))
    finally:
        password = b""
        if stage.exists():
            shutil.rmtree(stage)
        gpgconf = args.gpg.with_name("gpgconf.exe" if os.name == "nt" else "gpgconf")
        subprocess.run([str(gpgconf), "--homedir", gpg_path(public_home, args.gpg), "--kill", "gpg-agent"],
                       capture_output=True, timeout=15)
        shutil.rmtree(public_home)


if __name__ == "__main__":
    main()
