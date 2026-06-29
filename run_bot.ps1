param (
    [ValidateSet("single", "scheduler-start", "scheduler-stop", "scheduler-status", "scheduler-logs", "auth-login")]
    [string]$Mode = "single",

    [string]$MeetingLink,

    [string]$ScheduleFile = "meeting_schedule.json",

    [string]$BrowserProfileDir = ".joinly-browser-profile",

    [int]$VncPort = 5900,

    [string]$Image = "joinly-local:latest"
)

$SingleContainerName = "joinly-server"
$SchedulerContainerName = "joinly-scheduler"

function Get-PromptContent {
    $promptContent = Get-Content -Raw -Path "custom_prompt.txt"
    return $promptContent -replace "`r`n", " " -replace "`n", " " -replace "`"", "'"
}

function Resolve-ExistingFilePath([string]$PathValue) {
    if (-not (Test-Path -Path $PathValue -PathType Leaf)) {
        throw "File not found: $PathValue"
    }
    return (Resolve-Path -Path $PathValue).Path
}

$PromptContent = Get-PromptContent
$McpConfigHostPath = Resolve-ExistingFilePath "mcp_config_docker.json"
$WorkspaceHostPath = (Resolve-Path -Path ".").Path
$BrowserProfileHostPath = Join-Path $WorkspaceHostPath $BrowserProfileDir
New-Item -ItemType Directory -Force -Path $BrowserProfileHostPath | Out-Null

switch ($Mode) {
    "single" {
        if ([string]::IsNullOrWhiteSpace($MeetingLink)) {
            throw "-MeetingLink is required when -Mode single is used."
        }

        Write-Host "Stopping existing one-shot container (if any)..."
        docker stop $SingleContainerName 2>$null
        docker rm -f $SingleContainerName 2>$null

        Write-Host "Starting one-shot joinly bot in Docker..."
        docker run --rm `
            --name $SingleContainerName `
            --shm-size=2gb `
            --env-file .env `
            --entrypoint /app/.venv/bin/python `
            -e PYTHONPATH=/workspace `
            -v "${WorkspaceHostPath}:/workspace" `
            -v "${BrowserProfileHostPath}:/browser-profile" `
            -v "${McpConfigHostPath}:/runtime_mcp_config.json:ro" `
            -w /workspace `
            $Image `
            -m joinly.main `
            --client `
            --name Alex `
            --name-trigger `
            --transcription-controller-arg no_speech_event_delay=0.6 `
            --meeting-provider-arg browser_profile_dir=/browser-profile `
            --prompt "$PromptContent" `
            --mcp-config /runtime_mcp_config.json `
            "$MeetingLink"
    }

    "scheduler-start" {
        $ScheduleHostPath = Resolve-ExistingFilePath $ScheduleFile

        Write-Host "Restarting scheduler container..."
        docker stop $SchedulerContainerName 2>$null
        docker rm -f $SchedulerContainerName 2>$null

        docker run -d `
            --name $SchedulerContainerName `
            --restart unless-stopped `
            --shm-size=2gb `
            --env-file .env `
            --entrypoint /app/.venv/bin/python `
            -e PYTHONPATH=/workspace `
            -v "${WorkspaceHostPath}:/workspace" `
            -v "${BrowserProfileHostPath}:/browser-profile" `
            -v "${McpConfigHostPath}:/runtime_mcp_config.json:ro" `
            -v "${ScheduleHostPath}:/runtime_meeting_schedule.json:ro" `
            -w /workspace `
            $Image `
            -m joinly.main `
            --client `
            --name Alex `
            --name-trigger `
            --transcription-controller-arg no_speech_event_delay=0.6 `
            --meeting-provider-arg browser_profile_dir=/browser-profile `
            --prompt "$PromptContent" `
            --mcp-config /runtime_mcp_config.json `
            --schedule-file /runtime_meeting_schedule.json | Out-Null

        if ($LASTEXITCODE -ne 0) {
            throw "Failed to start scheduler container."
        }

        Write-Host "Scheduler is running in background container '$SchedulerContainerName'."
        Write-Host "Edit '$ScheduleFile' any time to change meeting timings; scheduler reloads automatically."
        Write-Host "Use -Mode scheduler-logs to monitor activity."
    }

    "scheduler-stop" {
        Write-Host "Stopping scheduler container..."
        docker stop $SchedulerContainerName 2>$null
        docker rm -f $SchedulerContainerName 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Scheduler container was not running."
        }
    }

    "scheduler-status" {
        Write-Host "Scheduler container status:"
        docker ps -a --filter "name=^${SchedulerContainerName}$"
    }

    "scheduler-logs" {
        Write-Host "Streaming scheduler logs (Ctrl+C to exit)..."
        docker logs -f $SchedulerContainerName
    }

    "auth-login" {
        Write-Host "Stopping existing one-shot container (if any)..."
        docker stop $SingleContainerName 2>$null
        docker rm -f $SingleContainerName 2>$null

        Write-Host "Starting Teams auth browser with persistent profile..."
        Write-Host "Connect a VNC viewer to localhost:$VncPort and sign in to Teams."
        Write-Host "When Teams is fully signed in, stop this command with Ctrl+C."
        docker run --rm `
            --name $SingleContainerName `
            --shm-size=2gb `
            --env-file .env `
            --entrypoint /app/.venv/bin/python `
            -e PYTHONPATH=/workspace `
            -p "${VncPort}:${VncPort}" `
            -v "${WorkspaceHostPath}:/workspace" `
            -v "${BrowserProfileHostPath}:/browser-profile" `
            -w /workspace `
            $Image `
            -m joinly.providers.browser.auth_login `
            --profile-dir /browser-profile `
            --vnc-port $VncPort
    }
}
