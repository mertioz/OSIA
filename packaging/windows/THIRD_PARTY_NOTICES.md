# OSIA third-party notices

OSIA is distributed with third-party open-source libraries and model assets.
The build creates `legal/DEPENDENCIES.json` and copies the license/notice files
available in each installed Python distribution into `legal/licenses/`.

## Bundled model assets

- **Kokoro-82M** (`hexgrad/Kokoro-82M`, pinned revision
  `f3ff3571791e39611d31c381e3a41a3af07b4987`) is provided under the Apache
  License 2.0. The bundled `ff_siwis` voice is credited to SIWIS and provided
  under CC BY 4.0; attribution must be retained when redistributing it.
- **faster-whisper-small** (`Systran/faster-whisper-small`, pinned revision
  `536b0662742c02347bc0e980a01041f333bce120`) is provided under the MIT License.

The corresponding upstream model cards remain the authoritative source for
model limitations, provenance, and license details.

## Local document reading

- **pypdfium2 5.12.1** is distributed under Apache-2.0 and BSD-3-Clause terms
  and bundles PDFium plus its applicable notices. It performs native PDF text
  extraction and page rendering without a separately installed executable.
- **RapidOCR 3.9.2** is distributed under Apache-2.0 and includes the pinned
  PP-OCR ONNX detection, classification, and recognition models used by OSIA's
  fully offline OCR path. ONNX Runtime performs local inference.

The generated dependency inventory and copied package notices are authoritative
for the exact binaries, model files, versions, and transitive licenses shipped
in a given build.

## Media codecs

OSIA performs in-process WAV-to-MP3 conversion through PyAV and the FFmpeg
libraries supplied by PyAV. No separate `ffmpeg.exe` is bundled. The generated
dependency inventory and copied notices describe the exact PyAV build used.

## Native copyleft distribution gate

The current internal-test bundle is not cleared for public redistribution:

- the official PyAV 17.1.0 Windows wheel contains an FFmpeg build linked with
  `libx264` and `libx265`; FFmpeg documents that such a build is GPL rather than
  LGPL;
- `espeakng-loader` 0.2.4 embeds the eSpeak NG DLL and data, and eSpeak NG is
  GPL-3.0-or-later.

Before a public release, the distributor must complete GPL compliance (including
the corresponding-source and license obligations) or replace these payloads
with an approved license-compatible build. This notice is deliberately not a
substitute for that review. The unsigned artifact produced without that review
is for internal validation only.
