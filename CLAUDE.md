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
- `MAX_RESPONSE_TOKENS` was intentionally **removed** (WOR-28) — do NOT reintroduce it (providers apply their native limits). See "VALEURS CRITIQUES ET DÉCISIONS ARCHITECTURALES" below.
- Provider config in `osia/models.py` (base URLs, model IDs)

## PROTOCOLE QUALITÉ — NE PAS CASSER L'EXISTANT EN CORRIGEANT

> Section **obligatoire**. Ces règles s'appliquent à **CHAQUE tâche**, sans
> exception, en plus des sections « Before ANY change » / « When fixing a bug » /
> « After every change » ci-dessous. **Principe directeur : la qualité prime sur
> la vitesse. Un fix qui crée une régression ailleurs est un échec, même s'il
> résout le problème initial.**

1. **Périmètre minimal** — ne modifier **QUE** ce qui est strictement nécessaire
   pour la tâche en cours. **Aucun** refactoring hors-scope, **aucune**
   « amélioration » non demandée. Ne pas toucher aux chemins de code voisins.

2. **Identifier les call sites AVANT de modifier** — avant de changer une
   fonction, lister **tous** ses appelants (`grep` sur le nom de la fonction /
   symbole) et vérifier que le changement n'en casse aucun. **Si la signature
   change, mettre à jour TOUS les appelants** dans le même changement.

3. **Vérifier les features adjacentes après chaque fix** — après une correction,
   vérifier que les fonctionnalités liées (celles qui partagent le **même
   fichier / module / flux**) fonctionnent toujours. **Lister dans le résumé**
   ce qui a été vérifié.

4. **`py_compile` obligatoire** — exécuter `python -m py_compile` sur **tous**
   les fichiers modifiés **+ un test d'import** du / des module(s) touché(s)
   (vérifier l'absence d'**import circulaire**) avant de considérer la tâche
   terminée.

5. **Revue adversariale pour les changements non triviaux** — lancer une revue
   **multi-angles** (correctness / régression / conformité CLAUDE.md), vérifier
   **chaque finding indépendamment**, et corriger les vrais problèmes.

6. **Préserver les contrats existants** — les valeurs et mécanismes protégés ne
   doivent **JAMAIS** régresser : `POPUP_W` (= `520`), la résolution `CONFIG_DIR`
   (`_resolve_config_dir` + import dans `server.py`), l'**absence** de
   `MAX_RESPONSE_TOKENS`, le système `key_chain` (fallback de clés), le parser de
   tool-calls (`_extract_tool_calls` & co.), l'ordre du pipeline
   `strip_raw_tool_tags` / `strip_reasoning_blocks` (parse avant strip),
   `_AntiLoopGuard`, le **protocole WebSocket** (`osia/server/server.py`), le
   **contrat anti-hallucination `ERROR:`**, et les gardes `sys.platform`. Voir
   « CRITICAL » ci-dessus et « VALEURS CRITIQUES » ci-dessous.

7. **Résumé précis et complet** — lister dans le résumé final : **(a)** les
   fichiers modifiés, **(b)** ce qui a été vérifié (call sites, `py_compile`,
   import, features adjacentes), et **(c)** tout effet de bord potentiel
   identifié.

## VALEURS CRITIQUES ET DÉCISIONS ARCHITECTURALES

> Valeurs et choix **délibérés** (souvent WOR-28) — à ne PAS « corriger »,
> réinitialiser ni réintroduire. Avant tout changement (et avant tout merge),
> vérifier que chacune est préservée à l'identique.

- **`POPUP_W = 520`** (`osia/gui/main_popup.py:71`) — largeur du popup principal,
  consommée par `setFixedSize(POPUP_W, POPUP_H)` (`main_popup.py:4311`). Valeur
  **délibérée** (WOR-28 l'avait passée de `480` → `560` ; réduite ensuite à `520`
  pour un popup plus compact, sur demande explicite). **Ne JAMAIS** la « corriger »
  ni la repasser à `480`/`560` sans demande explicite.

- **`CONFIG_DIR`** — **toujours** résolu via `_resolve_config_dir()`
  (`osia/config.py`), **jamais codé en dur**. Règle de résolution : si
  `<project>/config/settings.json` existe → utiliser `<project>/config`
  (mode dev/repo). Sinon → `Path.home() / ".osia"` (mode installé/Windows,
  identique sur Windows `C:\Users\<user>\.osia`, Mac et Linux). Tout autre
  module doit **importer `CONFIG_DIR` depuis `osia.config`** — jamais le
  redéfinir ni recopier la logique de résolution.

- **`CONFIG_DIR` dans `server.py`** — **importé** depuis `osia.config`
  (`from osia.config import CONFIG_DIR as _CONFIG_DIR`, `osia/server/server.py:79`),
  **jamais redéfini ni recalculé**. Le serveur l'enveloppe seulement dans
  `Path(...)` (`server.py:92`) car `osia.config.CONFIG_DIR` est un `str` et le
  serveur fait des opérations `Path`. But : le serveur et la GUI résolvent
  **EXACTEMENT le même** dossier via `_resolve_config_dir()`.

- **`MAX_RESPONSE_TOKENS`** — **supprimé volontairement (WOR-28)**. Les providers
  appliquent leurs **limites natives**. **Ne JAMAIS le réintroduire** — ni la
  valeur `32768`, ni un plafond équivalent. Son absence de tout l'arbre est voulue.

- **`QT_QPA_PLATFORM=xcb`** — **Linux uniquement** (X11 only), gardé par
  `sys.platform.startswith("linux")`. **Ne JAMAIS** le définir sur Windows/Mac
  (`osia/main.py`, `osia/gui/dynamic_island.py`, `osia/gui/main_popup.py`, et
  l'`Exec=` du `.desktop` d'autostart Linux dans `osia/utils/assets.py`).

- **`X11BypassWindowManagerHint`** — **Linux uniquement** (X11 only) : ajouté aux
  flags de fenêtre seulement quand `sys.platform != "win32"`
  (`osia/gui/dynamic_island.py:129`). Les flags de base
  `FramelessWindowHint | WindowStaysOnTopHint | Tool` suffisent sur Windows/Mac.

- **Règle cross-platform** — tout nouveau code au comportement dépendant de l'OS
  **doit** passer par un garde `sys.platform` (`startswith("linux")`, `== "win32"`,
  etc.). **Aucun nouveau chemin Linux codé en dur** : résoudre la racine projet
  via `pathlib.Path(__file__).resolve().parent...` et les dossiers scratch via
  `tempfile.gettempdir()` (jamais `/home/OSIA/...` ni `/tmp`).

- **Règle de merge** — lors d'un merge depuis une branche, **ne JAMAIS** prendre
  un fichier entier en bloc (pas de « take theirs/ours » aveugle). Toujours
  **differ** et **vérifier** que les valeurs critiques ci-dessus sont préservées :
  `POPUP_W = 520`, la résolution `CONFIG_DIR` (`_resolve_config_dir` + import dans
  `server.py`), l'**absence** de `MAX_RESPONSE_TOKENS`, et les gardes
  `sys.platform` (`xcb`, `X11BypassWindowManagerHint`).

- **Système de fallback de clés universel (`key_chain`)** —
  `osia/utils/key_chain.py` est la **source unique de vérité** pour la rotation
  des clés API. Règles à **ne JAMAIS casser** :
  - Tout **NOUVEAU** call site API (vision, TTS, STT, Live API, futurs providers)
    **DOIT** utiliser `with_key_rotation()` ou `iter_keys()` de `key_chain` —
    **jamais** un appel direct avec une seule clé via `get_api_key()`.
  - La rotation se déclenche **uniquement** sur erreurs techniques
    (`429`/`5xx`/`401`/`403`/`408`/timeout/connection) — **jamais** sur les
    erreurs de contenu (`400`).
  - `_provider_key_chain` dans `agent.py` **délègue** à `key_chain.iter_keys` —
    **ne pas dupliquer** la logique.
  - Les logs de rotation **masquent** les clés (4 derniers chars uniquement).
  - **Call sites protégés actuellement** : `agent.py` (chat principal +
    `_describe_screenshot`), `vision_router.py`, `voice/tts.py`, `voice/stt.py`,
    `server/server.py` (TTS mp3), `voice/live_api.py`.

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
