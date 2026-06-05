#!/usr/bin/env python3
"""OSIA internationalization (i18n) foundation.

A small, dependency-free translation layer for the PC GUI. Strings are
looked up by a stable key via :func:`t`; the active language is read from
``config/settings.json`` (key ``"language"``, default ``"en"``) and can be
changed at runtime with :func:`set_language`.

Usage::

    from osia import i18n
    label = i18n.t("settings.save")          # -> "Save" / "Enregistrer" / …
    title = i18n.t("provider.key_placeholder", name="OpenAI")

Live updates: widgets that display translated text can register a callback
with :func:`add_listener`; every :func:`set_language` call invokes the
listeners so the UI can re-translate itself in place. Remember to
:func:`remove_listener` when a widget is destroyed.

Migration status: this is the foundation. The newly-added / changed UI
strings (the provider configuration card, the "Get API key" row, and the
language selector) are routed through ``t()``. The remaining legacy GUI
strings are still hardcoded and tracked for a follow-up migration pass in
``docs/i18n_backlog.md``.
"""

from typing import Callable

# Supported languages: code -> native display name. English is the default
# and the fallback for any missing key.
LANGUAGES: dict[str, str] = {
    "en": "English",
    "fr": "Français",
    "zh": "中文",
    "ja": "日本語",
    "de": "Deutsch",
    "es": "Español",
}

DEFAULT_LANG = "en"

# ── Translation table ────────────────────────────────────────────────────
# TRANSLATIONS[key][lang] -> string. A missing (key, lang) falls back to the
# English entry, then to the key itself. Use ``{name}``-style placeholders
# and pass them as kwargs to :func:`t`.
TRANSLATIONS: dict[str, dict[str, str]] = {
    # — Provider configuration card —
    "provider.key_placeholder": {
        "en": "{name} API key",
        "fr": "Clé API {name}",
        "zh": "{name} API 密钥",
        "ja": "{name} API キー",
        "de": "{name} API-Schlüssel",
        "es": "Clave API de {name}",
    },
    "provider.save": {
        "en": "Save",
        "fr": "Enregistrer",
        "zh": "保存",
        "ja": "保存",
        "de": "Speichern",
        "es": "Guardar",
    },
    "provider.saved": {
        "en": "Saved ✓",
        "fr": "Enregistré ✓",
        "zh": "已保存 ✓",
        "ja": "保存しました ✓",
        "de": "Gespeichert ✓",
        "es": "Guardado ✓",
    },
    "provider.get_key": {
        "en": "Get API key",
        "fr": "Configurer une clé API",
        "zh": "获取 API 密钥",
        "ja": "API キーを取得",
        "de": "API-Schlüssel erhalten",
        "es": "Obtener clave API",
    },
    "provider.browse": {
        "en": "Browse",
        "fr": "Naviguer",
        "zh": "打开",
        "ja": "開く",
        "de": "Öffnen",
        "es": "Abrir",
    },
    "provider.no_key_required": {
        "en": "No key required",
        "fr": "Aucune clé requise",
        "zh": "无需密钥",
        "ja": "キー不要",
        "de": "Kein Schlüssel erforderlich",
        "es": "No se requiere clave",
    },
    # — Free-tier info badges (model panel grid + settings provider cards) —
    "google_freetier_info": {
        "en": "Google offers a free tier of 1500 req/day. ⚠️ In free mode, your "
              "data may be used by Google for model training. Enable billing "
              "for full privacy (paid, but private).",
        "fr": "Google offre un niveau gratuit de 1500 req/jour. ⚠️ En mode "
              "gratuit, vos données peuvent être utilisées par Google pour "
              "entraîner ses modèles. Activez la facturation pour une "
              "confidentialité totale (payant, mais privé).",
        "zh": "Google 提供每天 1500 次请求的免费额度。⚠️ 在免费模式下，您的数据"
              "可能被 Google 用于模型训练。启用计费以获得完全隐私（付费，但私密）。",
        "ja": "Google は1日1500リクエストの無料枠を提供しています。⚠️ 無料モード"
              "では、データが Google のモデル学習に使用される場合があります。完全な"
              "プライバシーのためには課金を有効にしてください（有料ですが、プライベートです）。",
        "de": "Google bietet ein kostenloses Kontingent von 1500 Anfragen/Tag. "
              "⚠️ Im kostenlosen Modus können Ihre Daten von Google für das "
              "Modelltraining verwendet werden. Aktivieren Sie die Abrechnung "
              "für vollständige Privatsphäre (kostenpflichtig, aber privat).",
        "es": "Google ofrece un nivel gratuito de 1500 solicitudes/día. ⚠️ En "
              "modo gratuito, Google puede usar tus datos para entrenar sus "
              "modelos. Activa la facturación para privacidad total (de pago, "
              "pero privado).",
    },
    "groq_freetier_info": {
        "en": "Groq offers a free tier with generous rate limits, ideal for "
              "testing. Limits are lower than paid usage.",
        "fr": "Groq offre un niveau gratuit avec des limites de débit "
              "généreuses, idéal pour tester. Les limites sont inférieures à "
              "celles de l'usage payant.",
        "zh": "Groq 提供速率限制宽松的免费额度，非常适合测试。其限制低于付费使用。",
        "ja": "Groq は寛大なレート制限の無料枠を提供しており、テストに最適です。"
              "制限は有料利用よりも低くなっています。",
        "de": "Groq bietet ein kostenloses Kontingent mit großzügigen "
              "Ratenlimits, ideal zum Testen. Die Limits sind niedriger als bei "
              "kostenpflichtiger Nutzung.",
        "es": "Groq ofrece un nivel gratuito con límites de uso generosos, "
              "ideal para pruebas. Los límites son inferiores a los del uso de "
              "pago.",
    },
    # — Conversations panel —
    "conversations.empty": {
        "en": "No conversations",
        "fr": "Aucune conversation",
        "zh": "暂无对话",
        "ja": "会話はありません",
        "de": "Keine Unterhaltungen",
        "es": "No hay conversaciones",
    },
    # — Language selector (part of the foundation) —
    "settings.language": {
        "en": "Language",
        "fr": "Langue",
        "zh": "语言",
        "ja": "言語",
        "de": "Sprache",
        "es": "Idioma",
    },
    "settings.language_sub": {
        "en": "Interface language.",
        "fr": "Langue de l'interface.",
        "zh": "界面语言。",
        "ja": "インターフェースの言語。",
        "de": "Sprache der Oberfläche.",
        "es": "Idioma de la interfaz.",
    },
    # — Section 1: AI providers (text / chat models) —
    "settings.ai_providers": {
        "en": "AI Providers",
        "fr": "Fournisseurs IA",
        "zh": "AI 提供商",
        "ja": "AI プロバイダー",
        "de": "KI-Anbieter",
        "es": "Proveedores de IA",
    },
    "settings.ai_providers_sub": {
        "en": "API keys for text & chat models.",
        "fr": "Clés API pour les modèles de texte et de chat.",
        "zh": "用于文本和聊天模型的 API 密钥。",
        "ja": "テキスト・チャットモデル用の API キー。",
        "de": "API-Schlüssel für Text- und Chat-Modelle.",
        "es": "Claves API para modelos de texto y chat.",
    },
    # — Section 2: voice providers (text-to-speech) —
    "settings.voice_providers": {
        "en": "Voice Providers",
        "fr": "Fournisseurs vocaux",
        "zh": "语音提供商",
        "ja": "音声プロバイダー",
        "de": "Sprachanbieter",
        "es": "Proveedores de voz",
    },
    "settings.voice_providers_sub": {
        "en": "Text-to-speech — how OSIA speaks.",
        "fr": "Synthèse vocale — comment OSIA parle.",
        "zh": "文字转语音 — OSIA 的发声方式。",
        "ja": "テキスト読み上げ — OSIA の話し方。",
        "de": "Sprachausgabe — wie OSIA spricht.",
        "es": "Texto a voz — cómo habla OSIA.",
    },
    "tts.local_option": {
        "en": "Local (Kokoro)",
        "fr": "Local (Kokoro)",
        "zh": "本地 (Kokoro)",
        "ja": "ローカル (Kokoro)",
        "de": "Lokal (Kokoro)",
        "es": "Local (Kokoro)",
    },
    "tts.kokoro_desc": {
        "en": "Local · free · no key required",
        "fr": "Local · gratuit · aucune clé requise",
        "zh": "本地 · 免费 · 无需密钥",
        "ja": "ローカル · 無料 · キー不要",
        "de": "Lokal · kostenlos · kein Schlüssel nötig",
        "es": "Local · gratis · sin clave",
    },
    # — Section 3: Whisper providers (speech-to-text) —
    "settings.whisper_providers": {
        "en": "Whisper Providers",
        "fr": "Fournisseurs Whisper",
        "zh": "Whisper 提供商",
        "ja": "Whisper プロバイダー",
        "de": "Whisper-Anbieter",
        "es": "Proveedores Whisper",
    },
    "settings.whisper_providers_sub": {
        "en": "Speech-to-text — microphone transcription.",
        "fr": "Reconnaissance vocale — transcription du micro.",
        "zh": "语音转文字 — 麦克风转录。",
        "ja": "音声認識 — マイクの文字起こし。",
        "de": "Spracherkennung — Mikrofon-Transkription.",
        "es": "Voz a texto — transcripción del micrófono.",
    },
    "stt.local_name": {
        "en": "faster-whisper (local)",
        "fr": "faster-whisper (local)",
        "zh": "faster-whisper（本地）",
        "ja": "faster-whisper（ローカル）",
        "de": "faster-whisper (lokal)",
        "es": "faster-whisper (local)",
    },
    "stt.groq_name": {
        "en": "Groq Whisper",
        "fr": "Groq Whisper",
        "zh": "Groq Whisper",
        "ja": "Groq Whisper",
        "de": "Groq Whisper",
        "es": "Groq Whisper",
    },
    "stt.groq_hint": {
        "en": "Uses your Groq API key (set in AI Providers).",
        "fr": "Utilise votre clé API Groq (définie dans Fournisseurs IA).",
        "zh": "使用您的 Groq API 密钥（在 AI 提供商中设置）。",
        "ja": "Groq API キーを使用します（AI プロバイダーで設定）。",
        "de": "Verwendet Ihren Groq-API-Schlüssel (in KI-Anbieter festgelegt).",
        "es": "Usa tu clave API de Groq (configurada en Proveedores de IA).",
    },
    "stt.model_not_downloaded": {
        "en": "{name} (not downloaded)",
        "fr": "{name} (non téléchargé)",
        "zh": "{name}（未下载）",
        "ja": "{name}（未ダウンロード）",
        "de": "{name} (nicht heruntergeladen)",
        "es": "{name} (no descargado)",
    },
    "settings.fallback_provider": {
        "en": "Fallback provider",
        "fr": "Fournisseur de secours",
        "zh": "备用提供商",
        "ja": "フォールバックプロバイダー",
        "de": "Ausweich-Anbieter",
        "es": "Proveedor de respaldo",
    },
    "stt.fallback_none": {
        "en": "None",
        "fr": "Aucun",
        "zh": "无",
        "ja": "なし",
        "de": "Keine",
        "es": "Ninguno",
    },
    # — Shared field labels (provider / voice / model selectors) —
    "settings.active_provider": {
        "en": "Active provider",
        "fr": "Fournisseur actif",
        "zh": "当前提供商",
        "ja": "使用中のプロバイダー",
        "de": "Aktiver Anbieter",
        "es": "Proveedor activo",
    },
    "settings.choose_provider": {
        "en": "Choose provider",
        "fr": "Choisir un fournisseur",
        "zh": "选择提供商",
        "ja": "プロバイダーを選択",
        "de": "Anbieter wählen",
        "es": "Elegir proveedor",
    },
    "settings.voice": {
        "en": "Voice",
        "fr": "Voix",
        "zh": "声音",
        "ja": "音声",
        "de": "Stimme",
        "es": "Voz",
    },
    "settings.model": {
        "en": "Model",
        "fr": "Modèle",
        "zh": "模型",
        "ja": "モデル",
        "de": "Modell",
        "es": "Modelo",
    },
    # — Mobile connection (pairing token) —
    "settings.mobile": {
        "en": "Mobile Connection",
        "fr": "Connexion mobile",
        "zh": "移动设备连接",
        "ja": "モバイル接続",
        "de": "Mobile Verbindung",
        "es": "Conexión móvil",
    },
    "settings.mobile_sub": {
        "en": "Pairing token for the OSIA mobile app.",
        "fr": "Jeton d'appairage pour l'application mobile OSIA.",
        "zh": "OSIA 移动应用的配对令牌。",
        "ja": "OSIA モバイルアプリのペアリングトークン。",
        "de": "Kopplungstoken für die OSIA-Mobil-App.",
        "es": "Token de emparejamiento para la app móvil OSIA.",
    },
    "settings.mobile_token": {
        "en": "Token",
        "fr": "Jeton",
        "zh": "令牌",
        "ja": "トークン",
        "de": "Token",
        "es": "Token",
    },
    "settings.show": {
        "en": "Show",
        "fr": "Afficher",
        "zh": "显示",
        "ja": "表示",
        "de": "Anzeigen",
        "es": "Mostrar",
    },
    "settings.regenerate": {
        "en": "Regenerate",
        "fr": "Régénérer",
        "zh": "重新生成",
        "ja": "再生成",
        "de": "Neu generieren",
        "es": "Regenerar",
    },
    "settings.close": {
        "en": "Close",
        "fr": "Fermer",
        "zh": "关闭",
        "ja": "閉じる",
        "de": "Schließen",
        "es": "Cerrar",
    },
    "settings.copy": {
        "en": "Copy",
        "fr": "Copier",
        "zh": "复制",
        "ja": "コピー",
        "de": "Kopieren",
        "es": "Copiar",
    },
    "settings.copied": {
        "en": "Copied ✓",
        "fr": "Copié ✓",
        "zh": "已复制 ✓",
        "ja": "コピーしました ✓",
        "de": "Kopiert ✓",
        "es": "Copiado ✓",
    },
    "settings.mobile_token_warning": {
        "en": "Regenerating disconnects the mobile app — you'll need to reconnect.",
        "fr": "Régénérer déconnecte l'app mobile — vous devrez vous reconnecter.",
        "zh": "重新生成会断开移动应用 — 您需要重新连接。",
        "ja": "再生成するとモバイルアプリが切断されます — 再接続が必要です。",
        "de": "Das Neugenerieren trennt die Mobil-App — Sie müssen sich erneut verbinden.",
        "es": "Regenerar desconecta la app móvil — tendrás que volver a conectar.",
    },
    "settings.mobile_token_current": {
        "en": "Mobile token",
        "fr": "Jeton mobile",
        "zh": "移动设备令牌",
        "ja": "モバイルトークン",
        "de": "Mobile-Token",
        "es": "Token móvil",
    },
    "settings.mobile_token_new": {
        "en": "New token generated",
        "fr": "Nouveau jeton généré",
        "zh": "已生成新令牌",
        "ja": "新しいトークンを生成しました",
        "de": "Neuer Token generiert",
        "es": "Nuevo token generado",
    },
    # — AI Task Hierarchy (Bloc 3) —
    "settings.ai_hierarchy": {
        "en": "AI Task Hierarchy",
        "fr": "Hiérarchie des tâches IA",
        "zh": "AI 任务层级",
        "ja": "AI タスク階層",
        "de": "KI-Aufgabenhierarchie",
        "es": "Jerarquía de tareas IA",
    },
    "settings.ai_hierarchy_sub": {
        "en": "Ordered fallback chains. OSIA routes each task to the first "
              "model, then down the list if it hits a technical error.",
        "fr": "Chaînes de secours ordonnées. OSIA dirige chaque tâche vers le "
              "premier modèle, puis descend la liste en cas d'erreur technique.",
        "zh": "有序的备用链。OSIA 将每个任务路由到第一个模型，遇到技术错误时依次"
              "向下尝试。",
        "ja": "順序付きフォールバックチェーン。OSIA は各タスクを最初のモデルに振り"
              "分け、技術的エラー時はリストを順に下ります。",
        "de": "Geordnete Ausweichketten. OSIA leitet jede Aufgabe an das erste "
              "Modell und geht bei einem technischen Fehler die Liste hinab.",
        "es": "Cadenas de respaldo ordenadas. OSIA dirige cada tarea al primer "
              "modelo y baja por la lista si encuentra un error técnico.",
    },
    "settings.hierarchy_heavy": {
        "en": "Heavy tasks",
        "fr": "Tâches lourdes",
        "zh": "重型任务",
        "ja": "重いタスク",
        "de": "Schwere Aufgaben",
        "es": "Tareas pesadas",
    },
    "settings.hierarchy_heavy_sub": {
        "en": "Code, refactoring, debugging, architecture, complex reasoning.",
        "fr": "Code, refactorisation, débogage, architecture, raisonnement complexe.",
        "zh": "代码、重构、调试、架构、复杂推理。",
        "ja": "コード、リファクタリング、デバッグ、設計、複雑な推論。",
        "de": "Code, Refactoring, Debugging, Architektur, komplexes Schließen.",
        "es": "Código, refactorización, depuración, arquitectura, razonamiento complejo.",
    },
    "settings.hierarchy_medium": {
        "en": "Medium tasks",
        "fr": "Tâches moyennes",
        "zh": "中型任务",
        "ja": "中程度のタスク",
        "de": "Mittlere Aufgaben",
        "es": "Tareas medianas",
    },
    "settings.hierarchy_medium_sub": {
        "en": "Web, data gathering, email, summaries, routine tasks.",
        "fr": "Web, collecte de données, e-mail, résumés, tâches courantes.",
        "zh": "网页、数据收集、邮件、摘要、日常任务。",
        "ja": "ウェブ、データ収集、メール、要約、日常的なタスク。",
        "de": "Web, Datenerfassung, E-Mail, Zusammenfassungen, Routineaufgaben.",
        "es": "Web, recopilación de datos, correo, resúmenes, tareas rutinarias.",
    },
    "settings.hierarchy_add": {
        "en": "Add model",
        "fr": "Ajouter un modèle",
        "zh": "添加模型",
        "ja": "モデルを追加",
        "de": "Modell hinzufügen",
        "es": "Añadir modelo",
    },
    "settings.hierarchy_empty": {
        "en": "No models — uses the manually selected model.",
        "fr": "Aucun modèle — utilise le modèle sélectionné manuellement.",
        "zh": "无模型 — 使用手动选择的模型。",
        "ja": "モデルなし — 手動で選択したモデルを使用します。",
        "de": "Keine Modelle — verwendet das manuell gewählte Modell.",
        "es": "Sin modelos — usa el modelo seleccionado manualmente.",
    },
    "hierarchy.enabled": {
        "en": "Enable automatic task routing",
        "fr": "Activer le routage automatique des tâches",
        "zh": "启用自动任务路由",
        "ja": "自動タスクルーティングを有効化",
        "de": "Automatisches Aufgaben-Routing aktivieren",
        "es": "Activar el enrutamiento automático de tareas",
    },
    # — Section 4: Live API (voice-to-voice) (Bloc 4) —
    "settings.liveapi": {
        "en": "Live API",
        "fr": "Live API",
        "zh": "实时 API",
        "ja": "Live API",
        "de": "Live-API",
        "es": "Live API",
    },
    "settings.liveapi_sub": {
        "en": "Native voice-to-voice — bypasses Whisper and TTS.",
        "fr": "Voix-à-voix native — contourne Whisper et la synthèse vocale.",
        "zh": "原生语音对语音 — 绕过 Whisper 和语音合成。",
        "ja": "ネイティブな音声対音声 — Whisper と音声合成をバイパスします。",
        "de": "Natives Sprache-zu-Sprache — umgeht Whisper und TTS.",
        "es": "Voz a voz nativa — omite Whisper y la síntesis de voz.",
    },
    "liveapi.enabled": {
        "en": "Enable Live API voice-to-voice",
        "fr": "Activer la Live API voix-à-voix",
        "zh": "启用实时 API 语音对语音",
        "ja": "Live API 音声対音声を有効化",
        "de": "Live-API Sprache-zu-Sprache aktivieren",
        "es": "Activar Live API de voz a voz",
    },
    "liveapi.notice": {
        "en": "Live API mode: STT (Whisper) and Voice Providers (TTS) are "
              "automatically disabled when Live API is active.",
        "fr": "Mode Live API : la transcription (Whisper) et les fournisseurs "
              "vocaux (synthèse) sont automatiquement désactivés lorsque la "
              "Live API est active.",
        "zh": "实时 API 模式：当实时 API 处于活动状态时，语音转文字（Whisper）和"
              "语音提供商（TTS）会自动禁用。",
        "ja": "Live API モード：Live API が有効な場合、音声認識（Whisper）と音声"
              "プロバイダー（TTS）は自動的に無効になります。",
        "de": "Live-API-Modus: Spracherkennung (Whisper) und Sprachanbieter "
              "(TTS) werden automatisch deaktiviert, wenn die Live-API aktiv ist.",
        "es": "Modo Live API: la transcripción (Whisper) y los proveedores de "
              "voz (TTS) se desactivan automáticamente cuando la Live API está "
              "activa.",
    },
    # — Scheduled Tasks / Cron (Bloc 5) —
    "settings.cron": {
        "en": "Scheduled Tasks",
        "fr": "Tâches planifiées",
        "zh": "定时任务",
        "ja": "スケジュールされたタスク",
        "de": "Geplante Aufgaben",
        "es": "Tareas programadas",
    },
    "settings.cron_sub": {
        "en": "Run a task automatically on a schedule. OSIA executes the "
              "description like a spoken command.",
        "fr": "Exécuter une tâche automatiquement selon un horaire. OSIA "
              "exécute la description comme une commande vocale.",
        "zh": "按计划自动运行任务。OSIA 会像执行语音命令一样执行该描述。",
        "ja": "スケジュールに沿ってタスクを自動実行します。OSIA は説明を音声コマンド"
              "のように実行します。",
        "de": "Eine Aufgabe automatisch nach einem Zeitplan ausführen. OSIA "
              "führt die Beschreibung wie einen Sprachbefehl aus.",
        "es": "Ejecuta una tarea automáticamente según un horario. OSIA ejecuta "
              "la descripción como un comando de voz.",
    },
    "cron.add": {
        "en": "Add task",
        "fr": "Ajouter une tâche",
        "zh": "添加任务",
        "ja": "タスクを追加",
        "de": "Aufgabe hinzufügen",
        "es": "Añadir tarea",
    },
    "cron.task_placeholder": {
        "en": "What should OSIA do?",
        "fr": "Que doit faire OSIA ?",
        "zh": "OSIA 应该做什么？",
        "ja": "OSIA に何をさせますか？",
        "de": "Was soll OSIA tun?",
        "es": "¿Qué debe hacer OSIA?",
    },
    "cron.empty": {
        "en": "No scheduled tasks.",
        "fr": "Aucune tâche planifiée.",
        "zh": "暂无定时任务。",
        "ja": "スケジュールされたタスクはありません。",
        "de": "Keine geplanten Aufgaben.",
        "es": "No hay tareas programadas.",
    },
    "cron.every_minutes": {
        "en": "Every … minutes",
        "fr": "Toutes les … minutes",
        "zh": "每 … 分钟",
        "ja": "… 分ごと",
        "de": "Alle … Minuten",
        "es": "Cada … minutos",
    },
    "cron.every_hours": {
        "en": "Every … hours",
        "fr": "Toutes les … heures",
        "zh": "每 … 小时",
        "ja": "… 時間ごと",
        "de": "Alle … Stunden",
        "es": "Cada … horas",
    },
    "cron.every_days": {
        "en": "Every … days",
        "fr": "Tous les … jours",
        "zh": "每 … 天",
        "ja": "… 日ごと",
        "de": "Alle … Tage",
        "es": "Cada … días",
    },
    "cron.daily_at": {
        "en": "Daily at …",
        "fr": "Chaque jour à …",
        "zh": "每天 …",
        "ja": "毎日 …",
        "de": "Täglich um …",
        "es": "Cada día a las …",
    },
    # Conversation association for scheduled tasks
    "cron.conversation": {
        "en": "Conversation",
        "fr": "Conversation",
        "zh": "对话",
        "ja": "会話",
        "de": "Unterhaltung",
        "es": "Conversación",
    },
    "cron.conversation_none": {
        "en": "None",
        "fr": "Aucune",
        "zh": "无",
        "ja": "なし",
        "de": "Keine",
        "es": "Ninguna",
    },
    "cron.conversation_dialog_title": {
        "en": "Link a conversation",
        "fr": "Associer une conversation",
        "zh": "关联对话",
        "ja": "会話をリンク",
        "de": "Unterhaltung verknüpfen",
        "es": "Vincular una conversación",
    },
    "cron.conversation_dialog_sub": {
        "en": "Pick a conversation to keep context across runs, or none to "
              "start fresh each time.",
        "fr": "Choisissez une conversation pour conserver le contexte entre les "
              "exécutions, ou aucune pour repartir de zéro à chaque fois.",
        "zh": "选择一个对话以在多次运行间保留上下文，或选择「无」以每次重新开始。",
        "ja": "実行間でコンテキストを保持する会話を選択するか、「なし」で毎回最初から"
              "開始します。",
        "de": "Wählen Sie eine Unterhaltung, um den Kontext über Läufe hinweg zu "
              "behalten, oder keine, um jedes Mal neu zu beginnen.",
        "es": "Elige una conversación para mantener el contexto entre ejecuciones, "
              "o ninguna para empezar de cero cada vez.",
    },
    "cron.conversation_none_option": {
        "en": "None (new conversation each time)",
        "fr": "Aucune (nouvelle conversation à chaque fois)",
        "zh": "无（每次新建对话）",
        "ja": "なし（毎回新しい会話）",
        "de": "Keine (jedes Mal neue Unterhaltung)",
        "es": "Ninguna (conversación nueva cada vez)",
    },
    "cron.conversation_empty": {
        "en": "No conversations yet.",
        "fr": "Aucune conversation pour l'instant.",
        "zh": "暂无对话。",
        "ja": "まだ会話がありません。",
        "de": "Noch keine Unterhaltungen.",
        "es": "Aún no hay conversaciones.",
    },
    # — Heartbeat / system monitoring (Bloc 5) —
    "settings.heartbeat": {
        "en": "Heartbeat",
        "fr": "Surveillance système",
        "zh": "系统监控",
        "ja": "システム監視",
        "de": "Systemüberwachung",
        "es": "Monitor del sistema",
    },
    "settings.heartbeat_sub": {
        "en": "Monitor system health and speak an alert when a metric crosses "
              "its threshold.",
        "fr": "Surveille l'état du système et émet une alerte vocale lorsqu'une "
              "métrique dépasse son seuil.",
        "zh": "监控系统状态，当某项指标超过阈值时发出语音警报。",
        "ja": "システムの状態を監視し、指標がしきい値を超えたら音声で警告します。",
        "de": "Überwacht den Systemzustand und gibt eine Sprachwarnung aus, wenn "
              "ein Messwert seinen Schwellenwert überschreitet.",
        "es": "Supervisa el estado del sistema y emite una alerta de voz cuando "
              "una métrica supera su umbral.",
    },
    "heartbeat.enabled": {
        "en": "Enable system monitoring",
        "fr": "Activer la surveillance système",
        "zh": "启用系统监控",
        "ja": "システム監視を有効化",
        "de": "Systemüberwachung aktivieren",
        "es": "Activar la monitorización del sistema",
    },
    "heartbeat.interval": {
        "en": "Check interval (minutes)",
        "fr": "Intervalle de vérification (minutes)",
        "zh": "检查间隔（分钟）",
        "ja": "チェック間隔（分）",
        "de": "Prüfintervall (Minuten)",
        "es": "Intervalo de comprobación (minutos)",
    },
    "heartbeat.check_cpu": {
        "en": "CPU usage",
        "fr": "Utilisation CPU",
        "zh": "CPU 使用率",
        "ja": "CPU 使用率",
        "de": "CPU-Auslastung",
        "es": "Uso de CPU",
    },
    "heartbeat.check_ram": {
        "en": "RAM usage",
        "fr": "Utilisation RAM",
        "zh": "内存使用率",
        "ja": "RAM 使用率",
        "de": "RAM-Auslastung",
        "es": "Uso de RAM",
    },
    "heartbeat.check_disk": {
        "en": "Disk space",
        "fr": "Espace disque",
        "zh": "磁盘空间",
        "ja": "ディスク容量",
        "de": "Speicherplatz",
        "es": "Espacio en disco",
    },
    "heartbeat.check_network": {
        "en": "Internet connectivity",
        "fr": "Connectivité Internet",
        "zh": "网络连接",
        "ja": "インターネット接続",
        "de": "Internetverbindung",
        "es": "Conectividad a Internet",
    },
    "heartbeat.hint_usage": {
        "en": "Alert when usage exceeds this %.",
        "fr": "Alerte lorsque l'utilisation dépasse ce %.",
        "zh": "使用率超过此百分比时发出警报。",
        "ja": "使用率がこの％を超えたら警告します。",
        "de": "Warnen, wenn die Auslastung diesen % übersteigt.",
        "es": "Alertar cuando el uso supere este %.",
    },
    "heartbeat.hint_disk": {
        "en": "Alert when free space falls below this %.",
        "fr": "Alerte lorsque l'espace libre passe sous ce %.",
        "zh": "可用空间低于此百分比时发出警报。",
        "ja": "空き容量がこの％を下回ったら警告します。",
        "de": "Warnen, wenn der freie Speicher unter diesen % fällt.",
        "es": "Alertar cuando el espacio libre baje de este %.",
    },
    "heartbeat.hint_network": {
        "en": "Alert when the connectivity check fails.",
        "fr": "Alerte lorsque le test de connexion échoue.",
        "zh": "连接检测失败时发出警报。",
        "ja": "接続チェックに失敗したら警告します。",
        "de": "Warnen, wenn die Verbindungsprüfung fehlschlägt.",
        "es": "Alertar cuando falle la comprobación de conexión.",
    },
    # Spoken alert messages (TTS) — {value} is the measured percentage.
    "heartbeat.alert_cpu": {
        "en": "Alert: CPU usage is high, at {value} percent.",
        "fr": "Alerte : l'utilisation du processeur est élevée, à {value} pour cent.",
        "zh": "警报：CPU 使用率过高，已达 {value}%。",
        "ja": "警告：CPU 使用率が高く、{value}％です。",
        "de": "Warnung: Die CPU-Auslastung ist hoch, bei {value} Prozent.",
        "es": "Alerta: el uso de CPU es alto, al {value} por ciento.",
    },
    "heartbeat.alert_ram": {
        "en": "Alert: memory usage is high, at {value} percent.",
        "fr": "Alerte : l'utilisation de la mémoire est élevée, à {value} pour cent.",
        "zh": "警报：内存使用率过高，已达 {value}%。",
        "ja": "警告：メモリ使用率が高く、{value}％です。",
        "de": "Warnung: Die Speicherauslastung ist hoch, bei {value} Prozent.",
        "es": "Alerta: el uso de memoria es alto, al {value} por ciento.",
    },
    "heartbeat.alert_disk": {
        "en": "Alert: disk space is low, {value} percent free.",
        "fr": "Alerte : l'espace disque est faible, {value} pour cent disponible.",
        "zh": "警报：磁盘空间不足，仅剩 {value}% 可用。",
        "ja": "警告：ディスク容量が少なく、空きは {value}％です。",
        "de": "Warnung: Wenig Speicherplatz, {value} Prozent frei.",
        "es": "Alerta: queda poco espacio en disco, {value} por ciento libre.",
    },
    "heartbeat.alert_network": {
        "en": "Alert: internet connectivity appears to be down.",
        "fr": "Alerte : la connexion Internet semble interrompue.",
        "zh": "警报：互联网连接似乎已中断。",
        "ja": "警告：インターネット接続が切断されているようです。",
        "de": "Warnung: Die Internetverbindung scheint unterbrochen zu sein.",
        "es": "Alerta: la conexión a Internet parece estar caída.",
    },
    # — API endpoint fallback (Bloc 5) —
    "settings.api_fallback": {
        "en": "API Fallback",
        "fr": "Secours API",
        "zh": "API 备用端点",
        "ja": "API フォールバック",
        "de": "API-Fallback",
        "es": "Respaldo de API",
    },
    "settings.api_fallback_sub": {
        "en": "Alternate API endpoints. If a provider's primary endpoint fails "
              "to connect, OSIA retries the request on its fallback URL.",
        "fr": "Points d'accès API alternatifs. Si le point d'accès principal "
              "d'un fournisseur ne répond pas, OSIA réessaie la requête sur "
              "l'URL de secours.",
        "zh": "备用 API 端点。如果某个提供商的主端点无法连接，OSIA 会在其备用 URL "
              "上重试请求。",
        "ja": "代替 API エンドポイント。プロバイダーの主エンドポイントに接続できない"
              "場合、OSIA はフォールバック URL でリクエストを再試行します。",
        "de": "Alternative API-Endpunkte. Wenn der primäre Endpunkt eines "
              "Anbieters keine Verbindung herstellt, wiederholt OSIA die "
              "Anfrage über die Fallback-URL.",
        "es": "Endpoints de API alternativos. Si el endpoint principal de un "
              "proveedor no conecta, OSIA reintenta la solicitud en su URL de "
              "respaldo.",
    },
    "api_fallback.primary": {
        "en": "Primary URL",
        "fr": "URL principale",
        "zh": "主 URL",
        "ja": "プライマリ URL",
        "de": "Primäre URL",
        "es": "URL principal",
    },
    "api_fallback.fallback": {
        "en": "Fallback URL",
        "fr": "URL de secours",
        "zh": "备用 URL",
        "ja": "フォールバック URL",
        "de": "Fallback-URL",
        "es": "URL de respaldo",
    },
    "api_fallback.fallback_placeholder": {
        "en": "Optional fallback base URL",
        "fr": "URL de base de secours (optionnel)",
        "zh": "可选的备用基础 URL",
        "ja": "任意のフォールバック ベース URL",
        "de": "Optionale Fallback-Basis-URL",
        "es": "URL base de respaldo (opcional)",
    },
    # — API key fallback hierarchy —
    "settings.api_keys": {
        "en": "API Key Fallback",
        "fr": "Clés API de secours",
        "zh": "API 密钥备用",
        "ja": "API キーのフォールバック",
        "de": "API-Schlüssel-Fallback",
        "es": "Respaldo de claves API",
    },
    "settings.api_keys_sub": {
        "en": "Add backup API keys per provider. If a key hits a rate limit or "
              "auth error, OSIA tries the next key before switching models.",
        "fr": "Ajoutez des clés API de secours par fournisseur. Si une clé "
              "atteint une limite de débit ou une erreur d'authentification, "
              "OSIA essaie la clé suivante avant de changer de modèle.",
        "zh": "为每个提供商添加备用 API 密钥。当某个密钥触发速率限制或认证错误时，"
              "OSIA 会先尝试下一个密钥，然后才切换模型。",
        "ja": "プロバイダーごとに予備の API キーを追加します。キーがレート制限や認証"
              "エラーに達すると、OSIA はモデルを切り替える前に次のキーを試します。",
        "de": "Fügen Sie pro Anbieter Ersatz-API-Schlüssel hinzu. Erreicht ein "
              "Schlüssel ein Ratenlimit oder einen Authentifizierungsfehler, "
              "versucht OSIA den nächsten Schlüssel, bevor das Modell gewechselt "
              "wird.",
        "es": "Añade claves API de respaldo por proveedor. Si una clave alcanza "
              "un límite de tasa o un error de autenticación, OSIA prueba la "
              "siguiente clave antes de cambiar de modelo.",
    },
    "api_keys.select_provider": {
        "en": "Select a provider to manage its keys…",
        "fr": "Choisissez un fournisseur pour gérer ses clés…",
        "zh": "选择一个提供商以管理其密钥…",
        "ja": "キーを管理するプロバイダーを選択…",
        "de": "Anbieter auswählen, um seine Schlüssel zu verwalten…",
        "es": "Elige un proveedor para gestionar sus claves…",
    },
    "api_keys.no_providers": {
        "en": "No providers configured yet — add an API key above first.",
        "fr": "Aucun fournisseur configuré — ajoutez d'abord une clé API ci-dessus.",
        "zh": "尚未配置任何提供商——请先在上方添加 API 密钥。",
        "ja": "設定済みのプロバイダーがありません。まず上で API キーを追加してください。",
        "de": "Noch keine Anbieter konfiguriert — fügen Sie zuerst oben einen "
              "API-Schlüssel hinzu.",
        "es": "Aún no hay proveedores configurados: añade primero una clave API "
              "arriba.",
    },
    "api_keys.dialog_title": {
        "en": "{name} API keys",
        "fr": "Clés API {name}",
        "zh": "{name} API 密钥",
        "ja": "{name} API キー",
        "de": "{name} API-Schlüssel",
        "es": "Claves API de {name}",
    },
    "api_keys.dialog_sub": {
        "en": "The first key is primary. The others are tried in order when it "
              "fails with a rate limit or auth error.",
        "fr": "La première clé est principale. Les autres sont essayées dans "
              "l'ordre en cas de limite de débit ou d'erreur d'authentification.",
        "zh": "第一个密钥为主密钥。当其触发速率限制或认证错误时，将按顺序尝试其余密钥。",
        "ja": "最初のキーが主キーです。レート制限や認証エラーで失敗すると、残りのキーが"
              "順番に試されます。",
        "de": "Der erste Schlüssel ist primär. Die übrigen werden der Reihe nach "
              "versucht, wenn er mit einem Ratenlimit oder Authentifizierungs"
              "fehler fehlschlägt.",
        "es": "La primera clave es la principal. Las demás se prueban en orden "
              "cuando falla por límite de tasa o error de autenticación.",
    },
    "api_keys.primary": {
        "en": "Primary",
        "fr": "Principale",
        "zh": "主密钥",
        "ja": "主キー",
        "de": "Primär",
        "es": "Principal",
    },
    "api_keys.fallback_n": {
        "en": "Fallback {n}",
        "fr": "Secours {n}",
        "zh": "备用 {n}",
        "ja": "予備 {n}",
        "de": "Fallback {n}",
        "es": "Respaldo {n}",
    },
    "api_keys.key_placeholder": {
        "en": "API key",
        "fr": "Clé API",
        "zh": "API 密钥",
        "ja": "API キー",
        "de": "API-Schlüssel",
        "es": "Clave API",
    },
    "api_keys.add": {
        "en": "Add key",
        "fr": "Ajouter une clé",
        "zh": "添加密钥",
        "ja": "キーを追加",
        "de": "Schlüssel hinzufügen",
        "es": "Añadir clave",
    },
}


# ── Active language state ────────────────────────────────────────────────

_current_lang: str | None = None
_listeners: list[Callable[[], None]] = []


def _load_lang_from_settings() -> str:
    try:
        from osia.config import get_setting
        lang = get_setting("language", DEFAULT_LANG)
    except Exception:
        lang = DEFAULT_LANG
    return lang if lang in LANGUAGES else DEFAULT_LANG


def get_language() -> str:
    """Return the active language code, loading it from settings on first use."""
    global _current_lang
    if _current_lang is None:
        _current_lang = _load_lang_from_settings()
    return _current_lang


def set_language(lang: str, persist: bool = True) -> None:
    """Switch the active language. Persists to settings and notifies listeners
    so the UI can re-translate in place. Unknown codes are ignored."""
    global _current_lang
    if lang not in LANGUAGES:
        return
    _current_lang = lang
    if persist:
        try:
            from osia.config import set_setting
            set_setting("language", lang)
        except Exception:
            pass
    for cb in list(_listeners):
        try:
            cb()
        except Exception:
            pass


def available_languages() -> dict[str, str]:
    """Return the {code: native name} map of supported languages."""
    return dict(LANGUAGES)


def t(key: str, **kwargs) -> str:
    """Translate ``key`` into the active language.

    Falls back to English, then to the key itself if untranslated. Any
    ``kwargs`` are applied with ``str.format`` so placeholders like
    ``{name}`` are filled in.
    """
    lang = get_language()
    entry = TRANSLATIONS.get(key)
    if entry is None:
        text = key
    else:
        text = entry.get(lang) or entry.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text


def add_listener(callback: Callable[[], None]) -> None:
    """Register a callback invoked after every :func:`set_language` change."""
    if callback not in _listeners:
        _listeners.append(callback)


def remove_listener(callback: Callable[[], None]) -> None:
    if callback in _listeners:
        _listeners.remove(callback)
