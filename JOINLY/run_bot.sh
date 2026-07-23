#!/usr/bin/env bash
set -euo pipefail

MODE="single"
MEETING_LINK=""
COOKIES_FILE=".joinly-teams-cookies.json"
SCHEDULE_FILE="meeting_schedule.json"
BROWSER_PROFILE_DIR=".joinly-browser-profile"
VNC_PORT="5900"
IMAGE="joinly-local:latest"
STORAGE_ACCOUNT_NAME="${AZURE_STORAGE_ACCOUNT_NAME:-}"
STORAGE_ACCOUNT_KEY="${AZURE_STORAGE_ACCOUNT_KEY:-}"
FILE_SHARE_NAME="${AZURE_FILE_SHARE_NAME:-}"
NO_PROFILE=0

SINGLE_CONTAINER_NAME="joinly-server"
SCHEDULER_CONTAINER_NAME="joinly-scheduler"
CONTAINER_PYTHON_PATH="/workspace:/workspace/client:/workspace/common"

usage() {
    cat <<'EOF'
Usage: ./run_bot.sh [options]

Options:
  -Mode, --mode VALUE                    single | scheduler-start | scheduler-stop |
                                         scheduler-status | scheduler-logs | auth-login |
                                         import-cookies | upload-profile
  -MeetingLink, --meeting-link VALUE     Meeting URL for single mode
  -CookiesFile, --cookies-file VALUE     Cookies file for import-cookies
  -ScheduleFile, --schedule-file VALUE   Schedule file for scheduler-start
  -BrowserProfileDir, --browser-profile-dir VALUE
                                         Persistent browser profile directory
  -VncPort, --vnc-port VALUE             Host VNC port, default 5900
  -Image, --image VALUE                  Docker image, default joinly-local:latest
  -StorageAccountName, --storage-account-name VALUE
  -StorageAccountKey, --storage-account-key VALUE
  -FileShareName, --file-share-name VALUE
  -NoProfile, --no-profile               Run single mode without persistent profile
  -h, --help                             Show this help

Examples:
  ./run_bot.sh --mode single --meeting-link "https://teams.microsoft.com/meet/..."
  ./run_bot.sh -Mode auth-login
  ./run_bot.sh -Mode scheduler-start -ScheduleFile meeting_schedule.json
EOF
}

die() {
    echo "Error: $*" >&2
    exit 1
}

is_blank() {
    [[ -z "${1//[[:space:]]/}" ]]
}

to_host_path() {
    local path_value="$1"
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -am "$path_value"
        return
    fi

    if [[ -d "$path_value" ]]; then
        (cd "$path_value" && pwd -W 2>/dev/null) || realpath "$path_value"
        return
    fi

    local dir_name
    local base_name
    dir_name="$(dirname "$path_value")"
    base_name="$(basename "$path_value")"
    if (cd "$dir_name" && pwd -W >/dev/null 2>&1); then
        printf '%s/%s\n' "$(cd "$dir_name" && pwd -W)" "$base_name"
    else
        realpath "$path_value"
    fi
}

resolve_existing_file_path() {
    local path_value="$1"
    [[ -f "$path_value" ]] || die "File not found: $path_value"
    to_host_path "$path_value"
}

get_prompt_content() {
    [[ -f "custom_prompt.txt" ]] || die "File not found: custom_prompt.txt"
    tr '\r\n' '  ' < "custom_prompt.txt" | sed "s/\"/'/g"
}

docker_cmd() {
    MSYS_NO_PATHCONV=1 docker "$@"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -Mode|--mode)
            MODE="${2:-}"
            shift 2
            ;;
        -MeetingLink|--meeting-link)
            MEETING_LINK="${2:-}"
            shift 2
            ;;
        -CookiesFile|--cookies-file)
            COOKIES_FILE="${2:-}"
            shift 2
            ;;
        -ScheduleFile|--schedule-file)
            SCHEDULE_FILE="${2:-}"
            shift 2
            ;;
        -BrowserProfileDir|--browser-profile-dir)
            BROWSER_PROFILE_DIR="${2:-}"
            shift 2
            ;;
        -VncPort|--vnc-port)
            VNC_PORT="${2:-}"
            shift 2
            ;;
        -Image|--image)
            IMAGE="${2:-}"
            shift 2
            ;;
        -StorageAccountName|--storage-account-name)
            STORAGE_ACCOUNT_NAME="${2:-}"
            shift 2
            ;;
        -StorageAccountKey|--storage-account-key)
            STORAGE_ACCOUNT_KEY="${2:-}"
            shift 2
            ;;
        -FileShareName|--file-share-name)
            FILE_SHARE_NAME="${2:-}"
            shift 2
            ;;
        -NoProfile|--no-profile)
            NO_PROFILE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            if [[ -z "$MEETING_LINK" && "$MODE" == "single" ]]; then
                MEETING_LINK="$1"
                shift
            else
                die "Unknown argument: $1"
            fi
            ;;
    esac
done

case "$MODE" in
    single|scheduler-start|scheduler-stop|scheduler-status|scheduler-logs|auth-login|import-cookies|upload-profile)
        ;;
    *)
        die "Invalid mode: $MODE"
        ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PROMPT_CONTENT="$(get_prompt_content)"
MCP_CONFIG_HOST_PATH="$(resolve_existing_file_path "mcp_config_docker.json")"
WORKSPACE_HOST_PATH="$(to_host_path ".")"
mkdir -p "$BROWSER_PROFILE_DIR"
BROWSER_PROFILE_HOST_PATH="$(to_host_path "$BROWSER_PROFILE_DIR")"

case "$MODE" in
    single)
        is_blank "$MEETING_LINK" && die "-MeetingLink is required when -Mode single is used."

        echo "Stopping existing one-shot container (if any)..."
        docker_cmd stop "$SINGLE_CONTAINER_NAME" >/dev/null 2>&1 || true
        docker_cmd rm -f "$SINGLE_CONTAINER_NAME" >/dev/null 2>&1 || true

        if [[ "$NO_PROFILE" -eq 1 ]]; then
            echo "Starting one-shot joinly bot in Docker (guest mode, no browser profile)..."
            echo "Connect a VNC viewer to localhost:$VNC_PORT to monitor the browser."
            if ! docker_cmd run -d \
                --name "$SINGLE_CONTAINER_NAME" \
                --shm-size=2gb \
                --env-file .env \
                --entrypoint /app/.venv/bin/python \
                -e "PYTHONPATH=$CONTAINER_PYTHON_PATH" \
                -p "$VNC_PORT:5900" \
                -v "$WORKSPACE_HOST_PATH:/workspace" \
                -v "$MCP_CONFIG_HOST_PATH:/runtime_mcp_config.json:ro" \
                -w /workspace \
                "$IMAGE" \
                -m joinly.main \
                --client \
                --name Alex \
                --name-trigger \
                --vnc-server \
                --transcription-controller-arg no_speech_event_delay=0.6 \
                --prompt "$PROMPT_CONTENT" \
                --mcp-config /runtime_mcp_config.json \
                "$MEETING_LINK" >/dev/null; then
                die "Failed to start bot container."
            fi
        else
            echo "Starting one-shot joinly bot in Docker..."
            echo "Connect a VNC viewer to localhost:$VNC_PORT to monitor the browser."
            if ! docker_cmd run -d \
                --name "$SINGLE_CONTAINER_NAME" \
                --shm-size=2gb \
                --env-file .env \
                --entrypoint /app/.venv/bin/python \
                -e "PYTHONPATH=$CONTAINER_PYTHON_PATH" \
                -p "$VNC_PORT:5900" \
                -v "$WORKSPACE_HOST_PATH:/workspace" \
                -v "$BROWSER_PROFILE_HOST_PATH:/browser-profile" \
                -v "$MCP_CONFIG_HOST_PATH:/runtime_mcp_config.json:ro" \
                -w /workspace \
                "$IMAGE" \
                -m joinly.main \
                --client \
                --name Alex \
                --name-trigger \
                --vnc-server \
                --transcription-controller-arg no_speech_event_delay=0.6 \
                --meeting-provider-arg browser_profile_dir=/browser-profile \
                --prompt "$PROMPT_CONTENT" \
                --mcp-config /runtime_mcp_config.json \
                "$MEETING_LINK" >/dev/null; then
                die "Failed to start bot container."
            fi
        fi

        echo "Bot is running in background container '$SINGLE_CONTAINER_NAME'."
        echo "Stream logs with: docker logs -f $SINGLE_CONTAINER_NAME"
        ;;

    scheduler-start)
        SCHEDULE_HOST_PATH="$(resolve_existing_file_path "$SCHEDULE_FILE")"

        echo "Restarting scheduler container..."
        docker_cmd stop "$SCHEDULER_CONTAINER_NAME" >/dev/null 2>&1 || true
        docker_cmd rm -f "$SCHEDULER_CONTAINER_NAME" >/dev/null 2>&1 || true

        if ! docker_cmd run -d \
            --name "$SCHEDULER_CONTAINER_NAME" \
            --restart unless-stopped \
            --shm-size=2gb \
            --env-file .env \
            --entrypoint /app/.venv/bin/python \
            -e "PYTHONPATH=$CONTAINER_PYTHON_PATH" \
            -v "$WORKSPACE_HOST_PATH:/workspace" \
            -v "$BROWSER_PROFILE_HOST_PATH:/browser-profile" \
            -v "$MCP_CONFIG_HOST_PATH:/runtime_mcp_config.json:ro" \
            -v "$SCHEDULE_HOST_PATH:/runtime_meeting_schedule.json:ro" \
            -w /workspace \
            "$IMAGE" \
            -m joinly.main \
            --client \
            --name Alex \
            --name-trigger \
            --transcription-controller-arg no_speech_event_delay=0.6 \
            --meeting-provider-arg browser_profile_dir=/browser-profile \
            --prompt "$PROMPT_CONTENT" \
            --mcp-config /runtime_mcp_config.json \
            --schedule-file /runtime_meeting_schedule.json >/dev/null; then
            die "Failed to start scheduler container."
        fi

        echo "Scheduler is running in background container '$SCHEDULER_CONTAINER_NAME'."
        echo "Edit '$SCHEDULE_FILE' any time to change meeting timings; scheduler reloads automatically."
        echo "Use -Mode scheduler-logs to monitor activity."
        ;;

    scheduler-stop)
        echo "Stopping scheduler container..."
        scheduler_was_running=1
        docker_cmd stop "$SCHEDULER_CONTAINER_NAME" >/dev/null 2>&1 || scheduler_was_running=0
        docker_cmd rm -f "$SCHEDULER_CONTAINER_NAME" >/dev/null 2>&1 || true
        if [[ "$scheduler_was_running" -eq 0 ]]; then
            echo "Scheduler container was not running."
        fi
        ;;

    scheduler-status)
        echo "Scheduler container status:"
        docker_cmd ps -a --filter "name=^${SCHEDULER_CONTAINER_NAME}$"
        ;;

    scheduler-logs)
        echo "Streaming scheduler logs (Ctrl+C to exit)..."
        docker_cmd logs -f "$SCHEDULER_CONTAINER_NAME"
        ;;

    auth-login)
        echo "Stopping existing one-shot container (if any)..."
        docker_cmd stop "$SINGLE_CONTAINER_NAME" >/dev/null 2>&1 || true
        docker_cmd rm -f "$SINGLE_CONTAINER_NAME" >/dev/null 2>&1 || true

        echo "Starting Teams auth browser with persistent profile..."
        echo "Connect a VNC viewer to localhost:$VNC_PORT and sign in to Teams."
        echo "When Teams is fully signed in, stop this command with Ctrl+C."
        docker_cmd run --rm \
            --name "$SINGLE_CONTAINER_NAME" \
            --shm-size=2gb \
            --env-file .env \
            --entrypoint /app/.venv/bin/python \
            -e "PYTHONPATH=$CONTAINER_PYTHON_PATH" \
            -p "$VNC_PORT:$VNC_PORT" \
            -v "$WORKSPACE_HOST_PATH:/workspace" \
            -v "$BROWSER_PROFILE_HOST_PATH:/browser-profile" \
            -w /workspace \
            "$IMAGE" \
            -m joinly.providers.browser.auth_login \
            --profile-dir /browser-profile \
            --vnc-port "$VNC_PORT"
        ;;

    import-cookies)
        COOKIES_HOST_PATH="$(resolve_existing_file_path "$COOKIES_FILE")"

        echo "Stopping existing one-shot container (if any)..."
        docker_cmd stop "$SINGLE_CONTAINER_NAME" >/dev/null 2>&1 || true
        docker_cmd rm -f "$SINGLE_CONTAINER_NAME" >/dev/null 2>&1 || true

        echo "Importing Teams cookies into persistent browser profile..."
        docker_cmd run --rm \
            --name "$SINGLE_CONTAINER_NAME" \
            --shm-size=2gb \
            --env-file .env \
            --entrypoint /app/.venv/bin/python \
            -e "PYTHONPATH=$CONTAINER_PYTHON_PATH" \
            -v "$WORKSPACE_HOST_PATH:/workspace" \
            -v "$BROWSER_PROFILE_HOST_PATH:/browser-profile" \
            -v "$COOKIES_HOST_PATH:/runtime_teams_cookies.json:ro" \
            -w /workspace \
            "$IMAGE" \
            -m joinly.providers.browser.import_cookies \
            --profile-dir /browser-profile \
            --cookies-file /runtime_teams_cookies.json
        ;;

    upload-profile)
        is_blank "$STORAGE_ACCOUNT_NAME" && die "-StorageAccountName (or env AZURE_STORAGE_ACCOUNT_NAME) is required."
        is_blank "$STORAGE_ACCOUNT_KEY" && die "-StorageAccountKey (or env AZURE_STORAGE_ACCOUNT_KEY) is required."
        is_blank "$FILE_SHARE_NAME" && die "-FileShareName (or env AZURE_FILE_SHARE_NAME) is required."
        [[ -d "$BROWSER_PROFILE_DIR" ]] || die "Browser profile directory not found: $BROWSER_PROFILE_HOST_PATH. Run auth-login or import-cookies first."

        echo "Uploading auth-critical profile files to Azure File Share '$FILE_SHARE_NAME'..."
        echo "  Source : $BROWSER_PROFILE_HOST_PATH"
        echo "  Account: $STORAGE_ACCOUNT_NAME"

        for dir_name in "Default" "Default/Network" "Default/Local Storage" "Default/Session Storage"; do
            az storage directory create \
                --account-name "$STORAGE_ACCOUNT_NAME" \
                --account-key "$STORAGE_ACCOUNT_KEY" \
                --share-name "$FILE_SHARE_NAME" \
                --name "$dir_name" >/dev/null 2>&1
        done

        single_files=(
            "Local State|Local State"
            "Default/Cookies|Default/Cookies"
            "Default/Preferences|Default/Preferences"
            "Default/Secure Preferences|Default/Secure Preferences"
            "Default/Extension Cookies|Default/Extension Cookies"
        )
        for item in "${single_files[@]}"; do
            IFS='|' read -r src_rel dst_path <<< "$item"
            src_path="$BROWSER_PROFILE_DIR/$src_rel"
            [[ -f "$src_path" ]] || continue
            src_host_path="$(to_host_path "$src_path")"
            az storage file upload \
                --account-name "$STORAGE_ACCOUNT_NAME" \
                --account-key "$STORAGE_ACCOUNT_KEY" \
                --share-name "$FILE_SHARE_NAME" \
                --source "$src_host_path" \
                --path "$dst_path" >/dev/null 2>&1
            echo "  Uploaded: $src_rel"
        done

        dir_uploads=(
            "Default/Network|$FILE_SHARE_NAME/Default/Network"
            "Default/Local Storage|$FILE_SHARE_NAME/Default/Local Storage"
            "Default/Session Storage|$FILE_SHARE_NAME/Default/Session Storage"
        )
        for item in "${dir_uploads[@]}"; do
            IFS='|' read -r src_rel dst_path <<< "$item"
            src_path="$BROWSER_PROFILE_DIR/$src_rel"
            [[ -d "$src_path" ]] || continue
            src_host_path="$(to_host_path "$src_path")"
            az storage file upload-batch \
                --account-name "$STORAGE_ACCOUNT_NAME" \
                --account-key "$STORAGE_ACCOUNT_KEY" \
                --destination "$dst_path" \
                --source "$src_host_path" >/dev/null 2>&1
            echo "  Uploaded dir: $src_rel"
        done

        echo ""
        echo "Auth profile uploaded. ACI containers will mount this at /browser-profile."
        echo "Re-run after each auth-login to keep the share in sync."
        ;;
esac