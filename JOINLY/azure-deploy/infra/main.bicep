@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Name of the Azure Container Registry')
param registryName string = 'joinlyreg'

@description('Name of the Container Apps Environment')
param containerEnvName string = 'joinly-env'

@description('Name of the meeting manager Container App')
param managerAppName string = 'joinly-meeting-manager'

@description('Image for the meeting manager (e.g. joinlyreg.azurecr.io/meeting-manager:latest)')
param managerImage string

@description('LLM provider (anthropic, openai, google, azure)')
param llmProvider string = 'anthropic'

@description('LLM model name')
param llmModel string = 'gpt-5.2-chat'

@description('API key callers must send as X-API-Key to use the Meeting Manager')
@secure()
param managerApiKey string

@description('Azure OpenAI endpoint URL')
param azureOpenAiEndpoint string = ''

@description('Azure OpenAI API version')
param openAiApiVersion string = '2025-01-01-preview'

@description('Azure OpenAI API key (forwarded to ACI containers)')
@secure()
param azureOpenAiApiKey string = ''

@description('STT provider (deepgram, whisper, google)')
param joinlySTT string = 'deepgram'

@description('TTS provider (deepgram, kokoro, elevenlabs)')
param joinlyTTS string = 'deepgram'

@description('Deepgram API key')
@secure()
param deepgramApiKey string = ''

@description('Agent display name shown in meetings')
param joinlyName string = 'Alex'

// ---------------------------------------------------------------------------
// Container Registry
// ---------------------------------------------------------------------------
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: true
  }
}

// ---------------------------------------------------------------------------
// Managed Identity for the meeting manager
// ---------------------------------------------------------------------------
resource managerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'joinly-manager-identity'
  location: location
}

// Give Contributor on the resource group so it can create/delete ACI
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, managerIdentity.id, contributorRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    principalId: managerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Log Analytics (required for Container Apps Environment)
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${containerEnvName}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------------------
// Container Apps Environment
// ---------------------------------------------------------------------------
resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Meeting Manager Container App
// ---------------------------------------------------------------------------
resource managerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: managerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managerIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: managerIdentity.id
        }
      ]
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'meeting-manager'
          image: managerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'AZURE_SUBSCRIPTION_ID'
              value: subscription().subscriptionId
            }
            {
              name: 'AZURE_RESOURCE_GROUP'
              value: resourceGroup().name
            }
            {
              name: 'AZURE_LOCATION'
              value: location
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: managerIdentity.properties.clientId
            }
            {
              name: 'MANAGER_API_KEY'
              value: managerApiKey
            }
            {
              name: 'JOINLY_LLM_PROVIDER'
              value: llmProvider
            }
            {
              name: 'JOINLY_LLM_MODEL'
              value: llmModel
            }
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: azureOpenAiEndpoint
            }
            {
              name: 'OPENAI_API_VERSION'
              value: openAiApiVersion
            }
            {
              name: 'AZURE_OPENAI_API_KEY'
              value: azureOpenAiApiKey
            }
            {
              name: 'JOINLY_STT'
              value: joinlySTT
            }
            {
              name: 'JOINLY_TTS'
              value: joinlyTTS
            }
            {
              name: 'DEEPGRAM_API_KEY'
              value: deepgramApiKey
            }
            {
              name: 'JOINLY_NAME'
              value: joinlyName
            }
          ]
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output managerUrl string = 'https://${managerApp.properties.configuration.ingress.fqdn}'
output registryLoginServer string = registry.properties.loginServer
output identityClientId string = managerIdentity.properties.clientId
