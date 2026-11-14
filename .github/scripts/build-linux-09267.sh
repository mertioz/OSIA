#!/usr/bin/env bash
set -euo pipefail
umask 022
expected=dfb4fe034f7dc1bd0dbe27c2f989de924d3d9d49
work="$RUNNER_TEMP/osia-linux-09267"
source="$work/source"
mkdir -p "$work"/{artifacts,reports,logs,runtime/downloads,runtime/bundle/runtimes,prior}
git worktree add --detach "$source" "$expected"
test "$(git -C "$source" rev-parse HEAD)" = "$expected"
test -z "$(git -C "$source" status --porcelain)"
archive="$work/prior/OSIA-Linux-x86_64.tar.xz"
curl -fL --retry 2 --max-time 300 https://download.osia.space/linux/0.9.266/OSIA-Linux-x86_64.tar.xz -o "$archive"
printf '%s  %s\n' be9c840a1c65d944f3a0a288c08e72198105d8e154b44dbe82c27f44011097cc "$archive" | sha256sum -c
tar --no-same-owner -xf "$archive" -C "$work/prior"
prior="$work/prior/OSIA-Linux-x86_64"
cmp "$prior/install.sh" "$source/packaging/linux/install.sh"
cp -a "$prior/payload/python" "$work/runtime/bundle/python"
cp -a "$prior/payload/runtimes/codex" "$work/runtime/bundle/runtimes/codex"
python="$work/runtime/bundle/python/bin/python3.12"
export PIP_CONFIG_FILE=/dev/null PIP_INDEX_URL=https://pypi.org/simple PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME
"$python" -I -B -m pip install --no-deps --require-hashes --report "$work/reports/runtime-install.json" -r "$prior/payload/bootstrap/requirements.lock" 2>&1 | tee "$work/logs/locked-install.log"
printf '%s\n' '{"install":[]}' > "$work/reports/torch-install.json"
"$python" -I -B -m pip check
"$python" -I -B "$source/packaging/linux/lock_dependencies.py" "$work/reports/torch-install.json" "$work/reports/runtime-install.json" > "$work/reports/reused-requirements.lock"
cmp "$work/reports/reused-requirements.lock" "$prior/payload/bootstrap/requirements.lock"
touch "$work/runtime/cpu-torch-ready" "$work/runtime/runtime-ready"
cd "$source"
python3 -I -B scripts/scan_secrets.py --osia-only
bash packaging/linux/prepare_runtime.sh "$work/runtime" "$work/reports" 2>&1 | tee "$work/logs/prepare-runtime.log"
bash packaging/linux/build_linux.sh "$work/runtime" "$work/reports" "$work/artifacts" 2>&1 | tee "$work/logs/build.log"
tar -xOf "$work/artifacts/OSIA-Linux-x86_64.tar.xz" OSIA-Linux-x86_64/payload/bootstrap/requirements.lock > "$work/reports/candidate-requirements.lock"
cmp "$work/reports/candidate-requirements.lock" "$prior/payload/bootstrap/requirements.lock"
cd "$work/artifacts"
sha256sum -c OSIA-Linux-x86_64.tar.xz.sha256
python3 -I -B - "$expected" <<'PY'
from pathlib import Path
import json
import sys
meta=json.loads(Path('linux-build-metadata.json').read_text())
assert meta['version']=='0.9.267' and meta['releaseSequence']==2026111401
assert meta['sourceCommit']==sys.argv[1]
assert meta['installerId']=='space.osia.desktop.linux.v1'
print('LINUX_BUILD_IDENTITY_VERIFIED')
PY
printf '%s\n' LINUX_CI_BUILD_COMPLETE
