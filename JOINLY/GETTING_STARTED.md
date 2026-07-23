# Joinly — Getting Started

Add AI agents to your video meetings. Agents join as real participants, listen, speak, and respond when called by name.

---

## What You Need

- Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) installed
- Docker Desktop running (for the agent server)
- A `.env` file with your API keys (see Setup below)
- A meeting link (Teams, Google Meet, or Zoom)

---

## Setup (One-Time)

### 1. Install dependencies

```bash
uv sync --frozen
```

### 2. Download AI models

```bash
uv run scripts/download_assets.py
```

### 3. Create your `.env` file

Copy the example and fill in your keys:

```bash
cp .env.example .env
```

Minimum required:

```env
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-key-here
DEEPGRAM_API_KEY=your-deepgram-key
JOINLY_LLM_PROVIDER=azure
JOINLY_LLM_MODEL=gpt-4o
```

---

## Making an Agent Join a Meeting

### Step 1 — Start the agent server

```bash
docker compose -f docker-compose.multi-agent.yml --env-file .env up -d joinly-aria
```

Wait a few seconds, then verify it's ready:

```bash
curl http://localhost:8001/health
# Expected: {"status":"healthy"}
```

### Step 2 — Join a meeting

```bash
uv run joinly --agents-config agents.yaml "YOUR_MEETING_URL"
```

Replace `YOUR_MEETING_URL` with your Teams, Meet, or Zoom link. Example:

```bash
uv run joinly --agents-config agents.yaml "https://teams.microsoft.com/meet/abc123"
```

Aria (the default agent) will appear in your meeting as a participant within ~30 seconds.

---

## Talking to Agents

Once in the meeting, just speak naturally:

| Say | What happens |
|---|---|
| `"Aria, can you introduce yourself?"` | Aria responds |
| `"Hey Aria, what's on the agenda?"` | Aria responds |
| `"Sam, what do you think?"` | Sam responds (if Sam is running) |
| Anything without a name | Aria responds (she's the default) |

Agents hear everything said in the meeting and respond when their name is mentioned.

---

## Choosing Which Agent to Run

### Default — Aria only (recommended for most meetings)

```bash
uv run joinly --agents-config agents.yaml "MEETING_URL"
```

### Run a specific agent

```bash
uv run joinly --agents-config agents.yaml --agent sam "MEETING_URL"
```
uv run joinly --agents-config agents.yaml --agent sam https://teams.microsoft.com/meet/221660438085425?p=8PmS2iiRROulYoa4zh
Start Sam's server first if using Sam:

```bash
docker compose -f docker-compose.multi-agent.yml --env-file .env up -d joinly-sam
```

### Run multiple agents together

Start both servers:

```bash
docker compose -f docker-compose.multi-agent.yml --env-file .env up -d joinly-aria joinly-sam
```

Join with both:

```bash
uv run joinly --agents-config agents.yaml --all-agents "MEETING_URL"
```

Both Aria and Sam join as separate participants. Aria responds by default; Sam only when you say "Sam".

---

## Available Agents

| Agent | Personality | Voice | Wake Word |
|---|---|---|---|
| **Aria** | Warm, friendly meeting facilitator | Female | "Aria" or "hey Aria" |
| **Sam** | Sharp strategic advisor | Male | "Sam" or "hey Sam" |

---

## Ending the Session

**Auto-end** — when all humans leave the meeting, agents detect the empty room and leave automatically within ~10 seconds.

**Manual end** — press `Ctrl+C` in the terminal where joinly is running. Agents leave cleanly.

**Ask the agent to leave** — say "Aria, please leave the meeting" and Aria will call leave and disconnect.

---

## Stopping the Server

When done for the day:

```bash
docker compose -f docker-compose.multi-agent.yml down
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Agent doesn't appear in meeting | Check `curl http://localhost:8001/health` — if no response, wait 30s and retry |
| Agent joins but doesn't respond | Check `DEEPGRAM_API_KEY` and `AZURE_OPENAI_API_KEY` in `.env` |
| Meeting link expired | Teams/Zoom links expire — get a fresh link and retry |
| "No module named..." error | Run `uv sync --frozen` again |
| Agent joins but can't be heard | Restart the Docker container: `docker compose -f docker-compose.multi-agent.yml restart joinly-aria` |

---

## Quick Reference

```bash
# Start server
docker compose -f docker-compose.multi-agent.yml --env-file .env up -d joinly-aria

# Join meeting (Aria, default)
uv run joinly --agents-config agents.yaml "MEETING_URL"

# Join with specific agent
uv run joinly --agents-config agents.yaml --agent sam "MEETING_URL"

# Join with all agents
uv run joinly --agents-config agents.yaml --all-agents "MEETING_URL"

# Stop servers
docker compose -f docker-compose.multi-agent.yml down
```
