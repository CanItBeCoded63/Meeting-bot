param (
    [ValidateSet("single", "scheduler-start", "scheduler-stop", "scheduler-status", "scheduler-logs", "auth-login", "import-cookies", "upload-profile")]
    [string]$Mode = "single",

    [ValidateSet("alex", "aria", "sam", "mira")]
    [string]$Agent = "alex",

    [string]$MeetingLink,

    [string]$CookiesFile = ".joinly-teams-cookies.json",

    [string]$ScheduleFile = "meeting_schedule.json",

    [string]$BrowserProfileDir,

    [int]$VncPort = 5900,

    [string]$Image = "joinly-local:latest",

    [string]$StorageAccountName = $env:AZURE_STORAGE_ACCOUNT_NAME,

    [string]$StorageAccountKey = $env:AZURE_STORAGE_ACCOUNT_KEY,

    [string]$FileShareName = $env:AZURE_FILE_SHARE_NAME,

    [int]$StopTimeoutSeconds = 120,

    [switch]$NoProfile
)

$AgentId = $Agent.Trim().ToLowerInvariant()
switch ($AgentId) {
    "alex" { $AgentName = "Alex" }
    "aria" { $AgentName = "Aria" }
    "sam" { $AgentName = "Sam" }
    "mira" { $AgentName = "Mira" }
}

if ([string]::IsNullOrWhiteSpace($BrowserProfileDir)) {
    if ($AgentId -eq "alex" -and (Test-Path -Path ".joinly-browser-profile" -PathType Container)) {
        $BrowserProfileDir = ".joinly-browser-profile"
    } else {
        $BrowserProfileDir = ".joinly-browser-profile-$AgentId"
    }
}

$SingleContainerName = "joinly-$AgentId"
$SchedulerContainerName = "joinly-scheduler-$AgentId"

function Get-PromptContent([string]$AgentId) {
    $promptPath = "agents/$AgentId/prompts.yml"
    if (-not (Test-Path -Path $promptPath -PathType Leaf)) {
        throw "Prompt file not found for agent $AgentId`: $promptPath"
    }

    $promptLines = New-Object System.Collections.Generic.List[string]
    $inPrompt = $false
    foreach ($line in Get-Content -Path $promptPath) {
        if (-not $inPrompt) {
            if ($line -match '^prompt:\s*\|\s*$') {
                $inPrompt = $true
            }
            continue
        }

        if ($line -match '^\S' -and -not [string]::IsNullOrWhiteSpace($line)) {
            break
        }
        $promptLines.Add(($line -replace '^  ', ''))
    }

    if ($promptLines.Count -eq 0) {
        throw "No prompt block found in $promptPath. Expected: prompt: |"
    }

    $promptContent = $promptLines -join " "
    return $promptContent -replace "`r`n", " " -replace "`n", " " -replace "`"", "'"
}

function Read-AgentProviderConfig([string]$AgentId) {
    $path = "agents/$AgentId/providers.yml"
    $envArgs = @()
    $cliArgs = @()
    $selfAliases = @()
    if (-not (Test-Path -Path $path -PathType Leaf)) {
        return [pscustomobject]@{ EnvArgs = $envArgs; CliArgs = $cliArgs }
    }

    $currentSection = ""
    $inJoinlyArgs = $false
    $currentJoinlyArgService = ""
    foreach ($rawLine in Get-Content -Path $path) {
        $line = $rawLine.TrimEnd()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
            continue
        }

        if ($currentSection -eq "self_aliases" -and $line -match '^\s{2}\S') {
            $currentSection = "joinly"
        }

        if ($line -match '^(llm|joinly):\s*$') {
            $currentSection = $Matches[1]
            $inJoinlyArgs = $false
            $currentJoinlyArgService = ""
            continue
        }

        if ($currentSection -eq "llm" -and $line -match '^\s{2}provider:\s*(.+)\s*$') {
            $value = Expand-AgentProviderValue $Matches[1]
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $envArgs += @("-e", "JOINLY_LLM_PROVIDER=$value")
            }
        }
        elseif ($currentSection -eq "llm" -and $line -match '^\s{2}model:\s*(.+)\s*$') {
            $value = Expand-AgentProviderValue $Matches[1]
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $envArgs += @("-e", "JOINLY_LLM_MODEL=$value")
            }
        }
        elseif ($currentSection -eq "joinly" -and $line -match '^\s{2}(language|vad|stt|tts):\s*(.+)\s*$') {
            $key = $Matches[1].ToUpperInvariant()
            $value = Expand-AgentProviderValue $Matches[2]
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $envArgs += @("-e", "JOINLY_$key=$value")
            }
        }
        elseif ($currentSection -eq "joinly" -and $line -match '^\s{2}self_aliases:\s*$') {
            $currentSection = "self_aliases"
        }
        elseif ($currentSection -eq "self_aliases" -and $line -match '^\s{4}-\s*(.+)\s*$') {
            $value = Expand-AgentProviderValue $Matches[1]
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $selfAliases += $value
            }
        }
        elseif ($currentSection -eq "joinly" -and $line -match '^\s{2}args:\s*$') {
            $inJoinlyArgs = $true
            $currentJoinlyArgService = ""
        }
        elseif ($currentSection -eq "joinly" -and $inJoinlyArgs -and $line -match '^\s{4}(vad|stt|tts):\s*(?:\{\})?\s*$') {
            $currentJoinlyArgService = $Matches[1]
        }
        elseif ($currentSection -eq "joinly" -and $inJoinlyArgs -and -not [string]::IsNullOrWhiteSpace($currentJoinlyArgService) -and $line -match '^\s{6}([A-Za-z0-9_]+):\s*(.+)\s*$') {
            $argName = $Matches[1]
            $value = Expand-AgentProviderValue $Matches[2]
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $cliArgs += @("--$currentJoinlyArgService-arg", "$argName=$value")
            }
        }
    }
    if ($selfAliases.Count -gt 0) {
        $envArgs += @("-e", "JOINLY_SELF_ALIASES=$($selfAliases -join ',')")
    }
    return [pscustomobject]@{ EnvArgs = $envArgs; CliArgs = $cliArgs }
}

function Expand-AgentProviderValue([string]$RawValue) {
    $value = $RawValue.Trim().Trim('"').Trim("'")
    if ($value -match '^\$\{([^}]+)\}$') {
        $envValue = [Environment]::GetEnvironmentVariable($Matches[1])
        return $envValue
    }
    return $value
}

function Resolve-ExistingFilePath([string]$PathValue) {
    if (-not (Test-Path -Path $PathValue -PathType Leaf)) {
        throw "File not found: $PathValue"
    }
    return (Resolve-Path -Path $PathValue).Path
}

$PromptContent = Get-PromptContent $AgentId
$McpConfigHostPath = Resolve-ExistingFilePath "agents/$AgentId/mcp_config.json"
$AgentProviderConfig = Read-AgentProviderConfig $AgentId
$AgentProviderEnvArgs = $AgentProviderConfig.EnvArgs
$AgentProviderCliArgs = $AgentProviderConfig.CliArgs
$WorkspaceHostPath = (Resolve-Path -Path ".").Path
$BrowserProfileHostPath = Join-Path $WorkspaceHostPath $BrowserProfileDir
$ContainerPythonPath = "/workspace:/workspace/client:/workspace/common"
New-Item -ItemType Directory -Force -Path $BrowserProfileHostPath | Out-Null

switch ($Mode) {
    "single" {
        if ([string]::IsNullOrWhiteSpace($MeetingLink)) {
            throw "-MeetingLink is required when -Mode single is used."
        }

        Write-Host "Stopping existing one-shot container (if any)..."
        docker stop -t $StopTimeoutSeconds $SingleContainerName 2>$null
        docker rm -f $SingleContainerName 2>$null

        if ($NoProfile) {
            Write-Host "Starting one-shot joinly bot in Docker (guest mode, no browser profile)..."
            Write-Host "Connect a VNC viewer to localhost:$VncPort to monitor the browser."
            docker run -d `
                --name $SingleContainerName `
                --shm-size=2gb `
                --env-file .env `
                @AgentProviderEnvArgs `
                --entrypoint /app/.venv/bin/python `
                -e PYTHONPATH=$ContainerPythonPath `
                -e "JOINLY_MEMORY_AGENT_ID=$AgentId" `
                -p "${VncPort}:5900" `
                -v "${WorkspaceHostPath}:/workspace" `
                -v "${McpConfigHostPath}:/runtime_mcp_config.json:ro" `
                -w /workspace `
                $Image `
                -m joinly.main `
                --client `
                --name $AgentName `
                --name-trigger `
                --vnc-server `
                --transcription-controller-arg no_speech_event_delay=0.6 `
                @AgentProviderCliArgs `
                --prompt "$PromptContent" `
                --mcp-config /runtime_mcp_config.json `
                "$MeetingLink" | Out-Null
        } else {
            Write-Host "Starting one-shot joinly bot in Docker..."
            Write-Host "Connect a VNC viewer to localhost:$VncPort to monitor the browser."
            docker run -d `
                --name $SingleContainerName `
                --shm-size=2gb `
                --env-file .env `
                @AgentProviderEnvArgs `
                --entrypoint /app/.venv/bin/python `
                -e PYTHONPATH=$ContainerPythonPath `
                -e "JOINLY_MEMORY_AGENT_ID=$AgentId" `
                -p "${VncPort}:5900" `
                -v "${WorkspaceHostPath}:/workspace" `
                -v "${BrowserProfileHostPath}:/browser-profile" `
                -v "${McpConfigHostPath}:/runtime_mcp_config.json:ro" `
                -w /workspace `
                $Image `
                -m joinly.main `
                --client `
                --name $AgentName `
                --name-trigger `
                --vnc-server `
                --transcription-controller-arg no_speech_event_delay=0.6 `
                --meeting-provider-arg browser_profile_dir=/browser-profile `
                @AgentProviderCliArgs `
                --prompt "$PromptContent" `
                --mcp-config /runtime_mcp_config.json `
                "$MeetingLink" | Out-Null
        }

        if ($LASTEXITCODE -ne 0) {
            throw "Failed to start bot container."
        }
        Write-Host "Bot is running in background container '$SingleContainerName'."
        Write-Host "Stream logs with: docker logs -f $SingleContainerName"
    }

    "scheduler-start" {
        $ScheduleHostPath = Resolve-ExistingFilePath $ScheduleFile

        Write-Host "Restarting scheduler container..."
            docker stop -t $StopTimeoutSeconds $SchedulerContainerName 2>$null
        docker rm -f $SchedulerContainerName 2>$null

        docker run -d `
            --name $SchedulerContainerName `
            --restart unless-stopped `
            --shm-size=2gb `
            --env-file .env `
            @AgentProviderEnvArgs `
            --entrypoint /app/.venv/bin/python `
            -e PYTHONPATH=$ContainerPythonPath `
            -e "JOINLY_MEMORY_AGENT_ID=$AgentId" `
            -v "${WorkspaceHostPath}:/workspace" `
            -v "${BrowserProfileHostPath}:/browser-profile" `
            -v "${McpConfigHostPath}:/runtime_mcp_config.json:ro" `
            -v "${ScheduleHostPath}:/runtime_meeting_schedule.json:ro" `
            -w /workspace `
            $Image `
            -m joinly.main `
            --client `
            --name $AgentName `
            --name-trigger `
            --transcription-controller-arg no_speech_event_delay=0.6 `
            --meeting-provider-arg browser_profile_dir=/browser-profile `
            @AgentProviderCliArgs `
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
            docker stop -t $StopTimeoutSeconds $SchedulerContainerName 2>$null
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
        docker stop -t $StopTimeoutSeconds $SingleContainerName 2>$null
        docker rm -f $SingleContainerName 2>$null

        Write-Host "Starting Teams auth browser with persistent profile..."
        Write-Host "Connect a VNC viewer to localhost:$VncPort and sign in to Teams."
        Write-Host "When Teams is fully signed in, stop this command with Ctrl+C."
        docker run --rm `
            --name $SingleContainerName `
            --shm-size=2gb `
            --env-file .env `
            --entrypoint /app/.venv/bin/python `
            -e PYTHONPATH=$ContainerPythonPath `
            -p "${VncPort}:${VncPort}" `
            -v "${WorkspaceHostPath}:/workspace" `
            -v "${BrowserProfileHostPath}:/browser-profile" `
            -w /workspace `
            $Image `
            -m joinly.providers.browser.auth_login `
            --profile-dir /browser-profile `
            --vnc-port $VncPort
    }

    "import-cookies" {
        $CookiesHostPath = Resolve-ExistingFilePath $CookiesFile

        Write-Host "Stopping existing one-shot container (if any)..."
        docker stop -t $StopTimeoutSeconds $SingleContainerName 2>$null
        docker rm -f $SingleContainerName 2>$null

        Write-Host "Importing Teams cookies into persistent browser profile..."
        docker run --rm `
            --name $SingleContainerName `
            --shm-size=2gb `
            --env-file .env `
            --entrypoint /app/.venv/bin/python `
            -e PYTHONPATH=$ContainerPythonPath `
            -v "${WorkspaceHostPath}:/workspace" `
            -v "${BrowserProfileHostPath}:/browser-profile" `
            -v "${CookiesHostPath}:/runtime_teams_cookies.json:ro" `
            -w /workspace `
            $Image `
            -m joinly.providers.browser.import_cookies `
            --profile-dir /browser-profile `
            --cookies-file /runtime_teams_cookies.json
    }

    "upload-profile" {
        if ([string]::IsNullOrWhiteSpace($StorageAccountName)) {
            throw "-StorageAccountName (or env AZURE_STORAGE_ACCOUNT_NAME) is required."
        }
        if ([string]::IsNullOrWhiteSpace($StorageAccountKey)) {
            throw "-StorageAccountKey (or env AZURE_STORAGE_ACCOUNT_KEY) is required."
        }
        if ([string]::IsNullOrWhiteSpace($FileShareName)) {
            throw "-FileShareName (or env AZURE_FILE_SHARE_NAME) is required."
        }
        if (-not (Test-Path -Path $BrowserProfileHostPath -PathType Container)) {
            throw "Browser profile directory not found: $BrowserProfileHostPath. Run auth-login or import-cookies first."
        }

        Write-Host "Uploading auth-critical profile files to Azure File Share '$FileShareName'..."
        Write-Host "  Source : $BrowserProfileHostPath"
        Write-Host "  Account: $StorageAccountName"

        # Create required directories on File Share
        foreach ($dir in @("Default", "Default/Network", "Default/Local Storage", "Default/Session Storage")) {
            az storage directory create `
                --account-name $StorageAccountName `
                --account-key $StorageAccountKey `
                --share-name $FileShareName `
                --name $dir 2>&1 | Out-Null
        }

        # Upload auth-critical files only — skip Cache, WebStorage, GPUCache
        $SingleFiles = @(
            @{ src = "Local State";              dst = "Local State" },
            @{ src = "Default\Cookies";          dst = "Default/Cookies" },
            @{ src = "Default\Preferences";      dst = "Default/Preferences" },
            @{ src = "Default\Secure Preferences"; dst = "Default/Secure Preferences" },
            @{ src = "Default\Extension Cookies"; dst = "Default/Extension Cookies" }
        )
        foreach ($item in $SingleFiles) {
            $src = Join-Path $BrowserProfileHostPath $item.src
            if (-not (Test-Path $src)) { continue }
            az storage file upload `
                --account-name $StorageAccountName `
                --account-key $StorageAccountKey `
                --share-name $FileShareName `
                --source $src `
                --path $item.dst 2>&1 | Out-Null
            Write-Host "  Uploaded: $($item.src)"
        }

        # Upload auth-critical directories via batch
        $DirUploads = @(
            @{ src = "Default\Network";          dst = "$FileShareName/Default/Network" },
            @{ src = "Default\Local Storage";    dst = "$FileShareName/Default/Local Storage" },
            @{ src = "Default\Session Storage";  dst = "$FileShareName/Default/Session Storage" }
        )
        foreach ($item in $DirUploads) {
            $src = Join-Path $BrowserProfileHostPath $item.src
            if (-not (Test-Path $src)) { continue }
            az storage file upload-batch `
                --account-name $StorageAccountName `
                --account-key $StorageAccountKey `
                --destination $item.dst `
                --source $src 2>&1 | Out-Null
            Write-Host "  Uploaded dir: $($item.src)"
        }

        Write-Host ""
        Write-Host "Auth profile uploaded. ACI containers will mount this at /browser-profile."
        Write-Host "Re-run after each auth-login to keep the share in sync."
    }
}
