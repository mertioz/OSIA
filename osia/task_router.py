#!/usr/bin/env python3
"""OSIA task router — lightweight rule/keyword task classification (Bloc 3).

Maps a user message to one of two task categories used by the AI task
hierarchy:

    "heavy"  — code, refactoring, complex reasoning, debugging,
               architecture, long-document analysis, deep/multi-source
               web research.
    "medium" — everything else: web navigation, data gathering, email,
               summaries, routine / short tasks.

Classification is rule + keyword based — NO mini-LLM. It is fast,
deterministic and dependency-free (stdlib ``re`` only) so it can run on
every turn with no latency cost. The agent uses the returned category to
pick the ordered fallback chain configured under ``ai_hierarchy`` in
``config/settings.json``.

Design note — asymmetric cost: a false "heavy" merely routes a simple
task through a stronger (slower) model, which is harmless; a false
"medium" can send a real coding task to a weak model. The keyword set
therefore leans slightly toward catching heavy work.
"""

import re

__all__ = ["classify_task", "HEAVY", "MEDIUM"]

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
    "code",        # code, coder, coding, codes
    "codag",       # FR: codage
    "program",     # program, programming, programme(FR), programmation
    "script",      # script, scripts, scripting
    "compil",      # compile, compiler, compilation, compilateur(FR)
    "refactor",    # refactor, refactoring, refactoriser(FR), refactorisation
    "implement",   # implement, implementing, implementation
    "implément",   # FR: implémenter, implémentation
    "algorithm",   # algorithm, algorithme(FR), algorithmic
    "architect",   # architect, architecture, architectural
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
    # function / class (whole-word only — stems would catch "classic",
    # "fonctionne", "functional context", etc.)
    "function", "functions", "fonction", "fonctions",
    "class", "classes", "classe",
    # bugs (FR "bogue")
    "bug", "bugs", "bogue", "bogues",
    # distinctive dev tokens, low collision as standalone words
    "api", "apis", "sql", "regex", "json", "endpoint", "endpoints",
    "backend", "frontend", "stacktrace", "traceback", "runtime",
    "compiler", "compilation",
    # optimize — whole-word only (stem would catch "optimist"/"optimistic")
    "optimize", "optimise", "optimization", "optimisation", "optimiser",
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
    # language & framework names (strong coding signal)
    "python", "javascript", "typescript",
    # deep / multi-source web research phrases (EN + FR) — imply multi-step,
    # multi-source investigation that should use the heavy model chain.
    "deep research", "thorough research", "comprehensive search",
    "find everything about", "compare plusieurs sites",
    "recherche approfondie", "recherche complète", "analyse complète",
    "dossier complet",  # "enquête" is covered by the "enquêt" stem above
)


def _build_heavy_re() -> "re.Pattern":
    parts = []
    for kw in sorted(set(_HEAVY_STEMS)):
        parts.append(r"\b" + re.escape(kw) + r"\w*")
    for kw in sorted(set(_HEAVY_WORDS)):
        parts.append(r"\b" + re.escape(kw) + r"\b")
    for kw in sorted(set(_HEAVY_LITERALS)):
        parts.append(re.escape(kw))
    return re.compile("|".join(parts), re.IGNORECASE)


_HEAVY_RE = _build_heavy_re()

# Structural signals that the message embeds source code even without any
# keyword (e.g. a pasted snippet preceded by "regarde ça"). High precision is
# essential: bare function words ("let me know", "import my contacts", "return
# this email", "private note") must NOT fire, so every branch demands genuine
# code syntax (a definition, an assignment, a module import, a brace line, …),
# not a keyword alone.
_CODE_FENCE_RE = re.compile(r"```")
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
      | </?[a-zA-Z][\w-]*\s*/?>              # html/xml/jsx tags
    """
)


def classify_task(user_message: str) -> str:
    """Classify ``user_message`` as ``"heavy"`` or ``"medium"``.

    Heavy = code / refactoring / debugging / architecture / complex
    reasoning / long-document analysis. Everything else is medium. Empty
    or non-string input is treated as medium.
    """
    if not user_message or not isinstance(user_message, str):
        return MEDIUM
    if _CODE_FENCE_RE.search(user_message) or _CODE_SIGNAL_RE.search(user_message):
        return HEAVY
    if _HEAVY_RE.search(user_message):
        return HEAVY
    return MEDIUM
