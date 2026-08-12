# Microsoft Edge Add-ons manual Partner Center checklist

This is a future user-controlled Partner Center procedure. Do not create an account/item, accept terms, upload, click the final submission action, or publish without separate explicit authorization.

## 1. Account readiness

- Confirm the intended publisher has the appropriate Microsoft Edge developer account and organizational ownership/access.
- Review current registration, publisher identity, agreements, and contact information before accepting anything.
- Confirm the updated, approved OSIA privacy policy, review-ready OSIA Desktop build, and clean screenshots are available.

## 2. Create the draft and upload

- Rebuild and verify the same shared Chromium artifact: `dist/store/osia-personal-browser-1.0.0.zip` and its sibling contents manifest.
- In Partner Center, open the **Edge** workspace and use **Create new extension**.
- On **Upload package (.zip file)**, drag the exact ZIP or use **Browse your files**.
- Review validation results and stop on any mismatch; do not fork browser business logic merely to satisfy the store.
- Record the resulting Edge extension ID from **Extension overview** exactly. This becomes the future `OSIA_EDGE_EXTENSION_ID`; do not reuse or infer the Chrome/development ID.
- Keep the submission in draft.

## 3. Availability

- In **Availability**, review **Visibility** and **Markets**.
- Prefer **Hidden** for pre-launch validation when appropriate: current Microsoft documentation says Hidden removes the item from search/browsing while allowing distribution by listing URL.
- Choose markets deliberately. Do not accept the all-current-and-future-markets default without review.
- Save the draft only; Hidden visibility is not a substitute for the separate submission gate.

## 4. Properties

- In **Properties**, select the accurate current category.
- Use an owned/current OSIA website URL only if it describes the extension accurately.
- Use `contact@osia.space` or an approved support URL for **Support contact detail**.
- Do not mark mature content unless the final product/listing actually requires it.

## 5. Privacy

- In **Single Purpose**, paste the matching `single_purpose_statement` from `en.json` / `fr.json`.
- In **Permission justification**, provide the exact four permission justifications and their “not used for” boundaries.
- For **Are you using remote code?**, select **No, I am not using remote code** only after rechecking the final ZIP.
- In **Data usage**, use `privacy-disclosures.md`; do not declare no data handling merely because processing begins locally.
- In **Privacy policy**, enter the approved and updated public OSIA policy URL only after the policy gate is complete.

## 6. Store listings

- In **Store listings**, edit details for English and French.
- Use the matching `full_description`. Microsoft currently requires 250–10,000 characters for each listed language.
- The name and short description are package-derived and read-only in the listing; verify they match each localized manifest message.
- Upload `assets/edge-logo-300.png` as the 1:1 extension logo for each locale (300 × 300; above the documented 128 × 128 minimum).
- Upload `assets/promo-small-440x280.png` if the current flow requests/allows the small promotional tile; the detailed current flow lists it as optional, although Microsoft overview material calls it part of the visual submission set.
- The 1400 × 560 large promotional tile and YouTube URL are optional.
- Upload the real localized screenshots produced by `screenshot-capture-spec.md` even though current detailed Microsoft guidance treats screenshots as optional promotional media. They materially improve reviewer clarity.
- Add localized search terms only if accurate; current documentation allows up to seven terms and 21 words total, with 30 characters per term.

## 7. Certification notes and separate submit gate

- Prepare **Notes for certification** from `reviewer_notes` and `reviewer_test_instructions` in `en.json`.
- Supply the review-ready OSIA Desktop build whose production trust contains this exact Edge ID. No credentials are required; use `https://example.com`.
- Stop before the final **Publish** action. Current Partner Center documentation uses that button to open/complete **Submit your extension** and send the extension to certification.
- Only after separate explicit authorization, re-verify the ZIP SHA-256, privacy policy, listing assets, exact Edge trust ID, and reviewer build before clicking the final submission action.
- Keep **Hidden** for pre-launch validation unless the user separately authorizes a visibility change.

Official field references checked on 2026-08-12:

- https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/publish-extension
- https://learn.microsoft.com/en-us/legal/microsoft-edge/extensions/developer-policies
