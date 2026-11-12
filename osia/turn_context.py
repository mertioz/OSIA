#!/usr/bin/env python3
"""UI localization and privacy-safe turn observability; response language is model-owned."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import re
import secrets


from osia import i18n


_LOG = logging.getLogger("osia.turn")
_UI_LOCALES = frozenset({
    "ar", "de", "el", "en", "es", "fr", "he", "hi", "it", "ja",
    "ko", "nl", "pl", "pt", "ru", "th", "tr", "zh",
})


@dataclass(frozen=True)
class TurnContext:
    """Turn correlation plus locale for application-owned notices, never AI prose.

    Legacy fields remain readable by callers restoring older turn metadata.
    They do not select, validate or constrain the language of model output.
    """

    language: str
    source: str
    turn_ref: str
    mode: str = "model"
    allowed_languages: tuple[str, ...] = ()
    source_language: str | None = None
    target_language: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "language", normalize_locale(self.language) or i18n.DEFAULT_LANG)
        object.__setattr__(self, "mode", "model")
        object.__setattr__(self, "allowed_languages", ())
        object.__setattr__(self, "source_language", None)
        object.__setattr__(self, "target_language", None)

    @property
    def canonical_language(self) -> str:
        """Compatibility alias for the UI locale, not an output-language decision."""
        return self.language

    @property
    def speech_language(self) -> None:
        """Let the speech backend select a voice from the actual emitted text."""
        return None

    def message_metadata(self) -> dict:
        """Record model ownership without pretending to know its response language."""
        return {"_language_mode": "model"}


def normalize_locale(value) -> str | None:
    """Normalize a locale to OSIA's UI/localization locale registry."""

    if not isinstance(value, str):
        return None
    candidate = value.strip().lower().replace("_", "-")
    if not candidate or len(candidate) > 35:
        return None
    candidate = candidate.split(".", 1)[0].split("@", 1)[0]
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", candidate):
        return None
    code = candidate.split("-", 1)[0]
    return code if code in _UI_LOCALES else None


def _display_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            str(part.get("text") or "")
            for part in value if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(value or "")


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
    """Create turn identity and UI locale without inspecting user text or history.

    The unused input/history arguments preserve the existing PC, voice and
    server call signatures. The model receives the user's original messages.
    """
    locale = (
        normalize_locale(client_locale)
        or normalize_locale(saved_language)
        or normalize_locale(i18n.get_language())
        or i18n.DEFAULT_LANG
    )
    context = TurnContext(locale, "ui_locale", _turn_ref(reference))
    log_turn_context(context)
    return context


def fixed_turn_context(
    language: str,
    *,
    source: str,
    reference: str | None = None,
    mode: str = "model",
    allowed_languages=(),
    source_language: str | None = None,
    target_language: str | None = None,
) -> TurnContext:
    """Restore a turn's UI locale while retiring legacy output-language controls."""
    context = TurnContext(normalize_locale(language) or i18n.DEFAULT_LANG, source, _turn_ref(reference))
    log_turn_context(context)
    return context


def persisted_language_drift_diagnostic(value) -> dict | None:
    """Read a legacy diagnostic without analyzing new prose or steering models."""

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
    "desktop_turn_setup_failed": {
        "en": "OSIA couldn't prepare this turn. Please try again.",
        "fr": "OSIA n’a pas pu préparer ce tour. Réessaie.",
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
        "en": "Use these factual results and fresh existing evidence. Choose another tool if required work or evidence remains missing; otherwise give the truthful final answer. Report uncertainty or a real limitation without claiming unobserved success.",
        "fr": "Utilise ces résultats factuels et les preuves existantes encore actuelles. Choisis un autre outil si du travail ou des preuves nécessaires manquent ; sinon, donne la réponse finale fidèle aux faits. Signale l’incertitude ou une limite réelle sans prétendre à un succès non observé.",
        "zh": "使用这些事实结果和仍然有效的已有证据。如果还缺少必要的工作或证据，选择另一个工具；否则给出符合事实的最终回答。如实报告不确定性或实际限制，不要声称未观察到的成功。",
        "ja": "これらの事実に基づく結果と、現在も有効な既存の証拠を使ってください。必要な作業や証拠が不足していれば次のツールを選び、そうでなければ事実に即した最終回答をしてください。不確実性や実際の制約を伝え、観測していない成功を主張しないでください。",
        "de": "Nutze diese faktischen Ergebnisse und weiterhin aktuelle Belege. Wähle ein weiteres Werkzeug, wenn nötige Arbeit oder Belege fehlen; andernfalls gib die wahrheitsgemäße abschließende Antwort. Benenne Unsicherheit oder eine echte Einschränkung, ohne unbeobachteten Erfolg zu behaupten.",
        "es": "Usa estos resultados fácticos y las pruebas existentes que sigan vigentes. Elige otra herramienta si falta trabajo o evidencia necesaria; de lo contrario, da la respuesta final fiel a los hechos. Indica la incertidumbre o una limitación real sin afirmar un éxito no observado.",
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


def content_hash(text) -> str:
    return hashlib.sha256(_display_text(text).encode("utf-8", errors="replace")).hexdigest()[:16]


def log_turn_context(context: TurnContext) -> None:
    _LOG.info("turn_ui_locale locale=%s source=%s turn_ref=%s",
              context.canonical_language, context.source, context.turn_ref)


def log_round(context: TurnContext, round_number: int, state: str) -> None:
    _LOG.info("turn_round turn_ref=%s round=%d state=%s",
              context.turn_ref, round_number, state)


def log_final(context: TurnContext, text) -> None:
    rendered = _display_text(text)
    _LOG.info("turn_final turn_ref=%s ui_locale=%s length=%d hash=%s",
              context.turn_ref, context.canonical_language,
              len(rendered), content_hash(rendered))
