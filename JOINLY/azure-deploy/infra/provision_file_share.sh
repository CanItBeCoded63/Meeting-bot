#!/usr/bin/env bash
set -euo pipefail

# Prevent Git Bash on Windows from converting /subscriptions/... paths
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

: "${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP must be set}"
: "${AZURE_LOCATION:?AZURE_LOCATION must be set}"

STORAGE_ACCOUNT_NAME="${AZURE_STORAGE_ACCOUNT_NAME:-joinlyprofiledev}"
FILE_SHARE_NAME="${AZURE_FILE_SHARE_NAME:-browser-profile}"

echo "==> Creating storage account: $STORAGE_ACCOUNT_NAME"
az storage account create \
  --name "$STORAGE_ACCOUNT_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --location "$AZURE_LOCATION" \
  --kind StorageV2 \
  --sku Standard_LRS \
  --output none

echo "==> Creating file share: $FILE_SHARE_NAME"
az storage share create \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --name "$FILE_SHARE_NAME" \
  --quota 5 \
  --output none

echo "==> Retrieving storage account key"
AZURE_STORAGE_ACCOUNT_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query "[0].value" -o tsv)

echo ""
echo "==========================================="
echo " Storage provisioning complete"
echo " AZURE_STORAGE_ACCOUNT_NAME=$STORAGE_ACCOUNT_NAME"
echo " AZURE_STORAGE_ACCOUNT_KEY=$AZURE_STORAGE_ACCOUNT_KEY"
echo " AZURE_FILE_SHARE_NAME=$FILE_SHARE_NAME"
echo " Add these values to your .env file."
echo "==========================================="
