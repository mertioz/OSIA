#!/usr/bin/env python3
"""
Train a custom openWakeWord verifier model for the wake word "osia".

Uses openwakeword's feature extraction + sklearn logistic regression
to create a verifier that distinguishes "osia" from other speech.

Since "osia" doesn't trigger any pretrained openWakeWord model, we:
1. Extract mel spectrogram features from all audio using openWakeWord's preprocessor
2. Train a logistic regression classifier on those features
3. Save as a pickle file compatible with openWakeWord's custom_verifier_models

The verifier is used as a second stage: the base model (alexa) fires broadly,
and the verifier confirms whether the activation was actually "osia".

Requires:
  - Positive samples: wake_training/positive/*.wav (recordings of "osia")
  - Negative samples: wake_training/negative/*.wav (recordings of anything else)

Output:
  - wake_training/osia_verifier.pkl  (scikit-learn pipeline)
"""

import os
import sys
import glob
import pickle
import collections
import numpy as np
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import openwakeword
from openwakeword.custom_verifier_model import train_verifier_model

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POSITIVE_DIR = os.path.join(BASE_DIR, "positive")
NEGATIVE_DIR = os.path.join(BASE_DIR, "negative")
OUTPUT_PATH = os.path.join(BASE_DIR, "osia_verifier.pkl")

# Use the alexa model as the base — it provides the feature extractor.
# The model_inputs dict tells us the expected window size for each model.
BASE_MODEL = "alexa"


def extract_features_from_clip(
    clip_path: str,
    oww_model: openwakeword.Model,
    model_name: str,
    N: int = 3,
) -> np.ndarray:
    """
    Extract mel spectrogram features from an audio clip using openWakeWord's
    preprocessor, regardless of whether the base model fires.

    Unlike openWakeWord's get_reference_clip_features, this captures features
    from ALL chunks, not just those that trigger the base model.
    """
    import scipy.io.wavfile as wav

    sr, dat = wav.read(clip_path)
    if dat.dtype != np.int16:
        dat = dat.astype(np.int16)

    positive_data = []

    for _ in range(N):
        # Slight random offset for augmentation
        offset = np.random.randint(0, min(1280, max(1, len(dat) - 1280)))
        dat_offset = dat[offset:]

        step_size = 1280
        for i in range(0, len(dat_offset) - step_size, step_size):
            chunk = dat_offset[i:i + step_size]
            if len(chunk) < step_size:
                continue
            # Feed chunk through model to update internal feature buffer
            oww_model.predict(chunk)
            # Now extract the features from the preprocessor
            try:
                features = oww_model.preprocessor.get_features(
                    oww_model.model_inputs[model_name]
                )
                if features is not None and features.size > 0:
                    positive_data.append(features)
            except Exception:
                continue

    if len(positive_data) == 0:
        return np.empty((0, oww_model.model_inputs[model_name], 96))
    return np.vstack(positive_data)


def main():
    positive_clips = sorted(glob.glob(os.path.join(POSITIVE_DIR, "*.wav")))
    negative_clips = sorted(glob.glob(os.path.join(NEGATIVE_DIR, "*.wav")))

    if not positive_clips:
        print(f"❌ No positive WAV files found in {POSITIVE_DIR}")
        print("   Run record_samples.py first to record 'osia' samples.")
        sys.exit(1)

    if not negative_clips:
        print(f"❌ No negative WAV files found in {NEGATIVE_DIR}")
        print("   Run record_negative.py first to record non-'osia' samples.")
        sys.exit(1)

    print("=" * 60)
    print("  OSIA Wake Word — Custom Verifier Training")
    print(f"  Feature extractor: {BASE_MODEL}")
    print(f"  Positive clips: {len(positive_clips)}")
    print(f"  Negative clips: {len(negative_clips)}")
    print(f"  Output: {OUTPUT_PATH}")
    print("=" * 60)

    # Load openWakeWord model (includes feature extractor + all pretrained models)
    oww = openwakeword.Model()

    # Extract features from positive clips
    print("\nExtracting features from positive clips...")
    positive_features = []
    for clip in tqdm(positive_clips, desc="Positive"):
        feats = extract_features_from_clip(clip, oww, BASE_MODEL, N=3)
        if feats.shape[0] > 0:
            positive_features.append(feats)
    oww.reset()

    if not positive_features:
        print("❌ No positive features extracted. Check your audio files.")
        sys.exit(1)
    positive_features = np.vstack(positive_features)
    print(f"   Positive feature vectors: {positive_features.shape[0]}")

    # Extract features from negative clips
    print("\nExtracting features from negative clips...")
    negative_features = []
    for clip in tqdm(negative_clips, desc="Negative"):
        feats = extract_features_from_clip(clip, oww, BASE_MODEL, N=1)
        if feats.shape[0] > 0:
            negative_features.append(feats)
    oww.reset()

    if not negative_features:
        print("❌ No negative features extracted. Check your audio files.")
        sys.exit(1)
    negative_features = np.vstack(negative_features)
    print(f"   Negative feature vectors: {negative_features.shape[0]}")

    # Create labels
    X = np.vstack((positive_features, negative_features))
    y = np.array(
        [1] * positive_features.shape[0] + [0] * negative_features.shape[0]
    )

    print(f"\nTotal training samples: {X.shape[0]}")
    print(f"  Positive (osia): {positive_features.shape[0]}")
    print(f"  Negative (other): {negative_features.shape[0]}")

    # Train logistic regression
    print("\nTraining logistic regression classifier...")
    lr_model = train_verifier_model(X, y)

    # Save model
    with open(OUTPUT_PATH, "wb") as f:
        pickle.dump(lr_model, f)

    print(f"\n✅ Training complete!")
    print(f"   Model saved to: {OUTPUT_PATH}")
    size_kb = os.path.getsize(OUTPUT_PATH) / 1024
    print(f"   File size: {size_kb:.1f} KB")

    # Quick validation
    from sklearn.metrics import accuracy_score
    y_pred = lr_model.predict(X)
    acc = accuracy_score(y, y_pred)
    print(f"   Training accuracy: {acc:.2%}")


if __name__ == "__main__":
    main()
