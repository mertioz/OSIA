# Product versions and historical identities

Current reference, consolidated on 16 November 2026.

## Sources

- [osia/version.py](../osia/version.py) owns Desktop `PRODUCT_VERSION`,
  `RELEASE_SEQUENCE` and historical display aliases.
- [osia/release_notes.py](../osia/release_notes.py) records embedded Desktop
  release history. A source declaration is distinct from a published artifact.
- [The historical inventory](WOR-357-version-inventory.md) preserves dated
  source/artifact/store observations and original hashes.
- Current build, publication and operator-acceptance order is maintained in
  [AGENTS.md](../AGENTS.md) and the [Drive installer rules](https://drive.google.com/file/d/11nRvwHVxn0JzaZbKqE8Mzy6pEyVVBGK4/view).

## Numbering

The operator's version instruction normally increments the last numeric public
component for a newly published update; an explicitly requested version takes
precedence. Technical product versions use numeric SemVer components without
leading zeros. Display aliases are presentation, not rewritten native versions.
Comparison uses numeric components and the updater's explicit legacy epochs.

Desktop `releaseSequence`, Android `versionCode` and iOS `CFBundleVersion` are
independent monotonic build/update identities. Mobile Settings reads the native
installed package version. Its public product version is not inferred by
padding an old version or by renaming historical artifacts.

The historical inventory and the release workflow distinguish source floors,
queried store maxima, failed candidates, uploaded drafts and public releases.
Old artifact URLs, identities and hashes retain their original meaning. Failed
or unpublished builds do not manufacture a new public version increment.

## Removed dated candidate instructions

The original 23 October instruction moved Mobile from 0.9.5/build22 to the
explicitly requested 0.9.51; Android24 and iOS25 were the final candidates, after
iOS24 failed a packaging guard. Those observations do not prescribe today's next
version, build floor or store maximum. [The original report](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/version-policy.md)
preserves their exact provenance. Desktop 0.9.0 and next-Mobile 0.9.52 statements
have been removed from the active policy.
