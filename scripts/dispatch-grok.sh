#!/usr/bin/env bash
# Headless Grok CLI worker for this repo.
# I/O: prompt file path → Grok stdout; diagnostics on stderr.
# Resolve: $GROK_BIN → grok on PATH → ~/.grok/bin/grok
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROMPT_FILE="${1:-}"

if [[ -z "$PROMPT_FILE" || ! -f "$PROMPT_FILE" ]]; then
  echo "usage: scripts/dispatch-grok.sh <prompt-file>" >&2
  exit 2
fi

resolve_grok() {
  if [[ -n "${GROK_BIN:-}" && -x "$GROK_BIN" ]]; then
    printf '%s\n' "$GROK_BIN"
    return
  fi
  if command -v grok >/dev/null 2>&1; then
    command -v grok
    return
  fi
  local known="$HOME/.grok/bin/grok"
  if [[ -x "$known" ]]; then
    printf '%s\n' "$known"
    return
  fi
  echo "dispatch-grok: grok CLI not found (set GROK_BIN, or install to ~/.grok/bin/grok)" >&2
  exit 127
}

BIN="$(resolve_grok)"

# Never pass -m/--model. Grok's *reported* id (e.g. grok-4.5-build) is not a
# launch id; round-tripping it breaks the run. Default model only.
if [[ -n "${GROK_MODEL:-}" ]]; then
  echo "dispatch-grok: ignoring GROK_MODEL (do not pass reported model ids to -m)" >&2
fi
if [[ $# -gt 1 ]]; then
  echo "dispatch-grok: extra args not allowed (would be a -m footgun). usage: $0 <prompt-file>" >&2
  exit 2
fi

# --prompt-file = headless user turn. --always-approve so Cursor is not stuck on TUI prompts.
exec "$BIN" \
  --prompt-file "$PROMPT_FILE" \
  --cwd "$ROOT" \
  --always-approve \
  --max-turns 40 \
  --output-format plain
