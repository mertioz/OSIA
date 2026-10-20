# WOR-357 — final source delivery, 2026-10-20

Source corrections are implemented, tested, committed and pushed from the two PRIMARY repositories. This is **not** release/build authorization. No final Windows application/installer was rebuilt, no Mobile binary was created/installed, and no website, manifest, store or platform distribution was published. No paid/quota-consuming inference occurred.

## Requirement coverage

| Requested item | Delivered evidence / remaining limit |
|---|---|
| 1. Duplicated Thinking root cause | Mobile history restored a cached assistant trace onto a user row sharing turn identity; the renderer had no role guard. Cancellation also had a separate trailing-slot assignment path. |
| 2. Mobile correction and placement | Assistant-only restore/render, shared exact-identity attachment, one owner once a streaming row exists, one empty control for legacy/unrecorded replies; real ChatScreen count/vertical placement/reopen tests. |
| 3. Desktop verification/correction | Existing role guard/map passed multi-turn Qt composition. Added empty legacy disclosure and ensured an existing pending trace is placed immediately above its own answer. No invented history identity or prose. |
| 4–5. Inventory and mapping | [Observed source/artifact inventory, dates, hashes, aliases and gaps](WOR-357-version-inventory.md). Mandatory Desktop 1.0.0 → 0.8.0 and 1.1.8 → 0.8.952. No tags/GitHub Releases exist; source declarations are distinguished from shipments. Google Play/older artifact gaps remain explicit. |
| 6. Desktop source version | Python product authority, Qt, PE/Inno generator and future manifest identity derive from **0.9.0**. App-server client metadata follows the product version. |
| 7. Mobile source version | Flutter **0.9.5+19**, unchanged Android/iOS product IDs, signing configuration, Keychain and storage paths; Settings development history uses build-qualified aliases. |
| 8. Windows updater compatibility | Explicit sequence **2026102001**, strict schema 2 metadata/manifest, schema 1 history compatibility, sequence/channel/hash/URL/signature tests and compiled non-installing Inno fixture. Known legacy registry/PE identity can migrate through standalone installer. Actual populated-profile installer upgrade/rollback remains unverified; shipped legacy updaters cannot be retroactively changed. |
| 9. Monotonic Mobile numbers | Accepted Android 18, new source floor 19; Android overrides <=18 rejected. Authenticated TestFlight browser inventory confirms iOS maximum **16**; **0.9.5 build 19** prepared above both. CI re-queries all-version maximum before building. |
| 10–11. Clean Windows / Codex | [Portability audit](WOR-357-chatgpt-windows-portability.md): copied pinned standalone 0.144.1 package starts/restarts signed out with empty PATH and fresh temporary profile. Frozen lookup no longer falls back to developer tools. Complete clean-machine OSIA installer/OAuth/tool acceptance remains unverified. LICENSE/NOTICE and exact Windows checkout bytes now retained. |
| 12. Official OpenAI eligibility | [Official plan-usage documentation](https://developers.openai.com/siwc/token-sharing-open-source) directs paid/hosted apps to its interest process. OSIA commercial eligibility, registration and authorized client route are not established by OSS licensing or the native probe. No form/message was sent. |
| 13. Tests | Results and qualified failures below. No live inference or operator auth was used by tests. |
| 14. Git evidence | Validated correction commits listed below; every push verified PRIMARY HEAD == origin/main == actual GitHub main. |
| 15. Remaining release/build blockers | Operator review/authorization, genuine clean-host installer/OAuth/tool acceptance, migration/rollback, physical Mobile acceptance, commercial-auth eligibility, store/older inventory gaps and live-provider checks; details below. |

The [Mobile Thinking ownership report](https://github.com/mertioz/osia-mobile/blob/f8c5224206d4ca629ca9f043d498c560cac4f074/docs/WOR-357-thinking-ownership.md) describes exact call sites, streaming/completion/cancellation/reconnect, empty-history behavior and unchanged public-commentary privacy. WOR-354 touch feedback is retained. No new planner, execution runtime, permissions flow or duplicate canonical store was introduced.

## Validation

- Final combined Python version/updater/packaging/account/runtime/Qt suites: **222 passed**, 18 existing FastAPI deprecation warnings. Changed Python files compiled and touched runtime/build modules imported successfully.
- The unchanged cancellation suite: **8 passed** on focused rerun. One concurrent-load run missed its 0.5-second timing assertion at 0.532 seconds; neither runtime cancellation behavior nor the test threshold was changed.
- Final full Flutter run: **512 passed, 1 existing opt-in relay diagnostic skipped** (`--exclude-tags guide-golden`, matching existing CI policy). `flutter analyze`: no issues. Production capability policy validate-only passed; iOS inventory/version contracts passed.
- Real native package startup/read-account/shutdown/restart with empty PATH and fresh profile passed. No password, OAuth token or operator auth cache was used/copied; account status was signed out and token refresh disabled.
- Additional legacy protocol run: **63 passed, 2 pre-existing failures**, reproduced against the unchanged pre-correction client in an isolated process. CLI-tail ordering and removed pending-permit/native-broker expectations remain documented, not silently waived or reported green. Current focused lifecycle/packaging suites passed.
- Exact candidate diffs reviewed; targeted secret scans clean with existing repository allowlist. Only intended files staged. Upstream notice SHA-256 values survive Windows Git checkout filters. Final public Windows EXE was never built; packaging tests used temporary non-installing fixtures.

## Validated commits

| Repository | Commit | Correction |
|---|---|---|
| Desktop | `4ce16bc636f48d3529ebdef93016126304e2eb3a` | Real Qt Thinking composition regression |
| Mobile | `e16664da69257e8b8067b9e14155a484a2529d2a` | Assistant-owned history/cancellation traces, real-screen regression, reduced-motion teardown |
| Desktop | `623c3c886ab6b82d8577d9b52512722c7ea7a4fd` | 0.9.0, aliases, release sequence/schema, installer/update source contracts and inventory |
| Mobile | `9e90af5a99afb83fb052a32c53de2153853664f0` | 0.9.5+19, Settings history, Android floor, all-version iOS query and packaging gates |
| Desktop | `13d910e6de5b409d98599208f0a805c867f041c8` | Self-contained runtime discovery, real credential-free probe, notices and portability audit |
| Mobile | `6038dcfe8a2a104af0b4ea262229bcb48c0274b7` | Authenticated iOS maximum 16; prepare build 19 and record Apple agreement gate |
| Desktop | `3d2f18c0c67673d79c3622f93aa916645f8f699f` | Record authenticated iOS inventory in cross-platform audit |
| Desktop | `e0817155df35d2d0b4d3eaf81a2c5f62172cc2b7` | Preserve exact upstream notice bytes on Windows checkout |
| Desktop | `daba7c2aea51cd60096debc83da0872b56280dde` | One disclosure for empty/legacy replies; final Qt composition |
| Mobile | `f8c5224206d4ca629ca9f043d498c560cac4f074` | One disclosure for empty/legacy replies and streaming owner; final full Flutter validation |

Product source heads before this report-only commit: Desktop `daba7c2aea51cd60096debc83da0872b56280dde`, Mobile `f8c5224206d4ca629ca9f043d498c560cac4f074`. The report commit changes only this document. Installed apps were not replaced; the Pixel remains the accepted private **1.1.2 build 18**, source `ff96862917efd40fa8009f59a8840ca6cbdb749e`, not these new source heads.

## iOS observation and remaining gates

After the operator signed in manually, read-only browser navigation verified OSIA TestFlight 1.1.2 builds 16/15, older 1.0.1 build 3, and failed upload 2. The highest observed number is 16. [Exact upload dates/states and prepared build](https://github.com/mertioz/osia-mobile/blob/6038dcfe8a2a104af0b4ea262229bcb48c0274b7/docs/WOR-357-ios-build-inventory.md). Build **19** is prepared, not built/uploaded. The Apps page also reports an updated Apple Developer Program agreement awaiting account-holder review. It remains untouched. The browser is left open on TestFlight.

Before subsequent release work:

1. Obtain the separate operator instruction for the final Windows rebuild. Then validate the actual frozen installer in a clean Windows environment without Codex/developer PATH/prior OSIA state. Source/native dependency probes do not replace this acceptance.
2. Establish the documented OpenAI commercial eligibility/registration/authorized integration route before promising ChatGPT-plan access to paying users. Test sign-in with a tester's own eligible account, browser callback, restart, logout/reconnect, selected model/effort and canonical read-only tool receipt. Inference requires explicit quota/spending approval; plan usage is never unlimited by promise.
3. Validate actual Windows legacy in-place migration, repair and rollback with populated profiles. Preserve conversations, credentials, pairing, settings, projects and provider configuration. Keep old channel manifests/installers untouched; no naive SemVer downgrade path.
4. Obtain new physical Android/iOS acceptance for these Thinking composition changes. Inspect Google Play all-track maximum before public Android build; refresh App Store/TestFlight maximum immediately before iOS build. Operator reviews the Apple agreement. Apple acceptance of the new 0.9.5 train and pending store-version configuration is not proven by build-number ordering.
5. Retain the documented historical-artifact/store inventory gaps. No missing publication date, hash, signature or Git provenance is manufactured. Do not rewrite immutable artifacts or create releases for old source declarations.
6. WOR-355/356 live API/account scenarios still require missing OpenAI/Anthropic/Google/Mistral/xAI credentials and explicit charged-call approval; DeepSeek/Groq credential presence is not paid-inference approval. iOS and unperformed API cases remain unverified.

All pre-existing operator paths remain outside commits: Mobile README modifications; Desktop `.claude/settings.local.json` deletion and pre-existing untracked benchmark/evidence/experimental/local-tool paths. No reset, clean, stash, uninstall or force push occurred. No browser/profile-launch logic was changed. WOR-202 remains the permanent In Progress register; WOR-357/WOR-349 retain their unverified release acceptance gates.
