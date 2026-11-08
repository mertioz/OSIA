#!/usr/bin/env bash
set -euo pipefail
umask 022
if [ "$#" -ne 2 ]; then
  printf 'Usage: %s WORK_DIR REPORT_DIR\n' "$0" >&2
  exit 2
fi
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ]
mkdir -p -- "$1" "$2"
root="$(realpath -e -- "$1")"
reports="$(realpath -e -- "$2")"
source="$(cd -- "$(dirname -- "$0")/../.." && pwd -P)"
mkdir -p -- "$root"/{downloads,bundle}
asset=cpython-3.12.16+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz
digest=6a79e922e50006b601f29e688407659da1c2b86ebcd4e9c7254cf234be8b89de
if [ ! -f "$root/downloads/$asset" ]; then
  curl -fL --retry 2 --max-time 240 \
    'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-unknown-linux-gnu-install_only.tar.gz' \
    -o "$root/downloads/$asset.partial"
  mv "$root/downloads/$asset.partial" "$root/downloads/$asset"
fi
printf '%s  %s\n' "$digest" "$root/downloads/$asset" | sha256sum --check
runtime="$root/bundle/python"
if [ ! -x "$runtime/bin/python3.12" ]; then
  tar -xzf "$root/downloads/$asset" -C "$root/bundle"
fi
python="$runtime/bin/python3.12"
"$python" -VV
export PIP_CONFIG_FILE=/dev/null
export PIP_INDEX_URL=https://pypi.org/simple
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_CACHE_DIR=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME
export CC="${CC:-gcc}"
if [ ! -f "$root/cpu-torch-ready" ]; then
  "$python" -m pip install --index-url https://download.pytorch.org/whl/cpu \
    --report "$reports/torch-install.json" 'torch==2.12.0+cpu'
  touch "$root/cpu-torch-ready"
fi
if [ ! -f "$root/runtime-ready" ]; then
  # The shared source explicitly disables acoustic wake. Its optional
  # tflite-runtime dependency does not ship CPython 3.12 Linux wheels.
  sed -E '/^openwakeword[>=]/d; /^scikit-learn==/d; /^PyQt6[>=]/d' "$source/requirements.txt" >"$root/requirements-linux.txt"
  "$python" -m pip install --report "$reports/runtime-install.json" \
    -r "$root/requirements-linux.txt" 'playwright==1.60.0' 'PySide6-Essentials==6.11.0'
  "$python" -m pip check
  "$python" -m pip freeze >"$reports/linux-resolved-requirements.txt"
  touch "$root/runtime-ready"
fi
export PLAYWRIGHT_BROWSERS_PATH="$root/bundle/browsers"
if [ ! -f "$root/browsers-ready" ]; then
  "$python" -m playwright install chromium firefox
  touch "$root/browsers-ready"
fi
codex_dir="$root/bundle/runtimes/codex/bin"
if [ ! -x "$codex_dir/codex" ]; then
  mkdir -p "$codex_dir"
  archive="$root/downloads/codex-linux-x64-0.166.0.tar.gz"
  curl -fsSL --retry 2 --max-time 240 \
    https://github.com/openai/codex/releases/download/rust-v0.166.0/codex-x86_64-unknown-linux-musl.tar.gz \
    -o "$archive"
  printf '%s  %s\n' a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978 "$archive" | sha256sum -c
  tar -xzf "$archive" -C "$codex_dir" codex-x86_64-unknown-linux-musl
  mv "$codex_dir/codex-x86_64-unknown-linux-musl" "$codex_dir/codex"
  "$codex_dir/codex" --version
  cp "$source/packaging/windows/licenses/codex/"{LICENSE,NOTICE} "$codex_dir/../"
fi
du -sh "$root/bundle/"*
printf '%s\n' 'OSIA_LINUX_RUNTIME_READY'
