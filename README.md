# Azure UDR M365 & Intune Automation

Keeps Azure Route Tables synchronized with Microsoft 365 IP ranges so M365 traffic (Teams, Exchange, SharePoint) bypasses your security appliance and routes directly to the internet — automatically, daily.

**How it works:** An Azure Function fetches the [M365 endpoint API](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service) daily, diffs the results against saved state, and adds/removes UDRs in your route tables. It also detects and restores routes that were manually deleted (drift detection). All runs are logged as JSON blobs in Azure Storage for audit.

The same Function App includes a second timer (`update_intune_routes`) for Intune traffic. The function checks the [MicrosoftDocs/memdocs](https://github.com/MicrosoftDocs/memdocs) GitHub repo on every run and updates the stored CIDR list automatically when Microsoft changes the endpoint file — no redeploy required. `shared/intune_api.py` serves as a hardcoded last-resort fallback if GitHub is unreachable.

> **Intune FQDN limitation:** UDRs route by IP only. Intune endpoints such as `*.manage.microsoft.com` and `*.dm.microsoft.com` are FQDN-only — not covered by UDRs. Configure Zscaler bypass (passthrough, not inspection) for those FQDNs separately. UDRs for IPs + Zscaler bypass for FQDNs = complete Intune traffic breakout.

> **When NOT to use this:** If your security appliance supports FQDN/URL-based filtering (e.g., Zscaler URL policies), that is generally the cleaner approach where supported. Use UDR-based routing only when IP-based routing is required.

---

## Table of Contents

- [Why these routes?](#why-these-routes)
  - [M365](#m365)
  - [Intune](#intune)
- [Prerequisites](#prerequisites)
- [Deploy](#deploy)
  - [Quick deploy with deploy.ps1](#quick-deploy-with-deployps1-recommended)
  - [Key parameters](#key-parameters)
  - [Manual deployment and upgrades](#manual-deployment-and-upgrades)
- [Schedule configuration](#schedule-configuration)
- [Trigger manually](#trigger-manually)
- [Run logs](#run-logs)
- [Troubleshooting](#troubleshooting)
- [Additional docs](#additional-docs)
- [References](#references)
- [License](#license)

---

## Why these routes?

### M365

Microsoft classifies M365 traffic into [three categories](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-network-connectivity-principles#new-office-365-endpoint-categories). This function defaults to `Optimize` + `Allow`:

| Category | What it covers | Route direct? |
|----------|---------------|--------------|
| **Optimize** | Latency-sensitive M365 traffic (Teams media, core Exchange/SharePoint). Microsoft recommends avoiding proxy inspection. | **Yes** |
| **Allow** | Additional Exchange/SharePoint/OneDrive endpoints. Lower sensitivity, still recommended for direct routing. | **Yes** |
| **Default** | Broad Microsoft CDN/telemetry/cloud traffic. | No |

**Why not `Default`?** Too broad — would bypass too much inspection. `Optimize` + `Allow` is ~34 routes.

**Why UDRs?** With a [forced tunnel](https://learn.microsoft.com/en-us/azure/vpn-gateway/vpn-gateway-forced-tunneling-rm), M365 can hairpin through your NVA and add latency. UDRs with `nextHopType: Internet` provide local breakout for only those CIDRs.

### Intune

The Intune IP list comes from the **"IP Subnets"** block in the [Intune consolidated endpoint list](https://learn.microsoft.com/en-us/mem/intune/fundamentals/intune-endpoints) (IPv4 only — ~85 CIDRs). It covers Intune device management services, Windows Update for Business, Microsoft Defender for Endpoint, and related Microsoft cloud services.

**Why not Azure Service Tags?** In testing, the `MicrosoftIntune` Service Tag returned only a small subset of the consolidated Intune CIDRs, so this project uses the published Intune endpoint list instead.

**How the list stays current:** Each daily sync compares the commit SHA of `endpoints.md` in the MicrosoftDocs GitHub repo against the last-known SHA stored in blob. When a change is detected, the function re-parses the file and writes the updated list to blob immediately — no redeploy needed.

---

## Prerequisites

- Azure subscription with Contributor + User Access Administrator (or Owner) on the deployment resource group. For cross-RG route tables, you also need permission to assign Network Contributor on each additional route-table resource group.
- PowerShell 7+ for `deploy.ps1`, or use [Azure Cloud Shell](https://shell.azure.com)
- The deployment resource group must exist before running `deploy.ps1`
- Route tables do **not** need to be pre-created — `deploy.ps1` creates any missing ones automatically

The Function App uses a **system-assigned managed identity** that needs:

| Role | Scope |
|------|-------|
| Network Contributor | Each resource group containing a managed route table |
| Storage Blob Data Contributor | Storage Account |
| Storage Queue Data Contributor | Storage Account |
| Storage Table Data Contributor | Storage Account |

Bicep assigns these automatically within the deployment resource group. For cross-RG route tables, see [docs/deployment.md](docs/deployment.md#3a-assign-network-contributor-on-additional-resource-groups).

---

## Deploy

### Quick deploy with deploy.ps1 (recommended)

```powershell
.\deploy.ps1 `
    -ParametersFile infra/main.testing.parameters.json `
    -ResourceGroup <resource-group> `
    -SubscriptionId <subscription-id>
```

`deploy.ps1` runs the full sequence: creates missing route tables, runs Bicep, assigns cross-RG RBAC roles, waits for propagation, and deploys the function zip. Run `Get-Help .\deploy.ps1` for full usage.

**Key parameters:**

| Parameter | Description | Required |
|-----------|-------------|----------|
| `subscriptionId` | Azure subscription ID | Yes |
| `functionAppName` | Globally unique Function App name | Yes |
| `storageAccountName` | 3–24 chars, lowercase + numbers, globally unique | Yes |
| `routeTableNames` | Comma-separated route tables. Bare name uses the deployment RG; `rg/tablename` targets another RG | Yes |
| `location` | Azure region (e.g., `centralus`) | Yes |
| `nextHopType` | `Internet` or `VirtualAppliance` | Default: `Internet` |
| `nextHopIp` | NVA private IP — required when `nextHopType` is `VirtualAppliance` | Conditional |
| `m365Categories` | M365 categories to sync | Default: `Optimize,Allow` |
| `intuneRouteTableNames` | Route tables for Intune routes | Default: same as `routeTableNames` |

### Manual deployment and upgrades

- [docs/deployment.md](docs/deployment.md) — Full manual steps and RBAC deep-dive
- [docs/upgrading.md](docs/upgrading.md) — Upgrading from M365-only to M365 + Intune

---

## Upgrading an existing M365-only deployment

If your Function App already runs `update_m365_routes`, adding Intune sync is an in-place upgrade — no new infrastructure required. The existing M365 function, state, and run logs are unaffected.

### Before you start

Verify the managed identity has **Network Contributor** on every resource group that contains an Intune target route table. If your Intune route tables are in different resource groups than your M365 ones, add the role assignment now:

```bash
PRINCIPAL_ID=$(az functionapp show \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --query identity.principalId -o tsv)

az role assignment create \
  --assignee-object-id $PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Network Contributor" \
  --scope "/subscriptions/<subscription-id>/resourceGroups/<intune-route-table-rg>"
```

> Wait at least 5 minutes after assigning the role before triggering the first Intune seed run.

### Safe upgrade sequence

**1. Deploy updated code**

```bash
pip install --target .python_packages/lib/site-packages -r requirements.txt --platform manylinux2014_x86_64 --only-binary=:all:
zip -r function.zip . -x "*.git*" "local.settings.json" "__pycache__/*" "*.pyc" "tests.py" "infra/*"

az functionapp deployment source config-zip \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --src function.zip
```

**2. Verify both functions appear**

In the Azure Portal, open the Function App and confirm both `update_m365_routes` and `update_intune_routes` are listed under Functions. The M365 function will continue running on its existing schedule without interruption.

**3. Add Intune app settings**

In the portal: Function App → Settings → Environment variables → add:

| Setting | Value |
|---------|-------|
| `INTUNE_ROUTE_TABLE_NAMES` | Comma-separated list of route tables to manage for Intune. Same `rg/tablename` format as `ROUTE_TABLE_NAMES`. Can be the same tables or different ones. |
| `INTUNE_ROUTE_SYNC_SCHEDULE` | NCRONTAB schedule (6 fields). Recommend `0 30 0 * * *` (12:30 AM UTC) to avoid overlap with M365. |

Save and restart the Function App after adding the settings.

**4. Run the first Intune seed manually**

Trigger `update_intune_routes` from the portal or CLI (see [Trigger manually](#trigger-manually)). The first run seeds all Intune routes (~85 per table). With the batch PUT implementation this completes in under 10 seconds for 6 tables.

**5. Confirm success**

Check the newest `intune/` blob in the `run-logs` container and verify:

- `result` is `success`
- `add_succeeded` equals `total_routes × number_of_tables`
- `add_failed` is `0`
- every table under `tables` has an empty `errors` array

The deployment is complete once all tables confirm clean.

---

## Schedule configuration

Schedules are controlled by app settings:

| App setting | Default | Function |
|---|---|---|
| `M365_ROUTE_SYNC_SCHEDULE` | `0 0 0 * * *` (midnight UTC) | `update_m365_routes` |
| `INTUNE_ROUTE_SYNC_SCHEDULE` | `0 30 0 * * *` (12:30 AM UTC) | `update_intune_routes` |

The Intune schedule is offset 30 minutes to avoid overlapping ARM API calls with M365.

To change without redeploying: Function App → Settings → Environment variables → update → Save → Restart.

![Function App environment variables example](image/envvars.png)

---

## Trigger manually

**CLI:**

```bash
# M365 routes
az rest --method post \
  --uri "https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Web/sites/<function-app-name>/hostruntime/admin/functions/update_m365_routes/trigger?api-version=2024-04-01"

# Intune routes
az rest --method post \
  --uri "https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Web/sites/<function-app-name>/hostruntime/admin/functions/update_intune_routes/trigger?api-version=2024-04-01"
```

**Portal:** Function App → select function name → Code + Test → Test/Run.

![Select function name in Azure Portal](image/functionname.png)

> **First Intune run:** The initial seed adds ~85 routes per table. The first run can take a few minutes depending on route table count; subsequent no-change runs usually complete in seconds. Check the `intune/` prefix in the `run-logs` container for `result: "success"` to confirm the seed completed.

---

## Run logs

Each run writes a JSON blob to `run-logs/<service>/YYYY/MM/DD/HH-MM-SS.json`. The example below is abbreviated — full schema including `added`, `removed`, and `drift_restored` fields is in [docs/operations.md](docs/operations.md#run-log-schema).

![run-logs container showing intune and m365 folders](image/runlogs.png)

```json
{
  "timestamp": "2026-04-23T01:03:20Z",
  "duration_seconds": 8,
  "result": "success",
  "source_version": "2026033100",
  "total_routes": 34,
  "add_succeeded": 1,
  "add_failed": 0,
  "remove_succeeded": 0,
  "remove_failed": 0,
  "tables": {
    "rg-spoke1/rt-spoke1": {
      "added": 1,
      "add_failed": 0,
      "added_routes": ["52.96.0.0/14"],
      "errors": []
    }
  }
}
```

Quick checks: `result` is `success` or `no_change`; counters look expected; no table-level errors. Full schema in [docs/operations.md](docs/operations.md#run-log-schema).

---

## Troubleshooting

**Authentication error / routes not updating** — Verify RBAC: `az role assignment list --assignee <principal-id> --query "[].{Role:roleDefinitionName, Scope:scope}" -o table`. The identity needs Network Contributor on the route table RG and Storage Blob Data Contributor on the storage account.

**`RoleAssignmentUpdateNotPermitted` during Bicep** — Stale orphaned assignment from an older managed identity. Delete it at the RG and storage account scope, then redeploy.

**Function shows ServiceUnavailable after deploy** — Zip hasn't been deployed yet. Run `az functionapp deployment source config-zip` (see [docs/deployment.md](docs/deployment.md)).

**Routes not restored after deletion** — Drift detection runs on every execution. Trigger manually to restore immediately.

**Will the function remove my custom routes?** — No. Only routes matching M365 or Intune published CIDRs are managed. Routes with prefixes outside those lists are never touched.

Full troubleshooting list in [docs/operations.md](docs/operations.md#troubleshooting).

---

## Additional docs

- [Full deployment guide](docs/deployment.md)
- [Upgrade from M365-only deployment](docs/upgrading.md)
- [Operations and troubleshooting](docs/operations.md)

---

## References

- [Microsoft 365 IP Web Service](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service)
- [M365 Endpoint Categories](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-network-connectivity-principles#identify-microsoft-365-network-traffic)
- [Intune network endpoints](https://learn.microsoft.com/en-us/mem/intune/fundamentals/intune-endpoints)
- [Azure Route Tables](https://learn.microsoft.com/en-us/azure/virtual-network/manage-route-table)
- [Azure Functions Python Developer Guide](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)

## License

MIT
