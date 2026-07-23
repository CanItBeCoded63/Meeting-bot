#!/usr/bin/env bash
set -euo pipefail

# Prevent Git Bash on Windows from converting /subscriptions/... paths
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

# Fix Azure CLI log streaming encoding crash on Windows (cp1252 → UTF-8)
export PYTHONUTF8=1

# ---------------------------------------------------------------------------
# Environment selection: dev | prod  (first arg or ENVIRONMENT env var)
# ---------------------------------------------------------------------------
ENVIRONMENT="${1:-${ENVIRONMENT:-dev}}"

if [[ "$ENVIRONMENT" != "dev" && "$ENVIRONMENT" != "prod" ]]; then
  echo "Usage: $0 [dev|prod]" >&2
  echo "  dev  — resource group rg_digian_va_join_dev (default)" >&2
  echo "  prod — resource group rg_digian_va_join" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Load env file: .env.<ENVIRONMENT> preferred, then .env
for candidate in \
  "${SCRIPT_DIR}/../.env.${ENVIRONMENT}" \
  "${SCRIPT_DIR}/../.env"; do
  if [[ -f "$candidate" ]]; then
    echo "==> Loading config: $candidate"
    set -o allexport
    # shellcheck source=/dev/null
    source "$candidate"
    set +o allexport
    break
  fi
done

# ---------------------------------------------------------------------------
# Environment-specific defaults
# ---------------------------------------------------------------------------
if [[ "$ENVIRONMENT" == "dev" ]]; then
  : "${AZURE_RESOURCE_GROUP:=rg_digian_va_join_dev}"
  : "${AZURE_REGISTRY_NAME:=joinlyregdev}"
  : "${AZURE_CONTAINER_ENV_NAME:=joinly-env-dev}"
  : "${MANAGER_APP_NAME:=joinly-meeting-manager-dev}"
  : "${IDENTITY_NAME:=joinly-manager-identity-dev}"
else
  : "${AZURE_RESOURCE_GROUP:=rg_digian_va_join}"
  : "${AZURE_REGISTRY_NAME:=joinlyreg}"
  : "${AZURE_CONTAINER_ENV_NAME:=joinly-env}"
  : "${MANAGER_APP_NAME:=joinly-meeting-manager}"
  : "${IDENTITY_NAME:=joinly-manager-identity}"
fi

# ---------------------------------------------------------------------------
# Required + optional vars
# ---------------------------------------------------------------------------
: "${AZURE_SUBSCRIPTION_ID:?AZURE_SUBSCRIPTION_ID must be set}"
: "${MANAGER_API_KEY:?MANAGER_API_KEY must be set}"
: "${AZURE_LOCATION:=eastus2}"
: "${JOINLY_LLM_PROVIDER:=azure}"
: "${JOINLY_LLM_MODEL:=gpt-5.2-chat}"
: "${AZURE_OPENAI_ENDPOINT:=}"
: "${OPENAI_API_VERSION:=2025-01-01-preview}"
: "${AZURE_OPENAI_API_KEY:=}"
: "${JOINLY_STT:=deepgram}"
: "${JOINLY_TTS:=deepgram}"
: "${JOINLY_TTS_ARGS:=}"
: "${DEEPGRAM_API_KEY:=}"
: "${JOINLY_NAME:=Alex}"
: "${AZURE_STORAGE_ACCOUNT_NAME:=}"
: "${AZURE_STORAGE_ACCOUNT_KEY:=}"
: "${AZURE_FILE_SHARE_NAME:=}"
# NOTE: deliberately no AZURE_STORAGE_CONNECTION_STRING here. `az
# containerapp update --set-env-vars` truncates any value at its first `;`
# (confirmed live: a full connection string landed as just
# "DefaultEndpointsProtocol=https"), so a semicolon-delimited connection
# string cannot be passed this way. ACCOUNT_NAME + ACCOUNT_KEY (both
# semicolon-free) pass through cleanly, and joinly_client.memory's
# _resolve_memory_connection_string() already builds the equivalent
# connection string from them at runtime — verified working end-to-end.

MANAGER_IMAGE_NAME="meeting-manager"

echo ""
echo "==========================================="
echo " Environment  : $ENVIRONMENT"
echo " Subscription : $AZURE_SUBSCRIPTION_ID"
echo " Resource Grp : $AZURE_RESOURCE_GROUP"
echo " Location     : $AZURE_LOCATION"
echo " Registry     : $AZURE_REGISTRY_NAME"
echo " Container Env: $AZURE_CONTAINER_ENV_NAME"
echo " App Name     : $MANAGER_APP_NAME"
echo "==========================================="
echo ""

# ---------------------------------------------------------------------------
# Helper: convert to host path for Windows Git Bash
# ---------------------------------------------------------------------------
_to_host_path() {
  if command -v cygpath &>/dev/null; then cygpath -w "$1"; else echo "$1"; fi
}

echo "==> Setting subscription"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"

echo "==> Creating resource group: $AZURE_RESOURCE_GROUP"
az group create \
  --name "$AZURE_RESOURCE_GROUP" \
  --location "$AZURE_LOCATION" \
  --output none

echo "==> Creating Azure Container Registry: $AZURE_REGISTRY_NAME"
az acr create \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_REGISTRY_NAME" \
  --sku Basic \
  --admin-enabled true \
  --output none 2>/dev/null || echo "    ACR already exists."

ACR_LOGIN_SERVER=$(az acr show \
  --name "$AZURE_REGISTRY_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query loginServer -o tsv)

# ---------------------------------------------------------------------------
# Build images via ACR Tasks (runs on Azure — no local Docker needed)
# ---------------------------------------------------------------------------
echo "==> Syncing agents.yaml into meeting-manager build context"
# The manager image's ACR build context is meeting_manager/ only, not the
# repo root, so agents.yaml (single source of truth for agent name/persona/
# voice — see agents.yaml header) never reaches the image unless copied in
# here. This copy is gitignored; agents.yaml at repo root is the only
# real source, refreshed on every deploy.
cp "${REPO_ROOT}/agents.yaml" "${SCRIPT_DIR}/../meeting_manager/agents.yaml"

echo "==> Building meeting-manager image → $ACR_LOGIN_SERVER/${MANAGER_IMAGE_NAME}:latest"
MANAGER_DIR="$(_to_host_path "${SCRIPT_DIR}/../meeting_manager")"
az acr build \
  --registry "$AZURE_REGISTRY_NAME" \
  --image "${MANAGER_IMAGE_NAME}:latest" \
  --no-logs \
  "${MANAGER_DIR}"

echo "==> Building joinly agent image → $ACR_LOGIN_SERVER/joinly:latest"
REPO_DIR="$(_to_host_path "${REPO_ROOT}")"
az acr build \
  --registry "$AZURE_REGISTRY_NAME" \
  --image "joinly:latest" \
  --file "docker/Dockerfile" \
  --no-logs \
  "${REPO_DIR}"

# ---------------------------------------------------------------------------
# Container Apps Environment
# ---------------------------------------------------------------------------
echo "==> Creating Container Apps Environment: $AZURE_CONTAINER_ENV_NAME"
# `env create` auto-provisions a NEW Log Analytics workspace before it even
# checks whether the environment name already exists, so re-running it
# unconditionally (relying on the name-conflict failure to no-op) leaks one
# orphaned workspace per deploy. Check existence first instead.
if az containerapp env show \
  --name "$AZURE_CONTAINER_ENV_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" &>/dev/null; then
  echo "    Environment already exists."
else
  az containerapp env create \
    --name "$AZURE_CONTAINER_ENV_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --location "$AZURE_LOCATION" \
    --output none
fi

# ---------------------------------------------------------------------------
# Managed identity (allows meeting-manager to create/delete ACI groups)
# ---------------------------------------------------------------------------
echo "==> Creating managed identity: $IDENTITY_NAME"
az identity create \
  --name "$IDENTITY_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --output none 2>/dev/null || true

IDENTITY_CLIENT_ID=$(az identity show \
  --name "$IDENTITY_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query clientId -o tsv)

IDENTITY_RESOURCE_ID=$(az identity show \
  --name "$IDENTITY_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query id -o tsv)

SUBSCRIPTION_SCOPE="/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/$AZURE_RESOURCE_GROUP"

echo "==> Assigning Contributor role to managed identity"
az role assignment create \
  --assignee "$IDENTITY_CLIENT_ID" \
  --role "Contributor" \
  --scope "$SUBSCRIPTION_SCOPE" \
  --output none 2>/dev/null || echo "    Role assignment may already exist."

# ---------------------------------------------------------------------------
# ACR credentials (forwarded so meeting-manager can pull joinly image into ACI)
# ---------------------------------------------------------------------------
ACR_PASS=$(az acr credential show \
  --name "$AZURE_REGISTRY_NAME" \
  --query "passwords[0].value" -o tsv)

ACR_USERNAME="${AZURE_REGISTRY_NAME}"
JOINLY_IMAGE="${ACR_LOGIN_SERVER}/joinly:latest"

# ---------------------------------------------------------------------------
# Deploy / update meeting-manager Container App
# ---------------------------------------------------------------------------
echo "==> Deploying Container App: $MANAGER_APP_NAME"

ENV_VARS=(
  "AZURE_SUBSCRIPTION_ID=${AZURE_SUBSCRIPTION_ID}"
  "AZURE_RESOURCE_GROUP=${AZURE_RESOURCE_GROUP}"
  "AZURE_LOCATION=${AZURE_LOCATION}"
  "AZURE_CLIENT_ID=${IDENTITY_CLIENT_ID}"
  "MANAGER_API_KEY=${MANAGER_API_KEY}"
  "JOINLY_LLM_PROVIDER=${JOINLY_LLM_PROVIDER}"
  "JOINLY_LLM_MODEL=${JOINLY_LLM_MODEL}"
  "AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_ENDPOINT}"
  "OPENAI_API_VERSION=${OPENAI_API_VERSION}"
  "AZURE_OPENAI_API_KEY=${AZURE_OPENAI_API_KEY}"
  "JOINLY_STT=${JOINLY_STT}"
  "JOINLY_TTS=${JOINLY_TTS}"
  "JOINLY_TTS_ARGS=${JOINLY_TTS_ARGS}"
  "DEEPGRAM_API_KEY=${DEEPGRAM_API_KEY}"
  "JOINLY_NAME=${JOINLY_NAME}"
  "JOINLY_IMAGE=${JOINLY_IMAGE}"
  "ACR_LOGIN_SERVER=${ACR_LOGIN_SERVER}"
  "ACR_USERNAME=${ACR_USERNAME}"
  "ACR_PASSWORD=${ACR_PASS}"
  "AZURE_STORAGE_ACCOUNT_NAME=${AZURE_STORAGE_ACCOUNT_NAME}"
  "AZURE_STORAGE_ACCOUNT_KEY=${AZURE_STORAGE_ACCOUNT_KEY}"
  "AZURE_FILE_SHARE_NAME=${AZURE_FILE_SHARE_NAME}"
)

if az containerapp show \
  --name "$MANAGER_APP_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" &>/dev/null; then
  echo "    Container App exists — updating."
  # --image stays as the ":latest" tag string across deploys, so Container
  # Apps sees no template diff and silently keeps running the OLD revision
  # even though ACR now has a freshly built image underneath that tag.
  # --revision-suffix forces a genuinely new revision (fresh pull) every run.
  az containerapp update \
    --name "$MANAGER_APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --image "${ACR_LOGIN_SERVER}/${MANAGER_IMAGE_NAME}:latest" \
    --min-replicas 1 \
    --max-replicas 2 \
    --revision-suffix "d$(date +%Y%m%d%H%M%S)" \
    --set-env-vars "${ENV_VARS[@]}" \
    --output none
else
  az containerapp create \
    --name "$MANAGER_APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --image "${ACR_LOGIN_SERVER}/${MANAGER_IMAGE_NAME}:latest" \
    --min-replicas 1 \
    --max-replicas 2 \
    --environment "$AZURE_CONTAINER_ENV_NAME" \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-username "$ACR_USERNAME" \
    --registry-password "$ACR_PASS" \
    --user-assigned "$IDENTITY_RESOURCE_ID" \
    --target-port 8000 \
    --ingress external \
    --env-vars "${ENV_VARS[@]}" \
    --output none
fi

MANAGER_URL=$(az containerapp show \
  --name "$MANAGER_APP_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query "properties.configuration.ingress.fqdn" -o tsv)

echo ""
echo "==========================================="
echo " Deployment complete! [$ENVIRONMENT]"
echo " Meeting Manager URL : https://${MANAGER_URL}"
echo " Manager API Key     : (see .env.${ENVIRONMENT}  →  MANAGER_API_KEY)"
echo ""
echo " Health check:"
echo "   curl https://${MANAGER_URL}/health"
echo ""
echo " Join a meeting:"
echo "   curl -X POST https://${MANAGER_URL}/meetings/join \\"
echo "     -H 'Content-Type: application/json' \\"
echo "     -H 'X-API-Key: \$MANAGER_API_KEY' \\"
echo "     -d '{\"url\":\"<MEETING_URL>\",\"name\":\"Alex\"}'"
echo "==========================================="
