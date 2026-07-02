@description('Azure subscription ID; defaults to the deployment subscription.')
param subscriptionId string = subscription().subscriptionId

@description('Resource group name; defaults to the deployment resource group.')
param resourceGroupName string = resourceGroup().name

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Name of the Azure Function App.')
param functionAppName string

@description('Route tables to manage. Each entry is either a bare table name (uses the deployment resource group) or a "resourcegroup/tablename" pair for tables in a different resource group within the same subscription. Example: "rg-hub/rt-hub,rg-spoke1/rt-spoke1,rt-legacy".')
param routeTableNames string

@description('Next hop type for M365 routes.')
@allowed(['Internet', 'VirtualAppliance'])
param nextHopType string = 'Internet'

@description('Next hop IP address; required when nextHopType is VirtualAppliance.')
param nextHopIp string = ''

@description('Globally unique name for the Azure Storage Account.')
param storageAccountName string

@description('Blob container name for M365 route state.')
param containerName string = 'm365-routes'

@description('Comma-separated M365 endpoint categories to include in route tables (Optimize, Allow, Default).')
param m365Categories string = 'Optimize,Allow'

@description('NCRONTAB schedule for the timer trigger in UTC (six fields: sec min hour day month day-of-week).')
param m365RouteSyncSchedule string = '0 0 0 * * *'

@description('Route tables to manage for Intune routes. Same format as routeTableNames. Defaults to the same tables as routeTableNames.')
param intuneRouteTableNames string = ''

@description('NCRONTAB schedule for the Intune timer trigger in UTC. Offset from M365 schedule to avoid overlap.')
param intuneRouteSyncSchedule string = '0 30 0 * * *'

// Application Insights
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${functionAppName}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    RetentionInDays: 30
  }
}

// Storage Account
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource stateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

resource runLogsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'run-logs'
  properties: {
    publicAccess: 'None'
  }
}

resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'scm-releases'
  properties: {
    publicAccess: 'None'
  }
}

// Flex Consumption plan (Linux)
resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${functionAppName}-plan'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

// Function App with System-Assigned Managed Identity
resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/scm-releases'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        alwaysReady: [
          { name: 'function:update_m365_routes', instanceCount: 1 }
          { name: 'function:update_intune_routes', instanceCount: 1 }
        ]
        instanceMemoryMB: 2048
        maximumInstanceCount: 40
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      cors: {
        allowedOrigins: ['https://portal.azure.com']
        supportCredentials: false
      }
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storageAccount.name }
        { name: 'AzureWebJobsStorage__blobServiceUri', value: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}' }
        { name: 'AzureWebJobsStorage__queueServiceUri', value: 'https://${storageAccount.name}.queue.${environment().suffixes.storage}' }
        { name: 'AzureWebJobsStorage__tableServiceUri', value: 'https://${storageAccount.name}.table.${environment().suffixes.storage}' }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'SUBSCRIPTION_ID', value: subscriptionId }
        { name: 'RESOURCE_GROUP', value: resourceGroupName }
        { name: 'ROUTE_TABLE_NAMES', value: routeTableNames }
        { name: 'STORAGE_ACCOUNT_NAME', value: storageAccountName }
        { name: 'CONTAINER_NAME', value: containerName }
        { name: 'NEXT_HOP_TYPE', value: nextHopType }
        { name: 'NEXT_HOP_IP', value: nextHopIp }
        { name: 'M365_CLIENT_REQUEST_ID', value: '' }
        { name: 'M365_CATEGORIES', value: m365Categories }
        { name: 'M365_ROUTE_SYNC_SCHEDULE', value: m365RouteSyncSchedule }
        { name: 'INTUNE_ROUTE_TABLE_NAMES', value: !empty(intuneRouteTableNames) ? intuneRouteTableNames : routeTableNames }
        { name: 'INTUNE_ROUTE_SYNC_SCHEDULE', value: intuneRouteSyncSchedule }
      ]
    }
  }
}

var networkContributorRoleId = '4d97b98b-1d4f-4787-a291-c67834d212e7'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var storageQueueDataContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

// Network Contributor on the resource group (for route table management)
resource networkContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, functionApp.id, networkContributorRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Blob Data Contributor
resource storageBlobRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageBlobDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storageQueueRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageQueueDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storageTableRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageTableDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = functionApp.name
output principalId string = functionApp.identity.principalId
output storageAccountName string = storageAccount.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
