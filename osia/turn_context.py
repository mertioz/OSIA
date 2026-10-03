#!/usr/bin/env python3
"""Per-turn language ownership and privacy-safe turn observability (WOR-177)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import re
import secrets
import unicodedata

from langdetect import DetectorFactory, detect_langs

from osia import i18n


_LOG = logging.getLogger("osia.turn")
DetectorFactory.seed = 0
_TURN_LANGUAGES = frozenset({
    "ar", "de", "el", "en", "es", "fr", "he", "hi", "it", "ja",
    "ko", "nl", "pl", "pt", "ru", "th", "tr", "zh",
})
_LANGUAGE_NAMES = {
    "en": ("english", "anglais", "inglés", "ingles", "englisch"),
    "fr": ("french", "français", "francais", "französisch", "franzoesisch"),
    "zh": ("chinese", "chinois", "chino", "chinesisch", "中文", "普通话", "普通話"),
    "ja": ("japanese", "japonais", "japonés", "japones", "japanisch", "日本語"),
    "de": ("german", "allemand", "alemán", "aleman", "deutsch"),
    "es": ("spanish", "espagnol", "español", "espanol", "spanisch"),
    "ar": ("arabic", "arabe", "árabe", "arabisch", "العربية"),
    "ru": ("russian", "russe", "ruso", "russisch", "русский"),
    "it": ("italian", "italien", "italiano", "italienisch"),
    "pt": ("portuguese", "portugais", "português", "portugues"),
    "ko": ("korean", "coréen", "coreen", "한국어"),
}
_LANG_LABELS = {
    "ar": "Arabic", "de": "German", "el": "Greek", "en": "English",
    "es": "Spanish", "fr": "French", "he": "Hebrew", "hi": "Hindi",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "nl": "Dutch",
    "pl": "Polish", "pt": "Portuguese", "ru": "Russian", "th": "Thai",
    "tr": "Turkish", "zh": "Chinese",
}
_LEXICAL_HINTS = {
    "en": frozenset({
        "please", "thanks", "thank", "hello", "could", "would", "answer",
        "show", "how", "help", "doing", "well", "sure", "okay",
        "continuing", "now",
    }),
    "fr": frozenset({
        "bonjour", "salut", "merci", "sûr", "sur", "donc", "peux", "réponds",
        "reponds", "fais", "produire", "pourquoi", "français", "francais",
    }),
    "de": frozenset({
        "hallo", "danke", "bitte", "kannst", "antwort", "warum", "deutsch",
    }),
    "es": frozenset({
        "hola", "gracias", "puedes", "responde", "muestra", "español",
        "espanol", "porqué", "porque", "cómo", "como",
    }),
    "it": frozenset({"ciao", "grazie", "italiano", "perché", "perche"}),
    "pt": frozenset({"olá", "ola", "obrigado", "português", "portugues"}),
}
_ASSISTANT_LEXICAL_HINTS = {
    **_LEXICAL_HINTS,
    "en": _LEXICAL_HINTS["en"] | frozenset({
        "yes", "done", "ready", "welcome", "sorry", "response",
    }),
    "fr": _LEXICAL_HINTS["fr"] | frozenset({
        "oui", "terminé", "termine", "prêt", "pret", "prête", "prete",
        "désolé", "desole", "désolée", "desolee", "volontiers",
    }),
    "de": _LEXICAL_HINTS["de"] | frozenset({
        "ja", "klar", "fertig", "bereit", "gerne",
    }),
    "es": _LEXICAL_HINTS["es"] | frozenset({
        "sí", "si", "claro", "listo", "hecho",
    }),
    "it": _LEXICAL_HINTS["it"] | frozenset({
        "sì", "si", "certo", "fatto", "pronto",
    }),
    "pt": _LEXICAL_HINTS["pt"] | frozenset({
        "sim", "claro", "feito", "pronto", "obrigada",
    }),
}

_SCRIPT_PATTERNS = (
    ("ja", re.compile(r"[\u3040-\u30ff]")),
    ("ko", re.compile(r"[\uac00-\ud7af]")),
    ("ar", re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")),
    ("he", re.compile(r"[\u0590-\u05ff]")),
    ("hi", re.compile(r"[\u0900-\u097f]")),
    ("th", re.compile(r"[\u0e00-\u0e7f]")),
    ("ru", re.compile(r"[\u0400-\u04ff]")),
    ("el", re.compile(r"[\u0370-\u03ff]")),
    ("zh", re.compile(r"[\u3400-\u9fff]")),
)


@dataclass(frozen=True)
class TurnContext:
    """One response-language preference attached only to the current user turn."""

    language: str
    source: str
    turn_ref: str
    mode: str = "single-language"
    allowed_languages: tuple[str, ...] = ()
    source_language: str | None = None
    target_language: str | None = None

    def __post_init__(self) -> None:
        canonical = (
            normalize_locale(self.target_language)
            or normalize_locale(self.language)
            or i18n.DEFAULT_LANG
        )
        object.__setattr__(self, "language", canonical)
        object.__setattr__(self, "target_language", canonical)

    @property
    def canonical_language(self) -> str:
        """The preferred output language for this turn."""

        return self.target_language or self.language


def normalize_locale(value) -> str | None:
    """Normalize a locale to OSIA's bounded turn-language registry."""

    if not isinstance(value, str):
        return None
    candidate = value.strip().lower().replace("_", "-")
    if not candidate or len(candidate) > 35:
        return None
    candidate = candidate.split(".", 1)[0].split("@", 1)[0]
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", candidate):
        return None
    code = candidate.split("-", 1)[0]
    return code if code in _TURN_LANGUAGES else None


def _fold(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    )


def _display_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            str(part.get("text") or "")
            for part in value if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(value or "")


def _lexical_language(words, *, hints_by_language=None) -> str | None:
    """Return one unambiguous language represented by the lexical registry."""

    registry = hints_by_language or _LEXICAL_HINTS
    folded_words = [_fold(str(word)) for word in words]
    lexical_scores = {
        code: sum(word in {_fold(item) for item in hints} for word in folded_words)
        for code, hints in registry.items()
    }
    ranked = sorted(lexical_scores.items(), key=lambda item: item[1], reverse=True)
    if (
        ranked
        and ranked[0][1] >= 1
        and (len(ranked) == 1 or ranked[0][1] > ranked[1][1])
    ):
        return ranked[0][0]
    return None


def _without_foreign_evidence(text: str) -> str:
    """Remove quoted/code/tool payloads before conversational language scoring."""

    visible = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    visible = re.sub(r"`[^`]*`", " ", visible)
    visible = re.sub(r"<tool_(?:call|result)>.*?</tool_(?:call|result)>", " ", visible,
                     flags=re.DOTALL | re.IGNORECASE)
    visible = re.sub(r'"[^"\n]*"|“[^”\n]*”|«[^»\n]*»', " ", visible)
    visible = re.sub(r"https?://\S+", " ", visible)
    return visible


def _mentioned_languages(text: str) -> tuple[str, ...]:
    visible = _without_foreign_evidence(_display_text(text))
    folded = _fold(visible)
    matches: list[tuple[int, str]] = []
    for code, names in _LANGUAGE_NAMES.items():
        positions = []
        for name in names:
            alias = _fold(name)
            match = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", folded)
            if match:
                positions.append(match.start())
        if positions:
            matches.append((min(positions), code))
    return tuple(code for _position, code in sorted(matches))


def _translation_target(text: str) -> str | None:
    visible = _without_foreign_evidence(_display_text(text))
    folded = _fold(visible)
    if not re.search(
        r"\b(?:translate|translation|traduis|traduire|traduction|traduce|"
        r"traducir|ubersetze|uebersetze|traduza)\b",
        folded,
    ):
        return None
    mentioned = _mentioned_languages(visible)
    if not mentioned:
        return None
    for code in reversed(mentioned):
        aliases = "|".join(re.escape(_fold(name)) for name in _LANGUAGE_NAMES[code])
        if re.search(
            r"\b(?:to|into|en|vers|al|a|auf|para)\s+(?:le |la |l')?(?:"
            + aliases + r")(?!\w)",
            folded,
        ):
            return code
    return mentioned[-1]


def _multilingual_languages(text: str) -> tuple[str, ...]:
    visible = _without_foreign_evidence(_display_text(text))
    folded = _fold(visible)
    mentioned = _mentioned_languages(visible)
    if len(mentioned) < 2:
        return ()
    if re.search(
        r"\b(?:bilingual|bilingue|multilingual|multilingue|both|deux|"
        r"languages|langues|idiomas)\b|\b(?:and|et|y)\b|[+/]",
        folded,
    ):
        return mentioned[:4]
    return ()


def explicit_language(text: str) -> str | None:
    """Return one explicitly requested response language, if present."""

    visible = _without_foreign_evidence(_display_text(text))
    folded = _fold(visible)
    if _translation_target(visible) or _multilingual_languages(visible):
        return None
    if not re.search(
        r"\b(?:reply|respond|answer|write|speak|continue|switch|use|give|"
        r"summarize|reponds|repondre|ecris|parle|passe|donne|resume|"
        r"responde|escribe|habla|"
        r"antworte|schreibe|sprich)\b",
        folded,
    ):
        return None
    mentioned = _mentioned_languages(visible)
    return mentioned[0] if mentioned else None


def detect_request_language(
    text: str, *, previous_language: str | None = None,
) -> str | None:
    """Infer user-authored prose with scripts, langdetect, then lexical hints."""

    visible = _without_foreign_evidence(_display_text(text)).strip()
    if not visible:
        return None
    if re.fullmatch(r"[A-Z0-9_:@./+-]+", visible):
        # Opaque identifiers and redacted fixture tokens are not natural prose.
        # Let the persisted/client fallback own these otherwise neutral turns.
        return None
    script_counts = [
        (len(pattern.findall(visible)), code) for code, pattern in _SCRIPT_PATTERNS
    ]
    script_count, script_language = max(script_counts, default=(0, ""))
    if script_count >= 2:
        return script_language
    words = re.findall(r"[^\W\d_]+", visible.casefold(), flags=re.UNICODE)
    if not words:
        return None
    lexical_language = _lexical_language(words)
    try:
        candidates = detect_langs(visible)
        best = candidates[0] if candidates else None
        detected = normalize_locale(getattr(best, "lang", None))
        confidence = float(getattr(best, "prob", 0.0))
    except Exception:
        detected, confidence = None, 0.0
    previous = normalize_locale(previous_language)
    short = len(words) <= 4
    if short:
        # One- and two-word replies are usually acknowledgements, commands or
        # shared technical terms ("OK", "continue", "do it", "encore").
        # ``langdetect`` is confidently wrong on several of these and used to
        # flip an otherwise stable conversation.  They are not a clear language
        # change: keep the previous turn when one exists, and otherwise prefer a
        # lexical signal/client locale over a guessed language.
        if len(words) <= 2:
            if lexical_language:
                return lexical_language
            if previous:
                return None
            return None
        if lexical_language:
            return lexical_language
        if previous and detected != previous and confidence < 0.80:
            return None
        return detected if confidence >= 0.80 else None
    if detected and confidence >= 0.80:
        return detected
    return lexical_language


def _history_language(history) -> tuple[str | None, str | None]:
    if not isinstance(history, list):
        return None, None
    for message in reversed(history):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        persisted = normalize_locale(message.get("_language"))
        if persisted:
            return persisted, "recent_user_context"
        text = _display_text(message.get("content"))
        language = explicit_language(text) or detect_request_language(text)
        if language:
            return language, "recent_user_context"
    return None, None


def _turn_ref(reference: str | None = None) -> str:
    seed = reference if isinstance(reference, str) and reference else secrets.token_hex(16)
    return "turn_" + hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:16]


def resolve_turn_context(
    user_input,
    *,
    history=None,
    client_locale=None,
    saved_language=None,
    previous_language=None,
    reference: str | None = None,
) -> TurnContext:
    """Resolve a lightweight response-language preference for this user turn."""

    text = _display_text(user_input)
    history_language, history_source = _history_language(history)
    previous = history_language or normalize_locale(previous_language)
    requested = explicit_language(text)
    if requested:
        context = TurnContext(
            requested, "explicit_request", _turn_ref(reference),
            allowed_languages=(requested,),
            target_language=requested,
        )
        log_turn_context(context)
        return context
    translation_target = _translation_target(text)
    if translation_target:
        source_language = detect_request_language(text, previous_language=previous)
        allowed = tuple(dict.fromkeys(
            code for code in (source_language, translation_target) if code
        ))
        context = TurnContext(
            translation_target,
            "translation_target",
            _turn_ref(reference),
            mode="translation",
            allowed_languages=allowed or (translation_target,),
            source_language=source_language,
            target_language=translation_target,
        )
        log_turn_context(context)
        return context
    multilingual = _multilingual_languages(text)
    if multilingual:
        dominant = detect_request_language(text, previous_language=previous)
        context = TurnContext(
            dominant if dominant in multilingual else multilingual[0],
            "explicit_multilingual",
            _turn_ref(reference),
            mode="multilingual",
            allowed_languages=multilingual,
            target_language=(
                dominant if dominant in multilingual else multilingual[0]
            ),
        )
        log_turn_context(context)
        return context
    candidates = (
        (detect_request_language(text, previous_language=previous), "current_request"),
        (previous, history_source or "recent_user_context"),
    )
    for language, source in candidates:
        if language:
            context = TurnContext(
                language, source, _turn_ref(reference),
                allowed_languages=(language,),
                target_language=language,
            )
            log_turn_context(context)
            return context

    context = TurnContext(
        i18n.DEFAULT_LANG,
        "natural_model",
        _turn_ref(reference),
        allowed_languages=(i18n.DEFAULT_LANG,),
        target_language=i18n.DEFAULT_LANG,
    )
    log_turn_context(context)
    return context


def fixed_turn_context(
    language: str,
    *,
    source: str,
    reference: str | None = None,
    mode: str = "single-language",
    allowed_languages=(),
    source_language: str | None = None,
    target_language: str | None = None,
) -> TurnContext:
    """Restore the preference of one already-running/resumed user turn."""

    normalized = normalize_locale(language) or i18n.DEFAULT_LANG
    canonical = normalize_locale(target_language) or normalized
    allowed = tuple(
        dict.fromkeys(
            code for code in (normalize_locale(item) for item in allowed_languages)
            if code
        )
    )
    if mode not in {"single-language", "translation", "multilingual"}:
        mode = "single-language"
    context = TurnContext(
        canonical,
        source,
        _turn_ref(reference),
        mode=mode,
        allowed_languages=allowed or (canonical,),
        source_language=normalize_locale(source_language),
        target_language=canonical,
    )
    log_turn_context(context)
    return context


def language_instruction(language: str | TurnContext) -> str:
    context = language if isinstance(language, TurnContext) else None
    code = normalize_locale(
        context.canonical_language if context else language
    ) or i18n.DEFAULT_LANG
    if context is not None and context.mode == "translation":
        allowed = ", ".join(
            _LANG_LABELS[item] for item in context.allowed_languages
            if item in _LANG_LABELS
        )
        mode_instruction = (
            f"Translation target for this turn: {_LANG_LABELS[code]}. "
            f"Requested languages: {allowed or _LANG_LABELS[code]}; preserve source "
            "text where the translation requires it."
        )
    elif context is not None and context.mode == "multilingual":
        allowed = ", ".join(
            _LANG_LABELS[item] for item in context.allowed_languages
            if item in _LANG_LABELS
        )
        mode_instruction = f"Requested response languages for this turn: {allowed}."
    elif context is not None and context.source == "natural_model":
        mode_instruction = (
            "No response language preference was resolved for this turn; respond "
            "naturally to the current user message."
        )
    else:
        mode_instruction = (
            f"Preferred response language for this turn: {_LANG_LABELS[code]} ({code})."
        )
    return (
        "[TURN RESPONSE LANGUAGE]\n"
        f"{mode_instruction} This preference applies only to the current turn. "
        "UI labels, code, names, quotations, and tool output keep their source text."
    )


_SOURCE_VALUE_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:source|original|quotation|quote|citation|"
    r"extrait|valeur(?:\s+source)?|texte\s+source|fuente|texto\s+original|"
    r"cita|valor(?:\s+de\s+origen)?)\s*:",
    re.IGNORECASE,
)

def _assistant_prose_for_language_check(text) -> str:
    """Return probable assistant prose without quoted or labelled source data."""

    visible = _without_foreign_evidence(_display_text(text))
    visible = re.sub(
        r"\u201c[^\u201d\n]*\u201d|\u00ab[^\u00bb\n]*\u00bb",
        " ",
        visible,
    )
    kept: list[str] = []
    for line in visible.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith(">")
            or _SOURCE_VALUE_LINE.match(stripped)
            or re.fullmatch(r"[A-Z0-9_:@./+-]+", stripped)
        ):
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            # Markdown tables commonly carry exact external field values.
            continue
        stripped = re.sub(r"^\s*(?:#{1,6}|[-*+]\s+|\d+[.)]\s+)", "", stripped)
        kept.append(stripped)
    return "\n".join(kept)


def _strong_prose_language(text: str) -> str | None:
    words = re.findall(r"[^\W\d_]+", str(text or ""), flags=re.UNICODE)
    lexical = _lexical_language(
        words, hints_by_language=_ASSISTANT_LEXICAL_HINTS,
    )
    if lexical:
        return lexical
    for code, pattern in _SCRIPT_PATTERNS:
        if pattern.search(str(text or "")):
            return code
    if len(words) < 4 or sum(len(word) for word in words) < 12:
        return None
    try:
        candidates = detect_langs(text)
        best = candidates[0] if candidates else None
        language = normalize_locale(getattr(best, "lang", None))
        confidence = float(getattr(best, "prob", 0.0))
    except Exception:
        return None
    return language if language and confidence >= 0.85 else None


def _language_signal_confidence(text: str, detected: str) -> float:
    """Return deterministic confidence for an already accepted strong signal."""

    words = re.findall(r"[^\W\d_]+", str(text or ""), flags=re.UNICODE)
    if _lexical_language(words, hints_by_language=_ASSISTANT_LEXICAL_HINTS) == detected:
        return 1.0
    for code, pattern in _SCRIPT_PATTERNS:
        if code == detected and pattern.search(str(text or "")):
            return 1.0
    try:
        for candidate in detect_langs(text):
            if normalize_locale(getattr(candidate, "lang", None)) == detected:
                return round(float(getattr(candidate, "prob", 0.0)), 6)
    except Exception:
        pass
    return 0.0


def response_language_drift_diagnostic(
    text, context: TurnContext | None,
) -> dict | None:
    """Describe foreign prose found only in the user-facing final candidate."""

    if (
        not isinstance(context, TurnContext)
        or context.mode != "single-language"
        or context.source == "natural_model"
    ):
        return None
    target = normalize_locale(context.canonical_language)
    if not target:
        return None
    visible = _assistant_prose_for_language_check(text)
    if not visible.strip():
        return None
    detected = _strong_prose_language(visible)
    if detected and detected != target:
        return {
            "analysis_scope": "user_facing_final_answer",
            "expected_language": target,
            "detected_final_answer_language": detected,
            "analyzed_text_span": visible.strip(),
            "confidence": _language_signal_confidence(visible, detected),
            "reason": "foreign_assistant_prose_response",
        }
    return None


def persisted_language_drift_diagnostic(value) -> dict | None:
    """Return the bounded, reasoning-free diagnostic safe for turn metadata."""

    if not isinstance(value, dict):
        return None
    expected = normalize_locale(value.get("expected_language"))
    detected = normalize_locale(value.get("detected_final_answer_language"))
    span = value.get("analyzed_text_span")
    reason = str(value.get("reason") or "")
    if (
        value.get("analysis_scope") != "user_facing_final_answer"
        or not expected
        or not detected
        or not isinstance(span, str)
        or not span.strip()
        or len(span) > 16_000
        or reason != "foreign_assistant_prose_response"
    ):
        return None
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return {
        "schema_version": 1,
        "analysis_scope": "user_facing_final_answer",
        "expected_language": expected,
        "detected_final_answer_language": detected,
        "analyzed_text_span": span.strip(),
        "confidence": round(confidence, 6),
        "reason": reason,
    }


def response_language_drift(text, context: TurnContext | None) -> str | None:
    """Return a clearly foreign assistant-prose language, if one is present.

    This is a presentation-repair signal, never a terminal validity decision. It
    is disabled for unresolved, translation and multilingual turns and ignores
    quotations, code, URLs, labelled source values and Markdown tables so exact
    external evidence remains untouched.
    """

    diagnostic = response_language_drift_diagnostic(text, context)
    if diagnostic is None:
        return None
    return str(diagnostic["detected_final_answer_language"])


def response_prose_language(text, context: TurnContext | None) -> str | None:
    """Return a strong language signal for bounded assistant prose.

    This shares the final-boundary evidence filtering used by
    :func:`response_language_drift`, but exposes only a confident positive
    signal.  Streaming callers can therefore hold a small prefix until its
    canonical language is known without buffering the complete response.
    """

    if not isinstance(context, TurnContext):
        return None
    visible = _assistant_prose_for_language_check(text)
    if not visible.strip():
        return None
    return _strong_prose_language(visible)


class ValidatedLiveAssistantChunk(str):
    """Assistant prose approved for replaceable live projection."""


_LOCALIZED = {
    "empty_completion": {
        "en": "I couldn't produce a reliable final response. Please try again or rephrase your request.",
        "fr": "Je n’ai pas pu produire de réponse finale fiable. Réessayez ou reformulez votre demande.",
        "zh": "我无法生成可靠的最终答复。请重试或重新表述您的请求。",
        "ja": "信頼できる最終回答を生成できませんでした。再試行するか、依頼を言い換えてください。",
        "de": "Ich konnte keine verlässliche abschließende Antwort erstellen. Bitte versuchen Sie es erneut oder formulieren Sie die Anfrage um.",
        "es": "No pude producir una respuesta final fiable. Inténtalo de nuevo o reformula la solicitud.",
    },
    "provider_failure": {
        "en": "I couldn't complete this request because the response provider failed. Please try again.",
        "fr": "Je n’ai pas pu terminer cette demande, car le fournisseur de réponse a échoué. Réessayez.",
        "zh": "由于响应服务失败，我无法完成此请求。请重试。",
        "ja": "応答プロバイダーでエラーが発生したため、この依頼を完了できませんでした。再試行してください。",
        "de": "Ich konnte diese Anfrage wegen eines Fehlers des Antwortanbieters nicht abschließen. Bitte versuchen Sie es erneut.",
        "es": "No pude completar esta solicitud porque falló el proveedor de respuesta. Inténtalo de nuevo.",
    },
    "capture_protected_surface": {
        "en": "I can't read this window's content because the application asks the operating system to exclude it from screen capture. I did not bypass that protection or infer the content from accessibility-only elements.",
        "fr": "Je ne peux pas lire le contenu de cette fenêtre, car l’application demande au système d’exploitation de l’exclure des captures d’écran. Je n’ai ni contourné cette protection ni déduit le contenu à partir des seuls éléments d’accessibilité.",
        "zh": "我无法读取此窗口的内容，因为该应用要求操作系统将它排除在屏幕捕获之外。我没有绕过这项保护，也没有仅根据辅助功能元素推断其内容。",
        "ja": "このアプリケーションが画面キャプチャから除外するようオペレーティングシステムに要求しているため、このウィンドウの内容を読み取れません。この保護を回避したり、アクセシビリティ要素だけから内容を推測したりはしていません。",
        "de": "Ich kann den Inhalt dieses Fensters nicht lesen, weil die Anwendung das Betriebssystem anweist, es von Bildschirmaufnahmen auszuschließen. Ich habe diesen Schutz weder umgangen noch den Inhalt allein aus Bedienungshilfen-Elementen abgeleitet.",
        "es": "No puedo leer el contenido de esta ventana porque la aplicación pide al sistema operativo que la excluya de las capturas de pantalla. No eludí esa protección ni deduje el contenido únicamente a partir de elementos de accesibilidad.",
    },
    "anti_loop_stop": {
        "en": "I stopped this turn because I could not confirm further progress. Please try again or rephrase your request.",
        "fr": "J’ai arrêté ce tour, car je ne pouvais plus confirmer de progrès. Réessayez ou reformulez votre demande.",
        "zh": "由于无法确认后续进展，我已停止本轮操作。请重试或重新表述您的请求。",
        "ja": "これ以上の進行を確認できなかったため、このターンを停止しました。再試行するか、依頼を言い換えてください。",
        "de": "Ich habe diesen Durchlauf beendet, weil kein weiterer Fortschritt bestätigt werden konnte. Bitte versuchen Sie es erneut oder formulieren Sie die Anfrage um.",
        "es": "Detuve este turno porque no pude confirmar más avances. Inténtalo de nuevo o reformula la solicitud.",
    },
    "not_scheduled": {
        "en": "I could not schedule that task.",
        "fr": "Je n’ai pas pu planifier cette tâche.",
        "zh": "我无法安排该任务。",
        "ja": "そのタスクをスケジュールできませんでした。",
        "de": "Ich konnte diese Aufgabe nicht planen.",
        "es": "No pude programar esa tarea.",
    },
    "memory_not_saved": {
        "en": "I did not add anything to local memory.",
        "fr": "Je n’ai rien ajouté à la mémoire locale.",
        "zh": "我没有向本地记忆添加任何内容。",
        "ja": "ローカルメモリには何も追加していません。",
        "de": "Ich habe dem lokalen Gedächtnis nichts hinzugefügt.",
        "es": "No añadí nada a la memoria local.",
    },
    "memory_saved": {
        "en": "It's memorized.", "fr": "C’est mémorisé.", "zh": "已记住。",
        "ja": "記憶しました。", "de": "Es ist gespeichert.", "es": "Está memorizado.",
    },
    "memory_rejected": {
        "en": "I can't memorize that information.",
        "fr": "Je ne peux pas mémoriser cette information.",
        "zh": "我无法记住该信息。", "ja": "その情報は記憶できません。",
        "de": "Ich kann diese Information nicht speichern.",
        "es": "No puedo memorizar esa información.",
    },
    "memory_failure": {
        "en": "Memory save failed.", "fr": "La mémorisation a échoué.",
        "zh": "记忆保存失败。", "ja": "メモリへの保存に失敗しました。",
        "de": "Das Speichern im Gedächtnis ist fehlgeschlagen.",
        "es": "No se pudo guardar en la memoria.",
    },
    "outcome_unknown": {
        "en": "The outcome of {action} is uncertain: it was dispatched and may still complete. I did not retry it or replay it on another provider. Check its effect before trying again.",
        "fr": "Le résultat de {action} est incertain : l’action a été lancée et peut encore se terminer. Je ne l’ai ni relancée ni rejouée sur un autre fournisseur. Vérifiez son effet avant toute nouvelle tentative.",
        "zh": "我无法确认{action}是否已完成。该操作已发出，仍可能完成。我没有重试，也没有通过其他提供商重放。再次尝试前，请先检查其效果。",
        "ja": "{action}が完了したことを確認できませんでした。この操作は送信済みで、まだ完了する可能性があります。再試行も別のプロバイダーでの再実行もしていません。もう一度試す前に結果を確認してください。",
        "de": "Ich konnte nicht bestätigen, dass {action} abgeschlossen wurde. Die Aktion wurde ausgelöst und kann noch abgeschlossen werden. Ich habe sie weder wiederholt noch über einen anderen Anbieter erneut ausgeführt. Prüfen Sie die Wirkung, bevor Sie es erneut versuchen.",
        "es": "No pude confirmar que {action} terminara. La acción se envió y aún podría completarse. No la reintenté ni la repetí con otro proveedor. Comprueba su efecto antes de volver a intentarlo.",
    },
    "effect_provider_failure": {
        "en": "{action} was already dispatched. The response provider then failed, and I did not replay the turn to avoid duplicating the action.",
        "fr": "{action} avait déjà été lancée. Le fournisseur de réponse a ensuite échoué ; je n’ai pas rejoué le tour afin d’éviter de dupliquer l’action.",
        "zh": "{action}已发出。随后响应提供商失败；为避免重复执行该操作，我没有重放本轮请求。",
        "ja": "{action}はすでに送信されていました。その後、応答プロバイダーでエラーが発生しました。操作の重複を避けるため、このターンは再実行していません。",
        "de": "{action} war bereits ausgelöst worden. Danach ist der Antwortanbieter fehlgeschlagen; ich habe den Durchlauf nicht wiederholt, um die Aktion nicht zu duplizieren.",
        "es": "{action} ya se había enviado. Después falló el proveedor de respuesta; no repetí el turno para evitar duplicar la acción.",
    },
    "stt_not_understood": {
        "en": "I didn't understand. Please repeat.",
        "fr": "Je n’ai pas compris. Répétez, s’il vous plaît.",
        "zh": "我没有听懂。请再说一遍。",
        "ja": "聞き取れませんでした。もう一度話してください。",
        "de": "Ich habe Sie nicht verstanden. Bitte wiederholen Sie es.",
        "es": "No he entendido. Repítelo, por favor.",
    },
    "scheduled_authorization_suspended": {
        "en": "ERROR: Scheduled-task authorization was suspended before dispatch ({reason}). No action was executed.",
        "fr": "ERROR: l’autorisation de la tâche planifiée a été suspendue avant l’exécution ({reason}). Aucune action n’a été effectuée.",
        "zh": "ERROR: 计划任务授权在执行前已暂停（{reason}）。未执行任何操作。",
        "ja": "ERROR: スケジュール済みタスクの承認は実行前に停止されました（{reason}）。操作は実行されていません。",
        "de": "ERROR: Die Autorisierung der geplanten Aufgabe wurde vor der Ausführung ausgesetzt ({reason}). Es wurde keine Aktion ausgeführt.",
        "es": "ERROR: La autorización de la tarea programada se suspendió antes de la ejecución ({reason}). No se realizó ninguna acción.",
    },
    "outcome_uncertain_banner": {
        "en": "⚠️ OUTCOME UNCERTAIN — do not assume success or failure and do not retry automatically: {payload}",
        "fr": "⚠️ RÉSULTAT INCERTAIN — ne suppose ni succès ni échec et ne relance pas automatiquement : {payload}",
        "zh": "⚠️ 结果不确定 — 不要假定成功或失败，也不要自动重试：{payload}",
        "ja": "⚠️ 結果不明 — 成功または失敗と決めつけず、自動で再試行しないでください：{payload}",
        "de": "⚠️ ERGEBNIS UNGEWISS — weder Erfolg noch Fehlschlag annehmen und nicht automatisch wiederholen: {payload}",
        "es": "⚠️ RESULTADO INCIERTO — no supongas éxito ni fracaso y no reintentes automáticamente: {payload}",
    },
    "tool_failed_banner": {
        "en": "⚠️ TOOL FAILED — the action did NOT happen: {payload}",
        "fr": "⚠️ ÉCHEC DE L’OUTIL — l’action n’a PAS eu lieu : {payload}",
        "zh": "⚠️ 工具失败 — 操作未执行：{payload}",
        "ja": "⚠️ ツール失敗 — 操作は実行されませんでした：{payload}",
        "de": "⚠️ WERKZEUG FEHLGESCHLAGEN — die Aktion wurde NICHT ausgeführt: {payload}",
        "es": "⚠️ LA HERRAMIENTA FALLÓ — la acción NO se realizó: {payload}",
    },
    "tool_result_header": {
        "en": "Tool {tool} result:",
        "fr": "Résultat de l’outil {tool} :",
        "zh": "工具 {tool} 的结果：",
        "ja": "ツール {tool} の結果：",
        "de": "Ergebnis des Werkzeugs {tool}:",
        "es": "Resultado de la herramienta {tool}:",
    },
    "unknown_tool_error": {
        "en": "ERROR: unknown tool: {tool}",
        "fr": "ERROR: outil inconnu : {tool}",
        "zh": "ERROR: 未知工具：{tool}",
        "ja": "ERROR: 不明なツール：{tool}",
        "de": "ERROR: unbekanntes Werkzeug: {tool}",
        "es": "ERROR: herramienta desconocida: {tool}",
    },
    "tools_complete_instruction": {
        "en": "The current tool batch finished. Reassess the full objective against the evidence: if any explicit step, deliverable, constraint, or required end state remains incomplete or unverified, call the next tool. Answer in plain text with no <tool_call> tags only when the objective is complete and verified, or when you are genuinely blocked, the user cancelled, or a safety barrier stopped you.",
        "fr": "Le lot d’outils courant est terminé. Réévalue l’objectif complet à la lumière des preuves : si une étape explicite, un livrable, une contrainte ou un état final requis reste incomplet ou non vérifié, appelle l’outil suivant. Ne réponds en texte simple, sans balise <tool_call>, que lorsque l’objectif est terminé et vérifié, ou lorsque tu es réellement bloqué, que l’utilisateur a annulé, ou qu’une barrière de sécurité t’a arrêté.",
        "zh": "当前工具批次已完成。请根据证据重新评估完整目标：如果仍有任何明确步骤、交付物、约束或所需最终状态未完成或未验证，请调用下一个工具。仅当目标已完成并经过验证，或你确实受阻、用户已取消、或安全屏障阻止了你时，才用纯文本回答，不要包含 <tool_call> 标签。",
        "ja": "現在のツールバッチが完了しました。証拠に照らして目標全体を再評価してください。明示された手順、成果物、制約、必要な最終状態のいずれかが未完了または未検証であれば、次のツールを呼び出してください。目標が完了し検証済みの場合、または真にブロックされた場合、ユーザーがキャンセルした場合、安全バリアに止められた場合にのみ、<tool_call> タグを使わずプレーンテキストで回答してください。",
        "de": "Der aktuelle Werkzeug-Batch ist abgeschlossen. Bewerte das gesamte Ziel anhand der Belege neu: Wenn ein ausdrücklicher Schritt, ein Ergebnis, eine Einschränkung oder ein erforderlicher Endzustand noch unvollständig oder unbestätigt ist, rufe das nächste Werkzeug auf. Antworte erst dann in Klartext ohne <tool_call>-Tags, wenn das Ziel abgeschlossen und bestätigt ist, oder wenn du wirklich blockiert bist, der Nutzer abgebrochen hat oder eine Sicherheitsbarriere dich gestoppt hat.",
        "es": "El lote actual de herramientas ha terminado. Reevalúa el objetivo completo a la luz de la evidencia: si algún paso explícito, entregable, restricción o estado final requerido sigue incompleto o sin verificar, llama a la siguiente herramienta. Responde en texto sin etiquetas <tool_call> solo cuando el objetivo esté completo y verificado, o cuando estés realmente bloqueado, el usuario haya cancelado o una barrera de seguridad te haya detenido.",
    },
    "schedule_ignored_instruction": {
        "en": "A scheduled-task tool call was ignored because this turn contains no explicit scheduling request. Do not mention scheduling or a scheduling failure. Answer the immediate request from the valid tool results.",
        "fr": "Un appel d’outil de tâche planifiée a été ignoré, car ce tour ne contient aucune demande explicite de planification. Ne mentionne ni planification ni échec de planification. Réponds à la demande immédiate à partir des résultats d’outils valides.",
        "zh": "由于本轮没有明确的计划任务请求，一次计划任务工具调用已被忽略。不要提及计划任务或计划失败。请根据有效的工具结果回答当前请求。",
        "ja": "このターンに明示的なスケジュール要求がないため、スケジュール済みタスクのツール呼び出しは無視されました。スケジュールやその失敗には触れず、有効なツール結果から現在の依頼に回答してください。",
        "de": "Ein Aufruf des Werkzeugs für geplante Aufgaben wurde ignoriert, weil dieser Durchlauf keine ausdrückliche Planungsanfrage enthält. Erwähne weder Planung noch einen Planungsfehler. Beantworte die aktuelle Anfrage anhand der gültigen Werkzeugergebnisse.",
        "es": "Se ignoró una llamada a la herramienta de tareas programadas porque este turno no contiene una solicitud explícita de programación. No menciones la programación ni un fallo de programación. Responde a la solicitud inmediata con los resultados válidos de las herramientas.",
    },
}


def localized_text(key: str, language: str, **values) -> str:
    code = normalize_locale(language) or i18n.DEFAULT_LANG
    translations = _LOCALIZED.get(key) or i18n.TRANSLATIONS.get(key, {})
    rendered = (
        translations.get(code)
        or translations.get(i18n.DEFAULT_LANG)
        or key
    )
    try:
        return rendered.format(**values)
    except (KeyError, ValueError):
        return rendered


_LANGUAGE_CONSISTENCY_RETRY = {
    "en": (
        "The previous candidate changed the language of OSIA-authored prose. "
        "Rewrite the same final answer in the preferred language for this turn. Preserve "
        "proper nouns, exact source values and direct quotations unchanged. Do not "
        "call another tool or mention this correction."
    ),
    "fr": (
        "La r\u00e9ponse pr\u00e9c\u00e9dente a chang\u00e9 la langue du texte r\u00e9dig\u00e9 "
        "par OSIA. R\u00e9cris la m\u00eame r\u00e9ponse finale dans la langue pr\u00e9f\u00e9r\u00e9e "
        "pour ce tour. Conserve les noms propres, les valeurs sources exactes et les citations "
        "directes sans les modifier. N'appelle aucun autre outil et ne mentionne pas "
        "cette correction."
    ),
    "es": (
        "La respuesta anterior cambi\u00f3 el idioma del texto redactado por OSIA. "
        "Reescribe la misma respuesta final en el idioma preferido para este turno. Conserva "
        "sin cambios los nombres propios, los valores exactos de la fuente y las "
        "citas directas. No llames a otra herramienta ni menciones esta correcci\u00f3n."
    ),
}

def language_consistency_retry_text(language: str) -> str:
    code = normalize_locale(language) or i18n.DEFAULT_LANG
    return (
        _LANGUAGE_CONSISTENCY_RETRY.get(code)
        or _LANGUAGE_CONSISTENCY_RETRY["en"]
    )
def content_hash(text) -> str:
    return hashlib.sha256(_display_text(text).encode("utf-8", errors="replace")).hexdigest()[:16]


def log_turn_context(context: TurnContext) -> None:
    _LOG.info("turn_language language=%s source=%s turn_ref=%s",
              context.canonical_language, context.source, context.turn_ref)


def log_round(context: TurnContext, round_number: int, state: str) -> None:
    _LOG.info("turn_round turn_ref=%s round=%d state=%s",
              context.turn_ref, round_number, state)


def log_final(context: TurnContext, text) -> None:
    rendered = _display_text(text)
    _LOG.info("turn_final turn_ref=%s language=%s length=%d hash=%s",
              context.turn_ref, context.canonical_language,
              len(rendered), content_hash(rendered))
