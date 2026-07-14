# Security Policy

## Supported versions

Until OSIA publishes versioned releases, security fixes are provided only for the
latest commit on the default branch. Older commits and private forks are not
supported.

## Reporting a vulnerability

Do not open a public issue, discussion, or pull request containing a
vulnerability, credential, token, private key, user data, or reproduction that
would expose one.

Use GitHub private vulnerability reporting for this repository if the owner has
enabled it. Otherwise, contact the maintainer through a private channel you have
already established. Before public release, the repository owner must review
this paragraph and configure a public, non-personal security contact. OSIA does
not currently claim a bug-bounty program.

Include the affected component and a minimal reproduction, but replace every
credential and user record with a non-secret synthetic value.

## Credential response

Treat a committed credential as compromised even after deleting it from the
current tree. The safe order is:

1. revoke or rotate the credential;
2. freeze repository writes and coordinate with every collaborator;
3. rewrite a fresh mirror with `git-filter-repo`;
4. rescan the rewritten mirror with both Gitleaks and the OSIA scanner;
5. force-push all rewritten branches and tags only after explicit approval;
6. invalidate old clones, forks, caches, artifacts, and release archives.

Never paste the credential into an issue, commit message, shell history, or scan
report. Keep replacement material outside the repository and delete it after
validation.

## Local secret scanning

The repository scanner requires Gitleaks **8.30.1** exactly. Install the official
release outside the repository, verify its published SHA-256 checksum, and set
`GITLEAKS_BIN` if it is not on `PATH`.

Run before every commit:

```text
python scripts/scan_secrets.py
python scripts/test_security_policy.py
```

To audit every locally reachable branch and tag:

```text
python scripts/scan_secrets.py --history
```

Scanner output is intentionally sanitized: it reports only rule, path,
commit/date metadata, and an identifier derived from that metadata—not the
matched value.

## Repository and hosting controls

Enable GitHub secret scanning and push protection when the repository becomes
public. Keep workflow permissions read-only unless a documented job requires
more. Third-party Actions must be pinned by full commit SHA.

## Local secret storage

OSIA PC stores primary API keys in the ignored `api_keys.json` store and OAuth
material in the separate ignored `oauth_tokens.json` store. Runtime
`settings.json`, mobile pairing state, relay identity, LAN identity/private
keys, task state, memory, logs, conversations, and screenshots are local user
data and must remain ignored. The tracked `config/settings.example.json`
contains non-secret defaults only.

The existing fallback-key contract can make runtime settings sensitive even
though the example contains an empty fallback map. Never commit or share a real
runtime settings file. Do not place API keys, OAuth tokens, TURN credentials, or
pairing material in documentation or examples.
