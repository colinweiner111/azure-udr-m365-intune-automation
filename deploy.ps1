#Requires -Version 7.0
<#
.SYNOPSIS
    Full deployment script for azure-udr-m365-automation.

.DESCRIPTION
    Runs the complete deployment sequence:
      1. Create route tables (idempotent — skips any that already exist)
            2. Deploy infrastructure via Bicep
            3. Assign Network Contributor on cross-RG route table resource groups
            4. Wait for RBAC propagation
            5. Deploy the function zip

.PARAMETER ParametersFile
    Path to the Bicep parameters file. Example: infra/main.testing.parameters.json

.PARAMETER ResourceGroup
    The deployment resource group. Example: rg-udr-m365-automation-testing

.PARAMETER ZipPath
    Path to the built function zip. Defaults to 'function.zip' in the current directory.
    Build the zip first with:
        pip install --target .python_packages/lib/site-packages -r requirements.txt `
            --platform manylinux2014_x86_64 --python-version 311 --only-binary=:all:
        Compress-Archive -Path function_app.py, host.json, requirements.txt, shared, .python_packages `
            -DestinationPath function.zip

.PARAMETER SkipZipDeploy
    Skip the zip deployment step. Useful when re-running Bicep only.

.EXAMPLE
    .\deploy.ps1 -ParametersFile infra/main.testing.parameters.json -ResourceGroup rg-udr-m365-automation-testing

.EXAMPLE
    .\deploy.ps1 -ParametersFile infra/main.prod.parameters.json -ResourceGroup rg-udr-m365-automation-prod -ZipPath C:\builds\function.zip
#>
param(
    [Parameter(Mandatory)]
    [string]$ParametersFile,

    [Parameter(Mandatory)]
    [string]$ResourceGroup,

    [string]$ZipPath = "function.zip",

    [switch]$SkipZipDeploy
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step { param($msg) Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "    WARN $msg" -ForegroundColor Yellow }

$deploymentName = "udr-m365-automation-$(Get-Date -Format 'yyyyMMddHHmmss')"

# ---------------------------------------------------------------------------
# 1. Read parameters file
# ---------------------------------------------------------------------------
Write-Step "Reading parameters from $ParametersFile"

if (-not (Test-Path $ParametersFile)) { throw "Parameters file not found: $ParametersFile" }

$params         = Get-Content $ParametersFile | ConvertFrom-Json
$subscriptionId = $params.parameters.subscriptionId.value
$functionApp    = $params.parameters.functionAppName.value
$rtNames        = $params.parameters.routeTableNames.value

if ($subscriptionId -match "^<") { throw "subscriptionId is still a placeholder in $ParametersFile" }

Write-Ok "Function app : $functionApp"
Write-Ok "Subscription : $subscriptionId"
Write-Ok "Route tables : $rtNames"

# ---------------------------------------------------------------------------
# 2. Create route tables (idempotent)
# ---------------------------------------------------------------------------
Write-Step "Creating route tables (skips existing)"

$entries = $rtNames -split "," | ForEach-Object { $_.Trim() }

foreach ($entry in $entries) {
    if ($entry -match "^([^/]+)/([^/]+)$") {
        $rtRg   = $matches[1]
        $rtName = $matches[2]
    } else {
        $rtRg   = $ResourceGroup
        $rtName = $entry
    }

    $exists = az network route-table show --resource-group $rtRg --name $rtName --query "name" -o tsv 2>$null
    if ($exists) {
        Write-Ok "$rtRg/$rtName  (already exists)"
    } else {
        $rtLocation = az group show --name $rtRg --query "location" -o tsv 2>&1
        az network route-table create `
            --resource-group $rtRg `
            --name $rtName `
            --location $rtLocation `
            --disable-bgp-route-propagation false `
            --output none 2>&1
        Write-Ok "$rtRg/$rtName  (created)"
    }
}

# ---------------------------------------------------------------------------
# 3. Deploy Bicep
# ---------------------------------------------------------------------------
Write-Step "Deploying infrastructure (Bicep)"

az deployment group create `
    --name $deploymentName `
    --resource-group $ResourceGroup `
    --template-file "infra/main.bicep" `
    --parameters $ParametersFile `
    --output none 2>&1

if ($LASTEXITCODE -ne 0) { throw "Bicep deployment failed (exit code $LASTEXITCODE)" }
Write-Ok "Bicep succeeded"

# ---------------------------------------------------------------------------
# 4. Assign Network Contributor on cross-RG route table resource groups
#    Uses the function app managed identity principal ID from Bicep output.
#    Skips silently if the assignment already exists — safe to rerun.
# ---------------------------------------------------------------------------
Write-Step "Assigning Network Contributor on cross-RG resource groups"

$principalId = az deployment group show `
    --resource-group $ResourceGroup `
    --name $deploymentName `
    --query "properties.outputs.principalId.value" -o tsv 2>&1

Write-Ok "Managed identity principal ID: $principalId"

$assignedRgs = @($ResourceGroup)  # Bicep already handles the deployment RG

foreach ($entry in $entries) {
    if ($entry -match "^([^/]+)/([^/]+)$") {
        $rtRg = $matches[1]
        if ($rtRg -notin $assignedRgs) {
            $scope = "/subscriptions/$subscriptionId/resourceGroups/$rtRg"
            $existing = az role assignment list --assignee $principalId --role "Network Contributor" --scope $scope --query "[].id" -o tsv 2>&1
            if ($existing) {
                Write-Ok "Network Contributor already assigned on $rtRg (skipped)"
            } else {
                az role assignment create `
                    --assignee-object-id $principalId `
                    --assignee-principal-type ServicePrincipal `
                    --role "Network Contributor" `
                    --scope $scope `
                    --query "id" -o tsv 2>&1 | Out-Null
                Write-Ok "Network Contributor assigned on $rtRg"
            }
            $assignedRgs += $rtRg
        }
    }
}

# ---------------------------------------------------------------------------
# 5. Wait for RBAC propagation
# ---------------------------------------------------------------------------
Write-Step "Waiting 5 minutes for RBAC propagation"
Start-Sleep -Seconds 300
Write-Ok "Done"

# ---------------------------------------------------------------------------
# 6. Deploy function zip
# ---------------------------------------------------------------------------
if ($SkipZipDeploy) {
    Write-Warn "Skipping zip deploy (-SkipZipDeploy set)"
} else {
    Write-Step "Deploying function zip ($ZipPath)"
    if (-not (Test-Path $ZipPath)) { throw "Zip file not found: $ZipPath" }

    az functionapp deployment source config-zip `
        --resource-group $ResourceGroup `
        --name $functionApp `
        --src $ZipPath 2>&1

    Write-Ok "Zip deployed"
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host "`nDeployment complete." -ForegroundColor Green
Write-Host ""
Write-Host "Trigger manually:"
Write-Host "  az rest --method post --uri `"https://management.azure.com/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$functionApp/hostruntime/admin/functions/update_m365_routes/trigger?api-version=2024-04-01`""
