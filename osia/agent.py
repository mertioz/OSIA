#!/usr/bin/env python3
"""
OSIA Core Agent — connects to local Ollama via OpenAI-compatible API
with a manual tool-calling loop that works with any model (llama3, etc.).
"""

import ast
import contextvars
import json
import re
import subprocess
import sys
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

    Returns one of the supported language codes; falls back to ``"en"`` if
    detection raises or yields a value outside the supported list.
    """
    supported = ("en", "fr", "zh", "ja", "de", "es")
    try:
        lang_env = os.environ.get("LANG", "") or ""
        # e.g. "fr_FR.UTF-8" / "zh_CN.UTF-8" -> "fr" / "zh"
        code = lang_env.split("_", 1)[0].split(".", 1)[0].strip().lower()
        if code in supported:
            return code
    except Exception:
        pass
    return "en"


DEFAULT_LANG = detect_system_lang()

# ---------------------------------------------------------------------------
# System prompt — dynamic per language
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_EN = """You are a local AI assistant running on the user's local machine with FULL SYSTEM ACCESS. Reply in English by default.
You can run any shell command, read/write/delete any file, launch any application, take screenshots, and control a web browser.
Your working directory is /home/OSIA/code/osia-project/.
When the user refers to the "desktop" or "bureau", it means the desktop folder — ~/Bureau on French Linux systems and ~/Desktop on English ones. Always use the exact absolute path given in the [DESKTOP PATH: ...] line of this prompt.

## What you can accomplish
OSIA is a general computer operator, not a chatbot — most real tasks need several tools in sequence, so plan the outcome you want, then chain tools to reach it.
- **Reach:** inspect and change the machine (shell, files, processes, volume, clipboard); operate the web through a real browser in multi-step flows (navigate → read_page → click/fill → verify); and drive native apps the browser can't reach (desktop_* + take_screenshot).
- **Chain in steps:** decompose the task — e.g. "open an app and do X" = open_application → take_screenshot (see the state) → desktop_click/desktop_type → take_screenshot (confirm).
- **Gather context first:** read_file/list_files/get_system_info to learn the real state instead of guessing.
- **Verify the REAL outcome before claiming success — DETERMINISTIC first, vision LAST.** After an action, confirm it IN THIS ORDER: (1) process / window — is_running / has_window; (2) file / manifest state — steam_game_state (is the game installed AND now running?), is_installed; (3) page state — get_current_url / read_page / web_state. Use take_screenshot (vision) ONLY when no deterministic check can confirm the outcome. Firing a deep link (open_uri) or spawning a process (open_application) is an ATTEMPT, not a confirmation — read the tool result, which tells you whether it was actually confirmed. If you cannot verify, say so honestly — NEVER claim a success you did not verify. Example: asked to "launch Elden Ring" when it is NOT installed, open_uri("steam://run/<appid>") only FIRES the link and launches nothing — steam_game_state shows it isn't installed, so tell the user it isn't installed and offer to install it; do NOT claim you launched it.
- **Recover from real failures, but stay BOUNDED:** when an action or page genuinely fails (won't load, element missing), don't give up at once — escalate through a FINITE sequence of ~3 steps: (1) retry once — reload/redo, then re-verify; (2) if it still fails, try ONE alternative — open a new tab and web-search (e.g. "<site> informations") to find the site or the cause; (3) if that fails too, STOP and tell the user honestly that it didn't work and why. This is a LIMITED number of attempts, NOT endless persistence — never repeat the same failing action over and over. Apply this to genuine failures, not to every minor step.
- **Prefer DETERMINISTIC mechanisms first.** To open or navigate an app, reach for them IN ORDER: (1) a deep link / URL scheme via open_uri (e.g. steam://open/games, spotify:, vscode://file/<path>); (2) the app's CLI / command-line arguments; (3) a keyboard shortcut (desktop_press). Use desktop_click (vision/mouse) ONLY as a LAST RESORT, when none of the above can reach the goal — it is the slowest and least reliable path; never use it to reach a destination a deep link can open directly. Example: "open Steam and go to the library" = open_application("steam") then open_uri("steam://open/games") — NOT desktop_click("Library tab"). Steam known routes: library (steam://open/games), friends, console, downloads, settings, store/<appid>, run/<appid>.
- On a web page, ALWAYS use the Browser tools (click / find_and_click) to click — they target the real page DOM and are far more precise than desktop_click. Use desktop_* ONLY for native windows (file managers, settings panes, dialogs, games), never for anything inside a browser tab.

## Available Tools
You have these tools available:

System & shell:
- bash: Execute a bash command (on Windows it runs in PowerShell — use PowerShell syntax there). Usage: {"tool": "bash", "command": "<command>"}
- run_command: Run any shell command and return its output. Usage: {"tool": "run_command", "command": "<command>"}
- open_application: Launch an installed application by its common name — OSIA finds the real executable automatically (Start menu, registry, common install folders, Store apps), so just pass a plain name like "Discord", "Spotify", "Steam", "Chrome", "VS Code", "OBS" or "Word"; no path needed (an absolute path also works). Usage: {"tool": "open_application", "name": "<app_name>"}
- open_uri: Open an app DESTINATION directly via its URL scheme / deep link — the FAST, deterministic way (no mouse, no vision). E.g. "steam://open/games" jumps Steam to the library; "steam://open/friends" / "steam://open/console" / "steam://open/downloads" / "steam://settings" / "steam://store/<appid>" / "steam://run/<appid>" reach those views; "spotify:" focuses Spotify and "spotify:search:<query>" searches it; "vscode://file/<path>" opens a file in VS Code. PREFER this over desktop_click whenever a deep link can reach the destination. Only whitelisted schemes are allowed; file:/javascript:/data:/vbscript: and bare paths are refused. Usage: {"tool": "open_uri", "uri": "steam://open/games"}
- take_screenshot: Capture the desktop and return a description of what is visible on screen (the screenshot is analyzed by the vision model — you CAN see the screen with this). Usage: {"tool": "take_screenshot"}
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
- click: Click an element by CSS selector. PRECISE (drives the real DOM) — on a web page PREFER this (or find_and_click) over desktop_click. Usage: {"tool": "click", "selector": "<css_selector>"}
- find_and_click: Click the element whose visible label matches. Use when you don't know the selector but can see the label. PRECISE (real DOM, accessible-name match) — this is the PREFERRED way to click on a web page; use it instead of desktop_click for buttons/links/menu items inside a tab. Usage: {"tool": "find_and_click", "text": "<visible_text>"}
- fill: Set the value of an input field in one shot (no keystroke events). Usage: {"tool": "fill", "selector": "<css_selector>", "value": "<text>"}
- type_text: Focus an element and type character-by-character via the keyboard. Use for search boxes / JS-heavy forms. Usage: {"tool": "type_text", "selector": "<css_selector>", "text": "<text>"}
- press_key: Press a single keyboard key on the active page (Enter, Escape, Tab, ArrowDown, Backspace, "Control+A", …). Usage: {"tool": "press_key", "key": "Enter"}
- read_page: Visible text of the current page (truncated). Usage: {"tool": "read_page"}
- get_page_text: Full visible text of the page (higher cap than read_page). Usage: {"tool": "get_page_text"}
- wait_for_load: Wait for the active page's DOM to be ready (domcontentloaded, max 8s). Usually unnecessary — navigate already waits — but useful for pages that re-render after the initial load. Usage: {"tool": "wait_for_load"}
- get_current_url: Return the URL of the active page. Usage: {"tool": "get_current_url"}
- scroll: Scroll by a pixel amount in a direction. Usage: {"tool": "scroll", "direction": "down", "amount": 600}
- scroll_down: Scroll down by one viewport (or by ``amount`` pixels). Usage: {"tool": "scroll_down"}
- go_back: Navigate back in history. Usage: {"tool": "go_back"}
- go_forward: Navigate forward in history. Usage: {"tool": "go_forward"}
- open_new_tab: Open a URL in a new tab and switch to it. Usage: {"tool": "open_new_tab", "url": "<url>"}
- open_browser: Open a SPECIFIC browser by name (firefox, chrome, chromium, opera, edge, brave), optionally at a URL. Use when the user asks for a particular browser ("open Firefox"). Firefox is FULLY supported for automation via WebDriver BiDi: after opening it, all browser tools (navigate, click, fill, type_text, read_page, screenshots, …) work inside Firefox exactly like Brave/Chrome. Usage: {"tool": "open_browser", "name": "firefox", "url": "<optional_url>"}

Desktop automation (control NATIVE desktop apps — file managers, settings panes, native dialogs, games — by looking at the screen and using the REAL mouse and keyboard. These are for windows that are NOT web pages. For ANYTHING inside a web page, use the Browser tools above (find_and_click / click), NOT these — the Browser tools click the real DOM and are far more precise):
- desktop_click: Look at the screen and click a UI element you describe in plain words. OSIA captures the screen, the vision model locates the element, then clicks its center. For NATIVE windows only — on a web page use find_and_click / click instead (real DOM, far more precise). Usage: {"tool": "desktop_click", "description": "the Create button"} — After a desktop_click, read the BEFORE/AFTER descriptions in the result carefully. If AFTER does not show the expected result (e.g. the file selected/opened), tell the user the click may have missed and offer to retry.
- desktop_type: Type a string of text into the currently focused field via the real keyboard. Usage: {"tool": "desktop_type", "text": "<text>"}
- desktop_press: Press a single key or a shortcut system-wide on the focused window (Enter, Escape, Tab, "ctrl+s", "alt+F4", …). Usage: {"tool": "desktop_press", "key": "Enter"}
- desktop_scroll: Scroll the active window in a direction by a number of notches. Usage: {"tool": "desktop_scroll", "direction": "down", "amount": 3}

Verification (DETERMINISTIC — confirm an outcome WITHOUT a screenshot; PREFER these over take_screenshot to check whether an action actually worked. A "no" answer is a real, trustworthy result, not an error):
- is_running: Is an application's PROCESS currently running? Deterministic (process table via psutil), no vision. Usage: {"tool": "is_running", "app": "discord"}
- is_installed: Is an application installed (its executable resolvable)? Deterministic, no vision (Windows). Usage: {"tool": "is_installed", "app": "steam"}
- has_window: Does an application have a visible WINDOW right now? Deterministic, no vision (Windows). Usage: {"tool": "has_window", "app": "spotify"}
- steam_game_state: Deterministic install/run state of a Steam game by name or appid — reads Steam's own manifests (no vision). Tells you whether the game is INSTALLED and whether it is RUNNING, so you never claim you launched a game that isn't installed. Usage: {"tool": "steam_game_state", "name": "Elden Ring"}
- web_state: Deterministically check the live browser page WITHOUT vision — its current URL and whether it contains an expected URL fragment / text / element. Usage: {"tool": "web_state", "expect_url": "youtube.com", "expect_text": "Subscribe"}

## How to Use Tools
To call a tool, output a JSON tool call wrapped in <tool_call> tags:

<tool_call>
{"tool": "open_application", "name": "spotify"}
</tool_call>

After each tool call you will receive the tool output. You may issue multiple tool calls in sequence.
When you have all the information you need, give your final answer as plain text (no <tool_call> tags).
When all tools are done, you MUST write a complete final answer based on everything you found — never end your turn with only tool calls, tool markup, or an empty reply.

## Honesty about tool results (CRITICAL)
Tool results are facts — never contradict them or invent outcomes you did not see.
- If a tool result starts with "ERROR:" or "⚠️ TOOL FAILED", the action did NOT happen. You MUST tell the user the action failed and exactly what the error was. NEVER say or imply it succeeded.
- Never claim you opened, installed, created, sent, or changed something unless the tool result confirms it. If you did not call a tool, you did not do the action.
- If you are unsure whether an action succeeded, say so honestly instead of guessing."""

SYSTEM_PROMPT_FR = """Tu es un assistant vocal IA local fonctionnant sur la machine locale de l'utilisateur avec UN ACCÈS SYSTÈME COMPLET. Réponds en français par défaut.
Tu peux exécuter n'importe quelle commande shell, lire/écrire/supprimer n'importe quel fichier, lancer n'importe quelle application, prendre des captures d'écran, et contrôler un navigateur web.
Ton répertoire de travail est /home/OSIA/code/osia-project/.
Quand l'utilisateur parle du « bureau » (ou « desktop »), il s'agit du dossier Bureau — ~/Bureau sur les systèmes Linux français et ~/Desktop sur les anglais. Utilise toujours le chemin absolu exact donné dans la ligne [DESKTOP PATH: ...] de ce prompt.

## Ce que tu peux accomplir
OSIA est un opérateur informatique généraliste, pas un chatbot — la plupart des tâches réelles nécessitent plusieurs outils à la suite : planifie le résultat visé, puis enchaîne les outils pour l'atteindre.
- **Portée :** inspecter et modifier la machine (shell, fichiers, processus, volume, presse-papiers) ; piloter le web via un vrai navigateur en plusieurs étapes (navigate → read_page → click/fill → vérifier) ; et piloter les applis natives hors de portée du navigateur (desktop_* + take_screenshot).
- **Enchaîne par étapes :** décompose la tâche — ex. « ouvrir une appli et faire X » = open_application → take_screenshot (voir l'état) → desktop_click/desktop_type → take_screenshot (confirmer).
- **Récupère le contexte d'abord :** read_file/list_files/get_system_info pour connaître l'état réel au lieu de deviner.
- **Vérifie le VRAI résultat avant d'annoncer un succès — DÉTERMINISTE d'abord, vision en DERNIER.** Après une action, confirme-la DANS CET ORDRE : (1) processus / fenêtre — is_running / has_window ; (2) état fichier / manifeste — steam_game_state (le jeu est-il installé ET maintenant lancé ?), is_installed ; (3) état de la page — get_current_url / read_page / web_state. N'utilise take_screenshot (vision) QUE lorsqu'aucune vérification déterministe ne peut confirmer le résultat. Déclencher un lien profond (open_uri) ou lancer un processus (open_application) est une TENTATIVE, pas une confirmation — lis le résultat de l'outil, qui te dit si c'est réellement confirmé. Si tu ne peux pas vérifier, dis-le honnêtement — n'affirme JAMAIS un succès que tu n'as pas vérifié. Exemple : si on te demande de « lancer Elden Ring » alors qu'il n'est PAS installé, open_uri(« steam://run/<appid> ») ne fait que DÉCLENCHER le lien et ne lance rien — steam_game_state montre qu'il n'est pas installé : dis à l'utilisateur qu'il n'est pas installé et propose de l'installer ; n'affirme PAS l'avoir lancé.
- **Récupère après un vrai échec, mais reste BORNÉ :** quand une action ou une page échoue vraiment (ne charge pas, élément introuvable), n'abandonne pas tout de suite — escalade selon une séquence FINIE d'environ 3 étapes : (1) réessaie une fois — recharge/refais, puis re-vérifie ; (2) si ça échoue encore, tente UNE alternative — ouvre un nouvel onglet et fais une recherche web (ex. « <site> informations ») pour retrouver le site ou la cause ; (3) si ça échoue aussi, ARRÊTE et dis honnêtement à l'utilisateur que ça n'a pas marché et pourquoi. C'est un nombre LIMITÉ de tentatives, PAS une persistance infinie — ne répète jamais la même action qui échoue en boucle. Applique ceci aux vrais échecs, pas à chaque petite étape.
- **Privilégie d'abord les mécanismes DÉTERMINISTES.** Pour ouvrir une appli ou y naviguer, utilise-les DANS CET ORDRE : (1) un lien profond / schéma d'URL via open_uri (ex. steam://open/games, spotify:, vscode://file/<chemin>) ; (2) la CLI / les arguments en ligne de commande de l'appli ; (3) un raccourci clavier (desktop_press). N'utilise desktop_click (vision/souris) QU'EN DERNIER RECOURS, quand aucun des moyens ci-dessus ne peut atteindre le but — c'est la voie la plus lente et la moins fiable ; ne l'utilise jamais pour atteindre une destination qu'un lien profond ouvre directement. Exemple : « ouvre Steam et va dans la bibliothèque » = open_application(« steam ») puis open_uri(« steam://open/games ») — PAS desktop_click(« onglet Bibliothèque »). Routes Steam connues : bibliothèque (steam://open/games), friends, console, downloads, settings, store/<appid>, run/<appid>.
- Dans une page web, utilise TOUJOURS les outils Navigateur (click / find_and_click) pour cliquer — ils ciblent le vrai DOM de la page et sont bien plus précis que desktop_click. N'utilise desktop_* QUE pour les fenêtres natives (gestionnaires de fichiers, paramètres, boîtes de dialogue, jeux), jamais pour quoi que ce soit dans un onglet du navigateur.

## Outils disponibles
Tu disposes des outils suivants :

Système & shell :
- bash : Exécuter une commande bash (sous Windows elle s'exécute dans PowerShell — utilise la syntaxe PowerShell). Usage : {"tool": "bash", "command": "<commande>"}
- run_command : Exécuter n'importe quelle commande shell et retourner sa sortie. Usage : {"tool": "run_command", "command": "<commande>"}
- open_application : Lancer une application installée par son nom courant — OSIA retrouve automatiquement l'exécutable réel (menu Démarrer, registre, dossiers d'installation courants, applis du Store) : passe simplement un nom comme « Discord », « Spotify », « Steam », « Chrome », « VS Code », « OBS » ou « Word » ; aucun chemin requis (un chemin absolu fonctionne aussi). Usage : {"tool": "open_application", "name": "<nom_app>"}
- open_uri : Ouvrir directement une DESTINATION d'appli via son schéma d'URL / lien profond — la voie RAPIDE et déterministe (sans souris ni vision). Ex. « steam://open/games » amène Steam à la bibliothèque ; « steam://open/friends » / « steam://open/console » / « steam://open/downloads » / « steam://settings » / « steam://store/<appid> » / « steam://run/<appid> » atteignent ces vues ; « spotify: » met Spotify au premier plan et « spotify:search:<requête> » y lance une recherche ; « vscode://file/<chemin> » ouvre un fichier dans VS Code. PRÉFÈRE cet outil à desktop_click dès qu'un lien profond peut atteindre la destination. Seuls les schémas autorisés passent ; file:/javascript:/data:/vbscript: et les chemins nus sont refusés. Usage : {"tool": "open_uri", "uri": "steam://open/games"}
- take_screenshot : Capturer le bureau et retourner une description de ce qui est visible à l'écran (la capture est analysée par le modèle de vision — tu PEUX voir l'écran avec cet outil). Usage : {"tool": "take_screenshot"}
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
- click : Cliquer sur un élément par sélecteur CSS. PRÉCIS (pilote le vrai DOM) — dans une page web, PRÉFÈRE cet outil (ou find_and_click) à desktop_click. Usage : {"tool": "click", "selector": "<selecteur_css>"}
- find_and_click : Cliquer sur l'élément dont le libellé visible correspond. À utiliser quand tu ne connais pas le sélecteur mais que tu vois le libellé. PRÉCIS (vrai DOM, correspondance par nom accessible) — c'est la façon PRÉFÉRÉE de cliquer dans une page web ; utilise-le au lieu de desktop_click pour les boutons/liens/éléments de menu d'un onglet. Usage : {"tool": "find_and_click", "text": "<texte_visible>"}
- fill : Définir la valeur d'un champ en une fois (sans événements clavier). Usage : {"tool": "fill", "selector": "<selecteur_css>", "value": "<texte>"}
- type_text : Focaliser un élément et taper caractère par caractère via le clavier. Pour les champs de recherche / formulaires JS. Usage : {"tool": "type_text", "selector": "<selecteur_css>", "text": "<texte>"}
- press_key : Appuyer sur une touche du clavier sur la page active (Enter, Escape, Tab, ArrowDown, Backspace, « Control+A »…). Usage : {"tool": "press_key", "key": "Enter"}
- read_page : Texte visible de la page actuelle (tronqué). Usage : {"tool": "read_page"}
- get_page_text : Texte visible complet de la page (limite plus haute que read_page). Usage : {"tool": "get_page_text"}
- wait_for_load : Attendre que le DOM de la page active soit prêt (domcontentloaded, max 8 s). Généralement inutile — navigate attend déjà — mais utile pour les pages qui se re-rendent après le chargement initial. Usage : {"tool": "wait_for_load"}
- get_current_url : Retourner l'URL de la page active. Usage : {"tool": "get_current_url"}
- scroll : Faire défiler d'un nombre de pixels dans une direction. Usage : {"tool": "scroll", "direction": "down", "amount": 600}
- scroll_down : Descendre d'une hauteur d'écran (ou de ``amount`` pixels). Usage : {"tool": "scroll_down"}
- go_back : Reculer dans l'historique. Usage : {"tool": "go_back"}
- go_forward : Avancer dans l'historique. Usage : {"tool": "go_forward"}
- open_new_tab : Ouvrir une URL dans un nouvel onglet et basculer dessus. Usage : {"tool": "open_new_tab", "url": "<url>"}
- open_browser : Ouvrir un navigateur PRÉCIS par son nom (firefox, chrome, chromium, opera, edge, brave), éventuellement sur une URL. À utiliser quand l'utilisateur demande un navigateur particulier (« ouvre Firefox »). Firefox est ENTIÈREMENT pris en charge pour l'automatisation via WebDriver BiDi : après l'avoir ouvert, tous les outils navigateur (navigate, click, fill, type_text, read_page, captures d'écran…) fonctionnent dans Firefox exactement comme avec Brave/Chrome. Usage : {"tool": "open_browser", "name": "firefox", "url": "<url_optionnelle>"}

Automatisation du bureau (piloter les applis NATIVES — gestionnaires de fichiers, paramètres, boîtes de dialogue natives, jeux — en regardant l'écran et en utilisant la VRAIE souris et le VRAI clavier. Ces outils sont pour les fenêtres qui ne sont PAS des pages web. Pour TOUT ce qui est dans une page web, utilise les outils Navigateur ci-dessus (find_and_click / click), PAS ceux-ci — les outils Navigateur cliquent le vrai DOM et sont bien plus précis) :
- desktop_click : Regarder l'écran et cliquer sur un élément d'interface que tu décris en mots. OSIA capture l'écran, le modèle de vision localise l'élément, puis clique en son centre. Pour les fenêtres NATIVES uniquement — dans une page web, utilise plutôt find_and_click / click (vrai DOM, bien plus précis). Usage : {"tool": "desktop_click", "description": "le bouton Créer"} — Après un desktop_click, lis attentivement les descriptions BEFORE/AFTER du résultat. Si AFTER ne montre pas le résultat attendu (par ex. le fichier sélectionné/ouvert), dis à l'utilisateur que le clic a peut-être raté et propose de réessayer.
- desktop_type : Taper une chaîne de texte dans le champ actuellement focalisé via le vrai clavier. Usage : {"tool": "desktop_type", "text": "<texte>"}
- desktop_press : Appuyer sur une touche ou un raccourci au niveau système sur la fenêtre active (Enter, Escape, Tab, « ctrl+s », « alt+F4 »…). Usage : {"tool": "desktop_press", "key": "Enter"}
- desktop_scroll : Faire défiler la fenêtre active dans une direction d'un certain nombre de crans. Usage : {"tool": "desktop_scroll", "direction": "down", "amount": 3}

Vérification (DÉTERMINISTE — confirmer un résultat SANS capture d'écran ; PRÉFÈRE ces outils à take_screenshot pour vérifier qu'une action a réellement marché. Une réponse « non » est un vrai résultat fiable, pas une erreur) :
- is_running : Le PROCESSUS d'une application tourne-t-il actuellement ? Déterministe (table des processus via psutil), sans vision. Usage : {"tool": "is_running", "app": "discord"}
- is_installed : Une application est-elle installée (son exécutable est-il résolvable) ? Déterministe, sans vision (Windows). Usage : {"tool": "is_installed", "app": "steam"}
- has_window : Une application a-t-elle une FENÊTRE visible en ce moment ? Déterministe, sans vision (Windows). Usage : {"tool": "has_window", "app": "spotify"}
- steam_game_state : État d'installation/d'exécution déterministe d'un jeu Steam par nom ou appid — lit les manifests de Steam (sans vision). Te dit si le jeu est INSTALLÉ et s'il TOURNE, pour ne jamais prétendre avoir lancé un jeu qui n'est pas installé. Usage : {"tool": "steam_game_state", "name": "Elden Ring"}
- web_state : Vérifier la page du navigateur en direct SANS vision — son URL actuelle et si elle contient un fragment d'URL / un texte / un élément attendu. Usage : {"tool": "web_state", "expect_url": "youtube.com", "expect_text": "Subscribe"}

## Comment utiliser les outils
Pour appeler un outil, produis un appel JSON dans des balises <tool_call> :

<tool_call>
{"tool": "open_application", "name": "spotify"}
</tool_call>

Après chaque appel, tu reçois le résultat de l'outil. Tu peux enchaîner plusieurs appels.
Quand tu as toutes les informations nécessaires, donne ta réponse finale en texte simple (sans balises <tool_call>).
Quand tous les outils ont terminé, tu DOIS écrire une réponse finale complète basée sur tout ce que tu as trouvé — ne termine jamais ton tour avec seulement des appels d'outils, du balisage d'outil ou une réponse vide.

## Honnêteté sur les résultats d'outils (CRITIQUE)
Les résultats d'outils sont des faits — ne les contredis jamais et n'invente pas de résultats que tu n'as pas vus.
- Si un résultat d'outil commence par « ERROR: » ou « ⚠️ TOOL FAILED », l'action n'a PAS eu lieu. Tu DOIS dire à l'utilisateur que l'action a échoué et exactement quelle était l'erreur. N'affirme JAMAIS qu'elle a réussi.
- N'affirme jamais avoir ouvert, installé, créé, envoyé ou modifié quelque chose sauf si le résultat de l'outil le confirme. Si tu n'as pas appelé d'outil, tu n'as pas fait l'action.
- Si tu n'es pas sûr qu'une action a réussi, dis-le honnêtement au lieu de deviner."""


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

DEFAULT_BASH_TIMEOUT_SECONDS = 30
# Slow install/download/clone commands get a much larger time budget than the
# default so a slow machine never reports a false timeout while the work is
# still in progress (WOR-31). Both the command's own timeout AND the agent's
# wall-clock tool budget (see ``_tool_timeout_seconds``) are raised for these.
LONG_RUNNING_TIMEOUT_SECONDS = 300
# Install/download/clone keywords. Word-boundaried so a substring inside an
# unrelated word (e.g. "adapter" containing "apt") never triggers the extended
# budget by accident.
_LONG_RUNNING_PATTERNS = (
    re.compile(r"\bpip[0-9]*\s+install\b", re.IGNORECASE),
    re.compile(r"\bnpm\s+(install|i|ci)\b", re.IGNORECASE),
    re.compile(r"\bapt(-get)?\b", re.IGNORECASE),
    re.compile(r"\bdnf\s+install\b", re.IGNORECASE),
    re.compile(r"\bwget\b", re.IGNORECASE),
    re.compile(r"\bcurl\b.*-O\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clone\b", re.IGNORECASE),
)


def _is_long_running_command(command: str) -> bool:
    """True when a shell command looks like a slow install/download/clone and
    therefore deserves the extended timeout (WOR-31). Non-string input (a
    malformed tool call) is never long-running — it falls through to the
    default budget and the executor reports the real error."""
    if not command or not isinstance(command, str):
        return False
    return any(p.search(command) for p in _LONG_RUNNING_PATTERNS)


def tool_bash(command: str) -> str:
    """Execute a bash command and return stdout + stderr (truncated).

    On Windows the command is run in PowerShell (not bash/sh), so use
    PowerShell syntax there.

    Install/download/clone commands (``pip install``, ``npm install``,
    ``apt``, ``dnf install``, ``wget``, ``curl -O``, ``git clone``) are
    detected as long-running: they get a 300 s budget instead of 30 s and the
    result reports how long the command actually took, so a slow install is
    waited out instead of failing blindly (WOR-31).
    """
    if not _is_long_running_command(command):
        # Fast path: short commands keep the simple, proven blocking run.
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", command],
                    capture_output=True, text=True,
                    timeout=DEFAULT_BASH_TIMEOUT_SECONDS, cwd=_PROJECT_DIR,
                )
            else:
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True,
                    timeout=DEFAULT_BASH_TIMEOUT_SECONDS, cwd=_PROJECT_DIR,
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
            return f"ERROR: command timed out after {DEFAULT_BASH_TIMEOUT_SECONDS}s"
        except Exception as e:
            return f"ERROR: {e}"

    # Long-running path: poll the process to completion (up to 300 s) via the
    # shared wait-and-recheck helper so we both wait it out AND report the
    # elapsed time. Output is captured to temp files (not pipes) so a chatty
    # installer can never deadlock on a full pipe buffer while we poll.
    from osia.tools.task_wait import wait_and_verify
    try:
        out_f = tempfile.TemporaryFile()
        err_f = tempfile.TemporaryFile()
    except Exception as e:
        return f"ERROR: {e}"
    try:
        if sys.platform == "win32":
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-Command", command],
                stdout=out_f, stderr=err_f, cwd=_PROJECT_DIR,
            )
        else:
            # start_new_session puts the shell in its own process group so a
            # timeout can kill the WHOLE tree: with shell=True the installer is
            # a grandchild of the shell, and proc.kill() alone would orphan it
            # (it keeps running while we report "killed"). POSIX only — the
            # win32 branch above launches powershell directly.
            proc = subprocess.Popen(
                command, shell=True, stdout=out_f, stderr=err_f,
                cwd=_PROJECT_DIR, start_new_session=True,
            )
    except Exception as e:
        out_f.close()
        err_f.close()
        return f"ERROR: {e}"
    done, elapsed = wait_and_verify(
        lambda: proc.poll() is not None,
        timeout_s=LONG_RUNNING_TIMEOUT_SECONDS,
        poll_interval_s=1.0,
        description=f"bash: {command[:60]}",
    )
    if not done:
        # Still running after the budget — kill the whole process group (so the
        # installer grandchild dies too, not just the shell) and report honestly.
        try:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=3)
        except Exception:
            pass
        out_f.close()
        err_f.close()
        return (
            f"ERROR: command timed out after {LONG_RUNNING_TIMEOUT_SECONDS}s "
            f"(elapsed {elapsed:.1f}s, still running — killed)"
        )
    try:
        out_f.seek(0)
        err_f.seek(0)
        out = out_f.read().decode("utf-8", "replace").strip()
        err = err_f.read().decode("utf-8", "replace").strip()
    finally:
        out_f.close()
        err_f.close()
    suffix = f"\n\n[completed in {elapsed:.1f}s, exit code {proc.returncode}]"
    if out and err:
        return f"STDOUT:\n{out}\n\nSTDERR:\n{err}{suffix}"
    if out:
        return f"{out}{suffix}"
    if err:
        return f"STDERR:\n{err}{suffix}"
    return f"(no output){suffix}"


def tool_read_file(path: str) -> str:
    """Read a file and return its contents (truncated to 4000 chars)."""
    if not os.path.isabs(path):
        path = os.path.join(_PROJECT_DIR, path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 4000:
            content = content[:4000] + "\n... [TRUNCATED]"
        return content
    except Exception as e:
        return f"ERROR: reading file: {e}"


def tool_write_file(path: str, content: str) -> str:
    """Write content to a file. Creates parent directories if needed."""
    if not os.path.isabs(path):
        path = os.path.join(_PROJECT_DIR, path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"ERROR: writing file: {e}"


def tool_open_application(name: str) -> str:
    """Launch an application by name. Detaches so it keeps running.

    The launch is only reported as success when it actually started. A typo'd or
    missing app makes the shell exit almost immediately with a non-zero code
    (Windows ``'X' n'est pas reconnu`` / ``is not recognized`` → 1/9009; POSIX
    ``command not found`` → 127), which we surface as an ``ERROR:`` so OSIA tells
    the user it could not launch the app instead of falsely claiming success
    (the old code returned "Launched" the instant ``Popen`` spawned the shell,
    never checking whether the inner command was found). A process still alive
    after a short grace window — or one that exits cleanly (code 0, e.g. a
    launcher that spawned the real process) — is a genuinely launched app.

    Before launching, if the app is ALREADY open its existing window is brought to
    the foreground instead of starting a second instance (WOR-50 follow-up #2):
    matched by owning PROCESS name (titles rarely carry the app name), exact base
    only so the wrong app is never focused, and the focus is poll-confirmed. On no
    or uncertain match — and on Linux/Mac, where already-open detection is not
    wired — we fall through and launch as usual (no regression, no false error).
    This also removes the edge case where an already-open+focused app timed out in
    the post-launch window wait.

    After a launch, the post-launch wait is ACTIVE on Windows (WOR-50 follow-up
    #3): the new window is looked up by owning process (find_app_window — sees
    BACKGROUND windows too) and raised + focus-confirmed (focus_app_window) the
    moment it exists, so an app that opens behind other windows is brought to the
    foreground instead of timing out into a false ERROR while a following
    take_screenshot/desktop_click hits whatever window stayed in front. Apps whose
    process name doesn't match the launch token (UWP frame hosts, `start`-style
    indirections) are still caught by the interleaved passive foreground watcher
    (wait_for_app_window, unchanged); Linux/Mac keep the passive wait only.

    On Windows the bare name is first RESOLVED to the app's real executable +
    arguments (WOR-80: desktop_control.resolve_windows_app — App Paths registry,
    Start-menu .lnk scan, common install roots, UWP/Store), so per-user apps that
    are not on PATH (Discord, Steam, Spotify, …) launch reliably; only when nothing
    resolves do we fall back to the `cmd /c <name>` PATH launch. Launch success is
    then keyed on the RESOLVED process bases, and a running process counts as
    launched even if its window never grabbed the foreground in time — so a heavy
    or tray-bound app is never falsely ERRORed. A genuinely missing app (no
    resolution, no process, shell reports "not recognized") still ERRORs
    (anti-hallucination contract). All of this is Windows-gated; Linux/Mac are
    unchanged.
    """
    _t_enter = time.monotonic()
    # ---- STEP 1 (unchanged, FIRST): already-open visible window -> focus it -----
    # Matched by the raw token's owning PROCESS name; brings an existing window to
    # the foreground instead of starting a second instance (WOR-50 follow-up #2).
    try:
        from osia.tools import desktop_control as _dc
        _existing = _dc.find_app_window(name)
    except Exception:
        _dc, _existing = None, None
    if _existing is not None:
        try:
            _st, _detail = _dc.focus_app_window(_existing)
        except Exception:
            _st, _detail = "unavailable", ""
        if _st == "ready":
            return (
                f"'{name}' is already open — brought its window '{_detail}' to the "
                "foreground and focused it (ready; did not launch a second instance)."
            )
        # focus unconfirmed -> fall through and launch normally (safer than
        # claiming success on a window we could not confirm focused).

    # ---- STEP 2 (Windows): resolve the real executable + broadened already-open /
    # tray detection (WOR-80). On Linux/Mac this whole block is skipped and the
    # launch below stays the existing `cmd /c <name>` behaviour, byte-for-byte. ----
    resolved = None            # (exe_path, [args]) when a concrete target is found
    candidates = set()         # process bases that identify THIS app's window/proc
    if sys.platform == "win32" and _dc is not None:
        try:
            resolved = _dc.resolve_windows_app(name)
        except Exception:
            resolved = None
        try:
            candidates = _dc.windows_app_candidates(name, resolved)
        except Exception:
            candidates = set()
        # Broadened already-open check: a visible window owned by ANY resolved
        # process base (catches names the raw token misses, e.g. "vs code" ->
        # Code.exe) -> focus it instead of starting a second instance.
        try:
            _win = _dc.find_app_window_multi(candidates)
        except Exception:
            _win = None
        if _win is not None:
            try:
                _st, _dt = _dc.focus_app_window(_win)
            except Exception:
                _st, _dt = "unavailable", ""
            if _st == "ready":
                return (
                    f"'{name}' is already open — brought its window '{_dt}' to the "
                    "foreground and focused it (ready; did not launch a second instance)."
                )
        # Tray-resident (WOR-80 follow-up): we reach here only when no focusable
        # window exists (find_app_window_multi above already foregrounded one if it
        # did). The app's process may still be running closed to the system tray —
        # an earlier fix early-returned "already running" here, but that left OSIA
        # with NO usable window to navigate, so it would say "already running" and
        # never act. We now intentionally do NOT early-return: fall through to STEP 3
        # and RELAUNCH the already-resolved exe. Single-instance tray apps (Steam,
        # Discord, Spotify, Slack, …) re-surface their existing main window when
        # their exe is re-run rather than spawning a second instance; the post-launch
        # active-raise loop below then polls find_app_window_multi for that surfaced
        # window and foregrounds it, returning "window '…' is ready and focused".
        # The ERROR contract stays intact: if the process is running but its window
        # is slow to surface, the launched-on-running-process (852) and
        # timeout-with-running-process (924) checks keep this a SUCCESS, never a
        # false ERROR; a genuinely-missing app (no resolution, no process) still
        # ERRORs via the unchanged shell fallback. (No extra surfacing mechanism is
        # needed — re-running the single-instance exe IS the surface-don't-duplicate
        # behaviour; the rare non-single-instance tray app spawning a 2nd copy is an
        # acceptable, strictly-better-than-no-window residual risk.)

    # Snapshot the foreground window BEFORE launching so the post-launch readiness
    # wait can tell when focus has moved to the NEW app window (WOR-50 follow-up).
    _before_fg = None
    if _dc is not None:
        try:
            _before_fg = _dc.foreground_snapshot()
        except Exception:
            _before_fg = None

    # ---- STEP 3: launch. A resolved concrete path is launched DIRECTLY with its
    # arguments (so per-user installs off PATH, and launcher shortcuts like
    # Discord's `Update.exe --processStart Discord.exe`, start reliably); otherwise
    # fall back to the existing `cmd /c <name>` PATH launch (no regression for apps
    # already on PATH — and the only path on Linux/Mac). ----
    err_path = None            # only used by the shell fallback (capture its stderr)
    err_fh = None

    def _cleanup():
        if err_fh is not None:
            try:
                err_fh.close()
            except Exception:
                pass
        if err_path:
            # On POSIX the child keeps its own fd so unlinking now is safe; on
            # Windows it may fail while the child holds the handle (the OS temp
            # sweeper reclaims it later) — either way, best-effort.
            try:
                os.remove(err_path)
            except OSError:
                pass

    proc = None
    if resolved is not None:
        _path, _args = resolved
        try:
            proc = subprocess.Popen(
                [_path, *[str(a) for a in (_args or [])]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            proc = None  # resolved path failed to spawn -> fall back to the shell
    if proc is None:
        # Capture the shell's stderr to a temp file rather than a PIPE so a
        # long-running app that chatters on stderr can never deadlock on a full
        # pipe buffer (we'd never be draining it).
        try:
            fd, err_path = tempfile.mkstemp(prefix="osia_launch_", suffix=".log")
            err_fh = os.fdopen(fd, "w+b")
        except Exception:
            err_path, err_fh = None, None
        try:
            proc = subprocess.Popen(
                name,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=(err_fh if err_fh is not None else subprocess.DEVNULL),
            )
        except Exception as e:
            _cleanup()
            return f"ERROR: launching {name}: {e}"

    # Watch briefly: a not-found/typo'd command dies in milliseconds; a real
    # GUI/CLI app is still running when the grace window elapses. A launcher that
    # exits cleanly (code 0) after spawning the real process also counts.
    grace, step, waited = 1.5, 0.05, 0.0
    rc = proc.poll()
    while rc is None and waited < grace:
        time.sleep(step)
        waited += step
        rc = proc.poll()

    launched = rc is None or rc == 0
    # Even on a non-zero launcher exit, if a matching app process is now running the
    # app DID start (bootstrapper handed off) -> treat as launched, never a false
    # ERROR (WOR-80 Fix B). Windows-only and only with a CONFIDENT resolution, so an
    # unresolved name whose raw token collides with an unrelated process is never
    # falsely reported as launched (review #2); the cmd /c fallback then keeps its
    # original honest-ERROR behaviour.
    if (not launched and sys.platform == "win32" and resolved is not None
            and candidates and _dc is not None):
        try:
            if _dc.is_app_running(candidates):
                launched = True
        except Exception:
            pass

    # Number of seconds the readiness wait below budgets, for honest messaging.
    _wait_secs = 14
    if _dc is not None:
        _wait_secs = int(
            _dc.APP_LAUNCH_WINDOW_TIMEOUT if sys.platform == "win32"
            else _dc.APP_WINDOW_TIMEOUT
        )

    if launched:
        _cleanup()
        # The process is alive (or a launcher exited cleanly) -> now ACTIVELY WAIT
        # for the app's window to appear and take foreground focus before reporting
        # success, so a following desktop_type/desktop_click cannot fire into the
        # void or the wrong window (WOR-50 follow-up). No fixed sleep: poll and
        # return the instant the window is ready (fast apps are not slowed). A
        # timeout is NOT a failure when the process is running (WOR-80 Fix B).
        try:
            if sys.platform == "win32" and _dc is not None:
                # ACTIVE raise (WOR-50 follow-up #3): wait_for_app_window only
                # OBSERVES the foreground, so an app whose window opened in the
                # BACKGROUND was never brought forward. Poll find_app_window_multi
                # — which enumerates ALL visible windows and matches the RESOLVED
                # process bases (so a bootstrapper that hands off to a differently
                # named process is still caught) — and the moment the new app's
                # window exists raise + focus-confirm it. Interleaved with 1s slices
                # of the passive wait so apps whose process name matches nothing in
                # `candidates` (UWP frame hosts) are still caught when they take
                # focus by themselves. The deadline is clamped to stay inside the
                # 30s tool-execution cap even if resolution/grace already ran.
                status, detail = "timeout", ""
                _no_raise = set()  # hwnds that refused focus -> one attempt each
                _deadline = min(time.monotonic() + _dc.APP_LAUNCH_WINDOW_TIMEOUT,
                                _t_enter + 26.0)
                while True:
                    _win = _dc.find_app_window_multi(candidates)
                    if _win is not None and _win[0] not in _no_raise:
                        _remaining = max(0.5, _deadline - time.monotonic())
                        _st, _dt = _dc.focus_app_window(
                            _win, timeout=min(_dc.APP_FOCUS_TIMEOUT, _remaining))
                        if _st == "ready":
                            status, detail = "ready", _dt
                            break
                        # Focus refused/unconfirmed: don't fight over this hwnd
                        # again; keep the passive watcher running — the window
                        # may still take focus on its own before the deadline.
                        _no_raise.add(_win[0])
                    if time.monotonic() >= _deadline:
                        break
                    status, detail = _dc.wait_for_app_window(_before_fg, timeout=1.0)
                    if status != "timeout":  # "ready" or "unavailable"
                        break
            elif _dc is not None:
                status, detail = _dc.wait_for_app_window(_before_fg)
            else:
                status, detail = "unavailable", ""
        except Exception:
            status, detail = "unavailable", ""
        if status == "ready":
            return f"Launched: {name} — window '{detail}' is ready and focused"
        if status == "timeout":
            # The window never grabbed the foreground in time. On Windows, if the
            # app's process IS running the launch SUCCEEDED (heavy/cold app still
            # drawing, or it lives in the tray) -> report success, NOT a false
            # ERROR (WOR-80 Fix B; this also fixes Steam's old 14s false timeout).
            if (sys.platform == "win32" and resolved is not None and candidates
                    and _dc is not None):
                try:
                    if _dc.is_app_running(candidates):
                        return (
                            f"Launched: {name} — the app is running (its window did "
                            f"not take the foreground within {_wait_secs}s; take a "
                            "screenshot to see the current state before typing)."
                        )
                except Exception:
                    pass
            return (
                f"ERROR: launched '{name}' but no application window appeared and "
                f"took focus within {_wait_secs}s — the app may have failed to open "
                "a window. Take a screenshot to see the current state before doing "
                "anything else; do NOT type yet — the keystrokes would land in the "
                "wrong window."
            )
        # 'unavailable': foreground detection is not wired on this platform (e.g.
        # Wayland without kdotool, or macOS) -> the process WAS spawned, but its
        # window can't be confirmed here. Report honestly as UNVERIFIED instead of
        # asserting success (WOR-87). This is NOT an ERROR (the app most likely
        # launched), but the model must verify before claiming it did.
        return (
            f"Launched: {name} — process spawned, but this platform can't confirm "
            "its window appeared (foreground detection isn't wired here). "
            "UNVERIFIED: confirm with is_running / take_screenshot before claiming "
            "success."
        )

    # Non-zero exit within the grace window AND no matching process is running →
    # genuine failure. Surface the shell's stderr (the "is not recognized" /
    # "command not found" line) so OSIA tells the user honestly (anti-hallucination
    # contract: a real missing app must still ERROR).
    err_text = ""
    if err_fh is not None:
        try:
            err_fh.seek(0)
            err_text = err_fh.read().decode("utf-8", "replace").strip()
        except Exception:
            err_text = ""
    _cleanup()
    detail = f": {err_text}" if err_text else ""
    return (
        f"ERROR: could not launch application '{name}' — not found or failed to "
        f"start (exit code {rc}){detail}"
    )


# steam://run/<appid>, steam://rungameid/<appid>, steam://install/<appid> — the
# deep links whose REAL outcome OSIA can confirm deterministically (WOR-87).
_STEAM_RUN_RE = re.compile(r"^\s*steam://(run|rungameid|install)/(\d+)",
                           re.IGNORECASE)


def _verify_open_uri(uri: str):
    """Confirm a FIRED deep link's REAL outcome where OSIA can do so
    deterministically (WOR-87). Currently Steam run/install: read the install
    manifest + process state so the model is handed what ACTUALLY happened, not
    just that the URI fired (an uninstalled game's ``steam://run`` fires fine but
    launches nothing). Returns a confirmation string, or ``None`` when no
    deterministic check applies (the caller then returns the honest
    FIRED-not-confirmed message). Never raises.
    """
    try:
        m = _STEAM_RUN_RE.match(uri or "")
        if not m:
            return None
        action, appid = m.group(1).lower(), m.group(2)
        from osia.tools import steam as _steam
        if _steam.steam_path() is None:
            return (f"{uri} fired, but Steam is not installed/detected on this "
                    "machine — cannot confirm anything launched. Tell the user "
                    "honestly; do NOT claim it launched.")
        state = _steam.steam_game_state(appid)
        label = (f"{state['name']} (appid {appid})"
                 if state and state.get("name") else f"appid {appid}")

        if action == "install":
            if state and state.get("installed"):
                return (f"{uri} fired — {label} is ALREADY fully installed; Steam "
                        "will just show its page, no download is needed.")
            return (f"{uri} fired — Steam will prompt to install {label}. The "
                    "install is NOT instant or confirmed yet: the download must "
                    "finish before the game can launch. Don't claim it's installed.")

        # run / rungameid
        if state is None:
            return (f"{uri} fired, but {label} is NOT installed (no Steam "
                    "appmanifest for it) — nothing will launch. Tell the user it "
                    "must be installed first; offer to install it. Do NOT claim "
                    "you launched it.")
        if not state.get("installed"):
            return (f"{uri} fired, but {label} is NOT fully installed "
                    "(download/update incomplete) — it will not launch yet. Tell "
                    "the user honestly; offer to finish installing it.")
        # Installed. Give the process a brief, BOUNDED moment to appear (a warm
        # Steam launches fast). We never wait out a cold-start (Steam can take
        # ~30s) — past the short budget we report 'starting', honestly.
        running = bool(state.get("running"))
        if not running:
            _deadline = time.monotonic() + 3.0
            while time.monotonic() < _deadline:
                time.sleep(0.5)
                _re_state = _steam.steam_game_state(appid)
                if _re_state and _re_state.get("running"):
                    running = True
                    break
        if running:
            return (f"{uri} fired — {label} is installed and its process is now "
                    "running: launch CONFIRMED.")
        return (f"{uri} fired — {label} is installed, but its process hasn't "
                "appeared yet (the game may still be starting). Do NOT claim it is "
                "running until confirmed — re-check with is_running, or take a "
                "screenshot.")
    except Exception:
        return None


def tool_open_uri(uri: str) -> str:
    """Open a deterministic deep link / URL scheme (WOR-85).

    OSIA's FAST, reliable way to reach an app DESTINATION — no mouse, no vision.
    ``steam://open/games`` jumps Steam straight to the library, ``spotify:``
    focuses Spotify, ``vscode://file/<path>`` opens a file in VS Code. The URI is
    validated against a NARROW whitelist (``deep_links.is_allowed``: the scheme
    must be allowed; file:/javascript:/data:/vbscript: and bare paths are
    refused) BEFORE launching, then handed to the OS URL handler per platform —
    mirroring ``tool_open_in_editor``'s three branches: Windows ``os.startfile``,
    macOS ``open``, Linux ``xdg-open``. The URI is ALWAYS passed as a SINGLE
    argument (never through a shell) so it can't be used for command injection,
    and the launch is detached so it never blocks the tool timeout. A disallowed
    URI or a launch failure returns ``ERROR:`` (anti-hallucination contract).
    """
    from osia.tools import deep_links as _dl
    if uri is None:
        return "ERROR: open_uri: no uri provided."
    uri = str(uri).strip()
    if not _dl.is_allowed(uri):
        return (
            f"ERROR: open_uri refused '{uri}' — not an allowed deep link. Allowed "
            f"schemes: {', '.join(sorted(_dl.ALLOWED_SCHEMES))} (e.g. "
            "steam://open/games, spotify:, vscode://file/<path>). file:, "
            "javascript:, data:, vbscript: and bare paths are blocked."
        )
    try:
        if sys.platform == "win32":
            os.startfile(uri)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", uri])
        else:
            subprocess.Popen(
                ["xdg-open", uri],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as e:
        return f"ERROR: open_uri launching {uri}: {e}"
    # The URI was handed to the OS handler — but FIRING a deep link is an ATTEMPT,
    # not a confirmation (WOR-87). Where OSIA can verify the real outcome
    # deterministically (Steam run/install), fold THAT into the reply; otherwise
    # say so honestly so the model never reads "opened" as "succeeded".
    verdict = _verify_open_uri(uri)
    if verdict:
        return verdict
    return (f"{uri} fired (handed to the OS handler). This is an ATTEMPT, not a "
            "confirmation — verify the real result (is_running / has_window / "
            "get_current_url, vision last) before claiming success.")


def tool_list_files(path: str) -> str:
    """List files in a directory."""
    if not os.path.isabs(path):
        path = os.path.join(_PROJECT_DIR, path)
    try:
        entries = os.listdir(path)
        entries.sort()
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"ERROR: listing {path}: {e}"


def tool_delete_file(path: str) -> str:
    """Delete a file."""
    if not os.path.isabs(path):
        path = os.path.join(_PROJECT_DIR, path)
    try:
        os.remove(path)
        return f"Deleted: {path}"
    except Exception as e:
        return f"ERROR: deleting {path}: {e}"


def tool_run_command(command: str) -> str:
    """Run a bash command and return its output. Alias for bash."""
    return tool_bash(command)


def _screenshot_is_blank(path: str) -> bool:
    """Cheap all-black detector for captures that silently failed.

    X11 tools (``scrot``) can't see native Wayland windows, so they write a
    fully black frame instead of erroring — returncode 0, file present, but
    nothing visible. We load the image and treat it as blank when its
    brightest pixel is near zero (max luminance < 10).

    Degrades gracefully: if Pillow is missing or the file can't be read we
    return ``False`` so a genuine capture is never discarded just because the
    check itself couldn't run.
    """
    try:
        from PIL import Image
        with Image.open(path) as img:
            _lo, hi = img.convert("L").getextrema()
        return hi < 10
    except Exception:
        return False


def _capture_screen(path: str) -> str | None:
    """Capture the full screen to ``path``. Returns ``None`` on success, or an
    ``ERROR: ...`` string on failure.

    The capture-backend chain (Pillow's ImageGrab on Windows; a Wayland-aware
    chain on Linux/mac, each verified to be present AND not all-black) is the
    single source of truth shared by :func:`tool_take_screenshot` (describe the
    screen) and the desktop-click path (locate an element by pixel) so the two
    never drift apart.
    """
    if sys.platform == "win32":
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(path)
        except Exception as e:
            return f"ERROR: taking screenshot: {e}"
        return None

    # Linux/mac. On Wayland (KDE Plasma/GNOME on Fedora) X11 tools like scrot
    # capture a black frame because they can't see native Wayland windows —
    # prefer Wayland-native backends there and only fall back to scrot via
    # XWayland. Each backend is verified (file present AND not all-black) before
    # we accept it; otherwise we try the next one.
    is_wayland = bool(
        os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )
    if is_wayland:
        backends = [
            ("spectacle", ["spectacle", "-b", "-n", "-o", path]),  # KDE
            ("grim", ["grim", path]),                              # wlroots
            ("gnome-screenshot", ["gnome-screenshot", "-f", path]),  # GNOME
            ("scrot", ["scrot", "-o", path]),                     # XWayland
        ]
    else:
        backends = [("scrot", ["scrot", "-o", path])]

    last_err = ""
    for name, cmd in backends:
        if shutil.which(name) is None:
            last_err = f"{name} is not installed"
            continue
        # Drop any stale file so the existence check below is meaningful.
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
        except Exception as e:
            last_err = f"{name}: {e}"
            continue
        if result.returncode != 0:
            last_err = result.stderr.strip() or f"{name} failed"
            continue
        if not os.path.isfile(path):
            last_err = f"{name} produced no file"
            continue
        if _screenshot_is_blank(path):
            last_err = f"{name} captured a black screen"
            continue
        return None

    return f"ERROR: taking screenshot: {last_err or 'no screenshot backend succeeded'}"


def tool_take_screenshot() -> str:
    """Capture the desktop and return a description of what is on screen.

    Captures to the system temp dir (Pillow's ImageGrab on Windows; a Wayland-
    aware backend chain on Linux/mac), then routes the image to the vision
    model resolved by
    :mod:`osia.vision_router` (the main model when it is vision-capable, else
    the configured ``vision_provider``/``vision_model`` companion — WOR-25) and
    returns its description, so the assistant can actually "see" the screen.

    Why analyse here instead of letting the run loop attach the image: the run
    loop only injects an image when a tool result is a browser-style
    ``{"text","screenshot"}`` dict (and only for the narrow set of providers
    ``_provider_supports_vision`` allows). ``take_screenshot`` returns a plain
    string, so its image was never seen by the model — which is why a text-only
    main model replied "I can't see images". Describing the image through the
    vision companion fixes that uniformly on every entry point (desktop GUI,
    mobile server, cron) without touching the run loop's injection gate.
    """
    path = str(Path(tempfile.gettempdir()) / "osia_screenshot.png")
    err = _capture_screen(path)
    if err:
        return err
    return _describe_screenshot(path)


# Prompt sent to the vision model alongside a captured screenshot. FR-first to
# match OSIA's default UX; the main model reformulates for the user's language.
_SCREENSHOT_VISION_PROMPT = (
    "Décris en détail tout ce qui est visible sur cette capture d'écran : "
    "fenêtres et applications ouvertes, textes lisibles, boutons, menus, "
    "messages d'erreur et état général de l'écran. Sois précis et factuel."
)


def _describe_screenshot(path: str) -> str:
    """Analyse a captured screenshot with the resolved vision model (WOR-25)
    and return its description as plain text.

    Vision routing is delegated to :func:`osia.vision_router.get_vision_client`,
    which honours the user's endpoint/auth config identically to
    ``OSIAAgent.set_model`` (it re-derives, never mutates, the frozen client
    wiring). The call passes NO tool schema, so the vision model cannot emit a
    tool call, and the result is a plain string, so the run loop never tries to
    attach base64 or start an extra tool round.

    Degrades gracefully: on any failure (no vision model configured, missing
    key, network/timeout, provider error) it returns the file path plus a plain
    note — never an ``ERROR:``-prefixed string, so a successful capture is not
    mistaken for a hard failure by ``_is_tool_error`` / the anti-loop guard.

    API keys honour the user's fallback chain: the request rotates through the
    vision provider's configured keys on a 429/5xx/auth/timeout error, and — if
    every vision-provider key is exhausted AND the main model is itself
    vision-capable — falls back to the main model's own key chain as a last
    resort (shared rotation via :func:`osia.utils.key_chain.with_key_rotation`).
    """
    try:
        import base64
        from osia.config import load_settings
        from osia.vision_router import resolve_vision_model, build_vision_client
        from osia.models import model_has_vision, provider_for_model
        from osia.utils.key_chain import (
            with_key_rotation, KeyChainEmpty, is_technical_error, error_status,
        )

        settings = load_settings()
        provider, model = resolve_vision_model(settings)
        # Only call a genuinely vision-capable model. resolve_vision_model()
        # falls back to the (possibly text-only) MAIN model when no companion is
        # configured; sending an image_url array to such a model raises a 400,
        # so guard before the network call.
        if not provider or not model or not model_has_vision(provider, model):
            return (
                f"{path}\n(Note : capture enregistrée, mais aucun modèle de "
                "vision n'est configuré — impossible de décrire l'écran. "
                "Configurez un modèle de vision dans les paramètres.)"
            )
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")

        def _vision_call(call_provider, call_model):
            """A screenshot request for ``(call_provider, call_model)`` that
            rebuilds its client from whichever key the rotation hands it. Bound
            well under the 30 s tool-executor cap so a slow provider yields a
            clean note, not the generic timeout [ERREUR] (which would double the
            retry). No max_tokens — providers apply their native limits
            (MAX_RESPONSE_TOKENS removed deliberately, WOR-28)."""
            def _do(key):
                client = build_vision_client(call_provider, key)
                resp = client.chat.completions.create(
                    model=call_model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _SCREENSHOT_VISION_PROMPT},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{b64}"}},
                        ],
                    }],
                    timeout=25,
                )
                return (resp.choices[0].message.content or "").strip()
            return _do

        # 1) Rotate through the vision provider's key chain (fallbacks first when
        #    configured, else the single saved key).
        description = ""
        last_err = None
        vision_failed = False
        try:
            description = with_key_rotation(
                provider, _vision_call(provider, model),
                label=f"vision:{provider}",
            )
        except KeyChainEmpty:
            # No vision-provider key at all — vacuously "exhausted". A configured
            # but key-less vision companion may still fall back to a
            # vision-capable main model below, so allow the last resort.
            vision_failed = True
            last_err = (
                f"aucune clé API configurée pour le fournisseur de vision "
                f"« {provider} »"
            )
        except Exception as e:
            last_err = e
            # Only treat as "vision keys exhausted" — which licenses the
            # main-model last resort — when the failure was technical
            # (429/5xx/auth/timeout across the whole chain). A non-technical
            # error (400/404) is re-raised by with_key_rotation WITHOUT rotating;
            # another model won't fix a malformed request, so surface it directly
            # instead of burning an extra call on the main model.
            if is_technical_error(e, error_status(e)):
                vision_failed = True

        # 2) Last resort: every vision-provider key exhausted AND the MAIN model
        #    is itself vision-capable (and a different target) — try the main
        #    model's own key chain before giving up.
        if vision_failed:
            main_model = (settings.get("selected_model") or "").strip()
            main_provider = provider_for_model(main_model) if main_model else None
            if (main_model and main_provider
                    and model_has_vision(main_provider, main_model)
                    and (main_provider, main_model) != (provider, model)):
                try:
                    description = with_key_rotation(
                        main_provider, _vision_call(main_provider, main_model),
                        label=f"vision-fallback:{main_provider}",
                    )
                except Exception as e:
                    last_err = e

        if not description:
            if last_err is not None:
                return (
                    f"{path}\n(Note : la capture est enregistrée mais l'analyse "
                    f"visuelle a échoué : {last_err})"
                )
            return (
                f"{path}\n(Note : le modèle de vision n'a renvoyé aucune "
                "description de l'écran.)"
            )
        return f"Description de l'écran (capture : {path}) :\n{description}"
    except Exception as e:
        return (
            f"{path}\n(Note : la capture est enregistrée mais l'analyse "
            f"visuelle a échoué : {e})"
        )


# ── Path helper (resolves relative paths against the dynamic _PROJECT_DIR) ──


def _abs(path: str) -> str:
    """Resolve a possibly-relative path against the project directory."""
    return path if os.path.isabs(path) else os.path.join(_PROJECT_DIR, path)


# ── File-system tools ──────────────────────────────────────────────────────

def tool_create_directory(path: str) -> str:
    """Create a directory (and any missing parents)."""
    path = _abs(path)
    try:
        os.makedirs(path, exist_ok=True)
        return f"Created directory: {path}"
    except Exception as e:
        return f"ERROR: creating directory {path}: {e}"


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
        return f"ERROR: deleting {path}: {e} (use recursive=true to remove non-empty dirs)"
    except Exception as e:
        return f"ERROR: deleting {path}: {e}"


def tool_rename_file(old_path: str, new_path: str) -> str:
    """Rename or move a file or directory."""
    src = _abs(old_path)
    dst = _abs(new_path)
    try:
        os.rename(src, dst)
        return f"Renamed: {src} -> {dst}"
    except Exception as e:
        return f"ERROR: renaming {src} -> {dst}: {e}"


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
        return f"ERROR: copying {src_abs} -> {dst_abs}: {e}"


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
        return f"ERROR: getting info for {path}: {e}"


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
        return f"ERROR: searching {directory}: {e}"
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
    if sys.platform == "win32":
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return f"Opened {path}"
        except Exception as e:
            return f"ERROR: opening {path}: {e}"
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", path])
            return f"Opened {path}"
        except Exception as e:
            return f"ERROR: opening {path}: {e}"
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
        return f"ERROR: opening {path} in {editor}: {e}"


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
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"ERROR: reading {path}: {e}"
    if start > len(lines):
        return f"ERROR: start_line {start} is past end of file ({len(lines)} lines)"
    end = min(end, len(lines))
    replacement = new_content if new_content.endswith("\n") else new_content + "\n"
    new_lines = lines[:start - 1] + [replacement] + lines[end:]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        return f"ERROR: writing {path}: {e}"
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
        du = psutil.disk_usage(str(Path.home().anchor))
        uname = platform.uname()
        return (
            f"host:    {uname.node} ({uname.system} {uname.release})\n"
            f"cpu:     {cpu_pct:.1f}% across {cores} cores  (load 1/5/15: {load_str})\n"
            f"memory:  {vm.used / 1024**3:.1f} / {vm.total / 1024**3:.1f} GiB used ({vm.percent:.0f}%)\n"
            f"disk /:  {du.used / 1024**3:.1f} / {du.total / 1024**3:.1f} GiB used ({du.percent:.0f}%)"
        )
    except Exception as e:
        return f"ERROR: collecting system info: {e}"


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
            return f"ERROR: killing PID {pid}: {e}"
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
            return f"ERROR: setting volume: {result.stderr.strip() or 'pactl failed'}"
        return f"Volume set to {pct}%"
    except FileNotFoundError:
        return "ERROR: pactl is not installed"
    except Exception as e:
        return f"ERROR: setting volume: {e}"


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
    if sys.platform == "win32":
        try:
            import pyperclip
            text = pyperclip.paste()
        except Exception:
            try:
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard"],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode != 0:
                    return f"ERROR: reading clipboard: {result.stderr.strip() or 'Get-Clipboard failed'}"
                text = result.stdout
            except Exception as e:
                return f"ERROR: reading clipboard: {e}"
        if len(text) > 4000:
            text = text[:4000] + "\n... [TRUNCATED]"
        return text if text else "(clipboard is empty)"
    backend = _clipboard_backend()
    if backend is None:
        return "ERROR: no clipboard backend (install wl-clipboard, xclip, or xsel)"
    read_cmd, _ = backend
    try:
        result = subprocess.run(read_cmd, capture_output=True, text=True, timeout=3)
        if result.returncode != 0:
            return f"ERROR: reading clipboard: {result.stderr.strip() or read_cmd[0] + ' failed'}"
        text = result.stdout
        if len(text) > 4000:
            text = text[:4000] + "\n... [TRUNCATED]"
        return text if text else "(clipboard is empty)"
    except Exception as e:
        return f"ERROR: reading clipboard: {e}"


def tool_set_clipboard(text: str) -> str:
    """Place ``text`` on the clipboard."""
    if sys.platform == "win32":
        try:
            import pyperclip
            pyperclip.copy(text)
            return f"Copied {len(text)} chars to clipboard"
        except Exception:
            try:
                result = subprocess.run(
                    ["clip.exe"], input=text, capture_output=True, text=True, timeout=3,
                )
                if result.returncode != 0:
                    return f"ERROR: writing clipboard: {result.stderr.strip() or 'clip.exe failed'}"
                return f"Copied {len(text)} chars to clipboard"
            except Exception as e:
                return f"ERROR: writing clipboard: {e}"
    backend = _clipboard_backend()
    if backend is None:
        return "ERROR: no clipboard backend (install wl-clipboard, xclip, or xsel)"
    _, write_cmd = backend
    try:
        result = subprocess.run(
            write_cmd, input=text, capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return f"ERROR: writing clipboard: {result.stderr.strip() or write_cmd[0] + ' failed'}"
        return f"Copied {len(text)} chars to clipboard"
    except Exception as e:
        return f"ERROR: writing clipboard: {e}"


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


# ---------------------------------------------------------------------------
# Desktop automation (WOR-42) — vision-guided control of NATIVE apps.
#
# These drive the real mouse/keyboard at the OS level (ydotool/xdotool/pyautogui
# via osia.tools.desktop_control). ``desktop_click`` is the smart entry point: it
# captures the screen, asks the vision model for the target's pixel coordinates,
# then clicks. Every vision call goes through key_chain rotation (CLAUDE.md).
# ---------------------------------------------------------------------------
def _vision_locate(path: str, description: str) -> dict:
    """Ask the vision model for the pixel coordinates of ``description`` in the
    screenshot at ``path``.

    Returns one of:
      ``{"found": True, "x": int, "y": int, "w": W, "h": H}``
      ``{"found": False, "reason": str}``
      ``{"error": "ERROR: ..."}``  (no vision model configured / technical failure)

    The vision request mirrors :func:`_describe_screenshot`'s core but swaps in a
    coordinate prompt; like it, the network call runs under ``with_key_rotation``
    so the vision provider's whole key chain (fallbacks first) is tried.
    """
    try:
        import base64
        from osia.config import load_settings
        from osia.vision_router import resolve_vision_model, build_vision_client
        from osia.models import model_has_vision
        from osia.utils.key_chain import with_key_rotation, KeyChainEmpty

        settings = load_settings()
        provider, model = resolve_vision_model(settings)
        if not provider or not model or not model_has_vision(provider, model):
            return {"error": (
                "ERROR: desktop_click needs a vision model to locate the target on "
                "screen, but none is configured. Set a vision model in the settings."
            )}

        try:
            from PIL import Image
            with Image.open(path) as img:
                w, h = img.size
        except Exception:
            w = h = 0

        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")

        size_note = (
            f"The image is {w}x{h} pixels; the top-left corner is (0,0). "
            if (w and h) else
            "The top-left corner of the image is (0,0). "
        )
        prompt = (
            "You are looking at a screenshot of a computer screen. " + size_note +
            f"Find this on-screen element: \"{description}\".\n"
            "Reply with ONLY a JSON object — no prose, no markdown fences.\n"
            "If you can see it, return the CENTER of the element in pixels: "
            "{\"found\": true, \"x\": <int>, \"y\": <int>}.\n"
            "If it is not visible, return: {\"found\": false, \"reason\": \"<short reason>\"}.\n"
            "If you cannot clearly see the described element, return found=false. "
            "NEVER guess coordinates."
        )

        def _call(call_provider, call_model):
            def _do(key):
                client = build_vision_client(call_provider, key)
                resp = client.chat.completions.create(
                    model=call_model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{b64}"}},
                        ],
                    }],
                    timeout=25,
                )
                return (resp.choices[0].message.content or "").strip()
            return _do

        try:
            raw = with_key_rotation(
                provider, _call(provider, model),
                label=f"desktop-locate:{provider}",
            )
        except KeyChainEmpty:
            return {"error": (
                f"ERROR: no API key configured for the vision provider '{provider}' "
                "— cannot locate the target on screen."
            )}
        except Exception as e:
            return {"error": f"ERROR: the vision model failed while locating the target: {e}"}

        # Strict schema: the reply MUST be a balanced JSON object — no falling
        # back to the raw text (which would let prose smuggle in numbers).
        blob = _extract_json_object(raw)
        if blob is None:
            return {"error": (
                f"ERROR: vision model did not return a JSON object while locating "
                f"\"{description}\" — refusing to guess: {raw[:200]}"
            )}
        try:
            data = json.loads(blob, strict=False)
        except Exception:
            return {"error": (
                f"ERROR: could not parse the vision model's reply while locating "
                f"\"{description}\": {raw[:200]}"
            )}
        # The object must match {"found": bool, "x": int, "y": int} exactly:
        # "found" present and a real bool (not a truthy string/number). Anything
        # else is rejected rather than guessed (anti-hallucination).
        if not isinstance(data, dict) or not isinstance(data.get("found"), bool):
            return {"error": (
                "ERROR: vision model returned an invalid response (expected "
                "{\"found\": bool, \"x\": int, \"y\": int}) while locating "
                f"\"{description}\": {raw[:200]}"
            )}
        if not data["found"]:
            reason = data.get("reason") or "element not visible on screen"
            return {"found": False, "reason": str(reason)}
        # found == True ⇒ x and y must both be numbers (int/float), never bool,
        # never a string. No coercion from other types.
        xv, yv = data.get("x"), data.get("y")
        if (not isinstance(xv, (int, float)) or isinstance(xv, bool)
                or not isinstance(yv, (int, float)) or isinstance(yv, bool)):
            return {"error": (
                f"ERROR: the vision model reported finding \"{description}\" but "
                f"returned no valid integer coordinates: {raw[:200]}"
            )}
        x = int(round(float(xv)))
        y = int(round(float(yv)))
        # Reject suspicious coordinates: a vision model that did NOT actually
        # find the element often defaults to (0,0) / negatives / off-screen.
        # Clicking the top-left corner is worse than reporting failure.
        if (x, y) == (0, 0) or x < 0 or y < 0 or (w and x >= w) or (h and y >= h):
            bounds = f", screen is {w}x{h}" if (w and h) else ""
            return {"error": (
                f"ERROR: vision model returned invalid coordinates ({x},{y}{bounds}) "
                f"for \"{description}\" — element probably not found."
            )}
        return {"found": True, "x": x, "y": y, "w": w, "h": h}
    except Exception as e:
        return {"error": f"ERROR: locating \"{description}\" on screen failed: {e}"}


def _summarize_description(desc: str) -> str:
    """Collapse a (possibly multi-line) screenshot description to a single short
    line for inline BEFORE/AFTER reporting in ``tool_desktop_click``."""
    if not desc:
        return ""
    one_line = " ".join(str(desc).split())
    return (one_line[:200] + "…") if len(one_line) > 200 else one_line


def _descriptions_nearly_identical(a: str, b: str) -> bool:
    """True when two screenshot descriptions are so similar that the screen
    likely did not change between them — used to flag a click that missed.

    Compares the full (normalised) descriptions, not just their summaries, so a
    change anywhere on screen breaks the match.
    """
    import difflib
    na = " ".join(str(a).split()).lower()
    nb = " ".join(str(b).split()).lower()
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.92


def _screens_changed(img_path_a: str, img_path_b: str, threshold: float = 1.0) -> bool:
    """Pure-local check (NO vision/network call): did the screen visibly change
    between two screenshots?

    Both images are down-scaled to a small grayscale thumbnail and the mean
    per-pixel absolute difference (0–255 scale) is computed; a value strictly
    above ``threshold`` means the screen changed. A lossless capture of an
    unchanged screen scores ~0, so the small default (1.0) cleanly separates
    "nothing happened" from a real change (menu/dialog/navigation, even a
    button-sized region) while ignoring trivial noise (caret blink, clock tick).

    Used by :func:`tool_desktop_click` to flag a click that probably missed,
    WITHOUT the two vision describe calls the old miss-check needed (those
    dominated click latency at ~4–8s each). Uses numpy when available (fast
    path) and falls back to a pure-Pillow ``ImageChops`` diff otherwise — both
    are already project dependencies. Any failure (Pillow missing, file
    unreadable) returns ``True`` (assume changed) so a genuine click is never
    falsely flagged as a no-op — mirroring the old guard that skipped the
    warning when a description was unavailable.
    """
    try:
        from PIL import Image
        size = (64, 64)
        with Image.open(img_path_a) as ia, Image.open(img_path_b) as ib:
            thumb_a = ia.convert("L").resize(size)
            thumb_b = ib.convert("L").resize(size)
        try:
            import numpy as _np
            arr_a = _np.asarray(thumb_a, dtype=_np.float32)
            arr_b = _np.asarray(thumb_b, dtype=_np.float32)
            mean_delta = float(_np.abs(arr_a - arr_b).mean())
        except Exception:
            # No numpy: mean of the per-pixel difference via its histogram.
            from PIL import ImageChops
            hist = ImageChops.difference(thumb_a, thumb_b).histogram()
            count = sum(hist)
            mean_delta = (
                sum(i * c for i, c in enumerate(hist)) / count
            ) if count else 0.0
        return mean_delta > threshold
    except Exception:
        return True


_REFINE_CROP_PX = 300   # side of the square crop used by the fine (2nd-pass) locate

# Zoom factor for the 2nd-pass crop (WOR-46 vol2). The crop is upscaled by this
# factor with LANCZOS before being sent to the vision model, so the element is
# genuinely LARGER on the image the model sees (a 30px button becomes 60px at
# Z=2) — that is what gives the refinement pass real sub-pixel precision over the
# coarse full-screen locate. The model then answers in UPSCALED-crop pixel space,
# so the crop→screen remap MUST divide the model's coordinates by this factor
# (see _vision_refine_crop: fx = x0 + cx/Z). The two MUST stay in lockstep: an
# off-by-Z here would send the click to Z× the intended offset. Single tunable
# constant; 2 is a safe default (3 zooms harder but enlarges the file / latency).
_REFINE_ZOOM = 2

# Max distance (px) the 2nd-pass refinement may move the click from the coarse
# point. A genuine refinement only NUDGES the target (a few px to a few tens of
# px) — the crop is only 300px wide, so a centred crop can never honestly move
# the point more than ~150px. Anything beyond that means the refinement drifted
# (vision returned bad/absolute coords) → discard it and keep the coarse point.
# This is measured in SCREEN pixels AFTER the /Z remap (never on upscaled coords).
# Near a screen edge _crop_window clamps the origin so the coarse point sits
# off-centre and the crop legitimately reaches further on one side — the guard is
# relaxed for that case in _refine_click_target (max of this and the crop's real
# per-axis reach) so a genuine far-side refinement isn't falsely rejected.
_MAX_REFINE_DRIFT_PX = 150

# A/B toggle for the 2nd-pass fine-locate refinement in tool_desktop_click.
# Override at runtime with the env var DESKTOP_CLICK_REFINE (1/true/yes/on to
# enable, 0/false/no/off to disable). Default is FALSE pending live A/B
# validation: WOR-46 vol2 added the missing real ZOOM (the crop is now upscaled
# by _REFINE_ZOOM so the model sees the element genuinely larger) and made the
# crop→screen remap zoom-aware (divide by Z, in lockstep with the upscale), which
# is what the earlier "drifts further away" observation was caused by (no zoom →
# no added precision). The default stays OFF until that fix is confirmed reliably
# better than coarse-only on a real no-accessibility app (e.g. Steam); enable via
# the env var to run the A/B without a code change.
_REFINE_DEFAULT = False


def _refine_flag() -> bool:
    """Resolve the DESKTOP_CLICK_REFINE toggle from the environment, falling back
    to ``_REFINE_DEFAULT`` when the var is unset/blank."""
    raw = os.environ.get("DESKTOP_CLICK_REFINE")
    if raw is None or not raw.strip():
        return _REFINE_DEFAULT
    return raw.strip().lower() in ("1", "true", "yes", "on")


DESKTOP_CLICK_REFINE = _refine_flag()


def _crop_window(center: int, span: int, extent: int):
    """Top-left coordinate and size of a ``span``-pixel window centred on
    ``center`` and kept fully inside ``[0, extent]`` (shrinks to ``extent`` when
    the image is smaller than ``span``). Returns ``(start, size)``."""
    if extent <= span:
        return 0, extent
    start = max(0, min(int(center) - span // 2, extent - span))
    return start, span


def _vision_refine_crop(path: str, description: str, x0: int, y0: int,
                        cw: int, ch: int):
    """Second-pass fine locate inside a crop. Asks the vision model for the
    corrected CENTER of ``description`` WITHIN the crop at ``path`` (whose
    top-left sits at screen ``(x0, y0)``), then maps the crop-relative answer back
    to absolute screen pixels. Returns ``(screen_x, screen_y)``, or ``None`` on
    ANY failure / not-found / out-of-crop result so the caller keeps the coarse
    coordinates (best-effort refinement — it must never fail the click).

    ``cw``/``ch`` are the NATIVE (screen-space) crop dimensions. The image saved
    at ``path`` was upscaled by ``_REFINE_ZOOM`` (Z), so the model sees a
    ``(cw*Z) x (ch*Z)`` image and answers in that UPSCALED pixel space. The
    remap therefore divides by Z to return to screen pixels — kept in lockstep
    with the upscale in :func:`_refine_click_target`: ``fx = x0 + cx/Z``,
    ``fy = y0 + cy/Z``.

    Mirrors :func:`_vision_locate`'s key_chain rotation and strict-JSON contract,
    on a small crop and with a refinement prompt (CLAUDE.md: every new vision
    call site rotates keys via ``with_key_rotation``)."""
    try:
        import base64
        from osia.config import load_settings
        from osia.vision_router import resolve_vision_model, build_vision_client
        from osia.models import model_has_vision
        from osia.utils.key_chain import with_key_rotation

        settings = load_settings()
        provider, model = resolve_vision_model(settings)
        if not provider or not model or not model_has_vision(provider, model):
            return None
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")

        # The crop on disk was upscaled by Z (LANCZOS) in _refine_click_target, so
        # the model sees a (cw*Z) x (ch*Z) image and must answer in that space.
        zoom = _REFINE_ZOOM
        uw, uh = cw * zoom, ch * zoom

        prompt = (
            f"You are looking at a zoomed-in {uw}x{uh}-pixel CROP of a computer "
            "screen; the top-left of this crop is (0,0). The mouse cursor needs to "
            f"click on this element: \"{description}\".\n"
            "Give the corrected coordinates of the CENTER of that element, measured "
            "WITHIN this crop image (relative to the crop's own top-left, in this "
            f"image's own {uw}x{uh} pixels).\n"
            "Reply with ONLY a JSON object — no prose, no markdown fences.\n"
            "If you can see it: {\"found\": true, \"x\": <int>, \"y\": <int>} "
            "(crop-relative pixels).\n"
            "If it is not inside this crop: {\"found\": false, \"reason\": \"<short reason>\"}.\n"
            "NEVER guess coordinates."
        )

        def _call(call_provider, call_model):
            def _do(key):
                client = build_vision_client(call_provider, key)
                resp = client.chat.completions.create(
                    model=call_model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{b64}"}},
                        ],
                    }],
                    timeout=25,
                )
                return (resp.choices[0].message.content or "").strip()
            return _do

        try:
            raw = with_key_rotation(
                provider, _call(provider, model),
                label=f"desktop-refine:{provider}",
            )
        except Exception:
            return None   # quota/auth/network/no-key -> keep the coarse coordinates

        blob = _extract_json_object(raw)
        if blob is None:
            return None
        try:
            data = json.loads(blob, strict=False)
        except Exception:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("found"), bool):
            return None
        if not data["found"]:
            return None
        xv, yv = data.get("x"), data.get("y")
        if (not isinstance(xv, (int, float)) or isinstance(xv, bool)
                or not isinstance(yv, (int, float)) or isinstance(yv, bool)):
            return None
        cx = int(round(float(xv)))
        cy = int(round(float(yv)))
        # The corrected point must land inside the UPSCALED crop the model saw
        # (cw*Z x ch*Z); otherwise the refinement is bogus (model hallucinated /
        # leaked absolute coords) -> discard and keep the coarse target.
        if not (0 <= cx < uw and 0 <= cy < uh):
            print(f"[DESKTOP-REFINE] vision crop-relative ({cx},{cy}) outside "
                  f"upscaled crop {uw}x{uh} (Z={zoom}) — discarding refinement")
            return None
        # Crop→screen conversion (zoom-aware, LOCKSTEP with the Z upscale): the
        # model answered in upscaled-crop pixels, so divide by Z before adding the
        # crop origin. fx = x0 + cx/Z, fy = y0 + cy/Z.
        fx = int(round(x0 + cx / zoom))
        fy = int(round(y0 + cy / zoom))
        print(f"[DESKTOP-REFINE] crop origin=({x0},{y0}) native={cw}x{ch} "
              f"upscaled={uw}x{uh} (Z={zoom}) vision upscaled-relative=({cx},{cy}) "
              f"-> screen=({fx},{fy})")
        return (fx, fy)
    except Exception:
        return None


def _refine_click_target(x: int, y: int, description: str, _dc):
    """Second pass of the vision-guided click for precision on small targets.

    Moves the cursor to the coarse point (NO click), re-captures the screen,
    crops a ``_REFINE_CROP_PX`` square centred on that point, and asks the vision
    model for the corrected element centre inside that crop (a zoomed view locates
    small elements — text links, tiny icons — far more precisely than the full
    screen). Returns ``(screen_x, screen_y, refined)``: the sharpened coordinates
    and ``True``, or the untouched ``(x, y)`` and ``False`` on ANY failure, so the
    caller still clicks the coarse point rather than failing the whole click."""
    try:
        # Move the cursor to the coarse target first (no click). If even the move
        # fails, the backend is not ready -> let the caller's click surface it.
        mv = _dc.move_to(x, y)
        if isinstance(mv, str) and mv.upper().startswith("ERROR"):
            return x, y, False
        try:
            from PIL import Image
        except Exception:
            return x, y, False   # no Pillow -> cannot crop; keep the coarse target
        refine_shot = str(Path(tempfile.gettempdir()) / "osia_desktop_refine.png")
        if _capture_screen(refine_shot) is not None:
            return x, y, False
        crop_path = str(Path(tempfile.gettempdir()) / "osia_desktop_crop.png")
        try:
            # Pillow ≥9.1 moved the resampling enum to Image.Resampling; fall back
            # to the (deprecated) top-level alias on older Pillow.
            _lanczos = getattr(Image, "Resampling", Image).LANCZOS
            with Image.open(refine_shot) as im:
                iw, ih = im.size
                x0, cw = _crop_window(x, _REFINE_CROP_PX, iw)
                y0, ch = _crop_window(y, _REFINE_CROP_PX, ih)
                crop = im.crop((x0, y0, x0 + cw, y0 + ch))
                # Real ZOOM (WOR-46 vol2): upscale the crop so the element is
                # genuinely larger to the vision model — the precision the 2nd
                # pass needs. The remap in _vision_refine_crop divides by the same
                # _REFINE_ZOOM (lockstep). cw/ch stay NATIVE here; the model is
                # told the upscaled cw*Z x ch*Z dims.
                if _REFINE_ZOOM != 1:
                    crop = crop.resize(
                        (cw * _REFINE_ZOOM, ch * _REFINE_ZOOM), _lanczos)
                crop.save(crop_path)
        except Exception:
            return x, y, False
        print(f"[DESKTOP-REFINE] coarse=({x},{y}) screenshot={iw}x{ih} "
              f"crop origin=({x0},{y0}) native={cw}x{ch} "
              f"upscaled={cw*_REFINE_ZOOM}x{ch*_REFINE_ZOOM} (Z={_REFINE_ZOOM})")
        refined = _vision_refine_crop(crop_path, description, x0, y0, cw, ch)
        if refined is None:
            return x, y, False
        fx, fy = refined
        # Sanity check: a real refinement only NUDGES the coarse point. If it
        # jumped further than the allowed drift, the refinement drifted (bad
        # conversion / hallucinated coords) -> discard, keep coarse. Measured in
        # SCREEN pixels, AFTER the /Z remap (refined is already screen-space).
        #
        # Edge-clamp handling: _crop_window centres the crop on the coarse point
        # EXCEPT near a screen edge, where it clamps the origin so the point sits
        # off-centre and the crop reaches further on one side. The honest per-axis
        # reach of the crop from the coarse point is then max(point→origin,
        # point→far-edge); for a centred 300px crop that is exactly 150 on each
        # axis, so the guard stays _MAX_REFINE_DRIFT_PX (=150) in the common case
        # and only WIDENS for a clamped crop — a genuine far-side refinement in a
        # clamped crop is no longer falsely rejected, while gross jumps still are.
        reach_x = max(x - x0, (x0 + cw) - x)
        reach_y = max(y - y0, (y0 + ch) - y)
        max_drift = max(_MAX_REFINE_DRIFT_PX, reach_x, reach_y)
        drift = ((fx - x) ** 2 + (fy - y) ** 2) ** 0.5
        if drift > max_drift:
            print(f"[DESKTOP-REFINE] refined=({fx},{fy}) drifts {drift:.0f}px "
                  f"from coarse ({x},{y}) > {max_drift:.0f}px "
                  f"(base {_MAX_REFINE_DRIFT_PX}, crop reach {reach_x}x{reach_y}) "
                  f"— discarding refinement, keeping coarse")
            return x, y, False
        print(f"[DESKTOP-REFINE] coarse=({x},{y}) -> refined=({fx},{fy}) "
              f"drift={drift:.0f}px (max {max_drift:.0f}px, accepted)")
        return fx, fy, True
    except Exception:
        return x, y, False


def tool_desktop_click(description: str) -> str:
    """Vision-guided click on a NATIVE desktop element described in plain words.

    Captures the screen, asks the vision model for the element's pixel
    coordinates, clicks there, then captures the screen again and compares the
    BEFORE/AFTER frames with a fast LOCAL pixel diff (no vision call) so the
    model can confirm the click actually changed something (anti-hallucination).
    A no-change result is returned as a FAILURE (ERROR:-prefixed) so the model
    sees the ⚠️ banner, knows the click likely MISSED, and re-locates/retries
    instead of assuming success (WOR-59).
    """
    if not description or not str(description).strip():
        return "ERROR: desktop_click: provide a description of what to click."
    description = str(description).strip()

    # WOR-84 — WEB DOM-FIRST. If OSIA's own browser is the foreground window and a
    # live Playwright page is driving it, click the target THROUGH the DOM (a real
    # locator.click() in the visible browser — pixel-exact, no coordinate
    # guessing). This runs BEFORE the screen capture, the WOR-46 UIA branch, and
    # the vision locate below. browser.dom_click() returns a success string on a
    # confident DOM resolution, or None on ANY miss (gate not satisfied, element
    # not found / ambiguous, canvas / cross-origin iframe, or any exception) —
    # None falls through UNCHANGED to the native paths, so the vision fallback is
    # always reachable and native (non-browser) apps are unaffected.
    try:
        from osia.tools import browser as _browser
        dom_res = _browser.dom_click(description)
    except Exception:
        dom_res = None
    if dom_res is not None:
        return dom_res

    shot = str(Path(tempfile.gettempdir()) / "osia_desktop_click.png")
    err = _capture_screen(shot)
    if err:
        return err

    from osia.tools import desktop_control as _dc

    # WOR-46 — accessibility-first / vision-fallback. On Windows, resolve the
    # element's EXACT bounding-rect center through the UI Automation backend
    # FIRST. A hit gives pixel-exact coordinates, so we skip the vision locate AND
    # the 2-pass refinement entirely (the rect center is already precise). The
    # resolver returns None on non-Windows, when uiautomation is unavailable, on
    # any fuzzy-match miss / COM error, or headless — and NEVER raises or returns
    # an "ERROR:" string — so None cleanly routes us to the existing vision path
    # below. Either way the BEFORE/AFTER verification screenshots are still taken.
    hit = _dc.resolve_element_rect(description)
    if hit is not None:
        cx, cy = hit
        fx, fy, refined = cx, cy, False
        print(f"[WOR-46] UIA resolved \"{description}\" -> ({cx}, {cy}) "
              f"— skipping vision locate + refine.")
    else:
        # WOR-90 — KEYBOARD-NAVIGATION tier (Windows-only), between UIA-direct
        # (above) and vision (below). When UIA could not resolve a clickable rect,
        # try to ACTIVATE the element by driving the keyboard: Tab/Shift+Tab across
        # the focusable controls (and the arrows within an already-open list/menu/
        # tree), reading the focused element via UIA after each keypress and pressing
        # Enter/Space on the fuzzy-matched target. It ACTIVATES (rather than
        # returning coordinates), so it runs its OWN BEFORE/AFTER screen-diff verify
        # — it returns a success string on a VERIFIED activation, or None on ANY
        # miss / non-Windows / no-op. None falls through UNCHANGED to the vision
        # locate below (the last resort), so every miss reaches vision as today.
        kbd = _dc.activate_by_keyboard(description)
        if kbd is not None:
            return kbd
        # The keyboard tier presses navigation keys (Tab / arrows) before it
        # misses, so the live screen may differ from `shot` (captured above, before
        # the tier ran). Re-capture on Windows so the vision locate AND the
        # BEFORE/AFTER miss-check below both run against the CURRENT screen — without
        # this a tier-induced change (e.g. moved selection) would desync the stale
        # `shot` and could make a missed vision click read as a false success. The
        # tier is a pure no-op off Windows, so this is gated to keep Linux/Mac
        # byte-identical to today; a re-capture failure leaves the prior `shot`.
        if sys.platform == "win32":
            _capture_screen(shot)
        loc = _vision_locate(shot, description)
        if loc.get("error"):
            return loc["error"]
        if not loc.get("found"):
            return (
                f"ERROR: could not find \"{description}\" on screen "
                f"({loc.get('reason', 'not visible')}). Take a screenshot to see what "
                "is currently displayed, then try a different description."
            )
        cx, cy = loc["x"], loc["y"]

        # Two-pass precision refinement: coarse full-screen locate -> move the cursor
        # there (no click) -> fine locate on a 300x300 crop around it -> click the
        # corrected point. Sharpens small targets (text links, tiny icons). Best-
        # effort: any hiccup (quota, vision error, not-found) falls back to the coarse
        # coordinates rather than failing the whole click. Gated by the
        # DESKTOP_CLICK_REFINE A/B flag (default OFF — coarse-only) so the zoom
        # refinement (WOR-46 vol2) can be toggled without code changes during live
        # A/B validation on a real no-accessibility app.
        if DESKTOP_CLICK_REFINE:
            fx, fy, refined = _refine_click_target(cx, cy, description, _dc)
        else:
            print(f"[DESKTOP-REFINE] disabled (DESKTOP_CLICK_REFINE=off) — "
                  f"clicking coarse ({cx}, {cy})")
            fx, fy, refined = cx, cy, False

    # Click IMMEDIATELY once coordinates are resolved — no vision describe in
    # front of the click. The BEFORE screenshot (`shot`, captured above) is
    # still kept and compared to the AFTER screenshot below via a pure-local
    # pixel diff (see _screens_changed), not a vision call.
    res = _dc.click_at(fx, fy)
    if isinstance(res, str) and res.upper().startswith("ERROR"):
        return res

    if refined and (fx, fy) != (cx, cy):
        note = (f"Clicked \"{description}\" at ({fx}, {fy}) "
                f"— refined from coarse ({cx}, {cy}).")
    else:
        note = f"Clicked \"{description}\" at ({fx}, {fy})."

    # Verification screenshot (distinct path so it never clobbers the click-shot).
    verify = str(Path(tempfile.gettempdir()) / "osia_desktop_verify.png")
    if _capture_screen(verify) is not None:
        # Re-capture failed — report the click without a before/after compare.
        return note

    # Miss-check via a PURE-LOCAL pixel diff between the BEFORE (`shot`) and
    # AFTER (`verify`) screenshots — no vision call. If the screen looks
    # unchanged, the click likely missed. Surface the miss as a real FAILURE:
    # the result is ERROR:-prefixed so `_is_tool_error` classifies it as an error,
    # which (1) wraps it in the ⚠️ TOOL FAILED banner and (2) counts it toward the
    # anti-loop error streak — a weak model can no longer skim a no-op click as if
    # it had worked; it must re-locate and retry (WOR-59). The wording makes clear
    # the cursor DID click (it just didn't land), so the model re-locates instead
    # of assuming the tool crashed. A click that DID change the screen stays a
    # normal success (plain `note`, no banner, resets the streak).
    if not _screens_changed(shot, verify):
        return (
            f"ERROR: clicked \"{description}\" at ({fx}, {fy}) but the screen "
            "did not visibly change — the click likely MISSED. Take a "
            "screenshot to re-locate the target, then retry once or try an "
            "alternative."
        )
    return note


def tool_desktop_type(text: str) -> str:
    """Type text into the currently focused native element."""
    if text is None:
        return "ERROR: desktop_type: no text provided."
    from osia.tools import desktop_control as _dc
    return _dc.type_text(str(text))


def tool_desktop_press(key: str) -> str:
    """Press a key or shortcut (e.g. "enter", "ctrl+s", "alt+F4") system-wide."""
    if not key or not str(key).strip():
        return "ERROR: desktop_press: no key provided."
    from osia.tools import desktop_control as _dc
    return _dc.press_key(str(key))


def tool_desktop_scroll(direction: str = "down", amount=3) -> str:
    """Scroll the active native window up/down/left/right."""
    from osia.tools import desktop_control as _dc
    return _dc.scroll(direction, amount)


# ---------------------------------------------------------------------------
# Deterministic verification tools (WOR-87) — confirm an outcome WITHOUT vision.
# Each returns a clear deterministic answer ("running: yes/no", …); these are NOT
# errors, so they never use the ``ERROR:`` prefix (a "no" is a successful check).
# ---------------------------------------------------------------------------

def tool_is_running(app: str) -> str:
    """Is the app's process currently running? Deterministic (psutil), no vision."""
    from osia.tools import verify
    yes = verify.app_running(app)
    return f"running: {'yes' if yes else 'no'} — process for '{app}'"


def tool_is_installed(app: str) -> str:
    """Is the app installed (executable resolvable)? Deterministic, no vision."""
    from osia.tools import verify
    res = verify.app_installed(app)
    if res is None:
        return (f"installed: unknown — OSIA can only verify installation "
                f"deterministically on Windows here. Try is_running('{app}').")
    if res:
        return f"installed: yes — '{app}' resolves to a real executable."
    return (f"installed: no — '{app}' did not resolve via App Paths / Start menu / "
            "common install folders / Store.")


def tool_has_window(app: str) -> str:
    """Does the app have a visible window right now? Deterministic, no vision."""
    from osia.tools import verify
    win = verify.app_window(app)
    if win is None:
        if sys.platform != "win32":
            return (f"window: unknown — window detection is Windows-only here; "
                    f"use is_running('{app}') instead.")
        return f"window: no — no visible window owned by '{app}' was found."
    _hwnd, title, _pid = win
    return f"window: yes — '{app}' has a visible window titled '{title}'."


def tool_steam_game_state(name: str) -> str:
    """Deterministic install/run state of a Steam game (by name or appid), read
    from Steam's own manifests — no vision. Tells the model whether the game is
    installed and whether it is running, so it never claims it launched a game
    that isn't installed (WOR-87)."""
    from osia.tools import verify
    st = verify.steam_game_state(name)
    if st is None:
        from osia.tools import steam as _steam
        if _steam.steam_path() is None:
            return (f"steam: Steam is not installed/detected on this machine — "
                    f"cannot verify '{name}'.")
        return (f"steam: '{name}' is NOT installed (no Steam appmanifest matches "
                "it). It must be installed before it can launch.")
    return (f"steam: '{st['name']}' (appid {st['appid']}) — "
            f"installed: {'yes' if st['installed'] else 'no'}, "
            f"running: {'yes' if st['running'] else 'no'}.")


def tool_web_state(expect_url: str = None, expect_text: str = None,
                   expect_selector: str = None) -> str:
    """Deterministically check the live browser page WITHOUT vision: its current
    URL and whether it contains an expected URL fragment / text / element. Gated
    on OSIA driving its own browser in the foreground (WOR-87)."""
    from osia.tools import verify
    st = verify.web_state(expect_url, expect_text, expect_selector)
    if not st.get("available"):
        return (f"web_state: unavailable — {st.get('reason', 'no live page')}. "
                "If OSIA's browser is in front use get_current_url / read_page, "
                "otherwise take_screenshot.")
    parts = [f"url: {st.get('url')}"]
    checks = st.get("checks") or {}
    for key, val in checks.items():
        parts.append(f"{key}: {'yes' if val else 'no'}")
    if not checks:
        parts.append("(no expectation given — current URL only)")
    return "web_state: " + " | ".join(parts)


TOOL_MAP = {
    # Shell & launchers
    "bash": tool_bash,
    "run_command": tool_run_command,
    "open_application": tool_open_application,
    "open_uri": tool_open_uri,
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
    # Desktop automation (native apps — OS-level mouse/keyboard, WOR-42)
    "desktop_click": tool_desktop_click,
    "desktop_type": tool_desktop_type,
    "desktop_press": tool_desktop_press,
    "desktop_scroll": tool_desktop_scroll,
    # Deterministic verification (confirm outcomes WITHOUT vision, WOR-87)
    "is_running": tool_is_running,
    "is_installed": tool_is_installed,
    "has_window": tool_has_window,
    "steam_game_state": tool_steam_game_state,
    "web_state": tool_web_state,
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


def _tool_timeout_seconds(tool_name: str, kwargs: dict) -> float:
    """Wall-clock budget for ONE tool execution in the executor. Slow
    install/download/clone shell commands get the extended budget plus a small
    grace so ``tool_bash``'s own timeout message fires before the executor's
    generic one (WOR-31); ``desktop_click`` gets 120 s because it makes up to
    three vision API calls (coarse locate + crop refinement + verification) and
    each can traverse several key rotations when keys are rate-limited; every
    other tool keeps the default 30 s.
    """
    if tool_name in ("bash", "run_command"):
        if _is_long_running_command(kwargs.get("command", "") or ""):
            return LONG_RUNNING_TIMEOUT_SECONDS + 30
    if tool_name == "desktop_click":
        return 120
    return TOOL_TIMEOUT_SECONDS


def _execute_tool_once(tool_name: str, kwargs: dict, timeout_s: float | None = None):
    """Run one tool with a wall-clock timeout (default 30 s; longer for
    install/download/clone commands — see ``_tool_timeout_seconds``). Returns
    the result (a string, or a ``{"text", "screenshot"}`` dict for navigation
    tools), or a clear [ERREUR] line on timeout/exception so the model can read
    the failure back and report it honestly. Submitting through a
    ThreadPoolExecutor means the surrounding agent loop can never be wedged by
    a stuck synchronous tool.
    """
    if timeout_s is None:
        timeout_s = TOOL_TIMEOUT_SECONDS
    fn = TOOL_MAP[tool_name]
    try:
        # Run the tool inside a copy of the CURRENT context so the per-task
        # sticky-key scope (key_chain.task_scope, a ContextVar set on the agent
        # thread) is visible inside this worker thread — ThreadPoolExecutor does
        # NOT propagate context on its own. The scope's state is a dict mutated
        # in place, so a key a tool's vision/TTS/STT call records (e.g. the 2nd
        # of desktop_click's three vision calls) is seen by the next call and by
        # the agent thread. Outside any task_scope this copy is inert.
        ctx = contextvars.copy_context()
        fut = _TOOL_EXECUTOR.submit(ctx.run, fn, **kwargs)
        result = fut.result(timeout=timeout_s)
        # Preserve dict results (browser tools) so screenshots survive.
        if isinstance(result, dict):
            return result
        return str(result)
    except FutureTimeoutError:
        return (
            f"[ERREUR] L'outil '{tool_name}' n'a pas répondu après "
            f"{int(timeout_s)}s (timeout). L'action n'a pas été confirmée."
        )
    except Exception as e:
        return f"[ERREUR] '{tool_name}' a échoué: {e}"


def _execute_tool_with_retry(tool_name: str, kwargs: dict):
    """Execute a tool and retry exactly once if the first attempt
    reports an error (or returns empty). Returns ``(result, is_error)``
    where ``is_error`` reflects the *final* attempt — callers use it to
    decide whether to keep dispatching the rest of a tool-call batch.
    """
    timeout_s = _tool_timeout_seconds(tool_name, kwargs)
    result = _execute_tool_once(tool_name, kwargs, timeout_s=timeout_s)
    # Slow install/download/clone commands are NOT auto-retried: an immediate
    # identical retry would just double an already-long wait (WOR-31).
    # ``desktop_click`` is also NOT auto-retried: a blind retry doubles the
    # (already long) wait and can double-click. Fast tools keep the
    # retry-once-on-error behavior.
    long_running = (
        tool_name in ("bash", "run_command")
        and _is_long_running_command(kwargs.get("command", "") or "")
    )
    no_retry = long_running or tool_name == "desktop_click"
    if _is_tool_error(result) and not no_retry:
        preview = _result_text(result).replace("\n", " ")[:120]
        print(f"[OSIA Agent] ↻ retry tool: {tool_name} (first attempt: {preview!r})")
        result = _execute_tool_once(tool_name, kwargs, timeout_s=timeout_s)
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
def _turn_time_limit() -> float:
    """Wall-clock budget for a single agent turn, in seconds. Defaults to
    300s (5 min); ``OSIA_TURN_TIME_LIMIT_SECONDS`` overrides it when set to a
    positive number (env takes precedence)."""
    raw = os.environ.get("OSIA_TURN_TIME_LIMIT_SECONDS")
    if raw and raw.strip():
        try:
            val = float(raw.strip())
            if val > 0:
                return val
        except ValueError:
            pass
    return 300.0


# Wall-clock budget for a single agent turn (model calls + tool execution).
TURN_TIME_LIMIT_SECONDS = _turn_time_limit()


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
            # WOR-83: internal model-steering frames (anti-loop / hallucination
            # / empty-response guards) live in self.messages so the model reads
            # them, but must NEVER be persisted — once stored they lose the
            # _internal marker (add_message keeps only role+content) and would
            # resurface on reload as a user bubble. This is the single
            # persistence boundary, so dropping them here keeps them out of the
            # store AND every conversation_history frame at once.
            if m.get("_internal"):
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

        Delegates to the shared :func:`osia.utils.key_chain.iter_keys` so the
        main chat path and every other provider call site (vision, TTS, STT)
        resolve the **identical** chain: the user's ``api_key_fallbacks`` list
        when set (key[0] = primary, the rest = fallbacks), otherwise the single
        saved key from ``api_keys.json``; ollama needs no key; blank entries are
        dropped. Behaviour is unchanged from the previous inline implementation —
        run()'s rotation on a 429/auth error still drives this list.
        """
        from osia.utils.key_chain import iter_keys
        return iter_keys(provider)

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

    def _wire_messages(self) -> list:
        """Build the exact message list sent to the provider.

        Two wire-only normalisations are applied to a SHALLOW copy — never to
        ``self.messages`` itself, so conversation history, persistence and what
        the model reads stay untouched:

        1. **Strip internal-only keys (WOR-83).** Model-steering frames added by
           the anti-loop / hallucination / empty-response guards carry an
           ``_internal`` marker (and, defensively, any future ``_``-prefixed
           bookkeeping key). The OpenAI-compatible client forwards unknown
           message keys to the provider and several reject them with a 400, so
           every ``_``-prefixed key is removed here. ``role`` and ``content``
           are kept verbatim — the model still reads the steering text.

        2. **Coalesce consecutive same-role text frames.** Those steering frames
           are role:``"user"`` purely to keep provider-required user/assistant
           alternation. Once they are filtered out of the PERSISTED history
           (see ``exported_history``), a reloaded conversation can place two
           ``assistant`` frames back to back (tool-call record + summary), which
           strict providers reject (DeepSeek: "does not support successive
           user or assistant messages"; Anthropic OpenAI-compat: "Multiple
           assistant messages in block"). Merging adjacent same-role STRING
           frames restores the alternation invariant the codebase already
           maintains. Frames with non-string (vision/list) content are never
           merged, and the merge only fires when such a duplicate exists, so
           normal turns are unaffected.

        Returns ``self.messages`` unchanged (same object) when neither
        normalisation is needed, so the common path allocates nothing.
        """
        msgs = self.messages
        has_internal = any(
            isinstance(m, dict) and any(str(k).startswith("_") for k in m)
            for m in msgs
        )
        has_adjacent_dupes = any(
            isinstance(a, dict) and isinstance(b, dict)
            and a.get("role") == b.get("role")
            and isinstance(a.get("content"), str)
            and isinstance(b.get("content"), str)
            for a, b in zip(msgs, msgs[1:])
        )
        if not has_internal and not has_adjacent_dupes:
            return msgs

        wire: list = []
        for m in msgs:
            if isinstance(m, dict):
                m = {k: v for k, v in m.items() if not str(k).startswith("_")}
                prev = wire[-1] if wire else None
                if (
                    isinstance(prev, dict)
                    and prev.get("role") == m.get("role")
                    and isinstance(prev.get("content"), str)
                    and isinstance(m.get("content"), str)
                ):
                    prev["content"] = "\n\n".join(
                        p for p in (prev["content"], m["content"]) if p
                    )
                    continue
            wire.append(m)
        return wire

    def _stream_deltas(self):
        """Yield text deltas from the OpenAI-compatible chat-completions
        stream, abstracting it so the sentence-splitting loop stays simple.

        Responses are never token-limited: no ``max_tokens`` is sent, so
        each provider applies its own native limit.
        """
        create_kwargs = {
            "model": self.model,
            "messages": self._wire_messages(),
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
                    "messages": self._wire_messages(),
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
        """Run one user turn under a per-task sticky-key scope, then delegate to
        :meth:`_run_turn` for the actual agent loop.

        Wrapping the whole turn in :func:`osia.utils.key_chain.task_scope` makes
        ONE user request = ONE task: every provider call it triggers — the main
        chat completion(s), and the vision / screenshot / TTS / STT calls made by
        the tools it runs — shares that task's *active key* (the first key that
        works is reused instead of re-walking the chain from key1 on each call),
        and the scope is cleared when the turn ends so the NEXT turn starts fresh.
        Being a context manager, the scope exits correctly on a normal return, an
        anti-loop stop, or an exception. The scope is entered exactly ONCE here,
        never per tool call, so stickiness is never reset mid-turn.
        """
        from osia.utils.key_chain import task_scope
        with task_scope():
            return self._run_turn(
                user_input,
                max_tool_rounds=max_tool_rounds,
                on_sentence=on_sentence,
                history=history,
                max_tokens=max_tokens,
                on_first_token=on_first_token,
            )

    def _run_turn(self, user_input: str, max_tool_rounds: int | None = None, on_sentence=None,
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

        # The chat path drives its OWN key rotation (the except block below +
        # _activate_next_api_key), so it never reaches with_key_rotation's
        # active-key recording. Import the explicit recorder to publish each
        # successful chat key into the task scope (no-op outside a task_scope).
        from osia.utils.key_chain import record_active_key

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
                        "messages": self._wire_messages(),
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

            # The main chat completion above succeeded on self._api_key. Publish
            # it as this task's active key so the rest of the turn — the vision /
            # screenshot / TTS / STT calls its tools make (which rotate through
            # with_key_rotation), plus any iter_keys consumer — starts from the
            # key that just worked instead of re-walking the chain from key1.
            # No-op outside a task_scope or for an unrecognised key (e.g. ollama).
            record_active_key(self.provider, self._api_key)

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
                        # WOR-83: model-steering frame, role:"user" only so the
                        # provider sees user/assistant alternation. Internal —
                        # never persisted nor shown to the user (filtered by
                        # exported_history / _displayable_history); the _internal
                        # key is stripped from the wire by _wire_messages.
                        "_internal": True,
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
                            f"Tool {tool_name} result:\n"
                            "⚠️ TOOL FAILED — the action did NOT happen: "
                            f"[ERREUR] outil inconnu: {tool_name}"
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
                    # Make failures impossible for a fast model to skim past:
                    # an error result the model might otherwise ignore gets an
                    # unmissable banner so it can never claim the action
                    # happened (WOR-31). The banner is added ONLY to the
                    # model-facing feedback — text_part stays clean for the
                    # anti-loop guard's failure log below.
                    if is_error:
                        display_text = (
                            "⚠️ TOOL FAILED — the action did NOT happen: "
                            f"{text_part}"
                        )
                    else:
                        display_text = text_part
                    result_entry = f"Tool {tool_name} result:\n{display_text}"
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
                        # WOR-83: internal model-steering frame (see _wire_messages
                        # / exported_history) — kept out of persistence + display.
                        "_internal": True,
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
                        # WOR-83: internal model-steering frame (see _wire_messages
                        # / exported_history) — kept out of persistence + display.
                        "_internal": True,
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
