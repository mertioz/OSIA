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
