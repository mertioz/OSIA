# Mobile Settings latency — source correction, 23 October 2026

Fresh-process profiling before the correction: config snapshot3961.5ms,
including3879.7ms importing osia.voice.stt/SciPy/audio solely to list installed
model directories. After extracting filesystem discovery:57.9ms, unchanged
snapshot contract. A live localhost authenticated trace on the original running
PC measured first config7179.3ms, warm15.9ms. These are separate experiments;
never compare the localhost first response with the pure function after timing.

Call sites updated: GUI model inventory, config snapshot, set_setting validation.
The stt public available_whisper_model_sizes import remains compatible. The
get_config handler moves filesystem work to asyncio.to_thread. No tool dispatch,
provider/parser, conversation or PC↔Mobile wire format changes.

Validation: py_compile and imports of all changed production modules passed.
116 focused Settings/parity/legacy-browser-config/turn-lifecycle/provider/
ActionTruth/packaged-provider/model-catalogue regressions passed, with18 existing
FastAPI lifespan warnings. Two inventory tests cover fresh-process absence of
audio/numerical imports and real filesystem cache/bundled discovery.

The Mobile implementation removes redundant startup queries, adds independent
local/remote loading and honest unavailable/retry/disconnected states. Its
release evidence and physical-device measurements are in the Mobile repository
at docs/WOR-352-mobile-0.9.51.md. A source runtime was refreshed through the
existing ensure_service_running readiness mechanism; this does not release
an updated Windows installer. Windows public0.9.0 remains unchanged, and
Desktop macOS development remains paused. See docs/version-policy.md.


## Physical reconnect uncovered a separate startup dependency

A real source-server restart stalled in application startup. Its45second
faulthandler trace identifies recover_orphaned_tool_attempts/release_continuation
performing filesystem recovery; the live store contains995attempt files and149
barrier files. The service supervisor killed/relaunched the still-unlistening
server after270seconds. No records or operator files were removed.

Recovery now runs as one tracked background maintenance task, started once per
server lifespan and cancelled/gathered at shutdown. Existing recovery ownership,
ActionTruth and continuation semantics are unchanged. Health, Settings and fresh
model calls do not wait for reconciliation of older attempts, as required by
WOR-298/305. There is no new agent, router, global timeout or turn limit.
A regression holds recovery pending and proves startup/root remain responsive,
duplicate startup does not create another maintenance task and shutdown cancels
it. Actual runtime readiness and phone reconnect must be rechecked after commit.

Startup/shutdown/voice/parity/ActionTruth regression set:87passed,18existing
FastAPI warnings. Compilation and server import passed.


## Provider-read queueing found by the physical after test

The first physical after run was still slow: warm config13768ms in-app,
cold app5865ms in-app. In both cases a model response preceded config.
The fresh Settings config request was serialized behind an earlier Chat model
catalogue request on the same socket. Model/account snapshots perform bounded
provider/runtime I/O even though their work is off the event loop; awaiting
that work inside the receive loop still blocks later config messages.

get_models and get_chatgpt_account now schedule independent, connection-owned
read tasks. Each distinct request receives its own observed PC snapshot;
there is no cache masquerading as fresh data or repeat-request veto. Pending
read tasks are cancelled/gathered before closing their exact connection, so
late reads cannot write through a replacement socket. Writes, selected-model
authority, Chat dispatch and exact-turn cancellation retain their existing paths.
No message type or payload shape changed; response ordering is deliberately
independent. Mobile already consumes these snapshots on separate streams.

Two real-ASGI socket regressions hold model/account reads pending, request
config behind them, prove config arrives first, then release the read and
verify its own response still arrives. Existing repeatability/turn tests retain
all assertions; their fake socket now stays open while awaiting requested
read replies instead of sending EOF and cancelling them before assertions.
The focused read/PC-Mobile parity/conversation/WebSocket lifecycle/provider/
ActionTruth/packaged-provider/catalogue set passes115tests,19existing dependency
warnings. Compilation and module import pass. Physical after recheck follows.


## Cold/reconnect replay cost

Further real measurements kept the cold connected path at12329ms in-app after
the queue fix, while an already warm Settings read reached56ms. The receive
loop was still finishing auth replay before consuming Settings messages. It
materialized every historical event and only then filtered to resumable streams.
The actual store has841streams/11283733bytes. A read-only local benchmark on
warm filesystem state found cursor lookup1050.2ms and all-history replay1879.0ms,
materializing9415frames that a fresh client would discard. Selecting the known
empty resume set first took0.0ms and read/replayed no unrelated streams. Cold
filesystem/concurrent recovery observations were slower; these warm function
numbers must not be substituted for physical first-frame latency.

replay_for_client now accepts an optional explicit stream_ids scope. Default
callers retain the full historical replay contract. The WebSocket auth caller
passes the same server/supplied cursor union it already used for filtering,
before materializing frames. ACK rules, sequence ordering, replay identity,
compaction and action truth are unchanged; history is preserved. Invalid scopes
are rejected safely. Model/account cold imports also run inside the worker,
so module initialization cannot block the event loop while config is ready.

182focused durable-transport/replay/cancellation, parity, conversation, provider,
ActionTruth and metadata regressions pass with19dependency warnings. The new
scope test proves selected replay equals the prior filtered result, cursor ACKs
remain effective, and empty/invalid scopes never enumerate unrelated history.
All changed Python files compile; module imports pass. Final physical tests
must use the resulting source generation; Windows public0.9.0 stays unchanged.


## Final controlled physical measurements

Pixel9a0.9.51/code24, sourcePCa4ad7e5b: connected in-app snapshot51/77/66ms,
median66ms; controlled cold-app snapshot49ms. ADB gesture-to-observation times
134.8/158.9/192.9ms include82.3–118.2ms clock-probe round trips, not a Flutter
first-frame benchmark. Earlier uncontrolled cold failures are retained as
inconclusive; the phone was later observed in another foreground application.
After the operator reserved OSIA in foreground, the controlled cold run passed.

Actual Desktop-unavailable check: stop_service returnedTrue; Mobile showed
Disconnected/Retry and no old remote categories. ensure_service_running restored
the canonical source runtime in19.5s; categories returned. The observed post-PC-
restart config read took5283ms, so reconnect is explicitly not claimed sub100ms.
The runtime health probe passed10.3s after the restored child launched. No
controlled packet-loss or new physical Chat/Thinking/Stop campaign is claimed.
The operator accepted Android24 and separately authorized its public APK.

The exact Android artifact is public at
https://download.osia.space/android/0.9.51/24/OSIA-Mobile.apk
SHA2565a9f3060250418553537ba29f1407315c2f27673d9d00ceff8decc5746eaeea6.
Windows0.9.0 installer and channels remain unchanged; these PC source fixes are
not retroactively included in that frozen artifact. Mobile final delivery,
store draft IDs and native/platform source distinctions are documented in
osia-mobile/docs/WOR-352-mobile-0.9.51.md. No store review was submitted.
