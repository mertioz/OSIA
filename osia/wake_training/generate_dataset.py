#!/usr/bin/env python3
"""
WOR-37 — Synthetic, multi-voice training dataset for the "osia" wake-word verifier.

The original verifier (osia_verifier.pkl) was trained on a single person's voice
(record_samples.py / record_negative.py). This script builds a *voice-agnostic*
dataset entirely from text-to-speech + signal-processing augmentation so the
retrained verifier works for any voice out of the box — no microphone needed.

Each positive is rendered from three spellings — "ossia" / "ocia" / "osia" — so
the dataset covers the hard /s/, soft /s/ and voiced /z/ pronunciations users
actually produce (see ``POSITIVE_VARIANTS``).

Sources
-------
* Gemini TTS  — the ~30 prebuilt Gemini voices already used by osia.voice.tts
                (Kore, Puck, Charon, Leda, Fenrir, Aoede, ...). Each positive
                voice × variant is rendered in three prompt styles:
                normal / slow / fast. Rate-limited (free tier), so optional.
* Kokoro TTS  — the full English + French offline Kokoro line-up (~14 voices,
                free, no rate limit) — the primary source when Gemini is rate-
                limited. Lazily downloaded from HuggingFace; each voice is wrapped
                in try/except and skipped gracefully if it fails to load. Kokoro
                positives are augmented deeply (``--kokoro-aug``, default 6) to
                make up for fewer base timbres than Gemini.
* numpy/scipy — silence, white-noise and pink-noise negatives, plus per-clip
                augmentation (speed/pitch/volume jitter + light background noise;
                Gemini ±10–20 %, Kokoro a wider ±20–25 % — see ``augment`` /
                ``_kokoro_aug``).

Output
------
Every clip is written as the exact format train.py expects:
**16 kHz, mono, 16-bit PCM WAV**, ``DURATION`` seconds long (default 1.5 s, the
same window record_samples.py uses), with the spoken word trimmed of leading /
trailing silence and re-centred so it falls inside the verifier's 16-frame window.

  positives -> <out>/synthetic/positive/*.wav
  negatives -> <out>/synthetic/negative/*.wav

With Kokoro alone (~14 voices) the defaults yield ~294 positives + ~1000
negatives; ``--target-total`` (default 1000) prints the on-disk total against
that goal at the end.

Rate limiting & resume
----------------------
Gemini's free tier is request-limited, so a configurable delay (``--tts-delay``,
default 2 s) is inserted after every *network* synthesis (Kokoro/noise are local
and never delayed), with exponential back-off on 429/5xx. Generation is
**resumable**: filenames are deterministic and a base clip whose output files all
already exist is skipped *before* any API call — re-run the same command to pick
up where an interrupted run stopped.

Run ``python generate_dataset.py --help`` for all options. After generation the
script prints the exact ``train.py`` command to retrain the verifier.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
import time
from fractions import Fraction
from math import gcd

import numpy as np
import scipy.io.wavfile as wav
from scipy.signal import resample_poly

# Keep the project root importable so ``osia.*`` resolves when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from osia.config import get_api_key

# Reuse the canonical Gemini voice list / model ids / SR from osia.voice.tts so
# this stays in sync with the rest of OSIA (CLAUDE.md: do not duplicate provider
# config). Fall back to a local copy if the import ever fails (headless edge).
try:
    from osia.voice.tts import (
        GEMINI_VOICES,
        GEMINI_TTS_MODELS,
        GEMINI_TTS_SR,
    )
except Exception as _tts_exc:  # pragma: no cover - defensive only
    print(f"⚠️  could not import osia.voice.tts ({_tts_exc}); using built-in voice list")
    GEMINI_VOICES = [
        "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
        "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
        "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
        "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
        "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
    ]
    GEMINI_TTS_MODELS = ("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts")
    GEMINI_TTS_SR = 24000

# ── Constants ────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_RATE = 16000  # train.py / wake.py both require 16 kHz mono

# The wake word is "osia". Real users pronounce it three ways, so we render all
# three spellings — each *forces* a different sibilant so the positives span the
# whole pronunciation space instead of whatever a single voice happens to read:
#   • "ossia" — hard, unvoiced /s/ (the doubled s is unambiguous in FR and EN)
#   • "ocia"  — soft /s/ (a slightly softer sibilant realisation)
#   • "osia"  — voiced /z/ ("ozia"); some users naturally pronounce it this way,
#               so it is kept as a *deliberate* variant, not an accident.
# Each entry is (variant_name, tts_text); the variant name is baked into every
# positive filename so (a) resume stays deterministic and (b) every NEW positive
# carries a variant token (ossia/ocia/osia), which is exactly what distinguishes
# it from the OLD, untagged "pos_<voice>_<style>" clips the cleanup step removes.
POSITIVE_VARIANTS = [
    ("ossia", "ossia"),  # hard /s/
    ("ocia", "ocia"),    # soft /s/
    ("osia", "osia"),    # voiced /z/ — some users pronounce it this way
]

# Phonetically close to "osia" — the hard negatives that teach the verifier the
# precise boundary of the wake word.
NEG_PHONETIC = [
    "austria", "asia", "ostia", "osier", "au-delà",
    "aussi ça", "officiel", "océan", "oser", "associer",
]
# Common short FR/EN phrases — easy negatives covering everyday speech.
NEG_PHRASES = [
    "ok google", "hey siri", "bonjour", "hello",
    "qu'est-ce que", "what is",
]
# Extra hard negatives (WOR-37) — more near-misses plus everyday FR/EN phrases.
# Names/words ending in "-ia" are especially valuable: they share the wake
# word's "-ia" vowel tail and are exactly what the verifier is most likely to
# false-trigger on, so the boundary must be taught explicitly.
NEG_EXTRA = [
    "c'est ça", "au cas où", "associé", "officiellement", "Astoria",
    "Sophia", "Olivia", "anesthésia", "ambrosia", "Asia Minor",
    "in Asia", "et voilà", "on y va", "où ça", "OK",
    "merci", "bonjour à tous", "salut", "dis-moi", "écoute",
]
NEG_TEXTS = NEG_PHONETIC + NEG_PHRASES + NEG_EXTRA

# Kokoro voices (offline, free, no rate limit). Prefix → language/gender:
# a=US-English, b=UK-English, f=French; the 2nd char (f/m) is female/male. This
# is the full English + French Kokoro line-up — the languages relevant to the
# "osia" wake word. Voices are lazily downloaded from HuggingFace on first use
# and every synthesis is wrapped in try/except (see ``kokoro_tts``), so any voice
# that fails to download or load is skipped gracefully instead of aborting the
# run. Both positives and negatives use the whole set for maximum timbre variety.
KOKORO_VOICES = [
    "af_sky", "af_heart", "af_bella", "af_nicole", "af_sarah",  # US-EN female
    "am_michael", "am_adam", "am_eric", "am_onyx",              # US-EN male
    "bf_emma", "bf_isabella",                                   # UK-EN female
    "bm_george", "bm_lewis",                                    # UK-EN male
    "ff_siwis",                                                 # FR female
]
KOKORO_POS_VOICES = KOKORO_VOICES
KOKORO_NEG_VOICES = KOKORO_VOICES

NOISE_KINDS = ["silence", "white", "pink"]

# Gemini read-verbatim prompts. ``normal`` mirrors osia.voice.tts.gemini_tts_prompt;
# ``slow`` / ``fast`` add a pace instruction so the same voice yields varied timing.
STYLE_PROMPTS = {
    "normal": (
        "Read aloud exactly the following text, in its original language, "
        "without adding any words, translation, or commentary:\n\n{t}"
    ),
    "slow": (
        "Read aloud the following single word slowly and very clearly, "
        "drawing out each syllable, with no extra words or commentary:\n\n{t}"
    ),
    "fast": (
        "Read aloud the following single word quickly and briskly, "
        "with no extra words or commentary:\n\n{t}"
    ),
}
POS_STYLES = ["normal", "slow", "fast"]


# ── Signal processing helpers (numpy / scipy only) ───────────────────────────

def _resample(x: np.ndarray, sr_in: int, sr_out: int = SAMPLE_RATE) -> np.ndarray:
    if sr_in == sr_out:
        return x.astype(np.float32)
    g = gcd(sr_in, sr_out)
    return resample_poly(x, sr_out // g, sr_in // g).astype(np.float32)


def _to_mono_float(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if np.issubdtype(x.dtype, np.integer):
        x = x.astype(np.float32) / 32768.0
    return x.astype(np.float32)


def trim_silence(x: np.ndarray, margin_s: float = 0.05) -> np.ndarray:
    """Strip leading/trailing near-silence so the spoken word can be re-centred."""
    if x.size == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak < 1e-6:
        return x
    thresh = max(0.02, 0.04 * peak)
    voiced = np.where(np.abs(x) > thresh)[0]
    if voiced.size == 0:
        return x
    margin = int(margin_s * SAMPLE_RATE)
    start = max(0, voiced[0] - margin)
    end = min(len(x), voiced[-1] + margin)
    out = x[start:end]
    # Guard against pathological over-trim (e.g. all-quiet TTS).
    return out if out.size >= int(0.15 * SAMPLE_RATE) else x


def normalize_peak(x: np.ndarray, target: float = 0.7) -> np.ndarray:
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak < 1e-6:
        return x
    return (x * (target / peak)).astype(np.float32)


def fit_duration(x: np.ndarray, duration: float, center: bool = True) -> np.ndarray:
    """Pad (centred) or centre-crop ``x`` to exactly ``duration`` seconds."""
    n = int(round(duration * SAMPLE_RATE))
    if len(x) == n:
        return x.astype(np.float32)
    if len(x) > n:
        start = (len(x) - n) // 2 if center else 0
        return x[start:start + n].astype(np.float32)
    pad = n - len(x)
    left = pad // 2 if center else 0
    right = pad - left
    return np.concatenate(
        [np.zeros(left, np.float32), x.astype(np.float32), np.zeros(right, np.float32)]
    )


def _time_stretch(x: np.ndarray, rate: float) -> np.ndarray:
    """Overlap-add time-stretch (rate = out_len/in_len). Used by pitch_shift to
    change pitch without changing tempo. Lightweight, no phase vocoder."""
    if abs(rate - 1.0) < 1e-3 or len(x) < 2048:
        return x
    win = 1024
    hop_a = win // 4
    hop_s = max(1, int(round(hop_a * rate)))
    w = np.hanning(win).astype(np.float32)
    n_frames = 1 + (len(x) - win) // hop_a
    out_len = hop_s * (n_frames - 1) + win
    out = np.zeros(out_len, np.float32)
    wsum = np.zeros(out_len, np.float32)
    for i in range(n_frames):
        a = i * hop_a
        s = i * hop_s
        out[s:s + win] += x[a:a + win] * w
        wsum[s:s + win] += w
    wsum[wsum < 1e-6] = 1.0
    return out / wsum


def pitch_shift(x: np.ndarray, pct: float) -> np.ndarray:
    """Shift pitch by ``pct`` (e.g. +0.10 = up 10 %) keeping duration ~constant:
    time-stretch longer, then resample shorter."""
    if abs(pct) < 1e-3:
        return x
    factor = 1.0 + pct
    stretched = _time_stretch(x, factor)
    fr = Fraction(1.0 / factor).limit_denominator(100)
    return resample_poly(stretched, fr.numerator, fr.denominator).astype(np.float32)


def speed_change(x: np.ndarray, pct: float) -> np.ndarray:
    """Change tempo+pitch together by ``pct`` (e.g. +0.15 = 15 % faster)."""
    if abs(pct) < 1e-3:
        return x
    factor = 1.0 + pct
    fr = Fraction(1.0 / factor).limit_denominator(100)  # faster -> shorter
    return resample_poly(x, fr.numerator, fr.denominator).astype(np.float32)


def _white_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    w = rng.standard_normal(n).astype(np.float32)
    return w / (np.max(np.abs(w)) + 1e-9)


def _pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """1/f (pink) noise via FFT shaping of white noise."""
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    f = np.arange(1, spec.size + 1)
    spec = spec / np.sqrt(f)  # 1/f power -> 1/sqrt(f) amplitude
    pink = np.fft.irfft(spec, n=n).astype(np.float32)
    return pink / (np.max(np.abs(pink)) + 1e-9)


def augment(
    x: np.ndarray,
    rng: np.random.Generator,
    *,
    speed: float = 0.15,
    pitch: float = 0.10,
    vol: float = 0.20,
    noise_p: float = 0.7,
    noise_lo: float = 0.003,
    noise_hi: float = 0.02,
) -> np.ndarray:
    """Apply a random combination of the four augmentations to a speech clip.

    The ranges are keyword-tunable so callers can dial augmentation *depth* up
    per source. The defaults reproduce the original behaviour (speed ±15 %, pitch
    ±10 %, volume ±20 %, light noise) used for the Gemini positives; the Kokoro
    positives pass wider ranges (see ``_kokoro_aug``)."""
    y = x.astype(np.float32)
    if rng.random() < 0.8:
        y = speed_change(y, rng.uniform(-speed, speed))
    if rng.random() < 0.8:
        y = pitch_shift(y, rng.uniform(-pitch, pitch))
    y = normalize_peak(y, 0.7)
    if rng.random() < noise_p:  # background-noise injection at varying levels
        noise = (_pink_noise if rng.random() < 0.5 else _white_noise)(len(y), rng)
        y = y + noise * rng.uniform(noise_lo, noise_hi)
    y = y * (1.0 + rng.uniform(-vol, vol))  # volume variation ±vol
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def _kokoro_aug(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Deeper augmentation for Kokoro positives (``--kokoro-aug`` copies): wider
    pitch/speed/volume swings (±20 %, ±20 %, ±25 %) and a touch more noise.
    Kokoro contributes fewer base timbres than Gemini, so each base clip is
    augmented more heavily to compensate."""
    return augment(
        x, rng,
        speed=0.20, pitch=0.20, vol=0.25,
        noise_p=0.85, noise_lo=0.003, noise_hi=0.03,
    )


def write_wav(path: str, x: np.ndarray) -> None:
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)
    wav.write(path, SAMPLE_RATE, pcm)


def _seed_for(stem: str) -> int:
    """Deterministic per-clip RNG seed (resume-safe, independent of PYTHONHASHSEED)."""
    return int(hashlib.md5(stem.encode("utf-8")).hexdigest()[:8], 16)


# ── TTS back-ends ────────────────────────────────────────────────────────────

class _RateLimited(Exception):
    """All Gemini models returned a retriable status (429/5xx)."""


def _gemini_once(text: str, voice: str, api_key: str):
    """One Gemini synthesis attempt. Returns (float32_mono, sr) or None.
    Raises _RateLimited when every model failed with a retriable status."""
    import requests

    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
            },
        },
    }
    retriable = False
    last_err = None
    for model in GEMINI_TTS_MODELS:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            parts = data["candidates"][0]["content"]["parts"]
            b64 = next(
                (p["inlineData"]["data"] for p in parts if "inlineData" in p), None
            )
            if not b64:
                last_err = "no audio in response (safety block?)"
                continue
            pcm = base64.b64decode(b64)
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            return samples, GEMINI_TTS_SR
        except requests.HTTPError as e:  # type: ignore[attr-defined]
            code = getattr(e.response, "status_code", None)
            last_err = f"HTTP {code}"
            if code in (429, 500, 502, 503, 504):
                retriable = True
        except Exception as e:
            last_err = str(e)
    if retriable:
        raise _RateLimited(str(last_err))
    print(f"      ⚠️  gemini failed ({voice}): {last_err}")
    return None


def gemini_tts(text: str, voice: str, api_key: str, base_delay: float, max_retries: int = 5):
    """Gemini synthesis with exponential back-off on rate limits."""
    attempt = 0
    while True:
        try:
            return _gemini_once(text, voice, api_key)
        except _RateLimited as e:
            attempt += 1
            if attempt > max_retries:
                print(f"      ⚠️  gemini gave up after {max_retries} retries ({e})")
                return None
            wait = min(60.0, max(base_delay, 2.0) * (2 ** attempt))
            print(f"      ⏳ rate-limited ({e}) — backing off {wait:.0f}s "
                  f"(attempt {attempt}/{max_retries})")
            time.sleep(wait)


_kokoro_pipelines: dict = {}


def kokoro_tts(text: str, voice: str, speed: float = 1.0):
    """Local Kokoro synthesis. Returns (float32_mono, sr) or None on failure."""
    try:
        from kokoro import KPipeline
    except Exception as e:
        print(f"      ⚠️  kokoro unavailable: {e}")
        return None
    lang_code = voice[0]  # a=US-EN, b=UK-EN, f=FR ...
    try:
        if lang_code not in _kokoro_pipelines:
            _kokoro_pipelines[lang_code] = KPipeline(lang_code=lang_code)
        pipe = _kokoro_pipelines[lang_code]
        segs = [a for _, _, a in pipe(text, voice=voice, speed=speed) if a is not None]
        if not segs:
            return None
        samples = np.concatenate([np.asarray(s, dtype=np.float32) for s in segs])
        return samples, 24000
    except Exception as e:
        print(f"      ⚠️  kokoro failed ({voice}): {e}")
        return None


# ── Generation driver ────────────────────────────────────────────────────────

class Stats:
    def __init__(self):
        self.written = 0
        self.skipped = 0
        self.failed = 0
        self.gemini_calls = 0


def _variant_paths(out_dir: str, stem: str, n_aug: int) -> list[str]:
    """variant 0 = clean original, 1..n_aug = augmented copies."""
    return [os.path.join(out_dir, f"{stem}_{i:02d}.wav") for i in range(n_aug + 1)]


def _emit(out_dir, stem, fetch, n_aug, duration, is_noise, stats, args, *, aug=None):
    """Resume-safe emission of one base clip + its augmented variants.

    ``fetch`` is a zero-arg callable returning (float32_mono, sr) or None. It is
    only invoked when at least one target file is missing, so resuming never
    re-hits the TTS API for already-generated clips. ``aug`` is an optional
    ``(x, rng) -> y`` augmentation callable for the speech variants (defaults to
    the standard :func:`augment`); pass :func:`_kokoro_aug` for deeper Kokoro
    augmentation. Returns True if a *network* synthesis actually happened (caller
    applies the rate-limit delay)."""
    aug_fn = aug if aug is not None else augment
    paths = _variant_paths(out_dir, stem, n_aug)
    if all(os.path.exists(p) for p in paths):
        stats.skipped += len(paths)
        return False
    audio = fetch()
    if audio is None:
        stats.failed += 1
        return False
    samples, sr = audio
    x = _resample(_to_mono_float(samples), sr)
    if not is_noise:
        x = trim_silence(x)
    rng = np.random.default_rng(_seed_for(stem))
    for idx, p in enumerate(paths):
        if os.path.exists(p):
            continue
        if is_noise:
            # Preserve the fetched amplitude (silence stays quiet); only the
            # augmented variants add a fresh noise layer + volume variation.
            y = fit_duration(x, duration, center=False)
            if idx > 0:
                extra = (_pink_noise if rng.random() < 0.5 else _white_noise)(
                    int(duration * SAMPLE_RATE), rng
                )
                y = y + extra * rng.uniform(0.01, 0.08)
                y = y * (1.0 + rng.uniform(-0.2, 0.2))
                y = np.clip(y, -1.0, 1.0).astype(np.float32)
        elif idx == 0:
            y = fit_duration(normalize_peak(x, 0.7), duration, center=True)
        else:
            y = fit_duration(aug_fn(x, rng), duration, center=True)
        write_wav(p, y)
        stats.written += 1
    return True


def _maybe_delay(used_network: bool, delay: float):
    if used_network and delay > 0:
        time.sleep(delay)


def generate(args):
    pos_dir = args.positive_dir
    neg_dir = args.negative_dir
    os.makedirs(pos_dir, exist_ok=True)
    os.makedirs(neg_dir, exist_ok=True)

    api_key = get_api_key("google")
    use_gemini = (not args.no_gemini) and bool(api_key)
    if not args.no_gemini and not api_key:
        print("⚠️  No Google API key found — skipping Gemini, using Kokoro + noise only.")

    # Resolve voice selections (smoke preset shrinks everything).
    if args.smoke:
        gem_pos_voices = GEMINI_VOICES[:1]
        gem_neg_voices = GEMINI_VOICES[1:2]
        kok_pos_voices = KOKORO_POS_VOICES[:1]
        kok_neg_voices = KOKORO_NEG_VOICES[:1]
        neg_texts = NEG_TEXTS[:3]
        noise_per_kind = 2
        pos_aug, neg_aug, noise_aug, kok_aug = 1, 1, 1, 1
        pos_styles = POS_STYLES
    else:
        if args.gemini_pos_voices.strip().lower() == "all":
            gem_pos_voices = list(GEMINI_VOICES)
        else:
            gem_pos_voices = GEMINI_VOICES[: int(args.gemini_pos_voices)]
        gem_neg_voices = GEMINI_VOICES[: args.gemini_neg_voices]
        kok_pos_voices = [] if args.no_kokoro else list(KOKORO_POS_VOICES)
        if str(args.kokoro_neg_voices).strip().lower() == "all":
            kok_neg_voices = [] if args.no_kokoro else list(KOKORO_NEG_VOICES)
        else:
            kok_neg_voices = (
                [] if args.no_kokoro
                else KOKORO_NEG_VOICES[: int(args.kokoro_neg_voices)]
            )
        neg_texts = NEG_TEXTS
        noise_per_kind = args.noise_per_kind
        pos_aug, neg_aug, noise_aug = args.pos_aug, args.neg_aug, args.noise_aug
        kok_aug = args.kokoro_aug
        pos_styles = POS_STYLES
    if args.no_kokoro:
        kok_pos_voices, kok_neg_voices = [], []
    if not use_gemini:
        gem_pos_voices, gem_neg_voices = [], []

    stats = Stats()
    delay = args.tts_delay

    def _cap_ok() -> bool:
        return args.max_tts_calls <= 0 or stats.gemini_calls < args.max_tts_calls

    print("=" * 64)
    print("  OSIA Wake Word — Synthetic Multi-Voice Dataset Generator (WOR-37)")
    print("=" * 64)
    print(f"  Gemini positives : {len(gem_pos_voices)} voices × {len(POSITIVE_VARIANTS)} variants × {len(pos_styles)} styles")
    print(f"  Gemini negatives : {len(gem_neg_voices)} voices × {len(neg_texts)} texts")
    print(f"  Kokoro positives : {len(kok_pos_voices)} voices × {len(POSITIVE_VARIANTS)} variants  (+{kok_aug} aug each)")
    print(f"  Kokoro negatives : {len(kok_neg_voices)} voices × {len(neg_texts)} texts")
    print(f"  Noise negatives  : {len(NOISE_KINDS)} kinds × {noise_per_kind}")
    print(f"  Aug copies       : gem-pos+{pos_aug}, kok-pos+{kok_aug}, neg+{neg_aug}, noise+{noise_aug}")
    print(f"  TTS delay        : {delay}s   max Gemini calls: "
          f"{args.max_tts_calls or 'unlimited'}")
    print(f"  Positive dir     : {pos_dir}")
    print(f"  Negative dir     : {neg_dir}")
    print("=" * 64)

    # ── POSITIVES — Gemini (all voices × ossia/ocia variants × normal/slow/fast) ──
    for vi, voice in enumerate(gem_pos_voices):
        for variant, text in POSITIVE_VARIANTS:
            for style in pos_styles:
                if not _cap_ok():
                    print("   ⚠️  max Gemini call cap reached — stopping Gemini positives.")
                    break
                stem = f"pos_gem_{voice}_{variant}_{style}"
                prompt = STYLE_PROMPTS[style].format(t=text)

                def fetch(p=prompt, v=voice):
                    stats.gemini_calls += 1
                    return gemini_tts(p, v, api_key, delay)

                used = _emit(pos_dir, stem, fetch, pos_aug, args.duration, False, stats, args)
                print(f"  [pos/gem {vi + 1}/{len(gem_pos_voices)}] {voice}/{variant}/{style} "
                      f"-> written={stats.written} skipped={stats.skipped} failed={stats.failed}")
                _maybe_delay(used, delay)

    # ── POSITIVES — Kokoro (each voice × all 3 variants, deeply augmented) ──
    for vi, voice in enumerate(kok_pos_voices):
        for variant, text in POSITIVE_VARIANTS:
            stem = f"pos_kok_{voice}_{variant}_normal"

            def fetch(v=voice, t=text):
                return kokoro_tts(t, v, speed=1.0)

            _emit(pos_dir, stem, fetch, kok_aug, args.duration, False, stats, args,
                  aug=_kokoro_aug)
            print(f"  [pos/kok {vi + 1}/{len(kok_pos_voices)}] {voice}/{variant} "
                  f"-> written={stats.written} skipped={stats.skipped} failed={stats.failed}")

    # ── NEGATIVES — Gemini (phonetic + phrases) ──
    for vi, voice in enumerate(gem_neg_voices):
        for text in neg_texts:
            if not _cap_ok():
                print("   ⚠️  max Gemini call cap reached — stopping Gemini negatives.")
                break
            stem = f"neg_gem_{voice}_{_slug(text)}"
            prompt = STYLE_PROMPTS["normal"].format(t=text)

            def fetch(p=prompt, v=voice):
                stats.gemini_calls += 1
                return gemini_tts(p, v, api_key, delay)

            used = _emit(neg_dir, stem, fetch, neg_aug, args.duration, False, stats, args)
            _maybe_delay(used, delay)
        print(f"  [neg/gem {vi + 1}/{len(gem_neg_voices)}] {voice} "
              f"-> written={stats.written} skipped={stats.skipped} failed={stats.failed}")

    # ── NEGATIVES — Kokoro ──
    for vi, voice in enumerate(kok_neg_voices):
        for text in neg_texts:
            stem = f"neg_kok_{voice}_{_slug(text)}"

            def fetch(t=text, v=voice):
                return kokoro_tts(t, v, speed=1.0)

            _emit(neg_dir, stem, fetch, neg_aug, args.duration, False, stats, args)
        print(f"  [neg/kok {vi + 1}/{len(kok_neg_voices)}] {voice} "
              f"-> written={stats.written} skipped={stats.skipped} failed={stats.failed}")

    # ── NEGATIVES — silence / white / pink noise (numpy) ──
    for kind in NOISE_KINDS:
        for i in range(noise_per_kind):
            stem = f"neg_noise_{kind}_{i:03d}"

            def fetch(k=kind, idx=i, st=stem):
                rng = np.random.default_rng(_seed_for(st) ^ 0x5EED)
                n = int(args.duration * SAMPLE_RATE)
                if k == "silence":
                    base = rng.standard_normal(n).astype(np.float32) * rng.uniform(0.0005, 0.003)
                elif k == "white":
                    base = _white_noise(n, rng) * rng.uniform(0.02, 0.12)
                else:  # pink
                    base = _pink_noise(n, rng) * rng.uniform(0.02, 0.12)
                return base, SAMPLE_RATE

            _emit(neg_dir, stem, fetch, noise_aug, args.duration, True, stats, args)
        print(f"  [neg/noise] {kind} ×{noise_per_kind} "
              f"-> written={stats.written} skipped={stats.skipped} failed={stats.failed}")

    # ── Summary ──
    n_pos = len([f for f in os.listdir(pos_dir) if f.endswith(".wav")])
    n_neg = len([f for f in os.listdir(neg_dir) if f.endswith(".wav")])
    n_total = n_pos + n_neg
    goal = args.target_total
    print("\n" + "=" * 64)
    print("  Generation complete.")
    print(f"  Written this run : {stats.written}   skipped (already present): "
          f"{stats.skipped}   failed: {stats.failed}")
    print(f"  Gemini API calls : {stats.gemini_calls}")
    print(f"  Positive WAVs on disk : {n_pos}  ({pos_dir})")
    print(f"  Negative WAVs on disk : {n_neg}  ({neg_dir})")
    print(f"  TOTAL on disk    : {n_total}  =  {n_pos} pos + {n_neg} neg")
    if goal > 0:
        pct = 100.0 * n_total / goal
        status = "✅ reached" if n_total >= goal else "⏳ below target"
        print(f"  Target total     : {n_total}/{goal}  ({pct:.0f}% of goal)  {status}")
    print("=" * 64)
    print("\nNext — retrain the verifier on this dataset:\n")
    print(f"  venv/bin/python osia/wake_training/train.py \\")
    print(f"      --positive-dir {pos_dir} \\")
    print(f"      --negative-dir {neg_dir}")
    print("\n  (add the personal recording dirs too for extra mic robustness:)")
    print(f"  venv/bin/python osia/wake_training/train.py \\")
    print(f"      --positive-dir {pos_dir} {os.path.join(BASE_DIR, 'positive')} \\")
    print(f"      --negative-dir {neg_dir} {os.path.join(BASE_DIR, 'negative')}\n")

    # ── Clean up the old, mispronounced positives ──
    # Pre-fix positives were synthesized from a bare "osia", which TTS reads as
    # "ozia" (voiced s) — they no longer match the wake word. Every *new* positive
    # filename carries a variant token (ossia/ocia); the old ones don't, so that
    # absence is the discriminator. The command is scoped to synthetic 'pos_*.wav'
    # only, so personal mic recordings ('osia_*.wav' from record_samples.py) are
    # left untouched. Build the exclusions from POSITIVE_VARIANTS so they stay
    # in sync if the variant list changes.
    find_excl = " ".join(f"! -name '*_{name}_*'" for name, _ in POSITIVE_VARIANTS)
    ps_re = "|".join(name for name, _ in POSITIVE_VARIANTS)
    print("=" * 64)
    print("Old positives synthesized from the bare \"osia\" text are mispronounced")
    print('("ozia") and should be deleted so they don\'t dilute the retrain. They')
    print("are the synthetic positives with NO variant token in their filename")
    print("(personal 'osia_*.wav' mic recordings are NOT matched). Preview, then run:\n")
    print(f"  find {pos_dir} -type f -name 'pos_*.wav' {find_excl} -print")
    print(f"  find {pos_dir} -type f -name 'pos_*.wav' {find_excl} -delete")
    print("\n  # Windows PowerShell equivalent:")
    print(f"  Get-ChildItem '{pos_dir}' -Filter pos_*.wav | "
          f"Where-Object {{ $_.Name -notmatch '_({ps_re})_' }} | Remove-Item")
    print("=" * 64)


def _slug(text: str) -> str:
    # ASCII-only token so filenames are safe on every OS (WOR-11 Windows encoding).
    keep = "".join(c if (c.isascii() and c.isalnum()) else "_" for c in text.lower())
    while "__" in keep:
        keep = keep.replace("__", "_")
    return keep.strip("_")[:24]


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Generate a synthetic multi-voice 'osia' wake-word dataset.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    default_pos = os.path.join(BASE_DIR, "synthetic", "positive")
    default_neg = os.path.join(BASE_DIR, "synthetic", "negative")
    p.add_argument("--positive-dir", default=default_pos, help="output dir for positive WAVs")
    p.add_argument("--negative-dir", default=default_neg, help="output dir for negative WAVs")
    p.add_argument("--tts-delay", type=float, default=2.0,
                   help="seconds to wait after each Gemini (network) synthesis")
    p.add_argument("--duration", type=float, default=1.5, help="clip length in seconds")
    p.add_argument("--gemini-pos-voices", default="all",
                   help="'all' (~30) or an integer count of Gemini voices for positives")
    p.add_argument("--gemini-neg-voices", type=int, default=8,
                   help="number of Gemini voices used for spoken negatives")
    p.add_argument("--kokoro-neg-voices", default="all",
                   help="'all' or an integer count of Kokoro voices for spoken negatives")
    p.add_argument("--noise-per-kind", type=int, default=16,
                   help="base noise clips per kind (silence/white/pink)")
    p.add_argument("--pos-aug", type=int, default=2,
                   help="augmented copies per Gemini positive base")
    p.add_argument("--kokoro-aug", type=int, default=6,
                   help="augmented copies per Kokoro positive base (deeper aug to "
                        "offset Kokoro's smaller timbre pool)")
    p.add_argument("--neg-aug", type=int, default=1, help="augmented copies per spoken-negative base")
    p.add_argument("--noise-aug", type=int, default=2, help="augmented copies per noise base")
    p.add_argument("--target-total", type=int, default=1000,
                   help="report positives+negatives on disk against this goal "
                        "(0 = don't show a target)")
    p.add_argument("--max-tts-calls", type=int, default=0,
                   help="hard cap on Gemini calls this run (0 = unlimited)")
    p.add_argument("--no-gemini", action="store_true", help="skip Gemini TTS entirely")
    p.add_argument("--no-kokoro", action="store_true", help="skip Kokoro TTS entirely")
    p.add_argument("--smoke", action="store_true",
                   help="tiny end-to-end test run (~6 Gemini calls)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    generate(args)


if __name__ == "__main__":
    main()
