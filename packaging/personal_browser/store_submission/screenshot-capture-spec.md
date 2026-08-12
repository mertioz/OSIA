# Manual screenshot capture specification

Meaningful listing screenshots require a foreground browser and live OSIA Desktop state. This is the only asset capture gate intentionally left manual. Do not synthesize UI, fabricate extension states, or capture authenticated/private content.

## Safe setup

1. Use a clean review browser profile with only OSIA Personal Browser visible.
2. Use `https://example.com`; do not use Gmail, payment sites, private accounts, or real personal data.
3. Run the canonical OSIA Desktop build with Personal Browser ready.
4. Hide bookmarks, profile names/photos, notifications, other tabs, desktop files, API keys, and task history.
5. Capture PNG at exactly 1280 × 800 with square corners and no added padding. Do not upscale a smaller capture.

## Required captures

Capture both English and French variants where OSIA UI text is visible.

### 1. Exact-tab authorization

Filename: `personal-browser-authorized-example-1280x800-{en|fr}.png`

- `https://example.com` is the active tab.
- The OSIA extension icon is visible in the browser toolbar.
- The extension shows the genuine `ON` badge after the user clicks it in that exact tab.
- OSIA Settings is visible on the Personal Browser section/status view and shows the real ready/authorized state.
- No other website or OSIA conversation content is visible.

### 2. Explicit turn selection

Filename: `personal-browser-explicit-turn-1280x800-{en|fr}.png`

- OSIA visibly shows the real `/browser personal` turn-scoped selector/chip.
- The harmless result identifies `Example Domain` or its visible page text.
- The browser remains on `https://example.com` with the genuine authorized badge.
- Do not include hidden prompts, tool protocol, local paths, provider credentials, or unrelated history.

### 3. Revoked / no silent fallback

Filename: `personal-browser-revoked-1280x800-{en|fr}.png`

- The Example Domain authorization has been explicitly revoked.
- OSIA shows the real no-authorized-tab/readiness result for an explicit Personal Browser request.
- The capture must not imply that the isolated browser was used as a fallback.

## Review before use

- Confirm dimensions and PNG format.
- Inspect at 100% and at the store's 640 × 400 downscaled size.
- Confirm no private/authenticated content, identifiers, local filesystem paths, secrets, notifications, or other tabs are readable.
- Confirm the state shown can be reproduced by the reviewer instructions in `en.json` / `fr.json`.
- Upload the localized screenshots only to the matching locale. The text-free promo tiles are shared globally.
