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

Migration status: the PC GUI popup is fully migrated — every user-facing
string in ``osia/gui/main_popup.py`` and ``osia/gui/dynamic_island.py``
(chat page, conversations panel, model picker, settings sections, the
startup section, and the bottom-nav tooltips) is routed through ``t()``.
Proper nouns (the "OSIA" wordmark, "Kokoro", provider/product names) are
shown verbatim by design. Any remaining legacy strings elsewhere are
tracked for a follow-up pass in ``docs/i18n_backlog.md``.
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
    "settings.language_desc": {
        "en": "Sets the language of every menu and spoken reply, plus how clocks are shown. Pick your language and time format — the interface updates instantly.",
        "fr": "Définit la langue de tous les menus et des réponses vocales, ainsi que l'affichage de l'heure. Choisissez votre langue et votre format horaire — l'interface se met à jour aussitôt.",
        "zh": "设置所有菜单和语音回复的语言，以及时间的显示方式。选择您的语言和时间格式 — 界面会立即更新。",
        "ja": "すべてのメニューと音声応答の言語、および時刻の表示方法を設定します。言語と時刻形式を選ぶと、インターフェースが即座に更新されます。",
        "de": "Legt die Sprache aller Menüs und gesprochenen Antworten sowie die Uhrzeitanzeige fest. Wählen Sie Ihre Sprache und Ihr Zeitformat — die Oberfläche wird sofort aktualisiert.",
        "es": "Define el idioma de todos los menús y las respuestas habladas, además de cómo se muestra la hora. Elige tu idioma y formato de hora — la interfaz se actualiza al instante.",
    },
    "settings.time_format": {
        "en": "Time format",
        "fr": "Format de l'heure",
        "zh": "时间格式",
        "ja": "時刻の形式",
        "de": "Zeitformat",
        "es": "Formato de hora",
    },
    "settings.time_format_24h": {
        "en": "24-hour (14:30)",
        "fr": "24 heures (14:30)",
        "zh": "24 小时制（14:30）",
        "ja": "24時間表示（14:30）",
        "de": "24-Stunden (14:30)",
        "es": "24 horas (14:30)",
    },
    "settings.time_format_12h": {
        "en": "12-hour (2:30 PM)",
        "fr": "12 heures (2:30 PM)",
        "zh": "12 小时制（2:30 PM）",
        "ja": "12時間表示（2:30 PM）",
        "de": "12-Stunden (2:30 PM)",
        "es": "12 horas (2:30 PM)",
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
    "settings.ai_providers_desc": {
        "en": "These are the language models that power OSIA's conversation, reasoning, and tool use. Choose your main provider and paste its API key; add a paired vision model if your main model can't read images.",
        "fr": "Ce sont les modèles de langage qui alimentent la conversation, le raisonnement et l'utilisation d'outils d'OSIA. Choisissez votre fournisseur principal et collez sa clé API ; ajoutez un modèle vision associé si votre modèle principal ne lit pas les images.",
        "zh": "这些是为 OSIA 的对话、推理和工具使用提供动力的语言模型。选择您的主要提供商并粘贴其 API 密钥；如果主模型无法处理图像，请添加一个配套的视觉模型。",
        "ja": "これらは OSIA の会話・推論・ツール利用を支える言語モデルです。メインプロバイダーを選んで API キーを貼り付けてください。メインモデルが画像を読めない場合は、連携ビジョンモデルを追加します。",
        "de": "Dies sind die Sprachmodelle, die OSIAs Konversation, Schlussfolgern und Werkzeugnutzung antreiben. Wählen Sie Ihren Hauptanbieter und fügen Sie dessen API-Schlüssel ein; ergänzen Sie ein verknüpftes Vision-Modell, falls Ihr Hauptmodell keine Bilder lesen kann.",
        "es": "Estos son los modelos de lenguaje que impulsan la conversación, el razonamiento y el uso de herramientas de OSIA. Elige tu proveedor principal y pega su clave API; añade un modelo de visión asociado si tu modelo principal no puede leer imágenes.",
    },
    # — Vision companion model (WOR-25): shown when the main model can't see —
    "settings.vision_model": {
        "en": "Paired vision model",
        "fr": "Modèle vision associé",
        "zh": "配套视觉模型",
        "ja": "連携ビジョンモデル",
        "de": "Verknüpftes Vision-Modell",
        "es": "Modelo de visión asociado",
    },
    "settings.vision_hint": {
        "en": "Use this if your main model cannot process images (e.g. DeepSeek, "
              "Groq, Llama, Codestral).",
        "fr": "Utile si votre modèle principal ne gère pas les images (ex: "
              "DeepSeek, Groq, Llama, Codestral).",
        "zh": "如果您的主模型无法处理图像（例如 DeepSeek、Groq、Llama、Codestral），请使用此选项。",
        "ja": "メインモデルが画像を処理できない場合に使用します（例：DeepSeek、Groq、Llama、Codestral）。",
        "de": "Nützlich, wenn Ihr Hauptmodell keine Bilder verarbeiten kann "
              "(z. B. DeepSeek, Groq, Llama, Codestral).",
        "es": "Útil si tu modelo principal no puede procesar imágenes (p. ej., "
              "DeepSeek, Groq, Llama, Codestral).",
    },
    "settings.vision_provider": {
        "en": "Vision provider",
        "fr": "Fournisseur vision",
        "zh": "视觉提供商",
        "ja": "ビジョンプロバイダー",
        "de": "Vision-Anbieter",
        "es": "Proveedor de visión",
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
    "settings.voice_providers_desc": {
        "en": "This controls the voice OSIA uses to speak back to you. Local (Kokoro) is free, private, and needs no key; OpenAI and Google offer more natural cloud voices but require an API key.",
        "fr": "Ceci contrôle la voix qu'OSIA utilise pour vous répondre. Local (Kokoro) est gratuit, confidentiel et ne nécessite aucune clé ; OpenAI et Google proposent des voix cloud plus naturelles mais demandent une clé API.",
        "zh": "这里控制 OSIA 回复时使用的语音。本地 (Kokoro) 免费、私密且无需密钥；OpenAI 和 Google 提供更自然的云端语音，但需要 API 密钥。",
        "ja": "OSIA が返答に使う音声を選びます。ローカル (Kokoro) は無料でプライベート、キーも不要です。OpenAI と Google はより自然なクラウド音声を提供しますが、API キーが必要です。",
        "de": "Hier legen Sie die Stimme fest, mit der OSIA antwortet. Lokal (Kokoro) ist kostenlos, privat und benötigt keinen Schlüssel; OpenAI und Google bieten natürlichere Cloud-Stimmen, erfordern aber einen API-Schlüssel.",
        "es": "Esto controla la voz que OSIA usa para responderte. Local (Kokoro) es gratis, privada y no necesita clave; OpenAI y Google ofrecen voces en la nube más naturales pero requieren una clave API.",
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
    "settings.whisper_providers_desc": {
        "en": "This decides how OSIA turns your speech into text. Local runs free and offline; Groq is faster in the cloud but needs its key — set a fallback so transcription keeps working if one fails.",
        "fr": "Ceci détermine comment OSIA transforme votre parole en texte. Local fonctionne gratuitement et hors ligne ; Groq est plus rapide dans le cloud mais nécessite sa clé — définissez un secours pour que la transcription continue si l'un échoue.",
        "zh": "这里决定 OSIA 如何将您的语音转换为文字。本地免费且可离线运行；Groq 在云端速度更快但需要密钥 — 设置备用提供商以便其中一个失败时转录能继续进行。",
        "ja": "OSIA が音声をどのように文字へ変換するかを決めます。ローカルは無料でオフラインでも動作し、Groq はクラウドで高速ですがキーが必要です。どちらかが失敗しても文字起こしが続くよう、フォールバックを設定してください。",
        "de": "Hier wird festgelegt, wie OSIA Ihre Sprache in Text umwandelt. Lokal läuft kostenlos und offline; Groq ist in der Cloud schneller, benötigt aber seinen Schlüssel — legen Sie ein Ausweichsystem fest, damit die Transkription weiterläuft, wenn einer ausfällt.",
        "es": "Esto decide cómo OSIA convierte tu voz en texto. Local funciona gratis y sin conexión; Groq es más rápido en la nube pero necesita su clave — define un respaldo para que la transcripción siga funcionando si uno falla.",
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
    "settings.mobile_desc": {
        "en": "This token pairs the OSIA mobile app with this computer so you can talk to OSIA from your phone. Treat it like a password; tap Show to pair, or Regenerate to revoke it everywhere if it may have leaked.",
        "fr": "Ce jeton associe l'application mobile OSIA à cet ordinateur pour que vous puissiez parler à OSIA depuis votre téléphone. Traitez-le comme un mot de passe ; appuyez sur Afficher pour l'appairer, ou sur Régénérer pour le révoquer partout s'il a pu être compromis.",
        "zh": "此令牌将 OSIA 移动应用与这台电脑配对，让您可以通过手机与 OSIA 对话。请像对待密码一样对待它；点击「显示」进行配对，或点击「重新生成」以在可能泄露时在所有设备上吊销它。",
        "ja": "このトークンは OSIA モバイルアプリをこのコンピューターと連携させ、スマートフォンから OSIA と話せるようにします。パスワードのように扱ってください。「表示」をタップしてペアリングし、漏えいの恐れがある場合は「再生成」ですべての端末から無効化できます。",
        "de": "Dieses Token koppelt die OSIA-Mobil-App mit diesem Computer, damit Sie vom Telefon aus mit OSIA sprechen können. Behandeln Sie es wie ein Passwort; tippen Sie auf Anzeigen zum Koppeln oder auf Neu generieren, um es überall zu widerrufen, falls es durchgesickert sein könnte.",
        "es": "Este token vincula la app móvil de OSIA con esta computadora para que puedas hablar con OSIA desde tu teléfono. Trátalo como una contraseña; toca Mostrar para emparejar, o Regenerar para revocarlo en todas partes si pudo filtrarse.",
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
    "settings.ai_hierarchy_desc": {
        "en": "OSIA can route each task down an ordered list of models, falling back to the next one on a technical error like a rate limit or outage. Leave it off to use a single model, or turn it on for resilience across the Heavy and Medium tiers.",
        "fr": "OSIA peut acheminer chaque tâche le long d'une liste ordonnée de modèles, en passant au suivant en cas d'erreur technique comme une limite de débit ou une panne. Laissez-le désactivé pour n'utiliser qu'un seul modèle, ou activez-le pour plus de robustesse sur les niveaux Heavy et Medium.",
        "zh": "OSIA 可以按顺序尝试多个模型执行每项任务，在遇到限流或宕机等技术错误时自动切换到下一个。不需要时可保持关闭，或启用它以在 Heavy 和 Medium 两个层级获得更强的容错能力。",
        "ja": "OSIA は各タスクを順序付きのモデルリストに沿って処理し、レート制限や障害などの技術的エラー時には次のモデルへ切り替えます。単一モデルだけを使う場合はオフのままにし、Heavy と Medium の各階層で耐障害性を高めたい場合はオンにします。",
        "de": "OSIA kann jede Aufgabe durch eine geordnete Liste von Modellen leiten und bei einem technischen Fehler wie einem Ratenlimit oder Ausfall zum nächsten wechseln. Lassen Sie es aus, um ein einziges Modell zu nutzen, oder aktivieren Sie es für erhöhte Ausfallsicherheit über die Stufen Heavy und Medium.",
        "es": "OSIA puede encaminar cada tarea por una lista ordenada de modelos, pasando al siguiente ante un error técnico como un límite de tasa o una caída. Déjalo desactivado para usar un único modelo, o actívalo para mayor resiliencia en los niveles Heavy y Medium.",
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
    "settings.liveapi_desc": {
        "en": "Live API is a real-time, speech-to-speech mode where the model listens and speaks directly, skipping separate transcription and synthesis. It feels more natural but uses more API quota, and your STT and TTS settings are bypassed while it's on.",
        "fr": "Live API est un mode voix-à-voix en temps réel où le modèle écoute et parle directement, sans étape distincte de transcription ni de synthèse. C'est plus naturel mais cela consomme davantage de quota d'API, et vos réglages STT et TTS sont contournés lorsqu'il est actif.",
        "zh": "Live API 是一种实时的语音对语音模式，模型直接聆听和说话，跳过单独的转录与合成步骤。它更自然，但会消耗更多 API 配额，启用时您的 STT 和 TTS 设置将被绕过。",
        "ja": "Live API はリアルタイムの音声対音声モードで、モデルが直接聞いて話し、別個の文字起こしや音声合成を省きます。より自然に感じられますが API の使用量は増え、有効な間は STT と TTS の設定はバイパスされます。",
        "de": "Live-API ist ein Echtzeit-Sprache-zu-Sprache-Modus, in dem das Modell direkt zuhört und spricht und separate Transkription und Synthese überspringt. Das wirkt natürlicher, verbraucht aber mehr API-Kontingent, und Ihre STT- und TTS-Einstellungen werden währenddessen umgangen.",
        "es": "Live API es un modo de voz a voz en tiempo real en el que el modelo escucha y habla directamente, omitiendo la transcripción y la síntesis por separado. Resulta más natural pero consume más cuota de API, y tus ajustes de STT y TTS se omiten mientras está activo.",
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
    # — Wake-word greeting response —
    "settings.wake_word_enabled": {
        "en": "Enable wake word",
        "fr": "Activer le wake word",
        "zh": "启用唤醒词",
        "ja": "ウェイクワードを有効にする",
        "de": "Aktivierungswort aktivieren",
        "es": "Activar la palabra de activación",
    },
    "settings.wake_greeting": {
        "en": "Wake word response",
        "fr": "Réponse au mot d'activation",
        "zh": "唤醒词回应",
        "ja": "ウェイクワード応答",
        "de": "Aktivierungswort-Antwort",
        "es": "Respuesta a la palabra de activación",
    },
    "settings.wake_greeting_sub": {
        "en": "Speak a short greeting when you say “OSIA”, before listening.",
        "fr": "Dire une courte phrase quand vous dites « OSIA », avant d'écouter.",
        "zh": "当您说「OSIA」时，先说一句简短的问候语，然后再开始聆听。",
        "ja": "「OSIA」と言ったとき、聞き取りを始める前に短い挨拶を話します。",
        "de": "Eine kurze Begrüßung sprechen, wenn Sie „OSIA“ sagen, bevor "
              "zugehört wird.",
        "es": "Decir un breve saludo cuando dices «OSIA», antes de escuchar.",
    },
    "settings.wake_greeting_desc": {
        "en": "OSIA can listen in the background for the wake word “OSIA” so you can start hands-free. When this is on, it speaks a short greeting to confirm it heard you before listening — write your own or use the default.",
        "fr": "OSIA peut écouter en arrière-plan le mot d'activation « OSIA » pour que vous puissiez démarrer sans les mains. Lorsque c'est activé, il prononce une courte phrase pour confirmer qu'il vous a entendu avant d'écouter — rédigez la vôtre ou gardez celle par défaut.",
        "zh": "OSIA 可以在后台聆听唤醒词「OSIA」，让您无需动手即可开始。开启后，它会先说一句简短的问候以确认听到了您，然后再开始聆听 — 您可以自定义内容或使用默认值。",
        "ja": "OSIA はウェイクワード「OSIA」をバックグラウンドで聞き取り、手を使わずに始められます。オンにすると、聞き取りを始める前に短い挨拶を話して聞こえたことを知らせます — 自分で文章を書くか、デフォルトを使えます。",
        "de": "OSIA kann im Hintergrund auf das Aktivierungswort „OSIA“ hören, damit Sie freihändig starten können. Wenn dies aktiviert ist, spricht es eine kurze Begrüßung, um zu bestätigen, dass es Sie gehört hat, bevor es zuhört — schreiben Sie eine eigene oder nutzen Sie die Standardvorgabe.",
        "es": "OSIA puede escuchar en segundo plano la palabra de activación «OSIA» para que empieces sin usar las manos. Cuando está activado, dice un breve saludo para confirmar que te oyó antes de escuchar — escribe el tuyo o usa el predeterminado.",
    },
    "wake.greeting_enabled": {
        "en": "Speak greeting on wake",
        "fr": "Dire un message à l'activation",
        "zh": "唤醒时播报问候语",
        "ja": "起動時に挨拶を話す",
        "de": "Begrüßung beim Aktivieren sprechen",
        "es": "Decir saludo al activar",
    },
    "wake.greeting_placeholder": {
        "en": "Greeting text",
        "fr": "Texte du message",
        "zh": "问候语文本",
        "ja": "挨拶のテキスト",
        "de": "Begrüßungstext",
        "es": "Texto del saludo",
    },
    # Default greeting, auto-filled into the text field per the active language.
    "wake.greeting_default": {
        "en": "I'm listening",
        "fr": "Je vous écoute",
        "zh": "我在听",
        "ja": "お聞きしています",
        "de": "Ich höre zu",
        "es": "Te escucho",
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
    "settings.cron_desc": {
        "en": "Schedule OSIA to run a task on its own — write it in plain language and OSIA executes it like a spoken command. Choose a repeating interval or a one-time date, and link each task to a conversation if you like.",
        "fr": "Planifiez OSIA pour qu'il exécute une tâche de lui-même — rédigez-la en langage courant et OSIA l'exécute comme une commande vocale. Choisissez un intervalle récurrent ou une date unique, et reliez chaque tâche à une conversation si vous le souhaitez.",
        "zh": "安排 OSIA 自动执行任务 — 用日常语言编写任务，OSIA 会像执行语音命令一样执行它。选择重复间隔或一次性日期，也可选择将每个任务关联到某个对话。",
        "ja": "OSIA がタスクを自動で実行するようにスケジュールします — 普段の言葉で書けば、OSIA はそれを音声コマンドのように実行します。繰り返し間隔か一度きりの日時を選び、必要に応じて各タスクを会話に紐づけられます。",
        "de": "Planen Sie, dass OSIA eine Aufgabe selbstständig ausführt — formulieren Sie sie in normaler Sprache, und OSIA führt sie wie einen Sprachbefehl aus. Wählen Sie ein wiederkehrendes Intervall oder ein einmaliges Datum und verknüpfen Sie jede Aufgabe bei Bedarf mit einer Unterhaltung.",
        "es": "Programa a OSIA para que ejecute una tarea por sí solo — escríbela en lenguaje natural y OSIA la ejecuta como un comando de voz. Elige un intervalo repetido o una fecha única, y vincula cada tarea a una conversación si lo deseas.",
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
    # Schedule-type selector per task: a repeating schedule vs a single run.
    "cron.type_repeat": {
        "en": "Repeat",
        "fr": "Répéter",
        "zh": "重复",
        "ja": "繰り返し",
        "de": "Wiederholen",
        "es": "Repetir",
    },
    "cron.type_once": {
        "en": "Once",
        "fr": "Une fois",
        "zh": "一次",
        "ja": "一回",
        "de": "Einmalig",
        "es": "Una vez",
    },
    # Date-field placeholder (mask hint only — the parser always reads
    # day/month/year by position regardless of the localized letters).
    "cron.once_date_placeholder": {
        "en": "DD/MM/YYYY",
        "fr": "JJ/MM/AAAA",
        "zh": "日/月/年",
        "ja": "日/月/年",
        "de": "TT/MM/JJJJ",
        "es": "DD/MM/AAAA",
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
    "settings.heartbeat_desc": {
        "en": "Heartbeat watches your computer's CPU, RAM, disk, and network, and speaks an alert when a metric crosses the threshold you set. Turn it on and pick the metrics to monitor, or leave it off if you don't need background checks.",
        "fr": "La surveillance système suit le processeur, la mémoire, le disque et le réseau de votre ordinateur, et émet une alerte vocale lorsqu'une métrique dépasse le seuil que vous avez fixé. Activez-la et choisissez les métriques à surveiller, ou laissez-la désactivée si vous n'avez pas besoin de vérifications en arrière-plan.",
        "zh": "系统监控会跟踪电脑的 CPU、内存、磁盘和网络，当某项指标超过您设定的阈值时发出语音警报。开启它并选择要监控的指标，如果不需要后台检查，则可保持关闭。",
        "ja": "システム監視はコンピューターの CPU・メモリ・ディスク・ネットワークを監視し、指標が設定したしきい値を超えると音声で警告します。オンにして監視する指標を選んでください。バックグラウンドのチェックが不要ならオフのままにします。",
        "de": "Die Systemüberwachung beobachtet CPU, Arbeitsspeicher, Festplatte und Netzwerk Ihres Computers und gibt eine Sprachwarnung aus, wenn ein Messwert den von Ihnen festgelegten Schwellenwert überschreitet. Schalten Sie sie ein und wählen Sie die zu überwachenden Messwerte, oder lassen Sie sie aus, wenn Sie keine Hintergrundprüfungen benötigen.",
        "es": "El monitor del sistema vigila la CPU, la memoria, el disco y la red de tu computadora, y emite una alerta de voz cuando una métrica supera el umbral que fijaste. Actívalo y elige las métricas a vigilar, o déjalo desactivado si no necesitas comprobaciones en segundo plano.",
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
    "settings.api_keys_desc": {
        "en": "Add several API keys for the same provider so OSIA keeps working when one is rate-limited or rejected. It rotates to the next key automatically before switching models — useful if you juggle multiple or free-tier keys.",
        "fr": "Ajoutez plusieurs clés API pour un même fournisseur afin qu'OSIA continue de fonctionner lorsqu'une clé est limitée ou refusée. Il passe automatiquement à la clé suivante avant de changer de modèle — pratique si vous jonglez avec plusieurs clés ou des clés gratuites.",
        "zh": "为同一提供商添加多个 API 密钥，这样当某个密钥被限流或拒绝时 OSIA 仍能继续工作。它会在切换模型之前自动轮换到下一个密钥 — 当您使用多个密钥或免费额度密钥时很有用。",
        "ja": "同じプロバイダーに複数の API キーを追加しておくと、あるキーがレート制限や拒否に遭っても OSIA は動作を続けられます。モデルを切り替える前に自動的に次のキーへ切り替わるため、複数のキーや無料枠のキーを使い分ける場合に便利です。",
        "de": "Fügen Sie mehrere API-Schlüssel für denselben Anbieter hinzu, damit OSIA weiterarbeitet, wenn einer ein Ratenlimit oder einen Authentifizierungsfehler erreicht. Es wechselt automatisch zum nächsten Schlüssel, bevor das Modell gewechselt wird — praktisch, wenn Sie mehrere oder kostenlose Schlüssel nutzen.",
        "es": "Añade varias claves API para el mismo proveedor para que OSIA siga funcionando cuando una alcanza su límite o es rechazada. Rota automáticamente a la siguiente clave antes de cambiar de modelo, útil si manejas varias claves o claves gratuitas.",
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
    # — OSIA tab (Bloc 6): personality + skills —
    "osia.personality": {
        "en": "Personality",
        "fr": "Personnalité",
        "zh": "个性",
        "ja": "性格",
        "de": "Persönlichkeit",
        "es": "Personalidad",
    },
    "osia.personality_sub": {
        "en": "Define how OSIA behaves and speaks. Prepended to the system "
              "prompt at the start of every conversation.",
        "fr": "Définissez le comportement et le ton d'OSIA. Ajouté au début de "
              "l'invite système au démarrage de chaque conversation.",
        "zh": "定义 OSIA 的行为和说话方式。在每次对话开始时添加到系统提示的开头。",
        "ja": "OSIA の振る舞いと話し方を定義します。会話の開始時にシステム"
              "プロンプトの先頭に追加されます。",
        "de": "Legen Sie fest, wie sich OSIA verhält und spricht. Wird zu "
              "Beginn jeder Unterhaltung an den Anfang des System-Prompts "
              "gestellt.",
        "es": "Define cómo se comporta y habla OSIA. Se añade al principio del "
              "prompt del sistema al inicio de cada conversación.",
    },
    "osia.reset_default": {
        "en": "Reset to default",
        "fr": "Réinitialiser",
        "zh": "恢复默认",
        "ja": "デフォルトに戻す",
        "de": "Zurücksetzen",
        "es": "Restablecer",
    },
    "osia.skills": {
        "en": "Skills",
        "fr": "Compétences",
        "zh": "技能",
        "ja": "スキル",
        "de": "Fähigkeiten",
        "es": "Habilidades",
    },
    "osia.skills_sub": {
        "en": "Reusable instruction files. Enabled skills are appended to the "
              "system prompt.",
        "fr": "Fichiers d'instructions réutilisables. Les compétences activées "
              "sont ajoutées à l'invite système.",
        "zh": "可重复使用的指令文件。已启用的技能会附加到系统提示中。",
        "ja": "再利用可能な指示ファイル。有効なスキルはシステムプロンプトに"
              "追加されます。",
        "de": "Wiederverwendbare Anweisungsdateien. Aktivierte Fähigkeiten "
              "werden an den System-Prompt angehängt.",
        "es": "Archivos de instrucciones reutilizables. Las habilidades "
              "activadas se añaden al prompt del sistema.",
    },
    "osia.skills_empty": {
        "en": "No skills yet. Create one with +.",
        "fr": "Aucune compétence. Créez-en une avec +.",
        "zh": "暂无技能。点击 + 创建一个。",
        "ja": "スキルがありません。+ で作成してください。",
        "de": "Noch keine Fähigkeiten. Erstellen Sie eine mit +.",
        "es": "Aún no hay habilidades. Crea una con +.",
    },
    "osia.skill_add": {
        "en": "New skill",
        "fr": "Nouvelle compétence",
        "zh": "新技能",
        "ja": "新しいスキル",
        "de": "Neue Fähigkeit",
        "es": "Nueva habilidad",
    },
    "osia.skill_edit": {
        "en": "Edit",
        "fr": "Modifier",
        "zh": "编辑",
        "ja": "編集",
        "de": "Bearbeiten",
        "es": "Editar",
    },
    "osia.skill_delete": {
        "en": "Delete",
        "fr": "Supprimer",
        "zh": "删除",
        "ja": "削除",
        "de": "Löschen",
        "es": "Eliminar",
    },
    "osia.skill_toggle": {
        "en": "Enable or disable this skill",
        "fr": "Activer ou désactiver cette compétence",
        "zh": "启用或禁用此技能",
        "ja": "このスキルを有効/無効にする",
        "de": "Diese Fähigkeit aktivieren oder deaktivieren",
        "es": "Activar o desactivar esta habilidad",
    },
    "osia.skill_close": {
        "en": "Close",
        "fr": "Fermer",
        "zh": "关闭",
        "ja": "閉じる",
        "de": "Schließen",
        "es": "Cerrar",
    },
    "osia.skill_new_title": {
        "en": "New skill",
        "fr": "Nouvelle compétence",
        "zh": "新技能",
        "ja": "新しいスキル",
        "de": "Neue Fähigkeit",
        "es": "Nueva habilidad",
    },
    "osia.skill_new_prompt": {
        "en": "Skill name:",
        "fr": "Nom de la compétence :",
        "zh": "技能名称：",
        "ja": "スキル名：",
        "de": "Name der Fähigkeit:",
        "es": "Nombre de la habilidad:",
    },
    # — Chat page (input row + empty state) —
    "chat.attach_file": {
        "en": "Attach a file",
        "fr": "Joindre un fichier",
        "zh": "附加文件",
        "ja": "ファイルを添付",
        "de": "Datei anhängen",
        "es": "Adjuntar un archivo",
    },
    "chat.input_placeholder": {
        "en": "Message OSIA…",
        "fr": "Écrire à OSIA…",
        "zh": "给 OSIA 发消息…",
        "ja": "OSIA にメッセージ…",
        "de": "Nachricht an OSIA…",
        "es": "Mensaje a OSIA…",
    },
    "chat.send": {
        "en": "Send",
        "fr": "Envoyer",
        "zh": "发送",
        "ja": "送信",
        "de": "Senden",
        "es": "Enviar",
    },
    "chat.empty": {
        "en": "Say “OSIA” or type below to start.",
        "fr": "Dites « OSIA » ou écrivez ci-dessous pour commencer.",
        "zh": "说「OSIA」或在下方输入以开始。",
        "ja": "「OSIA」と話しかけるか、下に入力して始めましょう。",
        "de": "Sagen Sie „OSIA“ oder tippen Sie unten, um zu beginnen.",
        "es": "Di «OSIA» o escribe abajo para empezar.",
    },
    # — Conversations panel —
    "conversations.title": {
        "en": "Conversations",
        "fr": "Conversations",
        "zh": "对话",
        "ja": "会話",
        "de": "Unterhaltungen",
        "es": "Conversaciones",
    },
    "conversations.new_chat": {
        "en": "New chat",
        "fr": "Nouveau chat",
        "zh": "新聊天",
        "ja": "新しいチャット",
        "de": "Neuer Chat",
        "es": "Nuevo chat",
    },
    "conversations.new_conversation": {
        "en": "New conversation",
        "fr": "Nouvelle conversation",
        "zh": "新对话",
        "ja": "新しい会話",
        "de": "Neue Unterhaltung",
        "es": "Nueva conversación",
    },
    "conversations.new_chat_confirm": {
        "en": "New chat started",
        "fr": "Nouvelle conversation démarrée",
        "zh": "已开始新对话",
        "ja": "新しいチャットを開始しました",
        "de": "Neues Gespräch gestartet",
        "es": "Nueva conversación iniciada",
    },
    "conversations.msg_count": {
        "en": "{count} msg",
        "fr": "{count} msg",
        "zh": "{count} 条",
        "ja": "{count} 件",
        "de": "{count} Nachr.",
        "es": "{count} msj",
    },
    "conversations.open": {
        "en": "Open",
        "fr": "Ouvrir",
        "zh": "打开",
        "ja": "開く",
        "de": "Öffnen",
        "es": "Abrir",
    },
    "conversations.rename": {
        "en": "Rename",
        "fr": "Renommer",
        "zh": "重命名",
        "ja": "名前を変更",
        "de": "Umbenennen",
        "es": "Renombrar",
    },
    "conversations.rename_title": {
        "en": "Rename conversation",
        "fr": "Renommer la conversation",
        "zh": "重命名对话",
        "ja": "会話の名前を変更",
        "de": "Unterhaltung umbenennen",
        "es": "Renombrar conversación",
    },
    "conversations.rename_prompt": {
        "en": "New name:",
        "fr": "Nouveau nom :",
        "zh": "新名称：",
        "ja": "新しい名前：",
        "de": "Neuer Name:",
        "es": "Nuevo nombre:",
    },
    # — Model panel (two-level picker) —
    "models.title": {
        "en": "Models",
        "fr": "Modèles",
        "zh": "模型",
        "ja": "モデル",
        "de": "Modelle",
        "es": "Modelos",
    },
    "models.choose_provider_sub": {
        "en": "Choose a provider to see its models.",
        "fr": "Choisissez un fournisseur pour voir ses modèles.",
        "zh": "选择一个提供商以查看其模型。",
        "ja": "プロバイダーを選択してそのモデルを表示します。",
        "de": "Wählen Sie einen Anbieter, um seine Modelle anzuzeigen.",
        "es": "Elige un proveedor para ver sus modelos.",
    },
    "models.pick_model_sub": {
        "en": "Pick the model OSIA will use for new messages.",
        "fr": "Choisissez le modèle qu'OSIA utilisera pour les nouveaux messages.",
        "zh": "选择 OSIA 用于新消息的模型。",
        "ja": "OSIA が新しいメッセージに使用するモデルを選択します。",
        "de": "Wählen Sie das Modell, das OSIA für neue Nachrichten verwendet.",
        "es": "Elige el modelo que OSIA usará para los nuevos mensajes.",
    },
    "models.none_for_provider": {
        "en": "No models available for this provider.",
        "fr": "Aucun modèle disponible pour ce fournisseur.",
        "zh": "此提供商没有可用的模型。",
        "ja": "このプロバイダーには利用可能なモデルがありません。",
        "de": "Keine Modelle für diesen Anbieter verfügbar.",
        "es": "No hay modelos disponibles para este proveedor.",
    },
    "models.back": {
        "en": "Back",
        "fr": "Retour",
        "zh": "返回",
        "ja": "戻る",
        "de": "Zurück",
        "es": "Atrás",
    },
    "models.use": {
        "en": "Use",
        "fr": "Utiliser",
        "zh": "使用",
        "ja": "使用",
        "de": "Verwenden",
        "es": "Usar",
    },
    "models.active": {
        "en": "Active",
        "fr": "Actif",
        "zh": "使用中",
        "ja": "使用中",
        "de": "Aktiv",
        "es": "Activo",
    },
    "models.count_one": {
        "en": "{count} model",
        "fr": "{count} modèle",
        "zh": "{count} 个模型",
        "ja": "{count} モデル",
        "de": "{count} Modell",
        "es": "{count} modelo",
    },
    "models.count_other": {
        "en": "{count} models",
        "fr": "{count} modèles",
        "zh": "{count} 个模型",
        "ja": "{count} モデル",
        "de": "{count} Modelle",
        "es": "{count} modelos",
    },
    # — Settings panel header —
    "settings.title": {
        "en": "Settings",
        "fr": "Paramètres",
        "zh": "设置",
        "ja": "設定",
        "de": "Einstellungen",
        "es": "Ajustes",
    },
    # — Settings category nav (WOR-48 P2 master-detail) —
    "settings.cat.general": {
        "en": "General",
        "fr": "Général",
        "zh": "常规",
        "ja": "一般",
        "de": "Allgemein",
        "es": "General",
    },
    "settings.cat.ai": {
        "en": "AI & Providers",
        "fr": "IA & Providers",
        "zh": "AI 与提供商",
        "ja": "AI とプロバイダー",
        "de": "KI & Anbieter",
        "es": "IA y proveedores",
    },
    "settings.cat.voice": {
        "en": "Voice",
        "fr": "Voix",
        "zh": "语音",
        "ja": "音声",
        "de": "Stimme",
        "es": "Voz",
    },
    "settings.cat.wake": {
        "en": "Wake word",
        "fr": "Mot d'activation",
        "zh": "唤醒词",
        "ja": "ウェイクワード",
        "de": "Aktivierungswort",
        "es": "Palabra de activación",
    },
    "settings.cat.mobile": {
        "en": "Connection / Mobile",
        "fr": "Connexion / Mobile",
        "zh": "连接 / 移动",
        "ja": "接続 / モバイル",
        "de": "Verbindung / Mobil",
        "es": "Conexión / Móvil",
    },
    "settings.cat.advanced": {
        "en": "Advanced",
        "fr": "Avancé",
        "zh": "高级",
        "ja": "詳細",
        "de": "Erweitert",
        "es": "Avanzado",
    },
    # ── MCP / Connectors (WOR-44) ───────────────────────────────────────────
    "settings.cat.mcp": {
        "en": "MCP / Connectors",
        "fr": "MCP / Connecteurs",
        "zh": "MCP / 连接器",
        "ja": "MCP / コネクタ",
        "de": "MCP / Konnektoren",
        "es": "MCP / Conectores",
    },
    "settings.mcp": {
        "en": "MCP / Connectors",
        "fr": "MCP / Connecteurs",
        "zh": "MCP / 连接器",
        "ja": "MCP / コネクタ",
        "de": "MCP / Konnektoren",
        "es": "MCP / Conectores",
    },
    "settings.mcp_sub": {
        "en": "Connect external tools and services as extra agent tools.",
        "fr": "Connectez des outils et services externes comme outils supplémentaires de l'agent.",
        "zh": "将外部工具和服务作为额外的代理工具进行连接。",
        "ja": "外部のツールやサービスをエージェントの追加ツールとして接続します。",
        "de": "Externe Tools und Dienste als zusätzliche Agent-Werkzeuge verbinden.",
        "es": "Conecta herramientas y servicios externos como herramientas adicionales del agente.",
    },
    "settings.mcp_desc": {
        "en": "MCP (Model Context Protocol) servers expose tools OSIA can use — search the web, read files, manage repos and more. Connect a preconfigured server with one tap (some need an API token), or add your own. Connected tools become available to OSIA with the same safety as built-in tools.",
        "fr": "Les serveurs MCP (Model Context Protocol) exposent des outils qu'OSIA peut utiliser — chercher sur le web, lire des fichiers, gérer des dépôts, etc. Connectez un serveur préconfiguré en un clic (certains exigent un jeton d'API), ou ajoutez le vôtre. Les outils connectés deviennent disponibles pour OSIA avec la même sécurité que les outils intégrés.",
        "zh": "MCP（模型上下文协议）服务器提供 OSIA 可使用的工具 — 网络搜索、读取文件、管理仓库等。一键连接预配置的服务器（部分需要 API 令牌），或添加你自己的。已连接的工具将以与内置工具相同的安全性提供给 OSIA。",
        "ja": "MCP（Model Context Protocol）サーバーは、OSIA が使えるツール（ウェブ検索、ファイル読み取り、リポジトリ管理など）を提供します。事前設定済みのサーバーをワンタップで接続（一部は API トークンが必要）するか、独自に追加できます。接続されたツールは組み込みツールと同じ安全性で OSIA から利用可能になります。",
        "de": "MCP-Server (Model Context Protocol) stellen Werkzeuge bereit, die OSIA nutzen kann — im Web suchen, Dateien lesen, Repos verwalten und mehr. Verbinden Sie einen vorkonfigurierten Server mit einem Tippen (einige benötigen ein API-Token) oder fügen Sie einen eigenen hinzu. Verbundene Werkzeuge stehen OSIA mit derselben Sicherheit wie integrierte Werkzeuge zur Verfügung.",
        "es": "Los servidores MCP (Model Context Protocol) exponen herramientas que OSIA puede usar: buscar en la web, leer archivos, gestionar repos y más. Conecta un servidor preconfigurado con un toque (algunos requieren un token de API), o añade el tuyo. Las herramientas conectadas quedan disponibles para OSIA con la misma seguridad que las integradas.",
    },
    "mcp.select_label": {
        "en": "Connector", "fr": "Connecteur", "zh": "连接器",
        "ja": "コネクタ", "de": "Konnektor", "es": "Conector",
    },
    "mcp.connect": {
        "en": "Connect", "fr": "Connecter", "zh": "连接",
        "ja": "接続", "de": "Verbinden", "es": "Conectar",
    },
    # WOR-44 Phase 5 (5-oauth-a): connect button for OAuth connectors (Linear).
    "mcp.connect_oauth": {
        "en": "Connect with OAuth", "fr": "Connecter via OAuth",
        "zh": "通过 OAuth 连接", "ja": "OAuth で接続",
        "de": "Mit OAuth verbinden", "es": "Conectar con OAuth",
    },
    "mcp.disconnect": {
        "en": "Disconnect", "fr": "Déconnecter", "zh": "断开",
        "ja": "切断", "de": "Trennen", "es": "Desconectar",
    },
    # WOR-97: abort an in-flight OAuth login (shown only while "connecting…").
    "mcp.cancel": {
        "en": "Cancel", "fr": "Annuler", "zh": "取消",
        "ja": "キャンセル", "de": "Abbrechen", "es": "Cancelar",
    },
    "mcp.status_disconnected": {
        "en": "Disconnected", "fr": "Déconnecté", "zh": "未连接",
        "ja": "未接続", "de": "Getrennt", "es": "Desconectado",
    },
    "mcp.status_connecting": {
        "en": "Connecting…", "fr": "Connexion…", "zh": "连接中…",
        "ja": "接続中…", "de": "Verbinde…", "es": "Conectando…",
    },
    "mcp.status_connected": {
        "en": "Connected", "fr": "Connecté", "zh": "已连接",
        "ja": "接続済み", "de": "Verbunden", "es": "Conectado",
    },
    "mcp.status_failed": {
        "en": "Failed", "fr": "Échec", "zh": "失败",
        "ja": "失敗", "de": "Fehlgeschlagen", "es": "Error",
    },
    "mcp.oauth_soon": {
        "en": "OAuth — soon", "fr": "OAuth — bientôt", "zh": "OAuth — 即将推出",
        "ja": "OAuth — 近日対応", "de": "OAuth — bald", "es": "OAuth — pronto",
    },
    "mcp.transport_soon": {
        "en": "HTTP — soon", "fr": "HTTP — bientôt", "zh": "HTTP — 即将推出",
        "ja": "HTTP — 近日対応", "de": "HTTP — bald", "es": "HTTP — pronto",
    },
    "mcp.err_http_soon": {
        "en": "Remote (HTTP) servers can't be connected yet — coming in a later update. Use a stdio command for now.",
        "fr": "Les serveurs distants (HTTP) ne peuvent pas encore être connectés — à venir. Utilisez une commande stdio pour l'instant.",
        "zh": "远程 (HTTP) 服务器暂不支持连接 — 后续更新将推出。目前请使用 stdio 命令。",
        "ja": "リモート (HTTP) サーバーはまだ接続できません — 今後のアップデートで対応予定です。今は stdio コマンドを使用してください。",
        "de": "Remote-Server (HTTP) können noch nicht verbunden werden — kommt in einem späteren Update. Verwenden Sie vorerst einen stdio-Befehl.",
        "es": "Los servidores remotos (HTTP) aún no se pueden conectar — llegará en una actualización posterior. Usa un comando stdio por ahora.",
    },
    # WOR-44 Phase 5 (5-conf-b): desktop confirmation dialog for write MCP tools.
    "mcp.confirm.title": {
        "en": "Allow this action?", "fr": "Autoriser cette action ?",
        "zh": "允许此操作？", "ja": "この操作を許可しますか？",
        "de": "Diese Aktion zulassen?", "es": "¿Permitir esta acción?",
    },
    "mcp.confirm.body": {
        "en": "{server} wants to run “{tool}”.",
        "fr": "{server} veut exécuter « {tool} ».",
        "zh": "{server} 想要运行“{tool}”。",
        "ja": "{server} が「{tool}」を実行しようとしています。",
        "de": "{server} möchte „{tool}“ ausführen.",
        "es": "{server} quiere ejecutar «{tool}».",
    },
    "mcp.confirm.warn": {
        "en": "This is a write action that may change or delete data.",
        "fr": "C'est une action d'écriture qui peut modifier ou supprimer des données.",
        "zh": "这是一项写入操作，可能会更改或删除数据。",
        "ja": "これはデータを変更または削除する可能性のある書き込み操作です。",
        "de": "Dies ist eine Schreibaktion, die Daten ändern oder löschen kann.",
        "es": "Esta es una acción de escritura que puede cambiar o eliminar datos.",
    },
    "mcp.confirm.approve": {
        "en": "Approve", "fr": "Approuver", "zh": "批准",
        "ja": "承認", "de": "Zulassen", "es": "Aprobar",
    },
    "mcp.confirm.decline": {
        "en": "Decline", "fr": "Refuser", "zh": "拒绝",
        "ja": "拒否", "de": "Ablehnen", "es": "Rechazar",
    },
    # WOR-44 Phase 5 (5-conf): PC "Write safety" toggles in the MCP settings
    # section — the global gate for WRITE MCP tools (mirrors the mobile card).
    # Wording is kept verbatim with osia-mobile l10n.dart (mcpConfirmWrites* /
    # mcpUnattendedWrites*) so PC and mobile read identically.
    "mcp.confirm_writes": {
        "en": "Confirm write actions",
        "fr": "Confirmer les actions d'écriture",
        "zh": "确认写入操作",
        "ja": "書き込み操作を確認する",
        "de": "Schreibaktionen bestätigen",
        "es": "Confirmar acciones de escritura",
    },
    "mcp.confirm_writes_sub": {
        "en": "Ask before a connector creates, changes or deletes anything.",
        "fr": "Demander avant qu'un connecteur crée, modifie ou supprime quelque chose.",
        "zh": "在连接器创建、修改或删除任何内容前先询问。",
        "ja": "コネクターが作成・変更・削除を行う前に確認します。",
        "de": "Nachfragen, bevor ein Connector etwas erstellt, ändert oder löscht.",
        "es": "Preguntar antes de que un conector cree, modifique o elimine algo.",
    },
    "mcp.unattended_writes": {
        "en": "Allow unattended writes",
        "fr": "Autoriser les écritures sans surveillance",
        "zh": "允许无人值守写入",
        "ja": "無人での書き込みを許可",
        "de": "Unbeaufsichtigte Schreibvorgänge erlauben",
        "es": "Permitir escrituras desatendidas",
    },
    "mcp.unattended_writes_sub": {
        "en": "Let write actions run with no device connected to confirm (e.g. scheduled tasks).",
        "fr": "Laisser les actions d'écriture s'exécuter sans appareil connecté pour confirmer (ex. tâches planifiées).",
        "zh": "在没有设备连接确认时，也允许写入操作运行（例如计划任务）。",
        "ja": "確認する端末が接続されていなくても書き込みを実行します（例：予約タスク）。",
        "de": "Schreibaktionen ausführen, auch wenn kein Gerät zum Bestätigen verbunden ist (z. B. geplante Aufgaben).",
        "es": "Permitir que las acciones de escritura se ejecuten sin un dispositivo conectado para confirmar (p. ej. tareas programadas).",
    },
    "mcp.field_slack_team": {
        "en": "Slack team ID", "fr": "ID d'équipe Slack", "zh": "Slack 团队 ID",
        "ja": "Slack チーム ID", "de": "Slack-Team-ID", "es": "ID de equipo de Slack",
    },
    "mcp.field_slack_team_ph": {
        "en": "e.g. T01234567", "fr": "ex. T01234567", "zh": "例如：T01234567",
        "ja": "例：T01234567", "de": "z. B. T01234567", "es": "p. ej. T01234567",
    },
    "mcp.add_custom": {
        "en": "Add custom server", "fr": "Ajouter un serveur",
        "zh": "添加自定义服务器", "ja": "カスタムサーバーを追加",
        "de": "Eigenen Server hinzufügen", "es": "Añadir servidor personalizado",
    },
    "mcp.remove": {
        "en": "Remove", "fr": "Supprimer", "zh": "移除",
        "ja": "削除", "de": "Entfernen", "es": "Quitar",
    },
    "mcp.lbl_token": {
        "en": "API token / key", "fr": "Jeton / clé d'API",
        "zh": "API 令牌 / 密钥", "ja": "API トークン / キー",
        "de": "API-Token / -Schlüssel", "es": "Token / clave de API",
    },
    "mcp.ph_token": {
        "en": "Paste token (stored locally, never synced)…",
        "fr": "Collez le jeton (stocké en local, jamais synchronisé)…",
        "zh": "粘贴令牌（本地存储，不会同步）…",
        "ja": "トークンを貼り付け（ローカル保存、同期されません）…",
        "de": "Token einfügen (lokal gespeichert, nie synchronisiert)…",
        "es": "Pega el token (se guarda localmente, nunca se sincroniza)…",
    },
    "mcp.lbl_dir": {
        "en": "Allowed directory", "fr": "Dossier autorisé",
        "zh": "允许的目录", "ja": "許可するディレクトリ",
        "de": "Erlaubtes Verzeichnis", "es": "Carpeta permitida",
    },
    "mcp.ph_dir": {
        "en": "Folder OSIA may read/write…",
        "fr": "Dossier qu'OSIA peut lire/écrire…",
        "zh": "OSIA 可读写的文件夹…",
        "ja": "OSIA が読み書きできるフォルダー…",
        "de": "Ordner, den OSIA lesen/schreiben darf…",
        "es": "Carpeta que OSIA puede leer/escribir…",
    },
    "mcp.custom_title": {
        "en": "Add a custom MCP server", "fr": "Ajouter un serveur MCP personnalisé",
        "zh": "添加自定义 MCP 服务器", "ja": "カスタム MCP サーバーを追加",
        "de": "Eigenen MCP-Server hinzufügen", "es": "Añadir un servidor MCP personalizado",
    },
    "mcp.custom_hint": {
        "en": "Add a local stdio command (e.g. npx …). Remote (HTTP) servers are coming in a later update. Adding does not connect — connect it explicitly afterwards.",
        "fr": "Ajoutez une commande stdio locale (ex. npx …). Les serveurs distants (HTTP) arriveront plus tard. L'ajout ne connecte pas — connectez-le ensuite explicitement.",
        "zh": "添加本地 stdio 命令（例如 npx …）。远程 (HTTP) 服务器将在后续更新中推出。添加不会自动连接 — 之后请手动连接。",
        "ja": "ローカルの stdio コマンド（例：npx …）を追加します。リモート (HTTP) サーバーは今後のアップデートで対応予定です。追加しても接続はされません — 後で明示的に接続してください。",
        "de": "Fügen Sie einen lokalen stdio-Befehl hinzu (z. B. npx …). Remote-Server (HTTP) folgen in einem späteren Update. Das Hinzufügen verbindet nicht — verbinden Sie ihn danach ausdrücklich.",
        "es": "Añade un comando stdio local (p. ej. npx …). Los servidores remotos (HTTP) llegarán en una actualización posterior. Añadir no conecta — conéctalo explícitamente después.",
    },
    "mcp.field_name": {
        "en": "Name", "fr": "Nom", "zh": "名称",
        "ja": "名前", "de": "Name", "es": "Nombre",
    },
    "mcp.field_name_ph": {
        "en": "e.g. My server", "fr": "ex. Mon serveur", "zh": "例如：我的服务器",
        "ja": "例：マイサーバー", "de": "z. B. Mein Server", "es": "p. ej. Mi servidor",
    },
    "mcp.field_command": {
        "en": "Command (stdio)", "fr": "Commande (stdio)", "zh": "命令 (stdio)",
        "ja": "コマンド (stdio)", "de": "Befehl (stdio)", "es": "Comando (stdio)",
    },
    "mcp.field_command_ph": {
        "en": "e.g. npx -y @scope/server …",
        "fr": "ex. npx -y @scope/server …",
        "zh": "例如：npx -y @scope/server …",
        "ja": "例：npx -y @scope/server …",
        "de": "z. B. npx -y @scope/server …",
        "es": "p. ej. npx -y @scope/server …",
    },
    "mcp.field_url": {
        "en": "URL (remote)", "fr": "URL (distant)", "zh": "URL（远程）",
        "ja": "URL（リモート）", "de": "URL (remote)", "es": "URL (remoto)",
    },
    "mcp.field_url_ph": {
        "en": "https://…", "fr": "https://…", "zh": "https://…",
        "ja": "https://…", "de": "https://…", "es": "https://…",
    },
    "mcp.field_token_optional": {
        "en": "Token (optional)", "fr": "Jeton (facultatif)", "zh": "令牌（可选）",
        "ja": "トークン（任意）", "de": "Token (optional)", "es": "Token (opcional)",
    },
    "mcp.field_token_ph": {
        "en": "Stored locally, never synced…",
        "fr": "Stocké en local, jamais synchronisé…",
        "zh": "本地存储，不会同步…",
        "ja": "ローカル保存、同期されません…",
        "de": "Lokal gespeichert, nie synchronisiert…",
        "es": "Se guarda localmente, nunca se sincroniza…",
    },
    "mcp.field_token_env_optional": {
        "en": "Token env var (stdio, optional)",
        "fr": "Variable d'env. du jeton (stdio, facultatif)",
        "zh": "令牌环境变量 (stdio，可选)",
        "ja": "トークンの環境変数 (stdio、任意)",
        "de": "Token-Umgebungsvariable (stdio, optional)",
        "es": "Variable de entorno del token (stdio, opcional)",
    },
    "mcp.field_token_env_ph": {
        "en": "e.g. MY_API_KEY", "fr": "ex. MY_API_KEY", "zh": "例如：MY_API_KEY",
        "ja": "例：MY_API_KEY", "de": "z. B. MY_API_KEY", "es": "p. ej. MY_API_KEY",
    },
    "mcp.err_name": {
        "en": "Please enter a name.", "fr": "Veuillez saisir un nom.",
        "zh": "请输入名称。", "ja": "名前を入力してください。",
        "de": "Bitte einen Namen eingeben.", "es": "Introduce un nombre.",
    },
    "mcp.err_transport": {
        "en": "Provide exactly one of: a command or a URL.",
        "fr": "Indiquez exactement l'un des deux : une commande ou une URL.",
        "zh": "请仅提供其一：命令或 URL。",
        "ja": "コマンドか URL のいずれか一方だけを指定してください。",
        "de": "Geben Sie genau eines an: einen Befehl oder eine URL.",
        "es": "Indica exactamente uno: un comando o una URL.",
    },
    "mcp.err_url": {
        "en": "URL must start with http:// or https://",
        "fr": "L'URL doit commencer par http:// ou https://",
        "zh": "URL 必须以 http:// 或 https:// 开头",
        "ja": "URL は http:// または https:// で始まる必要があります",
        "de": "Die URL muss mit http:// oder https:// beginnen",
        "es": "La URL debe empezar con http:// o https://",
    },
    "mcp.desc.filesystem": {
        "en": "Read & write local files in an allowed folder.",
        "fr": "Lire et écrire des fichiers locaux dans un dossier autorisé.",
        "zh": "在允许的文件夹中读写本地文件。",
        "ja": "許可されたフォルダー内のローカルファイルを読み書きします。",
        "de": "Lokale Dateien in einem erlaubten Ordner lesen und schreiben.",
        "es": "Leer y escribir archivos locales en una carpeta permitida.",
    },
    "mcp.desc.brave": {
        "en": "Web & local search via the Brave Search API.",
        "fr": "Recherche web et locale via l'API Brave Search.",
        "zh": "通过 Brave Search API 进行网络与本地搜索。",
        "ja": "Brave Search API によるウェブ・ローカル検索。",
        "de": "Web- und lokale Suche über die Brave-Search-API.",
        "es": "Búsqueda web y local mediante la API de Brave Search.",
    },
    "mcp.desc.github": {
        "en": "Repos, issues & pull requests on GitHub.",
        "fr": "Dépôts, issues et pull requests sur GitHub.",
        "zh": "GitHub 上的仓库、议题与拉取请求。",
        "ja": "GitHub のリポジトリ・Issue・プルリクエスト。",
        "de": "Repos, Issues und Pull Requests auf GitHub.",
        "es": "Repos, incidencias y pull requests en GitHub.",
    },
    "mcp.desc.slack": {
        "en": "Read & post messages in your Slack workspace.",
        "fr": "Lire et publier des messages dans votre espace Slack.",
        "zh": "在你的 Slack 工作区读取与发布消息。",
        "ja": "Slack ワークスペースでメッセージを読み書きします。",
        "de": "Nachrichten in Ihrem Slack-Workspace lesen und posten.",
        "es": "Leer y publicar mensajes en tu espacio de Slack.",
    },
    "mcp.desc.notion": {
        "en": "Pages & databases in your Notion workspace.",
        "fr": "Pages et bases de données de votre espace Notion.",
        "zh": "你的 Notion 工作区中的页面与数据库。",
        "ja": "Notion ワークスペースのページとデータベース。",
        "de": "Seiten und Datenbanken in Ihrem Notion-Workspace.",
        "es": "Páginas y bases de datos de tu espacio de Notion.",
    },
    "mcp.desc.linear": {
        "en": "Issues, projects & cycles in Linear.",
        "fr": "Issues, projets et cycles dans Linear.",
        "zh": "Linear 中的议题、项目与周期。",
        "ja": "Linear の Issue・プロジェクト・サイクル。",
        "de": "Issues, Projekte und Zyklen in Linear.",
        "es": "Incidencias, proyectos y ciclos en Linear.",
    },
    "mcp.desc.gdrive": {
        "en": "Browse & read files in Google Drive.",
        "fr": "Parcourir et lire des fichiers dans Google Drive.",
        "zh": "浏览并读取 Google Drive 中的文件。",
        "ja": "Google Drive のファイルを閲覧・読み取りします。",
        "de": "Dateien in Google Drive durchsuchen und lesen.",
        "es": "Explorar y leer archivos en Google Drive.",
    },
    "mcp.desc.gmail": {
        "en": "Search & manage email in Gmail.",
        "fr": "Rechercher et gérer les e-mails dans Gmail.",
        "zh": "在 Gmail 中搜索与管理电子邮件。",
        "ja": "Gmail のメールを検索・管理します。",
        "de": "E-Mails in Gmail durchsuchen und verwalten.",
        "es": "Buscar y gestionar el correo en Gmail.",
    },
    "settings.minimize": {
        "en": "Minimize",
        "fr": "Réduire",
        "zh": "最小化",
        "ja": "最小化",
        "de": "Minimieren",
        "es": "Minimizar",
    },
    # — Startup section (autostart services) —
    "settings.startup": {
        "en": "Startup",
        "fr": "Démarrage",
        "zh": "启动",
        "ja": "起動",
        "de": "Autostart",
        "es": "Inicio",
    },
    "settings.startup_sub": {
        "en": "Launch the mobile server and Cloudflare tunnel at startup.",
        "fr": "Lancer le serveur mobile et le tunnel Cloudflare au démarrage.",
        "zh": "在启动时启动移动服务器和 Cloudflare 隧道。",
        "ja": "起動時にモバイルサーバーと Cloudflare トンネルを起動します。",
        "de": "Mobilen Server und Cloudflare-Tunnel beim Start ausführen.",
        "es": "Iniciar el servidor móvil y el túnel de Cloudflare al arrancar.",
    },
    "settings.startup_desc": {
        "en": "These services let your phone reach OSIA: the mobile server accepts the app's connection and the Cloudflare tunnel exposes it securely over the internet. Enable them to start automatically with your computer, or leave them off if you only use OSIA on this desktop.",
        "fr": "Ces services permettent à votre téléphone d'atteindre OSIA : le serveur mobile accepte la connexion de l'application et le tunnel Cloudflare l'expose en toute sécurité sur Internet. Activez-les pour qu'ils démarrent automatiquement avec votre ordinateur, ou laissez-les désactivés si vous n'utilisez OSIA que sur ce poste.",
        "zh": "这些服务让您的手机能够连接 OSIA：移动服务器接受应用的连接，Cloudflare 隧道则通过互联网安全地将其暴露出来。启用它们可随电脑自动启动，或如果您只在这台电脑上使用 OSIA，则保持关闭。",
        "ja": "これらのサービスはスマートフォンから OSIA に接続できるようにします。モバイルサーバーがアプリの接続を受け付け、Cloudflare トンネルがインターネット上に安全に公開します。コンピューターと一緒に自動起動したい場合は有効にし、このデスクトップだけで OSIA を使う場合はオフのままにします。",
        "de": "Diese Dienste ermöglichen es Ihrem Telefon, OSIA zu erreichen: Der mobile Server nimmt die Verbindung der App an, und der Cloudflare-Tunnel stellt ihn sicher über das Internet bereit. Aktivieren Sie sie, damit sie automatisch mit Ihrem Computer starten, oder lassen Sie sie aus, wenn Sie OSIA nur auf diesem Desktop nutzen.",
        "es": "Estos servicios permiten que tu teléfono llegue a OSIA: el servidor móvil acepta la conexión de la app y el túnel de Cloudflare lo expone de forma segura por internet. Actívalos para que arranquen automáticamente con tu computadora, o déjalos desactivados si solo usas OSIA en este escritorio.",
    },
    "settings.startup_mobile_server": {
        "en": "Mobile server",
        "fr": "Serveur mobile",
        "zh": "移动服务器",
        "ja": "モバイルサーバー",
        "de": "Mobiler Server",
        "es": "Servidor móvil",
    },
    "settings.startup_cloudflare": {
        "en": "Cloudflare tunnel",
        "fr": "Tunnel Cloudflare",
        "zh": "Cloudflare 隧道",
        "ja": "Cloudflare トンネル",
        "de": "Cloudflare-Tunnel",
        "es": "Túnel de Cloudflare",
    },
    # — Floating capsule + autostart (WOR-22) —
    "settings.capsule": {
        "en": "Floating capsule",
        "fr": "Capsule flottante",
        "zh": "浮动胶囊",
        "ja": "フローティングカプセル",
        "de": "Schwebende Kapsel",
        "es": "Cápsula flotante",
    },
    "settings.capsule_sub": {
        "en": "Show the capsule; launch at login.",
        "fr": "Afficher la capsule ; lancer au démarrage.",
        "zh": "显示胶囊；登录时启动。",
        "ja": "カプセルを表示、ログイン時に起動。",
        "de": "Kapsel anzeigen; beim Anmelden starten.",
        "es": "Mostrar la cápsula; lanzar al iniciar sesión.",
    },
    "settings.capsule_desc": {
        "en": "The floating capsule is the small always-on-top button you click to talk to OSIA. Turn it off to keep OSIA in the tray only, or enable launch-at-login to start OSIA automatically.",
        "fr": "La capsule flottante est le petit bouton toujours au premier plan sur lequel vous cliquez pour parler à OSIA. Désactivez-la pour garder OSIA uniquement dans la barre des tâches, ou activez le lancement au démarrage pour ouvrir OSIA automatiquement.",
        "zh": "浮动胶囊是始终置顶的小按钮，点击它即可与 OSIA 对话。关闭它可让 OSIA 仅驻留在系统托盘中，或启用登录时启动以自动打开 OSIA。",
        "ja": "フローティングカプセルは常に最前面に表示される小さなボタンで、クリックすると OSIA と話せます。オフにすると OSIA はトレイのみに常駐し、ログイン時に起動を有効にすると OSIA が自動的に開きます。",
        "de": "Die schwebende Kapsel ist die kleine, stets im Vordergrund liegende Schaltfläche, die Sie anklicken, um mit OSIA zu sprechen. Schalten Sie sie aus, um OSIA nur im Infobereich zu behalten, oder aktivieren Sie den Start bei der Anmeldung, damit OSIA automatisch öffnet.",
        "es": "La cápsula flotante es el pequeño botón siempre visible que pulsas para hablar con OSIA. Desactívala para mantener OSIA solo en la bandeja, o activa el lanzamiento al iniciar sesión para abrir OSIA automáticamente.",
    },
    "settings.show_capsule": {
        "en": "Show the floating capsule",
        "fr": "Afficher la capsule flottante",
        "zh": "显示浮动胶囊",
        "ja": "フローティングカプセルを表示",
        "de": "Schwebende Kapsel anzeigen",
        "es": "Mostrar la cápsula flotante",
    },
    "settings.autostart_gui": {
        "en": "Launch OSIA at startup",
        "fr": "Lancer OSIA au démarrage",
        "zh": "开机时启动 OSIA",
        "ja": "起動時に OSIA を起動",
        "de": "OSIA beim Start ausführen",
        "es": "Iniciar OSIA al arrancar",
    },
    # — Autonomous agent (WOR-45 Phase 4) —
    "autonomous.title": {
        "en": "Autonomous",
        "fr": "Autonome",
        "zh": "自主模式",
        "ja": "自律モード",
        "de": "Autonom",
        "es": "Autónomo",
    },
    "autonomous.objective_title": {
        "en": "New autonomous task",
        "fr": "Nouvelle tâche autonome",
        "zh": "新建自主任务",
        "ja": "新しい自律タスク",
        "de": "Neue autonome Aufgabe",
        "es": "Nueva tarea autónoma",
    },
    "autonomous.objective_sub": {
        "en": "Describe a goal. OSIA plans it into steps, then carries them out "
              "on its own — verifying each step before moving on.",
        "fr": "Décrivez un objectif. OSIA le découpe en étapes puis les exécute "
              "seul, en vérifiant chaque étape avant de continuer.",
        "zh": "描述一个目标。OSIA 会将其拆分为多个步骤并自动执行，每步完成后会先验证再继续。",
        "ja": "目標を入力してください。OSIA がステップに分解し、各ステップを検証しながら自動的に実行します。",
        "de": "Beschreiben Sie ein Ziel. OSIA zerlegt es in Schritte und führt "
              "sie selbstständig aus – jeder Schritt wird vor dem nächsten "
              "überprüft.",
        "es": "Describe un objetivo. OSIA lo divide en pasos y los ejecuta por "
              "su cuenta, verificando cada paso antes de continuar.",
    },
    "autonomous.objective_placeholder": {
        "en": "e.g. Create a folder 'report' with three text files summarizing…",
        "fr": "ex. Créer un dossier « rapport » avec trois fichiers texte résumant…",
        "zh": "例如：创建一个名为“report”的文件夹，并生成三个文本文件…",
        "ja": "例：「report」フォルダを作成し、3 つのテキストファイルを生成…",
        "de": "z. B. Einen Ordner „report“ mit drei Textdateien erstellen…",
        "es": "p. ej. Crear una carpeta «report» con tres archivos de texto…",
    },
    "autonomous.model": {
        "en": "Model",
        "fr": "Modèle",
        "zh": "模型",
        "ja": "モデル",
        "de": "Modell",
        "es": "Modelo",
    },
    "autonomous.start": {
        "en": "Start",
        "fr": "Démarrer",
        "zh": "开始",
        "ja": "開始",
        "de": "Starten",
        "es": "Iniciar",
    },
    "autonomous.planning": {
        "en": "Planning…",
        "fr": "Planification…",
        "zh": "规划中…",
        "ja": "計画中…",
        "de": "Planung…",
        "es": "Planificando…",
    },
    "autonomous.running": {
        "en": "Running…",
        "fr": "En cours…",
        "zh": "执行中…",
        "ja": "実行中…",
        "de": "Läuft…",
        "es": "En curso…",
    },
    "autonomous.progress_title": {
        "en": "Progress",
        "fr": "Progression",
        "zh": "进度",
        "ja": "進捗",
        "de": "Fortschritt",
        "es": "Progreso",
    },
    "autonomous.phase": {
        "en": "Phase: {name}",
        "fr": "Phase : {name}",
        "zh": "阶段：{name}",
        "ja": "フェーズ：{name}",
        "de": "Phase: {name}",
        "es": "Fase: {name}",
    },
    "autonomous.tasks": {
        "en": "{done} / {total} tasks",
        "fr": "{done} / {total} tâches",
        "zh": "{done} / {total} 个任务",
        "ja": "{done} / {total} タスク",
        "de": "{done} / {total} Aufgaben",
        "es": "{done} / {total} tareas",
    },
    "autonomous.interrupt": {
        "en": "Interrupt",
        "fr": "Interrompre",
        "zh": "中断",
        "ja": "中断",
        "de": "Unterbrechen",
        "es": "Interrumpir",
    },
    "autonomous.abort": {
        "en": "Abandon task",
        "fr": "Abandonner la tâche",
        "zh": "放弃任务",
        "ja": "タスクを破棄",
        "de": "Aufgabe verwerfen",
        "es": "Abandonar tarea",
    },
    "autonomous.status_idle": {
        "en": "No task running.",
        "fr": "Aucune tâche en cours.",
        "zh": "没有正在运行的任务。",
        "ja": "実行中のタスクはありません。",
        "de": "Keine Aufgabe aktiv.",
        "es": "No hay ninguna tarea en curso.",
    },
    "autonomous.status_planning": {
        "en": "Planning the steps…",
        "fr": "Planification des étapes…",
        "zh": "正在规划步骤…",
        "ja": "ステップを計画中…",
        "de": "Schritte werden geplant…",
        "es": "Planificando los pasos…",
    },
    "autonomous.status_running": {
        "en": "Working on the task…",
        "fr": "Tâche en cours…",
        "zh": "正在执行任务…",
        "ja": "タスクを実行中…",
        "de": "Aufgabe wird bearbeitet…",
        "es": "Ejecutando la tarea…",
    },
    "autonomous.status_task_done": {
        "en": "Step done. {reason}",
        "fr": "Étape terminée. {reason}",
        "zh": "步骤完成。{reason}",
        "ja": "ステップ完了。{reason}",
        "de": "Schritt erledigt. {reason}",
        "es": "Paso completado. {reason}",
    },
    "autonomous.status_task_failed": {
        "en": "Step failed. {reason}",
        "fr": "Étape échouée. {reason}",
        "zh": "步骤失败。{reason}",
        "ja": "ステップ失敗。{reason}",
        "de": "Schritt fehlgeschlagen. {reason}",
        "es": "Paso fallido. {reason}",
    },
    "autonomous.status_salvaged": {
        "en": "A step was already complete — recovered on resume.",
        "fr": "Une étape était déjà terminée — récupérée à la reprise.",
        "zh": "某步骤已完成——恢复时已沿用。",
        "ja": "あるステップは完了済みでした — 再開時に引き継ぎました。",
        "de": "Ein Schritt war bereits fertig – beim Fortsetzen übernommen.",
        "es": "Un paso ya estaba completo — recuperado al reanudar.",
    },
    "autonomous.status_done": {
        "en": "Task complete. ✓",
        "fr": "Tâche terminée. ✓",
        "zh": "任务完成。✓",
        "ja": "タスク完了。✓",
        "de": "Aufgabe abgeschlossen. ✓",
        "es": "Tarea completada. ✓",
    },
    "autonomous.status_failed": {
        "en": "Task failed. {reason}",
        "fr": "Tâche échouée. {reason}",
        "zh": "任务失败。{reason}",
        "ja": "タスク失敗。{reason}",
        "de": "Aufgabe fehlgeschlagen. {reason}",
        "es": "Tarea fallida. {reason}",
    },
    "autonomous.status_interrupted": {
        "en": "Paused — resumes on the next launch.",
        "fr": "En pause — reprise au prochain démarrage.",
        "zh": "已暂停——下次启动时继续。",
        "ja": "一時停止 — 次回起動時に再開します。",
        "de": "Pausiert – wird beim nächsten Start fortgesetzt.",
        "es": "En pausa — se reanuda en el próximo inicio.",
    },
    "autonomous.status_aborted": {
        "en": "Task abandoned.",
        "fr": "Tâche abandonnée.",
        "zh": "任务已放弃。",
        "ja": "タスクを破棄しました。",
        "de": "Aufgabe verworfen.",
        "es": "Tarea abandonada.",
    },
    "autonomous.status_plan_failed": {
        "en": "Planning failed — try rephrasing the goal.",
        "fr": "Échec de la planification — reformulez l'objectif.",
        "zh": "规划失败——请尝试重新描述目标。",
        "ja": "計画に失敗しました — 目標を言い換えてください。",
        "de": "Planung fehlgeschlagen – Ziel bitte umformulieren.",
        "es": "Error de planificación — reformula el objetivo.",
    },
    # — Bottom navigation (icon tooltips) —
    "nav.conversations": {
        "en": "Conversations",
        "fr": "Conversations",
        "zh": "对话",
        "ja": "会話",
        "de": "Unterhaltungen",
        "es": "Conversaciones",
    },
    "nav.models": {
        "en": "Model",
        "fr": "Modèle",
        "zh": "模型",
        "ja": "モデル",
        "de": "Modell",
        "es": "Modelo",
    },
    "nav.settings": {
        "en": "Settings",
        "fr": "Paramètres",
        "zh": "设置",
        "ja": "設定",
        "de": "Einstellungen",
        "es": "Ajustes",
    },
    "nav.autonomous": {
        "en": "Autonomous",
        "fr": "Autonome",
        "zh": "自主",
        "ja": "自律",
        "de": "Autonom",
        "es": "Autónomo",
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
