# Product versions and runtime identity

## Version owners

- [osia/version.py](../osia/version.py) owns Desktop PRODUCT_VERSION, RELEASE_SEQUENCE, numeric parsing and historical display aliases.
- [osia/release_notes.py](../osia/release_notes.py) owns the embedded Desktop release history.
- Platform build metadata (app_metadata.json on Windows) and the release manifest identify a binary's product/display version, source commit, platform/channel and artifact metadata.
- Android versionCode and iOS CFBundleVersion are native build identities. Mobile Settings reads its installed package rather than inventing a version from a display alias.

The operator's explicitly requested release version takes precedence. Technical product versions use numeric components without leading zeros; presentation aliases do not rename or rewrite original artifacts. Updater comparisons use numeric product components and the established legacy epochs.

Desktop releaseSequence and Mobile native build identities are independent monotonic update values. Prepared candidates, store drafts, failed uploads and public releases are distinct states; a failed build does not become a new public release. Current manifests and store state supply their actual values.

## Source, build and loaded process

A synchronized source checkout does not update an already built EXE or code already loaded into a process. A product-version string alone does not prove that two builds contain the same fixes.

[osia/runtime.py](../osia/runtime.py) resolves source/frozen resources and exposes source-tree identity. The server health response identifies its PID, checkout root, executable/entry point, source mode and loaded runtime identity. Compare those observations with the intended source and the binary's embedded metadata.

For source delivery, use the primary checkout synchronization workflow in [AGENTS.md](../AGENTS.md). For an installed application, use the commit/hash in its actual artifact metadata and the exact public release manifest. A later source-only fix requires a new built generation before that installed binary contains it.

## Artifacts and update channels

Published artifact URLs, byte sizes, hashes and installed product identities retain their original meaning. A repair or new release uses the authorized versioned publication path rather than replacing old bytes or silently changing an installed channel.

[osia/updater.py](../osia/updater.py) owns Windows channel URLs, metadata/manifest validation, release ordering and installer-plan checks. Source declarations and embedded notes are not substitutes for the public manifests.

Current Windows/Linux installer identity, signing, publication and acceptance procedures are maintained once in the [Drive installer rules](https://drive.google.com/file/d/11nRvwHVxn0JzaZbKqE8Mzy6pEyVVBGK4/view) and AGENTS.md. Platform implementation is described in [Windows distribution](windows-installer.md) and [the Linux packaging guide](../packaging/linux/README.md).
