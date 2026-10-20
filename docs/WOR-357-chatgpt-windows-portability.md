# WOR-357 / WOR-349 — ChatGPT Account Windows portability

Audit date: 2026-10-20. Source/configuration work only. No final Windows installer rebuild, publication, actual sign-in, operator credentials or inference was used.

## What the source actually ships

`packaging/windows/build_windows.py` stages the official standalone Codex **0.144.1** Windows x64 package. Its archive is `codex-package-x86_64-pc-windows-msvc.tar.gz`, pinned SHA-256 `ce94e1fb84693d3f7332ceeab5be73e93de38725da4b82dff863cd2c795b4730`. It verifies archive identity, required layout and executable version. PyInstaller copies the entire staged runtime into `_internal/runtimes/codex`; Inno retains the OSIA AppId and installs the complete onedir payload.

The package contains `bin/codex.exe`, `bin/codex-code-mode-host.exe`, `codex-path/rg.exe`, `codex-resources/codex-command-runner.exe`, `codex-resources/codex-windows-sandbox-setup.exe`, and `codex-package.json`. The launcher resolves the bundled path. AppServerClient checks OSIA-managed per-user runtime paths and then its bundled resource. No separate Node/npm/Python/Codex installation is required for this native subprocess by the packaging design. OSIA's Python/Qt dependencies remain the responsibility of the frozen application bundle, not system Python.

PE import inspection of all five packaged executables found Windows system DLLs (kernel32, ntdll, bcrypt/bcryptprimitives, WinHTTP, crypt32, sockets and related system libraries), with no separately imported Node/Python/VC runtime DLL. This static dependency observation is not an OS compatibility certification.

## Demonstrated corrections

1. Frozen discovery previously continued into a developer override and global PATH candidates. A working developer installation could therefore conceal an incomplete distributable. Frozen builds now resolve only OSIA-owned runtime locations and fail with `runtime_missing` if absent. Source development retains its existing explicit/global discovery compatibility.
2. App-server initialize metadata now identifies the real OSIA product version (0.9.0), replacing an unrelated hardcoded 1.0.0.
3. The standalone archive contains no LICENSE/NOTICE, and Python dependency inventory cannot include a non-pip runtime. The builder now copies the exact upstream tag's LICENSE/NOTICE into `legal/licenses/codex-0.144.1/`, verifies their hashes, and inventories Codex. Hashes: LICENSE `d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc`; NOTICE `9d71575ecfd9a843fc1677b0efb08053c6ba9fd686a0de1a6f5382fd3c220915`. This source correction is not a claim that older installers already contained these notices.

## Credential-free native probe actually performed

On Windows 11 Home 10.0.26200, the test copied only the redistributable native package to a temporary package root. PATH was empty, the developer override was absent, frozen resource resolution pointed to that package, and OSIA's persistent profile/workspace/temp were fresh temporary directories. No operator auth file was read or copied.

The real pinned executable was discovered from that package, App Server initialized over JSONL stdio, health passed, and `account/read` with `refreshToken=false` reported **signed out**. Shutdown/restart and a second signed-out status passed. The child environment contained no API key/access-token variables. A missing-package test proves frozen discovery does not consult global PATH. This is an isolated **dependency/startup** test on the existing host, not a clean Windows VM or a complete OSIA installer test. No accessible Windows Sandbox/Hyper-V test environment was found/used.

Focused portability/account lifecycle/runtime compatibility plus packaging regressions: **86 passed**, with 18 existing FastAPI deprecation warnings. The native probe and notice-copy tests are included. Existing account-lifecycle fixtures verify replacement-process correlation, stale callback rejection, logout/login cancellation, account/model/quota invalidation and Mobile credential redaction.

An additional broad legacy protocol run (`tools/test_chatgpt_account_client.py` plus current account suites) produced **63 passed / 2 failed**. Both failures were reproduced against the unchanged pre-correction client loaded from Git HEAD in a separate process: `test_initialize_once_and_isolated_process` assumes CLI arguments end with the old three flags, despite the already-existing appended feature configuration; `test_observable_native_command_uses_broker_and_continues_same_turn` expects the removed pending-permit/native permission-broker path. These tests were not rewritten and obsolete runtime behavior was not restored. This extra suite is not reported as green. Reconcile those legacy fixtures with the canonical tool architecture before treating them as a release gate. Current packaged canonical-tool execution still needs actual installer acceptance.

## Authentication, persistence and tools

The existing implementation asks the managed App Server for `account/login/start` (ChatGPT browser login), validates its official HTTPS authorization URL, waits on the matching login ID, and supports cancellation/device-code fallback. The native runtime owns OAuth/PKCE/loopback callback mechanics; OSIA does not scrape browser cookies or borrow the developer's authentication. This audit did not perform that browser flow or claim its callback worked on a fresh machine.

The child receives a secret-minimized environment, an OSIA-specific persistent CODEX_HOME under the existing per-user OSIA Codex directory, isolated workspace/temp/AppData, and managed configuration. Its auth cache is separate from the user's global Codex home. Default Codex credential caching can be file-based under that private profile; this audit does **not** claim OSIA moved it into Windows Credential Manager or proved encryption at rest. Windows user-profile ACLs, sign-in, token refresh, restart retention and logout on an actual packaged clean installation remain acceptance checks. No credential-store migration was attempted, so existing pairing/settings/auth paths are preserved.

Models come from App Server's model catalog; the selected model and effective effort remain canonical. Catalog presence is not proof of entitlement. OSIA's canonical agent owns tools, receipts, ActionTruth and persistence; the native subprocess is not permission to execute independent local effects or introduce a second product planner. Existing protocol regression tests do not prove a real account inference/tool round trip in the eventual frozen application.

## Official OpenAI findings and commercial release gate

Official documentation was fetched on 2026-10-20:

- [Codex App Server](https://developers.openai.com/codex/app-server/) documents embedding authentication and streamed turns in a product. OSIA uses local stdio, not an exposed unauthenticated remote listener. Runtime features must match the pinned package, not a moving latest manual.
- [Sign in with ChatGPT plan usage](https://developers.openai.com/siwc/token-sharing-open-source) describes the OSS/local flow, a per-user/workspace registered client, stable host identity, consent, PKCE and token validation. It directs **paid or remotely hosted applications** to the linked interest form. This is a specific commercial eligibility gate for OSIA, not permission inferred from Apache-2.0 licensing.
- [App-server with ChatGPT plan usage](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) describes the app's own authorized access token and Responses provider configuration. It warns that model/list can be a bundled catalog and that successful inference verifies access for that request.
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) defines route-specific streaming, context and tool restrictions. It is not evidence that an older managed Codex login automatically qualifies OSIA for a separately monetized distribution.
- [Codex authentication](https://developers.openai.com/codex/auth/) documents browser/device-code login, per-user cached credentials, renewal, logout and file/keyring choices.

**Gate before commercially offering ChatGPT-plan sign-in:** establish the OSIA distribution model's eligibility with OpenAI via the documented process; record any required approval/registration and the authorized integration/client route. No form or message was sent on the operator's behalf. No approval, universal subscription entitlement or unlimited usage is claimed. If OpenAI requires the modern app-owned SIWC registration flow, implement it under that authorization with dedicated OAuth/token lifecycle tests; do not relabel the managed Codex client as a registered OSIA SIWC client or reuse another application's credentials.

## Before rebuilding/publishing Windows

- Resolve the commercial-auth eligibility/registration gate and approved claims.
- Obtain separate operator authorization to build the final Windows installer. Then test the actual frozen bundle/installer in a genuinely clean Windows environment without Codex/developer PATH or prior OSIA state.
- Use a tester's own authorized account for browser callback, consent, model/effort discovery, restart retention, logout/reconnect and a canonical OSIA read-only tool/receipt round trip. Any quota-consuming inference needs explicit approval first.
- Exercise real legacy 1.1.7/1.1.8 → 0.9.0 in-place update, interactive repair, rollback and populated profile preservation. Source contracts do not prove binary acceptance.
- Complete store inventory/physical Mobile gates and remaining WOR-355/356 live provider checks separately. No new binaries, website/manifests or store submissions were produced here.
