# Inviting the Joinly Bot to a Meeting

The bot joins Teams meetings as an authenticated org user (no lobby wait, no guest label).

---

## Prerequisites

- Access to the Meeting Manager API (deployed in Azure)
- A Teams meeting link
- `MANAGER_API_KEY` from `azure-deploy/.env.dev`

---

## Quick Start

### 1. Get your meeting URL

In Teams, create or open a meeting and copy the join link.  
It looks like:
```
https://teams.microsoft.com/meet/<meeting-id>?p=<passcode>
```

### 2. Send the bot in

```bash
curl -X POST https://joinly-meeting-manager-dev.thankfulriver-f345ce39.eastus2.azurecontainerapps.io/meetings/join \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MANAGER_API_KEY" \
  -d '{"url": "<YOUR_MEETING_URL>", "name": "Alex"}'
```

Response:
```json
{
  "meeting_id": "4da0b7428b11",
  "status": "starting",
  "agent_containers": [{"agent_id": "default", "container_group": "joinly-meeting-4da0b7428b11-default"}]
}
```

Save the `meeting_id` — you'll need it to check status or remove the bot.

### 3. Wait for the bot to connect (~60-90 seconds)

```bash
curl https://joinly-meeting-manager-dev.thankfulriver-f345ce39.eastus2.azurecontainerapps.io/meetings/<meeting_id> \
  -H "X-API-Key: $MANAGER_API_KEY"
```

When `state` is `Running`, the bot is live in the meeting.

### 4. Remove the bot

```bash
curl -X DELETE https://joinly-meeting-manager-dev.thankfulriver-f345ce39.eastus2.azurecontainerapps.io/meetings/<meeting_id> \
  -H "X-API-Key: $MANAGER_API_KEY"
```

---

## Using the Notebook

Open `dev/join.ipynb` for an interactive version of the above steps.  
Run cells 1→2→3→4 to join. Run cell 6 to remove.

---

## What Happens Internally

```
curl POST /meetings/join
    │
    ▼
Meeting Manager (Azure Container App — always-on)
    │  creates ACI container group
    ▼
Azure Container Instance
    │  copies browser profile from File Share → local /tmp
    │  launches Chromium with authenticated profile
    │  opens Teams meeting, bypasses lobby (org user)
    ▼
Teams Meeting — bot appears as "Alex"
```

The bot:
- Joins **without lobby wait** (authenticated org profile)
- Appears as **internal org user** (not "Guest")
- Uses **Deepgram** for speech-to-text and text-to-speech
- Responds to speech via **GPT (Azure OpenAI)**
- Auto-leaves when alone in the meeting

---

## Re-authenticating the Browser Profile

The org profile stored in Azure File Share expires when Teams tokens expire (~90 days).  
To refresh it:

1. Start VNC session locally:
   ```powershell
   .\run_bot.ps1 vnc
   ```

2. Connect with TigerVNC to `localhost:5900`

3. Log into Teams in the Chromium window that opens

4. Upload the refreshed profile to Azure File Share:
   ```powershell
   .\run_bot.ps1 upload-profile
   ```

---

## Environment

| Resource | Value |
|---|---|
| Meeting Manager URL | `https://joinly-meeting-manager-dev.thankfulriver-f345ce39.eastus2.azurecontainerapps.io` |
| Resource Group | `rg_digian_va_join_dev` |
| Container Registry | `joinlyregdev.azurecr.io` |
| Storage Account | `joinlyprofiledev` |
| File Share | `browser-profile` |
| Region | `eastus2` |
