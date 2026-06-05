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
