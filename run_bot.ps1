param (
    [Parameter(Mandatory=$true)]
    [string]$MeetingLink
)

# ── Stop and remove any existing bot container ───────────────────────────────
Write-Host "Stopping any existing bot containers..."
docker stop meeting-bot-server 2>$null
docker rm -f meeting-bot-server 2>$null

# ── Load custom prompt ────────────────────────────────────────────────────────
$PromptContent = Get-Content -Raw -Path "custom_prompt.txt"
$PromptContent = $PromptContent -replace "`r`n", " " -replace "`n", " " -replace "`"", "'"

# ── Run the full bot in Docker ────────────────────────────────────────────────
Write-Host "Starting meeting bot in Docker with MCP..."
docker run --rm `
    --name meeting-bot-server `
    --shm-size=2gb `
    --env-file .env `
    -v "${PWD}/mcp_config_docker.json:/app/mcp_config_docker.json" `
    meeting-bot-local:latest `
    --name Alex `
    --name-trigger `
    --transcription-controller-arg no_speech_event_delay=0.6 `
    --prompt "$PromptContent" `
    --mcp-config /app/mcp_config_docker.json `
    "$MeetingLink"
