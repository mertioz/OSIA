# OSIA local voice activation — source preview

Say **Hé OSIA**, pronounced **O-ssia** with an unvoiced s. The author confirmed
this pronunciation and approved the longer wake phrase on 15 November 2026.
The shorter name alone is not an activation phrase.

## Engine and assets

The detector uses the native French acoustic model **vosk-model-small-fr-0.22**
and an OSIA phonetic search configuration. It requires no personal recording,
voice enrollment, service key or network connection during listening. There is
no new language model/planner for Chat: the selected Chat provider/model remains
responsible for every command after normal configured STT.

The French lexicon encodes the sounds of the proper name using the fixed
sequence `hé oh sia`. These are internal phonetic labels, never user-visible
commands or instructions supplied to Chat/STT. Nearby phrases and `[unk]` compete
with the keyword so general speech is not forced into an activation.

- Source: <https://alphacephei.com/vosk/models/vosk-model-small-fr-0.22.zip>
- Upstream listing and model license: <https://alphacephei.com/vosk/models>
- Model license: **Apache-2.0**, separate from OSIA branding permissions.
- 68,483,807 bytes of model data, plus the native Vosk library already declared
  in requirements. This is a compact French speech engine, not the retired
  pickle classifier. The downloaded model is not relabeled as OSIA-authored.
- 16 kHz mono PCM, processed every 80 ms on a background CPU worker.
- Model availability uses file metadata; full SHA-256 verification and native
  loading run off the GUI/ASGI startup path. No model download at runtime.
- `manifest.json` records the original archive SHA-256 and every shipped file's
  size/hash. Windows/Linux keep their existing installers and asset layout.

## Privacy and lifecycle

Acoustic activation is **off by default**. Enable it in Settings → Wake word.
Ctrl+Alt+P continues to start manual recording even without the wake backend.
Missing audio drivers or model files leave Chat and the capsule usable.

Only one wake microphone worker may exist. It suspends for an accepted voice
turn, ignores its own playback/active voice capture and rejects obsolete generations. The
wake microphone closes before the command recorder opens. Manual activation
pauses and awaits that same stream; stop invalidates queued activations.

A bounded rolling audio buffer stays in RAM. The recognizer's observed wake-word
end determines the command handoff, excluding pre-keyword speech. Audio is not
saved or used for personal training. Command STT uses the user's existing
provider; a configured cloud STT provider may receive the command recording.
The detector does not infer or inject a response language.

## Validation on 15 November 2026

The shipped engine/configuration was exercised at the actual 80 ms cadence:

- 12/12 synthetic French wake phrases detected, including different speaking
  rates and a phrase followed immediately by a request.
- 0 activations on 100 negative cases: nearby words, the name alone, ordinary
  speech and 2,106.27 seconds of public background audio.
- Mean processing time 2.67 ms per frame; p95 8.29 ms on the development Windows
  PC. These are processing measurements, not a guarantee of recognition latency.

Synthetic speech uses Kokoro's French `ff_siwis` voice (Apache-2.0 model; SIWIS voice attribution CC BY 4.0,
<https://huggingface.co/hexgrad/Kokoro-82M>). Public noise is from MS-SNSD,
Reddy et al., Interspeech 2019, <https://github.com/microsoft/MS-SNSD>, revision
`fe61c4ba0d9ac8dd7e23d719cc79f8947e1dc742`. Its code is MIT; its audio retains
Freesound CC0 / DEMAND CC BY-SA 3.0 source licenses. Test audio is not shipped.

Earlier single-name classifiers and low-quality/augmented synthesis corpora did
not establish reliable multi-voice recognition and are not shipped. An
exploratory splice test using old synthetic clips had substantially lower
recall; it is not a real-speaker acceptance result. A new custom neural wake
model was therefore not declared reliable or substituted silently.

**Physical multi-voice/microphone/distance testing remains pending.** The source
preview is intended for an explicit user trial, not a claim that every voice,
room or device is validated. False activations or missed words can still occur.
Existing installer channels were not changed by this source feature.

## Source tuning, 15 November 2026

The wake confidence threshold is slightly relaxed from 0.80 to 0.75. The
complete ordered phonetic phrase, competing vocabulary, model bytes and 80 ms
analysis cadence remain unchanged. A 40 ms experiment did not improve replay
recognition latency and was not retained.

The comparison includes 36 synthetic positives (the original 12 plus 24
attenuated/noisy variants) and the same 100 negatives including 2,106.27 seconds
of public background audio. Both configurations detect 33/36 positives, with
12/12 on the original corpus and no false activation in the negative corpus.
The three missed noisy samples remain a known limitation; this is not a claim
of a measured accuracy or recognition-time gain for every voice.

Microphone capture and wake feedback request the device's lower audio latency.
An input opening failure or input overflow falls back to the stable default,
without overlapping microphone streams. A rejected output opening also falls
back to its default. Driver-dependent physical latency still needs an operator
trial.

After an acoustic wake, four seconds without command speech closes listening
and rearms detection silently. Speech already started can finish normally;
manual capture retains its existing retry behavior. The generated wake cue is
a brighter 180 ms bell at a higher digital level, with unclipped headroom. It
stays independent of spoken-reply muting and never delays command capture.

## Re-run verification

From the primary source checkout with its normal dependencies:

```powershell
python -m osia.wake_training.verify_wake_phrase C:\scratch\osia-wake-check --noise-dir C:\scratch\public-noise
```

The tool opens no microphone and writes generated WAVs/reports only to the given
output directory. Keep that evidence outside Git. On Linux/macOS use local
writable paths. The application needs only the committed model assets, not any
training/evidence folder.
