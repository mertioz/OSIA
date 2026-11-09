# Shared checks for the prepared runtime and the staged release payload.
codex_version=0.166.0

validate_codex_runtime() {
  local root="$1" name
  for name in bin/codex bin/codex-code-mode-host bin/codex-exec-server codex-path/rg \
    codex-resources/bwrap codex-resources/voice/bin/codex-voice-host; do
    if [ ! -f "$root/$name" ] || [ ! -x "$root/$name" ]; then
      printf 'Missing Codex executable: %s\n' "$name" >&2
      return 1
    fi
  done
  for name in codex-package.json codex-resources/voice/manifest.json LICENSE NOTICE; do
    if [ ! -s "$root/$name" ]; then
      printf 'Missing Codex package file: %s\n' "$name" >&2
      return 1
    fi
  done
  [ "$("$root/bin/codex" --version)" = "codex-cli $codex_version" ]
}
