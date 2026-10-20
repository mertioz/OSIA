# Final pre-release feature delivery — 2026-10-20

Implementation is delivered in the primary repositories. Live inference and
physical acceptance limitations remain explicit; the Linear issues stay In
Progress for those outstanding validations. No paid inference was performed.

## Results

1. **WOR-354:** remove the inherited rectangular Material touch ink locally
   from the Mobile Thinking disclosure. Preserve six-orb V5 animation, minimum
   touch target, accessibility, keyboard focus, collapse/reopen, reduced motion
   and lifecycle. Pixel and interaction assertions pass in both brightness
   modes. See [Mobile report](https://github.com/mertioz/osia-mobile/blob/ff96862917efd40fa8009f59a8840ca6cbdb749e/docs/WOR-354-thinking-touch.md).
2. **WOR-355:** PC owns discovered cloud identities and capabilities. Add
   DeepSeek/Mistral discovery, Google authentication/pagination, Claude capability
   metadata, replacement/empty/error/stale behavior, credential invalidation and
   explicit unverified fallbacks. Mobile projects source/availability and exact
   provider/model/effort identity. See [full provider matrix and dated official
   sources](WOR-355-cloud-provider-audit.md).
3. **WOR-356:** Grok / xAI is separate from GroqCloud. Implement Grok 4.7 via
   official Responses, native functions, image input, public summarized Thinking,
   private encrypted context and per-model effort. Existing OSIAAgent still owns
   dispatch, factual receipts, exact-route cancellation and reinjection. See
   [Grok research, contracts and account-login conclusion](WOR-356-grok-api-audit.md).

New capability contracts include GPT-6.1 Sol, GPT-6 Astra/Sol/Luna, GPT-5.6
Sol/Terra/Luna, Claude Fable 5.1/Opus 5.5/Sonnet 5.5/Haiku 5.5, Gemini 4 Argon
and 3.8 Flash, Qwen 3.8 27B, Mistral Large 4 and Grok 4.7. This list describes
documented contracts, **not universal account access**. Valid older OpenAI models
and explicitly published snapshots remain discoverable without adding offline
picker entries. GPT-5.5 Pro uses its documented nonstreaming contract and emits
its completed reply to the UI after completion, with streaming:false metadata.
Retired/redirected or media-only services are excluded from Chat; account and
enterprise exceptions are described in the provider report. DeepSeek remains
exclusively deepseek-flash.

## Changed implementation and call sites

| Area | Files and affected consumers |
|---|---|
| Canonical catalogue | `osia/models.py`, `osia/openai_model_contracts.py`; discovery callers in existing GUI/main/server catalogue paths keep their signatures. `get_all_models` and its cached form now use the same replacement policy. |
| Capabilities | `osia/model_capabilities.py`, `osia/reasoning.py`, `osia/conversation_context.py`; PC/Mobile reasoning and image/context decisions share exact metadata. |
| Protocol | `osia/responses_api.py`, `osia/claude_messages.py`, `osia/agent.py`; initial/key-rotation/fallback client construction all apply the adapter. Native tools are dispatched by OSIA and receipts return to their original provider/model. Public summaries and refusals are preserved, private reasoning stays private, native parser formats remain intact. |
| PC configuration | `osia/gui/main_popup.py`, `osia/i18n.py`; provider cards, availability, periodic discovery and six-language Grok notice. |
| Mobile projection | `lib/services/websocket_service.dart`, `lib/screens/providers_screen.dart`, `lib/widgets/model_selector_sheet.dart`, `lib/l10n.dart`; no second model authority or credential store. |
| Thinking | `lib/widgets/execution_trace_block.dart`; widget tests plus isolated integration entry point. |

New provider/discovery/transport/widget tests accompany the changes. Two
historical test assertions were narrowed where they froze the entire old model
catalogue; their established DeepSeek and non-DeepSeek contracts remain covered.
No browser launch/profile logic, OAuth SDK internals or product runtime authority
was changed.

## Validation and proof levels

- **Python regression:** 271 passed, 18 existing FastAPI deprecation warnings.
  Covers provider/parser, turn durability, ActionTruth, cancellation, public
  progress, Claude/ChatGPT Account, DeepSeek and PC↔Mobile settings/parity. The
  suite ran with temporary configuration/private storage before importing the
  runtime, avoiding the historical ActionTruth store used by older tests.
- **Final focused Python pass:** 35 passed after the additional native refusal
  assertion. Includes SDK HTTP contracts, canonical real local read_file and
  correlated native result reinjection in streaming and nonstreaming modes,
  discovery/rotation, unknown IDs, availability, images, summaries, stream close,
  ciphertext isolation and literal tool-output markup remaining data.
- **Python syntax/import:** py_compile on every changed Python file and imports
  of every touched production module succeeded.
- **Mobile:** 512 passed, 1 skipped; `flutter analyze lib test` and the isolated
  Thinking integration entry point reported no issues. The skipped test is the
  opt-in live relay E2E diagnostic requiring OSIA_RELAY_LIVE_TOKEN.
- **Real free discovery:** DeepSeek returned deepseek-flash; Groq returned
  allam-2-7b, openai/gpt-oss-20b, qwen/qwen3.8-27b and openai/gpt-oss-120b.
  Official managed ChatGPT model/list returned the account's GPT-6 Astra/Sol/Luna,
  GPT-5.6 Sol/Terra/Luna and Daybreak Blue. GPT-6.1 Sol was absent from that
  account catalogue and was not artificially inserted.
- **Other providers:** OpenAI API, Claude API, Gemini, Mistral and xAI had no keys
  configured on this machine. Their new contracts are mocked HTTP validation,
  not successful live inference. No paid inference ran, including for the
  providers with configured credentials.
- **Android attempt:** private debug APK built successfully. The existing
  space.osia.app.test debug identity isolates the test fixture. Wireless ADB
  disconnected during installation, and the test body could not execute.
  No DEVICE_VERIFIED claim; production app/data were not uninstalled. No iOS
  physical test was performed.

## Commits and repository provenance

Every row below was pushed and immediately verified against primary HEAD,
origin/main and GitHub main before proceeding.

| WOR | Repository | SHA |
|---|---|---|
| 354 — touch correction | Mobile | `a79008635db33f0edb4da1e396b1a72108ecac3a` |
| 355 — discovery/Responses | Desktop | `cf604106fa49a68964a4c3162c23a6a6376b4afe` |
| 355 — provenance | Mobile | `d4d55d7da813dfc798aea7c372434e0c35bdfd2f` |
| 355 — direct selection | Desktop | `ab7e76a0fc6dd1ca49b3c9a800cb7f525346a1e5` |
| 356 — Grok API | Desktop | `5d73c2fa87db757d2bbacbc30b76defa5013adb2` |
| 356 — Grok UI | Mobile | `a35d9ce9e3bcc57c9f815a78d63a8f51e3740c25` |
| 354 — isolated device fixture/report | Mobile | `ff96862917efd40fa8009f59a8840ca6cbdb749e` |
| 355 — older OpenAI contracts/snapshots | Desktop | `948aed81dceba353e04fdedd30e41e7f32075a53` |

Desktop implementation tip is 948aed81; this report's subsequent commit changes
documentation only. Mobile primary tip is ff968629. Final report revision and
both primary/remote SHAs are also recorded in the final chat/Linear evidence.

Targeted staging and credential-pattern scans were performed for every commit.
Both primary repositories retain their pre-existing local state: Desktop's
`.claude/settings.local.json` deletion and untracked legacy benchmarks, evidence,
tools, tests and local tool state were excluded; Mobile's pre-existing
`README.md` edit was excluded. No broad staging, force-add/reset/clean/force-push
or temporary development worktree was used. Detailed remaining path inventories
were saved outside Git for the operator.

## Required operator actions and WOR-357 boundary

1. Reconnect Android and rerun the isolated Thinking test, then visually accept
   the corrected real app; validate iOS separately on suitable hardware.
2. Configure missing API keys on Desktop, including xAI model/endpoint ACLs and
   API credits. Authorize paid smoke calls before validating live text, image,
   tool result reinjection, streaming/nonstreaming and Stop for each new route.
3. Grok Account is unavailable in OSIA pending an official eligible method and
   authorization. The official FAQ confirms shared identity but separate API
   billing; no third-party subscription-entitlement grant was established.
   Request vendor guidance at support@x.ai/sales@x.ai if desired. No message was
   sent on the operator's behalf. Claude Account/WOR-343 remains independent.
4. After those acceptance gates, WOR-357 owns Desktop 0.9.0 / Mobile 0.9.5 visible
   versions, monotone technical build/update ordering, migration/install/update
   QA, signatures, release notes and platform release preparation. Historical
   versions/manifests, credentials, conversations and pairing must remain intact.

No final version transition, public Windows/macOS/Android/iOS publication, store
submission or Linux packaging occurred during these three feature tickets.
