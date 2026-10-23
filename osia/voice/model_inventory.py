"""Filesystem-only speech model discovery; safe for Settings and startup.

Do not import audio, numerical libraries or model runtimes here.
"""
from pathlib import Path

from osia.runtime import bundled_model_path

_FASTER_WHISPER_REPO_PREFIX = "models--Systran--faster-whisper-"


def available_whisper_model_sizes() -> set[str]:
    """Return local faster-whisper model sizes available to OSIA."""
    found: set[str] = set()
    for size in ("tiny", "base", "small", "medium", "large-v3"):
        if bundled_model_path(f"faster-whisper-{size}") is not None:
            found.add(size)
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    try:
        for entry in hub.iterdir():
            if entry.is_dir() and entry.name.startswith(_FASTER_WHISPER_REPO_PREFIX):
                found.add(entry.name[len(_FASTER_WHISPER_REPO_PREFIX):])
    except OSError:
        pass
    return found
