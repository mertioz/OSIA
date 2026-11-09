#!/usr/bin/env bash
set -euo pipefail
umask 022

if [ "$#" -ne 3 ]; then
  printf 'Usage: %s RUNTIME_WORK_DIR PIP_REPORT_DIR OUTPUT_DIR\n' "$0" >&2
  exit 2
fi
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ]
work="$(realpath -e -- "$1")"
reports="$(realpath -e -- "$2")"
mkdir -p -- "$3"
output="$(realpath -e -- "$3")"
here="$(cd -- "$(dirname -- "$0")" && pwd -P)"
. "$here/codex_runtime.sh"
repo="$(cd -- "$here/../.." && pwd -P)"
git -C "$repo" diff --quiet HEAD
revision="$(git -C "$repo" rev-parse HEAD)"
epoch="$(git -C "$repo" show -s --format=%ct "$revision")"
python="$work/bundle/python/bin/python3.12"
test -x "$python"
test -f "$work/runtime-ready"
stage="$(mktemp -d -- "$work/staging.XXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
package="$stage/OSIA-Linux-x86_64"
payload="$package/payload"
mkdir -p -- "$payload/"{app,bin,integration,bootstrap}
git -C "$repo" archive "$revision" osia assets THIRD_PARTY_NOTICES.md \
  | tar -xf - -C "$payload/app"

# The user installer fetches third-party Python libraries and browsers from
# their publishers. The portable interpreter and Codex are verified inputs.
python_archive="$work/downloads/cpython-3.12.16+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz"
printf '%s  %s\n' 6a79e922e50006b601f29e688407659da1c2b86ebcd4e9c7254cf234be8b89de "$python_archive" | sha256sum -c
tar -xzf "$python_archive" -C "$payload"
cp -a -- "$work/bundle/runtimes" "$payload/"
install -m 0755 -- "$here/install.sh" "$package/install.sh"
install -m 0755 -- "$here/launch.sh" "$payload/bin/osia"
install -m 0644 -- "$here/doctor.py" "$payload/doctor.py"
install -m 0644 -- "$here/osia-server.service" "$here/write_desktop_entry.py" "$here/link_browsers.py" "$payload/integration/"
install -m 0644 -- "$here/README.md" "$package/README.md"

"$python" -I -B "$here/lock_dependencies.py" \
  "$reports/torch-install.json" "$reports/runtime-install.json" > "$payload/bootstrap/requirements.lock"
"$python" -I -B - "$payload" "$revision" <<'PY'
import json
from pathlib import Path
import runpy
import sys

root = Path(sys.argv[1])
release = runpy.run_path(str(root / "app/osia/version.py"))
manifest = {
    "schemaVersion": 1,
    "platform": "linux-x64",
    "installerId": "space.osia.desktop.linux.v1",
    "version": release["PRODUCT_VERSION"],
    "releaseSequence": release["RELEASE_SEQUENCE"],
    "sourceCommit": sys.argv[2],
}
(root / "metadata.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY
"$python" -I -B "$here/prepare_app.py" "$payload/app"
"$payload/bin/osia" --version
validate_codex_runtime "$payload/runtimes/codex"
name=OSIA-Linux-x86_64.tar.xz
test ! -e "$output/$name"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" \
  -C "$stage" -cf - OSIA-Linux-x86_64 | xz -T2 -1 -c > "$output/$name.partial"
xz -t "$output/$name.partial"
mv -T -- "$output/$name.partial" "$output/$name"
(
  cd -- "$output"
  sha256sum "$name" > "$name.sha256"
)
cp -- "$payload/metadata.json" "$output/linux-build-metadata.json"
printf 'Release archive: %s\n' "$output/$name"
