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
import shutil
import pickle
import argparse
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
# The personal (single-voice) verifier is preserved here the first time the
# shipped model is overwritten (WOR-37), so the original is never lost.
BACKUP_PATH = os.path.join(BASE_DIR, "osia_verifier_backup_personal.pkl")

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


def gather_clips(dirs):
    """Collect *.wav from one or more directories (order-stable, de-duplicated)."""
    clips, seen = [], set()
    for d in dirs:
        for c in sorted(glob.glob(os.path.join(d, "*.wav"))):
            rp = os.path.realpath(c)
            if rp not in seen:
                seen.add(rp)
                clips.append(c)
    return clips


def extract_per_clip(clips, oww, n_passes, desc):
    """Extract features grouped per clip (so train/test can split by clip, not by
    frame — frames from one clip never straddle the split, avoiding leakage)."""
    per_clip = []
    for clip in tqdm(clips, desc=desc):
        feats = extract_features_from_clip(clip, oww, BASE_MODEL, N=n_passes)
        if feats.shape[0] > 0:
            per_clip.append(feats)
    return per_clip


def split_indices(n, frac, rng):
    """Shuffle [0..n) into (train, test); guarantees ≥1 test clip when n≥2."""
    idx = np.arange(n)
    rng.shuffle(idx)
    n_test = max(1, int(round(n * frac))) if n >= 2 else 0
    return idx[n_test:], idx[:n_test]


def evaluate_and_recommend(model, pos_test, neg_test):
    """Print frame- and clip-level test metrics over a threshold sweep and
    return a recommended verifier threshold.

    Frame metrics: per 16×96 feature window. Clip metrics: a clip's score is the
    max positive-probability over its frames (an optimistic 'could it fire' proxy;
    wake.py adds an 8-frame patience on top at runtime). We recommend the lowest
    threshold that keeps the negative-clip false-fire rate ≤ 2 %, maximising
    positive-clip detection.
    """
    from sklearn.metrics import precision_score, recall_score, f1_score

    if not pos_test or not neg_test:
        print("   ⚠️  Not enough clips for a held-out split — skipping test metrics.")
        return None

    Xp = np.vstack(pos_test)
    Xn = np.vstack(neg_test)
    X = np.vstack((Xp, Xn))
    y = np.array([1] * len(Xp) + [0] * len(Xn))
    proba = model.predict_proba(X)[:, 1]

    pos_clip_scores = np.array([model.predict_proba(c)[:, 1].max() for c in pos_test])
    neg_clip_scores = np.array([model.predict_proba(c)[:, 1].max() for c in neg_test])

    print("\n  Held-out test set:")
    print(f"    positive clips: {len(pos_test)} ({len(Xp)} frames)")
    print(f"    negative clips: {len(neg_test)} ({len(Xn)} frames)")
    print("\n  thr | frame-acc  prec  recall   f1 | clip-detect  clip-FP")
    print("  ----+--------------------------------+--------------------")

    grid = sorted(set([round(t, 2) for t in np.arange(0.30, 0.96, 0.05)] + [0.85]))
    best = None
    for t in grid:
        pred = (proba >= t).astype(int)
        acc = float((pred == y).mean())
        prec = precision_score(y, pred, zero_division=0)
        rec = recall_score(y, pred, zero_division=0)
        f1 = f1_score(y, pred, zero_division=0)
        clip_detect = float((pos_clip_scores >= t).mean())
        clip_fp = float((neg_clip_scores >= t).mean())
        flag = " <-" if abs(t - 0.85) < 1e-9 else ""
        print(f"  {t:.2f}| {acc:8.2%}  {prec:.2f}  {rec:5.2f}  {f1:.2f} |"
              f"   {clip_detect:7.2%}  {clip_fp:6.2%}{flag}")
        if clip_fp <= 0.02 and (best is None or clip_detect > best[1]):
            best = (t, clip_detect, clip_fp)

    if best is None:  # nothing hit the ≤2% FP bar — fall back to best margin
        margins = [(t, float((pos_clip_scores >= t).mean()) - float((neg_clip_scores >= t).mean()))
                   for t in grid]
        t = max(margins, key=lambda m: m[1])[0]
        print(f"\n  ⚠️  No threshold reached ≤2% clip false-fire; best-margin = {t:.2f}")
        return t
    print(f"\n  ✅ Recommended threshold: {best[0]:.2f}  "
          f"(clip detect {best[1]:.0%}, clip false-fire {best[2]:.0%})")
    return best[0]


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Train the custom 'osia' wake-word verifier.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--positive-dir", nargs="+", default=[POSITIVE_DIR],
                   help="one or more dirs of positive ('osia') WAVs")
    p.add_argument("--negative-dir", nargs="+", default=[NEGATIVE_DIR],
                   help="one or more dirs of negative (not-'osia') WAVs")
    p.add_argument("--output", default=OUTPUT_PATH, help="verifier .pkl output path")
    p.add_argument("--backup", default=BACKUP_PATH,
                   help="where to preserve the existing verifier before overwriting")
    p.add_argument("--no-backup", action="store_true",
                   help="do not back up the existing verifier")
    p.add_argument("--test-frac", type=float, default=0.2,
                   help="fraction of clips held out for test metrics")
    p.add_argument("--seed", type=int, default=0, help="RNG seed for the clip split")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    positive_clips = gather_clips(args.positive_dir)
    negative_clips = gather_clips(args.negative_dir)

    if not positive_clips:
        print(f"❌ No positive WAV files found in {args.positive_dir}")
        print("   Run generate_dataset.py (or record_samples.py) first.")
        sys.exit(1)
    if not negative_clips:
        print(f"❌ No negative WAV files found in {args.negative_dir}")
        print("   Run generate_dataset.py (or record_negative.py) first.")
        sys.exit(1)

    print("=" * 60)
    print("  OSIA Wake Word — Custom Verifier Training")
    print(f"  Feature extractor: {BASE_MODEL}")
    print(f"  Positive dirs: {args.positive_dir}  ({len(positive_clips)} clips)")
    print(f"  Negative dirs: {args.negative_dir}  ({len(negative_clips)} clips)")
    print(f"  Output: {args.output}")
    print("=" * 60)

    # Load openWakeWord model (includes feature extractor + all pretrained models)
    oww = openwakeword.Model()

    # Extract features grouped per clip (3 augmentation passes for positives,
    # 1 for negatives — same as the original recipe).
    print("\nExtracting features from positive clips...")
    pos_clips_feats = extract_per_clip(positive_clips, oww, 3, "Positive")
    oww.reset()
    if not pos_clips_feats:
        print("❌ No positive features extracted. Check your audio files.")
        sys.exit(1)

    print("\nExtracting features from negative clips...")
    neg_clips_feats = extract_per_clip(negative_clips, oww, 1, "Negative")
    oww.reset()
    if not neg_clips_feats:
        print("❌ No negative features extracted. Check your audio files.")
        sys.exit(1)

    n_pos_frames = sum(c.shape[0] for c in pos_clips_feats)
    n_neg_frames = sum(c.shape[0] for c in neg_clips_feats)
    print(f"\n   Positive feature vectors: {n_pos_frames} (from {len(pos_clips_feats)} clips)")
    print(f"   Negative feature vectors: {n_neg_frames} (from {len(neg_clips_feats)} clips)")

    # ── Clip-grouped train/test split (no frame leakage across the split) ──
    rng = np.random.default_rng(args.seed)
    p_tr, p_te = split_indices(len(pos_clips_feats), args.test_frac, rng)
    n_tr, n_te = split_indices(len(neg_clips_feats), args.test_frac, rng)
    pos_train = [pos_clips_feats[i] for i in p_tr]
    pos_test = [pos_clips_feats[i] for i in p_te]
    neg_train = [neg_clips_feats[i] for i in n_tr]
    neg_test = [neg_clips_feats[i] for i in n_te]

    recommended = None
    if pos_train and neg_train and pos_test and neg_test:
        Xtr = np.vstack(pos_train + neg_train)
        ytr = np.array([1] * sum(c.shape[0] for c in pos_train)
                       + [0] * sum(c.shape[0] for c in neg_train))
        print("\nTraining (held-out split) for evaluation...")
        eval_model = train_verifier_model(Xtr, ytr)
        recommended = evaluate_and_recommend(eval_model, pos_test, neg_test)
    else:
        print("\n   ⚠️  Too few clips to hold out a test set — reporting train accuracy only.")

    # ── Final model: trained on ALL data so the shipped verifier uses everything ──
    X = np.vstack(pos_clips_feats + neg_clips_feats)
    y = np.array([1] * n_pos_frames + [0] * n_neg_frames)
    print(f"\nTraining FINAL classifier on all {X.shape[0]} feature vectors...")
    lr_model = train_verifier_model(X, y)

    # Back up the existing verifier before overwriting (only if a backup doesn't
    # already exist — never clobber the preserved personal model).
    if (not args.no_backup and os.path.exists(args.output)
            and args.output != args.backup and not os.path.exists(args.backup)):
        shutil.copy2(args.output, args.backup)
        print(f"\n💾 Backed up existing verifier -> {args.backup}")

    with open(args.output, "wb") as f:
        pickle.dump(lr_model, f)

    print(f"\n✅ Training complete!")
    print(f"   Model saved to: {args.output}")
    print(f"   File size: {os.path.getsize(args.output) / 1024:.1f} KB")

    # Sanity-check the saved model matches what wake.py expects (16 frames × 96).
    from sklearn.metrics import accuracy_score
    n_features = lr_model.named_steps["standardscaler"].n_features_in_
    print(f"   Verifier feature dim: {n_features} ({n_features // 96} frames × 96)")
    print(f"   Final training accuracy: {accuracy_score(y, lr_model.predict(X)):.2%}")
    if recommended is not None:
        print(f"\n🎚️  Recommended VERIFIER_THRESHOLD: {recommended:.2f}")
        print("    (current default in osia/voice/wake.py is 0.85 — update if desired)")


if __name__ == "__main__":
    main()
