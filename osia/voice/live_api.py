#!/usr/bin/env python3
"""OSIA Live API — native voice-to-voice streaming (Bloc 4, placeholder).

When *Live API* mode is enabled in settings, OSIA bypasses its local STT
(Whisper) and TTS pipelines entirely and instead streams microphone audio to a
provider's realtime / live endpoint, which transcribes, reasons and speaks back
natively over a single bidirectional connection.

This module is a **stub**. The real bidirectional WebSocket streaming to the
Google Gemini Live, OpenAI Realtime and Claude live endpoints is a separate,
larger task. :func:`_connect_live_api` currently logs and returns ``None`` so
the wake flow (``osia.main.wake_mode``) can already branch on Live API without
the streaming layer existing yet.

The API key is **reused** from the matching AI provider configured in the
"AI Providers" settings section — there is no separate Live-API key store.
"""

from osia.config import get_api_key

# Maps the Live API provider value (settings ``liveapi_provider``) to the
# AI-provider entry in the shared key store its credential is reused from.
LIVEAPI_KEY_PROVIDER = {
    "google": "google",
    "openai": "openai",
    "claude": "anthropic",
}


def _connect_live_api(provider: str, model: str | None = None):
    """Open a live voice-to-voice session with *provider* (stub).

    The real implementation will open a bidirectional WebSocket to the
    provider's live/realtime endpoint and pump microphone audio in / spoken
    audio out. For now this only logs that the feature is not yet wired up and
    returns ``None``.

    Never raises — a failure here must not crash the wake handler.
    """
    key_provider = LIVEAPI_KEY_PROVIDER.get(provider, provider)
    has_key = bool(get_api_key(key_provider))
    print(
        f"[LIVE API] Live API not yet implemented — provider={provider} "
        f"model={model or '(default)'} key={'set' if has_key else 'missing'}"
    )
    return None
