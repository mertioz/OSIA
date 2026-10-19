"""LAN certificate ACL probes must work when launched from PowerShell Core."""
from osia.lan_gateway import _windows_acl_environment


def test_acl_child_discovers_its_own_windows_modules(monkeypatch, tmp_path):
    monkeypatch.setenv("PSModulePath", "test-only-core-modules")
    monkeypatch.setenv("OSIA_TEST_SENTINEL", "test-only")
    target = tmp_path / "scratch.txt"
    environment = _windows_acl_environment(target)
    assert all(key.upper() != "PSMODULEPATH" for key in environment)
    assert environment["OSIA_LAN_ACL_TARGET"] == str(target)
    assert environment["OSIA_TEST_SENTINEL"] == "test-only"
    import os
    assert os.environ["PSModulePath"] == "test-only-core-modules"
