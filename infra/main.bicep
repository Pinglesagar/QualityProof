targetScope = 'resourceGroup'

@description('Short lowercase prefix used for resource names.')
@minLength(3)
@maxLength(12)
param prefix string

param location string = resourceGroup().location
param imageTag string = 'bootstrap'
param deployWorkloads bool = false
param runSubmissionEnabled bool = false
param controlMinReplicas int = 0
param logRetentionDays int = 30

@secure()
@description('Required only when runSubmissionEnabled=true; supply from deployment secret input.')
param controlApiToken string = ''

@secure()
@description('Separate report bearer token; required only when runSubmissionEnabled=true.')
param controlReportToken string = ''

@allowed([
  'Manual'
  'Event'
])
param jobTriggerType string = 'Manual'

var suffix = uniqueString(subscription().subscriptionId, resourceGroup().id)
var acrName = take(replace('${prefix}${suffix}', '-', ''), 50)
var storageName = take(replace('${prefix}${suffix}ev', '-', ''), 24)
var vaultName = take('${prefix}-${suffix}-kv', 24)
var workspaceName = '${prefix}-logs'
var environmentName = '${prefix}-env'
var controlName = '${prefix}-control'
var jobName = '${prefix}-browser'
var evidenceContainerName = 'evidence'
var runQueueName = 'browser-runs'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    retentionInDays: logRetentionDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    workspaceCapping: {
      dailyQuotaGb: 1
    }
  }
  sku: {
    name: 'PerGB2018'
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: jobTriggerType == 'Event'
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource evidence 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: evidenceContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {}
}

resource runQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queueService
  name: runQueueName
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenant().tenantId
  }
}

resource apiToken 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (runSubmissionEnabled) {
  parent: vault
  name: 'control-api-token'
  properties: {
    value: controlApiToken
  }
}

resource reportToken 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (runSubmissionEnabled) {
  parent: vault
  name: 'control-report-token'
  properties: {
    value: controlReportToken
  }
}

resource queueConnection 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (jobTriggerType == 'Event') {
  parent: vault
  name: 'queue-scaler-connection'
  properties: {
    value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
  }
}

resource controlIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-control-id'
  location: location
}

resource jobIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-job-id'
  location: location
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
  }
}

var acrPullRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var blobReaderRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
)
var blobContributorRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
)
var queueContributorRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
)
var keyVaultSecretsUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)

resource controlAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, controlIdentity.id, acrPullRole)
  scope: registry
  properties: {
    principalId: controlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRole
  }
}

resource jobAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, jobIdentity.id, acrPullRole)
  scope: registry
  properties: {
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRole
  }
}

resource controlBlobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(evidence.id, controlIdentity.id, blobReaderRole)
  scope: evidence
  properties: {
    principalId: controlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: blobReaderRole
  }
}

resource jobBlobWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(evidence.id, jobIdentity.id, blobContributorRole)
  scope: evidence
  properties: {
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: blobContributorRole
  }
}

resource controlQueueWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (runSubmissionEnabled) {
  name: guid(runQueue.id, controlIdentity.id, queueContributorRole)
  scope: runQueue
  properties: {
    principalId: controlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: queueContributorRole
  }
}

resource jobQueueReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (jobTriggerType == 'Event') {
  name: guid(runQueue.id, jobIdentity.id, queueContributorRole)
  scope: runQueue
  properties: {
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: queueContributorRole
  }
}

resource controlVaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (runSubmissionEnabled) {
  name: guid(apiToken.id, controlIdentity.id, keyVaultSecretsUserRole)
  scope: apiToken
  properties: {
    principalId: controlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRole
  }
}

resource controlReportVaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (runSubmissionEnabled) {
  name: guid(reportToken.id, controlIdentity.id, keyVaultSecretsUserRole)
  scope: reportToken
  properties: {
    principalId: controlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRole
  }
}

resource jobVaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (jobTriggerType == 'Event') {
  name: guid(queueConnection.id, jobIdentity.id, keyVaultSecretsUserRole)
  scope: queueConnection
  properties: {
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRole
  }
}

resource control 'Microsoft.App/containerApps@2024-03-01' = if (deployWorkloads) {
  name: controlName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${controlIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: true
        targetPort: 8000
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: controlIdentity.id
        }
      ]
      secrets: runSubmissionEnabled ? [
        {
          name: 'report-token'
          keyVaultUrl: reportToken.properties.secretUri
          identity: controlIdentity.id
        }
        {
          name: 'api-token'
          keyVaultUrl: apiToken.properties.secretUri
          identity: controlIdentity.id
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'control'
          image: '${registry.properties.loginServer}/qualityproof-control:${imageTag}'
          env: concat([
            {
              name: 'QUALITYPROOF_REVISION'
              value: imageTag
            }
            {
              name: 'QUALITYPROOF_STORAGE_ACCOUNT_URL'
              value: storage.properties.primaryEndpoints.blob
            }
            {
              name: 'QUALITYPROOF_EVIDENCE_CONTAINER'
              value: evidence.name
            }
            {
              name: 'QUALITYPROOF_RUN_QUEUE_URL'
              value: '${storage.properties.primaryEndpoints.queue}${runQueue.name}'
            }
            {
              name: 'QUALITYPROOF_RUN_SUBMISSION_ENABLED'
              value: string(runSubmissionEnabled)
            }
            {
              name: 'QUALITYPROOF_REPORT_ACCESS_ENABLED'
              value: string(runSubmissionEnabled)
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: controlIdentity.properties.clientId
            }
          ], runSubmissionEnabled ? [
            {
              name: 'QUALITYPROOF_API_TOKEN'
              secretRef: 'api-token'
            }
            {
              name: 'QUALITYPROOF_REPORT_TOKEN'
              secretRef: 'report-token'
            }
          ] : [])
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: controlMinReplicas
        maxReplicas: 2
      }
    }
  }
}

resource browserJob 'Microsoft.App/jobs@2024-03-01' = if (deployWorkloads) {
  name: jobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${jobIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: union({
      replicaRetryLimit: 1
      replicaTimeout: 1800
      triggerType: jobTriggerType
      registries: [
        {
          server: registry.properties.loginServer
          identity: jobIdentity.id
        }
      ]
      secrets: jobTriggerType == 'Event' ? [
        {
          name: 'queue-scaler'
          keyVaultUrl: queueConnection.properties.secretUri
          identity: jobIdentity.id
        }
      ] : []
    }, jobTriggerType == 'Manual' ? {
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
    } : {
      eventTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
        scale: {
          minExecutions: 0
          maxExecutions: 1
          pollingInterval: 30
          rules: [
            {
              name: 'run-queue'
              type: 'azure-queue'
              metadata: {
                accountName: storage.name
                queueName: runQueue.name
                queueLength: '1'
              }
              auth: [
                {
                  triggerParameter: 'connection'
                  secretRef: 'queue-scaler'
                }
              ]
            }
          ]
        }
      }
    })
    template: {
      containers: [
        {
          name: 'browser'
          image: '${registry.properties.loginServer}/qualityproof-job:${imageTag}'
          env: concat([
            {
              name: 'QUALITYPROOF_REVISION'
              value: imageTag
            }
            {
              name: 'QUALITYPROOF_STORAGE_ACCOUNT_URL'
              value: storage.properties.primaryEndpoints.blob
            }
            {
              name: 'QUALITYPROOF_EVIDENCE_CONTAINER'
              value: evidence.name
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: jobIdentity.properties.clientId
            }
          ], jobTriggerType == 'Event' ? [
            {
              name: 'QUALITYPROOF_RUN_QUEUE_URL'
              value: '${storage.properties.primaryEndpoints.queue}${runQueue.name}'
            }
          ] : [])
          resources: {
            cpu: 1
            memory: '2Gi'
          }
        }
      ]
    }
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output controlAppName string = controlName
output browserJobName string = jobName
output storageAccountName string = storage.name
output keyVaultName string = vault.name
