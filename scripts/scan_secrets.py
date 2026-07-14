#!/usr/bin/env python3
"""Sanitized, deterministic secret scanning for OSIA repositories."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

GITLEAKS_VERSION = "8.30.1"
MAX_MEMBER_BYTES = 20 * 1024 * 1024
PLACEHOLDER_WORDS = (
    "changeme",
    "dummy",
    "example",
    "fake",
    "placeholder",
    "replace_me",
    "sample",
    "test-only",
    "your_",
    "your-",
)

# These rules describe credential structure. They intentionally never retain or
# print matching values.
STRUCTURED_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "api-compatible-key",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "anthropic-key",
        re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("groq-key", re.compile(r"\bgsk_[A-Za-z0-9_-]{20,}\b")),
    (
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    (
        "stripe-live-key",
        re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
    ),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "jwt-token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "authorization-header",
        re.compile(r"(?im)^\s*authorization\s*:\s*(?:bearer|basic)\s+[^\s<>{}]{8,}\s*$"),
    ),
    (
        "basic-auth-url",
        re.compile(r"\bhttps?://[^/\s:@]+:[^/\s@]+@[A-Za-z0-9.-]+"),
    ),
    (
        "database-url-password",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://"
            r"[^/\s:@]+:[^/\s@]+@"
        ),
    ),
    (
        "private-registry-token",
        re.compile(r"(?im)^\s*(?://[^\s:]+/?:)?_authToken\s*=\s*[^\s<>{}]{8,}\s*$"),
    ),
    (
        "sentry-dsn",
        re.compile(r"\bhttps://[A-Fa-f0-9]{16,}@o\d+\.ingest(?:\.[A-Za-z0-9-]+)?\.sentry\.io/\d+\b"),
    ),
    (
        "pem-private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    ),
    (
        "tracked-certificate",
        re.compile("-----BEGIN " + "CERTIFICATE-----"),
    ),
)

LITERAL_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    \b(
        api[_-]?key|client[_-]?secret|cloudflare[_-]?(?:api[_-]?)?token|
        oauth[_-]?(?:access|refresh)?[_-]?token|password|passwd|credential|
        private[_-]?key|registry[_-]?token|signing[_-]?password|
        keystore[_-]?password|turn[_-]?(?:secret|credential|password)|
        mobile[_-]?token|relay[_-]?(?:token|proof)|pairing[_-]?(?:id|proof)
    )\b
    \s*["']?\s*[:=]\s*
    (?:"([^"\r\n]{12,})"|'([^'\r\n]{12,})')
    """
)
ENV_ASSIGNMENT_RE = re.compile(
    r"""(?ixm)^\s*
    (?:API[_-]?KEY|CLIENT[_-]?SECRET|CLOUDFLARE[_-]?(?:API[_-]?)?TOKEN|
       OAUTH[_-]?(?:ACCESS|REFRESH)?[_-]?TOKEN|PASSWORD|CREDENTIAL|
       REGISTRY[_-]?TOKEN|SIGNING[_-]?PASSWORD|KEYSTORE[_-]?PASSWORD|
       TURN[_-]?(?:SECRET|CREDENTIAL|PASSWORD)|MOBILE[_-]?TOKEN|
       RELAY[_-]?(?:TOKEN|PROOF))
    \s*=\s*([^\s<>{}]{12,})\s*$
    """
)
CREDENTIAL_VALUE_RE = re.compile(r"[A-Za-z0-9_./+=:@-]{12,}")
PRIVATE_IP_RE = re.compile(
    r"(?<!\d)(?:10\.(?:\d{1,3}\.){2}\d{1,3}|"
    r"192\.168\.(?:\d{1,3})\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3})\.\d{1,3})(?!\d)"
)
LOCAL_HOME_RE = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s<>]+[\\/]|"
    r"/(?:home|Users)/[^/\s<>]+/)"
)

SENSITIVE_FILE_NAMES = {
    ".dev.vars": "development-secret-store",
    ".env": "environment-secret-store",
    "api_keys.json": "api-key-store",
    "key.properties": "signing-credential-store",
    "lan_direct_identity.json": "lan-identity-store",
    "lan_direct_key.pem": "lan-private-key",
    "mobile_token.txt": "mobile-token-store",
    "oauth_tokens.json": "oauth-token-store",
    "relay_device.json": "relay-device-store",
    "settings.json": "runtime-settings",
}
SENSITIVE_SUFFIXES = {
    ".db": "database-artifact",
    ".jks": "signing-keystore",
    ".keystore": "signing-keystore",
    ".p12": "pkcs-private-material",
    ".pfx": "pkcs-private-material",
    ".pkcs12": "pkcs-private-material",
    ".sqlite": "database-artifact",
}
USER_DATA_NAME_RE = re.compile(
    r"(?i)(?:conversation|chat_export|task_state|cron_state|secure_storage_export|"
    r"pairing_record|oauth_token|api_keys?)(?:[._-]|$)"
)

# Exact path+rule exceptions for deliberately invalid/synthetic test material.
# They are regression-tested; structured provider rules remain active everywhere.
OSIA_ALLOWLIST = {
    ("tools/test_gmail_write_safety.py", "high-entropy-credential"),
    ("tools/test_memory_store.py", "pem-private-key"),
    ("tools/test_mobile_model_catalogue.py", "high-entropy-credential"),
}


@dataclass(frozen=True, order=True)
class Finding:
    source: str
    rule: str
    path: str
    commit: str = ""
    date: str = ""

    def public_id(self, repository: str) -> str:
        # This identifier is derived only from public metadata, never the match.
        material = "\n".join(
            (repository, self.source, self.rule, self.path, self.commit, self.date)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def _git(repo: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise RuntimeError(f"git command failed: {' '.join(args)}")
    return result.stdout


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {char: value.count(char) for char in set(value)}
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())


def _looks_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(word in lowered for word in PLACEHOLDER_WORDS) or len(set(value)) <= 3


def _path_rule(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1].lower()
    if name.endswith(".example") or ".example." in name:
        return None
    if name in SENSITIVE_FILE_NAMES:
        return SENSITIVE_FILE_NAMES[name]
    if (
        name.startswith(".env") or name.startswith(".dev.vars")
    ) and "example" not in name:
        return "environment-secret-store"
    for suffix, rule in SENSITIVE_SUFFIXES.items():
        if name.endswith(suffix):
            return rule
    if USER_DATA_NAME_RE.search(name) and Path(name).suffix.lower() in {
        ".csv",
        ".db",
        ".dump",
        ".json",
        ".log",
        ".sqlite",
        ".txt",
    }:
        return "user-runtime-data"
    return None


def _scan_text(text: str, path: str, source: str, commit: str, date: str) -> set[Finding]:
    findings: set[Finding] = set()
    path_rule = _path_rule(path)
    if path_rule:
        findings.add(Finding(source, path_rule, path, commit, date))

    for rule, pattern in STRUCTURED_RULES:
        if pattern.search(text):
            findings.add(Finding(source, rule, path, commit, date))

    candidates = [
        match.group(2) or match.group(3)
        for match in LITERAL_ASSIGNMENT_RE.finditer(text)
    ]
    env_suffixes = {
        "",
        ".conf",
        ".env",
        ".ini",
        ".md",
        ".properties",
        ".toml",
        ".txt",
        ".vars",
        ".yaml",
        ".yml",
    }
    if Path(path.split("!", 1)[-1]).suffix.lower() in env_suffixes:
        candidates.extend(match.group(1) for match in ENV_ASSIGNMENT_RE.finditer(text))
    for value in candidates:
        if (
            CREDENTIAL_VALUE_RE.fullmatch(value)
            and not _looks_placeholder(value)
            and _entropy(value) >= 3.35
        ):
            findings.add(Finding(source, "high-entropy-credential", path, commit, date))

    lowered = text.lower()
    type_marker = '"ty' + 'pe"'
    private_key_marker = '"private_' + 'key"'
    if type_marker in lowered and "service_account" in lowered and private_key_marker in lowered:
        findings.add(Finding(source, "service-account-json", path, commit, date))

    data_suffixes = {".conf", ".env", ".ini", ".json", ".md", ".toml", ".txt", ".yaml", ".yml"}
    if Path(path.split("!", 1)[-1]).suffix.lower() in data_suffixes and PRIVATE_IP_RE.search(text):
        findings.add(Finding(source, "private-network-identifier", path, commit, date))

    for match in LOCAL_HOME_RE.finditer(text):
        candidate = match.group(0).replace("\\", "/").lower()
        generic_markers = (
            "%username%",
            "%userprofile%",
            "$env:userprofile",
            "/home/osia/",
            "/home/user/",
            "/home/username/",
            "/users/user/",
            "/users/username/",
            "<user>",
            "<username>",
            "example",
            "your-user",
        )
        if not any(marker in candidate for marker in generic_markers):
            findings.add(Finding(source, "personal-local-path", path, commit, date))
            break

    return findings


def _scan_archive(
    data: bytes,
    path: str,
    source: str,
    commit: str,
    date: str,
    depth: int = 0,
) -> set[Finding]:
    if depth >= 3:
        return set()
    findings: set[Finding] = set()
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                        continue
                    member = archive.read(info)
                    findings.update(
                        _scan_bytes(
                            member,
                            f"{path}!{info.filename}",
                            source,
                            commit,
                            date,
                            depth + 1,
                        )
                    )
            return findings
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return findings

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member_info in archive.getmembers():
                if not member_info.isfile() or member_info.size > MAX_MEMBER_BYTES:
                    continue
                extracted = archive.extractfile(member_info)
                if extracted is None:
                    continue
                findings.update(
                    _scan_bytes(
                        extracted.read(),
                        f"{path}!{member_info.name}",
                        source,
                        commit,
                        date,
                        depth + 1,
                    )
                )
    except (OSError, tarfile.TarError):
        pass
    return findings


def _scan_bytes(
    data: bytes,
    path: str,
    source: str,
    commit: str = "",
    date: str = "",
    depth: int = 0,
) -> set[Finding]:
    findings = _scan_archive(data, path, source, commit, date, depth)
    text = data.decode("utf-8", errors="ignore")
    findings.update(_scan_text(text, path, source, commit, date))
    return findings


def _current_paths(repo: Path) -> list[str]:
    raw = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted(
        {
            item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            for item in raw.split(b"\0")
            if item
        }
    )


def scan_current(repo: Path) -> set[Finding]:
    findings: set[Finding] = set()
    for relative in _current_paths(repo):
        file_path = repo / Path(relative)
        if not file_path.is_file() or file_path.stat().st_size > MAX_MEMBER_BYTES:
            continue
        findings.update(_scan_bytes(file_path.read_bytes(), relative, "osia-current"))
    return {
        finding
        for finding in findings
        if (finding.path, finding.rule) not in OSIA_ALLOWLIST
    }


def scan_history(repo: Path) -> set[Finding]:
    findings: set[Finding] = set()
    raw_log = _git(
        repo,
        "log",
        "--all",
        "--full-history",
        "--reverse",
        "--date-order",
        "--root",
        "-m",
        "--no-renames",
        "--diff-filter=AMCR",
        "--raw",
        "--no-abbrev",
        "-z",
        "--format=%x1e%H%x00%cI%x00%B%x00",
    )
    records: dict[tuple[str, str], tuple[str, str]] = {}
    for chunk in raw_log.split(b"\x1e"):
        fields = chunk.split(b"\0")
        if len(fields) < 3:
            continue
        commit = fields[0].strip().decode("ascii", errors="ignore")
        date = fields[1].decode("utf-8", errors="ignore")
        message = fields[2].decode("utf-8", errors="ignore")
        if not commit:
            continue
        findings.update(_scan_text(message, "<commit-message>", "osia-history", commit, date))
        index = 3
        while index + 1 < len(fields):
            metadata = fields[index].lstrip(b"\r\n")
            if not metadata.startswith(b":"):
                index += 1
                continue
            parts = metadata.split()
            if len(parts) >= 5:
                oid = parts[3].decode("ascii", errors="ignore")
                relative = fields[index + 1].decode(
                    "utf-8", errors="surrogateescape"
                ).replace("\\", "/")
                if oid and relative:
                    records.setdefault((oid, relative), (commit, date))
            index += 2

    by_oid: dict[str, list[tuple[str, str, str]]] = {}
    for (oid, relative), (commit, date) in records.items():
        by_oid.setdefault(oid, []).append((relative, commit, date))

    process = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        for oid in sorted(by_oid):
            process.stdin.write((oid + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) < 3:
                continue
            size = int(header[2])
            data = process.stdout.read(size)
            process.stdout.read(1)
            if header[1] != b"blob" or size > MAX_MEMBER_BYTES:
                continue
            for relative, commit, date in by_oid[oid]:
                findings.update(
                    _scan_bytes(data, relative, "osia-history", commit, date)
                )
    finally:
        process.stdin.close()
        process.wait(timeout=30)

    tags = _git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)%00%(taggerdate:iso-strict)%00%(contents)%00",
        "refs/tags",
    ).split(b"\0")
    for index in range(0, len(tags) - 2, 3):
        tag = tags[index].decode("utf-8", errors="ignore")
        date = tags[index + 1].decode("utf-8", errors="ignore")
        contents = tags[index + 2].decode("utf-8", errors="ignore")
        if tag:
            findings.update(
                _scan_text(contents, f"<tag-message:{tag}>", "osia-history", tag, date)
            )
    return {
        finding
        for finding in findings
        if (finding.path, finding.rule) not in OSIA_ALLOWLIST
    }


def _gitleaks_binary() -> str:
    configured = os.environ.get("GITLEAKS_BIN")
    binary = configured or shutil.which("gitleaks")
    if not binary:
        raise RuntimeError(
            f"Gitleaks {GITLEAKS_VERSION} is required; set GITLEAKS_BIN or install it from "
            f"https://github.com/gitleaks/gitleaks/releases/tag/v{GITLEAKS_VERSION}"
        )
    version = subprocess.run(
        [binary, "version"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if version.returncode or GITLEAKS_VERSION not in version.stdout:
        raise RuntimeError(f"Gitleaks {GITLEAKS_VERSION} is required exactly")
    return binary


def _gitleaks_findings(repo: Path, history: bool) -> set[Finding]:
    binary = _gitleaks_binary()
    source = "gitleaks-history" if history else "gitleaks-current"
    config = repo / ".gitleaks.toml"
    with tempfile.TemporaryDirectory(prefix="osia-secret-scan-") as temp_name:
        temp_root = Path(temp_name)
        try:
            temp_root.chmod(0o700)
        except OSError:
            pass
        report = temp_root / "gitleaks.json"
        command = [
            binary,
            "git" if history else "dir",
            "--no-banner",
            "--no-color",
            "--redact=100",
            "--max-archive-depth=3",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ]
        if config.is_file():
            command.extend(("--config", str(config)))
        if history:
            command.extend(("--log-opts=--all --full-history", str(repo)))
            scan_root: Path | None = None
        else:
            scan_root = temp_root / "tracked"
            scan_root.mkdir()
            for relative in _current_paths(repo):
                source_path = repo / Path(relative)
                if not source_path.is_file():
                    continue
                destination = scan_root / Path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination)
            command.append(str(scan_root))

        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError("Gitleaks execution failed; output suppressed for safety")
        if not report.exists() or not report.read_text(encoding="utf-8").strip():
            return set()

        parsed = json.loads(report.read_text(encoding="utf-8"))
        findings: set[Finding] = set()
        for item in parsed:
            path = str(item.get("File") or "<unknown>").replace("\\", "/")
            if scan_root is not None:
                root_text = str(scan_root).replace("\\", "/").rstrip("/") + "/"
                if path.lower().startswith(root_text.lower()):
                    path = path[len(root_text) :]
            findings.add(
                Finding(
                    source,
                    str(item.get("RuleID") or "unknown-rule"),
                    path,
                    str(item.get("Commit") or ""),
                    str(item.get("Date") or ""),
                )
            )
        return findings


def _emit(findings: Iterable[Finding], repository: str, json_output: bool) -> None:
    ordered = sorted(set(findings))
    if json_output:
        payload = []
        for finding in ordered:
            item = asdict(finding)
            item["id"] = finding.public_id(repository)
            payload.append(item)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    if not ordered:
        print("Secret scan clean.")
        return
    for finding in ordered:
        commit = finding.commit or "<current>"
        date = finding.date or "<current>"
        print(
            "FINDING "
            f"source={finding.source} rule={finding.rule} path={finding.path} "
            f"commit={commit} date={date} id={finding.public_id(repository)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="scan every reachable Git ref")
    parser.add_argument(
        "--osia-only",
        action="store_true",
        help="run only the built-in deterministic scanner (CI runs Gitleaks first)",
    )
    parser.add_argument("--json", action="store_true", help="emit sanitized JSON")
    args = parser.parse_args()

    repo = Path(
        _git(Path.cwd(), "rev-parse", "--show-toplevel").decode("utf-8").strip()
    ).resolve()
    repository = repo.name
    findings = scan_history(repo) if args.history else scan_current(repo)
    if not args.osia_only:
        findings.update(_gitleaks_findings(repo, args.history))
    _emit(findings, repository, args.json)
    return 1 if findings else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
