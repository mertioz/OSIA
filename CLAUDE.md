# OSIA PC — Rules for Claude Code

> **Single cross-platform codebase.** This repo (`osia-project`) is the one
> source tree for OSIA on **Linux, Windows, and Mac** — the former `osia-windows`
> port (WOR-11) has been merged in. Every change must stay cross-platform and run
> natively on all three OSes from this tree (see "Cross-platform rules" below).

## CRITICAL — Do NOT modify these unless explicitly asked:
- `osia/agent.py` tool-call parser (`_extract_tool_calls`, `_parse_xml_tool_calls`, `_parse_bare_json_tool_calls`, `_parse_native_tool_calls`, `_parse_tool_json`) — this took many iterations to get right. Touching it breaks tool execution across providers.
- The `strip_raw_tool_tags` / `strip_reasoning_blocks` pipeline ORDER (parse before strip, strip only after execution)
- The `_AntiLoopGuard` class and its detectors
- `osia/tools/browser.py` — CDP/Firefox BiDi launch logic, profile isolation, `_cdp_launching`/`_firefox_launching` flags
- The WebSocket message protocol in `osia/server/server.py`
- `MAX_RESPONSE_TOKENS` (must stay 32768, provider-safe max)
- Provider config in `osia/models.py` (base URLs, model IDs)

## Cross-platform rules (WOR-11) — Linux / Windows / Mac:
- **Cross-platform paths only.** Resolve the project root dynamically with
  `pathlib.Path(__file__).resolve().parent...` — never hardcode `/home/OSIA/...`
  or `/tmp`. The user config/data dir is `Path.home() / ".osia"` on every OS
  (`osia/config.py` `CONFIG_DIR`, `osia/server/server.py` `CONFIG_DIR`). Scratch
  dirs come from `tempfile.gettempdir()`, not `/tmp`.
- **Never set `QT_QPA_PLATFORM=xcb` on Windows.** It is X11-only — guard every
  occurrence with `sys.platform.startswith("linux")` (`osia/main.py`,
  GUI self-test blocks in `osia/gui/main_popup.py` and `dynamic_island.py`).
- **`X11BypassWindowManagerHint` is X11-only** — only OR it into the window
  flags when `sys.platform != "win32"` (`osia/gui/dynamic_island.py`). The base
  flags `FramelessWindowHint | WindowStaysOnTopHint | Tool` work on Windows.
- **Autostart is platform-split** (`osia/utils/assets.py`): Linux uses the XDG
  `~/.config/autostart/osia.desktop`; Windows uses the HKCU `...\Run` registry
  value `OSIA` via `winreg`. Keep `autostart_enabled()` reflecting reality.
- **Audio must degrade gracefully.** Every file importing `sounddevice` wraps
  the import in `try/except` (`_AUDIO_OK`) so a missing PortAudio backend never
  crashes startup; recorders return empty audio, TTS skips playback
  (`osia/voice/stt.py`, `tts.py`, `wake.py`, `wake_training/record_*.py`).
- **System info is platform-safe** (`osia/agent.py`): use `platform.uname()`
  (not `os.uname()`, which is Unix-only) and guard `os.getloadavg()` in
  `try/except AttributeError` (absent on Windows).
- **Service control is platform-aware** (`osia/utils/systemd_manager.py`):
  Linux uses `systemctl`/`pkexec`; Windows (`sys.platform == "win32"`) routes
  the tunnel to `windows\*_tunnel.bat` and the mobile server to a detached
  process. Do NOT change the public function names/signatures — the GUI depends
  on them.
- **Tunnel = Cloudflare via `windows\` batch scripts** on Windows
  (`install_tunnel.bat` / `start_tunnel.bat` / `stop_tunnel.bat`), not systemd.
- **Entry points:** `install.bat` (one-click setup) and `start.bat` (launcher)
  at the repo root; dependencies in `requirements.txt`.
- **`osia/agent.py` and `osia/tools/browser.py` paths are cross-platform now:**
  `agent.py` resolves `_PROJECT_DIR` dynamically and writes screenshots to the
  OS temp dir; `browser.py` uses `OSIA_DATA_DIR = Path.home() / ".osia"`. Only
  their paths were touched — the protected tool-call parser (agent.py) and the
  CDP/Firefox-BiDi launch logic (browser.py) remain off-limits per the CRITICAL
  rules above.

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
