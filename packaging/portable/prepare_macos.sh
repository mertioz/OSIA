#!/usr/bin/env bash
set -euo pipefail
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]]
repo="$(git rev-parse --show-toplevel)"
mkdir -p "$1"
work="$(cd "$1" && pwd -P)"
asset=cpython-3.12.16+20261026-aarch64-apple-darwin-install_only.tar.gz
curl -fsSL --retry 3 "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/${asset/+/%2B}" -o "$work/python.tar.gz"
echo "fbf4afef53c2a6b72bb741bc06d5671a9dfaf87bd84e5aa73ac9b3d0d589f14e  $work/python.tar.gz" | shasum -a 256 -c -
mkdir -p "$work/bundle/runtimes/codex"
tar -xzf "$work/python.tar.gz" -C "$work/bundle"
python="$work/bundle/python/bin/python3.12"
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# openWakeWord and sklearn are only used by the retired training tool.
sed -E '/^openwakeword[>=]/d; /^scikit-learn==/d' "$repo/requirements.txt" > "$work/requirements.txt"
"$python" -I -m pip install --report "$work/packages.json" -r "$work/requirements.txt" 'playwright==1.60.0'
"$python" -I -m pip check
export PLAYWRIGHT_BROWSERS_PATH="$work/bundle/browsers"
"$python" -I -m playwright install chromium firefox
curl -fsSL --retry 3 https://github.com/openai/codex/releases/download/rust-v0.166.0/codex-package-aarch64-apple-darwin.tar.gz -o "$work/codex.tar.gz"
echo "55a440cb579f970abe7c79ab181e54b0ea4c419be99f333bbd868eebac9c21f0  $work/codex.tar.gz" | shasum -a 256 -c -
tar -xzf "$work/codex.tar.gz" -C "$work/bundle/runtimes/codex"
cp "$repo/packaging/windows/licenses/codex/"{LICENSE,NOTICE} "$work/bundle/runtimes/codex/"
for entry in bin/codex bin/codex-code-mode-host bin/codex-exec-server codex-path/rg; do
    test -x "$work/bundle/runtimes/codex/$entry"
done
test "$("$work/bundle/runtimes/codex/bin/codex" --version)" = 'codex-cli 0.166.0'
