# Windows distribution

## Installed layout

The existing build chain is PyInstaller onedir followed by Inno Setup. The application is installed per user under %LOCALAPPDATA%/Programs/OSIA. Its AppId, integrations, profile preservation and uninstall behavior are implemented in [OSIA.iss](../packaging/windows/OSIA.iss).

One OSIA.exe and its _internal directory contain the GUI and private service/server, wake-recovery, document/media-worker and maintenance entry points. The user does not need a separate Python, Git or Node installation to run the packaged application. User settings, conversations, credentials, pairing and browser profiles remain in their established user-data locations outside the installation. Normal uninstall preserves those profiles.

[osia/runtime.py](../osia/runtime.py) resolves packaged/source resources and executable modes. [osia/config.py](../osia/config.py) owns CONFIG_DIR resolution; source and installed profiles are not selected by copying credential files into the bundle.

## Build entry point and outputs

From the clean authorized source checkout and its configured build environment on Windows x64:

    ./venv/Scripts/python.exe ./packaging/windows/build_windows.py --channel stable

The builder's default channel is internal-preview, so Stable is explicit. [build_windows.py](../packaging/windows/build_windows.py) owns the actual compiler/runtime versions, immutable download revisions, SHA-256 pins, resource staging and legal inventory. [osia.spec](../packaging/windows/osia.spec) and the runtime hooks own the frozen module/data layout.

The complete managed Codex distribution, including its required auxiliary executables/resources, is staged under _internal/runtimes/codex. Discovery uses OSIA-owned locations rather than assuming a developer PATH. Kokoro voices, Whisper, UI Automation resources and native MP3 dependencies are staged by the same existing chain.

Generated outputs include dist/OSIA/, release/windows/OSIA-Setup.exe, its checksum and release manifest. app_metadata.json binds the product/display version, update ordering, platform/channel and source commit to the bundle. Use those actual files rather than a version pin copied into this guide.

## Installation and update behavior

The per-user OSIA Server task supervises the server. Source setup/start entry points remain install.bat and start.bat. Direct transport and private-LAN firewall behavior are described in [webrtc-direct.md](webrtc-direct.md).

The installer coordinates its owned tasks/processes and writes its installed version/update sequence through the established upgrade logic. The implementation includes known legacy-sequence recovery and rejects inconsistent/newer installation states; the source and exact artifact metadata define the applicable paths.

Standard Inno command-line switches and the existing isolated-integration switch are available. For an explicitly chosen disposable installation path:

    ./OSIA-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /NOINTEGRATION=1 /MERGETASKS="!firewall,!autostart" /DIR="C:/Path With Spaces/OSIA"

The isolated switch is a separate integration path, not proof that a normal operator installation, migration or account flow succeeded.

## Signing and publication

OSIA_SIGN_SHA1 selects an accessible Authenticode certificate for the builder's signing/verification path. An explicitly authorized unsigned Stable beta is supported by the bounded updater policy. Determine the actual signature state from the artifact and its manifest; a dependency's signature does not sign OSIA.

SmartScreen reputation is separate from build-integrity checks. The official HTTPS download and checksum support artifact verification without disabling Windows security protections. The package legal inventory records required notices/source obligations; commercial ChatGPT Account eligibility remains separately qualified.

The maintained build, immutable publication, full-download verification, channel and post-publication operator-acceptance workflow is in [AGENTS.md](../AGENTS.md) and the [Drive installer rules](https://drive.google.com/file/d/11nRvwHVxn0JzaZbKqE8Mzy6pEyVVBGK4/view). This page supplies no additional VM/OAuth gate or release authorization. [Version policy](version-policy.md) explains source, built and loaded identities.
