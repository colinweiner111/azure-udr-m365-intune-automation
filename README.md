# Azure UDR M365 Automation

Keeps Azure Route Tables synchronized with Microsoft 365 IP ranges so M365 traffic (Teams, Exchange, SharePoint) bypasses your security appliance and routes directly to the internet — automatically, daily.

**How it works:** An Azure Function fetches the [M365 endpoint API](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service) daily, diffs the results against saved state, and adds/removes UDRs in your route tables. It also detects and restores routes that were manually deleted (drift detection). All runs are logged as JSON blobs in Azure Storage for audit.

> **When NOT to use this:** If your security appliance supports FQDN/URL-based filtering (e.g., Zscaler URL policies), that is the preferred Microsoft approach. Use UDR-based routing only when IP-based routing is required.

---

## Table of Contents

- [Why these routes?](#why-these-routes)
- [Prerequisites](#prerequisites)
  - [Azure RBAC Requirements](#azure-rbac-requirements)
- [Deploy](#deploy)
  - [1. Clone the repo and set your subscription](#1-clone-the-repo-and-set-your-subscription)
  - [2. Configure parameters](#2-configure-parameters)
  - [3. Provision infrastructure](#3-provision-infrastructure)
  - [4. Deploy function code](#4-deploy-function-code)
  - [5. Verify](#5-verify)
- [Trigger manually](#trigger-manually)
- [Run logs](#run-logs)
- [Troubleshooting](#troubleshooting)
- [References](#references)
- [License](#license)

---

## Why these routes?

Microsoft classifies M365 network traffic into [three categories](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-network-connectivity-principles#new-office-365-endpoint-categories). This function defaults to `Optimize` + `Allow`:

| Category | What it covers | Route it direct? |
|----------|---------------|-----------------|
| **Optimize** | Most latency-sensitive M365 traffic (Teams media, core Exchange/SharePoint). Microsoft says these should avoid proxy inspection. | **Yes** |
| **Allow** | Additional Exchange/SharePoint/OneDrive endpoints. Lower sensitivity than Optimize, but still recommended for direct routing. | **Yes** |
| **Default** | Broad Microsoft CDN/telemetry/cloud traffic outside core M365 breakout needs. | No |

The full list of IPs per category is published at [Microsoft 365 URLs and IP address ranges](https://learn.microsoft.com/en-us/microsoft-365/enterprise/urls-and-ip-address-ranges?view=o365-worldwide).

**Why not `Default`?** It is too broad and would bypass too much inspection. `Optimize` + `Allow` is the targeted set (about 34 routes as of April 2026).

**Why UDRs?** With a [forced tunnel](https://learn.microsoft.com/en-us/azure/vpn-gateway/vpn-gateway-forced-tunneling-rm), M365 can hairpin through your NVA/VPN and add latency. UDRs with `nextHopType: Internet` provide local breakout only for those M365 CIDRs.

---

## Prerequisites

- Azure subscription with permission to create resources and assign RBAC roles
- Azure CLI installed, or use [Azure Cloud Shell](https://shell.azure.com) (no local install required)
- The deployment resource group must exist before running `deploy.ps1`
- Route tables do **not** need to be pre-created — `deploy.ps1` creates any missing ones automatically across all resource groups listed in `routeTableNames`. The resource groups themselves must exist.

### Azure RBAC Requirements

Two sets of permissions are required: one for **deploying the solution** (your user account) and one for the **Function App's managed identity** at runtime.

#### Deploying the solution (your user account)

| Role | Scope | Purpose |
|------|-------|---------|
| **Contributor** (or Owner) | Subscription or Resource Group | Create the resource group, Function App, Storage Account, and App Service Plan |
| **User Access Administrator** (or Owner) | Subscription or Resource Group | Assign RBAC roles to the Function App's managed identity during Bicep deployment |

> **Tip:** Owner at the resource group level satisfies both rows. If your account only has Contributor, a separate Owner or User Access Administrator must run the Bicep deployment (or pre-create the role assignments manually).

#### Function App managed identity (runtime)

The Bicep template automatically assigns these roles to the Function App's **system-assigned managed identity**. No manual steps are needed if you deploy with sufficient permissions above.

If you manage route tables in additional resource groups (using `resourcegroup/tablename` entries in `routeTableNames`), you must manually assign **Network Contributor** on each of those resource groups after running Bicep. The Bicep template can only assign roles within the deployment resource group.

> **Why:** The Function App uses a system-assigned managed identity. This identity is created inside the function app's resource group and has no automatic access to other resource groups. It is also tied to the function app's lifecycle — if you delete and recreate the function app (e.g. to fix a broken deployment), a new identity with a new principal ID is created and these cross-RG role assignments must be re-applied.

```bash
# Get the managed identity principal ID after Bicep deployment
PRINCIPAL_ID=$(az functionapp show \
  --resource-group <deployment-resource-group> \
  --name <function-app-name> \
  --query identity.principalId -o tsv)

# Assign Network Contributor on each additional route table resource group
for RG in rg-dept01 rg-dept02 rg-dept03; do
  az role assignment create \
    --assignee-object-id $PRINCIPAL_ID \
    --assignee-principal-type ServicePrincipal \
    --role "Network Contributor" \
    --scope "/subscriptions/<subscription-id>/resourceGroups/$RG"
done
```

| Role | Scope | Purpose |
|------|-------|---------|
| **Network Contributor** | Resource Group | Read and update Route Tables (add/remove UDR entries) |
| **Storage Blob Data Contributor** | Storage Account | Read/write route-state blobs, run-log blobs, and the deployment package |
| **Storage Queue Data Contributor** | Storage Account | Supports Functions host storage interactions in Flex Consumption |
| **Storage Table Data Contributor** | Storage Account | Supports Functions host storage interactions in Flex Consumption |

To verify role assignments after deployment:

```bash
# Get the Function App's managed identity principal ID
PRINCIPAL_ID=$(az functionapp show \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --query identity.principalId -o tsv)

# List all role assignments for that identity
az role assignment list \
  --assignee $PRINCIPAL_ID \
  --query "[].{Role:roleDefinitionName, Scope:scope}" \
  -o table
```

---

## Deploy

> **Recommended: use `deploy.ps1`** — a single PowerShell script that runs the full sequence automatically. The manual steps below are provided for reference or for environments where the script can't be used (e.g. Azure Cloud Shell without PowerShell 7).

### Using deploy.ps1 (recommended)

`deploy.ps1` handles the complete deployment in one command — including creating any missing route tables across all resource groups. Run `Get-Help .\deploy.ps1` for full usage.

```powershell
.\deploy.ps1 `
    -ParametersFile infra/main.testing.parameters.json `
    -ResourceGroup rg-udr-m365-automation-testing
```

---

### Manual steps

### 1. Clone the repo and set your subscription

```bash
git clone https://github.com/colinweiner111/azure-udr-m365-automation.git
cd azure-udr-m365-automation
az account set --subscription <subscription-id>
```

### 2. Configure parameters

Open the parameters file in the Cloud Shell editor:

```bash
code infra/main.parameters.json
```

For separate environments, use dedicated parameter files:

```bash
code infra/main.testing.parameters.json
code infra/main.prod.parameters.json
code infra/main.customer.parameters.template.json
```

Use different values per environment for at least:

- `functionAppName`
- `storageAccountName`
- `routeTableNames`
- deployment resource group

This keeps test and production state/log data isolated.

For customer deployments, copy `infra/main.customer.parameters.template.json` to a customer-specific parameters file and fill in the customer subscription, region, function app name, storage account name, and all route tables.

| Parameter | Description | Required |
|-----------|-------------|----------|
| `subscriptionId` | Azure subscription ID | Yes |
| `functionAppName` | Function App name (becomes `<name>.azurewebsites.net`) — must be globally unique | Yes |
| `storageAccountName` | Storage account name (3–24 chars, lowercase + numbers, globally unique) | Yes |
| `routeTableNames` | Route tables to manage. Each entry is a bare table name (uses the deployment resource group) **or** a `resourcegroup/tablename` pair for tables in different resource groups within the same subscription. Examples: `rt-spoke1,rt-spoke2` (same RG) or `rg-spoke1/rt-spoke1,rg-spoke2/rt-spoke2` (different RGs) | Yes |
| `location` | Azure region (e.g., `centralus`) | Yes |
| `nextHopType` | `Internet` or `VirtualAppliance` | Default: `Internet` |
| `nextHopIp` | NVA private IP — required only when `nextHopType` is `VirtualAppliance` | Conditional |
| `containerName` | Blob container for route state | Default: `m365-routes` |
| `m365Categories` | M365 categories to include: `Optimize`, `Allow`, `Default` | Default: `Optimize,Allow` |

> **Route table limit:** Azure caps each route table at ~400 routes. `Optimize,Allow` produces ~34 routes as of April 2026 — well within limits.

### 3. Provision infrastructure

```bash
az group create --name <resource-group> --location <location>

# Pick one parameter file per deployment
# infra/main.testing.parameters.json or infra/main.prod.parameters.json
az deployment group create \
  --resource-group <resource-group> \
  --template-file infra/main.bicep \
  --parameters <parameters-file>
```

> Takes under 20 minutes. Azure Cloud Shell disconnects after 20 minutes of inactivity — the deployment completes well within that window.

Bicep creates: Storage Account, Blob containers, Flex Consumption Function App (Python 3.11, FC1) with System-Assigned Managed Identity, Application Insights, and all required RBAC role assignments (Network Contributor on the RG, Storage Blob/Queue/Table Data Contributor on the storage account).

For customer deployments with route tables in multiple resource groups, treat Network Contributor on every resource group listed in `routeTableNames` as mandatory for first-run success.

### 3a. Assign Network Contributor on additional resource groups

Skip this step if all your route tables are in the deployment resource group.

If `routeTableNames` includes tables in other resource groups (`resourcegroup/tablename` format), assign Network Contributor on each of those RGs now. The managed identity does not have access to them automatically.

```bash
PRINCIPAL_ID=$(az functionapp show \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --query identity.principalId -o tsv)

for RG in rg-dept01 rg-dept02 rg-dept03; do
  az role assignment create \
    --assignee-object-id $PRINCIPAL_ID \
    --assignee-principal-type ServicePrincipal \
    --role "Network Contributor" \
    --scope "/subscriptions/<subscription-id>/resourceGroups/$RG"
done
```

> **Note:** If using `deploy.ps1`, this step and the route table creation are handled automatically. The manual steps here are for reference only.

> **Important:** RBAC assignment propagation is not immediate. Wait at least 5 minutes before the first manual trigger or validation run.

### 4. Deploy function code

Still in the same Cloud Shell session:

```bash
# Build zip (Linux-compiled packages required)
pip install --target .python_packages/lib/site-packages -r requirements.txt --platform manylinux2014_x86_64 --only-binary=:all:
zip -r function.zip . -x "*.git*" "local.settings.json" "__pycache__/*" "*.pyc" "tests.py" "infra/*"

# Deploy zip to Flex Consumption function app
az functionapp deployment source config-zip \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --src function.zip
```

> Takes under a minute.

### 5. Verify

```bash
az functionapp show --resource-group <resource-group> --name <function-app-name> --query state

az webapp log tail --resource-group <resource-group> --name <function-app-name>
```

For a customer handoff, run one manual trigger after deployment and inspect the newest blob in the `run-logs` container. Do not treat the deployment as complete until every table in the run-log shows an empty `errors` array.

The function runs automatically at 1:00 PM UTC daily (`0 0 13 * * *`), which is 6:00 AM PDT.

---

## Trigger manually

**From the CLI:**

```bash
az rest --method post \
  --uri "https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Web/sites/<function-app-name>/hostruntime/admin/functions/update_m365_routes/trigger?api-version=2024-04-01"
```

**From the Azure Portal:** Open the Function App → Functions → `update_m365_routes` → Code + Test → Test/Run.

---

## Run logs

Each run writes a JSON blob to the `run-logs` container in your storage account, organized by date (`YYYY/MM/DD/HH-MM-SS.json`). Browse them in Azure Storage Explorer or the portal.

Current log shape keeps summary counters at the top level and route-level details per table under `tables`.

```json
{
  "timestamp": "2026-04-23T01:03:20Z",
  "result": "success",
  "m365_version": "2026033100",
  "total_routes": 34,
  "add_succeeded": 1,
  "add_failed": 0,
  "remove_succeeded": 0,
  "remove_failed": 0,
  "tables": {
    "rg-udr-m365-county-dept01-testing/rt-m365-county-dept01-test": {
      "missing_before_run": 1,
      "added": 1,
      "add_failed": 0,
      "added_routes": ["52.96.0.0/14"],
      "add_failed_routes": [],
      "removed": 0,
      "remove_failed": 0,
      "removed_routes": [],
      "remove_failed_routes": [],
      "errors": []
    }
  }
}
```

Quick checks after each run:

- `result` is `success` or `no_change`
- top-level counters (`add_succeeded`, `remove_succeeded`, etc.) look expected
- each managed table appears under `tables`
- route-level details are visible per table (`added_routes`, `removed_routes`, failure arrays)

---

## Troubleshooting

**Authentication error / routes not updating**
- Verify RBAC: `az role assignment list --assignee <principal-id> --query "[].{Role:roleDefinitionName, Scope:scope}" -o table`
- The function identity needs Network Contributor on the RG and Storage Blob Data Contributor on the storage account (Bicep assigns these automatically).

**Managed identity deleted or role assignments missing**
- If the Function App is re-created or its managed identity is deleted (e.g. by an Azure Policy cleanup job), the role assignments are orphaned and must be re-applied. Re-run the Bicep deployment — it will create a new identity and re-assign roles within the deployment resource group. Then re-run the cross-RG assignments from Step 3a for any additional resource groups. Orphaned assignments show up as `Unknown` principals in IAM and can be safely deleted.

**Function shows ServiceUnavailable after deploy**
- The zip hasn't been deployed yet.
- Run `az functionapp deployment source config-zip` as shown in Step 4.

**Routes were deleted and not restored**
- Drift detection runs on every execution. Trigger manually (see above) to restore immediately rather than waiting for the next daily run.

**Azure Policy wiping route tables**
- Policies with a `Modify` effect can overwrite route table properties. Exempt the resource group from those policies. The function will restore any removed routes on the next run (daily or manual trigger).

**Will the function remove my custom/non-M365 routes?**
- No. The function only manages routes whose address prefixes appear in the M365 endpoint API. Any route you add manually (e.g. `0.0.0.0/0` pointing to a firewall) is invisible to the function and will never be modified or removed. The one exception: if a CIDR you added manually happens to match an M365-published prefix that Microsoft later drops, the function would remove it as part of normal M365 cleanup.

---

## References

- [Microsoft 365 IP Web Service](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service)
- [M365 Endpoint Categories (Optimize / Allow / Default)](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-network-connectivity-principles#identify-microsoft-365-network-traffic)
- [M365 Endpoints API — worldwide endpoints](https://endpoints.office.com/endpoints/worldwide?clientrequestid=b10c5ed1-bad1-445f-b386-b919946339a7) *(live JSON the function pulls)*
- [M365 Endpoints API — current version](https://endpoints.office.com/version/worldwide?clientrequestid=b10c5ed1-bad1-445f-b386-b919946339a7) *(version number used in run logs)*
- [Azure Route Tables](https://learn.microsoft.com/en-us/azure/virtual-network/manage-route-table)
- [Azure Functions Python Developer Guide](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)

## License

MIT
