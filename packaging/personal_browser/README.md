# OSIA Personal Browser store preparation

This directory contains the reproducible Chrome Web Store / Microsoft Edge Add-ons preparation sources for the delivered Manifest V3 extension. It does not publish, upload, register a developer account, accept agreements, or change any external store item.

## Build the shared Chromium ZIP

From the repository root:

```powershell
.\venv\Scripts\python.exe .\packaging\personal_browser\build_store_assets.py
.\venv\Scripts\python.exe .\packaging\personal_browser\build_store_package.py
```

The package builder writes `dist/store/osia-personal-browser-1.0.0.zip` and a sibling `.contents.json` containing the byte size, SHA-256, and per-file manifest. `dist/` is intentionally ignored; store ZIPs are build outputs, not source commits.

The ZIP is constructed from an explicit runtime allowlist, with `manifest.json` at its root. It excludes `metadata.json`, tests, caches, logs, source maps, temporary files, Git files, and all signing material. The source manifest's public `key` exists only to keep the deterministic development extension ID. The package builder removes that field from the production ZIP so Chrome and Edge can assign their real store item IDs. No private key exists in this repository.

The extension manifest version remains `1.0.0`. This is the first production store version and is independent of OSIA Desktop/Mobile application versions.

## Canonical submission sources

- English metadata: `store_submission/en.json`
- French metadata: `store_submission/fr.json`
- Privacy disclosure matrix: `store_submission/privacy-disclosures.md`
- Proposed privacy-policy update: `store_submission/privacy-policy-proposal.md`
- Reviewer screenshot capture gate: `store_submission/screenshot-capture-spec.md`
- Chrome manual checklist: `store_submission/chrome-manual-checklist.md`
- Edge manual checklist: `store_submission/edge-manual-checklist.md`
- Production trust / Brave status: `store_submission/production-trust.md`
- Generated repository-local imagery: `store_submission/assets/`

## Current stop gates

Do not upload or submit until all of the following are true:

1. the public OSIA privacy policy includes the proposed Personal Browser disclosure and is approved/current rather than a non-indexed draft;
2. the clean `example.com` screenshots in the capture specification exist and have been reviewed for private content;
3. a review-ready OSIA Desktop build can be supplied to store reviewers;
4. the user separately authorizes account/item creation and ZIP upload;
5. after each store creates its item, the exact 32-character Chrome and Edge IDs are wired into the production trust build and physically validated.

## Official preparation references checked on 2026-08-12

- Chrome: https://developer.chrome.com/docs/webstore/prepare
- Chrome listing fields/localization: https://developer.chrome.com/docs/webstore/cws-dashboard-listing/
- Chrome images: https://developer.chrome.com/docs/webstore/images
- Chrome submission/deferred publishing: https://developer.chrome.com/docs/webstore/publish/
- Chrome user-data and Limited Use policies: https://developer.chrome.com/docs/webstore/program-policies/user-data-faq/ and https://developer.chrome.com/docs/webstore/program-policies/limited-use/
- Edge submission flow: https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/publish-extension
- Edge policies: https://learn.microsoft.com/en-us/legal/microsoft-edge/extensions/developer-policies
