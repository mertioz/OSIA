#!/usr/bin/env bash
set -euo pipefail
root="$(dirname -- "$(dirname -- "$(readlink -f -- "$0")")")"
python="$root/python/bin/python3.12"
unset PYTHONHOME
export PYTHONPATH="$root/app"
export PYTHONNOUSERSITE=1
export PYTHONSAFEPATH=1
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$root/browsers}"
export PATH="$root/runtimes/codex/bin:$PATH"

case "${1:-}" in
  --version)
    exec "$python" -B -P -c 'from osia.version import PRODUCT_VERSION; print(PRODUCT_VERSION)'
    ;;
  --doctor)
    exec "$python" -B -P "$root/doctor.py"
    ;;
  --server)
    shift
    exec "$python" -B -P -m osia.server.run_server "$@"
    ;;
esac
if [ -z "${DISPLAY:-}" ]; then
  printf '%s\n' 'OSIA Desktop needs an X11 display. For a server, use osia --server.' >&2
  exit 1
fi
if command -v systemctl >/dev/null 2>&1 && systemctl --user is-enabled --quiet osia-server.service; then
  display_variables=(DISPLAY)
  if [ -n "${XAUTHORITY:-}" ]; then display_variables+=(XAUTHORITY); fi
  systemctl --user import-environment "${display_variables[@]}" || true
  systemctl --user start osia-server.service || printf '%s\n' 'OSIA server could not start.' >&2
fi
exec "$python" -B -P -m osia.main "$@"
