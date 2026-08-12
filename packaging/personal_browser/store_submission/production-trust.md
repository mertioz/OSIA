# Production extension trust and Brave status

## Exact production identity inputs

Do not invent IDs and do not insert placeholders. Production continues to fail closed while either ID is absent.

The canonical source status is `extension/personal-browser/metadata.json`:

- `development_extension_id` is the deterministic unpacked-development identity only;
- `chrome_store_id` and `edge_store_id` remain empty until the corresponding store creates a real item.

The production Windows packager consumes these strict build inputs in `packaging/windows/build_windows.py::_stage_personal_browser_metadata`:

- `OSIA_CHROME_EXTENSION_ID`
- `OSIA_EDGE_EXTENSION_ID`

Both must match exactly 32 lowercase characters in the Chromium extension-ID alphabet. The packager writes `build/windows/generated/personal_browser_metadata.json`, bundles it as `_internal/personal_browser/extension_metadata.json`, and the runtime derives the exact `chrome-extension://<id>/` origins through `osia/personal_browser/registration.py::allowed_origins`. The generated origins are placed in the Native Messaging manifest derived from `packaging/native_messaging/space.osia.browser.json.in`. If either ID is missing or invalid, production staging raises and the runtime returns no production origins.

## Future change and command after both stores assign IDs

Only after the IDs are known:

1. replace the two empty strings in `extension/personal-browser/metadata.json` with the exact assigned Chrome and Edge IDs and commit that narrow identity change;
2. set the matching build environment values and run the normal Windows production build:

```powershell
$env:OSIA_CHROME_EXTENSION_ID = '<exact Chrome item ID>'
$env:OSIA_EDGE_EXTENSION_ID = '<exact Edge item ID>'
.\venv\Scripts\python.exe .\packaging\windows\build_windows.py
```

The angle-bracket strings above are documentation only and must never be copied into source metadata or accepted by validation. Before distributing the build, verify the generated metadata and installed Native Messaging manifest contain exactly the two assigned origins and never the development ID.

## Brave

Do not create a Brave-specific package. Use the Chrome Web Store artifact and listing path for Brave. The remaining blocker is physical proof of Brave's Native Messaging host-discovery/registration behavior on supported OSes. No Brave registration path is claimed or created until that proof exists.
