# Public Thinking — consolidated V5 reference

Consolidated on 16 November 2026 from WOR-340 V2–V5 and the later WOR-341 presentation audit. The current root [AGENTS.md](../AGENTS.md), current implementation and Work Drive decisions govern the product. This file describes the Thinking display; the floating capsule has a separate [lifecycle implementation](../osia/gui/island_lifecycle.py).

## Current display behavior

- A new active turn opens its disclosure before commentary arrives. Completed history starts collapsed. An empty active control shows neutral dots, without invented narration.
- A manual collapse remains in effect through tool rounds, revisions, reconnect projections and remounting of the same exact turn. A distinct turn starts expanded independently.
- Received public commentary appears immediately in the existing text widget/document. Logical expansion and positive height do not replace Qt's explicit visibility setting.
- Final-answer chunks and individual tool receipts do not complete the control. Canonical completion, failure or committed cancellation collapses it once. A duplicate terminal receipt preserves a later manual reopening. Empty completed rows disappear.
- The six-dot control remains keyboard accessible. Stop keeps its own cancellation path; pressing Stop is not itself proof that cancellation has completed.
- The content remains flat and readable in the current theme. Reduced motion uses static active dots and avoids the Mobile zero-duration AnimatedSize layout defect. Hidden or inactive animations stop.
- The periodic six-dot orbit preserves position, radius, opacity and velocity at the cycle boundary. The V4 correction replaced fractional-cycle waveforms, rather than restarting or destroying widgets to hide a snap.

Desktop source: osia/gui/main_popup.py and osia/gui/thinking_orbs.py. MainPopup creates active traces with auto_expand=not historical. Mobile source: lib/widgets/execution_trace_block.dart, lib/widgets/thinking_orbs.dart and the PC-authored trace projection in lib/screens/chat_screen.dart.

## Public content and retention

Thinking displays designated public summaries or actual public tool-round commentary from the selected model. Private reasoning, tool arguments and generic activity-to-prose templates are not display substitutes. An empty public feed is a legitimate result; no additional model is called to invent narration.

The existing public-commentary path preserves fragmented native tool calls separately from public content and parses supported textual calls before removing protocol markup. Typed public text is normalized without stringifying private thinking chunks. A no-call retrospective remains the final answer.

Revisions from the same public source extend its entry; distinct provider rounds append distinct entries. Partial snapshots merge by the existing exact conversation/request/turn/run and provider/model/reasoning identities. Missing entries or stale revisions do not erase received wording. Canonical storage and transport projections have separate responsibilities; a smaller Relay projection is not permission to delete the canonical commentary.

Desktop-origin traces use the existing ordered localhost forwarding path to the canonical server and Mobile. Mobile projects PC-authored events and retains disclosure state; it creates neither another commentary generator nor a canonical store.

Source references: osia/public_commentary.py, osia/execution_trace.py, the selected provider adapter and osia/server/server.py. Provider support is determined by the actual implemented contract and model, not by the historical provider counts below.

## Narrow display formatting

The later WOR-341 audit demonstrated that complete whole-line bold headings were already present in provider-authored public summaries. Desktop's _thinking_display_text and Mobile's formatThinkingPublicText remove only the outer delimiters of supported simple whole-line headings. They retain the words and language, raw canonical event, provenance, transport and final answer.

Inline Markdown, links, code spans/fences, escapes, nested markup and incomplete streamed wrappers retain their existing handling. The display does not manufacture first-person claims or choose the model's response language. See the [prompt verification audit](WOR-341-prompt-verification-audit.md).

## Historical causes and verification

These are dated completed experiments, not current full-suite or live-provider guarantees. Test selections overlap and must not be summed.

| Stage | Unique demonstrated correction | Recorded verification |
| --- | --- | --- |
| V2, 11 October 2026 | Removed synthesized Activity narration; repaired per-round commentary retention, partial snapshot merging, flat presentation and Desktop-to-Mobile forwarding | 293 Python and 65 Flutter checks; Flutter analysis clean |
| V3, 11 October 2026 | Compatible-provider streaming despite suppressed final callbacks; native tool-delta preservation; typed text normalization; immediate Account summary seeds and explicit commentary phase | 353 Python and 74 Flutter checks; later seed-whitespace refinement: 33 directly affected Python checks |
| V4, October 2026 | Explicit Qt detail visibility, accessible disclosure, reduced-motion Mobile layout and continuous periodic animation | 98 Python and 79 Flutter checks; analysis clean |
| V5, October 2026 | Replaced V4's initial collapsed default with automatic expansion for a new active turn, retaining manual choices/history behavior | 84 Python and 48 Flutter checks; focused analysis clean |
| Later WOR-341 presentation audit, 11 October 2026 | Narrow whole-line heading display fallback, without changing stored/provider wording | 179 Python checks and 33 Flutter checks; one historical prompt aggregate explicitly excluded |

The WOR-341 reports record operator approval of V5. Old V2–V5 statements that the entire implementation remained uncommitted or awaited its first V5 acceptance are not current delivery status. The source and tests remain distinct from the generation loaded by a running application; current delivery and operator results belong in the dated Drive journal.

Focused regression entry points include tests/test_wor340_thinking.py, tests/test_wor340_provider_timelines.py, tests/test_wor340_thinking_v4_ui.py, tests/test_wor311_public_progress.py and tests/test_wor269_cancellation_thinking.py, with the corresponding Mobile suites. Rendering fixtures and metadata do not establish that every real provider emits commentary or prove actual screen-reader, audio or connected-device acceptance.

## Immutable historical evidence

The superseded V4 collapsed-default instructions and repeated dirty-tree inventories are removed from this reference. Their complete original reports remain available at the pre-consolidation Git revision:

- [V2 investigation and retained-wording evidence](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-340-thinking.md).
- [V3 provider contract matrix and streaming timelines](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-340-thinking-v3.md).
- [V4 Qt reproduction, accessibility and periodic geometry](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-340-thinking-v4.md).
- [Original V5 correction and verification inventory](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-340-thinking-v5.md).

The V4 visibility reproduction showed received text with expanded=True, explicitly_hidden=True and maximum_height=180 until reopening. Its correction controls visibility directly. The V4 orbit used an 1800 ms cycle with evenly spaced six-dot phase offsets; endpoint and velocity checks proved continuity. These causes remain useful even though the old versions no longer act as competing instructions.
