param (
    [Parameter(Mandatory=$true)]
    [string]$MeetingLink
)

# ── Stop and remove any existing joinly container ─────────────────────────────
Write-Host "Stopping any existing joinly containers..."
docker stop joinly-server 2>$null
docker rm -f joinly-server 2>$null

# ── Load custom prompt ────────────────────────────────────────────────────────
$PromptContent = Get-Content -Raw -Path "custom_prompt.txt"
$PromptContent = $PromptContent -replace "`r`n", " " -replace "`n", " " -replace "`"", "'"

# ── Run the full bot in Docker ────────────────────────────────────────────────
Write-Host "Starting joinly bot in Docker with MCP..."
docker run --rm `
    --name joinly-server `
    --shm-size=2gb `
    --env-file .env `
    -v "${PWD}/mcp_config_docker.json:/app/mcp_config_docker.json" `
    joinly-local:latest `
    --name Alex `
    --name-trigger `
    --transcription-controller-arg no_speech_event_delay=0.6 `
    --prompt "$PromptContent" `
    --mcp-config /app/mcp_config_docker.json `
    "$MeetingLink"
