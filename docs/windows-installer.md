# OSIA Windows distribution

The Windows release is a self-contained, per-user installer. It does not
require Python, Git, Node.js, FFmpeg, administrator rights for the application,
or a network connection after download.

## Architecture

- **PyInstaller 6.21 `onedir`** freezes one `OSIA.exe` plus an `_internal`
  directory. The same signed executable runs the GUI and private internal modes
  for the mobile server supervisor, server process, wake recovery, and installer
  maintenance. `onedir` avoids the startup extraction, temporary executable,
  and antivirus surface of a multi-gigabyte `onefile` build.
- **Inno Setup 6** installs per user into
  `%LOCALAPPDATA%\Programs\OSIA`. It provides Start Menu integration, optional
  desktop and login shortcuts, upgrade/repair behavior, rollback hooks, silent
  switches, and a normal uninstaller.
- The background `OSIA Server` Scheduled Task is per-user and least-privileged.
  It supervises the current direct-first transport: LAN, then WebRTC, then the
  public relay. The old Cloudflare quick-tunnel task and binary are not shipped.
- Source installs use the same server-only policy: `install.bat` neither
  downloads cloudflared nor registers a tunnel task, and `start.bat` launches
  only the GUI while the normal server task stays supervised. Upgrade and
  uninstall retain targeted removal of historical OSIA tunnel tasks/processes.
- Offline speech assets are pinned by immutable Hugging Face revisions and
  SHA-256 hashes: Kokoro-82M with the `af_sky`/`ff_siwis` voices, and
  faster-whisper-small. MP3 encoding is in-process through PyAV; no external
  `ffmpeg.exe` is required.
- The managed ChatGPT-account provider uses the pinned Codex 0.144.1 standalone
  package shipped inside OSIA. It is resolved before any machine-wide `PATH`
  entry, so users do not need to install the Codex CLI separately. Its isolated
  persistent profile remains under `%LOCALAPPDATA%\OSIA\Codex`.
- User settings, API/OAuth tokens, conversations, memories, certificates,
  skills, and browser profiles remain outside the installation under the
  existing OSIA user-data paths. Normal uninstall deliberately preserves them.

## Reproducible build

Build on Windows x64 with the repository's Python 3.12 virtual environment:

```powershell
.\venv\Scripts\python.exe -m pip install `
  pyinstaller==6.21.0 pyinstaller-hooks-contrib==2026.6
winget install --id JRSoftware.InnoSetup --version 6.7.3 --silent `
  --accept-package-agreements --accept-source-agreements
.\venv\Scripts\python.exe .\packaging\windows\build_windows.py
```

The build downloads only the exact pinned model files when they are absent from
the local cache, verifies every model/card hash, creates branding from the
official OSIA assets, inventories dependency versions and notices, performs a
package secret scan, and runs the frozen smoke probe before compiling Inno.

Outputs are ignored by Git:

- `dist\OSIA\` — unpacked frozen application
- `release\windows\OSIA-Setup.exe` — installer
- matching `.sha256` and `release-manifest.json`

## Code signing

The build signs `OSIA.exe` before installer compilation and signs the final
installer when `OSIA_SIGN_SHA1` contains the SHA-1 thumbprint of an accessible
Authenticode certificate. The signing stage uses SHA-256 and an RFC 3161
timestamp, then verifies the signature.

WOR-183 has an explicit product-approved exception for stable releases made
before WOR-220. `TEMPORARY_UNSIGNED_STABLE_POLICY` bounds that exception to the
exact official stable manifest endpoint, OSIA product/platform/architecture,
`download.osia.space`, and the exact versioned installer path. The manifest's
byte size and SHA-256 remain mandatory, and the complete download is rehashed
before execution. An arbitrary `signed: false` manifest is not trusted. Remove
this isolated policy when WOR-220 introduces recognized Authenticode signing.

The public Windows 1.1.0, 1.1.1, and 1.1.2 installers are intentionally unsigned and
their manifests record `signed: false` with no publisher claim. Windows may show **Unknown
publisher** / **Éditeur inconnu** or a Microsoft Defender SmartScreen warning.
Users should download only from the official OSIA HTTPS domain, verify the
published SHA-256, and may choose **More info** / **Informations
complémentaires**, then **Run anyway** / **Exécuter quand même** when they trust
that official download. They must not be told to disable SmartScreen, Microsoft
Defender, antivirus software, or other Windows security protections. WOR-220
owns Authenticode signing for future public releases.

Windows 1.1.1 and older Stable binaries cannot receive the WOR-226 source
correction before their manifest check. They may reject the next unsigned
Stable release with `stable_unsigned` (sometimes reported as
`stable_notsigned`). A Windows 1.1.1 installation with Preview explicitly
enabled can use the existing unsigned-preview path; otherwise use the official
immutable installer directly. The validated rollback bridge remains:

- `https://download.osia.space/windows/1.1.1/OSIA-Setup.exe`
- 1,185,800,867 bytes
- SHA-256 `519c64d75eb4d793f6bf1c50bba64a3b7d3c8c2e81092df230da7724d6fd1e10`

Verify both values, close OSIA, and run the installer in place. Do not uninstall
first, clear `%USERPROFILE%\.osia`, or disable SmartScreen, Defender, antivirus,
or any other Windows protection.

The stock PyAV wheel and bundled eSpeak NG runtime require the documented GPL
source and notice process. See the installed `THIRD_PARTY_NOTICES.md`.

## Installer behavior and switches

Interactive install defaults to login launch and the narrow mobile firewall
rule; the desktop shortcut is optional. Only the firewall helper elevates, and
only when that task is selected. Its rule is inbound TCP 8766, Private profile,
`LocalSubnet`, scoped to the installed `OSIA.exe`.

Standard Inno switches are supported, including:

```powershell
# Fully silent isolated test install without tasks, firewall/UAC, or automatic launch
.\OSIA-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
  /SP- /NOINTEGRATION=1 /MERGETASKS="!firewall,!autostart" `
  /DIR="C:\Path With Spaces\OSIA"

# Silent uninstall
& "C:\Path With Spaces\OSIA\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

Upgrade first stops the existing OSIA-owned task/process tree, then replaces
files and re-registers integration. Cancellation/failure before completion
restores the previous integration best-effort. The installer never deletes
`~\.osia` or `%LOCALAPPDATA%\OSIA\Codex`; a future explicit “remove all data”
flow must be separate and opt-in.

## Validation gates

Before publishing, validate on a clean Windows x64 VM or Windows Sandbox when
available:

1. Install from a path containing spaces and non-ASCII characters with no
   Python/Git/Node/FFmpeg on `PATH`.
2. Confirm branding, real progress, details toggle, cancellation, retry, Start
   Menu/desktop/login choices, installed version metadata, and no console flash.
3. Launch OSIA; exercise text, local TTS, local STT, wake word, microphone and
   speaker behavior, browser launch, remote MCP/OAuth, cron/heartbeat, mobile
   LAN/WebRTC/relay reconnection, and single-instance enforcement.
4. Validate `OSIA Server` and wake recovery after logoff/login and suspend/resume;
   ensure there are no duplicate supervisors, listeners, or stale tunnel tasks.
5. Repair and upgrade over a populated user profile. Verify settings, tokens,
   conversations, memories, skills, OAuth state, and pairing survive.
6. Uninstall and verify processes/tasks, shortcuts, login entry, installation
   files, and the OSIA firewall rule are removed while user data remains.
7. Reinstall, run silent install/uninstall, verify the SHA-256 manifest and
   Authenticode chain, and scan the final payload for credentials.
