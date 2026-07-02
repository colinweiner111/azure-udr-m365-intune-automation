"""Azure Function for automating Azure Route Table updates with M365 endpoints."""

import logging
import os
from datetime import datetime, timezone
import azure.functions as func
from typing import List, Dict, Any

from shared.m365_api import get_current_version, get_endpoints, extract_ipv4_cidrs
from shared.doc_version_checker import get_current_intune_cidrs
from shared.state_manager import StateManager
from shared.route_manager import RouteTableManager
from shared.run_logger import RunLogger


# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = func.FunctionApp()


@app.schedule(schedule="%M365_ROUTE_SYNC_SCHEDULE%", arg_name="mytimer", run_on_startup=False,
              use_monitor=True)
def update_m365_routes(mytimer: func.TimerRequest) -> None:
    """Timer-triggered M365 route sync."""
    _sync_routes()


@app.schedule(schedule="%INTUNE_ROUTE_SYNC_SCHEDULE%", arg_name="intunetimer", run_on_startup=False,
              use_monitor=True)
def update_intune_routes(intunetimer: func.TimerRequest) -> None:
    """Timer-triggered Intune route sync with doc version check."""
    _sync_intune_routes()


def _sync_routes() -> None:
    """Core sync logic for the M365 timer trigger."""

    config = parse_config()
    if not config:
        logger.error("Failed to parse configuration")
        return

    run_logger = RunLogger(config["storage_account_name"], service_name="m365")
    start = datetime.now(timezone.utc)

    logger.info("=" * 80)
    logger.info("Starting M365 Route Table Update Function")
    logger.info(f"Route tables: {config['route_table_names']}")

    try:
        state_mgr = StateManager(
            config["storage_account_name"],
            config["container_name"]
        )

        route_mgr = RouteTableManager(
            config["subscription_id"],
            config["resource_group"],
            config["route_table_names"],
            config["next_hop_type"],
            config["next_hop_ip"],
            service_name="m365"
        )

        logger.info(f"Fetching M365 endpoints (categories: {config['m365_categories']})...")
        endpoints = get_endpoints(categories=config["m365_categories"])
        if not endpoints:
            logger.error("Failed to fetch M365 endpoints")
            return

        new_cidrs = extract_ipv4_cidrs(endpoints)
        if not new_cidrs:
            logger.error("No IPv4 CIDRs extracted from endpoints")
            return

        current_version = get_current_version()
        if current_version is None:
            logger.warning("Could not determine M365 version, proceeding anyway")
        else:
            logger.info(f"M365 version: {current_version}")

        to_add, to_remove = state_mgr.get_diff(new_cidrs)

        current_routes_by_table = route_mgr.get_current_routes()
        drifted, missing_by_table = _find_drifted_cidrs(
            new_cidrs,
            to_remove,
            current_routes_by_table,
        )
        if drifted:
            logger.warning(
                "Detected %d drifted route(s) missing across %d route table(s)",
                len(drifted),
                len(missing_by_table),
            )
            for table_key, missing in missing_by_table.items():
                logger.warning("Table %s is missing %d route(s)", table_key, len(missing))
            to_add = sorted(set(to_add) | set(drifted))

        if not to_add and not to_remove:
            table_details = _build_table_details(
                route_mgr.route_tables,
                None,
                None,
                missing_by_table,
            )
            logger.info("No changes detected, exiting")
            run_logger.write(
                source_version=current_version,
                total_routes=len(new_cidrs),
                added=[],
                removed=[],
                drift_restored=[],
                add_succeeded=0,
                add_failed=0,
                remove_succeeded=0,
                remove_failed=0,
                result="no_change",
                table_details=table_details,
                duration_seconds=round((datetime.now(timezone.utc) - start).total_seconds()),
            )
            return

        logger.info(f"Changes detected: +{len(to_add)} -{len(to_remove)} (includes {len(drifted)} drifted)")

        add_summary = None
        remove_summary = None

        if to_remove:
            logger.info(f"Removing {len(to_remove)} routes...")
            remove_summary = route_mgr.remove_routes(to_remove)

        if to_add:
            logger.info(f"Adding {len(to_add)} routes...")
            add_summary = route_mgr.add_routes(to_add)

        if state_mgr.save_state(current_version, new_cidrs):
            logger.info("State saved successfully")
        else:
            logger.error("Failed to save state")

        log_summary(
            current_version,
            len(new_cidrs),
            to_add,
            to_remove,
            add_summary,
            remove_summary,
            drifted
        )

        table_details = _build_table_details(
            route_mgr.route_tables,
            add_summary,
            remove_summary,
            missing_by_table,
        )

        run_logger.write(
            source_version=current_version,
            total_routes=len(new_cidrs),
            added=to_add,
            removed=to_remove,
            drift_restored=drifted,
            add_succeeded=add_summary["added"] if add_summary else 0,
            add_failed=add_summary["failed"] if add_summary else 0,
            remove_succeeded=remove_summary["removed"] if remove_summary else 0,
            remove_failed=remove_summary["failed"] if remove_summary else 0,
            result="success",
            table_details=table_details,
            duration_seconds=round((datetime.now(timezone.utc) - start).total_seconds()),
        )

    except Exception as e:
        logger.exception(f"Error in main function: {e}")
        run_logger.write(
            source_version=None,
            total_routes=0,
            added=[],
            removed=[],
            drift_restored=[],
            add_succeeded=0,
            add_failed=0,
            remove_succeeded=0,
            remove_failed=0,
            result="error",
            table_details={},
            error=str(e),
            duration_seconds=round((datetime.now(timezone.utc) - start).total_seconds()),
        )
        raise


def _build_table_details(
    route_tables: List[tuple],
    add_summary: Dict[str, Any],
    remove_summary: Dict[str, Any],
    missing_by_table: Dict[str, List[str]],
) -> Dict[str, Dict[str, Any]]:
    """Build a per-table execution summary suitable for run-log JSON output."""
    table_keys = [f"{rg}/{table_name}" for rg, table_name in route_tables]
    details = {
        key: {
            "missing_before_run": len(missing_by_table.get(key, [])),
            "added": 0,
            "add_failed": 0,
            "added_routes": [],
            "add_failed_routes": [],
            "removed": 0,
            "remove_failed": 0,
            "removed_routes": [],
            "remove_failed_routes": [],
            "errors": [],
        }
        for key in table_keys
    }

    if add_summary and add_summary.get("tables"):
        for key, summary in add_summary["tables"].items():
            if key not in details:
                details[key] = {
                    "missing_before_run": len(missing_by_table.get(key, [])),
                    "added": 0,
                    "add_failed": 0,
                    "added_routes": [],
                    "add_failed_routes": [],
                    "removed": 0,
                    "remove_failed": 0,
                    "removed_routes": [],
                    "remove_failed_routes": [],
                    "errors": [],
                }
            details[key]["added"] = summary.get("added", 0)
            details[key]["add_failed"] = summary.get("failed", 0)
            details[key]["added_routes"] = summary.get("added_routes", [])
            details[key]["add_failed_routes"] = summary.get("failed_routes", [])
            details[key]["errors"].extend(summary.get("errors", []))

    if remove_summary and remove_summary.get("tables"):
        for key, summary in remove_summary["tables"].items():
            if key not in details:
                details[key] = {
                    "missing_before_run": len(missing_by_table.get(key, [])),
                    "added": 0,
                    "add_failed": 0,
                    "added_routes": [],
                    "add_failed_routes": [],
                    "removed": 0,
                    "remove_failed": 0,
                    "removed_routes": [],
                    "remove_failed_routes": [],
                    "errors": [],
                }
            details[key]["removed"] = summary.get("removed", 0)
            details[key]["remove_failed"] = summary.get("failed", 0)
            details[key]["removed_routes"] = summary.get("removed_routes", [])
            details[key]["remove_failed_routes"] = summary.get("failed_routes", [])
            details[key]["errors"].extend(summary.get("errors", []))

    for key, summary in details.items():
        summary["errors"] = sorted({err for err in summary["errors"] if err})

    return details


def _find_drifted_cidrs(
    new_cidrs: List[str],
    to_remove: List[str],
    current_routes_by_table: dict,
) -> tuple[List[str], dict]:
    """Find CIDRs missing from one or more target route tables.

    Returns:
        (drifted_cidrs, missing_by_table)
        - drifted_cidrs: sorted unique CIDRs missing from at least one table
        - missing_by_table: mapping of table_key -> sorted missing CIDRs
    """
    desired = set(new_cidrs)
    removing = set(to_remove)
    missing_by_table = {}

    for table_key, routes in current_routes_by_table.items():
        missing = sorted(desired - set(routes) - removing)
        if missing:
            missing_by_table[table_key] = missing

    drifted = sorted({cidr for missing in missing_by_table.values() for cidr in missing})
    return drifted, missing_by_table


def _sync_intune_routes() -> None:
    """Core sync logic for the Intune timer trigger."""

    config = parse_intune_config()
    if not config:
        return

    run_logger = RunLogger(config["storage_account_name"], service_name="intune")
    start = datetime.now(timezone.utc)

    logger.info("=" * 80)
    logger.info("Starting Intune Route Table Update Function")
    logger.info(f"Route tables: {config['intune_route_table_names']}")

    try:
        state_mgr = StateManager(
            config["storage_account_name"],
            config["container_name"],
            blob_name="intune/intune_route_state.json",
        )

        route_mgr = RouteTableManager(
            config["subscription_id"],
            config["resource_group"],
            config["intune_route_table_names"],
            config["next_hop_type"],
            config["next_hop_ip"],
            service_name="intune"
        )

        logger.info("Loading Intune CIDRs...")
        new_cidrs, last_verified, cidrs_updated = get_current_intune_cidrs(
            config["storage_account_name"], config["container_name"]
        )
        if cidrs_updated:
            logger.info("Intune CIDR list was refreshed from Microsoft docs this run")
        if not new_cidrs:
            logger.error("No IPv4 CIDRs available for Intune sync")
            return

        logger.info(f"Intune endpoint list: {len(new_cidrs)} CIDRs (last updated {last_verified})")

        to_add, to_remove = state_mgr.get_diff(new_cidrs)

        current_routes_by_table = route_mgr.get_current_routes()
        drifted, missing_by_table = _find_drifted_cidrs(
            new_cidrs,
            to_remove,
            current_routes_by_table,
        )
        if drifted:
            logger.warning(
                "Detected %d drifted route(s) missing across %d route table(s)",
                len(drifted),
                len(missing_by_table),
            )
            for table_key, missing in missing_by_table.items():
                logger.warning("Table %s is missing %d route(s)", table_key, len(missing))
            to_add = sorted(set(to_add) | set(drifted))

        if not to_add and not to_remove:
            table_details = _build_table_details(
                route_mgr.route_tables,
                None,
                None,
                missing_by_table,
            )
            logger.info("No changes detected, exiting")
            run_logger.write(
                source_version=last_verified,
                total_routes=len(new_cidrs),
                added=[],
                removed=[],
                drift_restored=[],
                add_succeeded=0,
                add_failed=0,
                remove_succeeded=0,
                remove_failed=0,
                result="no_change",
                table_details=table_details,
                duration_seconds=round((datetime.now(timezone.utc) - start).total_seconds()),
            )
            return

        logger.info(f"Changes detected: +{len(to_add)} -{len(to_remove)} (includes {len(drifted)} drifted)")

        add_summary = None
        remove_summary = None

        if to_remove:
            logger.info(f"Removing {len(to_remove)} routes...")
            remove_summary = route_mgr.remove_routes(to_remove)

        if to_add:
            logger.info(f"Adding {len(to_add)} routes...")
            add_summary = route_mgr.add_routes(to_add)

        if state_mgr.save_state(last_verified, new_cidrs):
            logger.info("State saved successfully")
        else:
            logger.error("Failed to save state")

        log_summary(
            last_verified,
            len(new_cidrs),
            to_add,
            to_remove,
            add_summary,
            remove_summary,
            drifted,
            service_name="Intune",
        )

        table_details = _build_table_details(
            route_mgr.route_tables,
            add_summary,
            remove_summary,
            missing_by_table,
        )

        run_logger.write(
            source_version=last_verified,
            total_routes=len(new_cidrs),
            added=to_add,
            removed=to_remove,
            drift_restored=drifted,
            add_succeeded=add_summary["added"] if add_summary else 0,
            add_failed=add_summary["failed"] if add_summary else 0,
            remove_succeeded=remove_summary["removed"] if remove_summary else 0,
            remove_failed=remove_summary["failed"] if remove_summary else 0,
            result="success",
            table_details=table_details,
            duration_seconds=round((datetime.now(timezone.utc) - start).total_seconds()),
        )

    except Exception as e:
        logger.exception(f"Error in Intune sync: {e}")
        run_logger.write(
            source_version=None,
            total_routes=0,
            added=[],
            removed=[],
            drift_restored=[],
            add_succeeded=0,
            add_failed=0,
            remove_succeeded=0,
            remove_failed=0,
            result="error",
            table_details={},
            error=str(e),
            duration_seconds=round((datetime.now(timezone.utc) - start).total_seconds()),
        )
        raise


def parse_intune_config() -> dict:
    """Parse and validate Intune environment configuration.

    Returns:
        Configuration dict or None if validation fails.
    """
    config = {
        "subscription_id": os.getenv("SUBSCRIPTION_ID"),
        "resource_group": os.getenv("RESOURCE_GROUP"),
        "storage_account_name": os.getenv("STORAGE_ACCOUNT_NAME"),
        "container_name": os.getenv("CONTAINER_NAME"),
        "next_hop_type": os.getenv("NEXT_HOP_TYPE", "Internet"),
        "next_hop_ip": os.getenv("NEXT_HOP_IP"),
        "intune_route_table_names": [
            name.strip()
            for name in os.getenv("INTUNE_ROUTE_TABLE_NAMES", "").split(",")
            if name.strip()
        ],
    }

    required = ["subscription_id", "resource_group", "storage_account_name", "container_name"]
    for key in required:
        if not config[key]:
            logger.error(f"Missing required configuration for Intune sync: {key}")
            return None

    if not config["intune_route_table_names"]:
        logger.warning(
            "INTUNE_ROUTE_TABLE_NAMES is not set — skipping Intune route sync. "
            "Set this app setting to enable Intune route management."
        )
        return None

    for entry in config["intune_route_table_names"]:
        if "/" not in entry:
            continue
        rg, table_name = entry.split("/", 1)
        if not rg.strip() or not table_name.strip():
            logger.error(
                "Invalid INTUNE_ROUTE_TABLE_NAMES entry '%s'. Expected 'resourcegroup/tablename' "
                "or a bare table name.",
                entry,
            )
            return None

    if config["next_hop_type"] not in ["Internet", "VirtualAppliance"]:
        logger.error(f"Invalid NEXT_HOP_TYPE: {config['next_hop_type']}")
        return None

    if config["next_hop_type"] == "VirtualAppliance" and not config["next_hop_ip"]:
        logger.error("NEXT_HOP_IP required when NEXT_HOP_TYPE is VirtualAppliance")
        return None

    return config


def parse_config() -> dict:
    """Parse and validate environment configuration.

    Returns:
        Configuration dict or None if validation fails.
    """
    config = {
        "subscription_id": os.getenv("SUBSCRIPTION_ID"),
        "resource_group": os.getenv("RESOURCE_GROUP"),
        "route_table_names": [
            name.strip()
            for name in os.getenv("ROUTE_TABLE_NAMES", "").split(",")
            if name.strip()
        ],
        "storage_account_name": os.getenv("STORAGE_ACCOUNT_NAME"),
        "container_name": os.getenv("CONTAINER_NAME"),
        "next_hop_type": os.getenv("NEXT_HOP_TYPE", "Internet"),
        "next_hop_ip": os.getenv("NEXT_HOP_IP"),
        "m365_categories": [
            c.strip()
            for c in os.getenv("M365_CATEGORIES", "Optimize,Allow").split(",")
            if c.strip()
        ],
    }

    required = [
        "subscription_id",
        "resource_group",
        "storage_account_name",
        "container_name"
    ]

    for key in required:
        if not config[key]:
            logger.error(f"Missing required configuration: {key}")
            return None

    if not config["route_table_names"]:
        logger.error("No route table names specified in ROUTE_TABLE_NAMES. "
                     "Use bare names (e.g. 'rt-spoke1') or 'resourcegroup/tablename' pairs "
                     "for tables in different resource groups (e.g. 'rg-spoke1/rt-spoke1,rg-spoke2/rt-spoke2').")
        return None

    for entry in config["route_table_names"]:
        if "/" not in entry:
            continue
        rg, table_name = entry.split("/", 1)
        if not rg.strip() or not table_name.strip():
            logger.error(
                "Invalid ROUTE_TABLE_NAMES entry '%s'. Expected 'resourcegroup/tablename' "
                "or a bare table name.",
                entry,
            )
            return None

    if config["next_hop_type"] not in ["Internet", "VirtualAppliance"]:
        logger.error(f"Invalid NEXT_HOP_TYPE: {config['next_hop_type']}")
        return None

    if config["next_hop_type"] == "VirtualAppliance" and not config["next_hop_ip"]:
        logger.error("NEXT_HOP_IP required when NEXT_HOP_TYPE is VirtualAppliance")
        return None

    return config


def log_summary(
    version: str,
    total_cidrs: int,
    to_add: List[str],
    to_remove: List[str],
    add_summary: dict,
    remove_summary: dict,
    drifted: List[str] = None,
    service_name: str = "M365",
) -> None:
    """Log execution summary."""
    drifted = drifted or []
    drifted_set = set(drifted)
    new_routes = [r for r in to_add if r not in drifted_set]

    logger.info("=" * 80)
    logger.info("EXECUTION SUMMARY")
    logger.info("=" * 80)
    logger.info(f"{service_name} Version: {version}")
    logger.info(f"Total CIDRs:     {total_cidrs}")
    logger.info(f"Routes Added:    {len(to_add)} ({len(drifted)} drift restores, {len(new_routes)} new from {service_name})")
    logger.info(f"Routes Removed:  {len(to_remove)} (retired from {service_name})")

    if drifted:
        logger.info(f"  Drift restored:  {', '.join(drifted)}")
    if new_routes:
        logger.info(f"  New {service_name} routes: {', '.join(new_routes)}")
    if to_remove:
        logger.info(f"  Removed routes:  {', '.join(to_remove)}")

    if add_summary:
        logger.info(f"Add result:      {add_summary['added']} succeeded, {add_summary['failed']} failed")
        if add_summary.get('failed'):
            for table, t in add_summary.get('tables', {}).items():
                for err in t.get('errors', []):
                    logger.error(f"  [{table}] {err}")

    if remove_summary:
        logger.info(f"Remove result:   {remove_summary['removed']} succeeded, {remove_summary['failed']} failed")
        if remove_summary.get('failed'):
            for table, t in remove_summary.get('tables', {}).items():
                for err in t.get('errors', []):
                    logger.error(f"  [{table}] {err}")

    logger.info("=" * 80)
