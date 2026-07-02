"""Hardcoded Intune IPv4 CIDR list — used only as a last-resort fallback.

The live CIDR list is auto-fetched from the Microsoft docs repo by
shared/doc_version_checker.py and stored in blob storage. This module is
only called when both GitHub and blob storage are unavailable.
"""

import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Source: https://learn.microsoft.com/en-us/mem/intune/fundamentals/intune-endpoints
# "Consolidated Endpoint List > IP Subnets" section — IPv4 only.
# git_commit_id: 50814f6116d5921773904ec78ab4012127572e92  (docs updated 2026-06-29)
# To update: copy the "IP Subnets" block from the consolidated list, strip IPv6 lines,
# update LAST_VERIFIED and SOURCE_COMMIT, then redeploy.
LAST_VERIFIED = "2026-06-29"
SOURCE_COMMIT = "50814f6116d5921773904ec78ab4012127572e92"

_INTUNE_CIDRS: List[str] = [
    "4.145.74.224/27",
    "4.150.254.64/27",
    "4.154.145.224/27",
    "4.200.254.32/27",
    "4.207.244.0/27",
    "4.213.25.64/27",
    "4.213.86.128/25",
    "4.216.205.32/27",
    "4.237.143.128/25",
    "13.67.13.176/28",
    "13.67.15.128/27",
    "13.69.67.224/28",
    "13.69.231.128/28",
    "13.70.78.128/28",
    "13.70.79.128/27",
    "13.74.111.192/27",
    "13.77.53.176/28",
    "13.86.221.176/28",
    "13.89.174.240/28",
    "13.89.175.192/28",
    "13.107.219.0/24",
    "13.107.227.0/24",
    "13.107.228.0/23",
    "20.37.153.0/24",
    "20.37.192.128/25",
    "20.38.81.0/24",
    "20.41.1.0/24",
    "20.42.1.0/24",
    "20.42.130.0/24",
    "20.42.224.128/25",
    "20.43.129.0/24",
    "20.44.19.224/27",
    "20.91.147.72/29",
    "20.168.189.128/27",
    "20.189.172.160/27",
    "20.189.229.0/25",
    "20.191.167.0/25",
    "20.192.159.40/29",
    "20.192.174.216/29",
    "20.199.207.192/28",
    "20.204.193.10/31",
    "20.204.193.12/30",
    "20.204.194.128/31",
    "20.208.149.192/27",
    "20.208.157.128/27",
    "20.214.131.176/29",
    "40.67.121.224/27",
    "40.70.151.32/28",
    "40.71.14.96/28",
    "40.74.25.0/24",
    "40.78.245.240/28",
    "40.78.247.128/27",
    "40.79.197.64/27",
    "40.79.197.96/28",
    "40.80.180.208/28",
    "40.80.180.224/27",
    "40.80.184.128/25",
    "40.82.248.224/28",
    "40.82.249.128/25",
    "40.84.70.128/25",
    "40.119.8.128/25",
    "48.218.252.128/25",
    "52.150.137.0/25",
    "52.162.111.96/28",
    "52.168.116.128/27",
    "52.182.141.192/27",
    "52.236.189.96/27",
    "52.240.244.160/27",
    "57.151.0.192/27",
    "57.153.235.0/25",
    "57.154.140.128/25",
    "57.154.195.0/25",
    "57.155.45.128/25",
    "68.218.134.96/27",
    "74.224.214.64/27",
    "74.242.35.0/25",
    "104.46.162.96/27",
    "104.208.197.64/27",
    "150.171.97.0/24",
    "172.160.217.160/27",
    "172.201.237.160/27",
    "172.202.86.192/27",
    "172.205.63.0/25",
    "172.212.214.0/25",
    "172.215.131.0/27",
]


def get_intune_cidrs() -> Tuple[List[str], str]:
    """Return the Intune IPv4 CIDR list and the date it was last verified.

    Returns:
        Tuple of (cidrs, source_version) where source_version is the
        last-verified date in YYYY-MM-DD format, used as the run-log
        source_version field.
    """
    logger.info(
        "Intune CIDRs: %d addresses (last verified %s, commit %s)",
        len(_INTUNE_CIDRS),
        LAST_VERIFIED,
        SOURCE_COMMIT[:8],
    )
    return list(_INTUNE_CIDRS), LAST_VERIFIED
