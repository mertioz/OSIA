# OSIA Linux x86-64

OSIA Linux is a beta package for a desktop session on Linux x86-64 or an OSIA
server launched from the terminal. It uses the shared OSIA Desktop runtime,
Qt for Python on Linux, and a portable Python 3.12 interpreter. The installer
downloads Python dependencies from the publishers using the bundled hashes
and installs Chromium and Firefox from Playwright. Allow at least 6 GB of
free space for installation. The desktop launcher requires an X11 session or
a working XWayland display. Access to native controls depends on the display
session and its input permissions.

## Install

Download the versioned archive and its SHA-256 file from download.osia.space
and run the following commands as your desktop user. Install the system
packages with your distribution package manager first.

```sh
sha256sum -c OSIA-Linux-x86_64.tar.xz.sha256
tar -xf OSIA-Linux-x86_64.tar.xz
cd OSIA-Linux-x86_64
./install.sh
~/.local/bin/osia --doctor
```

For Ubuntu 24.04, install the system packages with:

```sh
sudo apt install build-essential libgl1 libegl1 libxcb-cursor0 libxkbcommon-x11-0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 \
  libx11-xcb1 libnss3 libnspr4 libgtk-3-0t64 libasound2t64 libgbm1 libcups2t64 \
  libatk-bridge2.0-0t64 libxss1 libdbus-glib-1-2 libxt6t64 libxtst6 \
  ffmpeg libportaudio2 libsndfile1 xdotool wmctrl xclip scrot
```

Other x86-64 distributions provide equivalent system libraries, GCC and
Linux input headers through their package manager. See the supported
environments and test results on the release page. Browser dependencies
depend on the Linux distribution and can be checked with Playwright's host
diagnostics. The package does not require root privileges to install OSIA.

Launch OSIA from your application menu. The user service runs the mobile
server on systems with a systemd user manager. To start the same server
directly, run `~/.local/bin/osia --server`. Browser and desktop tools need a
display session when the server runs on a remote machine. Local AI models
are installed separately. The complete Codex runtime for ChatGPT Account is
included. Connect an account through OSIA and review the provider's terms.

## Updates

Use the same install.sh from each verified release. The installer keeps the
stable command at `~/.local/bin/osia`, installs into
`${XDG_DATA_HOME:-~/.local/share}/osia/releases/<version>` and switches the
`current` symlink after staging the new version. Runtime data remains in the
OSIA user profile. The previous release can be selected using its verified
archive and `./install.sh --rollback`. Back up the profile before testing a
rollback across a data format change.

## Build

Run `packaging/linux/prepare_runtime.sh WORK_DIR REPORT_DIR` on Linux x86-64
with bash, curl, gcc, coreutils and tar. Use
`packaging/linux/build_linux.sh WORK_DIR REPORT_DIR OUTPUT_DIR` from the
clean release commit. This creates the stable archive name, SHA-256 file
and build metadata. Versioned artifacts and their hashes are immutable.

## Release signatures

Linux releases use a detached OpenPGP signature of the exact published archive.
The primary OSIA Release Signing key fingerprint is
`5980 65BD 2712 E46B 4DF7 624C 0D99 E609 887B 0F28`.
Its public key is tracked in `packaging/linux/keys/osia-release-signing.asc`.
The operator's private key, encrypted passphrase, backup and revocation
certificate are stored in the local release signing vault, outside Git.

After building and checking an archive, run `packaging/linux/sign_release.py`
with its original manifest, the signing key fingerprint and protected keyring.
Supply the passphrase on standard input. The local bounded Windows entry point
is `%LOCALAPPDATA%/OSIA-Release/Signing/Invoke-OSIALinuxSigning.ps1`.
For a new release, update that local wrapper's explicit version, source and
artifact pins after verification.
The signer validates the release identity, size and SHA-256 before signing.
It verifies the result with a separate public-key-only keyring.

Publish the archive's `.asc`, the content-addressed public key and the signed
manifest and its `.asc` as new immutable objects. Keep the original archive,
checksum and build manifest at their existing URLs. The signature-enriched
manifest goes under `linux/<version>/signing/release-manifest.json`.
Only after public verification, update the Linux Stable channel and site
metadata to include the signature and key fingerprint. Keep the existing
installer identity, version, release sequence and manual update behavior.
