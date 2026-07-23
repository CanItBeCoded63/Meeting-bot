#!/usr/bin/env bash
# Join a meeting as a named agent, running inside Docker (Linux userspace
# with PulseAudio + Xvfb) — required on Windows hosts since joinly's browser
# provider shells out to /usr/bin/pactl and Xvfb, neither of which exist
# bare-metal on Windows. Mirrors run_bot.ps1's "single" mode, parameterized
# by agent name.
#
# Usage:
#   ./join_meeting_docker.sh <agent> <meeting-url> [vnc-port]
#
# <agent>: any id from agents.yaml (e.g. aria, sam, mira), or any custom name
#          (falls back to custom_prompt.txt, no voice/tts override)
# [vnc-port]: host port for VNC viewing (default: 5900)
#
# agents.yaml is the single source of truth for name/persona/voice — it's
# read fresh on every run via scripts/resolve_agent.py, the same file
# azure-deploy/meeting_manager reads for cloud deploys. Edit agents.yaml,
# both local and cloud pick it up — nothing to duplicate.

set -euo pipefail

# Git Bash/MSYS rewrites leading-slash args (e.g. /workspace) into Windows
# paths before they reach docker.exe. Disable that for this script.
export MSYS_NO_PATHCONV=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
  echo "Usage: $0 <agent> <meeting-url> [vnc-port]" >&2
  echo "Known agents: aria, sam, mira (or any custom name)" >&2
  exit 1
}

[ $# -ge 2 ] || usage

AGENT_KEY="$1"
MEETING_URL="$2"
VNC_PORT="${3:-5900}"
IMAGE="${JOINLY_IMAGE:-joinly-local:latest}"

# Relative paths here — MSYS_NO_PATHCONV (needed below for docker's
# leading-slash container paths) also blocks MSYS from translating
# SCRIPT_DIR's POSIX-style path for native Windows python.exe, which
# mangles it into garbage (e.g. /c/Users/... -> C:\c\Users\...).
RESOLVED="$(uv run python scripts/resolve_agent.py agents.yaml "${AGENT_KEY,,}" 2>/dev/null || true)"

DISPLAY_NAME="$AGENT_KEY"
PERSONA_FILE="$SCRIPT_DIR/custom_prompt.txt"
TTS=""
STT=""
TTS_VOICE=""

if [ -n "$RESOLVED" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      AGENT_NAME) DISPLAY_NAME="$value" ;;
      AGENT_TTS) TTS="$value" ;;
      AGENT_STT) STT="$value" ;;
      AGENT_TTS_VOICE) TTS_VOICE="$value" ;;
      AGENT_PERSONA_FILE) PERSONA_FILE="$value" ;;
    esac
  done <<< "$RESOLVED"
fi

[ -f "$PERSONA_FILE" ] || { echo "Persona file not found: $PERSONA_FILE" >&2; exit 1; }
[ -f "$SCRIPT_DIR/.env" ] || { echo ".env not found in $SCRIPT_DIR" >&2; exit 1; }
[ -f "$SCRIPT_DIR/mcp_config_docker.json" ] || { echo "mcp_config_docker.json not found" >&2; exit 1; }

CONTAINER_NAME="joinly-${AGENT_KEY,,}"
DOCKER_STOP_TIMEOUT="${JOINLY_DOCKER_STOP_TIMEOUT:-120}"
# Reuse the shared, already-authenticated Teams browser profile (same one
# run_bot.ps1 uses) so the bot joins signed-in instead of as an anonymous
# guest stuck in the lobby. Only one agent can use it at a time.
BROWSER_PROFILE_DIR="${JOINLY_BROWSER_PROFILE_DIR:-$SCRIPT_DIR/.joinly-browser-profile}"
[ -d "$BROWSER_PROFILE_DIR" ] || { echo "Browser profile not found: $BROWSER_PROFILE_DIR (run auth-login first)" >&2; exit 1; }

PROMPT_CONTENT="$(tr '\n' ' ' < "$PERSONA_FILE" | sed 's/"/'"'"'/g')"
[ "$PERSONA_FILE" != "$SCRIPT_DIR/custom_prompt.txt" ] && rm -f "$PERSONA_FILE"

echo "Stopping existing container '$CONTAINER_NAME' (if any)..." >&2
docker stop -t "$DOCKER_STOP_TIMEOUT" "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

DOCKER_ARGS=(
  -d --name "$CONTAINER_NAME" --shm-size=2gb --env-file .env
  --entrypoint /app/.venv/bin/python
  -e "PYTHONPATH=/workspace:/workspace/client:/workspace/common"
  -p "${VNC_PORT}:5900"
  -v "$SCRIPT_DIR:/workspace"
  -v "$BROWSER_PROFILE_DIR:/browser-profile"
  -v "$SCRIPT_DIR/mcp_config_docker.json:/runtime_mcp_config.json:ro"
  -w /workspace
  "$IMAGE"
  -m joinly.main --client
  --name "$DISPLAY_NAME" --name-trigger --vnc-server
  --transcription-controller-arg no_speech_event_delay=0.6
  --meeting-provider-arg browser_profile_dir=/browser-profile
  --prompt "$PROMPT_CONTENT"
  --mcp-config /runtime_mcp_config.json
)

[ -n "$TTS" ] && DOCKER_ARGS+=(--tts "$TTS")
[ -n "$STT" ] && DOCKER_ARGS+=(--stt "$STT")
[ -n "$TTS_VOICE" ] && DOCKER_ARGS+=(--tts-arg "model_name=$TTS_VOICE")

DOCKER_ARGS+=("$MEETING_URL")

echo "Starting '$DISPLAY_NAME' -> $MEETING_URL (container: $CONTAINER_NAME, VNC: localhost:$VNC_PORT)" >&2
docker run "${DOCKER_ARGS[@]}" >/dev/null

echo "Started. Stream logs with: docker logs -f $CONTAINER_NAME" >&2
