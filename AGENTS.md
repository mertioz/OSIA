# OSIA PC — Rules for Codex

> OSIA PC and OSIA Mobile share one cross-platform product. PC owns the canonical
> Chat runtime and state; Mobile mirrors that runtime and its protocol.

## Authoritative architecture

Follow the completed product decisions in Linear WOR-298, WOR-301, WOR-303,
WOR-305, WOR-306, WOR-308, WOR-309, WOR-311, and WOR-312. These decisions supersede older
tickets, tests, comments, and documentation when they conflict.

- There is one canonical Chat runtime. The selected provider/model remains the
  semantic authority through the turn; do not hand work to an Autonomous
  runtime, second planner, static router, or hidden progress agent.
- Every technically available tool remains visible and callable by the selected
  model. Do not reject or reroute a call because of task length, semantic domain,
  mode, strategy, or an application/tool relevance classifier.
- ToolAttempt and ActionTruth record dispatch and observed outcomes as facts.
  OUTCOME_UNKNOWN is a factual state: it is not success, does not itself end the
  user objective, and does not block a fresh model-requested call or require
  reconciliation before one.
- Do not add product-level anti-replay or duplicate-action vetoes based on
  action fingerprints, prior effects, retry counts, or semantic similarity.
  Transport deduplication for the same exact request/attempt identity,
  concurrent ownership, and corruption protection may remain technical
  integrity mechanisms; they must not block a distinct new model request.
- Preserve target/surface freshness before dispatch, correlated cancellation of
  the exact turn, and rejection of stale callbacks. These are factual integrity
  guarantees, not planner or permission-broker authority.
- BLOCK_SYSTEM_DESTRUCTION remains a terminal guard for destructive operations
  against proven critical operating-system or OSIA-runtime paths.
- There is no Autonomous product runtime, permission broker, ordinary
  confirmation mode, MCP write-confirmation flow, or scheduled-task
  authorization flow. Scheduled Tasks are time triggers for the same canonical
  Chat engine.
- PC is canonical for model choice, tool execution, conversation storage,
  ToolAttempt, ActionTruth, and turn state. Mobile is a client projection and
  control surface; it must not create a second canonical store or runtime.
- DeepSeek's sole canonical selectable model ID is deepseek-flash. Retired
  DeepSeek IDs must not reappear in the selectable catalog or API payloads.

## Runtime and protocol rules

- The provider/tool parser is changeable when required by the active provider
  lifecycle. Preserve all current native and textual tool-call formats,
  including DeepSeek DSML, Groq XML/text, and supported native structured calls.
  Parse tool calls before stripping reasoning or tool markup. Remove internal
  markup from public output only after parsing/execution; never expose raw
  protocol tags or hidden reasoning.
- The current anti-loop component is an observational per-call log only. Do not
  restore thresholds, progress/stall detectors, arbitrary turn limits, or
  refusal of repeated safe calls.
- The PC WebSocket protocol is a shared PC↔Mobile contract, not a frozen list of
  legacy event names. When changing it, update PC and Mobile together and test
  correlation, cancellation, reconnect, conversation projection, and event
  ordering. Do not restore removed Autonomous, permission, MCP-confirmation, or
  scheduled-authorization messages.
- ToolAttempt/ActionTruth must not invent completion. Text such as ERROR in
  stdout/stderr is data; use typed dispatch and outcome facts. OUTCOME_UNKNOWN
  remains unknown until evidence changes it.
- Do not add global response-token, turn-duration, tool-round, retry, or
  iteration ceilings that terminate feasible user work. Provider-native limits
  and local bounds for one operation are allowed when they do not terminate the
  overall objective.

## Stable implementation contracts

- POPUP_W remains 440 for the compact menu; Settings may expand to
  POPUP_W_WIDE = 640 and return to 440 through the existing width transition.
- Resolve CONFIG_DIR only through osia.config._resolve_config_dir and import
  CONFIG_DIR into server.py from osia.config. Do not hardcode user config paths.
- MAX_RESPONSE_TOKENS is not a runtime setting or provider-call parameter.
  Tests may mention the name only to assert its absence.
- osia/utils/key_chain.py is the source of truth for API-key rotation. New API
  call sites use with_key_rotation() or iter_keys(); rotate only on technical
  failures, never content errors; never log full key values.
- Preserve the Linux-only QT_QPA_PLATFORM=xcb guard, Linux-only X11 window
  hints, Windows registry autostart, cross-platform paths, graceful optional
  audio imports, and platform-aware service control.
- Do not change the CDP/Firefox BiDi launch, profile-isolation, or single-window
  logic in osia/tools/browser.py unless the user explicitly asks.
- Keep mcp_client.oauth.force_oauth_login compatible with the pinned MCP SDK
  internals unless the SDK surface is rechecked and the change is requested.
- Never commit API keys, tokens, passwords, private keys, OAuth or relay
  secrets, local credential stores, or secret-bearing environment files.

## Cross-platform rules (Linux / Windows / macOS)

- Resolve project paths from pathlib.Path(__file__).resolve(); use
  tempfile.gettempdir() for scratch files. Never add hardcoded Linux paths.
- Set QT_QPA_PLATFORM=xcb only under sys.platform.startswith("linux").
- Add X11BypassWindowManagerHint only on non-Windows X11 paths.
- Linux autostart uses XDG; Windows autostart uses the HKCU Run registry value.
  Keep autostart_enabled() aligned with actual platform behavior.
- Any sounddevice import must remain optional so missing PortAudio never crashes
  startup; recording/playback paths degrade gracefully.
- Use platform.uname(), not os.uname(); guard os.getloadavg() where unavailable.
- Keep systemd_manager public function names/signatures stable. Use systemd on
  Linux, Windows tunnel scripts and detached service processes on Windows.
- Keep install.bat, start.bat, and requirements.txt as the Windows setup path.

## Change and verification workflow

1. Keep scope to the requested work. Before changing a function or protocol,
   find its call sites and update all affected callers in the same change.
2. For Python edits, run py_compile on each changed Python file and import the
   touched module(s) to catch syntax and circular-import failures.
3. Run focused provider/parser, turn-lifecycle, ActionTruth, server/WebSocket,
   PC↔Mobile parity, and DeepSeek/non-DeepSeek regressions for affected paths.
   Run adjacent browser, TTS, and platform checks when those paths are touched.
4. Update an old test only when its assertion demonstrably conflicts with the
   completed architecture above. Do not reintroduce deleted runtime behavior to
   satisfy stale tests.
5. Before staging, scan every candidate file for credentials. Respect
   .gitignore; exclude generated evidence, benchmark output, caches, local tool
   state, build artifacts, and machine-specific files. Never force-add ignored
   files.
6. Summarize changed files, call sites, checks run, test results, and any
   remaining uncommitted paths with their reason.
