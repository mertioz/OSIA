# Mobile Settings latency — WOR-352

Source investigation and controlled measurements: 23 October 2026. Documentation consolidated on 16 November 2026. This report retains the distinct causes and verification evidence; current runtime/protocol rules are defined in [AGENTS.md](../AGENTS.md) and current implementation.

## Causes and corrections

### Installed-model discovery imported the audio stack

A fresh-process config snapshot took 3961.5 ms, including 3879.7 ms spent importing voice/STT, SciPy and audio solely to list installed model directories. Filesystem-only discovery reduced the same pure snapshot function to 57.9 ms.

GUI inventory, config snapshot and set_setting validation consume the extracted discovery function. The existing STT public import stays compatible. Filesystem work runs outside the event loop.

A live localhost authenticated trace separately measured first config at 7179.3 ms and warm config at 15.9 ms. These are separate experiments; the live first response must not be compared directly with the pure-function after result.

### Startup recovery delayed server readiness

A real restart stalled before listening. A 45-second faulthandler trace identified recover_orphaned_tool_attempts/release_continuation filesystem recovery; that store then contained 995 attempts and 149 barriers. The supervisor relaunched the unlistening server after 270 seconds.

Recovery was moved to one tracked background maintenance task per lifespan, cancelled/gathered during shutdown. Existing recovery ownership, ActionTruth and continuation semantics stayed intact. A fixture holds recovery pending and proves startup remains responsive, duplicate startup does not duplicate maintenance and shutdown settles its task.

### Provider reads queued ahead of Settings

An initial physical after run still measured warm config at 13768 ms and cold in-app config at 5865 ms. Model/account reads were off the event loop but awaited serially by the same receive loop, so they delayed later config messages.

get_models and get_chatgpt_account use independent connection-owned read tasks. Each distinct request keeps its own observed response; closing that connection cancels/gathers its reads so a late result cannot write through a replacement socket. Writes and canonical Chat dispatch keep their existing paths. No message type or payload shape changed.

Two real-ASGI socket regressions hold provider reads pending, request config behind them, observe config first and then observe the released read's own response.

### Cold authentication materialized unrelated history

A further cold connected measurement was 12329 ms while a warm Settings read reached 56 ms. Authentication materialized all old events before filtering them to the resumable stream set.

The inspected store contained 841 streams, 11,283,733 bytes and 9415 frames. On a warm filesystem, cursor lookup measured 1050.2 ms and all-history replay 1879.0 ms; selecting the known empty resume set first read/replayed no unrelated streams and measured 0.0 ms.

replay_for_client accepts an optional explicit stream_ids scope. Existing default callers retain their full replay contract; authentication supplies its established cursor union before materialization. ACKs, sequence/replay identity, compaction and action truth retain their existing roles. Invalid scopes are rejected. Cold model/account imports occur inside their read worker.

These function measurements do not substitute for a cold-device or first-frame benchmark.

## Verification history

Selections overlap and are not a unique aggregate:

| Stage | Recorded checks |
| --- | --- |
| Filesystem model discovery | 116 focused Settings/parity/provider/lifecycle/catalogue checks |
| Background startup recovery | 87 startup/shutdown/voice/parity/ActionTruth checks |
| Connection-owned provider reads | 115 read/parity/conversation/WebSocket/provider/metadata checks |
| Scoped authentication replay | 182 transport/replay/cancellation/parity/provider/metadata checks |

Changed Python files compiled and imported. Existing dependency warnings were recorded; no tool dispatch, provider parser or conversation-store redesign was performed. Mobile removed redundant startup queries and separated local/remote loading with observed unavailable/retry/disconnected states.

## Controlled physical results

Pixel 9a Mobile 0.9.51/code 24 with source PC a4ad7e5b:

- Connected in-app snapshots: 51 / 77 / 66 ms; median 66 ms.
- Controlled cold-app snapshot: 49 ms.
- Gesture-to-observation measurements: 134.8 / 158.9 / 192.9 ms, including 82.3–118.2 ms clock-probe round trips; these are not Flutter first-frame measurements.
- Desktop-unavailable state: Mobile showed Disconnected/Retry without old remote categories.
- Source-service restoration: 19.5 s; health passed 10.3 s after the restored child launched.
- Post-PC-restart config read: 5283 ms. Reconnect was not claimed below 100 ms.

Earlier uncontrolled cold failures remained inconclusive because the phone was later observed in another foreground app. The controlled foreground run passed. No controlled packet-loss or new physical Chat/Thinking/Stop campaign was claimed.

## Dated delivery evidence

The operator accepted Android code 24 and authorized its APK publication:

- [OSIA Mobile 0.9.51/code 24](https://download.osia.space/android/0.9.51/24/OSIA-Mobile.apk).
- SHA-256: 5a9f3060250418553537ba29f1407315c2f27673d9d00ceff8decc5746eaeea6.
- Mobile evidence: osia-mobile/docs/WOR-352-mobile-0.9.51.md.

At that October delivery, the public Windows installer remained 0.9.0 and those source corrections were not retroactively in it. This is a dated provenance fact, not a current version pin or instruction to keep a future release at 0.9.0. Subsequent installer generations and operator results belong in the Work Drive.

[Complete original report and measurement sequence](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-352-settings-performance.md).
