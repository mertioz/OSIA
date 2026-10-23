# Permanent OSIA version policy — 23 October 2026

Authority: operator instruction of 23 October 2026 and Linear WOR-202/WOR-352.
Each newly published Mobile or Desktop update increments the final public
numeric version component by one until the operator explicitly changes this.
The requested next Mobile candidate is **0.9.51**, following historical
**0.9.5/build22**; this explicit transition is not inferred by padding 0.9.5.
Subsequent Mobile publications are 0.9.52, 0.9.53, and so on.

Desktop Windows remains **0.9.0**; no Desktop binary publication is authorized
in this mission. Desktop macOS development remains paused. Examples such as
0.9.00 → 0.9.01 → 0.9.02 are display conventions: future strict SemVer/native
metadata must use 0.9.0 → 0.9.1 → 0.9.2, with separate zero-padded display text
only if requested. Never feed zero-padded components into strict version parsers.
The current Desktop display is not retroactively renamed.

Native versions and public source metadata must agree with the built product.
Mobile Settings reads PackageInfo from the installed native package, never a
history alias or a hardcoded replacement. iOS has no Beta footer (Apple 2.2);
Android keeps “OSIA <installed version> — Beta”.

Build identifiers are independent, unique, positive and monotonic. Query live
Apple all-version builds and the complete Play bundle inventory before assigning
a new candidate. The 23 October inspection found Apple max23 and Play max22;
0.9.51 therefore starts at build24. Codemagic rechecks Apple's all-version
maximum at build time and advances further if necessary. Reconcile the actual
IPA build rather than assuming it equals the source floor.

Compare numeric components, never version strings. Existing Desktop
parse_product_version rejects leading zeros and updater.compare_versions uses
numeric tuples plus its explicit legacy epoch rules; releaseSequence remains
the authoritative update epoch. Mobile native upgrades use versionCode and
CFBundleVersion. Historical 1.x identifiers and display aliases must not be
rewritten to manufacture an update order. No installed channel or migration
policy is changed here.

Never reuse a native build, overwrite an immutable release object, change an
old URL, or describe an uploaded draft as store-public. Preserve Mobile
0.9.5/build22 and Windows0.9.0 exactly. Store submission, physical acceptance,
APK publication, and financial-site publication remain separate gates.
