from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "personal-browser"
STORE = ROOT / "packaging" / "personal_browser" / "store_submission"
APPROVED_PERMISSIONS = ["activeTab", "scripting", "nativeMessaging", "storage"]


def _load_builder():
    path = ROOT / "packaging" / "personal_browser" / "build_store_package.py"
    spec = importlib.util.spec_from_file_location("wor165_store_builder", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_store_manifest_is_localized_mv3_and_has_required_icons():
    manifest = json.loads((EXTENSION / "manifest.json").read_text("utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["version"] == "1.0.0"
    assert manifest["default_locale"] == "en"
    assert manifest["permissions"] == APPROVED_PERMISSIONS
    assert manifest["incognito"] == "not_allowed"
    assert "host_permissions" not in manifest
    assert "content_scripts" not in manifest
    assert manifest["name"] == "__MSG_extensionName__"
    assert manifest["description"] == "__MSG_extensionDescription__"

    for size in (16, 32, 48, 128):
        path = EXTENSION / manifest["icons"][str(size)]
        with Image.open(path) as image:
            assert image.size == (size, size)
            assert image.format == "PNG"

    for locale in ("en", "fr"):
        messages = json.loads(
            (EXTENSION / "_locales" / locale / "messages.json").read_text("utf-8")
        )
        assert messages["extensionName"]["message"] == "OSIA Personal Browser"
        assert len(messages["extensionDescription"]["message"]) <= 132


def test_shared_store_zip_is_deterministic_runtime_only_and_has_manifest_at_root(tmp_path):
    builder = _load_builder()
    first = builder.build(tmp_path / "first.zip")
    second = builder.build(tmp_path / "second.zip")
    first_bytes = (tmp_path / "first.zip").read_bytes()
    second_bytes = (tmp_path / "second.zip").read_bytes()

    assert first_bytes == second_bytes
    assert first["sha256"] == second["sha256"] == hashlib.sha256(first_bytes).hexdigest()
    assert first["size_bytes"] == len(first_bytes)
    assert first["extension_version"] == "1.0.0"
    assert first["browser_targets"] == [
        "chrome", "edge", "brave-via-chrome-web-store",
    ]

    expected = {
        "manifest.json",
        "service_worker.js",
        "content_agent.js",
        "icons/icon16.png",
        "icons/icon32.png",
        "icons/icon48.png",
        "icons/icon128.png",
        "_locales/en/messages.json",
        "_locales/fr/messages.json",
    }
    with zipfile.ZipFile(tmp_path / "first.zip") as archive:
        names = archive.namelist()
        assert set(names) == expected
        assert names == sorted(names)
        assert names.count("manifest.json") == 1
        packaged = json.loads(archive.read("manifest.json"))
        assert packaged["manifest_version"] == 3
        assert packaged["permissions"] == APPROVED_PERMISSIONS
        assert "key" not in packaged
        assert "host_permissions" not in packaged
        assert "metadata.json" not in names
        assert all(not name.endswith((".map", ".log", ".tmp", ".pyc")) for name in names)
        assert all(".git" not in name and "__pycache__" not in name for name in names)

    contents = json.loads((tmp_path / "first.contents.json").read_text("utf-8"))
    assert contents["sha256"] == first["sha256"]
    assert {item["path"] for item in contents["files"]} == expected


def test_store_copy_permissions_privacy_and_review_flow_match_implementation():
    locales = {}
    for locale in ("en", "fr"):
        data = json.loads((STORE / f"{locale}.json").read_text("utf-8"))
        locales[locale] = data
        assert data["extension_name"] == "OSIA Personal Browser"
        assert len(data["short_description"]) <= 132
        assert 250 <= len(data["full_description"]) <= 10_000
        assert set(data["permission_justifications"]) == set(APPROVED_PERMISSIONS)
        assert len(data["reviewer_test_instructions"]) == 10
        assert "/browser personal" in data["full_description"]
        assert "example.com" in " ".join(data["reviewer_test_instructions"])

    en_messages = json.loads(
        (EXTENSION / "_locales" / "en" / "messages.json").read_text("utf-8")
    )
    fr_messages = json.loads(
        (EXTENSION / "_locales" / "fr" / "messages.json").read_text("utf-8")
    )
    assert locales["en"]["short_description"] == en_messages[
        "extensionDescription"
    ]["message"]
    assert locales["fr"]["short_description"] == fr_messages[
        "extensionDescription"
    ]["message"]

    privacy = (STORE / "privacy-disclosures.md").read_text("utf-8")
    policy = (STORE / "privacy-policy-proposal.md").read_text("utf-8")
    screenshots = (STORE / "screenshot-capture-spec.md").read_text("utf-8")
    trust = (STORE / "production-trust.md").read_text("utf-8")
    assert "Do not select “no user data handling.”" in privacy
    assert "selected a cloud AI provider" in privacy
    assert "13. Outils et contrôle de l'ordinateur" in policy
    assert "not yet an adequate production store privacy-policy source" in policy
    assert "https://example.com" in screenshots
    assert "1280 × 800" in screenshots
    assert "OSIA_CHROME_EXTENSION_ID" in trust
    assert "OSIA_EDGE_EXTENSION_ID" in trust
    assert "Do not create a Brave-specific package" in trust


def test_generated_store_assets_have_exact_dimensions_and_digests():
    manifest_path = STORE / "assets" / "assets-manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert manifest["schema_version"] == 1
    for item in manifest["assets"]:
        path = ROOT / item["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        with Image.open(path) as image:
            assert image.size == (item["width"], item["height"])
    dimensions = {
        (item["width"], item["height"])
        for item in manifest["assets"]
    }
    assert {(128, 128), (300, 300), (440, 280), (1400, 560)} <= dimensions
