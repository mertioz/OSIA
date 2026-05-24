#!/usr/bin/env python3
"""
OSIA Core Agent — connects to local Ollama via OpenAI-compatible API
with a manual tool-calling loop that works with any model (llama3, etc.).
"""

import json
import re
import subprocess
import os
from openai import OpenAI

OLLAMA_BASE_URL = "http://localhost:11434/v1"
MODEL = "llama3"

# ---------------------------------------------------------------------------
# Language detection (Phase 1)
# ---------------------------------------------------------------------------

def detect_system_lang() -> str:
    """Detect host system language from $LANG env var.
    Returns 'fr' if French, 'en' otherwise.
    """
    lang_env = os.environ.get("LANG", "")
    if lang_env.lower().startswith("fr"):
        return "fr"
    return "en"


DEFAULT_LANG = detect_system_lang()

# ---------------------------------------------------------------------------
# Response cleaning rules (shared across languages)
# ---------------------------------------------------------------------------

CLEAN_RESPONSE_RULES = """IMPORTANT RULES FOR RESPONSES:
- Never mention tool names, function names, or technical steps in your spoken response.
- Never say things like 'I will use bash' or 'calling tool' or '#tool_call'.
- Just speak the result naturally and concisely, like a human assistant would.
- Maximum 2-3 sentences in spoken responses unless more detail is explicitly requested.
- If you performed an action, just say what you did, not how."""

# ---------------------------------------------------------------------------
# System prompt — dynamic per language
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_EN = f"""You are OSIA, an AI assistant. Reply in English by default.
You are a local AI voice assistant running on a Fedora Linux machine with FULL SYSTEM ACCESS.
You can run any shell command, read/write/delete any file, launch any application, take screenshots, and control a web browser.
Your working directory is /home/OSIA/code/osia-project/.

## Available Tools
You have these tools available:

System & shell:
- bash: Execute a bash command. Usage: {{"tool": "bash", "command": "<command>"}}
- run_command: Run any shell command and return its output. Usage: {{"tool": "run_command", "command": "<command>"}}
- open_application: Launch an application by name. Usage: {{"tool": "open_application", "name": "<app_name>"}}
- take_screenshot: Capture the desktop to /tmp/screenshot.png and return the path. Usage: {{"tool": "take_screenshot"}}

Files:
- read_file: Read a file. Usage: {{"tool": "read_file", "path": "<path>"}}
- write_file: Write to a file. Usage: {{"tool": "write_file", "path": "<path>", "content": "<content>"}}
- list_files: List files in a directory. Usage: {{"tool": "list_files", "path": "<path>"}}
- delete_file: Delete a file. Usage: {{"tool": "delete_file", "path": "<path>"}}

Browser:
- navigate: Open a URL in the browser. Usage: {{"tool": "navigate", "url": "<url>"}}
- click: Click an element by CSS selector. Usage: {{"tool": "click", "selector": "<css_selector>"}}
- fill: Type into an input field. Usage: {{"tool": "fill", "selector": "<css_selector>", "value": "<text>"}}
- read_page: Get all visible text from the current page. Usage: {{"tool": "read_page"}}

## How to Use Tools
When you need to use a tool, FIRST output one short sentence telling the user what you're about to do (e.g. "Opening Spotify.", "Let me check.", "Reading that file."). This sentence is streamed and spoken to the user in parallel with the tool running, so it hides the tool's execution latency. THEN output a JSON tool call wrapped in <tool_call> tags:

Opening Spotify.
<tool_call>
{{"tool": "open_application", "name": "spotify"}}
</tool_call>

You may issue multiple tool calls in sequence. After each tool call, you will receive the tool output.
After you have all information needed, provide your final answer as plain text WITHOUT <tool_call> tags.

## Browser Usage
When asked to browse the web: first navigate to a URL, then optionally click, fill, or read_page.
Use simple CSS selectors like 'input[name="q"]', 'button', 'textarea', etc.

{CLEAN_RESPONSE_RULES}

## Rules
- Be concise and direct — your responses will be spoken via TTS.
- Always use tools when you need to access the system — never guess.
- When you are done, give a clear final answer without any <tool_call> blocks.
- Use absolute paths when reading/writing files."""

SYSTEM_PROMPT_FR = f"""Tu es OSIA, un assistant IA. Réponds en français par défaut.
Tu es un assistant vocal IA local fonctionnant sur une machine Fedora Linux avec UN ACCÈS SYSTÈME COMPLET.
Tu peux exécuter n'importe quelle commande shell, lire/écrire/supprimer n'importe quel fichier, lancer n'importe quelle application, prendre des captures d'écran, et contrôler un navigateur web.
Ton répertoire de travail est /home/OSIA/code/osia-project/.

## Outils disponibles
Tu disposes des outils suivants :

Système & shell :
- bash : Exécuter une commande bash. Usage : {{"tool": "bash", "command": "<commande>"}}
- run_command : Exécuter n'importe quelle commande shell et retourner sa sortie. Usage : {{"tool": "run_command", "command": "<commande>"}}
- open_application : Lancer une application par son nom. Usage : {{"tool": "open_application", "name": "<nom_app>"}}
- take_screenshot : Capturer le bureau vers /tmp/screenshot.png et retourner le chemin. Usage : {{"tool": "take_screenshot"}}

Fichiers :
- read_file : Lire un fichier. Usage : {{"tool": "read_file", "path": "<chemin>"}}
- write_file : Écrire dans un fichier. Usage : {{"tool": "write_file", "path": "<chemin>", "content": "<contenu>"}}
- list_files : Lister les fichiers d'un répertoire. Usage : {{"tool": "list_files", "path": "<chemin>"}}
- delete_file : Supprimer un fichier. Usage : {{"tool": "delete_file", "path": "<chemin>"}}

Navigateur :
- navigate : Ouvrir une URL dans le navigateur. Usage : {{"tool": "navigate", "url": "<url>"}}
- click : Cliquer sur un élément par sélecteur CSS. Usage : {{"tool": "click", "selector": "<selecteur_css>"}}
- fill : Remplir un champ de saisie. Usage : {{"tool": "fill", "selector": "<selecteur_css>", "value": "<texte>"}}
- read_page : Obtenir tout le texte visible de la page actuelle. Usage : {{"tool": "read_page"}}

## Comment utiliser les outils
Quand tu as besoin d'un outil, produit D'ABORD une phrase courte annonçant l'action à l'utilisateur (ex : « J'ouvre Spotify. », « Je regarde. », « Je lis le fichier. »). Cette phrase est lue à l'utilisateur pendant que l'outil s'exécute, masquant ainsi sa latence. ENSUITE, produit l'appel d'outil JSON dans des balises <tool_call> :

J'ouvre Spotify.
<tool_call>
{{"tool": "open_application", "name": "spotify"}}
</tool_call>

Tu peux faire plusieurs appels d'outils à la suite. Après chaque appel, tu recevras le résultat.
Quand tu as toutes les informations nécessaires, donne ta réponse finale en texte simple SANS balises <tool_call>.

## Navigation web
Quand on te demande de naviguer sur le web : d'abord naviguer vers une URL, puis éventuellement cliquer, remplir ou lire la page.
Utilise des sélecteurs CSS simples comme 'input[name="q"]', 'button', 'textarea', etc.

{CLEAN_RESPONSE_RULES}

## Règles
- Sois concis et direct — tes réponses seront lues à voix haute par TTS.
- Utilise toujours les outils quand tu as besoin d'accéder au système — ne devine jamais.
- Quand tu as fini, donne une réponse finale claire sans aucun bloc <tool_call>.
- Utilise des chemins absolus pour lire/écrire des fichiers."""


def get_system_prompt(lang: str = None) -> str:
    """Return the system prompt for the given language."""
    if lang is None:
        lang = DEFAULT_LANG
    if lang == "fr":
        return SYSTEM_PROMPT_FR
    return SYSTEM_PROMPT_EN


# Default system prompt
SYSTEM_PROMPT = get_system_prompt()


# ---------------------------------------------------------------------------
# PHASE 4: Response cleaning
# ---------------------------------------------------------------------------

def clean_response(text: str) -> str:
    """Post-process agent response to remove tool mentions, markdown, and code.

    - Removes <tool_call> and <tool_result> blocks first
    - Removes lines that are PURELY tool-related (contain only tool syntax)
    - Removes markdown: **, *, #, `, ---
    - Removes lines that are clearly code (contain { } or =>)
    - Returns clean natural text only
    """
    if not text:
        return text

    # Step 1: Remove <tool_call> and <tool_result> blocks entirely
    text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
    text = re.sub(r'<tool_result>.*?</tool_result>', '', text, flags=re.DOTALL)

    # Step 2: Remove tool-mentioning phrases from within lines
    # These regexes catch common patterns where the model talks about using tools
    phrase_patterns = [
        r'\bI will use \w+\b[^.]*\.?\s*',   # "I will use bash to ..."
        r'\bI\'ll use \w+\b[^.]*\.?\s*',    # "I'll use bash to ..."
        r'\bLet me use \w+\b[^.]*\.?\s*',    # "Let me use bash to ..."
        r'\bcalling tool\b[^.]*\.?\s*',        # "calling tool ..."
        r'\bUsing \w+ tool\b[^.]*\.?\s*',     # "Using bash tool..."
        r'#tool_call\b',                           # "#tool_call"
        r'#tool_result\b',                         # "#tool_result"
    ]
    for pattern in phrase_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)

    # Step 3: Process line by line for remaining issues
    lines = text.split("\n")
    cleaned_lines = []

    for line in lines:
        stripped = line.strip()

        # Keep empty lines (collapse later)
        if not stripped:
            cleaned_lines.append("")
            continue

        # Skip lines that are PURELY tool-related markers
        lower = stripped.lower()
        if lower in ("#tool_call", "#tool_result", "```", "```json", "```python",
                     "```bash", "```text", "```html"):
            continue

        # Skip lines that are ONLY a code block fence or tool marker
        if re.match(r'^```\w*$', stripped):
            continue

        # Skip lines starting with # followed by tool mention
        if stripped.startswith("#") and any(kw in lower for kw in
            ["tool", "bash", "function", "command"]):
            continue

        # Skip lines that look like pure JSON tool calls
        if stripped.startswith('{"tool"') and stripped.endswith('}'):
            continue

        # Skip lines that are ONLY code (contain => or are { ... })
        if "=>" in stripped and len(stripped) < 120:
            continue
        if re.match(r'^\{.*\}$', stripped) and ('"' in stripped):
            continue

        cleaned_lines.append(stripped)

    # Rejoin
    text = "\n".join(cleaned_lines)

    # Step 4: Remove markdown formatting
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)   # bold
    text = re.sub(r'\*(.*?)\*', r'\1', text)        # italic
    text = re.sub(r'`(.*?)`', r'\1', text)           # inline code
    text = re.sub(r'^---+\s*$', '', text, flags=re.MULTILINE)  # hr

    # Step 5: Collapse multiple blank lines and strip
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    # Step 6: Fix double spaces from phrase removal
    text = re.sub(r'  +', ' ', text)

    # Step 7: Fix sentence fragments like ". File contents: ..." (leading period)
    text = re.sub(r'^\s*\.\s+', '', text)
    text = re.sub(r'\s+\.\s*$', '.', text)

    # If everything was stripped, return fallback
    if not text:
        return "I've completed the task."

    return text



# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_bash(command: str) -> str:
    """Execute a bash command and return stdout + stderr (truncated)."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=30, cwd="/home/OSIA/code/osia-project"
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if out and err:
            return f"STDOUT:\n{out}\n\nSTDERR:\n{err}"
        if out:
            return out
        if err:
            return f"STDERR:\n{err}"
        return "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 30s"
    except Exception as e:
        return f"ERROR: {e}"


def tool_read_file(path: str) -> str:
    """Read a file and return its contents (truncated to 4000 chars)."""
    if not path.startswith("/"):
        path = os.path.join("/home/OSIA/code/osia-project", path)
    try:
        with open(path, "r") as f:
            content = f.read()
        if len(content) > 4000:
            content = content[:4000] + "\n... [TRUNCATED]"
        return content
    except Exception as e:
        return f"ERROR reading file: {e}"


def tool_write_file(path: str, content: str) -> str:
    """Write content to a file. Creates parent directories if needed."""
    if not path.startswith("/"):
        path = os.path.join("/home/OSIA/code/osia-project", path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"ERROR writing file: {e}"


def tool_open_application(name: str) -> str:
    """Launch an application by name. Detaches so it keeps running."""
    try:
        subprocess.Popen(name, shell=True)
        return f"Launched: {name}"
    except Exception as e:
        return f"ERROR launching {name}: {e}"


def tool_list_files(path: str) -> str:
    """List files in a directory."""
    if not path.startswith("/"):
        path = os.path.join("/home/OSIA/code/osia-project", path)
    try:
        entries = os.listdir(path)
        entries.sort()
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"ERROR listing {path}: {e}"


def tool_delete_file(path: str) -> str:
    """Delete a file."""
    if not path.startswith("/"):
        path = os.path.join("/home/OSIA/code/osia-project", path)
    try:
        os.remove(path)
        return f"Deleted: {path}"
    except Exception as e:
        return f"ERROR deleting {path}: {e}"


def tool_run_command(command: str) -> str:
    """Run a bash command and return its output. Alias for bash."""
    return tool_bash(command)


def tool_take_screenshot() -> str:
    """Take a screenshot of the desktop using scrot. Returns the path."""
    path = "/tmp/screenshot.png"
    try:
        result = subprocess.run(
            ["scrot", "-o", path], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return f"ERROR taking screenshot: {result.stderr.strip() or 'scrot failed'}"
        return path
    except FileNotFoundError:
        return "ERROR: scrot is not installed"
    except Exception as e:
        return f"ERROR taking screenshot: {e}"


# Browser tools are imported lazily to avoid startup overhead
_browser_tools = None

def _get_browser_tools():
    global _browser_tools
    if _browser_tools is None:
        from osia.tools.browser import navigate, click, fill, read_page
        _browser_tools = {
            "navigate": navigate,
            "click": click,
            "fill": fill,
            "read_page": read_page,
        }
    return _browser_tools


def tool_navigate(url: str) -> str:
    return _get_browser_tools()["navigate"](url)

def tool_click(selector: str) -> str:
    return _get_browser_tools()["click"](selector)

def tool_fill(selector: str, value: str) -> str:
    return _get_browser_tools()["fill"](selector, value)

def tool_read_page() -> str:
    return _get_browser_tools()["read_page"]()


TOOL_MAP = {
    "bash": tool_bash,
    "run_command": tool_run_command,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "list_files": tool_list_files,
    "delete_file": tool_delete_file,
    "open_application": tool_open_application,
    "take_screenshot": tool_take_screenshot,
    "navigate": tool_navigate,
    "click": tool_click,
    "fill": tool_fill,
    "read_page": tool_read_page,
}


def parse_tool_calls(text: str) -> list:
    """Extract tool calls from <tool_call>...</tool_call> blocks."""
    pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
    matches = re.findall(pattern, text, re.DOTALL)
    calls = []
    for m in matches:
        try:
            calls.append(json.loads(m))
        except json.JSONDecodeError:
            pass
    return calls


def has_tool_call(text: str) -> bool:
    """Check if text contains a <tool_call> block."""
    return bool(re.search(r"<tool_call>", text))


class OSIAAgent:
    def __init__(self, model: str = MODEL, base_url: str = OLLAMA_BASE_URL, lang: str = None):
        self.client = OpenAI(base_url=base_url, api_key="ollama")
        self.model = model
        self.provider = "ollama"
        self.lang = lang if lang else DEFAULT_LANG
        self.messages = [{"role": "system", "content": get_system_prompt(self.lang)}]

    def reset(self):
        """Clear conversation history except system prompt."""
        self.messages = [{"role": "system", "content": get_system_prompt(self.lang)}]

    def set_model(self, model_id: str) -> None:
        """Switch the agent to a new model, reconfiguring the client for that
        model's provider (Anthropic, OpenAI, DeepSeek, Google, xAI, Ollama).
        """
        from osia.models import provider_for_model, PROVIDERS
        from osia.config import get_api_key

        provider = provider_for_model(model_id)
        cfg = PROVIDERS.get(provider, PROVIDERS["ollama"])
        api_key = get_api_key(provider) if provider != "ollama" else "ollama"
        if not api_key:
            api_key = "ollama"  # placeholder; request will likely 401 if used

        self.client = OpenAI(base_url=cfg["base_url"], api_key=api_key)
        self.model = model_id
        self.provider = provider

    # Match a sentence-ending punctuation char followed by whitespace or end-of-string.
    _SENTENCE_END_RE = re.compile(r'[.!?](?=\s|$)')
    # Secondary break points used for early flush — commas, semicolons,
    # colons, em/en-dashes. Lets a long lead-in clause start synthesizing
    # before the model emits the final period.
    _EARLY_BREAK_RE = re.compile(r'[,;:—–](?=\s|$)')
    _EARLY_FLUSH_MIN_CHARS = 40

    def _emit_sentences(self, speakable: str, on_sentence, flush: bool = False) -> str:
        """Drain complete sentences out of ``speakable`` and emit them.

        Also emits early at a sub-sentence break (comma, semicolon, …)
        once the buffer has grown past ``_EARLY_FLUSH_MIN_CHARS`` — this
        keeps TTS latency low when the model produces a long lead-in
        clause before any sentence-ending punctuation.

        Returns the leftover (incomplete sentence) buffer.
        """
        while True:
            m = self._SENTENCE_END_RE.search(speakable)
            if m:
                end = m.end()
            elif len(speakable) >= self._EARLY_FLUSH_MIN_CHARS:
                early = self._EARLY_BREAK_RE.search(speakable)
                if not early or early.end() < self._EARLY_FLUSH_MIN_CHARS:
                    break
                end = early.end()
            else:
                break
            sentence = speakable[:end].strip()
            speakable = speakable[end:].lstrip()
            if sentence:
                cleaned = clean_response(sentence)
                if cleaned and cleaned.strip():
                    on_sentence(cleaned)
        if flush and speakable.strip():
            cleaned = clean_response(speakable.strip())
            if cleaned and cleaned.strip():
                on_sentence(cleaned)
            speakable = ""
        return speakable

    def _stream_with_sentences(self, on_sentence) -> str:
        """Stream a chat completion, emitting each complete sentence to
        ``on_sentence`` as it arrives. Returns the full accumulated content
        so the outer loop can still detect tool calls.

        Anything inside a ``<tool_call>...</tool_call>`` block is buffered
        but never emitted — we don't want to speak tool markup.
        """
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            stream=True,
        )

        full = ""
        pending = ""       # raw text not yet routed to speakable/tool
        speakable = ""     # awaiting a sentence-ending punctuation char
        in_tool_call = False

        for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content or ""
            except (IndexError, AttributeError):
                delta = ""
            if not delta:
                continue
            full += delta
            pending += delta

            # Walk through pending, splitting at <tool_call>/</tool_call> markers.
            while pending:
                if in_tool_call:
                    close = pending.find("</tool_call>")
                    if close == -1:
                        pending = ""  # still inside tool call, discard
                        break
                    pending = pending[close + len("</tool_call>"):]
                    in_tool_call = False
                    continue

                open_idx = pending.find("<tool_call>")
                if open_idx == -1:
                    # Hold back the last few chars in case they're the start of
                    # a partial "<tool_call>" tag that's split across chunks.
                    safe = max(0, len(pending) - 10)
                    speakable += pending[:safe]
                    pending = pending[safe:]
                    speakable = self._emit_sentences(speakable, on_sentence)
                    break

                # Flush text up to the tag, then enter tool-call mode.
                speakable += pending[:open_idx]
                pending = pending[open_idx + len("<tool_call>"):]
                in_tool_call = True
                speakable = self._emit_sentences(
                    speakable, on_sentence, flush=True,
                )

        # End of stream — final flush of anything still pending.
        if not in_tool_call:
            speakable += pending
            self._emit_sentences(speakable, on_sentence, flush=True)

        return full

    def run(self, user_input: str, max_tool_rounds: int = 10, on_sentence=None) -> str:
        """Run the agent loop: send user input, handle tool calls, return final response.

        If ``on_sentence`` is provided, the API call is streamed and the
        callback is invoked with each complete sentence (cleaned) as soon
        as it arrives. Sentences that fall inside a ``<tool_call>`` block
        are skipped so tool markup is never spoken.
        """
        self.messages.append({"role": "user", "content": user_input})

        for round_num in range(max_tool_rounds):
            if on_sentence is not None:
                content = self._stream_with_sentences(on_sentence)
            else:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.messages,
                )
                content = response.choices[0].message.content or ""

            # Check for tool calls
            if has_tool_call(content):
                tool_calls = parse_tool_calls(content)

                if not tool_calls:
                    self.messages.append({"role": "assistant", "content": content})
                    return content

                # Add assistant message with the tool call
                self.messages.append({"role": "assistant", "content": content})

                # Execute each tool and collect results
                tool_results = []
                for tc in tool_calls:
                    tool_name = tc.get("tool", "")
                    if tool_name in TOOL_MAP:
                        kwargs = {k: v for k, v in tc.items() if k != "tool"}
                        result = TOOL_MAP[tool_name](**kwargs)
                    else:
                        result = f"Unknown tool: {tool_name}"
                    tool_results.append(f"Tool {tool_name} result:\n{result}")

                # Feed tool results back
                feedback = "\n\n".join(tool_results)
                self.messages.append({
                    "role": "user",
                    "content": f"<tool_result>\n{feedback}\n</tool_result>\nPlease continue with your response based on these results."
                })
                continue

            # No tool call — final response
            self.messages.append({"role": "assistant", "content": content})
            return content

        return "I've reached the maximum number of tool calls without a final answer."


def run_agent(user_input: str, model: str = MODEL, lang: str = None) -> str:
    """Convenience function: run a single query and return the response."""
    agent = OSIAAgent(model=model, lang=lang)
    return agent.run(user_input)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = input("OSIA> ")
    result = run_agent(query)
    print(f"\n{result}")
