# Store asset inventory

Run `packaging/personal_browser/build_store_assets.py` to reproduce these assets from `assets/osia_icon.png`.

| Asset | Use | Requirement status |
| --- | --- | --- |
| `extension/personal-browser/icons/icon128.png` | Chrome Web Store icon and packaged extension icon | Required, 128 × 128 PNG |
| `edge-logo-300.png` | Edge Add-ons localized extension logo | Required by current Partner Center flow, 300 × 300 PNG (above 128 × 128 minimum) |
| `promo-small-440x280.png` | Chrome required small promo tile; reusable for Edge | Required for Chrome; accepted Edge promotional size |
| `promo-marquee-1400x560.png` | Optional Chrome/Edge large promotional tile | Optional |
| `assets-manifest.json` | Source digest, exact dimensions, and output SHA-256 values | Reproducibility evidence |

Actual product screenshots are deliberately absent until the foreground capture gate in `../screenshot-capture-spec.md` is completed. Chrome requires at least one screenshot. Edge documentation describes screenshots as optional promotional media, but the WOR-165 submission package requires the same truthful reviewer imagery for both stores.
