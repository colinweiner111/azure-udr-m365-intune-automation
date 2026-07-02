"""Azure Route Table management."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Tuple
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.network.models import Route
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import HttpResponseError

logger = logging.getLogger(__name__)

MAX_ROUTES_PER_TABLE = 400
# Cap parallel table writes to avoid ARM throttling spikes during seed runs
_MAX_PARALLEL_TABLE_WRITES = 3


class RouteTableManager:
    """Manages Azure Route Tables, optionally across multiple resource groups."""

    def __init__(
        self,
        subscription_id: str,
        resource_group: str,
        route_table_names: List[str],
        next_hop_type: str = "Internet",
        next_hop_ip: str = None,
        service_name: str = "m365"
    ):
        """
        Args:
            subscription_id: Azure subscription ID (all route tables must be in the same subscription).
            resource_group: Default resource group; used for any entry in route_table_names that
                            does not include an explicit RG prefix (``rg/tablename`` format).
            route_table_names: Table names to manage.  Each entry may be either a bare table name
                               (uses ``resource_group``) or a ``<resource-group>/<table-name>`` pair
                               to target a table in a different resource group.
                               Example: ``["rg-hub/rt-hub", "rg-spoke1/rt-spoke1", "rt-legacy"]``
            next_hop_type: ``Internet`` or ``VirtualAppliance``.
            next_hop_ip: NVA private IP; required when next_hop_type is ``VirtualAppliance``.
            service_name: Service name prefix for route naming (e.g., "m365" or "intune"). Defaults to "m365".
        """
        self.subscription_id = subscription_id
        self.default_resource_group = resource_group
        self.next_hop_type = next_hop_type
        self.next_hop_ip = next_hop_ip
        self.service_name = service_name
        # Single credential shared across all threads — avoids per-thread IMDS probing
        self._credential = DefaultAzureCredential()

        # Resolve each entry into a (resource_group, table_name) tuple.
        self.route_tables: List[tuple] = []
        for entry in route_table_names:
            entry = entry.strip()
            if "/" in entry:
                rg, tbl = entry.split("/", 1)
                rg = rg.strip()
                tbl = tbl.strip()
                if not rg or not tbl:
                    raise ValueError(
                        f"Invalid ROUTE_TABLE_NAMES entry '{entry}': expected 'resourcegroup/tablename' "
                        "or a bare table name."
                    )
                self.route_tables.append((rg, tbl))
            else:
                if not entry:
                    raise ValueError(
                        "Invalid ROUTE_TABLE_NAMES entry: empty table name is not allowed."
                    )
                self.route_tables.append((resource_group, entry))

        # Keep route_table_names as a property for backwards-compatible logging.
        self.route_table_names = [tbl for _, tbl in self.route_tables]

        if next_hop_type == "VirtualAppliance" and not next_hop_ip:
            raise ValueError("next_hop_ip required when next_hop_type is VirtualAppliance")

        logger.info(f"RouteTableManager initialized for tables: {self.route_tables}, next_hop: {next_hop_type}")

    def _make_client(self) -> NetworkManagementClient:
        """Create a NetworkManagementClient using the shared credential."""
        return NetworkManagementClient(self._credential, self.subscription_id)

    def get_current_routes(self) -> Dict[str, List[str]]:
        def _fetch(rg: str, table_name: str) -> Tuple[str, List[str]]:
            key = f"{rg}/{table_name}"
            try:
                route_table = self._make_client().route_tables.get(rg, table_name)
                routes = sorted(
                    r.address_prefix for r in (route_table.routes or []) if r.address_prefix
                )
                logger.info(f"Retrieved {len(routes)} routes from {key}")
                return key, routes
            except Exception as e:
                logger.error(f"Failed to retrieve routes from {key}: {e}")
                return key, []

        workers = max(1, min(len(self.route_tables), 10))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch, rg, tbl): (rg, tbl) for rg, tbl in self.route_tables}
            return {key: routes for f in as_completed(futures) for key, routes in [f.result()]}

    def add_routes(self, cidrs: List[str]) -> Dict[str, Any]:
        summary = {"total_cidrs": len(cidrs), "added": 0, "failed": 0, "tables": {}}
        if not cidrs:
            logger.info("No routes to add")
            return summary

        def _add_to_table(rg: str, table_name: str) -> Tuple[str, Dict[str, Any]]:
            key = f"{rg}/{table_name}"

            for attempt in range(3):
                # Reset per attempt so ETag retries don't double-count failures
                table_summary = {"added": 0, "failed": 0, "errors": [], "added_routes": [], "failed_routes": []}
                try:
                    client = self._make_client()
                    route_table = client.route_tables.get(rg, table_name)
                    etag = route_table.etag

                    existing_by_prefix = {
                        r.address_prefix: r for r in (route_table.routes or []) if r.address_prefix
                    }
                    existing_count = len(existing_by_prefix)

                    to_add = [c for c in cidrs if c not in existing_by_prefix]
                    if not to_add:
                        logger.info(f"All {len(cidrs)} routes already exist in {key}")
                        return key, table_summary

                    # Add only up to available capacity; clamp to 0 so slice never inverts
                    available = max(0, MAX_ROUTES_PER_TABLE - existing_count)
                    if len(to_add) > available:
                        skipped = to_add[available:]
                        to_add = to_add[:available]
                        msg = (
                            f"{key}: only {available} slots available, "
                            f"skipping {len(skipped)} CIDRs: {skipped}"
                        )
                        logger.warning(msg)
                        table_summary["errors"].append(msg)
                        table_summary["failed"] += len(skipped)
                        table_summary["failed_routes"] += [
                            {"cidr": c, "error": "route table at capacity"} for c in skipped
                        ]

                    if not to_add:
                        return key, table_summary

                    # Merge: only add managed routes; all existing routes preserved
                    for cidr in to_add:
                        route_name = self._generate_route_name(cidr)
                        existing_by_prefix[cidr] = Route(
                            name=route_name,
                            address_prefix=cidr,
                            next_hop_type=self.next_hop_type,
                            next_hop_ip_address=self.next_hop_ip,
                        )

                    # Full-state PUT: reuse the existing route_table object so tags and all
                    # other properties are preserved; only routes is updated
                    route_table.routes = list(existing_by_prefix.values())
                    poller = client.route_tables.begin_create_or_update(
                        rg, table_name, route_table, headers={"If-Match": etag}
                    )
                    poller.result()

                    # Post-write verification
                    updated = client.route_tables.get(rg, table_name)
                    actual = {r.address_prefix for r in (updated.routes or [])}
                    confirmed = [c for c in to_add if c in actual]
                    missing = [c for c in to_add if c not in actual]

                    table_summary["added"] = len(confirmed)
                    table_summary["added_routes"] = confirmed
                    if missing:
                        table_summary["failed"] += len(missing)
                        table_summary["failed_routes"] += [
                            {"cidr": c, "error": "not present after write"} for c in missing
                        ]
                        table_summary["errors"].append(
                            f"Post-write: {len(missing)} routes missing from {key}: {missing}"
                        )
                        logger.error(
                            f"Post-write verification failed on {key}: "
                            f"{len(missing)} of {len(to_add)} routes missing after PUT"
                        )
                    else:
                        logger.info(
                            f"Batch added and verified {len(confirmed)} routes to {key}"
                        )
                    return key, table_summary

                except HttpResponseError as e:
                    if e.status_code == 412:
                        logger.warning(
                            f"ETag conflict on {key} (attempt {attempt + 1}/3) — "
                            "table modified concurrently, retrying with fresh state"
                        )
                        continue
                    logger.error(f"HTTP error updating route table {key}: {e}")
                    table_summary["errors"].append(str(e))
                    table_summary["failed"] += len(to_add) if "to_add" in dir() else len(cidrs)
                    return key, table_summary
                except Exception as e:
                    logger.error(f"Failed to batch-update route table {key}: {e}")
                    table_summary["errors"].append(str(e))
                    table_summary["failed"] += len(to_add) if "to_add" in dir() else len(cidrs)
                    return key, table_summary

            msg = f"ETag conflict on {key} persisted after 3 attempts — skipping table"
            logger.error(msg)
            table_summary["errors"].append(msg)
            table_summary["failed"] += len(cidrs)
            return key, table_summary

        workers = min(len(self.route_tables), _MAX_PARALLEL_TABLE_WRITES)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_add_to_table, rg, tbl): (rg, tbl) for rg, tbl in self.route_tables}
            for f in as_completed(futures):
                key, table_summary = f.result()
                summary["tables"][key] = table_summary
                summary["added"] += table_summary["added"]
                summary["failed"] += table_summary["failed"]

        logger.info(f"Route addition summary: {summary}")
        return summary

    def remove_routes(self, cidrs: List[str]) -> Dict[str, Any]:
        summary = {"total_cidrs": len(cidrs), "removed": 0, "failed": 0, "tables": {}}
        if not cidrs:
            logger.info("No routes to remove")
            return summary

        cidrs_set = set(cidrs)
        service_prefix = f"{self.service_name}_"

        def _remove_from_table(rg: str, table_name: str) -> Tuple[str, Dict[str, Any]]:
            key = f"{rg}/{table_name}"

            for attempt in range(3):
                # Reset per attempt so ETag retries don't double-count failures
                table_summary = {"removed": 0, "failed": 0, "errors": [], "removed_routes": [], "failed_routes": []}
                try:
                    client = self._make_client()
                    route_table = client.route_tables.get(rg, table_name)
                    etag = route_table.etag

                    existing = route_table.routes or []

                    # Only remove routes that are (a) in cidrs_set AND (b) owned by this service
                    to_remove_prefixes = [
                        r.address_prefix for r in existing
                        if r.address_prefix in cidrs_set and r.name.startswith(service_prefix)
                    ]
                    keep = [
                        r for r in existing
                        if not (r.address_prefix in cidrs_set and r.name.startswith(service_prefix))
                    ]

                    if not to_remove_prefixes:
                        logger.info(f"No managed routes to remove from {key}")
                        return key, table_summary

                    logger.info(
                        f"Removing {len(to_remove_prefixes)} managed routes from {key}: {to_remove_prefixes}"
                    )

                    # Full-state PUT preserving tags and all other table properties
                    route_table.routes = keep
                    poller = client.route_tables.begin_create_or_update(
                        rg, table_name, route_table, headers={"If-Match": etag}
                    )
                    poller.result()

                    # Post-write verification
                    updated = client.route_tables.get(rg, table_name)
                    actual = {r.address_prefix for r in (updated.routes or [])}
                    confirmed = [c for c in to_remove_prefixes if c not in actual]
                    still_present = [c for c in to_remove_prefixes if c in actual]

                    table_summary["removed"] = len(confirmed)
                    table_summary["removed_routes"] = confirmed
                    if still_present:
                        table_summary["failed"] += len(still_present)
                        table_summary["failed_routes"] += [
                            {"cidr": c, "error": "still present after write"} for c in still_present
                        ]
                        table_summary["errors"].append(
                            f"Post-write: {len(still_present)} routes still present in {key}: {still_present}"
                        )
                        logger.error(
                            f"Post-write verification failed on {key}: "
                            f"{len(still_present)} routes still present after removal"
                        )
                    else:
                        logger.info(
                            f"Batch removed and verified {len(confirmed)} routes from {key}"
                        )
                    return key, table_summary

                except HttpResponseError as e:
                    if e.status_code == 412:
                        logger.warning(
                            f"ETag conflict on {key} (attempt {attempt + 1}/3) — retrying"
                        )
                        continue
                    logger.error(f"HTTP error updating route table {key}: {e}")
                    table_summary["errors"].append(str(e))
                    table_summary["failed"] += len(cidrs)
                    return key, table_summary
                except Exception as e:
                    logger.error(f"Failed to batch-update route table {key}: {e}")
                    table_summary["errors"].append(str(e))
                    table_summary["failed"] += len(cidrs)
                    return key, table_summary

            msg = f"ETag conflict on {key} persisted after 3 attempts — skipping table"
            logger.error(msg)
            table_summary["errors"].append(msg)
            table_summary["failed"] += len(cidrs)
            return key, table_summary

        workers = min(len(self.route_tables), _MAX_PARALLEL_TABLE_WRITES)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_remove_from_table, rg, tbl): (rg, tbl) for rg, tbl in self.route_tables}
            for f in as_completed(futures):
                key, table_summary = f.result()
                summary["tables"][key] = table_summary
                summary["removed"] += table_summary["removed"]
                summary["failed"] += table_summary["failed"]

        logger.info(f"Route removal summary: {summary}")
        return summary

    def _generate_route_name(self, cidr: str) -> str:
        safe_name = cidr.replace(".", "_").replace("/", "_")
        route_name = f"{self.service_name}_{safe_name}"
        if len(route_name) > 80:
            raise ValueError(f"Generated route name '{route_name}' exceeds Azure's 80-character limit")
        return route_name
