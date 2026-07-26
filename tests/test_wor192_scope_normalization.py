from __future__ import annotations

import json
from pathlib import Path

import pytest

from osia.scope_normalization import (
    ScopeNormalizationError,
    WebScopeKind,
    canonical_serialize,
    normalize_command_scope,
    normalize_file_scope,
    normalize_gmail_scope,
    normalize_mcp_scope,
    normalize_purchase_scope,
    normalize_web_scope,
    sensitive_hash_equal,
)


def _private(scope):
    return json.loads(scope.private_canonical)


def test_windows_paths_are_absolute_case_stable_and_separator_stable():
    left = normalize_file_scope(
        r"Folder/Item.txt", base=r"C:\Fixture", recursive=False,
    )
    right = normalize_file_scope(
        r"folder\item.TXT", base=r"c:\fixture", recursive=False,
    )
    assert left.scope_hash == right.scope_hash
    assert _private(left)["path"] == r"c:\fixture\folder\item.txt"


def test_unc_is_preserved_and_device_or_parent_paths_are_rejected():
    unc = normalize_file_scope(
        r"\\server\share\Folder\item.txt", base=r"C:\Fixture",
    )
    assert _private(unc)["path"].startswith(r"\\server\share")
    with pytest.raises(ScopeNormalizationError, match="unsupported_device_path"):
        normalize_file_scope(r"\\?\C:\Windows\x", base=r"C:\Fixture")
    with pytest.raises(ScopeNormalizationError, match="unsupported_device_path"):
        normalize_file_scope(r"C:\Fixture\NUL.txt", base=r"C:\Fixture")
    with pytest.raises(ScopeNormalizationError, match="parent_traversal_ambiguous"):
        normalize_file_scope(r"Folder\..\other", base=r"C:\Fixture")


def test_recursive_scope_is_explicit_and_private_value_is_not_in_repr(tmp_path):
    sentinel = "WOR192_PRIVATE_PATH_SENTINEL"
    scope = normalize_file_scope(
        tmp_path / sentinel, base=tmp_path, recursive=True,
    )
    assert scope.recursive is True
    assert scope.safe_category == "directory_tree"
    assert sentinel not in repr(scope)


def test_url_origin_host_port_idna_and_trailing_dot_are_canonical():
    left = normalize_web_scope(
        "https://TÄST.example.:443", kind=WebScopeKind.EXACT_ORIGIN,
    )
    right = normalize_web_scope(
        "https://xn--tst-qla.example", kind=WebScopeKind.EXACT_ORIGIN,
    )
    assert left.scope_hash == right.scope_hash
    assert _private(left)["identity"] == "https://xn--tst-qla.example"


def test_url_rejects_userinfo_and_distinguishes_exact_and_wildcard():
    with pytest.raises(ScopeNormalizationError, match="userinfo_not_allowed"):
        normalize_web_scope(
            "https://user:password@example.test/", kind=WebScopeKind.EXACT_URL,
        )
    exact = normalize_web_scope(
        "https://example.test", kind=WebScopeKind.EXACT_ORIGIN,
    )
    wildcard = normalize_web_scope(
        "https://*.example.test", kind=WebScopeKind.SUBDOMAIN_WILDCARD,
    )
    assert exact.scope_hash != wildcard.scope_hash


def test_exact_url_normalizes_path_and_ignores_fragment_identity():
    left = normalize_web_scope(
        "https://example.test/a/../b#display", kind=WebScopeKind.EXACT_URL,
    )
    right = normalize_web_scope(
        "https://EXAMPLE.test/b", kind=WebScopeKind.EXACT_URL,
    )
    assert left.scope_hash == right.scope_hash


def test_command_requires_argv_and_binds_executable_cwd_and_env(tmp_path):
    executable = str(Path(__import__("sys").executable).resolve())
    scope = normalize_command_scope(
        executable,
        ["-c", "print('fixture')"],
        cwd=tmp_path,
        env_allowlist=["PATH", "TEMP"],
    )
    payload = _private(scope)
    assert payload["resolved"] is True
    assert payload["argv"] == ["-c", "print('fixture')"]
    with pytest.raises(ScopeNormalizationError, match="argv_list_required"):
        normalize_command_scope(executable, "-c fixture", cwd=tmp_path)


def test_gmail_and_mcp_bind_stable_operation_account_and_schema():
    gmail = normalize_gmail_scope("account:opaque", "gmail_send_draft")
    mcp = normalize_mcp_scope(
        "server-1", "write_item", account_ref="tenant:opaque",
        tenant_ref="tenant:fixture", schema_hash="a" * 64,
    )
    assert _private(gmail)["operation"] == "gmail_send_draft"
    assert _private(mcp)["connector_id"] == "mcp:server-1"
    assert _private(mcp)["schema_hash"] == "a" * 64
    assert _private(mcp)["tenant_ref"] == "tenant:fixture"


def test_purchase_uses_minor_units_exact_currency_and_no_conversion():
    eur = normalize_purchase_scope(
        "https://merchant.example", 1234, "eur", "order-1",
    )
    usd = normalize_purchase_scope(
        "https://merchant.example", 1234, "USD", "order-1",
    )
    assert _private(eur)["amount_minor"] == 1234
    assert _private(eur)["currency"] == "EUR"
    assert eur.scope_hash != usd.scope_hash
    with pytest.raises(ScopeNormalizationError, match="minor_units_integer_required"):
        normalize_purchase_scope(
            "https://merchant.example", 12.34, "EUR", "order-1",
        )


def test_canonical_serialization_is_stable_and_hash_compare_is_exact():
    assert canonical_serialize({"b": [2, 1], "a": True}) == (
        '{"a":true,"b":[2,1]}'
    )
    assert sensitive_hash_equal("a" * 64, "a" * 64) is True
    assert sensitive_hash_equal("a" * 64, "b" * 64) is False
