# Chrome Web Store manual dashboard checklist

This is a future user-controlled dashboard procedure. Do not execute any step that creates an account/item, uploads the ZIP, accepts terms, pays a fee, submits for review, or publishes without separate explicit authorization.

## 1. Account readiness

- Confirm the intended publisher already has an appropriate Chrome Web Store developer account or deliberately decides to register one.
- Review publisher identity, contact email, ownership/access, current program policies, and any registration fee or agreement before accepting or paying anything.
- Confirm `https://osia.space/legal/privacy` has been updated, approved, and made current for Personal Browser; the present draft is not sufficient.
- Confirm the review-ready OSIA Desktop build dependency and clean screenshots are available.

## 2. Create the draft and upload

- Rebuild and verify `dist/store/osia-personal-browser-1.0.0.zip` against `dist/store/osia-personal-browser-1.0.0.contents.json`.
- In the **Chrome Developer Dashboard**, use **Add new item**.
- Use **Choose file**, select that exact ZIP, then **Upload**.
- Stop if the uploaded size or manifest version differs from the local contents manifest.
- Record the resulting **Item ID** / extension ID exactly as shown by the dashboard. This is the future `OSIA_CHROME_EXTENSION_ID`; do not assume the development ID.
- Keep the item as a draft. Uploading a draft is not authorization to submit it.

## 3. Package

- In **Package**, verify Manifest V3, extension version `1.0.0`, localized name/description, and exactly four permissions: `activeTab`, `scripting`, `nativeMessaging`, `storage`.
- Confirm there is no `host_permissions`, `<all_urls>`, remote hosted code, or development manifest `key` in the uploaded production package.

## 4. Store Listing

- Use `en.json` for English and `fr.json` for French. The ZIP includes matching `_locales/en` and `_locales/fr` entries so both localized listings are available.
- Paste each `full_description` into the matching locale's detailed description.
- Select the current primary category that most accurately represents a local productivity/browser companion; do not choose a category for marketing reach alone.
- Upload `extension/personal-browser/icons/icon128.png` as the 128 × 128 store icon.
- Upload `assets/promo-small-440x280.png` as the required small promo tile.
- `assets/promo-marquee-1400x560.png` is optional.
- Upload at least one real 1280 × 800 screenshot per active locale from `screenshot-capture-spec.md`; up to five are supported. Never use synthetic or private screenshots.
- Add only verified/current homepage and support URLs. Use `contact@osia.space` in support text and never provide a credential in listing copy.

## 5. Privacy

- Paste `single_purpose_statement` from the matching metadata source.
- For the four permission fields, paste the corresponding `used_for` plus `not_used_for` text.
- Declare no remote hosted code.
- Do not claim “no user data.” Use `privacy-disclosures.md`: disclose website content/resources, form/user-provided content when applicable, and current-tab browsing activity (origin/path/title) handled for the explicit user-facing feature.
- Certify Limited Use statements only after verifying the final policy and package remain consistent with the matrix.
- Enter the approved, updated OSIA privacy-policy URL in the designated privacy-policy field.

## 6. Distribution

- Review current country/region and user-visibility controls in **Distribution**; choose only the intended pre-launch audience.
- Do not change any existing item visibility as part of this preparation run.
- Do not select automatic public availability merely because review completes.

## 7. Test instructions

- In **Test instructions**, paste `reviewer_notes` and `reviewer_test_instructions` from `en.json`.
- Provide a review-ready OSIA Desktop build whose production Native Messaging allowlist contains this exact Chrome Item ID.
- State that no credentials are needed and that `https://example.com` is the deterministic test surface.
- Confirm the instructions prove one-turn personal selection, next-turn isolated behavior, revoke, and unauthorized sibling rejection.

## 8. Separate submit gate

- Stop before **Submit for Review**.
- Only after separate explicit authorization, re-verify ZIP SHA-256, screenshots, policy URL, trust ID, reviewer build, and every dashboard field.
- In the submission confirmation, choose deferred publishing by disabling automatic publication after successful review. Current Chrome documentation states that a staged approved item must then be manually published within 30 days or it returns to draft.
- Submission authorization is not publication authorization. Do not manually publish later without a separate deliberate decision.

Official field references checked on 2026-08-12:

- https://developer.chrome.com/docs/webstore/publish/
- https://developer.chrome.com/docs/webstore/cws-dashboard-listing/
- https://developer.chrome.com/docs/webstore/images
- https://developer.chrome.com/docs/webstore/program-policies/user-data-faq/
