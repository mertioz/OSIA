# Historical Windows 0.9.0 publication

This is the dated 20 October 2026 release record. Current build rules and
acceptance order are maintained in [AGENTS.md](../AGENTS.md), the
[installer reference](windows-installer.md) and the Drive release workflow.
The original report's instructions and next steps are not repeated here.

## Published artifact

| Field | Recorded value |
| --- | --- |
| Product | OSIA Desktop 0.9.0 — Public Beta, Windows x86_64 |
| Source commit | `69ffe4da7119ca41e5e731244f9824e3499be41e` |
| Sequence / updater schema | `2026102001` / `2` |
| Installer URL | https://download.osia.space/windows/0.9.0/OSIA-Setup.exe |
| Installer bytes | `1187568701` |
| Installer SHA-256 | `6a68c7c38ca74944a4ca8f73d3c31ebe839e954b72693a102bb2bcfdf1ae2d08` |
| Application SHA-256 | `a1030a9177e7135e5b17c93c77b306ef1db1c1d27276ca9bf52211da8aebab8b` |
| Authenticode | Application and installer NotSigned |
| Site commit | `32d3518f589181cbe90f38ba8dedc22a2c0a9b89` |
| Pages deployment | `ccb5cc54-f542-4233-afd7-ac3788e033b7`, successful 2026-10-20T17:27:46.329528Z |

The original publication added five immutable objects and observed 70 existing
objects unchanged. It retained the old Stable/Preview objects; its sidecar
rollout was paused at that date. This is a historical state, not an instruction
to pause today's Stable channel.

## Qualified evidence

The published candidate passed its frozen package smoke and a second empty-PATH,
fresh-profile run. All 9,527 bundle entries were rehashed; Defender found no
threat in the application/installer. The source corrections covered tracked
module collection, missing Windows timezone data and the canonical Claude
offline probe.

Recorded suites were 224 passes plus two Inno skips subsequently run explicitly,
218 extended regression passes and 19 scheduled-task/timezone passes. The
additional legacy run retained 13 failures reproduced on its baseline. Its
intermediate failed installer and failed harness observations were not reported
as acceptance of the published candidate.

At publication, real installed Windows migration/account/physical Mobile
acceptance remained OPERATOR_PENDING. The report also retained qualified
redistribution/legal and commercial-account uncertainties. The source and
operator acceptance states were distinct.

The [complete original publication record](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-349-windows-0.9.0-rc.md) preserves full
test commands, intermediate artifact hashes, native runtime/license inventory,
Access-policy readback, public-download evidence and repository preservation.
