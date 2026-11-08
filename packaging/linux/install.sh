#!/usr/bin/env bash
set -euo pipefail
umask 022

if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
  printf '%s\n' 'This OSIA installer requires Linux x86-64.' >&2
  exit 1
fi
if [ "$(id -u)" -eq 0 ]; then
  printf '%s\n' 'Run this installer as the account that will use OSIA.' >&2
  exit 1
fi
rollback=false
case "${1:-}" in
  '') ;;
  --rollback) rollback=true ;;
  *) printf 'Usage: %s [--rollback]\n' "$0" >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || exit 2
base="$(cd -- "$(dirname -- "$0")" && pwd -P)"
payload="$base/payload"
python="$payload/python/bin/python3.12"
read_metadata() {
  "$python" -I -B - "$1" <<'PY'
import json
import re
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
version = data["version"]
sequence = data["releaseSequence"]
if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
    raise SystemExit("Invalid OSIA product version.")
if type(sequence) is not int or sequence <= 0:
    raise SystemExit("Invalid OSIA release sequence.")
if data["installerId"] != "space.osia.desktop.linux.v1":
    raise SystemExit("Invalid OSIA installer identity.")
print(version, sequence)
PY
}
metadata="$(read_metadata "$payload/metadata.json")"
read -r version sequence <<< "$metadata"

data="${XDG_DATA_HOME:-$HOME/.local/share}/osia"
config="${XDG_CONFIG_HOME:-$HOME/.config}"
launcher="$HOME/.local/bin/osia"
units="$config/systemd/user"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
current="$data/current"
release="$data/releases/$version"
first_install=false
mkdir -p -- "$data/releases" "$(dirname -- "$launcher")" "$units" "$apps"
exec 9>"$data/.install.lock"
flock -x 9

if [ -e "$launcher" ] || [ -L "$launcher" ]; then
  if [ "$(readlink -- "$launcher" || true)" != "$current/bin/osia" ]; then
    printf 'An existing command owns %s.\n' "$launcher" >&2
    exit 1
  fi
fi
if [ -e "$current/metadata.json" ]; then
  installed_metadata="$(read_metadata "$current/metadata.json")"
  read -r installed_version installed_sequence <<< "$installed_metadata"
  if [ "$sequence" -lt "$installed_sequence" ] && [ "$rollback" != true ]; then
    printf 'OSIA %s is already installed. Use --rollback to select an older verified release.\n' "$installed_version" >&2
    exit 1
  fi
else
  first_install=true
fi
if [ "$rollback" = true ] && [ ! -d "$release" ]; then
  printf '%s\n' 'Rollback requires a previously installed release.' >&2
  exit 1
fi
if [ -e "$release" ]; then
  cmp -s -- "$payload/metadata.json" "$release/metadata.json" || {
    printf '%s\n' 'The installed release with this version has a different identity.' >&2
    exit 1
  }
else
  stage="$(mktemp -d -- "$data/releases/.incoming.XXXXXX")"
  trap 'rm -rf -- "$stage"' EXIT
  cp -a -- "$payload/." "$stage/"
  runtime_python="$stage/python/bin/python3.12"
  PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 CC=gcc \
    "$runtime_python" -I -B -m pip install --no-cache-dir --require-hashes \
      --no-deps -r "$stage/bootstrap/requirements.lock"
  "$runtime_python" -I -B -m pip check
  PLAYWRIGHT_BROWSERS_PATH="$stage/browsers" \
    "$runtime_python" -I -B -m playwright install chromium firefox
  PLAYWRIGHT_BROWSERS_PATH="$stage/browsers" \
    "$runtime_python" -I -B "$stage/integration/link_browsers.py"
  [ "$("$stage/bin/osia" --version)" = "$version" ]
  mv -T -- "$stage" "$release"
  trap - EXIT
fi

# The stable command and unit always follow one atomically selected release.
switch="$(mktemp -d -- "$data/.activate.XXXXXX")"
trap 'rm -rf -- "$switch"' EXIT
ln -s -- "releases/$version" "$switch/current"
install -m 0644 -- "$release/integration/osia-server.service" "$units/osia-server.service"
"$python" -I -B "$release/integration/write_desktop_entry.py" "$launcher" "$current" "$apps"
if [ ! -L "$launcher" ]; then
  ln -s -- "$current/bin/osia" "$launcher"
fi
mv -Tf -- "$switch/current" "$current"
if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload 2>/dev/null; then
  if [ "$first_install" = true ]; then
    systemctl --user enable osia-server.service || printf '%s\n' 'Enable the OSIA user service from Settings.' >&2
  else
    systemctl --user try-restart osia-server.service || printf '%s\n' 'Restart the OSIA user service from Settings.' >&2
  fi
fi

printf 'OSIA %s installed at %s\n' "$version" "$launcher"
printf '%s\n' 'Launch OSIA from your application menu or run osia. Use osia --server for a manual server.'
