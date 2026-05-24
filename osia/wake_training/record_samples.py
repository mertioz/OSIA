#!/usr/bin/env python3
"""
Record positive training samples for "osia" wake word.
Records 70 audio samples at 16kHz mono, 1.5 seconds each.
Saves as 16-bit WAV files in wake_training/positive/.
"""

import os
import sys
import time
import sounddevice as sd
import scipy.io.wavfile as wav

SAMPLE_RATE = 16000
DURATION = 1.5  # seconds
NUM_SAMPLES = 70

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positive")


def record_sample(filename: str):
    """Record a single 1.5s sample at 16kHz mono."""
    print(f"\n  🎤 3... ", end="", flush=True)
    time.sleep(1)
    print("2... ", end="", flush=True)
    time.sleep(1)
    print("1... ", end="", flush=True)
    time.sleep(1)
    print("🗣️ SAY 'OSIA' NOW!")
    
    audio = sd.rec(
        int(DURATION * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
    )
    sd.wait()
    
    wav.write(filename, SAMPLE_RATE, audio)
    print(f"     ✅ Saved: {os.path.basename(filename)}")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    print("=" * 60)
    print("  OSIA Wake Word — Positive Sample Recorder")
    print(f"  Will record {NUM_SAMPLES} samples of you saying 'osia'")
    print(f"  Sample rate: {SAMPLE_RATE} Hz, Duration: {DURATION}s")
    print(f"  Output: {OUTPUT_DIR}")
    print("=" * 60)
    print()
    print("Instructions:")
    print("  - Say 'osia' clearly and naturally when prompted")
    print("  - Vary your tone, speed, and volume slightly")
    print("  - Stay in a quiet environment")
    print()
    input("Press Enter to begin...")
    
    for i in range(1, NUM_SAMPLES + 1):
        print(f"\n[{i}/{NUM_SAMPLES}]", end="")
        filename = os.path.join(OUTPUT_DIR, f"osia_{i:03d}.wav")
        try:
            record_sample(filename)
        except KeyboardInterrupt:
            print("\n\nRecording interrupted.")
            break
        except Exception as e:
            print(f"     ❌ Error: {e}")
            continue
    
    print("\n" + "=" * 60)
    print(f"  Recording complete! {i} samples saved.")
    print(f"  Files in: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
