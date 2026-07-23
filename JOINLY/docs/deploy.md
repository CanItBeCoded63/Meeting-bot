# Joinly Deployment Guide

Deploys two Azure resources:
- **Meeting Manager** — Azure Container App (always-on FastAPI, creates/destroys ACI containers)
- **Joinly agent** — Azure Container Instance (ephemeral, one per meeting)

Images are built on Azure via ACR Tasks — no local Docker required.

---

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) installed and logged in (`az login`)
- Git Bash (Windows) or bash (Mac/Linux)
- Azure subscription with Contributor access

---

## Environment Files

Create one file per environment in `azure-deploy/`:

| File | Used for |
|---|---|
| `azure-deploy/.env.dev` | Dev deployment |
| `azure-deploy/.env.prod` | Prod deployment |

Copy `.env.example` as a starting point:

```bash
cp azure-deploy/.env.example azure-deploy/.env.dev
cp azure-deploy/.env.example azure-deploy/.env.prod
```

### Required variables

```bash
MANAGER_API_KEY=<generate: python -c "import secrets; print(secrets.token_urlsafe(32))">
AZURE_SUBSCRIPTION_ID=<your-subscription-id>

# LLM
JOINLY_LLM_PROVIDER=azure
JOINLY_LLM_MODEL=gpt-5.2-chat
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com/
OPENAI_API_VERSION=2025-01-01-preview

# STT / TTS
JOINLY_STT=deepgram
JOINLY_TTS=deepgram
DEEPGRAM_API_KEY=<key>

# Browser profile (see "First-time setup" below)
AZURE_STORAGE_ACCOUNT_NAME=joinlyprofiledev
AZURE_STORAGE_ACCOUNT_KEY=<key>
AZURE_FILE_SHARE_NAME=browser-profile
```

### Dev vs prod resource names

Dev and prod use separate Azure resource groups and registries by default:

| Resource | Dev | Prod |
|---|---|---|
| Resource group | `rg_digian_va_join_dev` | `rg_digian_va_join` |
| Container registry | `joinlyregdev.azurecr.io` | `joinlyreg.azurecr.io` |
| Container Apps env | `joinly-env-dev` | `joinly-env` |
| Meeting Manager app | `joinly-meeting-manager-dev` | `joinly-meeting-manager` |
| Managed identity | `joinly-manager-identity-dev` | `joinly-manager-identity` |

Override any default by setting the corresponding variable in your `.env` file.

---

## Deploy

Run from the repo root in Git Bash:

```bash
# Deploy to dev (default)
bash azure-deploy/infra/deploy.sh dev

# Deploy to prod
bash azure-deploy/infra/deploy.sh prod
```

What the script does (idempotent — safe to re-run):

1. Creates resource group (if not exists)
2. Creates ACR (if not exists)
3. Builds `meeting-manager` image via ACR Tasks
4. Builds `joinly` agent image via ACR Tasks
5. Creates Container Apps environment (if not exists)
6. Creates managed identity + Contributor role assignment
7. Creates or updates the Meeting Manager Container App

On completion, the script prints the Meeting Manager URL and a ready-to-use curl command.

### Windows note

Run in **Git Bash**, not PowerShell or cmd. The script sets `MSYS_NO_PATHCONV=1` internally to prevent Git Bash from mangling Azure resource paths.

---

## Redeploy after code changes

Re-run the deploy script — it rebuilds both images and updates the Container App:

```bash
bash azure-deploy/infra/deploy.sh prod
```

If the Container App already exists, the script uses `az containerapp update` (not recreate), so existing env vars are preserved unless explicitly overridden.

### Force a new revision without rebuilding images

Use this when you only need to pick up an image already rebuilt in ACR:

```bash
az containerapp update \
  --name joinly-meeting-manager \
  --resource-group rg_digian_va_join \
  --revision-suffix "$(date -u +%Y%m%d%H%M%S)"
```

For dev: use `joinly-meeting-manager-dev` / `rg_digian_va_join_dev`.

---

## First-time setup: browser profile storage

The bot joins Teams as an authenticated org user. Auth tokens are stored in a Chromium profile on Azure File Share and copied to local disk at container startup (SMB doesn't support POSIX locks needed by Chromium).

### Step 1 — Provision the File Share (one-time)

```bash
export AZURE_RESOURCE_GROUP=rg_digian_va_join_dev   # or prod group
export AZURE_LOCATION=eastus2
export AZURE_STORAGE_ACCOUNT_NAME=joinlyprofiledev
bash azure-deploy/infra/provision_file_share.sh
```

Copy the printed `AZURE_STORAGE_ACCOUNT_KEY` into your `.env` file.

### Step 2 — Authenticate the browser profile

```powershell
# Start a local VNC session with Chromium
.\run_bot.ps1 vnc
```

Connect TigerVNC to `localhost:5900`, log into `teams.microsoft.com` with the org account, complete MFA.

### Step 3 — Upload the profile to Azure File Share

```powershell
.\run_bot.ps1 upload-profile
```

All future ACI containers pick up the profile on next join. Tokens last ~90 days — repeat steps 2-3 to re-authenticate.

---

## Verify deployment

```bash
# Health check
curl https://<MANAGER_URL>/health -H "X-API-Key: $MANAGER_API_KEY"
# → {"status":"ok"}

# Join a meeting
curl -X POST https://<MANAGER_URL>/meetings/join \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MANAGER_API_KEY" \
  -d '{"url": "<TEAMS_MEETING_URL>", "name": "Alex"}'

# Poll status
curl https://<MANAGER_URL>/meetings/<meeting_id> -H "X-API-Key: $MANAGER_API_KEY"
# state: Waiting → Running (bot is live, ~60-90 s)

# Remove bot
curl -X DELETE https://<MANAGER_URL>/meetings/<meeting_id> -H "X-API-Key: $MANAGER_API_KEY"
```

See `dev/join.ipynb` for an interactive version of these steps.

---

## Current endpoints

| Env | Meeting Manager URL |
|---|---|
| Dev | `https://joinly-meeting-manager-dev.thankfulriver-f345ce39.eastus2.azurecontainerapps.io` |
| Prod | `https://joinly-meeting-manager.politestone-81144374.eastus2.azurecontainerapps.io` |

API key: `MANAGER_API_KEY` from `azure-deploy/.env.dev` or `.env.prod`.

---

## Troubleshooting

### Bot terminates immediately

Check ACI logs:

```bash
az container logs \
  --resource-group rg_digian_va_join \
  --name joinly-meeting-<meeting_id>-default
```

Common causes:

| Error | Fix |
|---|---|
| `set: -: invalid option` in copy-profile.sh | CRLF line endings — fixed in Dockerfile (`sed -i 's/\r$//'`) |
| `SingletonLock: Operation not supported (95)` | SMB POSIX lock issue — copy-profile.sh should copy profile to `/tmp` |
| Bot joins as Guest / sits in lobby | Browser profile expired — re-authenticate (steps 2-3 above) |
| `unrecognized arguments: --env-vars` | Using `update` command — must use `--set-env-vars` not `--env-vars` |

### ACR build log streaming crashes on Windows

The `az acr build` log stream contains Unicode chars that crash cp1252. The deploy script uses `--no-logs` to skip streaming. Check build status:

```bash
az acr task list-runs --registry joinlyreg --top 5 --output table
az acr task show-run --registry joinlyreg --run-id <id> --query status
```
