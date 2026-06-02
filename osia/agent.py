#!/usr/bin/env python3
"""
OSIA Core Agent — connects to local Ollama via OpenAI-compatible API
with a manual tool-calling loop that works with any model (llama3, etc.).
"""

import json
import re
import subprocess
import os
import shutil
import stat
import fnmatch
import signal
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime

import psutil
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
# System prompt — dynamic per language
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_EN = """You are a local AI assistant running on the user's local machine with FULL SYSTEM ACCESS. Reply in English by default.
You can run any shell command, read/write/delete any file, launch any application, take screenshots, and control a web browser.
Your working directory is /home/OSIA/code/osia-project/.

## Available Tools
You have these tools available:

System & shell:
- bash: Execute a bash command. Usage: {"tool": "bash", "command": "<command>"}
- run_command: Run any shell command and return its output. Usage: {"tool": "run_command", "command": "<command>"}
- open_application: Launch an application by name. Usage: {"tool": "open_application", "name": "<app_name>"}
- take_screenshot: Capture the desktop to /tmp/screenshot.png and return the path. Usage: {"tool": "take_screenshot"}
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

Browser (drives a REAL visible Chromium browser via CDP — same page the user sees; cookies/sessions persist):
- navigate: Go to a URL in the real browser. Launches Brave/Chromium with remote debugging on first use. Usage: {"tool": "navigate", "url": "<url>"}
- click: Click an element by CSS selector. Usage: {"tool": "click", "selector": "<css_selector>"}
- find_and_click: Click the first element whose visible text matches. Use when you don't know the selector but can see the label. Usage: {"tool": "find_and_click", "text": "<visible_text>"}
- fill: Set the value of an input field in one shot (no keystroke events). Usage: {"tool": "fill", "selector": "<css_selector>", "value": "<text>"}
- type_text: Focus an element and type character-by-character via the keyboard. Use for search boxes / JS-heavy forms. Usage: {"tool": "type_text", "selector": "<css_selector>", "text": "<text>"}
- press_key: Press a single keyboard key on the active page (Enter, Escape, Tab, ArrowDown, Backspace, "Control+A", …). Usage: {"tool": "press_key", "key": "Enter"}
- read_page: Visible text of the current page (truncated). Usage: {"tool": "read_page"}
- get_page_text: Full visible text of the page (higher cap than read_page). Usage: {"tool": "get_page_text"}
- wait_for_load: Wait for the page to finish loading (network idle, up to 15s). Usage: {"tool": "wait_for_load"}
- get_current_url: Return the URL of the active page. Usage: {"tool": "get_current_url"}
- scroll: Scroll by a pixel amount in a direction. Usage: {"tool": "scroll", "direction": "down", "amount": 600}
- scroll_down: Scroll down by one viewport (or by ``amount`` pixels). Usage: {"tool": "scroll_down"}
- go_back: Navigate back in history. Usage: {"tool": "go_back"}
- go_forward: Navigate forward in history. Usage: {"tool": "go_forward"}
- open_new_tab: Open a URL in a new tab and switch to it. Usage: {"tool": "open_new_tab", "url": "<url>"}
- open_browser: Open a SPECIFIC browser by name (firefox, chrome, chromium, opera, edge, brave), optionally at a URL. Use when the user asks for a particular browser ("open Firefox"). Usage: {"tool": "open_browser", "name": "firefox", "url": "<optional_url>"}

## How to Use Tools
To call a tool, output a JSON tool call wrapped in <tool_call> tags:

<tool_call>
{"tool": "open_application", "name": "spotify"}
</tool_call>

After each tool call you will receive the tool output. You may issue multiple tool calls in sequence.
When you have all the information you need, give your final answer as plain text (no <tool_call> tags)."""

SYSTEM_PROMPT_FR = """Tu es un assistant vocal IA local fonctionnant sur la machine locale de l'utilisateur avec UN ACCÈS SYSTÈME COMPLET. Réponds en français par défaut.
Tu peux exécuter n'importe quelle commande shell, lire/écrire/supprimer n'importe quel fichier, lancer n'importe quelle application, prendre des captures d'écran, et contrôler un navigateur web.
Ton répertoire de travail est /home/OSIA/code/osia-project/.

## Outils disponibles
Tu disposes des outils suivants :

Système & shell :
- bash : Exécuter une commande bash. Usage : {"tool": "bash", "command": "<commande>"}
- run_command : Exécuter n'importe quelle commande shell et retourner sa sortie. Usage : {"tool": "run_command", "command": "<commande>"}
- open_application : Lancer une application par son nom. Usage : {"tool": "open_application", "name": "<nom_app>"}
- take_screenshot : Capturer le bureau vers /tmp/screenshot.png et retourner le chemin. Usage : {"tool": "take_screenshot"}
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

Navigateur (pilote le VRAI navigateur Chromium visible via CDP — la page que voit l'utilisateur ; les cookies/sessions persistent) :
- navigate : Aller à une URL dans le vrai navigateur. Lance Brave/Chromium avec le port de debug au premier appel. Usage : {"tool": "navigate", "url": "<url>"}
- click : Cliquer sur un élément par sélecteur CSS. Usage : {"tool": "click", "selector": "<selecteur_css>"}
- find_and_click : Cliquer sur le premier élément dont le texte visible correspond. À utiliser quand tu ne connais pas le sélecteur mais que tu vois le libellé. Usage : {"tool": "find_and_click", "text": "<texte_visible>"}
- fill : Définir la valeur d'un champ en une fois (sans événements clavier). Usage : {"tool": "fill", "selector": "<selecteur_css>", "value": "<texte>"}
- type_text : Focaliser un élément et taper caractère par caractère via le clavier. Pour les champs de recherche / formulaires JS. Usage : {"tool": "type_text", "selector": "<selecteur_css>", "text": "<texte>"}
- press_key : Appuyer sur une touche du clavier sur la page active (Enter, Escape, Tab, ArrowDown, Backspace, « Control+A »…). Usage : {"tool": "press_key", "key": "Enter"}
- read_page : Texte visible de la page actuelle (tronqué). Usage : {"tool": "read_page"}
- get_page_text : Texte visible complet de la page (limite plus haute que read_page). Usage : {"tool": "get_page_text"}
- wait_for_load : Attendre la fin du chargement de la page (réseau au repos, jusqu'à 15s). Usage : {"tool": "wait_for_load"}
- get_current_url : Retourner l'URL de la page active. Usage : {"tool": "get_current_url"}
- scroll : Faire défiler d'un nombre de pixels dans une direction. Usage : {"tool": "scroll", "direction": "down", "amount": 600}
- scroll_down : Descendre d'une hauteur d'écran (ou de ``amount`` pixels). Usage : {"tool": "scroll_down"}
- go_back : Reculer dans l'historique. Usage : {"tool": "go_back"}
- go_forward : Avancer dans l'historique. Usage : {"tool": "go_forward"}
- open_new_tab : Ouvrir une URL dans un nouvel onglet et basculer dessus. Usage : {"tool": "open_new_tab", "url": "<url>"}
- open_browser : Ouvrir un navigateur PRÉCIS par son nom (firefox, chrome, chromium, opera, edge, brave), éventuellement sur une URL. À utiliser quand l'utilisateur demande un navigateur particulier (« ouvre Firefox »). Usage : {"tool": "open_browser", "name": "firefox", "url": "<url_optionnelle>"}

## Comment utiliser les outils
Pour appeler un outil, produis un appel JSON dans des balises <tool_call> :

<tool_call>
{"tool": "open_application", "name": "spotify"}
</tool_call>

Après chaque appel, tu reçois le résultat de l'outil. Tu peux enchaîner plusieurs appels.
Quand tu as toutes les informations nécessaires, donne ta réponse finale en texte simple (sans balises <tool_call>)."""


def get_system_prompt(lang: str = None, model: str = None, provider: str = None) -> str:
    """Return the system prompt for the given language, with the active
    model and provider injected at the start so the agent knows what it's
    running on (and can answer if asked).
    """
    if lang is None:
        lang = DEFAULT_LANG
    body = SYSTEM_PROMPT_FR if lang == "fr" else SYSTEM_PROMPT_EN
    if model and provider:
        header = f"Tu es OSIA. Tu tournes sur {model} ({provider}).\n\n"
        return header + body
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
    r'</?(?:tool_call|tool_result|function_calls|thinking|think|tool)>'
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


# ── Path helper (new tools only — existing tools keep their inline form) ───

_PROJECT_DIR = "/home/OSIA/code/osia-project"


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
        load1, load5, load15 = os.getloadavg()
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        uname = os.uname()
        return (
            f"host:    {uname.nodename} ({uname.sysname} {uname.release})\n"
            f"cpu:     {cpu_pct:.1f}% across {cores} cores  (load 1/5/15: {load1:.2f} / {load5:.2f} / {load15:.2f})\n"
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

def tool_open_browser(name: str, url: str | None = None) -> str:
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
# Universal tool-call parser — all providers
# ---------------------------------------------------------------------------

# Per-provider tool-call format, used to route parsing in
# ``_extract_tool_calls``. "native" providers return structured
# ``message.tool_calls`` (OpenAI-style function calling); "xml" providers
# (Groq's Llama/Qwen) emit ``<tool_call>{...}</tool_call>`` blocks as text.
# Whichever format the provider prefers is tried first; the other is always
# tried as a fallback, because the system prompt teaches the XML format to
# every provider.
TOOL_FORMAT = {
    "openai": "native",
    "anthropic": "native",
    "google": "native",
    "deepseek": "native",
    "xai": "native",
    "groq": "xml",
    "ollama": "native",
}


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


def _parse_tool_json(raw: str) -> dict | None:
    """Parse one tool-call JSON payload into ``{"tool": name, **kwargs}``.

    Accepts every shape models actually produce:
    - flat:     {"tool": "bash", "command": "ls"}
    - nested:   {"tool": "bash", "parameters": {"command": "ls"}}
                {"name": "bash", "arguments": {"command": "ls"}}
    - native:   {"function": {"name": "bash", "arguments": "{...}"}}
    - payloads with trailing prose, or truncated by max_tokens (repaired
      via balanced-brace extraction / closing-brace completion).

    Returns ``None`` when no tool name can be recovered.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Repair 1: extract the first balanced {...} (drops trailing prose).
        candidate = _extract_json_object(raw)
        if candidate:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                data = None
        # Repair 2: truncated payload — try completing the JSON.
        if data is None:
            for suffix in ('"}', '"}}', "}", "}}"):
                try:
                    data = json.loads(raw + suffix)
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
            raw_params = json.loads(raw_params)
        except json.JSONDecodeError:
            raw_params = {}
    if isinstance(raw_params, dict):
        kwargs.update(raw_params)
    return {"tool": name, **kwargs}


def _parse_xml_tool_calls(text: str) -> list:
    """Parse ``<tool_call>{...}</tool_call>`` blocks (Groq Llama/Qwen
    format) from text, including an unclosed trailing block left by
    max_tokens truncation.
    """
    if not text:
        return []
    calls = []
    closed_end = 0
    for m in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL):
        call = _parse_tool_json(m.group(1))
        if call:
            calls.append(call)
        closed_end = m.end()
    # Unclosed trailing block — only look after the last closed block so
    # nothing is parsed twice.
    m = re.search(r"<tool_call>\s*(\{.*)$", text[closed_end:], re.DOTALL)
    if m:
        call = _parse_tool_json(m.group(1))
        if call:
            calls.append(call)
    return calls


def _parse_native_tool_calls(message) -> list:
    """Convert native structured tool calls (``message.tool_calls`` —
    OpenAI/Anthropic/DeepSeek/Google/Ollama/xAI OpenAI-compatible APIs)
    into ``{"tool": name, **kwargs}`` dicts. Returns ``[]`` when the
    message carries no structured calls.
    """
    if message is None:
        return []
    tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls and isinstance(message, dict):
        tool_calls = message.get("tool_calls")
    if not tool_calls:
        return []
    calls = []
    for tc in tool_calls:
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
    return calls


def _extract_tool_calls(text: str, message=None, provider: str = None) -> list:
    """Universal tool-call parser — handles every provider format.

    Returns a list of ``{"tool": name, **kwargs}`` dicts ready for
    ``TOOL_MAP`` dispatch, in execution order.

    Formats handled:
    1. Native structured tool calls (``message.tool_calls`` — OpenAI,
       Anthropic, DeepSeek, Google, Ollama, xAI).
    2. XML ``<tool_call>{...}</tool_call>`` text blocks (Groq Llama/Qwen),
       flat or nested-``parameters`` JSON, including truncated blocks.
    3. Llama special tokens (``<|python_tag|>…``): NEVER executed — they
       are stripped from the final output only (``strip_raw_tool_tags``).
    4. Plain-text descriptions of actions ("I'll open Brave now"): nothing
       is extracted — the text is passed as-is to the user, never executed.

    ``TOOL_FORMAT[provider]`` decides which format is tried first; the
    other always serves as fallback.
    """
    fmt = TOOL_FORMAT.get((provider or "").lower(), "native")
    native_calls = _parse_native_tool_calls(message)
    xml_calls = _parse_xml_tool_calls(text or "")
    if fmt == "xml":
        return xml_calls or native_calls
    return native_calls or xml_calls


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
    a ``<tool_call>`` tag or a Llama ``<|python_tag|>`` payload — whether
    or not it parses into an executable call. Used for fallback-honesty
    warnings when nothing executable was extracted.
    """
    if not text:
        return False
    return bool(re.search(r"<tool_call>|<\|python_tag\|>", text))


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


class OSIAAgent:
    def __init__(self, model: str = MODEL, base_url: str = OLLAMA_BASE_URL, lang: str = None):
        self.client = OpenAI(base_url=base_url, api_key="ollama")
        self.model = model
        self.provider = "ollama"
        self.lang = lang if lang else DEFAULT_LANG
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

    def set_model(self, model_id: str) -> None:
        """Switch the agent to a new model, reconfiguring the client for that
        model's provider (Anthropic, OpenAI, DeepSeek, Google, xAI, Groq, Ollama).
        """
        from osia.models import provider_for_model, PROVIDERS
        from osia.config import get_api_key

        provider = provider_for_model(model_id)
        cfg = PROVIDERS.get(provider, PROVIDERS["ollama"])
        api_key = get_api_key(provider) if provider != "ollama" else "ollama"

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

        self.client = OpenAI(
            base_url=cfg["base_url"],
            api_key=api_key,
            default_headers=default_headers,
        )
        self.model = model_id
        self.provider = provider
        # Refresh the system message so the model/provider line at the
        # top of the prompt reflects the newly-selected model.
        if self.messages and isinstance(self.messages[0], dict) and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = get_system_prompt(self.lang, self.model, self.provider)
        print(f"[DEBUG] provider: {self.provider}, model: {self.model}")

    def _stream_deltas(self, max_tokens: int | None):
        """Yield text deltas from the OpenAI-compatible chat-completions
        stream, abstracting it so the sentence-splitting loop stays simple.
        """
        create_kwargs = {
            "model": self.model,
            "messages": self.messages,
            "stream": True,
        }
        if max_tokens is not None:
            create_kwargs["max_tokens"] = max_tokens
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

    def _stream_with_sentences(self, on_sentence, max_tokens: int | None = None,
                               on_first_token=None) -> str:
        """Stream a chat completion, emitting each complete sentence to
        ``on_sentence`` as it arrives. Returns the full accumulated content
        so the outer loop can still detect tool calls.

        Anything inside a ``<tool_call>...</tool_call>`` block is buffered
        but never emitted — we don't want to speak tool markup. The same
        goes for ``<think>...</think>`` / ``<thinking>...</thinking>``
        reasoning blocks (Qwen 3, DeepSeek-R1, …): they are kept in the
        returned content but never spoken or streamed to the user.

        If ``on_first_token`` is provided it is invoked (once per call)
        the first time a non-empty content delta arrives from the model.
        Useful for latency profiling.
        """
        full = ""
        pending = ""       # raw text not yet routed to speakable/tool
        speakable = ""     # awaiting a sentence-ending punctuation char
        in_tool_call = False
        think_close = None  # closing tag of the reasoning block we're inside, if any
        # Once True, the model has committed to a tool this round. Anything
        # it says afterwards is ungrounded (the tool hasn't actually run
        # yet) — we still feed it into `full` so the outer loop sees the
        # tool_call markup, but we MUST NOT speak it. The next round, run
        # after the real tool result is fed back, produces the spoken reply.
        tool_call_seen = False
        first_token_fired = False

        for delta in self._stream_deltas(max_tokens):
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
                    close = pending.find("</tool_call>")
                    if close == -1:
                        pending = ""  # still inside tool call, discard
                        break
                    pending = pending[close + len("</tool_call>"):]
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

                # Locate the earliest special tag in the buffer.
                tool_idx = pending.find("<tool_call>")
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
                pending = pending[tool_idx + len("<tool_call>"):]
                in_tool_call = True
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

    def run(self, user_input: str, max_tool_rounds: int = 10, on_sentence=None,
            history: list | None = None, max_tokens: int | None = 512,
            on_first_token=None) -> str:
        """Run the agent loop: send user input, handle tool calls, return final response.

        Each round runs the tool pipeline in strict order: ``<think>``
        blocks are stripped first, tool calls are parsed with the universal
        parser (native structured calls + XML blocks, routed per provider
        via ``TOOL_FORMAT``) and executed BEFORE any tag stripping, and raw
        tool markup is stripped only from the final text once all execution
        decisions are done.

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

        ``max_tokens`` caps the response length of each round (default
        512, sized for short conversational replies). Pass ``None`` to
        leave it unset.
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

        # Hallucinated-tool-claim guard (Llama 4 Scout & co): tracks whether
        # any tool ACTUALLY ran this turn, and whether the one allowed
        # "you never called the tool" retry has been spent.
        any_tool_executed = False
        hallucination_retry_used = False

        for round_num in range(max_tool_rounds):
            try:
                if on_sentence is not None:
                    # on_first_token only fires the first time a delta arrives
                    # in this round. The server-side flag dedupes across rounds.
                    # Streaming aggregates text only — no native message object.
                    message = None
                    content = self._stream_with_sentences(
                        on_sentence,
                        max_tokens=max_tokens,
                        on_first_token=on_first_token,
                    )
                else:
                    create_kwargs = {
                        "model": self.model,
                        "messages": self.messages,
                    }
                    if max_tokens is not None:
                        create_kwargs["max_tokens"] = max_tokens
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

            # STEP 1 — strip internal reasoning blocks (<think>…</think>)
            # FIRST: the user must never see chain-of-thought, and a tool
            # call only deliberated inside reasoning must not be executed.
            content = strip_reasoning_blocks(content)

            # STEP 2 — parse tool calls with the universal parser (native
            # structured calls + XML blocks, routed per provider via
            # TOOL_FORMAT). The content is still un-stripped here.
            tool_calls = _extract_tool_calls(
                content, message=message, provider=self.provider,
            )

            # STEP 3 — execute the parsed tool calls.
            if tool_calls:
                # Record the assistant turn. Native structured calls can
                # arrive with empty text content — serialize them as XML
                # blocks so the model sees what it called on the next round
                # (display paths strip these blocks before rendering).
                assistant_record = content
                if not has_tool_call(assistant_record):
                    blocks = "\n".join(
                        f"<tool_call>\n{json.dumps(tc, ensure_ascii=False)}\n</tool_call>"
                        for tc in tool_calls
                    )
                    assistant_record = (
                        f"{content}\n{blocks}".strip() if content.strip() else blocks
                    )
                self.messages.append({"role": "assistant", "content": assistant_record})

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
                feedback_text = (
                    f"<tool_result>\n{feedback}\n</tool_result>\n"
                    "Please continue with your response based on these results."
                )
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
                continue

            # No structured tool call — candidate final response.
            #
            # STEP 4 — fallback honesty. Nothing was executed this round.
            # If the model nevertheless emitted unparseable tool markup
            # (malformed/truncated <tool_call>, a <|python_tag|> payload —
            # never executed by design), or described an action in prose
            # ("I'll open Brave now" / "I've opened Firefox"), do NOT
            # execute anything and do NOT let it pass as done: log a
            # warning and retry ONCE with an explicit instruction to emit
            # a real structured tool call.
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
            # tags and Llama special tokens cited as literal text (Groq
            # Llama/Qwen, Llama 4 Scout) — all execution decisions are done,
            # so anything left is markup the user must never see in mobile,
            # history, or TTS.
            content = strip_raw_tool_tags(content)
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
