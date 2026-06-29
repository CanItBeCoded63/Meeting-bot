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

## One-Time Teams Login

Alex can reuse a persistent Playwright/Chromium profile so Teams sees the bot as
a signed-in internal user instead of a fresh guest browser every run.

1. Start the login browser:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -Mode auth-login
```

2. Open a VNC viewer and connect to:

```text
127.0.0.1:5900
```

3. Sign in to Teams in the VNC browser. Leave the terminal running while you
	complete Microsoft login/MFA.
4. After Teams is fully signed in, stop the auth-login terminal with `Ctrl+C`.

The saved browser profile lives in `.joinly-browser-profile/`. It is ignored by
Git because it contains local auth state. Normal `single` and `scheduler-start`
runs automatically mount and reuse this profile.

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
