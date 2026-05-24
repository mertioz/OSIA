# fix_osia4.py
from openai import OpenAI
import subprocess, json, os

client = OpenAI(
    api_key="sk-66c9981ace1c46d9904791fdb1843138",
    base_url="https://api.deepseek.com"
)

MODEL = "deepseek-v4-pro"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a bash command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
]

def run_tool(name, args):
    def bash(cmd, timeout=120):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return (r.stdout + r.stderr).strip()[:5000] or "(no output)"
        except subprocess.TimeoutExpired:
            return f"⏱️ Timeout after {timeout}s"
    if name == "bash":
        return bash(args["command"], args.get("timeout", 120))
    if name == "write_file":
        os.makedirs(os.path.dirname(args["path"]) or ".", exist_ok=True)
        with open(args["path"], "w") as f:
            f.write(args["content"])
        return f"✅ Written: {args['path']}"
    if name == "read_file":
        try:
            with open(args["path"]) as f:
                return f.read()[:5000]
        except Exception as e:
            return str(e)
    return f"❌ unknown tool: {name}"

SYSTEM = """
You are a senior Python/Linux developer patching OSIA at /home/OSIA/code/osia-project/.
Always use /home/OSIA/code/osia-project/venv/bin/python.
Read ALL relevant files before modifying anything.

FIXES TO APPLY:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 1 — Remove small floating overlay, keep only wake screen
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Read main.py and gui/overlay.py.

- Remove all references to the small floating overlay (overlay.py) from main.py
- Do NOT delete overlay.py file, just stop launching it
- The only GUI that remains is the wake screen (wake_screen.py)
- main.py --wake should start wake detection + wake screen only, no floating panel

DONE when: launching main.py --wake shows NO small panel, only wake screen on trigger.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 2 — Add model selector to wake screen (bottom right)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Read gui/wake_screen.py.

Add to the wake screen overlay (the dim fullscreen):
- Bottom-right corner: a small QComboBox listing available ollama models
  → populate by running "ollama list" at startup, parse model names
  → position: fixed at screen_width - 220, screen_height - 60
  → size: 200px wide, 36px tall
  → style: dark semi-transparent, white text, no border, small font (11px)
  → selecting a model updates the active model used by the agent
- The combobox is always visible (even when screen is not dimmed)
  → make it a separate always-on-top frameless window anchored bottom-right
  → so it's visible at all times without dimming the screen

DONE when: model selector visible bottom-right, changing it affects agent model.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 3 — Keep listening until command received, no premature "Thinking..."
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Read voice/wake.py and voice/stt.py.

Current problem: after wake word, it records 5 seconds then goes to "Thinking..."
even if the user said nothing meaningful.

Fix:
- After wake word detection, enter a "listening loop":
  → Record 4 second chunks
  → If transcribed text is empty or less than 3 words → record another chunk
  → If transcribed text has 3+ words → proceed to agent
  → Max 3 retries, then give up and say "Je n'ai pas compris, répétez."
- Only print "Thinking..." and call the agent AFTER valid command received

DONE when: OSIA waits for a real command before thinking.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 4 — Clean agent responses, no tool mentions, concise speech
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Read agent.py and the system prompt used for ollama.

Fix 1 — Update the ollama system prompt to include:
  "IMPORTANT RULES FOR RESPONSES:
  - Never mention tool names, function names, or technical steps in your spoken response.
  - Never say things like 'I will use bash' or 'calling tool' or '#tool_call'.
  - Just speak the result naturally and concisely, like a human assistant would.
  - Maximum 2-3 sentences in spoken responses unless more detail is explicitly requested.
  - If you performed an action, just say what you did, not how."

Fix 2 — Post-process the agent response before TTS:
  Add a clean_response(text) function that:
  - Removes any line starting with # or containing "tool_call", "bash(", "function"
  - Removes markdown: **, *, #, `, ---
  - Removes lines that are clearly code (contain { } or =>)
  - Returns clean natural text only
  Apply clean_response() to the agent output before passing to speak()

DONE when: OSIA speaks naturally without mentioning tools or markdown.

Rules:
- Read files before editing
- Test each phase
- Fix errors before moving on
- When ALL 4 phases done: say exactly OSIA_FIX4_COMPLETE
"""

TASK = """
Apply all 4 fixes to OSIA at /home/OSIA/code/osia-project/.
Read existing files first. Do not rewrite what works.
Do not stop until all phases done. Say OSIA_FIX4_COMPLETE when finished.
"""

def run_agent(prompt, max_iterations=200):
    print("🔧 Starting OSIA fix #4 agent...\n")
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]

    for i in range(max_iterations):
        print(f"\n--- Iteration {i+1} ---")
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        messages.append(msg)
        finish = response.choices[0].finish_reason

        if finish == "stop":
            content = msg.content or ""
            print(f"\n💬 Agent: {content}")
            if "OSIA_FIX4_COMPLETE" in content:
                print("\n✅ OSIA fix #4 done!")
                return
            messages.append({"role": "user", "content": "Continue. Move to the next phase."})
            continue

        if msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                print(f"  🔧 {name}: {str(args)[:150]}")
                result = run_tool(name, args)
                print(f"  → {result[:500]}")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    print("⚠️ max_iterations reached")

if __name__ == "__main__":
    run_agent(TASK)
