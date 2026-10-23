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
