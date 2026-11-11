"""Release signing rejects mismatched artifacts before accessing a private key."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sign_linux_release", ROOT / "packaging/linux/sign_release.py")
signer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(signer)


@pytest.fixture
def release(tmp_path):
    artifact = tmp_path / signer.INSTALLER
    artifact.write_bytes(b"controlled archive")
    manifest = {
        "version": "0.9.264", "platform": "linux-x64",
        "installerId": "space.osia.desktop.linux.v1", "sourceCommit": "a" * 40,
        "releaseSequence": 2026111101,
        "installer": {"url": f"{signer.ORIGIN}/linux/0.9.264/{signer.INSTALLER}",
                      "sizeBytes": artifact.stat().st_size,
                      "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()},
    }
    return artifact, manifest


def test_exact_archive_is_accepted(release):
    signer.validate_release(*release)


def test_modified_archive_is_rejected(release):
    artifact, manifest = release
    artifact.write_bytes(b"modified archive")
    with pytest.raises(ValueError, match="mismatch"):
        signer.validate_release(artifact, manifest)


@pytest.mark.parametrize(("field", "value"), [
    ("platform", "windows"), ("installerId", "different-installer"),
    ("releaseSequence", True), ("sourceCommit", "invalid"),
])
def test_wrong_identity_is_rejected(release, field, value):
    artifact, original = release
    manifest = copy.deepcopy(original)
    manifest[field] = value
    with pytest.raises(ValueError, match="mismatch"):
        signer.validate_release(artifact, manifest)


def test_unofficial_archive_url_is_rejected(release):
    artifact, manifest = release
    manifest["installer"]["url"] = "https://unapproved.example/archive.tar.xz"
    with pytest.raises(ValueError, match="mismatch"):
        signer.validate_release(artifact, manifest)
