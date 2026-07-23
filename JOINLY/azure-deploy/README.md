# Joinly — Azure Deployment

Deploys a **Meeting Manager** REST API to Azure Container Apps. The API dynamically provisions one Azure Container Instance (ACI) per meeting, each running a joinly agent that joins and participates autonomously.

---

## Architecture

```
Client (curl / your app)
    │
    ▼
Meeting Manager (Azure Container App — always on)
    │  POST /meetings/join
    │  GET  /meetings/{id}
    │  DELETE /meetings/{id}
    ▼
Azure Container Instances (one per active meeting)
    └─ ghcr.io/joinly-ai/joinly:latest --client <meeting_url>
           │
           ▼
       Video Meeting (Zoom / Google Meet / Teams)
```

---

## Prerequisites

- Azure CLI (`az`) installed and logged in (`az login`)
- Docker (for local testing)
- An Azure subscription

---

## Quick Deploy

```bash
# 1. Copy and fill in env vars
cp .env.example .env
# Edit .env with your AZURE_SUBSCRIPTION_ID and API keys

# 2. Run deploy script (takes ~5 minutes)
chmod +x infra/deploy.sh
./infra/deploy.sh

# The script prints the Meeting Manager URL when done.
```

---

## API Usage

### Join a meeting

```bash
curl -X POST https://<MANAGER_URL>/meetings/join \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://meet.google.com/abc-defg-hij",
    "name": "AI Assistant",
    "llm_provider": "anthropic",
    "llm_model": "claude-sonnet-4-6",
    "api_key": "sk-ant-..."
  }'
# → {"meeting_id": "a1b2c3d4e5f6", "status": "starting", "container_group": "joinly-meeting-a1b2c3d4e5f6"}
```

### Check meeting status

```bash
curl https://<MANAGER_URL>/meetings/a1b2c3d4e5f6
# → {"meeting_id": "...", "state": "Running", "ip": "..."}
```

### Remove agent from meeting

```bash
curl -X DELETE https://<MANAGER_URL>/meetings/a1b2c3d4e5f6
# → 204 No Content
```

### List all active meetings

```bash
curl https://<MANAGER_URL>/meetings
```

---

## Local Testing

```bash
# Set at minimum:
export AZURE_SUBSCRIPTION_ID=your-sub-id
export AZURE_RESOURCE_GROUP=joinly-rg
export AZURE_LOCATION=eastus

# Run locally (will make real ACI calls using your az login credentials)
docker compose -f docker-compose.local.yml up --build

# API available at http://localhost:8000
```

---

## Bicep Deploy (alternative to shell script)

```bash
# Build and push image first
az acr build --registry joinlyreg --image meeting-manager:latest ./meeting_manager

# Deploy with Bicep
az deployment group create \
  --resource-group joinly-rg \
  --template-file infra/main.bicep \
  --parameters managerImage="joinlyreg.azurecr.io/meeting-manager:latest"
```

---

## Cost Estimate

| Resource | Cost |
|---|---|
| Meeting Manager (Container App, always-on, 0.5 vCPU / 1 GB) | ~$15/mo |
| Each joinly agent (ACI, 2 vCPU / 4 GB) | ~$0.10–0.20/hr while running |
| Container Registry (Basic) | ~$5/mo |

Agents auto-terminate when the meeting ends (container exits). Billing stops immediately.

---

## File Structure

```
azure-deploy/
├── .env.example              # Required env vars
├── docker-compose.local.yml  # Local testing
├── meeting_manager/
│   ├── main.py               # FastAPI app
│   ├── container_manager.py  # Azure ACI management
│   ├── requirements.txt
│   └── Dockerfile
└── infra/
    ├── deploy.sh             # One-shot az CLI deploy script
    └── main.bicep            # Infrastructure as code
```
