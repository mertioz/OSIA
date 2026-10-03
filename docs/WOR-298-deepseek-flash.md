# WOR-298 — DeepSeek V4.1 Flash only, with verified image transport

Verified on 2026-09-11. Scope: the DeepSeek catalog, compatibility boundaries,
reasoning controls and shared image adapter registration. Existing dirty changes
were retained; the comparison baseline is the worktree at task start, not HEAD.

1. **Official product/display name:** DeepSeek-V4.1-Flash; OSIA display label
   **DeepSeek V4.1 Flash**. [Official announcement](https://www.deepseek.com/en/news/deepseek-v4-1-flash/).

2. **Official canonical API ID:** `deepseek-flash`.
   `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` are retired compatibility
   aliases. DeepSeek V4 Pro remains offered by DeepSeek according to the current
   pricing page; hiding it in OSIA is the requested catalog policy, not a claim
   that the provider has already retired it. [Current model details](https://api-docs.deepseek.com/quick_start/pricing/).

3. **Official image capability:** native text and image understanding in the
   same model; JPEG, PNG, GIF and WebP are supported. OSIA uses inline data URLs
   in OpenAI-compatible Chat Completions content arrays. [Official vision guide](https://api-docs.deepseek.com/guides/vision/).

4. **Previous selectable DeepSeek inventory:** `deepseek-v4-pro` / DeepSeek V4
   Pro (`vision=False`), and `deepseek-v4-flash` / DeepSeek V4 Flash
   (`vision=False`). Their reasoning options were Default, Off, High and Max.

5. **Final selectable DeepSeek inventory:** exactly one entry,
   **DeepSeek V4.1 Flash → `deepseek-flash`**, with text, image, and function/tool
   capability metadata. Old and unknown DeepSeek entries in remote caches are
   excluded from both registry builders and the shared PC/Mobile inventory.
   There is no separate vision model. DeepSeek's positions in simple/complex
   provider preference lists are unchanged.

6. **Reasoning modes:** Default, Non-thinking, Low, High, Max. Internal UI IDs
   are `auto`, `off`, `low`, `high`, `max`. Unsupported generic labels such as
   Medium, Ultra and XHigh are absent and rejected at request construction for
   DeepSeek, without silently replacing the saved setting.

7. **Exact outgoing JSON mapping:** SDK `extra_body` is flattened into the
   top-level HTTP JSON by the existing OpenAI SDK. Default emits neither field.
   The hosted API documents named presets. [Official thinking guide](https://api-docs.deepseek.com/guides/thinking_mode/),
   [Chat Completions reference](https://api-docs.deepseek.com/api/create-chat-completion/).

   | UI mode | `thinking` | `reasoning_effort` |
   |---|---|---|
   | Default | omitted | omitted; provider default is thinking/high |
   | Non-thinking | `{"type":"disabled"}` | omitted |
   | Low | `{"type":"enabled"}` | `"low"` |
   | High | `{"type":"enabled"}` | `"high"` |
   | Max | `{"type":"enabled"}` | `"max"` |

8. **Custom 1–100:** not exposed. The first-party model encoder supports exact
   integers 1–100 and maps Low→50, High→75, Max→100; the current hosted Chat
   Completions reference documents string presets, not arbitrary integers.
   Native numeric range/presets and their source remain in reasoning capability
   metadata with `api_exposed=False`. Existing UI/config controls are named
   options; no unrelated numeric-control redesign was made.
   [Official model encoding reference](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/encoding/README.md).

9. **Actual transport path:**
   - PC: `stage_attachment` → `prepare_desktop_turn` → canonical `ImageArtifact`
     and opaque `image_ref` content.
   - Mobile: committed upload → `build_agent_input` → the same canonical image
     artifacts and references. The mobile WebSocket protocol was not changed.
   - Both: `ensure_image_input_supported` → `OSIAAgent._wire_messages` →
     `build_context` → `adapt_messages_for_provider` → `_wire_image_part` →
     `OpenAI.chat.completions.create` → `api.deepseek.com/v1/chat/completions`.
   - DeepSeek now registers `openai_image_url` with all four accepted MIME
     formats. Its policy specifies 32 MiB/image, 48 MiB encoded envelope and 600
     images. Existing shared OSIA ingestion/resource limits can be stricter.
     Oversized or invalid input fails explicitly; there is no text downgrade.
   - The selected canonical model retains the documented 1M context window.

10. **Proof of delivery:** [sanitized live HTTP evidence](wor298-live-evidence.json)
    contains all five requests, their method-independent endpoint identity,
    request/response model IDs, HTTP status, image counts and pixel SHA-256,
    presence of both text and image, reasoning requested/effective, exact fields
    and compatibility transformations. Each image request has one real
    `image_url` part and both text and image survive; every response identifies
    `deepseek-flash`. No auth headers, keys, user content or chain-of-thought
    are recorded. Representative serialized content:

    ```json
    {
      "model": "deepseek-flash",
      "messages": [{
        "role": "user",
        "content": [
          {"type": "text", "text": "<redacted>"},
          {"type": "image_url", "image_url": {"url": "data:image/png;base64,<redacted>"}},
          {"type": "text", "text": "<redacted language instruction>"}
        ]
      }],
      "stream": true,
      "thinking": {"type": "enabled"},
      "reasoning_effort": "low"
    }
    ```

11. **Normalization and planning:** deterministic tests capture actual SDK HTTP
    bytes for PC and Mobile in all five reasoning modes. They assert exact
    pixel hashes, preserved prompt text, one image, unchanged logical content,
    canonical model/endpoint and correct reasoning fields. The existing
    non-streaming DeepSeek `run_planning` → `run` route also sends images with
    Low reasoning. That interface has no conversation ID parameter, so its test
    uses the existing process-owned staging scope. Account-only V2 production
    planning remains explicitly unavailable to other providers through its
    existing `selected_provider_v2_transport_unavailable` guard. It was audited,
    not enabled or redesigned here. Unsupported model/provider image routes
    raise `ERROR:` rather than quietly dropping pixels.

12. **Migration behavior:**
    - `load_settings` returns migrated data in memory; it does not write on read.
      Subsequent normal `save_settings` calls persist it through the existing
      atomic writer.
    - Explicit DeepSeek `deepseek-*` selections migrate to `deepseek-flash` for
      main/autonomous models and each `provider:model` hierarchy entry. Provider
      identity, chain order, duplicate positions, keys and endpoint fallbacks
      are preserved. Other providers are untouched, including Ollama models
      with DeepSeek names.
    - With no provider stored, only the five historical official API IDs in
      `LEGACY_API_IDS` are recognized. Local R1 names do not imply DeepSeek.
    - Existing canonical reasoning overrides win. Otherwise the selected,
      autonomous, then hierarchy legacy model's explicit override is copied to
      the canonical ID. Old keys remain historical data; invalid labels are
      not approximated.
    - `set_model`, Mobile/autonomous selection resolution and scheduled model
      deserialization also canonicalize old IDs. Stale Mobile selections do
      not fall back to a different provider merely because the old ID vanished.
    - Active user configuration was ChatGPT Account, with empty hierarchy;
      no live selection needed migration during this task.

13. **Files changed and purpose:**

    | File | WOR-298 change |
    |---|---|
    | `osia/deepseek.py` (new) | Pure compatibility helpers for model IDs and settings |
    | `osia/models.py` | One catalog entry, stale-cache filtering, same preference positions |
    | `osia/reasoning.py` | Exact five options, numeric metadata, explicit invalid-mode rejection |
    | `osia/config.py` | Migration at settings load/save boundaries |
    | `osia/agent.py` | Only `set_model` and `_reasoning_request`: ID migration and fail-closed reasoning errors |
    | `osia/image_inputs.py` | DeepSeek adapter and image policy registration only |
    | `osia/mobile_model_catalogue.py` | `_canonical_entry` filtering and `resolve_autonomous_selection` migration |
    | `osia/scheduled_tasks.py` | Fallback catalog entry and `_resolve_model_metadata` migration |
    | `osia/conversation_context.py` | Canonical DeepSeek 1M context entry |
    | `benchmarks/wor260/phase4a.py` | Existing capability fixture points to current DeepSeek model |
    | `tests/test_wor209_reasoning.py` | Updated DeepSeek expectations and capture fixture lifecycle state |
    | `tests/test_wor210_context.py` | Existing DeepSeek context checks use canonical ID |
    | `tests/test_wor260_phase4a_model_capabilities.py` | Current DeepSeek option expectations |
    | `tests/test_wor298_deepseek_flash.py` (new) | Catalog, migration, parity, HTTP transport and planning tests |
    | `tests/fixtures/wor298_non_deepseek.json` (new) | Frozen pre-change non-DeepSeek inventory and payload metadata |
    | `tools/wor298_deepseek_probe.py` (new) | Opt-in tiny real API smoke using `key_chain.with_key_rotation` |
    | `docs/wor298-live-evidence.json` (new) | Sanitized actual HTTP evidence |
    | `docs/WOR-298-deepseek-flash.md` (new) | This report |

14. **PC/Mobile parity:** both consume `build_model_inventory`; Mobile serializes
    those entries through `_mobile_model`. Tests compare canonical IDs, labels,
    image modalities, capability, selectable count and all reasoning options.
    No separate mobile model catalog was created. Live PC and Mobile staging
    paths both produced accepted image requests.

15. **Non-DeepSeek no-diff proof:** frozen before/after values compare exactly
    for every non-DeepSeek catalog entry, provider config, reasoning capability
    and its request kwargs, image adapter/formats, shared inventory, provider
    order and relative preference lists. Baseline fixture SHA-256:
    `28dd92d417905c00a969c41c8581cacc859fa6bc2be7bf90cec09f4c9217f1b3`.
    Source review additionally confirms shared message serialization was not
    changed; the only agent edits are guarded DeepSeek compatibility handling.

16. **Validation:**
    - 69 passed: WOR-298, reasoning, current model capabilities, adaptive
      reasoning integration and the context model-switch check.
    - 46 passed: streaming TTS, TTS shutdown, key rotation, browser integration,
      Firefox context reuse, Brave launch arguments, and DeepSeek native/Groq
      XML parser preservation.
    - 45 passed, 2 explicitly deselected: PC attachments, existing multimodal
      context, and conversation context. The deselected tests failed identically
      against the saved pre-WOR-298 source: the existing OpenAI attachment
      end-to-end test returns an unfinished-turn message, and a server
      persistence test rejects its old fixture.
    - An initial legacy Mobile suite produced six failures because its fixtures
      patch the already-removed `model_has_vision` module attribute. That failure
      was reproduced against the pre-change module. These unrelated failures
      were retained, not hidden by product changes.
    - Every modified Python file passed `py_compile`; imports passed, with no
      circular import failure. Existing function signatures were AST-compared
      and remained identical. Caller searches were performed before edits.
    - Adversarial review covered correctness, regressions and AGENTS.md. It
      caught and corrected stale Mobile selection fallback and outdated
      DeepSeek-only context/capability test fixtures.
    - Hash comparisons preserve every initial file outside the listed scope.
      Protected parser/anti-loop bodies, pipeline order, browser launch logic,
      WebSocket protocol, `POPUP_W`, config resolution, platform guards and
      key-chain implementation are unchanged. No response-token cap was added.
    - Browser and audio checks were deterministic tests; physical cross-OS
      browser/audio sessions were not launched. Linux/Mac were not run here.

17. **Real API smoke:** two small five-case runs succeeded; the second records
    response model/status metadata as well. The retained run used 1,468 total
    tokens. All image cases correctly identified colors that were not named in
    the prompts, demonstrating actual image understanding as well as transport.

    | Case | Entry path | Mode | Images | HTTP | Response model | Answer check |
    |---|---|---|---:|---:|---|---|
    | A: text only | shared agent | Default | 0 | 200 | `deepseek-flash` | passed |
    | B: image + tiny prompt | PC staging | Default | 1 | 200 | `deepseek-flash` | passed |
    | C: text + image | Mobile staging | Default | 1 | 200 | `deepseek-flash` | passed |
    | D: text + image | PC staging | Non-thinking | 1 | 200 | `deepseek-flash` | passed |
    | E: text + image | Mobile staging | Low | 1 | 200 | `deepseek-flash` | passed |

18. **WOR-296/WOR-297 semantics untouched:** all pre-existing semantic, evidence,
    goal-contract and execution modules were compared to the initial hashes.
    No V2, Steam, semantic-loop or completion behavior was changed.

19. **Worktree discipline:** no commit, push, reset, clean, stash, staging,
    publication or version bump. All pre-existing dirty edits remain. Behavioral
    effects are limited to the requested DeepSeek catalog/transport and its
    legacy selections; explicitly unsupported DeepSeek reasoning now reports an
    error. Existing baseline test failures and Account-only V2 limitations above
    remain outside this provider update.

WOR298_DEEPSEEK_V41_FLASH_ONLY_AND_MULTIMODAL_GREEN
