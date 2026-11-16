# Windows installer implementation

Reference checked on 16 November 2026. Build/publication rules and acceptance
order have one maintained home: [AGENTS.md](../AGENTS.md) and
[Drive installer rules](https://drive.google.com/file/d/11nRvwHVxn0JzaZbKqE8Mzy6pEyVVBGK4/view).
This page describes the existing implementation rather than duplicating their
release checklist.

## Package and installed identity

The existing chain is PyInstaller `onedir` followed by Inno Setup. One
`OSIA.exe` with its `_internal` directory supports the GUI and internal service,
server, wake-recovery and maintenance modes. Inno installs per user under
`%LOCALAPPDATA%/Programs/OSIA` with the existing AppId, integration and
uninstaller. User settings, conversations, credentials, pairing and browser
profiles remain in the existing user-data locations outside that installation.
Normal uninstall preserves those profiles.

[build_windows.py](../packaging/windows/build_windows.py) declares the reviewed
tool/runtime versions, immutable download revisions, hashes, assets and notice
inventory. The complete Codex runtime is staged under `_internal/runtimes/codex`;
frozen discovery uses OSIA-owned runtime locations rather than developer PATH.
The old documentation's Codex 0.144.1 pin has been removed; the executable
`CODEX_RUNTIME_VERSION` declaration owns that value.

Kokoro voices, Whisper and native MP3 dependencies are staged by this chain.
Model hashes and dependency/license metadata are verified by its existing
builder. The pinned resource/dependency definitions are not duplicated here.

## Build entry point

From the clean authorized source commit on Windows x64, the Stable entry point
is:

```powershell
./venv/Scripts/python.exe ./packaging/windows/build_windows.py --channel stable
```

The builder's default channel is internal-preview; it does not substitute for
Stable. Generated outputs are `dist/OSIA/`, `release/windows/OSIA-Setup.exe`,
the checksum and release manifest. The source scripts own DLL isolation,
tracked-module collection, frozen smoke and payload integrity checks.

## Signing and verification status

Authenticode signing is conditional on an accessible certificate selected by
`OSIA_SIGN_SHA1`; the builder signs/verifies the application and installer when
configured. It can also produce the explicitly authorized unsigned Stable beta
under the bounded updater policy. The exact artifact's manifest and signature
verification establish its status; the executable must not be called signed
merely because Codex or another dependency has its own signature.

SmartScreen reputation is separate from those checks. Published guidance uses
the official HTTPS download and its checksum without disabling Windows security
protections. Existing third-party notices/source obligations are recorded in
the package legal inventory; copied notices alone are not a legal-clearance
claim. Commercial ChatGPT Account eligibility remains separately qualified.

The old pre-publication VM/OAuth checklist is removed. The current automated
checks and post-publication `OPERATOR_PENDING` scenarios are defined in the
maintained release workflow linked above.

## Integration and switches

The per-user OSIA Server task supervises the server. The direct transport,
private-LAN firewall scope and relay behavior are described once in
[webrtc-direct.md](webrtc-direct.md). Source launch/setup entry points remain
`start.bat` and `install.bat`.

Standard Inno switches and the existing isolated-test integration switch remain
available. Example for an explicitly chosen disposable installation path:

```powershell
./OSIA-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
  /SP- /NOINTEGRATION=1 /MERGETASKS="!firewall,!autostart" `
  /DIR="C:/Path With Spaces/OSIA"
```

The installer coordinates its existing owned tasks/processes and preserves the
profile. Its implementation and tests own upgrade/repair/rollback details; an
old source/native probe is not equivalent to acceptance of a new binary.

## Historical proofs

The [20 October portability audit](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-357-chatgpt-windows-portability.md) records the signed-out,
empty-PATH standalone runtime probe, exact original upstream LICENSE/NOTICE
hashes and qualifications. The [source delivery](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-357-source-delivery.md) records
the initial version epoch and Mobile Thinking ownership corrections. Their old
build/VM instructions are not current prerequisites.

[The original installer reference](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/windows-installer.md) retains the immutable
1.1.1 bridge and 1.1.5 rollback metadata as historical records, not current
rollback recommendations. The [0.9.0 publication report](WOR-349-windows-0.9.0-rc.md)
records that specific artifact's provenance and acceptance state.
