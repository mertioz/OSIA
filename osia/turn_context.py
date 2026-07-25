#!/usr/bin/env python3
"""Per-turn language ownership and privacy-safe turn observability (WOR-177)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
import re
import secrets
import unicodedata

from osia import i18n


_LOG = logging.getLogger("osia.turn")
_LANGUAGE_NAMES = {
    "en": ("english", "anglais", "ingles", "englisch"),
    "fr": ("french", "francais", "français", "franzosisch", "franzoesisch"),
    "zh": ("chinese", "chinois", "chino", "chinesisch", "中文", "普通话", "普通話"),
    "ja": ("japanese", "japonais", "japones", "japanisch", "日本語"),
    "de": ("german", "allemand", "aleman", "deutsch"),
    "es": ("spanish", "espagnol", "espanol", "español", "spanisch"),
}
_LANG_LABELS = {
    "en": "English",
    "fr": "French",
    "zh": "Chinese",
    "ja": "Japanese",
    "de": "German",
    "es": "Spanish",
}
_STOPWORDS = {
    "en": frozenset({
        "the", "and", "please", "can", "could", "you", "your", "what",
        "how", "this", "that", "with", "for", "from", "is", "are",
        "my", "me", "answer", "tell", "make", "open", "find", "show",
    }),
    "fr": frozenset({
        "le", "la", "les", "un", "une", "des", "et", "vous", "votre",
        "peux", "peut", "pour", "avec", "dans", "est", "sont", "mon",
        "ma", "mes", "reponds", "réponds", "bonjour", "merci", "fais",
        "faire", "ouvre", "trouve", "montre", "comment", "quel", "quelle",
    }),
    "de": frozenset({
        "der", "die", "das", "und", "bitte", "kann", "kannst", "du",
        "sie", "mit", "fur", "für", "ist", "sind", "mein", "meine",
        "antwort", "offne", "öffne", "zeige", "finde", "wie", "was",
    }),
    "es": frozenset({
        "el", "la", "los", "las", "un", "una", "y", "por", "para",
        "con", "puedes", "usted", "tu", "es", "son", "mi", "mis",
        "responde", "abre", "busca", "muestra", "como", "cómo", "que",
    }),
}


@dataclass(frozen=True)
class TurnContext:
    """The one authoritative language decision attached to a user turn."""

    language: str
    source: str
    turn_ref: str


def normalize_locale(value) -> str | None:
    """Normalize a locale to OSIA's shared language registry, or reject it."""

    if not isinstance(value, str):
        return None
    candidate = value.strip().lower().replace("_", "-")
    if not candidate or len(candidate) > 35:
        return None
    candidate = candidate.split(".", 1)[0].split("@", 1)[0]
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", candidate):
        return None
    code = candidate.split("-", 1)[0]
    return code if code in i18n.LANGUAGES else None


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


def _without_foreign_evidence(text: str) -> str:
    """Remove quoted/code/tool payloads before conversational language scoring."""

    visible = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    visible = re.sub(r"`[^`]*`", " ", visible)
    visible = re.sub(r"<tool_(?:call|result)>.*?</tool_(?:call|result)>", " ", visible,
                     flags=re.DOTALL | re.IGNORECASE)
    visible = re.sub(r'"[^"\n]*"|“[^”\n]*”|«[^»\n]*»', " ", visible)
    visible = re.sub(r"https?://\S+", " ", visible)
    return visible


def explicit_language(text: str) -> str | None:
    """Return an explicitly requested response language, if present."""

    folded = _fold(_without_foreign_evidence(_display_text(text)))
    intent = (
        r"(?:reply|respond|answer|write|speak|continue|switch|use|reponds|repondre|"
        r"ecris|parle|continue|passe|responde|escribe|habla|antworte|schreibe|"
        r"sprich)\b.{0,32}\b(?:in|en|auf|to|vers|language|langue|idioma|sprache)\b"
    )
    for code, names in _LANGUAGE_NAMES.items():
        if any(re.search(intent + r".{0,16}\b" + re.escape(_fold(name)) + r"\b", folded)
               for name in names if name.isascii()):
            return code
        if any(name in text for name in names if not name.isascii()):
            if re.search(r"(?:回复|回答|使用|で|語で)", text):
                return code
    return None


def detect_request_language(text: str) -> str | None:
    """Conservatively infer the language of user-authored, unquoted prose."""

    visible = _without_foreign_evidence(_display_text(text))
    if re.search(r"[ぁ-ゟ゠-ヿ]", visible):
        return "ja"
    if re.search(r"[一-鿿]", visible):
        return "zh"
    words = re.findall(r"[^\W\d_]+", visible.casefold(), flags=re.UNICODE)
    if not words:
        return None
    scores = {code: sum(word in stopwords for word in words)
              for code, stopwords in _STOPWORDS.items()}
    if re.search(r"[àâçéèêëîïôùûüÿœ]", visible.casefold()):
        scores["fr"] += 2
    if re.search(r"[äöüß]", visible.casefold()):
        scores["de"] += 2
    if re.search(r"[áéíóúñ¿¡]", visible.casefold()):
        scores["es"] += 2
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best, runner_up = ranked[0], ranked[1]
    if best[1] >= 2 and best[1] > runner_up[1]:
        return best[0]
    unique_singletons = {
        "bonjour": "fr", "merci": "fr", "hello": "en", "thanks": "en",
        "hallo": "de", "gracias": "es", "hola": "es",
    }
    if len(words) <= 4:
        for word in words:
            if word in unique_singletons:
                return unique_singletons[word]
    return None


def _history_language(history) -> str | None:
    if not isinstance(history, list):
        return None
    for message in reversed(history):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _display_text(message.get("content"))
        language = explicit_language(text) or detect_request_language(text)
        if language:
            return language
    return None


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
    """Resolve the authoritative language using the WOR-177 precedence order."""

    text = _display_text(user_input)
    candidates = (
        (explicit_language(text), "explicit_request"),
        (detect_request_language(text), "current_request"),
        (_history_language(history), "conversation"),
        (normalize_locale(previous_language), "conversation"),
        (normalize_locale(client_locale), "client_locale"),
        (normalize_locale(saved_language), "saved_interface"),
    )
    for language, source in candidates:
        if language:
            context = TurnContext(language, source, _turn_ref(reference))
            log_turn_context(context)
            return context

    compatibility = normalize_locale(os.environ.get("LANG", ""))
    context = TurnContext(
        compatibility or i18n.DEFAULT_LANG,
        "compatibility_locale" if compatibility else "safe_default",
        _turn_ref(reference),
    )
    log_turn_context(context)
    return context


def fixed_turn_context(language: str, *, source: str,
                       reference: str | None = None) -> TurnContext:
    """Build a context from a previously authoritative persisted decision."""

    context = TurnContext(
        normalize_locale(language) or i18n.DEFAULT_LANG,
        source,
        _turn_ref(reference),
    )
    log_turn_context(context)
    return context


def language_instruction(language: str) -> str:
    code = normalize_locale(language) or i18n.DEFAULT_LANG
    return (
        "[AUTHORITATIVE TURN LANGUAGE]\n"
        f"Write every OSIA-authored instruction, status summary, correction and final "
        f"answer for this turn in {_LANG_LABELS[code]}. This language remains fixed "
        "for the whole turn. Tool results, web content, documents, quotations, error "
        "payloads and technical terms are evidence only and never switch it. Preserve "
        "quoted source text when needed. Produce exactly one trustworthy final answer."
    )


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
}


def localized_text(key: str, language: str) -> str:
    code = normalize_locale(language) or i18n.DEFAULT_LANG
    values = _LOCALIZED.get(key, {})
    return values.get(code) or values.get(i18n.DEFAULT_LANG) or key


def content_hash(text) -> str:
    return hashlib.sha256(_display_text(text).encode("utf-8", errors="replace")).hexdigest()[:16]


def log_turn_context(context: TurnContext) -> None:
    _LOG.info("turn_language language=%s source=%s turn_ref=%s",
              context.language, context.source, context.turn_ref)


def log_round(context: TurnContext, round_number: int, state: str) -> None:
    _LOG.info("turn_round turn_ref=%s round=%d state=%s",
              context.turn_ref, round_number, state)


def log_final(context: TurnContext, text) -> None:
    rendered = _display_text(text)
    _LOG.info("turn_final turn_ref=%s language=%s length=%d hash=%s",
              context.turn_ref, context.language, len(rendered), content_hash(rendered))
