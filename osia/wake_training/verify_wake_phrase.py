"""Verify the packaged wake engine with synthetic French speech/public noise.

This explicit developer tool never opens a microphone. Generated WAVs/reports
belong outside Git. Kokoro is used for test synthesis, not by the wake engine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from osia.voice.acoustic import CHUNK_SIZE, SAMPLE_RATE, WINDOW_SAMPLES, KeywordScorer

POSITIVE = ["Hé Ossia.", "Hé Ocia.", "Hey Ossia !", "Hé Ossia, bonjour."]
NEGATIVE = [
    "Ossia.", "Ocia.", "Océan.", "Osier.", "Sophia.", "Asia.",
    "Hé Sophia.", "Hé Asia.", "Hé Océan.", "Hé osier.", "Hé, aussi ça.",
    "Et aussi à Lyon.", "Et aussi ailleurs.", "Hé, on y va.",
    "Bonjour, voici les informations demandées.", "Nous allons commencer maintenant.",
    "Le navigateur est ouvert.", "Je ne sais pas.", "Merci beaucoup.",
]


def observe(scorer, audio):
    pcm = (np.clip(np.pad(audio, (8000, 24000)), -1, 1) * 32767).astype(np.int16)
    scorer.reset()
    timings, detected, first = [], False, None
    for end in range(CHUNK_SIZE, len(pcm) + 1, CHUNK_SIZE):
        window = pcm[max(0, end - WINDOW_SAMPLES):end]
        started = time.perf_counter()
        score = scorer.score(window)
        timings.append(time.perf_counter() - started)
        if score >= scorer.threshold:
            detected = True
            first = end / SAMPLE_RATE
            break
    return {"detected": detected, "first_seconds": first}, timings


def verify(output: Path, noise_dir: Path | None):
    output.mkdir(parents=True, exist_ok=True)
    scorer = KeywordScorer()
    from kokoro import KPipeline
    pipeline = KPipeline(lang_code="f", repo_id="hexgrad/Kokoro-82M", device="cpu")
    results, timings = [], []
    for label, phrases in [(1, POSITIVE), (0, NEGATIVE)]:
        for phrase in phrases:
            for rate in [0.85, 1.0, 1.15]:
                key = hashlib.sha256(phrase.encode()).hexdigest()[:10]
                path = output / f"speech-{label}-{key}-{rate}.wav"
                if not path.exists():
                    audio = np.concatenate([r.audio.numpy() for r in pipeline(phrase, voice="ff_siwis", speed=rate)])
                    sf.write(path, audio, 24000)
                audio, _ = sf.read(path, dtype="float32")
                audio = resample_poly(audio, 2, 3)
                result, elapsed = observe(scorer, audio)
                timings.extend(elapsed)
                results.append({"label": label, "phrase": phrase, "rate": rate, **result})
    noise_seconds = 0.0
    if noise_dir:
        for path in sorted(noise_dir.glob("*.wav")):
            audio, sr = sf.read(path, dtype="float32", always_2d=True)
            audio = audio.mean(axis=1)
            if sr != SAMPLE_RATE:
                from scipy.signal import resample
                audio = resample(audio, round(len(audio) * SAMPLE_RATE / sr)).astype(np.float32)
            result, elapsed = observe(scorer, audio)
            timings.extend(elapsed)
            noise_seconds += len(audio) / SAMPLE_RATE
            results.append({"label": 0, "file": path.name, "seconds": len(audio)/SAMPLE_RATE, **result})
            print(f"Checked public noise {path.name}", flush=True)
    positives = [r for r in results if r["label"]]
    negatives = [r for r in results if not r["label"]]
    summary = {
        "true_positive": sum(r["detected"] for r in positives), "positive_total": len(positives),
        "false_positive": sum(r["detected"] for r in negatives), "negative_total": len(negatives),
        "public_noise_seconds": noise_seconds,
        "frame_mean_ms": float(np.mean(timings)*1000), "frame_p95_ms": float(np.percentile(timings,95)*1000),
        "scope": "Synthetic French ff_siwis; public noise if supplied. Real microphone/voice acceptance pending.",
    }
    (output / "verification.json").write_text(json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--noise-dir", type=Path)
    args = parser.parse_args()
    verify(args.output, args.noise_dir)
