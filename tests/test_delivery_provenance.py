from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from tools import delivery_provenance as provenance
from tools.delivery_provenance import (
    RuntimeRecord,
    assess_runtime,
    compare_apk_hashes,
    inspect_artifact,
    inspect_repo,
    parse_adb_devices,
    select_device,
)


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _matching_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "OSIA Test")
    _git(repo, "config", "user.email", "osia-test@example.invalid")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    protected = repo / "osia" / "mcp_client" / "reconnect.py"
    protected.parent.mkdir(parents=True)
    protected.write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt", "osia/mcp_client/reconnect.py")
    _git(repo, "commit", "-m", "baseline")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    return repo, head


def _error_checks(checks):
    return [check for check in checks if check.severity == "error"]


def test_matching_head_passes(tmp_path):
    repo, head = _matching_repo(tmp_path)

    result, checks = inspect_repo(repo, label="pc", expected_sha=head)

    assert result["head"] == result["origin_main"] == head
    assert all(check.ok for check in _error_checks(checks))


def test_stale_primary_head_fails(tmp_path):
    repo, old_head = _matching_repo(tmp_path)
    _git(repo, "checkout", "-b", "upstream-simulation")
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "upstream")
    new_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", new_head)
    _git(repo, "checkout", "main")

    result, checks = inspect_repo(repo, label="pc")

    assert result["head"] == old_head
    assert result["origin_main"] == new_head
    assert not next(
        check for check in checks if check.name == "pc.head_matches_origin"
    ).ok


def test_stale_runtime_start_fails(tmp_path):
    repo, _head = _matching_repo(tmp_path)
    source = repo / "osia" / "main.py"
    source.write_text("# runtime\n", encoding="utf-8")
    record = RuntimeRecord("desktop", 41, str(source), started_at=100.0)

    checks = assess_runtime([record], pc_repo=repo, head_updated_at=200.0)

    assert not next(check for check in checks if check.name.endswith(".fresh")).ok


def test_missing_artifact_fails(tmp_path):
    repo, _head = _matching_repo(tmp_path)

    result, checks = inspect_artifact(
        repo / "build" / "app-release.apk",
        mobile_repo=repo,
        head_updated_at=0.0,
    )

    assert "sha256" not in result
    assert checks == [
        type(checks[0])("mobile.artifact.exists", False, "release APK is missing")
    ]


def test_wrong_apk_hash_fails():
    check = compare_apk_hashes("a" * 64, 10, "b" * 64, 10)

    assert not check.ok
    assert check.name == "mobile.installed_apk_matches_artifact"


def test_no_device_fails_clearly():
    devices = parse_adb_devices("List of devices attached\n\n")

    selected, check = select_device(devices)

    assert selected is None
    assert not check.ok
    assert "found 0" in check.detail


def test_windows_adb_space_separated_device_is_detected():
    devices = parse_adb_devices(
        "List of devices attached\n"
        "5C091JEBF03275         device product:tegu model:Pixel_9a device:tegu\n"
    )

    selected, check = select_device(devices)

    assert check.ok
    assert selected is not None
    assert selected["serial"] == "5C091JEBF03275"


def test_absent_android_package_is_not_a_probe_failure(monkeypatch):
    monkeypatch.setattr(
        provenance.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="", stderr=""
        ),
    )

    assert provenance._adb_package_paths(
        Path("adb"), "physical-device", "space.osia.app.test"
    ) == []


def test_protected_dirty_checkout_is_reported_without_mutation(tmp_path):
    repo, head = _matching_repo(tmp_path)
    protected = repo / "osia" / "mcp_client" / "reconnect.py"
    protected.write_text("local protected state\n", encoding="utf-8")
    before_bytes = protected.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_head = _git(repo, "rev-parse", "HEAD")
    before_status = _git(repo, "status", "--short")

    result, checks = inspect_repo(repo, label="pc", expected_sha=head)

    assert result["protected_dirty_paths"] == ["osia/mcp_client/reconnect.py"]
    assert result["unapproved_dirty_paths"] == []
    assert all(check.ok for check in _error_checks(checks))
    assert protected.read_bytes() == before_bytes
    assert hashlib.sha256(protected.read_bytes()).hexdigest() == before_hash
    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert _git(repo, "status", "--short") == before_status
