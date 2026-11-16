# Public Thinking and capsule activity

This guide describes the current GUI implementation. Product decisions are maintained in [AGENTS.md](../AGENTS.md) and the Work Drive. A source change becomes active in an installed application only when its built generation is installed and loaded; see [versions and runtime identity](version-policy.md).

## Chat Thinking panel

The panel displays designated public summaries and actual public tool-round commentary from the selected model. It does not display private reasoning or generate narration from tool receipts. A provider can legitimately return no public commentary.

A new active turn starts expanded before text arrives. Historical turns start collapsed. Manual collapse/reopening is retained for the same exact turn through revisions and repeated projections. A distinct turn has its own disclosure state.

Received text updates the existing document and becomes visible immediately when expanded. Final-answer chunks and individual tool results do not settle the panel. Canonical completion, failure or committed cancellation collapses it once; duplicate terminal updates preserve a later manual reopening. Empty completed traces disappear.

The six-dot control supports keyboard interaction and accessibility labels. Reduced motion uses static dots; hidden or inactive animation stops. The public content area uses the current flat theme styling rather than a generated activity card.

### Content and projection

The current path is:

    selected provider → public-commentary adapter → execution trace
    → Desktop projection and PC transport → Mobile projection

[public_commentary.py](../osia/public_commentary.py) normalizes public text and assembles native tool-call fragments separately. [execution_trace.py](../osia/execution_trace.py) records provenance and revisions. Supported textual calls are parsed before internal protocol markup is removed.

Revisions from the same source extend their entry; distinct rounds append. Partial snapshots merge by exact conversation/request/turn/run and provider/model/reasoning binding. Missing or stale updates do not erase received wording. Canonical storage and transport projection budgets are separate. Mobile projects PC-authored state rather than generating another trace.

The narrow display helper in [main_popup.py](../osia/gui/main_popup.py) removes outer delimiters from supported complete whole-line bold headings. It preserves words/language, raw stored events, provenance and final answers. It does not convert a heading into an invented first-person action claim.

### Source locations

- [main_popup.py](../osia/gui/main_popup.py): _ChatThinkingTrace, apply_trace, _thinking_display_text and exact-turn UI binding.
- [thinking_orbs.py](../osia/gui/thinking_orbs.py): periodic six-dot geometry and rendering lifecycle.
- [execution_trace.py](../osia/execution_trace.py): canonical trace projection and merging.
- [server.py](../osia/server/server.py) and [relay_connector.py](../osia/relay_connector.py): PC-owned delivery.
- Mobile execution_trace_block.dart, thinking_orbs.dart and chat_screen.dart consume the canonical PC projection.

## Floating capsule

The capsule is an independent display surface. [IslandLifecycle](../osia/gui/island_lifecycle.py) owns a locked activity projection; [wake_mode in main.py](../osia/main.py) binds it to current producers. [DynamicIsland](../osia/gui/dynamic_island.py) paints and positions that projection.

Notifications request the current state rather than carrying a stale state value. begin establishes a producer's owner, update applies only to that owner, finish removes only it and close shuts down the projection. A late callback cannot revive a finished or replaced owner. A reserved conversation alone is not proof that model work is active. Recording and playback take priority over background Thinking.

The capsule supports horizontal, circular/corner and side-docked shapes. Shape/position restoration is independent from the activity state. Thinking uses the current six-light motion; listening uses audio waves and the microphone cue. Its animation clock is monotonic and unnecessary timers stop at rest, while hidden or with reduced motion. Geometry, theme, pointer interaction and Windows always-on-top repair remain in DynamicIsland.

## Desktop-input decoration

The screen-edge neon and temporary Windows cursors are a third presentation, separate from both Thinking surfaces:

- [desktop_activity.py](../osia/desktop_activity.py) records native-input activity and per-turn intent without storing tool arguments.
- [desktop_feedback.py](../osia/gui/desktop_feedback.py) paints theme-colored, click-through edge feedback without taking focus.
- [control_cursor.py](../osia/control_cursor.py) owns temporary Windows cursor resources and restoration.

Requests recognized by desktop_activity.DESKTOP_INTENT_TOOLS start a per-turn intent phase that ends with the matching terminal turn, including gaps between actions. Native primitives also publish their actual input activity. Stale presentation notifications cannot revive a finished phase. In the current source, a conversation with /mousecontrol disabled contributes no mouse decoration for its keyboard/UIA activity. Allowed keyboard, vision and structured browser actions remain callable; concurrent enabled work keeps its own indicator.

Display state is not an execution receipt or proof that the physical pointer moved. Tool outcomes come from the canonical [tool contracts and dispatch records](tools.md).
