#!/usr/bin/env bash
# Join a meeting locally (no Docker) as a named agent, single embedded process.
#
# Usage:
#   ./join_meeting.sh <agent> <meeting-url> [extra joinly args...]
#
# <agent> is any id from agents.yaml (e.g. aria, sam, mira), or any custom display
# name (falls back to default prompt style, kokoro/whisper local voice stack).
#
# agents.yaml is the single source of truth for name/persona/voice — read
# fresh on every run via scripts/resolve_agent.py, the same file
# azure-deploy/meeting_manager reads for cloud deploys.
#
# Env overrides (optional):
#   AGENT_LLM_PROVIDER / AGENT_LLM_MODEL   - override LLM for this run
#   AGENT_TTS / AGENT_STT                  - override TTS/STT service
#
# Requires: uv, and .env (or --env-file passed via extra args) with the
# relevant provider/API keys set. Runs joinly's --client mode directly
# (embedded MCP server, same as run_bot.ps1 uses inside Docker).
#
# NOTE: this bare (non-Docker) path does not work on Windows hosts — joinly's
# browser provider shells out to /usr/bin/pactl and Xvfb, both Linux-only.
# Use join_meeting_docker.sh on Windows. This script is for Linux/WSL hosts.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
  echo "Usage: $0 <agent> <meeting-url> [extra joinly args...]" >&2
  echo "Known agents: aria, sam, mira (or any custom name)" >&2
  exit 1
}

[ $# -ge 2 ] || usage

AGENT_KEY="$1"
MEETING_URL="$2"
shift 2

RESOLVED="$(uv run python scripts/resolve_agent.py agents.yaml "${AGENT_KEY,,}" 2>/dev/null || true)"

DISPLAY_NAME="$AGENT_KEY"
PERSONA_FILE=""
TTS_VOICE=""
TTS="${AGENT_TTS:-}"
STT="${AGENT_STT:-}"

if [ -n "$RESOLVED" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      AGENT_NAME) DISPLAY_NAME="$value" ;;
      AGENT_TTS) TTS="${AGENT_TTS:-$value}" ;;
      AGENT_STT) STT="${AGENT_STT:-$value}" ;;
      AGENT_TTS_VOICE) TTS_VOICE="$value" ;;
      AGENT_PERSONA_FILE) PERSONA_FILE="$value" ;;
    esac
  done <<< "$RESOLVED"
fi

ARGS=(--client --name "$DISPLAY_NAME" --name-trigger)

[ -n "${AGENT_LLM_PROVIDER:-}" ] && ARGS+=(--llm-provider "$AGENT_LLM_PROVIDER")
[ -n "${AGENT_LLM_MODEL:-}" ] && ARGS+=(--llm-model "$AGENT_LLM_MODEL")
[ -n "$TTS" ] && ARGS+=(--tts "$TTS")
[ -n "$STT" ] && ARGS+=(--stt "$STT")
[ -n "$TTS_VOICE" ] && ARGS+=(--tts-arg "model_name=$TTS_VOICE")
if [ -n "$PERSONA_FILE" ]; then
  ARGS+=(--prompt "$(cat "$PERSONA_FILE")")
  rm -f "$PERSONA_FILE"
fi

echo "Joining as '$DISPLAY_NAME' -> $MEETING_URL" >&2

exec uv run joinly "${ARGS[@]}" "$@" "$MEETING_URL"
