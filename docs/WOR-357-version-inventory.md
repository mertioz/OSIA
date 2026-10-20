# WOR-357 — historical version inventory and migration

Audited 2026-10-20. These are display aliases, not rewritten releases. Original version/build identifiers, timestamps, channels, SHA-256 hashes, signatures and Git identities remain immutable. A source declaration is not proof of distribution.

## Inventory scope and limits

Both repositories have zero local/remote Git tags and zero GitHub Releases. Inventory uses all reachable source history, embedded Desktop release notes, historical site release-manifest commits (read-only GitHub API), the existing local packaging manifest, and the accepted Android package record. Live download channel requests returned HTTP errors; no channel was changed. App Store Connect / TestFlight and Google Play console inventory is unavailable in this Windows session. Store completeness is therefore an explicit release gate, not a verified claim. No unobserved builds 6–13 or hypothetical releases are invented.

## Desktop identities

| Original technical version | Display alias | First source declaration (original timestamp) | Git provenance |
|---|---|---|---|
| 1.0.0 | 0.8.0 | 2026-07-13T22:22:07+02:00 | `526dab4c4b44d7660a0c200b7561d1c1cc3f5569` |
| 1.0.2 | 0.8.2 | 2026-07-20T02:33:56+02:00 | `37ddbaa192dff52778b8a4b3dd6cb4ad3ea798de` |
| 1.0.3 | 0.8.3 | 2026-07-20T17:46:39+02:00 | `108a8afa7144a3d72f1f1ce64f896f5af3bdbd1d` |
| 1.0.4 | 0.8.4 | 2026-07-20T19:35:38+02:00 | `c3f147a48d454bd5a3337a31b3a294bc207507a9` |
| 1.0.5 | 0.8.5 | 2026-07-21T21:46:40+02:00 | `2793133ce3c2154e9aaed74b817b5e389dfa8b5e` |
| 1.0.6 | 0.8.6 | 2026-07-24T01:51:51+02:00 | `a9b696bbc99589a60136f0fc73cbaa6a9d23ba02` |
| 1.1.0 | 0.8.944 | 2026-08-02T19:22:00+02:00 | `e2091c67f16e5988abe45cae7832cdbc09d962c0` |
| 1.1.1 | 0.8.945 | 2026-08-03T12:31:49+02:00 | `015d41bc21f655bad8d7785cce56491c6fd27786` |
| 1.1.2 | 0.8.946 | 2026-08-03T21:15:31+02:00 | `08be77a6807a321eb317dc9b49fbe4239f84de6d` |
| 1.1.3 | 0.8.947 | 2026-08-04T20:36:49+02:00 | `0d9e066b8169af913d9127658fcf97f4236035db` |
| 1.1.4 | 0.8.948 | 2026-08-06T00:17:40+02:00 | `b19252ae99c7ca0323bf814f68f57bfcfd2b19d7` |
| 1.1.5 | 0.8.949 | 2026-08-06T07:23:24+02:00 | `30b0f845c783e6751898be7027975920b1a297d6` |
| 1.1.6 | 0.8.950 | 2026-08-07T08:28:46+02:00 | `7b6b3b000b8930cdb679b66a6abed071035b9b44` |
| 1.1.7 | 0.8.951 | 2026-10-07T23:12:23+02:00 | `f1217e8ba2c7494c47edb2cf0ce9881488d9ab6e` |
| 1.1.8 | 0.8.952 | 2026-10-18T17:00:31+02:00 | `36e820c1acb9090899ed3cef4891643579057577` |

`1.0.1 → 0.8.1` is a legacy updater minimum / internal validation override identity, not a canonical source release or a claim of public shipment. Its publication chronology is unestablished. It is reserved between 1.0.0 and 1.0.2 for compatibility presentation; no release entry/date is fabricated. Future commercial 1.0.0 is not a historical artifact. Multiple hashes under the same original version are distinct builds in one version family; the hash/channel/timestamp qualify them and must not be collapsed into a replacement artifact.

The source order above is monotone. Original embedded release dates remain on their original entries (including 1.1.8 internal candidate dated 2026-10-18). The new 0.9.0 source candidate has no invented publication date.

## Mobile identities

| Original version + build | Display alias | First source declaration / evidence | Git provenance |
|---|---|---|---|
| 1.0.0+1 | 0.8.0 | 2026-05-24T19:52:00+02:00 | `3a3987bc043f6e5cad5c304dbfa896cf13b3724e` |
| 1.0.1+2 | 0.8.1 | 2026-07-21T23:36:34+02:00 | `e4337bbf5bdc17b03b6d1fd84630fedc81ab4e4f` |
| 1.0.1+3 | 0.8.2 | 2026-07-24T23:41:53+02:00 | `e94ac43bdb516c7e7dfc2fb6a006d0c4a97b3866` |
| 1.0.2+3 | 0.8.3 | 2026-08-02T19:22:08+02:00 | `3fb7344f38416be01b9881ccad39c466646091ca` |
| 1.1.0+4 | 0.8.4 | 2026-08-03T12:31:59+02:00 | `028ab5735659a2ff0b00efd467459aacd35b8a23` |
| 1.1.1+5 | 0.8.5 | 2026-08-04T20:37:01+02:00 | `d492cc89cf7739802e22b07e021b2a86961f8ead` |
| 1.1.1+14 | 0.8.6 | 2026-10-04T00:35:47+02:00 | `e6ca277fc5802f3a5ac67312900a4a2cc655b39d` |
| 1.1.2+15 | 0.8.7 | 2026-10-07T18:26:07+02:00 | `244b031e1b2d7ac9dd0b022cddc25210e9d53d04` |
| 1.1.2+16 | 0.8.8 | 2026-10-07T21:59:14+02:00 | `8c9a974386afbae3c8eaeac4dfed4139a74a54a3` |
| 1.1.2+17 | 0.8.9 | Existing signed Pixel installation observed 2026-10-20; original build timestamp not established | Source SHA unestablished; unchanged old APK retained locally |
| 1.1.2+18 | 0.8.10 | Private in-place installation accepted 2026-10-20 | `ff96862917efd40fa8009f59a8840ca6cbdb749e` |

The historical reuse of Android build 3 across 1.0.1 and 1.0.2 is recorded as it existed. It is not repeated or repaired by rewriting artifacts. Builds 17/18 are private Android evidence, not iOS releases. The accepted build 18 APK hash is `3c6d66a3c557949f2b764a8ab08998194696e84fb421f027bfabd6e090bd1ccc`.

## Observed artifact records

These are original site-manifest records, not new publication dates. `available=false` is not a public downloadable artifact. Hash spelling/case is preserved here.

| Identity | Original release date in site manifest | First observed site commit timestamp | SHA-256 | Evidence commit |
|---|---|---|---|---|
| windows 1.1.0 (available=True) | 2026-08-02 | 2026-08-02T21:20:01Z | `BA410F31197766975034334DC41BCA23DA616F7DF78C686F3A7E3DB8D92451CD` | `c09dc1def495e6bcb9675b5bec7bc3dc54074601` |
| android 1.0.2 (available=False) | 2026-08-02 | 2026-08-02T21:20:01Z | `not recorded` | `c09dc1def495e6bcb9675b5bec7bc3dc54074601` |
| windows 1.0.6 (rollback) | not recorded | 2026-08-02T21:20:01Z | `CB68D0920C863C76D8A1629D3FDCCCE3D0F083CE9A82169873788F74F38018A1` | `c09dc1def495e6bcb9675b5bec7bc3dc54074601` |
| windows 1.1.1 (available=True) | 2026-08-03 | 2026-08-03T12:26:43Z | `519C64D75EB4D793F6BF1C50BBA64A3B7D3C8C2E81092DF230DA7724D6FD1E10` | `8812c71452f8a505aed1313828084dacf3a32b73` |
| android 1.1.0+4 (available=False) | 2026-08-03 | 2026-08-03T12:26:43Z | `not recorded` | `8812c71452f8a505aed1313828084dacf3a32b73` |
| windows 1.1.2 (available=True) | 2026-08-03 | 2026-08-03T20:28:40Z | `EFA7A1D949FE366043AE3105BC973ADB08BEDDF4DF9E2F5858BB45793B193C8A` | `4d85cb236bbd3514ead75faefed32a5c004cd91c` |
| windows 1.1.3 (available=True) | 2026-08-05 | 2026-08-05T03:07:00Z | `B2C449E30E68F3860B4DC3799908E8A6BA65146CEF9E64496FB03D6CADF7AE51` | `f408c09979c33707a0ebd8d4b010116912995dc2` |
| android 1.1.1+5 (available=False) | 2026-08-05 | 2026-08-05T03:07:00Z | `not recorded` | `f408c09979c33707a0ebd8d4b010116912995dc2` |
| windows 1.1.4 (available=True) | 2026-08-06 | 2026-08-06T03:00:23Z | `CC594867594BC73C0BC45443297DB875506F73B08670B07920D33F0AA783791E` | `8873343aa2abcd5ec9d83d47064ae2bf88e7fd9c` |
| windows 1.1.7 (available=True) | 2026-10-08 | 2026-10-08T07:55:52Z | `a8f98d823bd946e6cb7d9f4a896bdbee49a82bf3e1cce496fffb4fc177a4d780` | `c9773da23b0ce81027480d0526e5de3ce4682a10` |
| windows 1.1.6 (rollback) | not recorded | 2026-10-08T07:55:52Z | `aeafacd25162f9e944e225d9a35833230d9875f83cf8c27edf87eb65b8b5e995` | `c9773da23b0ce81027480d0526e5de3ce4682a10` |
| android 1.1.2+16 (available=False) | 2026-10-08 | 2026-10-10T15:28:45Z | `not recorded` | `7b5da92aaa03072c971606fa7d9a46ff9191676b` |

The existing local Windows 1.0.6 internal-preview manifest additionally records publishedAt `2026-07-31T12:39:30Z`, SHA-256 `9fef97bc7990cddc22e2bebaf629fc52d825566139dcc5fe85ec345820887a8b`, size 1185772401, source `6953685f38ec804b4de4ccb537113ea0b4cdfbdf`. It differs from the public rollback hash; both records are preserved. The existing Windows installer rollback documentation records public 1.1.5 at `https://download.osia.space/windows/1.1.5/OSIA-Setup.exe`, size 1186101696, SHA-256 `0098631f3d19abc089a273ad7ab75ee1d6350f0a9cb64ddeec10ebf5fe4f1fcf`. These are recorded metadata, not freshly rehashed downloads. Earlier 1.0.x artifact hashes remain unverified. Do not manufacture absent hashes.

## Source transition and migration contract

- Desktop runtime, Qt version, Windows PE product version, Inno product version and future manifest generator derive from `osia.version.PRODUCT_VERSION = 0.9.0`. New update schema 2 carries integer `releaseSequence = 2026102001`, shared by embedded metadata, channel manifest and installer registry marker. Future releases must increment it.
- Strict schema 1 parsing remains for historical metadata. New-schema candidates compare explicit sequences; known legacy metadata maps to the original technical sequence. New installed schema-2 clients refuse old schema-1 update manifests. Channel, URL, hash, signature, architecture and rollout checks remain in force.
- Legacy binaries cannot be retroactively taught schema 2 or lower SemVer. Initial migration uses the separately reviewed standalone 0.9.0 installer. Do not deploy schema 2 onto the old channel as if old clients supported it. No historical installers/manifests are modified and no bridge build is fabricated.
- Inno verifies the existing registry version against the existing PE version, same install directory/AppId and valid layout. Known 1.0.0–1.0.6 / 1.1.0–1.1.8 identities without a sequence may enter the new epoch. New installs persist sequence. Both interactive and updater launches reject backward sequence; same-sequence interactive repair remains possible. Future unknown legacy identities fail closed.
- Historical aliases are presentation only: Qt cards/notes/notifications show aliases with original identity in card tooltips; historical note data and URLs remain intact. Mobile shows build-qualified development history and original identity in Settings.
- No user-data path/schema/package identity, pairing store, account profile or signing key changes. No migration clears data. Real signed installer upgrade/repair/rollback with populated isolated profiles is still a binary acceptance gate.
- Mobile pubspec is 0.9.5+19. Android uses that versionName/code and rejects overrides <=18. This exceeds all observed source/site/private codes. Google Play all-track maximum must still be inspected before publication.
- iOS product version remains wired through FLUTTER_BUILD_NAME. 19 is the source floor, not a store-verified iOS assignment. Both manual Codemagic workflows query `app-store-connect get-latest-build-number --platform IOS --all-versions` before selecting max(source floor, store max+1). Empty/invalid/inaccessible responses fail. Local official release helper requires the queried maximum and a greater selected build. No workflow was triggered; no iOS build/submission occurred.

Codemagic CLI contract: https://github.com/codemagic-ci-cd/cli-tools/blob/master/docs/app-store-connect/get-latest-build-number.md . iOS physical update/Keychain/pairing and marketing-version acceptance by the store remain unverified. All historical public site surfaces are a later publication task; this mission changes only the two primary source repositories.
