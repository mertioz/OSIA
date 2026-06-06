#!/usr/bin/env python3
"""
OSIA Core Agent — connects to local Ollama via OpenAI-compatible API
with a manual tool-calling loop that works with any model (llama3, etc.).
"""

import ast
import json
import re
import subprocess
import os
import platform
import shutil
import stat
import fnmatch
import signal
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
from pathlib import Path

import psutil
from openai import OpenAI

OLLAMA_BASE_URL = "http://localhost:11434/v1"
MODEL = "llama3"

# Project root — resolved dynamically so paths work on any machine and OS
# (Windows + Linux) instead of a hardcoded home directory.
_PROJECT_DIR = str(Path(__file__).resolve().parent.parent)

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
# System prompt — dynamic per language
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_EN = """You are a local AI assistant running on the user's local machine with FULL SYSTEM ACCESS. Reply in English by default.
You can run any shell command, read/write/delete any file, launch any application, take screenshots, and control a web browser.
Your working directory is /home/OSIA/code/osia-project/.
When the user refers to the "desktop" or "bureau", it means the desktop folder — ~/Bureau on French Linux systems and ~/Desktop on English ones. Always use the exact absolute path given in the [DESKTOP PATH: ...] line of this prompt.

## Available Tools
You have these tools available:

System & shell:
- bash: Execute a bash command. Usage: {"tool": "bash", "command": "<command>"}
- run_command: Run any shell command and return its output. Usage: {"tool": "run_command", "command": "<command>"}
- open_application: Launch an application by name. Usage: {"tool": "open_application", "name": "<app_name>"}
- take_screenshot: Capture the desktop to the system temp directory and return the path. Usage: {"tool": "take_screenshot"}
- get_system_info: Snapshot of CPU, RAM and disk usage. Usage: {"tool": "get_system_info"}
- list_running_processes: Top processes by CPU/memory. Usage: {"tool": "list_running_processes", "limit": 20}
- kill_process: Kill a process by PID (int) or name (substring match). Usage: {"tool": "kill_process", "name_or_pid": "<pid_or_name>"}
- set_volume: Set system volume to 0-100 percent. Usage: {"tool": "set_volume", "level": 50}
- get_clipboard: Return the current clipboard contents. Usage: {"tool": "get_clipboard"}
- set_clipboard: Put text on the clipboard. Usage: {"tool": "set_clipboard", "text": "<text>"}

Files:
- read_file: Read a file. Usage: {"tool": "read_file", "path": "<path>"}
- write_file: Write to a file. Usage: {"tool": "write_file", "path": "<path>", "content": "<content>"}
- list_files: List files in a directory. Usage: {"tool": "list_files", "path": "<path>"}
- delete_file: Delete a file. Usage: {"tool": "delete_file", "path": "<path>"}
- create_directory: Create a folder (and any missing parents). Usage: {"tool": "create_directory", "path": "<path>"}
- delete_directory: Delete a folder. Pass recursive=true to remove non-empty dirs. Usage: {"tool": "delete_directory", "path": "<path>", "recursive": false}
- rename_file: Rename or move a file or folder. Usage: {"tool": "rename_file", "old_path": "<old>", "new_path": "<new>"}
- copy_file: Copy a file. Usage: {"tool": "copy_file", "src": "<src>", "dst": "<dst>"}
- get_file_info: Size, permissions, owner, and timestamps. Usage: {"tool": "get_file_info", "path": "<path>"}
- search_files: Recursively find files by glob pattern. Usage: {"tool": "search_files", "directory": "<dir>", "pattern": "*.py"}

Code editor:
- open_in_editor: Open a file in the user's preferred editor (VS Code, gedit, kate…). Usage: {"tool": "open_in_editor", "path": "<path>"}
- edit_file_lines: Replace a range of lines (1-indexed, inclusive) in a file. Usage: {"tool": "edit_file_lines", "path": "<path>", "start_line": 10, "end_line": 12, "new_content": "<replacement>"}

Browser (drives a REAL visible browser — Chromium/Brave via CDP, or Firefox via WebDriver BiDi — same page the user sees; cookies/sessions persist):
- navigate: Go to a URL in the real browser. Launches Brave/Chromium with remote debugging on first use (falls back to Firefox over BiDi if no Chromium browser is installed). Automatically waits for the page DOM to be ready — never wait manually after navigating. Usage: {"tool": "navigate", "url": "<url>"}
- click: Click an element by CSS selector. Usage: {"tool": "click", "selector": "<css_selector>"}
- find_and_click: Click the first element whose visible text matches. Use when you don't know the selector but can see the label. Usage: {"tool": "find_and_click", "text": "<visible_text>"}
- fill: Set the value of an input field in one shot (no keystroke events). Usage: {"tool": "fill", "selector": "<css_selector>", "value": "<text>"}
- type_text: Focus an element and type character-by-character via the keyboard. Use for search boxes / JS-heavy forms. Usage: {"tool": "type_text", "selector": "<css_selector>", "text": "<text>"}
- press_key: Press a single keyboard key on the active page (Enter, Escape, Tab, ArrowDown, Backspace, "Control+A", …). Usage: {"tool": "press_key", "key": "Enter"}
- read_page: Visible text of the current page (truncated). Usage: {"tool": "read_page"}
- get_page_text: Full visible text of the page (higher cap than read_page). Usage: {"tool": "get_page_text"}
- get_current_url: Return the URL of the active page. Usage: {"tool": "get_current_url"}
- scroll: Scroll by a pixel amount in a direction. Usage: {"tool": "scroll", "direction": "down", "amount": 600}
- scroll_down: Scroll down by one viewport (or by ``amount`` pixels). Usage: {"tool": "scroll_down"}
- go_back: Navigate back in history. Usage: {"tool": "go_back"}
- go_forward: Navigate forward in history. Usage: {"tool": "go_forward"}
- open_new_tab: Open a URL in a new tab and switch to it. Usage: {"tool": "open_new_tab", "url": "<url>"}
- open_browser: Open a SPECIFIC browser by name (firefox, chrome, chromium, opera, edge, brave), optionally at a URL. Use when the user asks for a particular browser ("open Firefox"). Firefox is FULLY supported for automation via WebDriver BiDi: after opening it, all browser tools (navigate, click, fill, type_text, read_page, screenshots, …) work inside Firefox exactly like Brave/Chrome. Usage: {"tool": "open_browser", "name": "firefox", "url": "<optional_url>"}

## How to Use Tools
To call a tool, output a JSON tool call wrapped in <tool_call> tags:

<tool_call>
{"tool": "open_application", "name": "spotify"}
</tool_call>

After each tool call you will receive the tool output. You may issue multiple tool calls in sequence.
When you have all the information you need, give your final answer as plain text (no <tool_call> tags).
When all tools are done, you MUST write a complete final answer based on everything you found — never end your turn with only tool calls, tool markup, or an empty reply."""

SYSTEM_PROMPT_FR = """Tu es un assistant vocal IA local fonctionnant sur la machine locale de l'utilisateur avec UN ACCÈS SYSTÈME COMPLET. Réponds en français par défaut.
Tu peux exécuter n'importe quelle commande shell, lire/écrire/supprimer n'importe quel fichier, lancer n'importe quelle application, prendre des captures d'écran, et contrôler un navigateur web.
Ton répertoire de travail est /home/OSIA/code/osia-project/.
Quand l'utilisateur parle du « bureau » (ou « desktop »), il s'agit du dossier Bureau — ~/Bureau sur les systèmes Linux français et ~/Desktop sur les anglais. Utilise toujours le chemin absolu exact donné dans la ligne [DESKTOP PATH: ...] de ce prompt.

## Outils disponibles
Tu disposes des outils suivants :

Système & shell :
- bash : Exécuter une commande bash. Usage : {"tool": "bash", "command": "<commande>"}
- run_command : Exécuter n'importe quelle commande shell et retourner sa sortie. Usage : {"tool": "run_command", "command": "<commande>"}
- open_application : Lancer une application par son nom. Usage : {"tool": "open_application", "name": "<nom_app>"}
- take_screenshot : Capturer le bureau vers le répertoire temporaire système et retourner le chemin. Usage : {"tool": "take_screenshot"}
- get_system_info : Aperçu de l'utilisation CPU, RAM et disque. Usage : {"tool": "get_system_info"}
- list_running_processes : Top des processus par CPU/mémoire. Usage : {"tool": "list_running_processes", "limit": 20}
- kill_process : Tuer un processus par PID (entier) ou nom (sous-chaîne). Usage : {"tool": "kill_process", "name_or_pid": "<pid_ou_nom>"}
- set_volume : Régler le volume système entre 0 et 100. Usage : {"tool": "set_volume", "level": 50}
- get_clipboard : Retourner le contenu actuel du presse-papiers. Usage : {"tool": "get_clipboard"}
- set_clipboard : Mettre du texte dans le presse-papiers. Usage : {"tool": "set_clipboard", "text": "<texte>"}

Fichiers :
- read_file : Lire un fichier. Usage : {"tool": "read_file", "path": "<chemin>"}
- write_file : Écrire dans un fichier. Usage : {"tool": "write_file", "path": "<chemin>", "content": "<contenu>"}
- list_files : Lister les fichiers d'un répertoire. Usage : {"tool": "list_files", "path": "<chemin>"}
- delete_file : Supprimer un fichier. Usage : {"tool": "delete_file", "path": "<chemin>"}
- create_directory : Créer un dossier (et les parents manquants). Usage : {"tool": "create_directory", "path": "<chemin>"}
- delete_directory : Supprimer un dossier. recursive=true pour supprimer un dossier non vide. Usage : {"tool": "delete_directory", "path": "<chemin>", "recursive": false}
- rename_file : Renommer ou déplacer un fichier ou dossier. Usage : {"tool": "rename_file", "old_path": "<ancien>", "new_path": "<nouveau>"}
- copy_file : Copier un fichier. Usage : {"tool": "copy_file", "src": "<src>", "dst": "<dst>"}
- get_file_info : Taille, permissions, propriétaire et dates. Usage : {"tool": "get_file_info", "path": "<chemin>"}
- search_files : Rechercher des fichiers récursivement par motif glob. Usage : {"tool": "search_files", "directory": "<dossier>", "pattern": "*.py"}

Éditeur de code :
- open_in_editor : Ouvrir un fichier dans l'éditeur préféré de l'utilisateur (VS Code, gedit, kate…). Usage : {"tool": "open_in_editor", "path": "<chemin>"}
- edit_file_lines : Remplacer une plage de lignes (1-indexé, inclus) dans un fichier. Usage : {"tool": "edit_file_lines", "path": "<chemin>", "start_line": 10, "end_line": 12, "new_content": "<remplacement>"}

Navigateur (pilote le VRAI navigateur visible — Chromium/Brave via CDP, ou Firefox via WebDriver BiDi — la page que voit l'utilisateur ; les cookies/sessions persistent) :
- navigate : Aller à une URL dans le vrai navigateur. Lance Brave/Chromium avec le port de debug au premier appel (bascule sur Firefox via BiDi si aucun navigateur Chromium n'est installé). Attend automatiquement que le DOM de la page soit prêt — ne jamais attendre manuellement après une navigation. Usage : {"tool": "navigate", "url": "<url>"}
- click : Cliquer sur un élément par sélecteur CSS. Usage : {"tool": "click", "selector": "<selecteur_css>"}
- find_and_click : Cliquer sur le premier élément dont le texte visible correspond. À utiliser quand tu ne connais pas le sélecteur mais que tu vois le libellé. Usage : {"tool": "find_and_click", "text": "<texte_visible>"}
- fill : Définir la valeur d'un champ en une fois (sans événements clavier). Usage : {"tool": "fill", "selector": "<selecteur_css>", "value": "<texte>"}
- type_text : Focaliser un élément et taper caractère par caractère via le clavier. Pour les champs de recherche / formulaires JS. Usage : {"tool": "type_text", "selector": "<selecteur_css>", "text": "<texte>"}
- press_key : Appuyer sur une touche du clavier sur la page active (Enter, Escape, Tab, ArrowDown, Backspace, « Control+A »…). Usage : {"tool": "press_key", "key": "Enter"}
- read_page : Texte visible de la page actuelle (tronqué). Usage : {"tool": "read_page"}
- get_page_text : Texte visible complet de la page (limite plus haute que read_page). Usage : {"tool": "get_page_text"}
- get_current_url : Retourner l'URL de la page active. Usage : {"tool": "get_current_url"}
- scroll : Faire défiler d'un nombre de pixels dans une direction. Usage : {"tool": "scroll", "direction": "down", "amount": 600}
- scroll_down : Descendre d'une hauteur d'écran (ou de ``amount`` pixels). Usage : {"tool": "scroll_down"}
- go_back : Reculer dans l'historique. Usage : {"tool": "go_back"}
- go_forward : Avancer dans l'historique. Usage : {"tool": "go_forward"}
- open_new_tab : Ouvrir une URL dans un nouvel onglet et basculer dessus. Usage : {"tool": "open_new_tab", "url": "<url>"}
- open_browser : Ouvrir un navigateur PRÉCIS par son nom (firefox, chrome, chromium, opera, edge, brave), éventuellement sur une URL. À utiliser quand l'utilisateur demande un navigateur particulier (« ouvre Firefox »). Firefox est ENTIÈREMENT pris en charge pour l'automatisation via WebDriver BiDi : après l'avoir ouvert, tous les outils navigateur (navigate, click, fill, type_text, read_page, captures d'écran…) fonctionnent dans Firefox exactement comme avec Brave/Chrome. Usage : {"tool": "open_browser", "name": "firefox", "url": "<url_optionnelle>"}

## Comment utiliser les outils
Pour appeler un outil, produis un appel JSON dans des balises <tool_call> :

<tool_call>
{"tool": "open_application", "name": "spotify"}
</tool_call>

Après chaque appel, tu reçois le résultat de l'outil. Tu peux enchaîner plusieurs appels.
Quand tu as toutes les informations nécessaires, donne ta réponse finale en texte simple (sans balises <tool_call>).
Quand tous les outils ont terminé, tu DOIS écrire une réponse finale complète basée sur tout ce que tu as trouvé — ne termine jamais ton tour avec seulement des appels d'outils, du balisage d'outil ou une réponse vide."""


def get_desktop_path() -> str:
    """Resolve the user's desktop directory for this machine.

    French Linux systems name it ``~/Bureau`` and English ones
    ``~/Desktop``; check those in order and fall back to the home
    directory if neither exists, so a resolved path never points at a
    missing folder.
    """
    home = os.path.expanduser("~")
    for name in ("Bureau", "Desktop"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return home


def get_system_prompt(lang: str = None, model: str = None, provider: str = None) -> str:
    """Return the system prompt for the given language, with the active
    model and provider injected at the start so the agent knows what it's
    running on (and can answer if asked).
    """
    if lang is None:
        lang = DEFAULT_LANG
    body = SYSTEM_PROMPT_FR if lang == "fr" else SYSTEM_PROMPT_EN
    # Point the model at the real project root on this machine instead of the
    # /home/OSIA path baked into the prompt text (works on Windows + Linux).
    body = body.replace("/home/OSIA/code/osia-project", _PROJECT_DIR)
    if model and provider:
        header = f"Tu es OSIA. Tu tournes sur {model} ({provider}).\n\n"
        body = header + body
    # Inject the resolved desktop path so the model always targets the
    # correct folder when the user says "desktop"/"bureau" (~/Bureau on
    # French systems, ~/Desktop on English ones).
    body = body + f"\n\n[DESKTOP PATH: {get_desktop_path()}]"
    # Bloc 6 — prepend the user's custom personality (~/.osia/personality.md)
    # and append any enabled skill files (~/.osia/skills/*.md). Both are
    # optional Markdown the user edits from the "OSIA" tab; any failure here
    # must never break prompt construction, so fall back silently.
    try:
        from osia import personality
        persona = personality.load_personality().strip()
        if persona:
            body = persona + "\n\n" + body
        skills = personality.load_enabled_skills_text().strip()
        if skills:
            body = body + "\n\n" + skills
    except Exception:
        pass
    return body


# Default system prompt
SYSTEM_PROMPT = get_system_prompt()


# ---------------------------------------------------------------------------
# PHASE 4: Response cleaning
# ---------------------------------------------------------------------------

# Internal reasoning blocks emitted by some models (Qwen 3 32B, DeepSeek-R1…).
# Their content is chain-of-thought — the user must never see or hear it.
_THINK_BLOCK_PATTERNS = [
    re.compile(r'<think>.*?</think>', re.DOTALL),
    re.compile(r'<thinking>.*?</thinking>', re.DOTALL),
]
# An unclosed reasoning block (stream cut off / max_tokens truncation):
# everything from the opening tag to the end of the text is reasoning.
_UNCLOSED_THINK_PATTERN = re.compile(r'<think(?:ing)?>.*$', re.DOTALL)
# Raw tool/function XML tags that some models (Groq's Llama/Qwen) output as
# literal text in their responses instead of real tool calls. Longest
# alternatives first so e.g. "tool_call" is never half-matched as "tool".
_RAW_TOOL_TAG_PATTERN = re.compile(
    r'</?(?:tool_call|tool_result|tool_code|function_calls|thinking|think|tool)>'
)
# Llama-family special tokens that Llama 4 Scout cites as literal text:
# <|python_tag|>, <|eot_id|>, <|start_header_id|>, <|end_header_id|>,
# <|eom_id|>, … A header block (<|start_header_id|>role<|end_header_id|>)
# is stripped whole — the role name between the tokens is markup too. The
# generic pattern then removes ANY remaining <|...|> token so new/unknown
# special tokens never reach the user.
_LLAMA_HEADER_BLOCK_PATTERN = re.compile(
    r'<\|start_header_id\|>.*?<\|end_header_id\|>', re.DOTALL
)
# <|python_tag|> marks Llama tool-call payload: everything from the tag to
# the next <|eot_id|>/<|eom_id|> (or end of text) is code/JSON the model
# meant to execute — like a <tool_call> block, it is stripped whole so the
# raw payload never reaches the user.
_LLAMA_PYTHON_TAG_BLOCK_PATTERN = re.compile(
    r'<\|python_tag\|>.*?(?:<\|eo[tm]_id\|>|$)', re.DOTALL
)
_LLAMA_SPECIAL_TOKEN_PATTERN = re.compile(r'<\|[^<>|]*\|>')
# Full tool blocks — tags AND the JSON/result payload between them. Some
# models echo entire <tool_call>{"tool": ...}</tool_call> blocks as literal
# text; stripping only the tags would leave the raw JSON visible to the user.
_TOOL_BLOCK_PATTERNS = [
    re.compile(r'<tool_call>.*?</tool_call>', re.DOTALL),
    re.compile(r'<tool_result>.*?</tool_result>', re.DOTALL),
    # Gemini's Python-code tool-call format (<tool_code>print(open_browser(...))
    # </tool_code>) — strip the whole block so the raw Python never reaches
    # display/TTS. Parsing happens before this, in _parse_tool_code_tool_calls.
    re.compile(r'<tool_code>.*?</tool_code>', re.DOTALL),
]
# An unclosed <tool_call> (stream cut off / max_tokens truncation): everything
# from the opening tag to the end of the text is tool markup.
_UNCLOSED_TOOL_CALL_PATTERN = re.compile(r'<tool_call>.*$', re.DOTALL)


def strip_reasoning_blocks(text: str) -> str:
    """Remove <think>…</think> / <thinking>…</thinking> reasoning blocks.

    An unclosed block (truncated response) is stripped to the end of the
    text, and orphaned closing tags are removed by ``strip_raw_tool_tags``.
    """
    if not text:
        return text
    for pattern in _THINK_BLOCK_PATTERNS:
        text = pattern.sub('', text)
    text = _UNCLOSED_THINK_PATTERN.sub('', text)
    return text


def strip_raw_tool_tags(text: str) -> str:
    """Remove raw tool/function XML markup cited as literal text.

    Strips, in order:
    1. Entire ``<tool_call>…</tool_call>`` / ``<tool_result>…</tool_result>``
       blocks INCLUDING the JSON/result payload between the tags — some
       models echo full tool-call blocks as literal text.
    2. An unclosed ``<tool_call>…`` running to the end of the text
       (stream truncation).
    3. Llama-family special tokens (Llama 4 Scout): header blocks
       ``<|start_header_id|>…<|end_header_id|>`` including the role name,
       ``<|python_tag|>…<|eot_id|>`` blocks including the tool payload,
       then any other ``<|...|>`` token (<|eot_id|>, <|eom_id|>, …).
    4. Any remaining orphan tags: <tool_call>, </tool_call>, <tool>,
       </tool>, <function_calls>, </function_calls>, …
    """
    if not text:
        return text
    for pattern in _TOOL_BLOCK_PATTERNS:
        text = pattern.sub('', text)
    text = _UNCLOSED_TOOL_CALL_PATTERN.sub('', text)
    text = _LLAMA_HEADER_BLOCK_PATTERN.sub('', text)
    text = _LLAMA_PYTHON_TAG_BLOCK_PATTERN.sub('', text)
    text = _LLAMA_SPECIAL_TOKEN_PATTERN.sub('', text)
    return _RAW_TOOL_TAG_PATTERN.sub('', text)


def clean_response(text: str) -> str:
    """Post-process agent response to remove tool mentions, markdown, and code.

    - Removes <tool_call> and <tool_result> blocks first
    - Removes <think>/<thinking> internal reasoning blocks
    - Strips raw tool/function XML tags cited as literal text
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

    # Step 1b: Remove internal reasoning blocks, then strip any raw
    # tool/function tags the model output as literal text.
    text = strip_reasoning_blocks(text)
    text = strip_raw_tool_tags(text)

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
            timeout=30, cwd=_PROJECT_DIR
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
        path = os.path.join(_PROJECT_DIR, path)
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
        path = os.path.join(_PROJECT_DIR, path)
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
        path = os.path.join(_PROJECT_DIR, path)
    try:
        entries = os.listdir(path)
        entries.sort()
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"ERROR listing {path}: {e}"


def tool_delete_file(path: str) -> str:
    """Delete a file."""
    if not path.startswith("/"):
        path = os.path.join(_PROJECT_DIR, path)
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
    path = str(Path(tempfile.gettempdir()) / "osia_screenshot.png")
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


# ── Path helper (resolves relative paths against the dynamic _PROJECT_DIR) ──


def _abs(path: str) -> str:
    """Resolve a possibly-relative path against the project directory."""
    return path if path.startswith("/") else os.path.join(_PROJECT_DIR, path)


# ── File-system tools ──────────────────────────────────────────────────────

def tool_create_directory(path: str) -> str:
    """Create a directory (and any missing parents)."""
    path = _abs(path)
    try:
        os.makedirs(path, exist_ok=True)
        return f"Created directory: {path}"
    except Exception as e:
        return f"ERROR creating directory {path}: {e}"


def tool_delete_directory(path: str, recursive: bool = False) -> str:
    """Delete a directory. With ``recursive=True`` also removes contents;
    otherwise the directory must be empty.
    """
    path = _abs(path)
    try:
        if recursive:
            shutil.rmtree(path)
            return f"Recursively deleted directory: {path}"
        os.rmdir(path)
        return f"Deleted (empty) directory: {path}"
    except FileNotFoundError:
        return f"ERROR: {path} does not exist"
    except OSError as e:
        return f"ERROR deleting {path}: {e} (use recursive=true to remove non-empty dirs)"
    except Exception as e:
        return f"ERROR deleting {path}: {e}"


def tool_rename_file(old_path: str, new_path: str) -> str:
    """Rename or move a file or directory."""
    src = _abs(old_path)
    dst = _abs(new_path)
    try:
        os.rename(src, dst)
        return f"Renamed: {src} -> {dst}"
    except Exception as e:
        return f"ERROR renaming {src} -> {dst}: {e}"


def tool_copy_file(src: str, dst: str) -> str:
    """Copy a file. If ``dst`` is an existing directory, the file is
    copied into it preserving the source filename.
    """
    src_abs = _abs(src)
    dst_abs = _abs(dst)
    try:
        result = shutil.copy2(src_abs, dst_abs)
        return f"Copied: {src_abs} -> {result}"
    except IsADirectoryError:
        return f"ERROR: source {src_abs} is a directory; this tool copies files only"
    except Exception as e:
        return f"ERROR copying {src_abs} -> {dst_abs}: {e}"


def tool_get_file_info(path: str) -> str:
    """Return size, permissions, ownership, and timestamps for a file or directory."""
    path = _abs(path)
    try:
        st = os.stat(path)
        kind = (
            "directory" if stat.S_ISDIR(st.st_mode)
            else "symlink" if stat.S_ISLNK(st.st_mode)
            else "file"
        )
        mode = stat.filemode(st.st_mode)
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        ctime = datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
        atime = datetime.fromtimestamp(st.st_atime).strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"path:        {path}\n"
            f"type:        {kind}\n"
            f"size:        {size} bytes\n"
            f"permissions: {mode} ({oct(st.st_mode & 0o777)})\n"
            f"owner uid:   {st.st_uid}\n"
            f"group gid:   {st.st_gid}\n"
            f"modified:    {mtime}\n"
            f"changed:     {ctime}\n"
            f"accessed:    {atime}"
        )
    except FileNotFoundError:
        return f"ERROR: {path} does not exist"
    except Exception as e:
        return f"ERROR getting info for {path}: {e}"


def tool_search_files(directory: str, pattern: str) -> str:
    """Recursively find files in ``directory`` whose name matches ``pattern``
    (glob syntax, e.g. ``*.py``, ``test_*.txt``). Returns up to 200 hits.
    """
    directory = _abs(directory)
    if not os.path.isdir(directory):
        return f"ERROR: {directory} is not a directory"
    matches: list[str] = []
    try:
        for root, _dirs, files in os.walk(directory):
            for name in files:
                if fnmatch.fnmatch(name, pattern):
                    matches.append(os.path.join(root, name))
                    if len(matches) >= 200:
                        break
            if len(matches) >= 200:
                break
    except Exception as e:
        return f"ERROR searching {directory}: {e}"
    if not matches:
        return f"(no files matching {pattern!r} under {directory})"
    suffix = "\n... [TRUNCATED at 200 hits]" if len(matches) >= 200 else ""
    return "\n".join(matches) + suffix


# ── Code-editor tools ──────────────────────────────────────────────────────

_EDITOR_CANDIDATES = ["code", "codium", "code-oss", "gedit", "kate", "xed", "nano"]


def _detect_editor() -> str | None:
    for name in _EDITOR_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return shutil.which("xdg-open")


def tool_open_in_editor(path: str) -> str:
    """Open a file in the user's preferred editor (VS Code, gedit, kate,
    xed, nano…). Falls back to ``xdg-open``.
    """
    path = _abs(path)
    editor = _detect_editor()
    if not editor:
        return "ERROR: no known editor or xdg-open available"
    try:
        subprocess.Popen(
            [editor, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return f"Opened {path} in {os.path.basename(editor)}"
    except Exception as e:
        return f"ERROR opening {path} in {editor}: {e}"


def tool_edit_file_lines(path: str, start_line: int, end_line: int, new_content: str) -> str:
    """Replace lines ``start_line`` through ``end_line`` (1-indexed,
    inclusive) in ``path`` with ``new_content``. The replacement may have
    a different number of lines than the original range.
    """
    path = _abs(path)
    try:
        start = int(start_line)
        end = int(end_line)
    except (TypeError, ValueError):
        return f"ERROR: start_line and end_line must be integers"
    if start < 1 or end < start:
        return f"ERROR: invalid range start_line={start}, end_line={end}"
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except Exception as e:
        return f"ERROR reading {path}: {e}"
    if start > len(lines):
        return f"ERROR: start_line {start} is past end of file ({len(lines)} lines)"
    end = min(end, len(lines))
    replacement = new_content if new_content.endswith("\n") else new_content + "\n"
    new_lines = lines[:start - 1] + [replacement] + lines[end:]
    try:
        with open(path, "w") as f:
            f.writelines(new_lines)
    except Exception as e:
        return f"ERROR writing {path}: {e}"
    return f"Replaced lines {start}-{end} of {path} ({end - start + 1} -> {replacement.count(chr(10))} lines)"


# ── System tools ───────────────────────────────────────────────────────────

def tool_get_system_info() -> str:
    """Return a snapshot of CPU, RAM and disk usage."""
    try:
        cpu_pct = psutil.cpu_percent(interval=0.3)
        cores = psutil.cpu_count(logical=True)
        try:
            load1, load5, load15 = os.getloadavg()
            load_str = f"{load1:.2f} / {load5:.2f} / {load15:.2f}"
        except AttributeError:
            load_str = "N/A"  # os.getloadavg() is Unix-only (absent on Windows)
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        uname = platform.uname()
        return (
            f"host:    {uname.node} ({uname.system} {uname.release})\n"
            f"cpu:     {cpu_pct:.1f}% across {cores} cores  (load 1/5/15: {load_str})\n"
            f"memory:  {vm.used / 1024**3:.1f} / {vm.total / 1024**3:.1f} GiB used ({vm.percent:.0f}%)\n"
            f"disk /:  {du.used / 1024**3:.1f} / {du.total / 1024**3:.1f} GiB used ({du.percent:.0f}%)"
        )
    except Exception as e:
        return f"ERROR collecting system info: {e}"


def tool_list_running_processes(limit: int = 20) -> str:
    """List the top processes by CPU then memory usage. ``limit`` caps the
    number of entries returned (default 20).
    """
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 20
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "memory_percent"]):
        try:
            info = p.info
            info["cpu_percent"] = p.cpu_percent(interval=None)
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # Prime cpu_percent — first call returns 0.0 for everything.
    psutil.cpu_percent(interval=0.2)
    for p in psutil.process_iter():
        try:
            for entry in procs:
                if entry["pid"] == p.pid:
                    entry["cpu_percent"] = p.cpu_percent(interval=None)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: (x.get("cpu_percent") or 0.0, x.get("memory_percent") or 0.0), reverse=True)
    procs = procs[:limit]
    header = f"{'PID':>7}  {'CPU%':>5}  {'MEM%':>5}  {'USER':<12}  NAME"
    rows = [header]
    for p in procs:
        rows.append(
            f"{p['pid']:>7}  {p.get('cpu_percent') or 0:>5.1f}  "
            f"{p.get('memory_percent') or 0:>5.1f}  "
            f"{(p.get('username') or '-')[:12]:<12}  {p.get('name') or '-'}"
        )
    return "\n".join(rows)


def tool_kill_process(name_or_pid) -> str:
    """Terminate a process by PID (int or numeric string) or by process
    name (substring match). Sends SIGTERM. Returns a summary of what was
    killed.
    """
    target = str(name_or_pid).strip()
    if not target:
        return "ERROR: no PID or name given"
    if target.isdigit():
        pid = int(target)
        try:
            p = psutil.Process(pid)
            name = p.name()
            p.terminate()
            return f"Sent SIGTERM to PID {pid} ({name})"
        except psutil.NoSuchProcess:
            return f"ERROR: no process with PID {pid}"
        except psutil.AccessDenied:
            return f"ERROR: permission denied killing PID {pid}"
        except Exception as e:
            return f"ERROR killing PID {pid}: {e}"
    killed = []
    failed = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if target.lower() in (p.info["name"] or "").lower():
                p.terminate()
                killed.append(f"{p.info['pid']} ({p.info['name']})")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            failed.append(f"{p.info['pid']} ({p.info['name']})")
    if not killed and not failed:
        return f"No processes matched name {target!r}"
    parts = []
    if killed:
        parts.append(f"Killed {len(killed)}: " + ", ".join(killed))
    if failed:
        parts.append(f"Permission denied for {len(failed)}: " + ", ".join(failed))
    return " | ".join(parts)


def tool_set_volume(level) -> str:
    """Set system volume to ``level`` percent (0-100) on the default sink
    via pactl (works on PulseAudio and PipeWire).
    """
    try:
        pct = int(level)
    except (TypeError, ValueError):
        return f"ERROR: level must be an integer 0-100, got {level!r}"
    pct = max(0, min(100, pct))
    try:
        result = subprocess.run(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return f"ERROR setting volume: {result.stderr.strip() or 'pactl failed'}"
        return f"Volume set to {pct}%"
    except FileNotFoundError:
        return "ERROR: pactl is not installed"
    except Exception as e:
        return f"ERROR setting volume: {e}"


def _clipboard_backend() -> tuple[list[str], list[str]] | None:
    """Pick (read_cmd, write_cmd) based on what's available. Prefers
    Wayland (wl-paste/wl-copy) and falls back to xclip / xsel for X11.
    """
    if shutil.which("wl-paste") and shutil.which("wl-copy"):
        return (["wl-paste", "--no-newline"], ["wl-copy"])
    if shutil.which("xclip"):
        return (["xclip", "-selection", "clipboard", "-o"],
                ["xclip", "-selection", "clipboard", "-i"])
    if shutil.which("xsel"):
        return (["xsel", "--clipboard", "--output"],
                ["xsel", "--clipboard", "--input"])
    return None


def tool_get_clipboard() -> str:
    """Return the current clipboard text."""
    backend = _clipboard_backend()
    if backend is None:
        return "ERROR: no clipboard backend (install wl-clipboard, xclip, or xsel)"
    read_cmd, _ = backend
    try:
        result = subprocess.run(read_cmd, capture_output=True, text=True, timeout=3)
        if result.returncode != 0:
            return f"ERROR reading clipboard: {result.stderr.strip() or read_cmd[0] + ' failed'}"
        text = result.stdout
        if len(text) > 4000:
            text = text[:4000] + "\n... [TRUNCATED]"
        return text if text else "(clipboard is empty)"
    except Exception as e:
        return f"ERROR reading clipboard: {e}"


def tool_set_clipboard(text: str) -> str:
    """Place ``text`` on the clipboard."""
    backend = _clipboard_backend()
    if backend is None:
        return "ERROR: no clipboard backend (install wl-clipboard, xclip, or xsel)"
    _, write_cmd = backend
    try:
        result = subprocess.run(
            write_cmd, input=text, capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return f"ERROR writing clipboard: {result.stderr.strip() or write_cmd[0] + ' failed'}"
        return f"Copied {len(text)} chars to clipboard"
    except Exception as e:
        return f"ERROR writing clipboard: {e}"


# Browser tools are imported lazily to avoid startup overhead
_browser_tools = None

def _get_browser_tools():
    global _browser_tools
    if _browser_tools is None:
        from osia.tools.browser import (
            navigate, click, fill, read_page,
            get_current_url, scroll, go_back, go_forward, open_new_tab,
            find_and_click, wait_for_load, get_page_text, scroll_down,
            type_text, press_key, open_browser,
        )
        _browser_tools = {
            "navigate": navigate,
            "click": click,
            "fill": fill,
            "read_page": read_page,
            "get_current_url": get_current_url,
            "scroll": scroll,
            "go_back": go_back,
            "go_forward": go_forward,
            "open_new_tab": open_new_tab,
            "find_and_click": find_and_click,
            "wait_for_load": wait_for_load,
            "get_page_text": get_page_text,
            "scroll_down": scroll_down,
            "type_text": type_text,
            "press_key": press_key,
            "open_browser": open_browser,
        }
    return _browser_tools


def tool_navigate(url: str):
    return _get_browser_tools()["navigate"](url)

def tool_click(selector: str):
    return _get_browser_tools()["click"](selector)

def tool_fill(selector: str, value: str) -> str:
    return _get_browser_tools()["fill"](selector, value)

def tool_read_page() -> str:
    return _get_browser_tools()["read_page"]()

def tool_get_current_url() -> str:
    return _get_browser_tools()["get_current_url"]()

def tool_scroll(direction: str = "down", amount: int = 600) -> str:
    return _get_browser_tools()["scroll"](direction, amount)

def tool_go_back():
    return _get_browser_tools()["go_back"]()

def tool_go_forward():
    return _get_browser_tools()["go_forward"]()

def tool_open_new_tab(url: str) -> str:
    return _get_browser_tools()["open_new_tab"](url)

def tool_find_and_click(text: str):
    return _get_browser_tools()["find_and_click"](text)

def tool_wait_for_load() -> str:
    return _get_browser_tools()["wait_for_load"]()

def tool_get_page_text() -> str:
    return _get_browser_tools()["get_page_text"]()

def tool_scroll_down(amount: int | None = None) -> str:
    return _get_browser_tools()["scroll_down"](amount)

def tool_type_text(selector: str, text: str) -> str:
    return _get_browser_tools()["type_text"](selector, text)

def tool_press_key(key: str) -> str:
    return _get_browser_tools()["press_key"](key)

def tool_open_browser(name: str, url: str | None = None):
    # Returns a dict (text + screenshot) when Firefox is opened over BiDi,
    # a plain string for regular application launches.
    return _get_browser_tools()["open_browser"](name, url)


TOOL_MAP = {
    # Shell & launchers
    "bash": tool_bash,
    "run_command": tool_run_command,
    "open_application": tool_open_application,
    "take_screenshot": tool_take_screenshot,
    # Files
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "list_files": tool_list_files,
    "delete_file": tool_delete_file,
    "create_directory": tool_create_directory,
    "delete_directory": tool_delete_directory,
    "rename_file": tool_rename_file,
    "copy_file": tool_copy_file,
    "get_file_info": tool_get_file_info,
    "search_files": tool_search_files,
    # Code editor
    "open_in_editor": tool_open_in_editor,
    "edit_file_lines": tool_edit_file_lines,
    # System
    "get_system_info": tool_get_system_info,
    "list_running_processes": tool_list_running_processes,
    "kill_process": tool_kill_process,
    "set_volume": tool_set_volume,
    "get_clipboard": tool_get_clipboard,
    "set_clipboard": tool_set_clipboard,
    # Browser
    "navigate": tool_navigate,
    "click": tool_click,
    "fill": tool_fill,
    "read_page": tool_read_page,
    "get_current_url": tool_get_current_url,
    "scroll": tool_scroll,
    "go_back": tool_go_back,
    "go_forward": tool_go_forward,
    "open_new_tab": tool_open_new_tab,
    "find_and_click": tool_find_and_click,
    "wait_for_load": tool_wait_for_load,
    "get_page_text": tool_get_page_text,
    "scroll_down": tool_scroll_down,
    "type_text": tool_type_text,
    "press_key": tool_press_key,
    "open_browser": tool_open_browser,
}

# Known tool names — bare-JSON candidates (priority 3 of the universal
# parser) are validated against this set so arbitrary JSON the model shows
# in prose (code examples, config snippets…) is never executed as a tool.
KNOWN_TOOL_NAMES = set(TOOL_MAP.keys())


# ---------------------------------------------------------------------------
# Tool execution: hard wall-clock timeout + retry-once-on-error
# ---------------------------------------------------------------------------

TOOL_TIMEOUT_SECONDS = 30
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="osia-tool")

_ERROR_PREFIXES = ("ERROR", "ERREUR", "[ERROR]", "[ERREUR]")


def _result_text(result) -> str:
    """Extract the text part of a tool result. Browser navigation tools
    return ``{"text": ..., "screenshot": ...}``; everything else returns
    a plain string.
    """
    if isinstance(result, dict):
        return str(result.get("text", ""))
    return str(result) if result is not None else ""


def _is_tool_error(result) -> bool:
    """A tool result is treated as a failure when its text is empty/whitespace
    or its first non-blank token begins with ERROR/ERREUR (with or
    without brackets). Sentinels like ``(no output)`` or ``(empty
    directory)`` are *not* errors — they're legitimate empty signals.
    Browser tools return a dict with a ``text`` field; we inspect that.
    """
    text = _result_text(result)
    s = text.strip()
    if not s:
        # Dicts carrying a screenshot but no text are still valid signals.
        return not (isinstance(result, dict) and result.get("screenshot"))
    upper = s.upper()
    return any(upper.startswith(p) for p in _ERROR_PREFIXES)


def _execute_tool_once(tool_name: str, kwargs: dict):
    """Run one tool with a 30 s wall-clock timeout. Returns the result
    (a string, or a ``{"text", "screenshot"}`` dict for navigation
    tools), or a clear [ERREUR] line on timeout/exception so the model
    can read the failure back and report it honestly. Submitting through
    a ThreadPoolExecutor means the surrounding agent loop can never be
    wedged by a stuck synchronous tool.
    """
    fn = TOOL_MAP[tool_name]
    try:
        fut = _TOOL_EXECUTOR.submit(fn, **kwargs)
        result = fut.result(timeout=TOOL_TIMEOUT_SECONDS)
        # Preserve dict results (browser tools) so screenshots survive.
        if isinstance(result, dict):
            return result
        return str(result)
    except FutureTimeoutError:
        return (
            f"[ERREUR] L'outil '{tool_name}' n'a pas répondu après "
            f"{TOOL_TIMEOUT_SECONDS}s (timeout). L'action n'a pas été confirmée."
        )
    except Exception as e:
        return f"[ERREUR] '{tool_name}' a échoué: {e}"


def _execute_tool_with_retry(tool_name: str, kwargs: dict):
    """Execute a tool and retry exactly once if the first attempt
    reports an error (or returns empty). Returns ``(result, is_error)``
    where ``is_error`` reflects the *final* attempt — callers use it to
    decide whether to keep dispatching the rest of a tool-call batch.
    """
    result = _execute_tool_once(tool_name, kwargs)
    if _is_tool_error(result):
        preview = _result_text(result).replace("\n", " ")[:120]
        print(f"[OSIA Agent] ↻ retry tool: {tool_name} (first attempt: {preview!r})")
        result = _execute_tool_once(tool_name, kwargs)
    return result, _is_tool_error(result)


def _provider_supports_vision(provider: str, model: str) -> bool:
    """Return True only for models that accept OpenAI vision-style content
    arrays (list of ``{"type": "text"|"image_url", ...}`` parts) in message
    content: Gemini and GPT-4o variants.

    Every other provider/model (DeepSeek, Groq Llama/Qwen, xAI, Ollama, …)
    only accepts plain strings as message content — sending a list raises
    ``400 messages[N].content must be a string`` and cuts the response.
    """
    provider_l = (provider or "").lower()
    model_l = (model or "").lower()
    if provider_l == "google" or "gemini" in model_l:
        return True
    if "gpt-4o" in model_l:
        return True
    return False


# Mandatory instruction prepended to every navigation tool result (navigate,
# click, find_and_click) that carries a page screenshot the model will
# actually receive (vision-capable provider). Forces the model to ground its
# next action in what the page really shows instead of guessing selectors.
SCREENSHOT_ANALYSIS_INSTRUCTION = (
    "IMPORTANT: Analyze the screenshot carefully before taking any action. "
    "Identify the exact position of buttons, links, and elements visible on "
    "screen. Base your next action on what you actually see in the image."
)


# ---------------------------------------------------------------------------
# Universal tool-call parser — all providers, all formats
# ---------------------------------------------------------------------------

# Bare JSON tool-call object cited directly in prose (no wrapping tags):
# {"tool": "name", "param": "value"}. Matches flat (non-nested) objects;
# nested-``parameters`` payloads are recovered by the anchored balanced-brace
# pass in ``_parse_bare_json_tool_calls``.
_BARE_JSON_TOOL_PATTERN = re.compile(
    r'\{[^{}]*?"tool"\s*:\s*"[^"]+?"[^{}]*?\}', re.DOTALL
)

# Llama-family python-tag payload: <|python_tag|>{...}<|eot_id|>. The JSON
# between the special tokens is a tool call (same shapes as the XML format);
# an unterminated payload (max_tokens truncation) runs to end of text.
_PYTHON_TAG_CALL_PATTERN = re.compile(
    r'<\|python_tag\|>\s*(.*?)\s*(?:<\|eo[tm]_id\|>|$)', re.DOTALL
)

# Gemini's Python-code tool-call format: a <tool_code>...</tool_code> block
# whose body is Python like ``print(open_browser(name="brave", url="..."))``
# instead of the <tool_call> JSON contract. Parsed with ast (never eval/exec)
# by ``_parse_tool_code_tool_calls``.
_TOOL_CODE_PATTERN = re.compile(r'<tool_code>(.*?)</tool_code>', re.DOTALL)


def _extract_json_object(raw: str) -> str | None:
    """Return the first balanced ``{...}`` object in ``raw`` (string-aware),
    or ``None`` if no balanced object exists (e.g. truncated payload).
    """
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(raw)):
        c = raw[i]
        if escaped:
            escaped = False
            continue
        if c == "\\":
            escaped = in_str
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


def _is_isolated_span(text: str, start: int, end: int) -> bool:
    """True when ``text[start:end]`` stands alone on its line(s): nothing
    but whitespace before it on its first line and after it on its last
    line.

    Bare ``{"tool": ...}`` JSON only counts as a tool call / tool markup
    when isolated like this. JSON embedded inside a sentence (the system
    prompt's own ``Usage: {"tool": "bash", ...}`` examples, the model
    quoting or explaining the format…) is prose and must never be executed
    or stripped.
    """
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        return False
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    return not text[end:line_end].strip()


# A "full sentence" of prose: a letter starting a clause of 10+ characters
# that ends with sentence punctuation. Used to tell a conversational answer
# apart from a tool-call preamble.
_PROSE_SENTENCE_PATTERN = re.compile(
    r"[A-Za-zÀ-ÿ][^.!?\n]{10,}[.!?](?:\s|$)"
)


def _reads_as_prose(text: str) -> bool:
    """Heuristic: does ``text`` read like a normal conversational response
    rather than a tool-call preamble? Two or more full sentences qualify.

    A single short preamble ("I'll check that now.") followed by a tool
    call is NOT prose; a multi-sentence answer that happens to quote tool
    JSON is.
    """
    return len(_PROSE_SENTENCE_PATTERN.findall(text or "")) >= 2


def _parse_tool_json(raw: str) -> dict | None:
    """Parse one tool-call JSON payload into ``{"tool": name, **kwargs}``.

    Accepts every shape models actually produce:
    - flat:     {"tool": "bash", "command": "ls"}
    - nested:   {"tool": "bash", "parameters": {"command": "ls"}}
                {"name": "bash", "arguments": {"command": "ls"}}
    - native:   {"function": {"name": "bash", "arguments": "{...}"}}
    - payloads with trailing prose, or truncated by max_tokens (repaired
      via balanced-brace extraction / closing-brace completion).

    No size limit — payloads of any length parse (a full HTML file in a
    write_file call, …). All parsing is ``strict=False`` so literal
    newlines/tabs inside string values (how models actually emit
    multi-line file content) are accepted instead of rejected.

    Returns ``None`` when no tool name can be recovered.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    data = None
    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        # Repair 1: extract the first balanced {...} (drops trailing prose).
        candidate = _extract_json_object(raw)
        if candidate:
            try:
                data = json.loads(candidate, strict=False)
            except json.JSONDecodeError:
                data = None
        # Repair 2: truncated payload — try completing the JSON.
        if data is None:
            for suffix in ('"}', '"}}', "}", "}}"):
                try:
                    data = json.loads(raw + suffix, strict=False)
                    break
                except json.JSONDecodeError:
                    continue
    if not isinstance(data, dict):
        return None

    kwargs: dict = {}
    if isinstance(data.get("function"), dict):
        # OpenAI-native-style nesting cited as text.
        fn = data["function"]
        name = fn.get("name")
        raw_params = fn.get("arguments") or fn.get("parameters") or {}
    else:
        # Tool name: "tool" wins; "function"/"name" only when "tool" is
        # absent ("name" can legitimately be an argument of the call, e.g.
        # open_application / open_browser).
        name, name_key = None, None
        for key in ("tool", "function", "name"):
            value = data.get(key)
            if isinstance(value, str) and value:
                name, name_key = value, key
                break
        if name is None:
            return None
        raw_params = data.get("parameters") or data.get("arguments") or {}
        kwargs = {
            k: v for k, v in data.items()
            if k not in (name_key, "parameters", "arguments")
        }

    if not name or not isinstance(name, str):
        return None
    # Native "arguments" may itself be a JSON-encoded string.
    if isinstance(raw_params, str):
        try:
            raw_params = json.loads(raw_params, strict=False)
        except json.JSONDecodeError:
            raw_params = {}
    if isinstance(raw_params, dict):
        kwargs.update(raw_params)
    return {"tool": name, **kwargs}


def _parse_xml_tool_calls(text: str) -> list:
    """Parse ``<tool_call>{...}</tool_call>`` blocks (Groq Llama/Qwen
    format) from text, including an unclosed trailing block left by
    max_tokens truncation.

    Tag boundaries are located with plain string search and the payload
    between them is sliced out wholesale, then handed to
    ``_parse_tool_json`` (``json.loads`` + repair logic) — no regex, so
    arbitrarily large payloads (a full HTML file in a write_file call,
    nested braces, multi-line content…) parse without any pattern-size or
    backtracking limit.
    """
    if not text:
        return []
    calls = []
    open_tag, close_tag = "<tool_call>", "</tool_call>"
    pos = 0
    while True:
        start = text.find(open_tag, pos)
        if start == -1:
            break
        payload_start = start + len(open_tag)
        end = text.find(close_tag, payload_start)
        if end == -1:
            # Unclosed trailing block (max_tokens truncation): everything
            # after the opening tag is the payload — _parse_tool_json's
            # repair logic completes the truncated JSON.
            call = _parse_tool_json(text[payload_start:])
            if call:
                calls.append(call)
            break
        call = _parse_tool_json(text[payload_start:end])
        if call:
            calls.append(call)
        pos = end + len(close_tag)
    return calls


def _parse_native_tool_calls(message) -> list:
    """Convert native structured tool calls into ``{"tool": name, **kwargs}``
    dicts. Returns ``[]`` when the message carries no structured calls.

    Handles both shapes the SDKs produce:
    - ``message.tool_calls`` (OpenAI/Anthropic/DeepSeek/Google/Ollama/xAI
      OpenAI-compatible APIs) — function-calling entries;
    - content blocks with ``type="tool_use"`` (Anthropic Messages API) —
      blocks carrying ``name`` + ``input``.

    Both are already parsed by the SDK; this only normalizes them.
    """
    if message is None:
        return []
    calls = []

    # Shape 1: OpenAI-style message.tool_calls.
    tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls and isinstance(message, dict):
        tool_calls = message.get("tool_calls")
    for tc in tool_calls or []:
        fn = getattr(tc, "function", None)
        if fn is None and isinstance(tc, dict):
            fn = tc.get("function")
        if fn is None:
            continue
        name = getattr(fn, "name", None)
        args = getattr(fn, "arguments", None)
        if isinstance(fn, dict):
            name = name or fn.get("name")
            args = args if args is not None else fn.get("arguments")
        if not name:
            continue
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append({"tool": name, **(args if isinstance(args, dict) else {})})

    # Shape 2: Anthropic-style content blocks with type="tool_use".
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                btype, name, args = block.get("type"), block.get("name"), block.get("input")
            else:
                btype = getattr(block, "type", None)
                name = getattr(block, "name", None)
                args = getattr(block, "input", None)
            if btype != "tool_use" or not name:
                continue
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"tool": name, **(args if isinstance(args, dict) else {})})

    return calls


def _parse_bare_json_tool_calls(text: str) -> list:
    """Parse bare JSON tool-call objects cited directly in text (no
    wrapping tags): ``{"tool": "name", "param": "value"}``.

    STRICT BY DESIGN — bare JSON is the most false-positive-prone format
    (any prose can quote tool JSON, and the system prompt itself contains
    inline ``Usage: {"tool": ...}`` examples the model may echo). A
    candidate object is only accepted as a call when ALL of:
    1. ``_parse_tool_json`` recovers a call from it — ``json.loads`` plus
       the repair logic (balanced-brace extraction, truncation completion,
       literal newlines in strings), so arbitrarily large payloads and
       payloads cut by max_tokens are not lost;
    2. it carries a literal ``"tool"`` key whose value exactly matches a
       key in ``KNOWN_TOOL_NAMES`` (``"name"``/``"function"`` aliases are
       NOT accepted for this format — both passes anchor on a literal
       ``"tool"`` key);
    3. it is isolated from prose: nothing but whitespace around it on its
       own line(s) (``_is_isolated_span``).

    Two passes, deduped by span:
    1. Flat objects via ``_BARE_JSON_TOOL_PATTERN``.
    2. Nested-``parameters`` / large / truncated payloads the flat regex
       can't span: anchor at the ``{`` preceding each ``"tool"`` key and
       extract the balanced object from there (balanced-brace walk — no
       regex size limit); a payload with no balanced close (max_tokens
       truncation) runs to the end of the text and is repaired by
       ``_parse_tool_json``.
    """
    if not text or '"tool"' not in text:
        return []
    found: list[tuple[int, int, dict]] = []

    def _try_add(raw: str, start: int, end: int) -> None:
        # Rule 3 — the object must stand alone, not sit inside a sentence.
        if not _is_isolated_span(text, start, end):
            return
        # Rules 1+2 — parse (with large-payload/truncation repairs) and
        # require the literal "tool" key to name a known tool.
        call = _parse_tool_json(raw)
        if not call or call.get("tool") not in KNOWN_TOOL_NAMES:
            return
        for s, e, _ in found:
            if start < e and end > s:  # overlaps an already-captured span
                return
        found.append((start, end, call))

    # Pass 1 — flat bare objects.
    for m in _BARE_JSON_TOOL_PATTERN.finditer(text):
        _try_add(m.group(0), m.start(), m.end())

    # Pass 2 — nested/large payloads via anchored balanced-brace extraction.
    for m in re.finditer(r'"tool"\s*:\s*"', text):
        start = text.rfind("{", 0, m.start())
        if start == -1:
            continue
        candidate = _extract_json_object(text[start:])
        if candidate:
            _try_add(candidate, start, start + len(candidate))
        else:
            # No balanced close — payload truncated by max_tokens. Hand
            # everything from the anchor to the end of the text to
            # _parse_tool_json's repair logic (closing-brace completion).
            _try_add(text[start:], start, len(text))

    found.sort(key=lambda item: item[0])
    return [call for _, _, call in found]


def _parse_python_tag_tool_calls(text: str) -> list:
    """Parse Llama-family ``<|python_tag|>{...}<|eot_id|>`` payloads
    (Llama 4 Scout & co) into tool calls. The JSON between the special
    tokens accepts the same shapes as the XML format, including payloads
    truncated by max_tokens.
    """
    if not text or "<|python_tag|>" not in text:
        return []
    calls = []
    for m in _PYTHON_TAG_CALL_PATTERN.finditer(text):
        call = _parse_tool_json(m.group(1))
        if call:
            calls.append(call)
    return calls


def _parse_tool_code_tool_calls(text: str) -> list:
    """Parse Gemini ``<tool_code>`` blocks of Python-style tool calls into
    ``{"tool": name, **kwargs}`` dicts.

    Gemini sometimes emits a tool call as executable Python rather than the
    ``<tool_call>`` JSON contract, e.g.::

        <tool_code>
        print(open_browser(name="brave", url="https://www.microsoft.com"))
        </tool_code>

    Each block is parsed with ``ast`` (NEVER ``eval``/``exec``). Every
    ``Call`` node whose function name matches a tool in ``KNOWN_TOOL_NAMES``
    becomes a call; keyword arguments are read as Python literals via
    ``ast.literal_eval`` (falling back to the argument's source text for a
    non-literal expression). A ``print(...)`` wrapper is ignored because
    ``print`` is not a known tool. Blocks that don't parse as Python — or
    name no known tool — yield nothing.
    """
    if not text or "<tool_code>" not in text:
        return []
    calls = []
    for block in _TOOL_CODE_PATTERN.findall(text):
        snippet = block.strip()
        if not snippet:
            continue
        try:
            tree = ast.parse(snippet)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            else:
                continue
            if name not in KNOWN_TOOL_NAMES:
                continue
            kwargs = {}
            for kw in node.keywords:
                if kw.arg is None:  # **kwargs splat — no name to map onto
                    continue
                try:
                    kwargs[kw.arg] = ast.literal_eval(kw.value)
                except (ValueError, SyntaxError, TypeError):
                    # Non-literal expression — keep its source as a string.
                    try:
                        kwargs[kw.arg] = ast.unparse(kw.value)
                    except Exception:
                        continue
            calls.append({"tool": name, **kwargs})
    return calls


def strip_bare_tool_json(text: str) -> str:
    """Remove bare ``{"tool": ...}`` JSON tool-call objects from display
    text. Only objects naming a KNOWN tool AND isolated on their own
    line(s) are removed — JSON the model legitimately shows the user
    inside prose (code examples, usage explanations, config snippets…)
    stays. Mirrors the strictness of ``_parse_bare_json_tool_calls`` so
    display stripping can never eat parts of a normal answer.
    """
    if not text or '"tool"' not in text:
        return text
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r'"tool"\s*:\s*"([^"]+)"', text):
        if m.group(1) not in KNOWN_TOOL_NAMES:
            continue
        start = text.rfind("{", 0, m.start())
        if start == -1:
            continue
        candidate = _extract_json_object(text[start:])
        # A truncated (never-balanced) object runs to the end of the text.
        end = start + len(candidate) if candidate else len(text)
        # Same isolation rule as the parser: an object embedded in a
        # sentence is prose the user should see, not markup to strip.
        if not _is_isolated_span(text, start, end):
            continue
        spans.append((start, end))
    if not spans:
        return text
    # Merge overlapping spans, then cut them out.
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    parts = []
    prev = 0
    for s, e in merged:
        parts.append(text[prev:s])
        prev = e
    parts.append(text[prev:])
    return "".join(parts)


def _looks_like_pure_markup(text: str) -> bool:
    """True when ``text`` is genuinely tool/reasoning markup that display
    surfaces must hide entirely: a reasoning block (closed or truncated),
    a ``<tool_call>``/``<tool_result>`` block, a Llama special-token
    payload, bare ``{"tool": ...}`` JSON, or the tool-result feedback
    instruction. Used by ``strip_tool_artifacts`` to tell "correctly hidden
    markup" apart from "normal response wrongly stripped to empty".
    """
    stripped = text.strip()
    if not stripped:
        return True
    # Reasoning or tool blocks (closed or truncated) at the start.
    if re.match(r'^<(?:think(?:ing)?|tool_call|tool_result)>', stripped):
        return True
    # Llama special tokens at the start (<|python_tag|>, <|start_header_id|>, …).
    if stripped.startswith("<|"):
        return True
    # Bare {"tool": ...} JSON naming a known tool, on its own line.
    if has_tool_markup(stripped):
        return True
    # The tool-result feedback instruction echoed alone.
    if stripped.startswith("Please continue with your response"):
        return True
    return False


def strip_tool_artifacts(text: str) -> str:
    """One-stop display cleaner: remove every kind of tool/reasoning markup
    a model can leak into visible text — reasoning blocks, ``<tool_call>``/
    ``<tool_result>`` blocks, Llama special tokens, orphan tags, bare
    ``{"tool": ...}`` JSON objects, and the tool-result feedback
    instruction. Display surfaces (GUI chat bubbles, history rendering)
    call this before showing a message; a message that comes back empty
    must not be rendered at all.

    Guard: stripping must never turn a normal response into an invisible
    message. If the cleaned text is empty but the raw text was not, and the
    raw text doesn't actually look like pure tool/reasoning markup, a
    warning is logged and the raw text is returned instead of "".
    """
    if not text:
        return ""
    cleaned = strip_reasoning_blocks(text)
    cleaned = strip_raw_tool_tags(cleaned)
    cleaned = strip_bare_tool_json(cleaned)
    cleaned = cleaned.replace(
        "Please continue with your response based on these results.", ""
    ).strip()
    if cleaned or not text.strip():
        return cleaned
    # Stripping emptied a non-empty message. That is correct for real
    # markup (intermediate tool-call rounds, reasoning blocks, tool-result
    # echoes — those must stay hidden or they render as raw JSON/phantom
    # bubbles), but a normal response must never vanish.
    if _looks_like_pure_markup(text):
        return ""
    print(
        "[WARN] strip_tool_artifacts: stripping emptied a non-markup message "
        f"— returning raw text instead ({text.strip()[:120]!r})"
    )
    return text.strip()


def _extract_tool_calls(text: str, message=None, provider: str = None) -> list:
    """Universal tool-call parser — single entry point for every provider
    and every format models actually emit.

    Formats are tried in strict priority order; parsing STOPS at the first
    format that yields at least one valid call:

    1. native     — structured ``message.tool_calls`` or content blocks
                    with ``type="tool_use"`` (OpenAI/Anthropic/DeepSeek/
                    Google/xAI/Ollama). Already parsed by the SDK, just
                    normalized.
    2. xml        — ``<tool_call>{...}</tool_call>`` text blocks (Groq
                    Llama/Qwen), flat or nested-``parameters`` JSON,
                    any payload size, including blocks truncated by
                    max_tokens.
    3. tool_code  — Gemini's Python-code format:
                    ``<tool_code>print(open_browser(name="brave", ...))
                    </tool_code>``. Parsed with ``ast`` (never eval/exec);
                    only calls naming a tool in ``KNOWN_TOOL_NAMES`` count.
    4. bare       — bare JSON objects in plain text:
                    ``{"tool": "name", ...}``. Strict: literal ``"tool"``
                    key naming a tool in ``KNOWN_TOOL_NAMES``, isolated
                    on its own line, AND the surrounding text must not
                    read as a normal conversational answer — prose JSON
                    is never executed.
    5. python_tag — Llama special tokens:
                    ``<|python_tag|>{...}<|eot_id|>``.

    Plain-text descriptions of actions ("I'll open Brave now") never yield
    calls — fallback honesty in ``run()`` handles those.

    Returns a list of ``{"tool": name, **kwargs}`` dicts ready for
    ``TOOL_MAP`` dispatch, in emission order.

    IMPORTANT: must be called on the UN-stripped model output — stripping
    tool markup before parsing destroys the calls. Residual markup is
    stripped from display text only after execution (see ``run``).
    """
    candidates = (
        ("native", lambda: _parse_native_tool_calls(message)),
        ("xml", lambda: _parse_xml_tool_calls(text or "")),
        ("tool_code", lambda: _parse_tool_code_tool_calls(text or "")),
        ("bare", lambda: _parse_bare_json_tool_calls(text or "")),
        ("python_tag", lambda: _parse_python_tag_tool_calls(text or "")),
    )
    for fmt, parse in candidates:
        calls = parse()
        if not calls:
            continue
        # Safety bypass for the false-positive-prone bare format. Reaching
        # it means native and xml found nothing, so the only evidence of a
        # tool call is loose JSON in plain text. If what remains of the
        # text after removing that JSON reads like a normal conversational
        # answer (full sentences of prose), the JSON is almost certainly
        # quoted/illustrative — return it as text instead of executing it.
        if fmt == "bare" and _reads_as_prose(strip_bare_tool_json(text or "")):
            print(
                "[TOOL PARSER] bare JSON ignored: surrounding text reads as "
                f"prose, treating as normal response (provider={provider or '?'})"
            )
            continue
        names = ", ".join(c.get("tool", "?") for c in calls)
        print(
            f"[TOOL PARSER] format={fmt}, tool={names} "
            f"({len(calls)} call(s), provider={provider or '?'})"
        )
        return calls
    return []


def parse_tool_calls(text: str) -> list:
    """Extract tool calls from <tool_call>...</tool_call> blocks.

    Backwards-compatible wrapper around the universal XML parser; prefer
    ``_extract_tool_calls`` which also handles native structured calls.
    """
    return _parse_xml_tool_calls(text)


def has_tool_call(text: str) -> bool:
    """Check if text contains a <tool_call> block."""
    return bool(re.search(r"<tool_call>", text))


def has_tool_markup(text: str) -> bool:
    """Check if text contains anything that LOOKS like a tool invocation —
    a ``<tool_call>`` tag, a Llama ``<|python_tag|>`` payload, or a bare
    JSON object naming a known tool — whether or not it parses into an
    executable call. Used for fallback-honesty warnings when nothing
    executable was extracted.

    Mirrors the strictness of ``_parse_bare_json_tool_calls`` for the bare
    format: a ``"tool": "name"`` fragment embedded inside prose (the model
    quoting or explaining the call format) is NOT markup — only fragments
    sitting on a line of their own (a malformed/truncated attempt at a
    real call) count. Otherwise normal answers that mention tool JSON
    would trigger the fallback-honesty retry.
    """
    if not text:
        return False
    if re.search(r"<tool_call>|<\|python_tag\|>", text):
        return True
    # Bare JSON naming a known tool (malformed beyond repair if it reached
    # this check — parseable ones are executed by the universal parser).
    for m in re.finditer(r'"tool"\s*:\s*"([^"]+)"', text):
        if m.group(1) not in KNOWN_TOOL_NAMES:
            continue
        # Only count it as markup when the fragment's line is JSON-shaped
        # (starts with "{" or a quoted key), not a sentence quoting JSON.
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end].strip()
        if line.startswith("{") or line.startswith('"'):
            return True
    return False


# Phrases by which a model claims it already performed an action that only a
# tool could perform. Some models (especially Llama 4 Scout) hallucinate:
# they SAY they executed a tool but never emit a <tool_call> block — the
# "result" they describe is invented. English and French, past-tense action
# verbs that map onto tool effects (opened, launched, executed, navigated…).
_TOOL_CLAIM_PATTERNS = [
    # "I opened…", "I've launched…", "I have just executed…", "I ran…"
    re.compile(
        r"\bI(?:'ve| have)?\s+(?:just\s+)?(?:successfully\s+)?"
        r"(?:opened|launched|executed|navigated|clicked|created|deleted|"
        r"wrote|written|killed|copied|renamed|moved|filled|typed|pressed|"
        r"scrolled|took|taken|ran(?!\s+into))\b",
        re.IGNORECASE,
    ),
    # "The command has been executed", "the file was created", "is now open"
    re.compile(
        r"\b(?:has|have|was|were) been (?:opened|launched|executed|run|"
        r"created|deleted|written|killed|copied|renamed|moved|completed)\b"
        r"|\b(?:was|were) (?:opened|launched|executed|run|created|deleted|"
        r"written|killed|copied|renamed|moved|completed)\b"
        r"|\bis now (?:open(?:ed)?|running|launched|playing)\b",
        re.IGNORECASE,
    ),
    # "J'ai ouvert…", "j'ai lancé…", "j'ai exécuté…"
    re.compile(
        r"\bj'ai\s+(?:ouvert|lancé|exécuté|navigué|cliqué|créé|supprimé|"
        r"écrit|tué|copié|renommé|déplacé|rempli|tapé|pris)\b",
        re.IGNORECASE,
    ),
    # "a été ouvert", "ont été supprimés", "est maintenant ouvert"
    re.compile(
        r"\b(?:a|ont) été (?:ouvert|lancé|exécuté|créé|supprimé|écrit|tué|"
        r"copié|renommé|déplacé)"
        r"|\best maintenant (?:ouvert|lancé|en cours)\b",
        re.IGNORECASE,
    ),
]


def claims_tool_execution(text: str) -> bool:
    """Heuristic: does ``text`` claim that an action/tool was already
    performed? Used to catch models that describe the result of a tool
    they never actually called.
    """
    if not text:
        return False
    return any(p.search(text) for p in _TOOL_CLAIM_PATTERNS)


# Phrases announcing an action the model is ABOUT to do ("I'll open Brave
# now", "je vais ouvrir…"). When such an announcement arrives with no
# structured tool call, nothing will actually happen — fallback honesty
# requires logging a warning and never pretending it was done.
_TOOL_INTENT_PATTERNS = [
    re.compile(
        r"\b(?:I(?:'ll| will|'m going to| am going to)|let me)\s+(?:now\s+)?"
        r"(?:open|launch|execute|run|navigate|click|create|delete|write|"
        r"kill|copy|rename|move|fill|type|press|scroll|take|search|start)\b",
        re.IGNORECASE,
    ),
    # "Je vais ouvrir…", "je vais lancer…"
    re.compile(
        r"\bje vais\s+(?:ouvrir|lancer|exécuter|naviguer|cliquer|créer|"
        r"supprimer|écrire|tuer|copier|renommer|déplacer|remplir|taper|"
        r"prendre|chercher|démarrer)\b",
        re.IGNORECASE,
    ),
]


def describes_tool_use(text: str) -> bool:
    """Heuristic: does ``text`` describe tool use in prose — either claiming
    an action already happened (``claims_tool_execution``) or announcing
    one is about to happen ("I'll open Brave now")?

    A response for which this is True but which carries no structured tool
    call must never be executed nor presented as done.
    """
    if not text:
        return False
    if claims_tool_execution(text):
        return True
    return any(p.search(text) for p in _TOOL_INTENT_PATTERNS)


# ---------------------------------------------------------------------------
# Anti-loop protection — smart detectors instead of a hard tool-call cap
# ---------------------------------------------------------------------------

# Same tool called with the exact same parameters this many times in a row.
REPEAT_CALL_LIMIT = 5
# This many consecutive tool calls all returning errors.
ERROR_STREAK_LIMIT = 8
# A cycle of identical (tool, params) calls — e.g. navigate→read_page→
# navigate→read_page… — repeated more than this many times means no progress.
STALL_CYCLE_REPEATS = 3
# Longest cycle length (in tool calls) the stall detector looks for.
STALL_MAX_CYCLE_LEN = 4
# Wall-clock budget for a single agent turn (model calls + tool execution).
TURN_TIME_LIMIT_SECONDS = 180.0


class _AntiLoopGuard:
    """Watches the tool-calling loop for signs it stopped making progress.

    Replaces the old hard ``max_tool_rounds`` cap: the agent is never cut
    off just for making many tool calls — only when one of these
    detectors trips:

    - **repetition**: the same tool called with the exact same parameters
      ``REPEAT_CALL_LIMIT`` times in a row
    - **error streak**: ``ERROR_STREAK_LIMIT`` consecutive tool calls all
      returned errors
    - **stall**: the same sequence of (tool, params) calls (cycle length
      2..``STALL_MAX_CYCLE_LEN``) repeated more than ``STALL_CYCLE_REPEATS``
      times — identical calls each cycle means no progress
    - **time limit**: the whole turn exceeded ``TURN_TIME_LIMIT_SECONDS``

    Every executed tool call must be reported through :meth:`record`. When
    a detector trips, :attr:`stop_reason` (for the user-facing summary) and
    :attr:`model_notice` (the message told to the model) are set, and the
    agent loop is expected to stop calling tools and wrap up with a summary
    (see ``OSIAAgent._finish_anti_loop_stop``).
    """

    def __init__(self, lang: str = "fr"):
        self.lang = lang
        self.started_at = time.monotonic()
        # Executed calls as (tool_name, canonical-params-JSON) fingerprints.
        self.calls: list[tuple[str, str]] = []
        # Human-readable log of every executed action: (label, succeeded).
        self.actions: list[tuple[str, bool]] = []
        self.consecutive_errors = 0
        self.recent_failures: list[str] = []
        # Set by the first detector that trips.
        self.stop_reason: str | None = None
        self.model_notice: str | None = None

    @property
    def tripped(self) -> bool:
        return self.stop_reason is not None

    # -- recording ----------------------------------------------------------

    def record(self, tool_name: str, kwargs: dict, result_text: str,
               is_error: bool) -> bool:
        """Record one executed tool call, then run every detector.
        Returns True when a detector tripped (the loop must stop).
        """
        fingerprint = json.dumps(
            kwargs, sort_keys=True, ensure_ascii=False, default=str,
        )
        self.calls.append((tool_name, fingerprint))

        args_preview = ", ".join(
            f"{k}={str(v)[:40]}" for k, v in kwargs.items()
        )
        self.actions.append((f"{tool_name}({args_preview})", not is_error))

        if is_error:
            self.consecutive_errors += 1
            failure = result_text.strip().replace("\n", " ")[:160]
            self.recent_failures.append(f"{tool_name}: {failure}")
            self.recent_failures = self.recent_failures[-ERROR_STREAK_LIMIT:]
        else:
            self.consecutive_errors = 0
            self.recent_failures = []

        return (
            self._check_repetition()
            or self._check_error_streak()
            or self._check_stall()
            or self.check_time()
        )

    # -- detectors ----------------------------------------------------------

    def _trip(self, detector: str, reason_fr: str, reason_en: str,
              notice_fr: str, notice_en: str) -> bool:
        """Latch the first tripped detector's reason/notice in the agent's
        language. Always returns True so detectors can ``return self._trip(…)``.
        """
        tool = self.calls[-1][0] if self.calls else "(none)"
        print(f"[ANTI-LOOP] detector={detector}, tool={tool}, stopping")
        if self.lang == "fr":
            self.stop_reason, self.model_notice = reason_fr, notice_fr
        else:
            self.stop_reason, self.model_notice = reason_en, notice_en
        return True

    def _check_repetition(self) -> bool:
        """Same tool, exact same parameters, REPEAT_CALL_LIMIT times in a row."""
        if len(self.calls) < REPEAT_CALL_LIMIT:
            return False
        tail = self.calls[-REPEAT_CALL_LIMIT:]
        if len(set(tail)) != 1:
            return False
        tool_name = tail[0][0]
        return self._trip(
            detector="repetition",
            reason_fr=(
                f"la même action ({tool_name}) a déjà été tentée "
                f"{REPEAT_CALL_LIMIT} fois de suite sans succès"
            ),
            reason_en=(
                f"the same action ({tool_name}) was already attempted "
                f"{REPEAT_CALL_LIMIT} times in a row without success"
            ),
            notice_fr=(
                f"Cette action a déjà été tentée {REPEAT_CALL_LIMIT} fois "
                "sans succès, essaie une approche différente."
            ),
            notice_en=(
                f"This action has already been attempted {REPEAT_CALL_LIMIT} "
                "times without success, try a different approach."
            ),
        )

    def _check_error_streak(self) -> bool:
        """ERROR_STREAK_LIMIT consecutive tool calls all returned errors."""
        if self.consecutive_errors < ERROR_STREAK_LIMIT:
            return False
        failures = "; ".join(self.recent_failures)
        return self._trip(
            detector="error_streak",
            reason_fr=(
                f"les {ERROR_STREAK_LIMIT} derniers appels d'outils ont tous "
                f"échoué ({failures})"
            ),
            reason_en=(
                f"the last {ERROR_STREAK_LIMIT} tool calls all failed "
                f"({failures})"
            ),
            notice_fr=(
                f"Les {ERROR_STREAK_LIMIT} derniers appels d'outils ont tous "
                f"échoué : {failures}"
            ),
            notice_en=(
                f"The last {ERROR_STREAK_LIMIT} tool calls all failed: "
                f"{failures}"
            ),
        )

    def _check_stall(self) -> bool:
        """The same sequence of calls cycling with no progress — e.g.
        navigate→read_page→navigate→read_page… repeated more than
        STALL_CYCLE_REPEATS times with identical parameters each cycle.
        (Single-call cycles are handled by the repetition detector.)
        """
        min_repeats = STALL_CYCLE_REPEATS + 1
        for period in range(2, STALL_MAX_CYCLE_LEN + 1):
            window = period * min_repeats
            if len(self.calls) < window:
                continue
            tail = self.calls[-window:]
            cycle = tail[:period]
            if all(tail[i] == cycle[i % period] for i in range(window)):
                names = "→".join(name for name, _ in cycle)
                return self._trip(
                    detector="stall",
                    reason_fr=(
                        f"je répétais la même séquence d'actions ({names}) "
                        "sans progresser"
                    ),
                    reason_en=(
                        f"I kept repeating the same sequence of actions "
                        f"({names}) without making progress"
                    ),
                    notice_fr=(
                        f"Tu as répété la séquence {names} plus de "
                        f"{STALL_CYCLE_REPEATS} fois sans aucun progrès, "
                        "essaie une approche différente."
                    ),
                    notice_en=(
                        f"You repeated the sequence {names} more than "
                        f"{STALL_CYCLE_REPEATS} times with no progress, "
                        "try a different approach."
                    ),
                )
        return False

    def check_time(self) -> bool:
        """The whole turn exceeded TURN_TIME_LIMIT_SECONDS of wall-clock time.
        Safe to call at any point in the loop, not just from ``record``.
        """
        if self.tripped:
            return True
        if time.monotonic() - self.started_at <= TURN_TIME_LIMIT_SECONDS:
            return False
        minutes = int(TURN_TIME_LIMIT_SECONDS // 60)
        return self._trip(
            detector="time_limit",
            reason_fr=(
                f"la limite de temps de {minutes} minutes pour un seul tour "
                "a été atteinte"
            ),
            reason_en=(
                f"the {minutes}-minute time limit for a single turn was reached"
            ),
            notice_fr=f"Ce tour a dépassé la limite de {minutes} minutes.",
            notice_en=f"This turn exceeded the {minutes}-minute time limit.",
        )

    # -- summaries -----------------------------------------------------------

    def action_log(self) -> str:
        """Bullet list of every executed action with its outcome."""
        if not self.actions:
            return "- (aucune action)" if self.lang == "fr" else "- (no actions)"
        return "\n".join(
            f"- {label} {'✓' if ok else '✗'}" for label, ok in self.actions
        )

    def stop_instruction(self) -> str:
        """Message fed to the model when a detector trips: the detector's
        notice, the action log, and an order to stop calling tools and
        answer with the required summary format.
        """
        n = len(self.actions)
        log = self.action_log()
        if self.lang == "fr":
            return (
                f"[PROTECTION ANTI-BOUCLE] {self.model_notice}\n\n"
                f"ARRÊTE d'appeler des outils maintenant. Tu as effectué "
                f"{n} action(s) :\n{log}\n\n"
                "Donne ta réponse finale à l'utilisateur en suivant exactement "
                "ce format :\n"
                f'"J\'ai effectué {n} actions. Voici ce que j\'ai accompli : '
                "[liste courte de ce qui a été fait]. Je me suis arrêté car "
                f'{self.stop_reason}. Que souhaitez-vous que je fasse ?"\n'
                "N'appelle AUCUN outil dans cette réponse."
            )
        return (
            f"[ANTI-LOOP PROTECTION] {self.model_notice}\n\n"
            f"STOP calling tools now. You performed {n} action(s):\n{log}\n\n"
            "Give your final answer to the user following exactly this "
            "format:\n"
            f'"I performed {n} actions. Here is what I accomplished: '
            "[short list of what was done]. I stopped because "
            f'{self.stop_reason}. What would you like me to do?"\n'
            "Do NOT call ANY tool in this response."
        )

    def fallback_summary(self) -> str:
        """Deterministic user-facing summary in the required format, used
        when the final summary model call fails or only emits tool markup.
        """
        n = len(self.actions)
        log = self.action_log()
        if self.lang == "fr":
            return (
                f"J'ai effectué {n} actions. Voici ce que j'ai accompli :\n"
                f"{log}\n\n"
                f"Je me suis arrêté car {self.stop_reason}. "
                "Que souhaitez-vous que je fasse ?"
            )
        return (
            f"I performed {n} actions. Here is what I accomplished:\n"
            f"{log}\n\n"
            f"I stopped because {self.stop_reason}. "
            "What would you like me to do?"
        )


class OSIAAgent:
    def __init__(self, model: str = MODEL, base_url: str = OLLAMA_BASE_URL, lang: str = None):
        self.client = OpenAI(base_url=base_url, api_key="ollama")
        self.model = model
        self.provider = "ollama"
        self.lang = lang if lang else DEFAULT_LANG
        # API endpoint fallback (Bloc 5) state — populated for real by
        # set_model(); seeded here so the attributes always exist.
        self._api_key = "ollama"
        # API key fallback hierarchy state — the ordered keys for the current
        # provider and the cursor into them; reset by set_model(), advanced by
        # _activate_next_api_key() on a 429/auth error.
        self._api_key_chain = ["ollama"]
        self._api_key_index = 0
        self._default_headers = None
        self._primary_base_url = base_url
        self._fallback_base_url = None
        self._active_base_url = base_url
        self.messages = [{"role": "system", "content": get_system_prompt(self.lang, self.model, self.provider)}]

    def reset(self):
        """Clear conversation history except system prompt."""
        self.messages = [{"role": "system", "content": get_system_prompt(self.lang, self.model, self.provider)}]

    def exported_history(self) -> list:
        """Return self.messages with the system prompt (index 0) removed,
        plus any other system-role frame or system-prompt-looking content
        filtered out. Callers crossing the agent boundary (server,
        conversation persistence, mobile client) MUST use this instead of
        slicing self.messages directly, so that the system prompt can
        never leak into the conversation history.
        """
        safe: list = []
        for m in self.messages[1:]:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "system":
                continue
            content = str(m.get("content", ""))
            if "RÈGLES ABSOLUES" in content or "ABSOLUTE RULES" in content:
                continue
            if content.lstrip().startswith("##"):
                continue
            safe.append(m)
        return safe

    def set_model(self, model_id: str, provider: str | None = None) -> None:
        """Switch the agent to a new model, reconfiguring the client for that
        model's provider (Anthropic, OpenAI, DeepSeek, Google, xAI, Groq, Ollama).

        ``provider`` is optional and defaults to the provider derived from the
        model id (``provider_for_model``). Pass it explicitly to force a
        specific provider — used by the task-hierarchy router, whose entries
        are stored as ``"provider:model"`` and may pin a model to a provider
        that differs from the id-based heuristic.
        """
        from osia.models import (
            provider_for_model, PROVIDERS,
            endpoint_for_provider, fallback_endpoint_for_provider,
        )

        if not provider:
            provider = provider_for_model(model_id)
        cfg = PROVIDERS.get(provider, PROVIDERS["ollama"])
        # API key fallback hierarchy: when the user configured an ordered key
        # list for this provider (api_key_fallbacks), it is authoritative —
        # key[0] is the primary used here; the rest are tried in run() on a
        # 429/auth error. Otherwise the single saved key (api_keys.json) is
        # used, exactly as before. Reset the per-turn key cursor to the primary.
        self._api_key_chain = self._provider_key_chain(provider)
        self._api_key_index = 0
        api_key = self._api_key_chain[0] if self._api_key_chain else "ollama"

        if not api_key:
            api_key = "ollama"  # placeholder; request will likely 401 if used

        # Every provider authenticates with its own API key. Anthropic uses
        # its standard endpoint with the ``x-api-key`` header; all other
        # providers (OpenAI, DeepSeek, Google, xAI, Groq) are OpenAI-compatible
        # and use Bearer auth, which the OpenAI client derives from
        # ``api_key`` automatically.
        default_headers: dict[str, str] | None = None
        if provider == "anthropic":
            default_headers = {"x-api-key": api_key}

        # API endpoint fallback (Bloc 5): resolve the provider's *effective*
        # primary base_url (honouring any user override) and remember the
        # optional fallback URL + the auth bits, so run() can rebuild the
        # client against the fallback endpoint on a connection/timeout error.
        self._api_key = api_key
        self._default_headers = default_headers
        self._primary_base_url = endpoint_for_provider(provider) or cfg["base_url"]
        self._fallback_base_url = fallback_endpoint_for_provider(provider)
        self._active_base_url = self._primary_base_url

        self.client = OpenAI(
            base_url=self._primary_base_url,
            api_key=api_key,
            default_headers=default_headers,
        )
        self.model = model_id
        self.provider = provider
        # Refresh the system message so the model/provider line at the
        # top of the prompt reflects the newly-selected model.
        if self.messages and isinstance(self.messages[0], dict) and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = get_system_prompt(self.lang, self.model, self.provider)

    def _activate_fallback_endpoint(self) -> bool:
        """Rebuild the client against the provider's fallback base_url (Bloc 5).

        Returns ``True`` if a fallback URL is configured and we are not already
        using it (so the caller can retry the request); ``False`` otherwise.
        Same model, key and auth headers — only the endpoint changes.
        """
        fallback = self._fallback_base_url
        if not fallback or self._active_base_url == fallback:
            return False
        self.client = OpenAI(
            base_url=fallback,
            api_key=self._api_key,
            default_headers=self._default_headers,
        )
        self._active_base_url = fallback
        return True

    # ── API key fallback hierarchy ─────────────────────────────────────────
    # Each provider can have an ordered list of API keys (api_key_fallbacks):
    # key[0] is the primary, the rest are tried in turn on a 429/auth error
    # BEFORE the AI Task Hierarchy advances to a different model.

    def _provider_key_chain(self, provider: str) -> list:
        """Ordered list of API keys to try for ``provider``.

        When the user configured an ``api_key_fallbacks`` list for the provider
        it is authoritative (key[0] = primary, the rest = fallbacks). Otherwise
        the single saved key from ``api_keys.json`` is used. Ollama needs no
        key. Blank entries are dropped so an empty slot never shadows a real key.
        """
        if provider == "ollama":
            return ["ollama"]
        from osia.config import get_setting, get_api_key
        fallbacks = get_setting("api_key_fallbacks", {}) or {}
        chain = [k.strip() for k in (fallbacks.get(provider) or [])
                 if isinstance(k, str) and k.strip()]
        if chain:
            return chain
        key = get_api_key(provider)
        return [key] if key else []

    def _activate_next_api_key(self):
        """Advance to the next key in the current provider's fallback chain and
        rebuild the client with it — same model, provider and endpoint, only the
        API key (and Anthropic's ``x-api-key`` header) change.

        Returns the new key's 1-based position on success (so the caller can log
        "key1 failed, trying key2"), or ``None`` when the chain is exhausted.
        """
        chain = self._api_key_chain or []
        if self._api_key_index + 1 >= len(chain):
            return None
        self._api_key_index += 1
        new_key = chain[self._api_key_index]
        self._api_key = new_key
        if self.provider == "anthropic":
            self._default_headers = {"x-api-key": new_key}
        self.client = OpenAI(
            base_url=self._active_base_url,
            api_key=new_key,
            default_headers=self._default_headers,
        )
        return self._api_key_index + 1

    # ── Task-hierarchy routing (Bloc 3) ───────────────────────────────────
    # Classify each turn (heavy/medium) and try an ordered fallback chain of
    # "provider:model" entries, advancing to the next model ONLY on a
    # technical/API error (429, 5xx, auth, timeout, connection). Normal
    # responses and content/client errors never trigger a fallback. When no
    # chain is configured for the task, the manually selected model is used
    # unchanged.

    @staticmethod
    def _parse_hierarchy_entry(entry):
        """Parse a ``"provider:model"`` (or bare ``"model"``) chain entry into
        ``(provider_or_None, model_id)``; returns ``None`` for blanks. Model
        ids never contain ':' (Groq ids use '/'), so one split is safe.
        """
        if not isinstance(entry, str):
            return None
        entry = entry.strip()
        if not entry:
            return None
        if ":" in entry:
            provider, model_id = entry.split(":", 1)
            provider = provider.strip() or None
            model_id = model_id.strip()
        else:
            provider, model_id = None, entry
        if not model_id:
            return None
        return (provider, model_id)

    def _fmt_route(self, candidate) -> str:
        """Format a ``(provider, model_id)`` candidate as ``provider:model``."""
        provider, model_id = candidate
        if not provider:
            try:
                from osia.models import provider_for_model
                provider = provider_for_model(model_id)
            except Exception:
                provider = "?"
        return f"{provider}:{model_id}"

    def _resolve_route(self, user_input: str):
        """Classify the task and return ``(category, candidates)``.

        ``candidates`` is the ordered, de-duplicated list of
        ``(provider, model_id)`` tuples from the configured chain for that
        category. Returns an empty list when no hierarchy is configured — the
        caller then leaves the currently selected model untouched.
        """
        try:
            from osia.task_router import classify_task
            from osia.config import get_setting
            category = classify_task(user_input)
            hierarchy = get_setting("ai_hierarchy", {}) or {}
            raw_chain = hierarchy.get(category) or []
        except Exception:
            return ("medium", [])
        candidates = []
        seen = set()
        for entry in raw_chain:
            parsed = self._parse_hierarchy_entry(entry)
            if parsed is None or parsed in seen:
                continue
            seen.add(parsed)
            candidates.append(parsed)
        return (category, candidates)

    @staticmethod
    def _error_status(e):
        """Extract an HTTP status code from an OpenAI-SDK / requests error."""
        return (
            getattr(e, "status_code", None)
            or getattr(getattr(e, "response", None), "status_code", None)
        )

    def _is_technical_error(self, e, status=None) -> bool:
        """True for transient/technical API failures that warrant a model
        fallback: rate limits (429), server errors (5xx), auth errors
        (401/403), request timeout (408), timeouts and connection drops.

        Returns False for client/content errors (400/404/422 …) and anything
        that is not an API/transport failure — those must NOT silently switch
        the model.
        """
        if status is None:
            status = self._error_status(e)
        if isinstance(status, int):
            return status == 429 or status >= 500 or status in (401, 403, 408)
        # No HTTP status (timeouts, connection resets): classify by type/name.
        name = type(e).__name__.lower()
        if any(s in name for s in ("timeout", "connection", "serviceunavailable")):
            return True
        msg = str(e).lower()
        return any(
            s in msg
            for s in ("timed out", "timeout", "connection error",
                      "connection reset", "temporarily unavailable", "rate limit")
        )

    def _technical_error_reason(self, e, status=None) -> str:
        """Short label for the fallback log line ('429', '503', a type name…)."""
        if status is None:
            status = self._error_status(e)
        if isinstance(status, int):
            return str(status)
        return type(e).__name__

    def _stream_deltas(self):
        """Yield text deltas from the OpenAI-compatible chat-completions
        stream, abstracting it so the sentence-splitting loop stays simple.

        Responses are never token-limited: no ``max_tokens`` is sent, so
        each provider applies its own native limit.
        """
        create_kwargs = {
            "model": self.model,
            "messages": self.messages,
            "stream": True,
        }
        stream = self.client.chat.completions.create(**create_kwargs)
        for chunk in stream:
            try:
                yield chunk.choices[0].delta.content or ""
            except (IndexError, AttributeError):
                yield ""

    # Reasoning-block tag pairs (Qwen 3, DeepSeek-R1, …). Text between them
    # is internal chain-of-thought — kept in `full` but never spoken.
    _THINK_TAG_PAIRS = (
        ("<think>", "</think>"),
        ("<thinking>", "</thinking>"),
    )

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

    def _stream_with_sentences(self, on_sentence, on_first_token=None) -> str:
        """Stream a chat completion, emitting each complete sentence to
        ``on_sentence`` as it arrives. Returns the full accumulated content
        so the outer loop can still detect tool calls.

        Anything inside a ``<tool_call>...</tool_call>`` block (or Gemini's
        ``<tool_code>...</tool_code>`` Python-code variant) is buffered but
        never emitted — we don't want to speak tool markup. The same goes
        for ``<think>...</think>`` / ``<thinking>...</thinking>`` reasoning
        blocks (Qwen 3, DeepSeek-R1, …): they are kept in the returned
        content but never spoken or streamed to the user.

        If ``on_first_token`` is provided it is invoked (once per call)
        the first time a non-empty content delta arrives from the model.
        Useful for latency profiling.
        """
        full = ""
        pending = ""       # raw text not yet routed to speakable/tool
        speakable = ""     # awaiting a sentence-ending punctuation char
        in_tool_call = False
        tool_close_tag = "</tool_call>"  # closing tag of the tool block we're in
        think_close = None  # closing tag of the reasoning block we're inside, if any
        # Once True, the model has committed to a tool this round. Anything
        # it says afterwards is ungrounded (the tool hasn't actually run
        # yet) — we still feed it into `full` so the outer loop sees the
        # tool_call markup, but we MUST NOT speak it. The next round, run
        # after the real tool result is fed back, produces the spoken reply.
        tool_call_seen = False
        first_token_fired = False

        for delta in self._stream_deltas():
            if not delta:
                continue
            if not first_token_fired:
                first_token_fired = True
                if on_first_token is not None:
                    try:
                        on_first_token()
                    except Exception:
                        pass
            full += delta
            pending += delta

            # Walk through pending, splitting at <tool_call> / <think> markers.
            while pending:
                if in_tool_call:
                    close = pending.find(tool_close_tag)
                    if close == -1:
                        pending = ""  # still inside tool call, discard
                        break
                    pending = pending[close + len(tool_close_tag):]
                    in_tool_call = False
                    continue

                if think_close is not None:
                    # Inside a <think>/<thinking> block — discard the
                    # reasoning, holding back a tail in case the closing
                    # tag is split across chunks.
                    close = pending.find(think_close)
                    if close == -1:
                        pending = pending[-(len(think_close) - 1):]
                        break
                    pending = pending[close + len(think_close):]
                    think_close = None
                    continue

                if tool_call_seen:
                    # Post-tool_call prose this round — keep it in `full`
                    # but never speak it; the model can't yet know what
                    # the tool returned.
                    pending = ""
                    break

                # Locate the earliest special tag in the buffer. Both
                # <tool_call> (Groq/Llama JSON) and <tool_code> (Gemini's
                # Python-code format) are suppressed identically so neither
                # the JSON nor the raw Python is ever spoken/streamed.
                tool_idx = pending.find("<tool_call>")
                tool_open, tool_close = "<tool_call>", "</tool_call>"
                code_idx = pending.find("<tool_code>")
                if code_idx != -1 and (tool_idx == -1 or code_idx < tool_idx):
                    tool_idx, tool_open, tool_close = code_idx, "<tool_code>", "</tool_code>"
                think_idx, think_open, think_end = -1, None, None
                for open_tag, close_tag in self._THINK_TAG_PAIRS:
                    idx = pending.find(open_tag)
                    if idx != -1 and (think_idx == -1 or idx < think_idx):
                        think_idx, think_open, think_end = idx, open_tag, close_tag

                if tool_idx == -1 and think_idx == -1:
                    # Hold back the last few chars in case they're the start of
                    # a partial "<tool_call>"/"<thinking>" tag split across chunks.
                    safe = max(0, len(pending) - 10)
                    speakable += pending[:safe]
                    pending = pending[safe:]
                    speakable = self._emit_sentences(speakable, on_sentence)
                    break

                if think_idx != -1 and (tool_idx == -1 or think_idx < tool_idx):
                    # Reasoning block: speak what precedes it, then suppress
                    # everything until the matching closing tag.
                    speakable += pending[:think_idx]
                    pending = pending[think_idx + len(think_open):]
                    think_close = think_end
                    speakable = self._emit_sentences(speakable, on_sentence)
                    continue

                # Flush text up to the tag, then enter tool-call mode.
                speakable += pending[:tool_idx]
                pending = pending[tool_idx + len(tool_open):]
                in_tool_call = True
                tool_close_tag = tool_close
                tool_call_seen = True
                speakable = self._emit_sentences(
                    speakable, on_sentence, flush=True,
                )

        # End of stream — only flush the tail if this round produced no
        # tool call at all and we're not still inside a reasoning block.
        # If a tool call was seen, anything still in `speakable`/`pending`
        # is ungrounded follow-up text; if a reasoning block never closed,
        # the leftovers are chain-of-thought.
        if not in_tool_call and not tool_call_seen and think_close is None:
            speakable += pending
            self._emit_sentences(speakable, on_sentence, flush=True)

        return full

    def _finish_anti_loop_stop(self, guard: _AntiLoopGuard, on_sentence=None,
                               on_first_token=None) -> str:
        """Wrap up a turn stopped by the anti-loop guard.

        The stop instruction (detector notice + action log + summary format)
        has already been appended to ``self.messages``; this makes ONE final
        model call to produce the user-facing summary. Tool calls in that
        response are never executed — only stripped. If the call fails or
        the response is empty once tool markup is removed, falls back to a
        deterministic summary built from the guard's action log so the user
        always gets the "J'ai effectué N actions…" report.
        """
        print(
            f"[OSIA Agent] ⛔ anti-loop stop after {len(guard.actions)} "
            f"tool call(s): {guard.stop_reason}"
        )
        content = ""
        try:
            if on_sentence is not None:
                content = self._stream_with_sentences(
                    on_sentence, on_first_token=on_first_token,
                )
            else:
                create_kwargs = {
                    "model": self.model,
                    "messages": self.messages,
                }
                response = self.client.chat.completions.create(**create_kwargs)
                content = response.choices[0].message.content or ""
            # Same final-text pipeline as a normal response: strip reasoning
            # first, then every form of tool markup (nothing gets executed).
            content = strip_reasoning_blocks(content)
            content = strip_raw_tool_tags(content)
            content = strip_bare_tool_json(content).strip()
        except Exception as e:
            print(f"[OSIA Agent] ✗ anti-loop summary call failed: {e}")
            content = ""

        if not content:
            # Model couldn't produce a usable summary — use the deterministic
            # one, and speak it too in streaming mode (nothing was emitted).
            content = guard.fallback_summary()
            if on_sentence is not None:
                try:
                    on_sentence(clean_response(content))
                except Exception:
                    pass

        self.messages.append({"role": "assistant", "content": content})
        return content

    def _empty_response_fallback(self, guard: _AntiLoopGuard) -> str:
        """Deterministic user-facing reply for when the model's final answer
        is empty once tool/reasoning markup is stripped (even after the
        empty-response retry). Reports executed actions when there are any,
        so the user knows what actually happened this turn.
        """
        n = len(guard.actions)
        if n:
            if self.lang == "fr":
                return (
                    f"J'ai exécuté {n} action(s) :\n{guard.action_log()}\n\n"
                    "Mais je n'ai pas réussi à formuler une réponse finale. "
                    "Que souhaitez-vous que je fasse ?"
                )
            return (
                f"I executed {n} action(s):\n{guard.action_log()}\n\n"
                "But I couldn't produce a final response. "
                "What would you like me to do?"
            )
        if self.lang == "fr":
            return (
                "Désolé, je n'ai pas réussi à formuler une réponse. "
                "Pouvez-vous reformuler votre demande ?"
            )
        return (
            "Sorry, I couldn't produce a response. "
            "Could you rephrase your request?"
        )

    def _drop_history_screenshots(self) -> None:
        """Reduce past tool-result messages in ``self.messages`` to their
        text part, dropping any base64 screenshot/image data.

        A screenshot is attached as a vision part only so the model can
        analyze the page on the very next turn; once that turn has run,
        the now-stale image (100K-600K base64 chars each) would otherwise
        sit in history forever and balloon the context across a multi-step
        browser task. This is called right before the current turn's fresh
        screenshot is appended, so the live screenshot is left untouched
        and only the already-seen ones are stripped.
        """
        saved = 0
        for msg in self.messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            texts = []
            had_image = False
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    had_image = True
                    url = (part.get("image_url") or {}).get("url", "")
                    saved += len(url.split("base64,", 1)[-1])
            if had_image:
                msg["content"] = "\n\n".join(t for t in texts if t)
        if saved:
            print(f"[CONTEXT] dropped screenshot from history ({saved} chars saved)")

    def run(self, user_input: str, max_tool_rounds: int | None = None, on_sentence=None,
            history: list | None = None, max_tokens: int | None = None,
            on_first_token=None) -> str:
        """Run the agent loop: send user input, handle tool calls, return final response.

        Each round runs the tool pipeline in strict order: ``<think>``
        blocks are stripped first, tool calls are parsed with the universal
        parser (native structured calls, XML blocks, bare JSON, Llama
        python-tag payloads — tried in that priority order) and executed
        BEFORE any tag stripping, and raw tool markup is stripped only from
        the final text once all execution decisions are done.

        If ``on_sentence`` is provided, the API call is streamed and the
        callback is invoked with each complete sentence (cleaned) as soon
        as it arrives. Sentences that fall inside a ``<tool_call>`` or
        ``<think>``/``<thinking>`` block are skipped so tool markup and
        internal reasoning are never spoken.

        The returned response (and the assistant messages stored in
        history) has reasoning blocks and raw tool XML tags stripped —
        the user must never see them in mobile or hear them in TTS.

        If ``history`` is provided, it replaces the agent's internal
        conversation history (excluding the system prompt) for this run.
        After the call, callers can read ``self.messages[1:]`` to retrieve
        the updated history (system prompt is at index 0).

        ``max_tokens`` is DEPRECATED and ignored. Responses are never
        token-limited: no ``max_tokens`` is sent on any model call, so each
        provider applies its own native limit and no reply is ever truncated
        mid-answer, regardless of what callers pass here.

        ``max_tool_rounds`` is DEPRECATED and ignored. The hard cap on
        tool rounds was replaced by smart anti-loop detection
        (``_AntiLoopGuard``): the agent is never cut off just for making
        many tool calls — only when it repeats the exact same call, hits
        a streak of errors, cycles through the same call sequence with no
        progress, or exceeds the per-turn time budget. In those cases it
        stops gracefully and returns a summary of what was done.
        """
        if history is not None:
            # Defensively strip anything that looks like a system prompt
            # from the incoming history — only the index-0 system message
            # owned by THIS agent is allowed to carry system content.
            sanitized = [
                m for m in history
                if isinstance(m, dict)
                and m.get("role") in ("user", "assistant")
                and "RÈGLES ABSOLUES" not in str(m.get("content", ""))
                and "ABSOLUTE RULES" not in str(m.get("content", ""))
                and not str(m.get("content", "")).lstrip().startswith("##")
            ]
            self.messages = [{"role": "system", "content": get_system_prompt(self.lang, self.model, self.provider)}] + sanitized
        self.messages.append({"role": "user", "content": user_input})

        # ── Task-hierarchy routing (Bloc 3) ──────────────────────────────
        # Classify this turn and, if a fallback chain is configured for the
        # task category, switch to the top of the chain. The manually selected
        # model is kept as the ultimate fallback, so an exhausted or
        # unconfigured chain degrades cleanly to today's behavior. The except
        # block below advances ``route_idx`` on technical/API errors only.
        _default_route = (self.provider, self.model)
        # Master switch: only classify the task and route through a chain when
        # automatic task routing is enabled. When disabled, the manually
        # selected model is always used (the configured chains are ignored).
        from osia.config import get_setting as _get_setting
        _hierarchy_enabled = bool(_get_setting("ai_hierarchy_enabled", False))
        if _hierarchy_enabled:
            _route_category, route_candidates = self._resolve_route(user_input)
        else:
            _route_category, route_candidates = (None, [])
        route_idx = 0
        if route_candidates:
            if (_default_route not in route_candidates
                    and (None, self.model) not in route_candidates):
                route_candidates.append(_default_route)
            first_provider, first_model = route_candidates[0]
            try:
                self.set_model(first_model, provider=first_provider)
                route_idx = 0
            except Exception as _route_err:
                # set_model failed (e.g. missing key): the MANUAL model is
                # still loaded. Point route_idx at it so the except-block
                # fallback names the right model and never re-runs it.
                print(f"[ROUTER] could not select {self._fmt_route(route_candidates[0])} "
                      f"({_route_err}); keeping {self._fmt_route(_default_route)}")
                try:
                    route_idx = route_candidates.index(_default_route)
                except ValueError:
                    route_idx = 0
            print(f"[ROUTER] task={_route_category}, "
                  f"trying {self._fmt_route(route_candidates[route_idx])}")
        elif _hierarchy_enabled:
            print(f"[ROUTER] task={_route_category}, no hierarchy configured — "
                  f"using selected model {self._fmt_route(_default_route)}")
        else:
            print(f"[ROUTER] automatic task routing disabled — "
                  f"using selected model {self._fmt_route(_default_route)}")

        # Streaming fallback safety: a mid-stream technical drop must NOT cause
        # the next model to re-speak sentences already streamed to TTS/mobile.
        # This tracks whether any sentence was emitted on the current attempt;
        # the except block only takes the streaming fallback when nothing was
        # emitted yet (pre-token failures: 429/401/403/connection-open).
        _stream_emitted = {"v": False}
        if on_sentence is not None:
            def _on_sentence_cb(_text, _orig=on_sentence, _flag=_stream_emitted):
                _flag["v"] = True
                return _orig(_text)
        else:
            _on_sentence_cb = on_sentence

        # Hallucinated-tool-claim guard (Llama 4 Scout & co): tracks whether
        # any tool ACTUALLY ran this turn, and whether the one allowed
        # "you never called the tool" retry has been spent.
        any_tool_executed = False
        hallucination_retry_used = False
        # Empty-response guard: whether the one allowed "your reply was
        # empty after stripping markup" retry has been spent.
        empty_retry_used = False

        # API endpoint fallback (Bloc 5): whether the one allowed "primary
        # endpoint failed → retry on the fallback base_url" switch has been
        # spent this turn (prevents flapping between endpoints).
        endpoint_fallback_tried = False

        # Smart anti-loop protection (replaces the old hard round cap):
        # repetition, error-streak, stall and turn-time detectors. The loop
        # below runs for as many rounds as the model needs — it is only
        # stopped when the guard says no progress is being made.
        guard = _AntiLoopGuard(lang=self.lang)

        while True:
            try:
                if on_sentence is not None:
                    # on_first_token only fires the first time a delta arrives
                    # in this round. The server-side flag dedupes across rounds.
                    # Streaming aggregates text only — no native message object.
                    message = None
                    _stream_emitted["v"] = False  # reset per attempt
                    content = self._stream_with_sentences(
                        _on_sentence_cb,
                        on_first_token=on_first_token,
                    )
                else:
                    create_kwargs = {
                        "model": self.model,
                        "messages": self.messages,
                    }
                    response = self.client.chat.completions.create(**create_kwargs)
                    # Keep the message object: native providers may return
                    # structured tool calls on it (message.tool_calls).
                    message = response.choices[0].message
                    content = message.content or ""
            except Exception as e:
                # Surface the full 429 response so we can tell a real rate
                # limit apart from an auth problem masquerading as one.
                status = (
                    getattr(e, "status_code", None)
                    or getattr(getattr(e, "response", None), "status_code", None)
                )
                if status == 429:
                    resp = getattr(e, "response", None)
                    body = None
                    if resp is not None:
                        try:
                            body = resp.text
                        except Exception:
                            body = None
                    if not body:
                        body = getattr(e, "body", None)
                    print(
                        f"[DEBUG] 429 from provider={self.provider} model={self.model}"
                    )
                    print(f"[DEBUG] 429 response body: {body!r}")
                    try:
                        headers = dict(getattr(resp, "headers", {}) or {})
                        print(f"[DEBUG] 429 response headers: {headers}")
                    except Exception:
                        pass
                # Task-hierarchy fallback (Bloc 3): on a technical/API error,
                # advance to the next model in the chain and retry this turn.
                # `self.messages` is unchanged on a failed call, so the retry
                # re-sends a clean conversation to the next model. Non-technical
                # errors and an exhausted chain re-raise exactly as before.
                #
                # Streaming guard: if sentences were ALREADY streamed to
                # TTS/mobile this attempt (a rare mid-stream drop), re-raise
                # instead of falling back — re-running the next model would
                # re-speak the opening of the reply. Pre-token failures
                # (429/401/403/connection-open) emit nothing, so fallback there
                # stays fully effective.
                _already_streamed = on_sentence is not None and _stream_emitted["v"]

                # API endpoint fallback (Bloc 5): a connection/timeout failure
                # against the PRIMARY endpoint is retried once against the
                # provider's configured fallback base_url — same model, key and
                # headers, only the endpoint changes. Tried BEFORE the model
                # hierarchy so a transient endpoint blip doesn't burn a model
                # slot. Only transport-level failures (no HTTP status) qualify:
                # a 401/403/429 is an account/key issue a mirror won't fix.
                # Skipped once sentences were streamed (re-running would
                # re-speak the reply's opening).
                if (not endpoint_fallback_tried
                        and status is None
                        and self._is_technical_error(e, status)
                        and not _already_streamed
                        and self._activate_fallback_endpoint()):
                    endpoint_fallback_tried = True
                    print(f"[AGENT] primary endpoint for provider={self.provider} "
                          f"failed ({self._technical_error_reason(e, status)}), "
                          f"retrying on fallback {self._active_base_url}")
                    continue

                # API key fallback: a rate-limit (429) or auth (401/403) failure
                # is retried with the NEXT key in this provider's
                # api_key_fallbacks chain BEFORE the AI Task Hierarchy advances
                # to a different model. Same model/provider/endpoint — only the
                # key changes. Skipped once sentences were streamed (re-running
                # would re-speak the reply's opening).
                if status in (429, 401, 403) and not _already_streamed:
                    _next_key_pos = self._activate_next_api_key()
                    if _next_key_pos is not None:
                        print(f"[API FALLBACK] {self.provider} "
                              f"key{_next_key_pos - 1} failed "
                              f"({self._technical_error_reason(e, status)}), "
                              f"trying key{_next_key_pos}")
                        continue

                if (route_idx + 1 < len(route_candidates)
                        and self._is_technical_error(e, status)
                        and not _already_streamed):
                    prev = route_candidates[route_idx]
                    nxt = route_candidates[route_idx + 1]
                    try:
                        self.set_model(nxt[1], provider=nxt[0])
                    except Exception as switch_err:
                        print(f"[ROUTER] cannot switch to {self._fmt_route(nxt)} "
                              f"({switch_err})")
                        raise
                    route_idx += 1
                    print(f"[ROUTER] {self._fmt_route(prev)} failed "
                          f"({self._technical_error_reason(e, status)}), "
                          f"falling back to {self._fmt_route(nxt)}")
                    continue
                raise

            # ── Tool-call pipeline ──────────────────────────────────────
            # The order below is load-bearing:
            #   1. strip <think> blocks (reasoning never contains real
            #      tool calls; deliberated-only calls must not run)
            #   2. parse tool calls — BEFORE any tag stripping, otherwise
            #      real calls would be destroyed before execution
            #   3. execute the parsed calls
            #   4. fallback honesty on unexecutable descriptions
            #   5. strip raw tool tags ONLY on the final text
            # ────────────────────────────────────────────────────────────

            # Keep the un-stripped model output for the empty-response
            # guard at STEP 5: if stripping reduces the final reply to
            # nothing, this is what the model actually said.
            raw_round_output = content

            # STEP 1 — strip internal reasoning blocks (<think>…</think>)
            # FIRST: the user must never see chain-of-thought, and a tool
            # call only deliberated inside reasoning must not be executed.
            content = strip_reasoning_blocks(content)

            # STEP 2 — parse tool calls with the universal parser (native
            # structured calls → XML blocks → bare JSON → python-tag, in
            # priority order). The content is still un-stripped here:
            # stripping before parsing would destroy the calls.
            tool_calls = _extract_tool_calls(
                content, message=message, provider=self.provider,
            )

            # STEP 3 — execute the parsed tool calls.
            if tool_calls:
                # Record the assistant turn. Calls that didn't arrive as
                # XML blocks (native/bare/python_tag) are serialized as
                # canonical <tool_call> blocks so the model sees what it
                # called on the next round; the raw bare-JSON/python-tag
                # payload is stripped from the recorded prose so history
                # never carries the same call twice (display paths strip
                # these blocks before rendering).
                assistant_record = content
                if not has_tool_call(assistant_record):
                    prose = strip_bare_tool_json(strip_raw_tool_tags(content)).strip()
                    blocks = "\n".join(
                        f"<tool_call>\n{json.dumps(tc, ensure_ascii=False)}\n</tool_call>"
                        for tc in tool_calls
                    )
                    assistant_record = (
                        f"{prose}\n{blocks}".strip() if prose else blocks
                    )
                self.messages.append({"role": "assistant", "content": assistant_record})

                # Anti-loop: don't even start the batch if the turn has
                # already blown through its time budget (the model call
                # itself may have pushed it over). The pending tool calls
                # are NOT executed — the stop instruction says so.
                if guard.check_time():
                    notice = (
                        "Les appels d'outils ci-dessus n'ont PAS été exécutés."
                        if self.lang == "fr"
                        else "The tool calls above were NOT executed."
                    )
                    self.messages.append({
                        "role": "user",
                        "content": f"{notice}\n\n{guard.stop_instruction()}",
                    })
                    return self._finish_anti_loop_stop(
                        guard, on_sentence=on_sentence,
                        on_first_token=on_first_token,
                    )

                # Execute tools SEQUENTIALLY — each one completes (or
                # times out / errors) before the next starts. If any tool
                # ultimately errors (even after the retry), stop the batch
                # so the model sees the failure on the next round and can
                # report it honestly instead of plowing into more actions
                # that depended on the failed one.
                tool_results = []
                tool_screenshots: list[str] = []
                for i, tc in enumerate(tool_calls):
                    tool_name = tc.get("tool", "")
                    if tool_name not in TOOL_MAP:
                        print(
                            f"[OSIA Agent] ✗ unknown tool requested: {tool_name!r} "
                            f"(known tools: {len(TOOL_MAP)})"
                        )
                        tool_results.append(
                            f"Tool {tool_name} result:\n[ERREUR] outil inconnu: {tool_name}"
                        )
                        # Unknown-tool requests count toward the anti-loop
                        # detectors too (repetition / error streak).
                        guard.record(
                            tool_name,
                            {k: v for k, v in tc.items() if k != "tool"},
                            f"[ERREUR] outil inconnu: {tool_name}",
                            True,
                        )
                        # Treat unknown tool as a hard error: stop the batch.
                        remaining = tool_calls[i + 1:]
                        if remaining:
                            print(
                                f"[OSIA Agent] ⏸ skipping {len(remaining)} follow-up "
                                f"tool call(s) after unknown-tool error"
                            )
                        break

                    kwargs = {k: v for k, v in tc.items() if k != "tool"}
                    kwargs_preview = {
                        k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v)
                        for k, v in kwargs.items()
                    }
                    print(f"[OSIA Agent] ▶ tool: {tool_name}({kwargs_preview})")
                    result, is_error = _execute_tool_with_retry(tool_name, kwargs)
                    any_tool_executed = True
                    text_part = _result_text(result)
                    screenshot = (
                        result.get("screenshot")
                        if isinstance(result, dict) else None
                    )
                    status = "✗ error" if is_error else "✓ ok"
                    extra = (
                        f" +screenshot({len(screenshot)} b64 chars)"
                        if screenshot else ""
                    )
                    print(
                        f"[OSIA Agent] ◀ tool: {tool_name} → "
                        f"{len(text_part)} chars [{status}]{extra}"
                    )
                    result_entry = f"Tool {tool_name} result:\n{text_part}"
                    # Force screenshot analysis: navigation tool results
                    # (navigate, click, find_and_click) carrying a screenshot
                    # the model will actually see get a mandatory
                    # analyze-before-acting instruction prepended.
                    if screenshot and _provider_supports_vision(self.provider, self.model):
                        result_entry = f"{SCREENSHOT_ANALYSIS_INSTRUCTION}\n\n{result_entry}"
                    tool_results.append(result_entry)
                    if screenshot:
                        tool_screenshots.append(screenshot)
                    # Anti-loop: record the executed call and run every
                    # detector (repetition, error streak, stall, time).
                    if guard.record(tool_name, kwargs, text_part, is_error):
                        remaining = tool_calls[i + 1:]
                        if remaining:
                            names = [t.get("tool", "?") for t in remaining]
                            print(
                                f"[OSIA Agent] ⏸ anti-loop tripped — skipping "
                                f"{len(remaining)} follow-up tool call(s): {names}"
                            )
                        break
                    if is_error:
                        remaining = tool_calls[i + 1:]
                        if remaining:
                            names = [t.get("tool", "?") for t in remaining]
                            print(
                                f"[OSIA Agent] ⏸ skipping {len(remaining)} "
                                f"follow-up tool call(s) after error: {names}"
                            )
                        break

                # Feed tool results back. When a browser tool returned a
                # screenshot AND the active model supports vision content,
                # attach it as a vision part so the model can SEE the
                # current page state and pick the right next action
                # (instead of guessing selectors blindly). Non-vision
                # models (DeepSeek, Groq Llama/Qwen, …) only accept plain
                # strings as message content — sending a parts list causes
                # a 400 error — so they get the text part only.
                feedback = "\n\n".join(tool_results)
                # Final-synthesis instruction: after the tool results come
                # back, the model must either chain another tool call or
                # write its complete final answer — never end the turn with
                # nothing (the "je n'ai pas réussi à formuler une réponse
                # finale" fallback).
                if self.lang == "fr":
                    synthesis_instruction = (
                        "Tous les outils ont terminé. Écris maintenant ta "
                        "réponse finale complète en te basant sur tout ce que "
                        "tu as trouvé. (Si tu as encore besoin d'informations "
                        "pour terminer la tâche, appelle un autre outil à la "
                        "place — sinon, réponds en texte simple, sans aucune "
                        "balise <tool_call>.)"
                    )
                else:
                    synthesis_instruction = (
                        "All tools are done. Write your complete final answer "
                        "now based on everything you found. (If you still need "
                        "more information to finish the task, call another tool "
                        "instead — otherwise answer in plain text, with no "
                        "<tool_call> tags.)"
                    )
                feedback_text = (
                    f"<tool_result>\n{feedback}\n</tool_result>\n"
                    f"{synthesis_instruction}"
                )
                # Anti-loop stop: replace the "please continue" instruction
                # with the stop instruction (detector notice + action log +
                # summary format) in the SAME user message, so providers
                # never see two consecutive user turns.
                if guard.tripped:
                    feedback_text = (
                        f"<tool_result>\n{feedback}\n</tool_result>\n\n"
                        f"{guard.stop_instruction()}"
                    )
                # Before storing this turn's tool results, strip the base64
                # screenshots already in history: the model saw them on the
                # turn right after their capture, so keeping them only
                # balloons the context. The fresh screenshot for THIS turn is
                # appended just below, so it survives for the next turn.
                self._drop_history_screenshots()
                if tool_screenshots and _provider_supports_vision(self.provider, self.model):
                    content_parts: list = [{"type": "text", "text": feedback_text}]
                    for shot in tool_screenshots:
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{shot}",
                            },
                        })
                    self.messages.append({"role": "user", "content": content_parts})
                else:
                    if tool_screenshots:
                        print(
                            f"[OSIA Agent] ⚠ dropping {len(tool_screenshots)} screenshot(s): "
                            f"model {self.model!r} ({self.provider}) doesn't support vision content"
                        )
                    self.messages.append({"role": "user", "content": feedback_text})
                # Anti-loop stop: no more tool rounds — produce the final
                # summary of what was done and why the agent stopped.
                if guard.tripped:
                    return self._finish_anti_loop_stop(
                        guard, on_sentence=on_sentence,
                        on_first_token=on_first_token,
                    )
                continue

            # No tool call in any format — candidate final response.
            #
            # STEP 4 — fallback honesty. Nothing was executed this round.
            # If the model nevertheless emitted unparseable tool markup
            # (malformed/truncated <tool_call>, <|python_tag|> payload, or
            # bare {"tool": ...} JSON too broken to repair), or described
            # an action in prose ("I'll open Brave now" / "I've opened
            # Firefox"), do NOT execute anything and do NOT let it pass as
            # done: log a warning and retry ONCE with an explicit
            # instruction to emit a real structured tool call.
            described = has_tool_markup(content) or describes_tool_use(content)
            if not any_tool_executed and described:
                print(
                    "[WARN] Model described tool use without structured call "
                    "— skipping execution."
                    f" (model={self.model!r}, provider={self.provider!r})"
                )
                if not hallucination_retry_used:
                    hallucination_retry_used = True
                    print(
                        "[OSIA Agent] ↻ retrying with explicit tool-call instruction"
                    )
                    self.messages.append({"role": "assistant", "content": content})
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "Your previous reply described a tool action, but it "
                            "contained no valid structured tool call, so NOTHING was "
                            "actually executed. Never describe results you have not "
                            "obtained. If the task requires a tool, emit the "
                            "<tool_call> JSON block now (format: <tool_call>"
                            '{"tool": "<name>", ...}</tool_call>); otherwise answer '
                            "plainly without claiming any action was performed."
                        ),
                    })
                    continue
                print(
                    "[OSIA Agent] ✗ unbacked tool description repeated after the "
                    "retry — returning the text as-is"
                )

            # STEP 5 — final response. ONLY NOW strip raw tool/function XML
            # tags, Llama special tokens, and residual bare {"tool": ...}
            # JSON cited as literal text (Groq Llama/Qwen, Llama 4 Scout) —
            # all execution decisions are done, so anything left is markup
            # the user must never see in mobile, history, or TTS.
            content = strip_raw_tool_tags(content)
            content = strip_bare_tool_json(content).strip()

            # Empty-response guard: stripping must never turn a model reply
            # into silence. Typical causes: a <think> block truncated by
            # max_tokens (the unclosed-tag pattern strips it to end-of-text)
            # or a reply that was nothing but echoed tool markup. Returning
            # "" here would mean an empty bubble, an empty mobile reply, and
            # no TTS — instead retry once asking for a plain-text answer,
            # then fall back to a deterministic reply.
            if not content:
                print(
                    "[WARN] final response empty after stripping "
                    f"(raw round output was {len(raw_round_output)} chars: "
                    f"{raw_round_output[:120]!r}, "
                    f"tools_executed={len(guard.actions)}, "
                    f"empty_retry_used={empty_retry_used})"
                )
                if not empty_retry_used:
                    empty_retry_used = True
                    print(
                        "[OSIA Agent] ↻ retrying with explicit "
                        "plain-text-answer instruction"
                    )
                    # Record what the model actually emitted (display paths
                    # strip this markup from history), then ask for a real
                    # answer. Never append an empty assistant frame — some
                    # providers reject it.
                    self.messages.append({
                        "role": "assistant",
                        "content": raw_round_output if raw_round_output.strip() else "…",
                    })
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "Ta réponse précédente était vide une fois le "
                            "balisage retiré (bloc <think> non fermé ou "
                            "balises d'outils). Tous les outils ont terminé. "
                            "Écris maintenant ta réponse finale complète en "
                            "te basant sur tout ce que tu as trouvé, en "
                            "texte simple, sans bloc <think> ni balise "
                            "<tool_call>."
                            if self.lang == "fr" else
                            "Your previous reply was empty once markup was "
                            "removed (an unterminated <think> block or tool "
                            "tags). All tools are done. Write your complete "
                            "final answer now based on everything you found, "
                            "in plain text, with no <think> block and no "
                            "<tool_call> tags."
                        ),
                    })
                    continue
                # Retry also produced nothing usable — deterministic
                # fallback so the user always gets a reply and TTS always
                # has text to speak. In streaming mode nothing was emitted
                # this round, so speak the fallback too.
                content = self._empty_response_fallback(guard)
                if on_sentence is not None:
                    try:
                        on_sentence(clean_response(content))
                    except Exception:
                        pass

            self.messages.append({"role": "assistant", "content": content})
            return content


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
