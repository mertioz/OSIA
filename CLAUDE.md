# OSIA PC — Rules for Claude Code

## CRITICAL — Do NOT modify these unless explicitly asked:
- `osia/agent.py` tool-call parser (`_extract_tool_calls`, `_parse_xml_tool_calls`, `_parse_bare_json_tool_calls`, `_parse_native_tool_calls`, `_parse_tool_json`) — this took many iterations to get right. Touching it breaks tool execution across providers.
- The `strip_raw_tool_tags` / `strip_reasoning_blocks` pipeline ORDER (parse before strip, strip only after execution)
- The `_AntiLoopGuard` class and its detectors
- `osia/tools/browser.py` — CDP/Firefox BiDi launch logic, profile isolation, `_cdp_launching`/`_firefox_launching` flags
- The WebSocket message protocol in `osia/server/server.py`
- `MAX_RESPONSE_TOKENS` (must stay 32768, provider-safe max)
- Provider config in `osia/models.py` (base URLs, model IDs)

## Before ANY change:
- Only modify the specific files/functions needed for the requested task
- Never refactor or "improve" code outside the scope of the request
- Never change function signatures that other files depend on
- Preserve the parse-before-strip pipeline order at all costs

## When fixing a bug:
- Make the SMALLEST possible change that fixes it
- Do not touch unrelated code paths
- List exactly which files and functions you changed and why

## After every change, verify these still work:
- Tool calls execute (DeepSeek native + Groq XML formats)
- Browser opens (Brave CDP + Firefox BiDi), one window only
- TTS speaks, responses are never empty
- No `<think>` or `<tool_call>` tags leak into output
