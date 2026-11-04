#!/usr/bin/env python3
"""Choose the configured model for a task before Chat accepts the turn.

Maps a user message to one of two task categories used by the AI task
hierarchy:

    "heavy"  — code, refactoring, complex reasoning, debugging,
               architecture, long-document analysis, deep/multi-source
               web research.
    "medium" — everything else: web navigation, data gathering, email,
               summaries, routine / short tasks.

Classifying the request only chooses a configured model. The chosen model
owns the task and all subsequent tool decisions for the entire turn.
"""

import re

__all__ = ["classify_task", "available_hierarchy_models", "resolve_chat_route_binding", "HEAVY", "MEDIUM"]

HEAVY = "heavy"
MEDIUM = "medium"

# ── Keyword tables ─────────────────────────────────────────────────────────
# Three match modes, combined into one regex:
#   STEMS    — prefix/stem match (``\bkw\w*``): one entry covers a whole word
#              family across EN+FR, e.g. "refactor" → refactor / refactoring /
#              refactoriser / refactorisation.
#   WORDS    — whole-word match (``\bkw\b``): used where a stem would over-match
#              (e.g. "class" must not fire on "classic"/"classroom").
#   LITERALS — plain substring: phrases and symbol tokens that carry no clean
#              word boundary (e.g. "c++", "erreur de code", "stack trace").
# All matching is case-insensitive; ``\w`` is Unicode so accented French words
# ("débugge", "implémenter") get proper boundaries.

_HEAVY_STEMS = (
    # — code / programming (EN, shared EN+FR where the stem allows) —
    "codag",       # FR: codage
    "refactor",    # refactor, refactoring, refactoriser(FR), refactorisation
    "implement",   # implement, implementing, implementation
    "implément",   # FR: implémenter, implémentation
    "algorithm",   # algorithm, algorithme(FR), algorithmic
    "recursi",     # recursion, recursive
    "récursi",     # FR: récursion, récursif
    # — debugging (EN + accented FR variants) —
    "debug",       # debug, debugging, debugger, debugged
    "débug",       # FR: débugge, débuggage
    "debog",       # debogue, deboguer (unaccented FR spelling)
    "débog",       # FR: débogue, déboguer, débogage, débogueur
    # — deep / multi-source web research (implies multi-step investigation) —
    "investigat",  # investigate, investigates, investigating, investigation
    "enquêt",      # FR: enquête, enquêter, enquêtes, enquêteur
    # NOTE: bare "analyse"/"analyze" intentionally NOT keywords — they collide
    # with everyday MEDIUM requests ("analyse du marché", "analyze my sales").
    # Genuinely-heavy analysis ("analyse cet algorithme", "analyze this code")
    # already carries code/algorithm/architecture keywords below.
)

_HEAVY_WORDS = (
    # bugs (FR "bogue")
    "bug", "bugs", "bogue", "bogues",
    # distinctive dev tokens, low collision as standalone words
    "stacktrace", "traceback",
    "compiler", "compilation",
    # reasoning / proofs
    "reasoning", "theorem", "theorems",
)

_HEAVY_LITERALS = (
    # phrases (EN + FR)
    "erreur de code", "erreur de compilation", "stack trace",
    "source code", "code source", "design pattern", "data structure",
    "unit test", "test unitaire", "ligne de code", "lines of code",
    # symbol / dotted tokens that have no clean word boundary
    "c++", "c#", "node.js", "nodejs",
    # deep / multi-source web research phrases (EN + FR) — imply multi-step,
    # multi-source investigation that should use the heavy model chain.
    "deep research", "thorough research", "comprehensive search",
    "find everything about", "compare plusieurs sites",
    "recherche approfondie", "recherche complète",
)


def _build_heavy_re() -> "re.Pattern":
    parts = []
    for kw in sorted(set(_HEAVY_STEMS)):
        parts.append(r"\b" + re.escape(kw) + r"\w*")
    for kw in sorted(set(_HEAVY_WORDS)):
        parts.append(r"\b" + re.escape(kw) + r"\b")
    for kw in sorted(set(_HEAVY_LITERALS)):
        parts.append(r"\b" + re.escape(kw) + (r"\b" if kw[-1].isalnum() else ""))
    return re.compile("|".join(parts), re.IGNORECASE)


_HEAVY_RE = _build_heavy_re()

# Structural signals that the message embeds source code even without any
# keyword (e.g. a pasted snippet preceded by "regarde ça"). High precision is
# essential: bare function words ("let me know", "import my contacts", "return
# this email", "private note") must NOT fire, so every branch demands genuine
# code syntax (a definition, an assignment, a module import, a brace line, …),
# not a keyword alone.
_CODE_SIGNAL_RE = re.compile(
    r"""(?mx)
        \#\s*include\b                       # C/C++ include
      | ^\s*from\s+[\w.]+\s+import\s+\w       # python: from x import y
      | ^\s*import\s+[\w.]+(?:\s*,\s*[\w.]+)*\s*$   # python: import x[, y] (line-only)
      | \b(?:def|func|function|fn)\s+\w+\s*\(      # function definition
      | \bclass\s+\w+\s*[:({]                # class definition (class Foo: / { / ( )
      | \b(?:const|let|var)\s+\w+\s*=        # js/ts variable assignment
      | (?:for|while|if|switch)\s*\([^)]*\)\s*[{:]?   # control-flow with condition
      | =>                                   # arrow functions
      | ::                                   # scope resolution
      | ^\s*[{}]\s*$                          # a lone brace line (block delimiter)
      | ^\s*```(?:py(?:thon)?|js|javascript|ts|typescript|jsx|tsx|rust|go|java|sql|c\+\+|html|css|bash|powershell)\s*$
    """
)

_TECHNICAL_INTENT_RE = re.compile(
    r"\b(?:writ\w*|écri\w*|rédig\w*|build\w*|develop\w*|développ\w*|fais|faites|make|compil\w*|"
    r"cré(?:e|er|ez|ation)|design\w*|conçoi\w*|concev\w*|fix\w*|"
    r"corrig\w*|répar\w*|test\w*|optimis\w*|optimiz\w*|explain\w*|"
    r"expliqu\w*|analy[sz]\w*|diagnos\w*|review\w*|migr\w*)\b", re.I,
)
_TECHNICAL_TARGET_RE = re.compile(
    r"\b(?:(?<!confirmation )(?<!verification )(?<!discount )(?<!security )"
    r"code(?!\s+de\s+(?:confirmation|vérification|sécurité|réduction|suivi)\b)|"
    r"scripts?|python|javascript|typescript|api|backend|frontend|endpoint|"
    r"sql|regex|architecture|algorith\w*|fonctions?|functions?|stack|logs?|"
    r"test suite|dépôt git|repo(?:sitory)?|application|logiciel)\b", re.I,
)
_RESEARCH_INTENT_RE = re.compile(
    r"\b(?:cross[- ]check|cross[- ]reference|crois\w*|compar\w*|synthéti\w*|"
    r"synthesi[sz]\w*|analy[sz]\w*)\b", re.I,
)
_RESEARCH_SCOPE_RE = re.compile(
    r"\b(?:sources? indépendantes?|"
    r"independent sources?|plusieurs (?:sources|documents|rapports|études)|"
    r"multiple (?:sources|documents|reports|studies)|full (?:report|dataset)|"
    r"(?:rapport|dossier) (?:complet|de \d{2,} pages))\b", re.I,
)


def classify_task(user_message: str) -> str:
    """Classify ``user_message`` as ``"heavy"`` or ``"medium"``.

    Heavy = code / refactoring / debugging / architecture / complex
    reasoning / long-document analysis. Everything else is medium. Empty
    or non-string input is treated as medium.
    """
    if not user_message or not isinstance(user_message, str):
        return MEDIUM
    if (
        _CODE_SIGNAL_RE.search(user_message)
        or _HEAVY_RE.search(user_message)
        or (_RESEARCH_INTENT_RE.search(user_message) and _RESEARCH_SCOPE_RE.search(user_message))
        or (_TECHNICAL_INTENT_RE.search(user_message) and _TECHNICAL_TARGET_RE.search(user_message))
    ):
        return HEAVY
    return MEDIUM


def available_hierarchy_models(account_models=None, *, allow_last_listed=False) -> list[dict]:
    """Read the current eligible choices without starting provider requests."""
    from osia.chatgpt_account.provider import get_cached_account_models
    from osia.config import load_api_keys
    from osia.mobile_model_catalogue import build_model_inventory, configured_api_providers
    from osia.models import get_all_models_cached

    if account_models is None:
        account_models = get_cached_account_models()
    inventory = build_model_inventory(
        get_all_models_cached(), configured_api_providers(load_api_keys()),
        {"connected": bool(account_models), "available": bool(account_models),
         "models": account_models},
    )
    return [
        entry for entry in inventory
        if entry["available"] or (
            allow_last_listed and entry["catalogue_source"] == "provider_discovery"
            and entry["availability"] == "stale"
        )
    ]


def resolve_chat_route_binding(settings, default_model, turn_directives=None, *, task_text="") -> dict[str, str]:
    """Bind one model and its reasoning before PC or Mobile persists a turn."""
    from osia.models import provider_for_model_cached
    from osia.reasoning import resolve_reasoning_option_id

    model_override = getattr(turn_directives, "model_override", None)
    reasoning_override = getattr(turn_directives, "reasoning_override", None)
    if model_override is not None:
        provider = str(model_override.provider_id or "").strip().lower()
        model = str(model_override.model_id or "").strip()
    else:
        model = str(settings.get("selected_model") or default_model).strip()
        provider = str(settings.get("selected_model_provider") or "").strip().lower()
        if not provider and model:
            provider = provider_for_model_cached(model)

    if (
        task_text and settings.get("ai_hierarchy_enabled")
        and model_override is None and reasoning_override is None
    ):
        hierarchy = settings.get("ai_hierarchy")
        category = classify_task(task_text)
        chain = hierarchy.get(category) if isinstance(hierarchy, dict) else None
        if isinstance(chain, list) and chain:
            # Saved priorities can use the last successful listing after its
            # picker TTL. Credential changes invalidate that listing outright.
            candidates = available_hierarchy_models(allow_last_listed=True)
            by_key = {entry["stable_key"]: entry for entry in candidates}
            for value in chain:
                if not isinstance(value, str):
                    continue
                value = value.strip()
                entry = by_key.get(value)
                if entry is None:  # Legacy chains stored bare model IDs, including Ollama tags.
                    matches = [item for item in candidates if item["model_id"] == value]
                    entry = matches[0] if len(matches) == 1 else None
                if entry is not None:
                    provider, model = entry["provider_id"], entry["model_id"]
                    print(f"[ROUTER] task={category}, bound {provider}:{model}")
                    break

    reasoning = str(reasoning_override if reasoning_override is not None else
                    resolve_reasoning_option_id(provider, model, settings)).strip().lower()
    if not all((provider, model, reasoning)):
        raise RuntimeError("canonical Chat route binding is incomplete")
    return {"provider_id": provider, "model_id": model, "effective_reasoning": reasoning}
