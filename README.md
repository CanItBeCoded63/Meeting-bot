# Meeting Bot

Setup instructions for this personal project.

## Setup Files

- `.env.example` - environment variable template
- `.env` - local environment variables (create from `.env.example`)
- `mcp_config.json` - MCP configuration for local runs
- `mcp_config_docker.json` - MCP configuration for Docker runs
- `run_bot.ps1` - helper script to start the bot in Docker
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
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -MeetingLink "<MeetingURL>"
```

## Docker Setup (Optional)

1. Build local image:

```bash
docker build -f docker/Dockerfile -t meeting-bot-local:latest .
```

2. Start with the helper script:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_bot.ps1 -MeetingLink "<MeetingURL>"
```
