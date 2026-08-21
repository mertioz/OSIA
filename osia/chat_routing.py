"""Bounded Chat-to-Autonomous routing contract for WOR-223.

The chat model makes the semantic decision inside its existing provider turn.
This module validates only the small structured decision, preserves the exact
user-authored objective, and delegates the actual start to the canonical host
launcher supplied by the PC or Mobile server entry point.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


ROUTING_TOOL_NAME = "start_autonomous_mission"
AUTONOMOUS_REASON_CODES = (
    "dependent_phases",
    "extended_research",
    "iterative_artifact",
    "sustained_computer_work",
    "bounded_turn_infeasible",
)
_SOURCE_REF_RE = re.compile(r"\b(?:src|doc|web)_[0-9a-f]{32}\b")
_EXPLICIT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:use|start|launch|run)\s+(?:the\s+)?autonomous\s+mode\b",
        r"\b(?:in|with)\s+autonomous\s+mode\b",
        r"\bautonomously\b",
        r"\ben\s+mode\s+autonome\b",
        r"\b(?:de\s+maniere|de\s+fa[cç]on)\s+autonome\b",
        r"\b(?:lance|lancer|utilise|utiliser|demarre|d[eé]marre)\b.{0,24}\bautonome\b",
        r"\ben\s+modo\s+aut[oó]nomo\b",
        r"\bde\s+forma\s+aut[oó]noma\b",
        r"\b(?:im|mit\s+dem)\s+autonomen\s+modus\b",
        r"(?:使用|启用|用)(?:自主模式|自律模式)|(?:自主地|自律地)",
        r"(?:自律モードで|自律的に)",
    )
)
_NEGATED_EXPLICIT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:do\s+not|don['’]?t|never)\s+(?:use|start|launch|run)\b.{0,24}\bautonomous\b",
        r"\bwithout\s+(?:using\s+)?(?:the\s+)?autonomous\s+mode\b",
        r"\b(?:ne|n['’])\s*(?:lance|utilise|d[eé]marre)\b.{0,24}\bpas\b.{0,24}\bautonome\b",
        r"\bno\s+(?:uses?|utilices?|lances?|ejecutes?)\b.{0,24}\baut[oó]nom[oa]\b",
        r"\b(?:nicht|nie)\b.{0,24}\bautonomen\s+modus\b",
        r"(?:不要|不使用|请勿).{0,8}(?:自主模式|自律模式)",
        r"(?:自律モード).{0,8}(?:使わない|使用しない|用いない)",
    )
)
_COMMAND_ONLY_RE = re.compile(
    r"^(?:please\s+)?(?:do|run|start|launch|continue)\s+(?:it|that|this)\b|"
    r"^(?:fais|fait|lance|continue)\s+(?:le|la|ça|ceci|cela)\b|"
    r"^(?:haz|ejecuta|contin[uú]a)\s+(?:lo|eso|esto)\b|"
    r"^(?:mach|f[uü]hre|starte)\s+(?:das|es|dies)\b",
    re.IGNORECASE,
)
_LONG_MARKERS = re.compile(
    r"\b(?:audit|analyse|analyze|diagnos|implement|build|migrate|research|"
    r"compare|investigate|iterate|test|verify|review|deploy|document|report|"
    r"end[- ]to[- ]end|multi[- ]step|plusieurs\s+[eé]tapes|recherche|"
    r"impl[eé]ment|diagnosti|pruebas?|investigaci[oó]n|umfassend|"
    r"recherchier)\w*\b",
    re.IGNORECASE,
)
_SCHEDULE_ROUTE_RE = re.compile(
    r"\b(?:(?:every|each)\s+(?:\d+\s+)?(?:minutes?|hours?|days?|weekdays?|"
    r"weekends?|weeks?|months?|mornings?|evenings?)|daily|weekly|monthly|"
    r"tomorrow|tonight|at\s+\d{1,2}:\d{2}|"
    r"in\s+\d+\s+(?:minutes?|hours?|days?)|"
    r"chaque\s+(?:minute|heure|jour|semaine|mois)|tous\s+les\s+(?:jours|"
    r"matins|soirs)|toutes\s+les\s+(?:minutes|heures|semaines)|demain|"
    r"ce\s+soir|"
    r"quotidien|hebdomadaire|dans\s+\d+\s+(?:minutes?|heures?|jours?)|"
    r"cada|diari|semanal|mañana|ma[ñn]ana|"
    r"jeden|jede|t[aä]glich|w[oö]chentlich|morgen)\b|"
    r"(?:每天|每周|明天|稍后|毎日|毎週|明日|後で)",
    re.IGNORECASE,
)


def routing_prompt(language: str = "en") -> str:
    """Return the single canonical provider-only routing policy."""

    del language
    return (
        "## Chat / Autonomous routing\n"
        "Before any normal tool call, choose exactly one route for the current "
        "request: normal chat, Autonomous, Scheduled Tasks, or clarification. "
        "Do not add a classification request. Answer simple questions directly "
        "only when no tool or computer action is needed. Every future or recurring "
        "request uses only the Scheduled Tasks tools. For every request that needs "
        "a tool, computer action, or multiple dependent steps, call "
        "start_autonomous_mission BEFORE every other tool. Use reason_code "
        "sustained_computer_work for tool or computer actions, whether bounded "
        "or long; use the other reason codes for dependent phases, extended "
        "research, iterative artifact work, or work that cannot reasonably fit "
        "one turn. Supply "
        "only reason_code, confidence, and explicit_request; never "
        "reveal private reasoning. Never combine this handoff with another tool "
        "in the same batch. If the essential objective is missing, ask one "
        "clarifying question."
    )


def explicit_autonomous_request(text: Any) -> bool:
    value = str(text or "").strip()
    return bool(
        value
        and not any(pattern.search(value) for pattern in _NEGATED_EXPLICIT_PATTERNS)
        and any(pattern.search(value) for pattern in _EXPLICIT_PATTERNS)
    )


def extract_source_refs(value: Any) -> tuple[str, ...]:
    """Extract only canonical opaque document references, in original order."""

    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        text = "\n".join(
            str(part.get("text") or part.get("content") or "")
            for part in value
            if isinstance(part, dict)
        )
    else:
        text = str(value or "")
    return tuple(dict.fromkeys(_SOURCE_REF_RE.findall(text)))[:10]


def _previous_user_text(history: Any) -> str:
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        return ""
    for message in reversed(history):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def exact_objective(current_text: Any, history: Any = ()) -> str:
    """Select one exact user-authored objective without model paraphrasing."""

    current = str(current_text or "").strip()
    if not current:
        return ""
    stripped = current
    for pattern in _EXPLICIT_PATTERNS:
        stripped = pattern.sub(" ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip(" .,:;!?-\t\r\n")
    if not stripped or _COMMAND_ONLY_RE.search(stripped):
        return _previous_user_text(history)
    return current


def _looks_genuinely_long(objective: str, reason_code: str) -> bool:
    words = re.findall(r"[^\W_]+", objective, flags=re.UNICODE)
    marker_count = len(_LONG_MARKERS.findall(objective))
    separators = len(re.findall(r"(?:\band\b|\bthen\b|\bpuis\b|\bet\b|[,;])", objective, re.IGNORECASE))
    if reason_code == "bounded_turn_infeasible":
        return len(words) >= 18 and marker_count >= 1
    if reason_code in {"extended_research", "iterative_artifact"}:
        return len(words) >= 10 and marker_count >= 1
    return len(words) >= 12 and (marker_count >= 2 or separators >= 2)


def looks_like_schedule_route(text: str) -> bool:
    from osia import scheduled_tasks

    return scheduled_tasks.looks_like_schedule_request(text) or bool(
        _SCHEDULE_ROUTE_RE.search(text)
    )


def canonicalize_handoff_arguments(
    arguments: Mapping[str, Any] | None,
    original_user_text: str,
) -> dict[str, Any]:
    """Bound model routing metadata before strict schema validation.

    Some providers include an objective or proposed command beside the routing
    metadata. Those fields are never authoritative: the exact objective comes
    from the current turn context, and sibling side effects are discarded at
    this boundary. An explicit user request also supplies the routing intent,
    so malformed metadata must not make that handoff provider-format-dependent.
    """

    values = dict(arguments or {})
    explicit = explicit_autonomous_request(original_user_text)
    reason = str(values.get("reason_code") or "").strip()
    if reason not in AUTONOMOUS_REASON_CODES:
        reason = "dependent_phases"
    try:
        confidence = float(values.get("confidence"))
    except (TypeError, ValueError):
        # Calling the dedicated route tool is itself the provider's structured
        # route selection. Eligibility still depends on the deterministic
        # objective/schedule/clarification guards in evaluate_route_request().
        confidence = 1.0
    return {
        "reason_code": reason,
        "confidence": max(0.0, min(1.0, confidence)),
        "explicit_request": explicit,
    }


def evaluate_route_request(
    context: Mapping[str, Any] | None,
    *,
    reason_code: Any,
    confidence: Any,
    explicit_request: Any,
) -> dict[str, Any]:
    """Validate a model-selected Autonomous route before any handoff effect."""

    ctx = dict(context or {})
    original = str(ctx.get("original_user_text") or "").strip()
    reason = str(reason_code or "").strip()
    try:
        score = float(confidence)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))

    if looks_like_schedule_route(original):
        return {
            "ok": False,
            "route": "scheduled",
            "status": "scheduled_route_required",
            "reason_code": "scheduled_request",
            "confidence": score,
        }

    explicit = explicit_autonomous_request(original)
    objective = exact_objective(original, ctx.get("history") or ())
    if explicit and not objective:
        return {
            "ok": False,
            "route": "clarification",
            "status": "objective_required",
            "reason_code": "missing_objective",
            "confidence": score,
        }
    if reason not in AUTONOMOUS_REASON_CODES:
        return {
            "ok": False,
            "route": "normal",
            "status": "invalid_reason",
            "reason_code": "invalid_reason",
            "confidence": score,
        }
    automatic_execution = bool(
        reason == "sustained_computer_work" and objective and score >= 0.80
    )
    if not explicit and (
        bool(explicit_request)
        or score < 0.80
        or (not automatic_execution and not _looks_genuinely_long(objective, reason))
    ):
        return {
            "ok": False,
            "route": "normal",
            "status": "automatic_route_rejected",
            "reason_code": reason,
            "confidence": score,
        }
    if not objective:
        return {
            "ok": False,
            "route": "clarification",
            "status": "objective_required",
            "reason_code": "missing_objective",
            "confidence": score,
        }
    return {
        "ok": True,
        "route": "autonomous",
        "status": "validated",
        "reason_code": reason,
        "confidence": score,
        "explicit_request": explicit,
        "objective": objective,
        "source_refs": list(extract_source_refs(ctx.get("provider_input"))),
    }


def execute_route_request(
    context: Mapping[str, Any] | None,
    *,
    reason_code: Any,
    confidence: Any,
    explicit_request: Any,
) -> dict[str, Any]:
    """Validate and invoke one host launcher, returning a bounded safe result."""

    ctx = dict(context or {})
    decision = evaluate_route_request(
        ctx,
        reason_code=reason_code,
        confidence=confidence,
        explicit_request=explicit_request,
    )
    if not decision.get("ok"):
        return decision
    launcher = ctx.get("launcher")
    if not callable(launcher):
        return {
            **{key: decision[key] for key in ("route", "reason_code", "confidence")},
            "ok": False,
            "status": "unavailable",
            "error_code": "runner_transport_unavailable",
        }
    request = {
        "objective": decision["objective"],
        "source_refs": decision["source_refs"],
        "provider_id": str(ctx.get("provider_id") or "").strip().lower()[:48],
        "model_id": str(ctx.get("model_id") or "").strip()[:160],
        "request_id": str(ctx.get("request_id") or "")[:160],
        "source_request_id": str(
            ctx.get("source_request_id") or ctx.get("request_id") or ""
        )[:160],
        "source_turn_id": str(
            ctx.get("source_turn_id") or ctx.get("request_id") or ""
        )[:160],
        "source_assistant_message_id": str(
            ctx.get("source_assistant_message_id") or ""
        )[:160],
        "conversation_id": str(ctx.get("conversation_id") or "")[:160],
        "origin_ref": str(ctx.get("origin_ref") or "desktop")[:160],
        "device_id": str(ctx.get("device_id") or "")[:128],
        "route": "autonomous",
        "reason_code": decision["reason_code"],
        "confidence": decision["confidence"],
        "explicit_request": decision["explicit_request"],
        "turn_context": ctx.get("turn_context"),
    }
    progress_callback = ctx.get("progress_callback")
    if callable(progress_callback):
        try:
            progress_callback({
                "status": "planning",
                "progress_status": "planning",
                "terminal": False,
                "conversation_id": request["conversation_id"],
                "request_id": request["source_request_id"],
                "turn_id": request["source_turn_id"],
                "assistant_message_id": request["source_assistant_message_id"],
                "provider_id": request["provider_id"],
                "model_id": request["model_id"],
            })
        except Exception:
            pass
    try:
        result = launcher(request)
    except Exception:
        result = {
            "status": "unavailable",
            "error_code": "runner_transport_unavailable",
        }
    if not isinstance(result, Mapping):
        return {
            "ok": False,
            "route": "autonomous",
            "status": "unavailable",
            "reason_code": decision["reason_code"],
            "confidence": decision["confidence"],
            "error_code": "runner_transport_unavailable",
        }
    initial_result = dict(result)
    bind_execution = ctx.get("bind_execution")
    if callable(bind_execution) and (
        initial_result.get("run_id") or initial_result.get("task_id")
    ):
        try:
            bind_execution(initial_result, request)
        except Exception:
            pass
    waiter = ctx.get("waiter")
    if callable(waiter) and str(initial_result.get("status") or "") in {
        "running", "authorization_pending", "starting", "outcome_unknown",
    }:
        try:
            waited = waiter(
                initial_result,
                request,
                progress_callback=ctx.get("progress_callback"),
                bind_execution=bind_execution,
            )
            if isinstance(waited, Mapping):
                result = waited
        except Exception:
            result = {
                **initial_result,
                "status": "outcome_unknown",
                "error_code": "durable_state_unavailable",
            }
    status = str(result.get("status") or "failed")
    if status not in {
        "running", "authorization_pending", "starting", "busy",
        "unavailable", "failed", "outcome_unknown", "completed",
        "blocked", "cancelled",
    }:
        status = "failed"
    return {
        "ok": status in {
            "running", "authorization_pending", "starting", "completed",
        },
        "route": "autonomous",
        "status": status,
        "reason_code": decision["reason_code"],
        "confidence": decision["confidence"],
        "task_id": str(result.get("task_id") or "")[:160],
        "run_id": str(result.get("run_id") or "")[:160],
        "error_code": str(result.get("error_code") or "")[:80],
        "provider_id": str(
            result.get("provider_id") or request["provider_id"]
        )[:48],
        "model_id": str(result.get("model_id") or request["model_id"])[:160],
        "terminal": bool(result.get("terminal")) or status in {
            "completed", "blocked", "cancelled", "failed",
        },
        "final_text": str(result.get("final_text") or "")[:32_000],
        "outcome_unknown": bool(result.get("outcome_unknown")),
        "progress_status": str(result.get("progress_status") or "")[:32],
    }


def durable_progress_from_state(st: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project one durable run into bounded Chat progress without reasoning."""

    if not isinstance(st, Mapping):
        return {}
    status = str(st.get("status") or "")
    run_state = str(st.get("run_state") or "")
    abandon_effect = str(st.get("abandon_effect_state") or "")
    outcome_unknown = bool(
        run_state in {"blocked_outcome_unknown", "blocked_unknown_effect"}
        or abandon_effect == "effect_possible_or_unknown"
    )
    terminal = False
    if status == "done" or run_state == "completed":
        progress_status = "completed"
        terminal = True
    elif status in {"aborted", "cancelled"} or run_state in {
        "cancelled", "cancelled_proven",
    }:
        progress_status = "blocked" if outcome_unknown else "cancelled"
        terminal = True
    elif status == "failed" or run_state in {
        "failed", "blocked_unknown_effect",
    }:
        progress_status = "blocked"
        terminal = True
    elif run_state in {
        "authorization_pending", "suspended_scope_extension",
        "suspended_authorization_revoked", "waiting_tool",
        "cancellation_requested", "suspended_browser_intervention",
        "suspended_browser_state",
    }:
        progress_status = "waiting"
    elif run_state == "verifying_tool":
        progress_status = "verifying"
    elif run_state in {"resuming", "blocked_outcome_unknown"}:
        progress_status = "recovering"
    elif run_state in {"planning", "ready"}:
        progress_status = "planning"
    else:
        progress_status = "executing"
    tasks = [
        task
        for phase in (st.get("phases") or [])
        if isinstance(phase, Mapping)
        for task in (phase.get("tasks") or [])
        if isinstance(task, Mapping)
    ]
    return {
        "status": progress_status,
        "progress_status": progress_status,
        "terminal": terminal,
        "outcome_unknown": outcome_unknown,
        "conversation_id": str(st.get("source_conversation_id") or "")[:160],
        "request_id": str(st.get("source_request_id") or "")[:160],
        "turn_id": str(st.get("source_turn_id") or "")[:160],
        "assistant_message_id": str(
            st.get("source_assistant_message_id") or ""
        )[:160],
        "task_id": str(st.get("task_id") or "")[:160],
        "run_id": str(st.get("run_id") or "")[:160],
        "provider_id": str(st.get("provider") or "")[:48],
        "model_id": str(st.get("model") or "")[:160],
        "tasks_done": sum(task.get("status") == "done" for task in tasks),
        "tasks_total": len(tasks),
    }


_RESPONSES = {
    "en": {
        "running": "I started this as an Autonomous mission. Progress and controls are available in Autonomous.",
        "starting": "The Autonomous mission is starting. Progress and controls are available in Autonomous.",
        "authorization_pending": "I prepared the Autonomous mission, but it is waiting for your authorization before it can run.",
        "busy": "I could not start this mission because another Autonomous mission is already active. I did not run it in normal chat.",
        "unavailable": "I could not reach the canonical Autonomous runner, so I did not start or execute the mission in normal chat.",
        "transport_unavailable": "I could not reach the canonical Autonomous runner, so I did not start or execute the mission in normal chat.",
        "chat_model_unavailable": "The model used for this chat turn is not available for Autonomous, so I did not start the mission or run it in normal chat.",
        "failed": "The Autonomous mission could not be started. I did not execute it in normal chat.",
        "outcome_unknown": "The Autonomous start outcome could not be confirmed. Check Autonomous before retrying; I did not execute it in normal chat.",
        "objective_required": "What exact objective should I run in Autonomous mode?",
    },
    "fr": {
        "running": "J’ai lancé cette demande comme mission Autonomous. La progression et les contrôles sont disponibles dans Autonomous.",
        "starting": "La mission Autonomous démarre. La progression et les contrôles sont disponibles dans Autonomous.",
        "authorization_pending": "J’ai préparé la mission Autonomous, mais elle attend votre autorisation avant de pouvoir s’exécuter.",
        "busy": "Je n’ai pas pu lancer cette mission, car une autre mission Autonomous est déjà active. Je ne l’ai pas exécutée dans le chat normal.",
        "unavailable": "Je n’ai pas pu joindre le runner Autonomous canonique ; la mission n’a été ni lancée ni exécutée dans le chat normal.",
        "transport_unavailable": "Je n’ai pas pu joindre le runner Autonomous canonique ; la mission n’a été ni lancée ni exécutée dans le chat normal.",
        "chat_model_unavailable": "Le modèle utilisé pour ce tour de chat n’est pas disponible pour Autonomous ; la mission n’a été ni lancée ni exécutée dans le chat normal.",
        "failed": "La mission Autonomous n’a pas pu démarrer. Je ne l’ai pas exécutée dans le chat normal.",
        "outcome_unknown": "Le résultat du démarrage Autonomous n’a pas pu être confirmé. Vérifiez Autonomous avant de réessayer ; je ne l’ai pas exécutée dans le chat normal.",
        "objective_required": "Quel objectif exact dois-je exécuter en mode Autonomous ?",
    },
    "es": {
        "running": "He iniciado esta solicitud como misión Autonomous. El progreso y los controles están disponibles en Autonomous.",
        "starting": "La misión Autonomous se está iniciando. El progreso y los controles están disponibles en Autonomous.",
        "authorization_pending": "He preparado la misión Autonomous, pero espera tu autorización antes de ejecutarse.",
        "busy": "No pude iniciar esta misión porque ya hay otra misión Autonomous activa. No la ejecuté en el chat normal.",
        "unavailable": "No pude acceder al ejecutor Autonomous canónico, así que no inicié ni ejecuté la misión en el chat normal.",
        "transport_unavailable": "No pude acceder al ejecutor Autonomous canónico, así que no inicié ni ejecuté la misión en el chat normal.",
        "chat_model_unavailable": "El modelo usado en este turno de chat no está disponible para Autonomous, así que no inicié ni ejecuté la misión en el chat normal.",
        "failed": "No se pudo iniciar la misión Autonomous. No la ejecuté en el chat normal.",
        "outcome_unknown": "No se pudo confirmar el inicio de Autonomous. Comprueba Autonomous antes de reintentar; no lo ejecuté en el chat normal.",
        "objective_required": "¿Qué objetivo exacto debo ejecutar en modo Autonomous?",
    },
    "de": {
        "running": "Ich habe diese Anfrage als Autonomous-Mission gestartet. Fortschritt und Steuerung sind in Autonomous verfügbar.",
        "starting": "Die Autonomous-Mission wird gestartet. Fortschritt und Steuerung sind in Autonomous verfügbar.",
        "authorization_pending": "Ich habe die Autonomous-Mission vorbereitet; sie wartet vor der Ausführung auf deine Autorisierung.",
        "busy": "Ich konnte die Mission nicht starten, weil bereits eine Autonomous-Mission aktiv ist. Im normalen Chat wurde sie nicht ausgeführt.",
        "unavailable": "Der kanonische Autonomous-Runner war nicht erreichbar; die Mission wurde weder gestartet noch im normalen Chat ausgeführt.",
        "transport_unavailable": "Der kanonische Autonomous-Runner war nicht erreichbar; die Mission wurde weder gestartet noch im normalen Chat ausgeführt.",
        "chat_model_unavailable": "Das in diesem Chat verwendete Modell ist für Autonomous nicht verfügbar; die Mission wurde weder gestartet noch im normalen Chat ausgeführt.",
        "failed": "Die Autonomous-Mission konnte nicht gestartet werden. Im normalen Chat wurde sie nicht ausgeführt.",
        "outcome_unknown": "Der Autonomous-Start konnte nicht bestätigt werden. Prüfe Autonomous vor einem neuen Versuch; im normalen Chat wurde nichts ausgeführt.",
        "objective_required": "Welches genaue Ziel soll ich im Autonomous-Modus ausführen?",
    },
    "zh": {
        "running": "我已将此请求作为 Autonomous 任务启动。你可以在 Autonomous 中查看进度和控制任务。",
        "starting": "Autonomous 任务正在启动。你可以在 Autonomous 中查看进度和控制任务。",
        "authorization_pending": "Autonomous 任务已准备好，但需要你的授权才能运行。",
        "busy": "当前已有一个 Autonomous 任务在运行，因此无法启动此任务。我没有在普通聊天中执行它。",
        "unavailable": "无法连接规范的 Autonomous 运行器，因此任务未启动，也没有在普通聊天中执行。",
        "transport_unavailable": "无法连接规范的 Autonomous 运行器，因此任务未启动，也没有在普通聊天中执行。",
        "chat_model_unavailable": "此聊天轮次使用的模型无法用于 Autonomous，因此任务未启动，也没有在普通聊天中执行。",
        "failed": "Autonomous 任务无法启动。我没有在普通聊天中执行它。",
        "outcome_unknown": "无法确认 Autonomous 是否启动。重试前请先检查 Autonomous；我没有在普通聊天中执行它。",
        "objective_required": "你希望 Autonomous 模式执行的确切目标是什么？",
    },
    "ja": {
        "running": "この依頼を Autonomous ミッションとして開始しました。進行状況と操作は Autonomous で確認できます。",
        "starting": "Autonomous ミッションを開始しています。進行状況と操作は Autonomous で確認できます。",
        "authorization_pending": "Autonomous ミッションを準備しましたが、実行前に承認が必要です。",
        "busy": "別の Autonomous ミッションが実行中のため開始できませんでした。通常チャットでは実行していません。",
        "unavailable": "正規の Autonomous ランナーに接続できなかったため、ミッションは開始せず、通常チャットでも実行していません。",
        "transport_unavailable": "正規の Autonomous ランナーに接続できなかったため、ミッションは開始せず、通常チャットでも実行していません。",
        "chat_model_unavailable": "このチャットターンで使用したモデルは Autonomous では利用できないため、ミッションは開始せず、通常チャットでも実行していません。",
        "failed": "Autonomous ミッションを開始できませんでした。通常チャットでは実行していません。",
        "outcome_unknown": "Autonomous の開始結果を確認できませんでした。再試行前に Autonomous を確認してください。通常チャットでは実行していません。",
        "objective_required": "Autonomous モードで実行する正確な目的を教えてください。",
    },
}


_TERMINAL_RESPONSES = {
    "en": {
        "completed": "The durable execution completed.",
        "blocked": "The durable execution stopped without a verified completion.",
        "cancelled": "I cancelled this durable execution.",
    },
    "fr": {
        "completed": "L’exécution durable est terminée.",
        "blocked": "L’exécution durable s’est arrêtée sans résultat final vérifié.",
        "cancelled": "J’ai annulé cette exécution durable.",
    },
}


def terminal_result_from_state(
    st: Mapping[str, Any] | None,
    language: str = "en",
) -> dict[str, Any]:
    """Return the canonical terminal Chat result for one durable checkpoint."""

    projected = durable_progress_from_state(st)
    if not projected or not projected.get("terminal"):
        return projected
    lang = str(language or "en").lower()
    localized = _TERMINAL_RESPONSES.get(lang, _TERMINAL_RESPONSES["en"])
    status = str(projected.get("status") or "blocked")
    final_text = str((st or {}).get("terminal_response") or "").strip()
    if not final_text:
        final_text = localized.get(status, localized["blocked"])
    diagnostic = (st or {}).get("failure_diagnostic")
    error_code = (
        str(diagnostic.get("reason_code") or "")[:80]
        if isinstance(diagnostic, Mapping) else ""
    )
    return {
        **projected,
        "final_text": final_text[:32_000],
        "error_code": error_code,
    }


_CHAT_MODEL_ERROR_CODES = frozenset({
    "chat_model_identity_missing",
    "chat_model_unavailable",
    "model_setup_failure",
    "no_configured_model",
    "strict_saved_selection_unavailable",
})
_TRANSPORT_ERROR_CODES = frozenset({
    "runner_transport_unavailable",
    "server_unavailable",
})


def handoff_response_text(result: Mapping[str, Any], language: str = "en") -> str:
    lang = str(language or "en").lower()
    if lang not in _RESPONSES:
        lang = "en"
    error_code = str(result.get("error_code") or "")
    if error_code in _CHAT_MODEL_ERROR_CODES:
        return _RESPONSES[lang]["chat_model_unavailable"]
    if error_code in _TRANSPORT_ERROR_CODES:
        return _RESPONSES[lang]["transport_unavailable"]
    final_text = str(result.get("final_text") or "").strip()
    if final_text and str(result.get("status") or "") in {
        "completed", "blocked", "cancelled",
    }:
        return final_text
    status = str(result.get("status") or "failed")
    if status in _TERMINAL_RESPONSES["en"]:
        localized = _TERMINAL_RESPONSES.get(lang, _TERMINAL_RESPONSES["en"])
        return localized.get(status, _TERMINAL_RESPONSES["en"][status])
    return _RESPONSES[lang].get(status, _RESPONSES[lang]["failed"])


__all__ = [
    "AUTONOMOUS_REASON_CODES",
    "ROUTING_TOOL_NAME",
    "durable_progress_from_state",
    "evaluate_route_request",
    "exact_objective",
    "execute_route_request",
    "explicit_autonomous_request",
    "extract_source_refs",
    "handoff_response_text",
    "looks_like_schedule_route",
    "routing_prompt",
    "terminal_result_from_state",
]
