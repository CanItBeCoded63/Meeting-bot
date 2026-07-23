# Meeting Bot

Setup instructions for this personal project.

## Setup Files

- `.env.example` - environment variable template
- `.env` - local environment variables (create from `.env.example`)
- `mcp_config.json` - MCP configuration for local runs
- `mcp_config_docker.json` - MCP configuration for Docker runs
- `run_bot.ps1` - helper script to start the bot in Docker
- `meeting_schedule.json` - schedule file for auto-join mode
- `pyproject.toml` - workspace dependencies and tool configuration

## Prerequisites

- Python 3.11+
- `uv` installed: https://docs.astral.sh/uv/
- Docker Desktop (optional, for container runs)
- API key for your selected LLM provider

## Local Setup

1. Install dependencies:

```bash
uv sync --frozen
```

2. Create your environment file:

```powershell
Copy-Item .env.example .env
```

3. Edit `.env` and replace placeholder values with your actual provider, model, and API key.

4. Download required runtime assets:

```bash
uv run scripts/download_assets.py
```

## Run

Start the bot with the helper script:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode single -MeetingLink "<MeetingURL>"
```

## Cookie Flow

This flow imports exported Teams/Microsoft cookies into Alex's persistent
Chromium profile. Use it only with a bot-owned Teams account, and treat the cookie
JSON like a password.

### What Is Required

- A cookie export from a browser where the bot account is already signed in to
	Teams.
- The export saved as `.joinly-teams-cookies.json` in the repo root.
- Cookie JSON in either of these formats:
	- a plain JSON list of cookie objects
	- a Playwright `storage_state` object with a top-level `cookies` array
- Docker image `joinly-local:latest` built locally.
- `.env` configured with the bot's LLM/STT/TTS keys.

Do not commit `.joinly-teams-cookies.json`. It contains live login secrets.

### Commands in Order

1. Stop any running Alex container:

```powershell
docker stop joinly-server 2>$null
docker rm -f joinly-server 2>$null
```

2. If the active profile is signed in with the wrong Teams account, move it out
of the active path:

```powershell
if (Test-Path .joinly-browser-profile) {
    $backup = ".joinly-browser-profile.signed-out-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Move-Item .joinly-browser-profile $backup
}
```

3. Put the exported cookie file in the repo root:

```text
.joinly-teams-cookies.json
```

4. Import the cookies into Alex's persistent Chromium profile:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode import-cookies -CookiesFile .\.joinly-teams-cookies.json
```

This command writes the cookies into:

```text
.joinly-browser-profile/  ->  /browser-profile inside Docker
```

5. Run Alex with the imported profile:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode single -MeetingLink "<TeamsMeetingURL>"
```

6. For scheduler mode, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode scheduler-start
```

The profile is mounted by [run_bot.ps1](run_bot.ps1) with these arguments:

```powershell
-v "${BrowserProfileHostPath}:/browser-profile"
--meeting-provider-arg browser_profile_dir=/browser-profile
```

### Error We Got

The key cookie/profile issue we hit was:

```text
Teams guest name field not shown; assuming signed-in profile.
```

Meaning: Alex was reusing an existing signed-in Teams profile. In our case, that
profile was signed in with your own Teams account, so Alex appeared as you and
could conflict when you joined the same meeting yourself. The fix is to reset the
active profile and import/sign in with a separate bot Teams account.

### Limitation

Teams authentication is often more than cookies. It can also depend on local
storage, session storage, device state, MFA, and refresh tokens. If cookie import
still opens a login/MFA screen, use the VNC Flow below because it saves the full
browser profile naturally.

## VNC Flow

VNC is used only for the one-time human login. It opens Alex's Docker browser on
a virtual desktop so you can sign in to Teams and complete MFA. After that,
Chromium saves the auth state into `.joinly-browser-profile/`, and normal bot
runs can use the saved profile without VNC.

Use a separate bot Microsoft/Teams account for this flow. Do not sign Alex in
with your personal account if you also need to join the same meeting yourself.

Default VNC command we tried:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode auth-login
```

The container started and printed:

```text
Started VNC server on port: 5900
Opened https://teams.microsoft.com/v2/
Connect a VNC viewer to localhost:5900 and sign in.
```

Error/problem we got: VNC did not connect to Alex correctly because port `5900`
was already used on the Windows host by TightVNC.

Commands used to diagnose the port conflict:

```powershell
netstat -ano | Select-String -Pattern "5900|5901"
Get-Process -Id 3704,26064,11288 -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,Path
```

The important process was:

```text
tvnserver
```

Meaning: Windows TightVNC was using the default VNC port `5900`, so a VNC viewer
could connect to the wrong desktop or fail.

Command that fixed it by using port `5901`:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode auth-login -VncPort 5901
```

Connect from TigerVNC/RealVNC to:

```text
127.0.0.1::5901
```

Some VNC clients use one colon instead:

```text
127.0.0.1:5901
```

Steps after connecting:

1. Sign in to Teams in the VNC browser.
2. Complete Microsoft login/MFA.
3. Wait until the main Teams screen loads.
4. Stop the auth-login terminal with `Ctrl+C`.

Browser startup error we also saw during testing:

```text
RuntimeError: Client failed to connect
ProcessLookupError
browser_session.py -> self._proc.terminate()
```

Meaning: Chromium exited before the code finished reading the DevTools endpoint,
and cleanup tried to terminate a process that had already disappeared. The
browser cleanup code was hardened so this startup failure no longer crashes while
cleaning up.

### Running After VNC Login

After the profile is saved, start Alex for one meeting with:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode single -MeetingLink "<TeamsMeetingURL>"
```

Or start the scheduler with:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode scheduler-start
```

Both modes mount `.joinly-browser-profile/` into Docker and pass
`browser_profile_dir=/browser-profile`, so Teams receives the saved browser
cookies/session and Alex joins as the signed-in bot user.

### Resetting or Signing Out Alex

To remove the active saved Teams login without deleting it permanently, stop the
bot and move the profile out of the active path:

```powershell
docker stop joinly-server 2>$null
$backup = ".joinly-browser-profile.signed-out-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Move-Item .joinly-browser-profile $backup
```

The next run creates a fresh browser profile. To sign in again, rerun auth-login
with the VNC port:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode auth-login -VncPort 5901
```

Never commit, upload, or share `.joinly-browser-profile/`. It contains local auth
state and is intentionally ignored by Git.

## Scheduled Auto-Join

Use scheduler mode to keep one Docker container running and auto-join meetings at
configured times.

1. Edit `meeting_schedule.json` with your meeting URL, time, days, and timezone.
2. Optional but recommended for Teams: run `auth-login` once so Alex joins with
	the saved Teams browser profile.
3. Start scheduler mode once:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode scheduler-start
```

4. Check scheduler status:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode scheduler-status
```

5. Follow scheduler logs:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode scheduler-logs
```

6. Stop scheduler:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode scheduler-stop
```

To change meeting timing, just edit `meeting_schedule.json` and save. The scheduler
reloads the file automatically. You do not need to rebuild the image or restart
Docker for schedule-only changes.

### Schedule File Format

```json
{
	"timezone": "Asia/Kolkata",
	"meetings": [
		{
			"id": "daily-standup",
			"url": "https://meet.google.com/replace-me",
			"time": "11:00",
			"days": ["mon", "tue", "wed", "thu", "fri"],
			"enabled": true
		}
	]
}
```

Notes:

- `time` must be 24-hour `HH:MM`.
- `days` accepts `mon` to `sun` (or full names like `monday`).
- `timezone` must be an IANA timezone (for example `Asia/Kolkata`, `UTC`, `Europe/London`).
- Set `enabled` to `false` to temporarily disable a meeting without deleting it.
- Only one scheduled meeting is run at a time. If one meeting is active, another
	due meeting waits for the next valid scheduled run.

## Docker Setup (Optional)

1. Build local image:

```bash
docker build -f docker/Dockerfile -t meeting-bot-local:latest .
```

2. Start with the helper script:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -MeetingLink "<MeetingURL>"
```
