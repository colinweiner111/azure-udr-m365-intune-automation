"""Auto-refreshes the Intune CIDR list from the Microsoft docs repo when the doc changes."""

import ipaddress
import json
import logging
import re
import urllib.request
from datetime import date
from typing import List, Optional, Tuple

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)

_GITHUB_COMMITS_URL = (
    "https://api.github.com/repos/MicrosoftDocs/memdocs/commits"
    "?path=intune/fundamentals/endpoints.md&per_page=1"
)
_GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/MicrosoftDocs/memdocs/main"
    "/intune/fundamentals/endpoints.md"
)
_DOC_URL = "https://learn.microsoft.com/en-us/mem/intune/fundamentals/intune-endpoints"
_CIDRS_BLOB_NAME = "doc-version/intune_cidrs.json"

_IPV4_CIDR_RE = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3}/(?:[1-2]?\d|3[0-2]))\b')
# Matches "IP Subnets" as a standalone line, optionally preceded by markdown heading hashes
_IP_SUBNETS_HEADING_RE = re.compile(
    r'^(?:(#{1,6})\s+)?IP\s+Subnets\s*$',
    re.IGNORECASE | re.MULTILINE,
)
_CODE_FENCE_RE = re.compile(r'```')
_MIN_CIDRS = 10


def get_current_intune_cidrs(
    storage_account_name: str,
    container_name: str,
) -> Tuple[List[str], str, bool]:
    """Return the current Intune IPv4 CIDR list, auto-refreshing from GitHub when the doc changes.

    On each call:
      - Fetches the latest commit SHA for endpoints.md from MicrosoftDocs/memdocs.
      - If the SHA changed (or no stored list exists), fetches the raw file and parses CIDRs.
      - Stores the parsed list to blob so the next run picks it up without re-fetching.
      - Falls back to the stored blob list, then to the hardcoded list in intune_api.py,
        if GitHub is unreachable or parsing fails.

    Returns:
        (cidrs, source_date, was_updated)
        cidrs        — List of IPv4 CIDR strings
        source_date  — "YYYY-MM-DD" used as run-log source_version
        was_updated  — True if the list was refreshed from GitHub this run
    """
    from shared.intune_api import get_intune_cidrs as _hardcoded_fallback

    latest_sha, commit_url = _fetch_latest_commit()
    stored = _read_stored(storage_account_name, container_name)

    stored_sha = stored.get("sha") if stored else None
    stored_cidrs = stored.get("cidrs") if stored else None
    stored_date = stored.get("last_updated") if stored else None

    needs_refresh = latest_sha and (stored_sha != latest_sha or not stored_cidrs)

    if needs_refresh:
        new_cidrs = _fetch_and_parse_cidrs()
        if new_cidrs:
            today = date.today().isoformat()
            _write_stored(storage_account_name, container_name, latest_sha, new_cidrs, today, commit_url)
            if stored_sha and stored_sha != latest_sha:
                logger.info(
                    "Intune CIDR list auto-updated: %d CIDRs (commit %s -> %s). "
                    "Review %s to verify the changes look correct.",
                    len(new_cidrs), stored_sha[:8], latest_sha[:8], commit_url or _DOC_URL,
                )
            else:
                logger.info(
                    "Intune CIDR list initialised from GitHub: %d CIDRs (commit %s)",
                    len(new_cidrs), latest_sha[:8],
                )
            return new_cidrs, today, True
        else:
            logger.warning(
                "Could not parse CIDRs from GitHub doc (commit %s) — "
                "falling back to stored/hardcoded list. "
                "Stored SHA is NOT advanced so the next run will retry.",
                latest_sha[:8] if latest_sha else "unknown",
            )

    if stored_cidrs:
        logger.info(
            "Intune CIDR list loaded from blob: %d CIDRs (last updated %s)",
            len(stored_cidrs), stored_date,
        )
        return stored_cidrs, stored_date, False

    # Total failure (GitHub down, blob empty, first run with no connectivity)
    cidrs, fallback_date = _hardcoded_fallback()
    logger.warning(
        "Using hardcoded Intune CIDR fallback: %d CIDRs (last verified %s)",
        len(cidrs), fallback_date,
    )
    return cidrs, fallback_date, False


def _fetch_and_parse_cidrs() -> Optional[List[str]]:
    """Fetch endpoints.md from GitHub and extract IPv4 CIDRs from the 'IP Subnets' section."""
    try:
        req = urllib.request.Request(
            _GITHUB_RAW_URL,
            headers={"User-Agent": "azure-udr-intune-automation"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        cidrs = _parse_ip_subnets_section(content)
        if cidrs is None:
            return None
        if len(cidrs) < _MIN_CIDRS:
            logger.warning(
                "Parsed only %d CIDRs from the 'IP Subnets' section (expected at least %d) — "
                "possible parse failure or doc restructure.",
                len(cidrs), _MIN_CIDRS,
            )
            return None
        return cidrs
    except Exception as e:
        logger.warning("Failed to fetch or parse endpoints.md from GitHub: %s", e)
        return None


def _parse_ip_subnets_section(content: str) -> Optional[List[str]]:
    """Extract IPv4 CIDRs from the 'IP Subnets' section of endpoints.md only.

    The Microsoft docs format is a bare 'IP Subnets' line followed by a fenced
    code block containing one CIDR per line. Extraction is constrained to that
    code block so CIDRs anywhere else in the page are never included.

    Assumption: the first code fence (```) after the 'IP Subnets' heading is the
    CIDR block. If Microsoft reorders content within that section this could pick
    up the wrong block. A parse failure or CIDR count below _MIN_CIDRS will catch
    most such cases and trigger the fallback path.

    Returns None if the section or code block cannot be found (signals a doc restructure).
    """
    m = _IP_SUBNETS_HEADING_RE.search(content)
    if not m:
        logger.warning(
            "'IP Subnets' section not found in endpoints.md — "
            "page may have been restructured."
        )
        return None

    after_heading = content[m.end():]

    # CIDRs live in a fenced code block (```) immediately after the heading
    fence_open = _CODE_FENCE_RE.search(after_heading)
    if fence_open:
        after_fence = after_heading[fence_open.end():]
        fence_close = _CODE_FENCE_RE.search(after_fence)
        section_text = after_fence[:fence_close.start()] if fence_close else after_fence
    else:
        # No code fence — fall back to stopping at the next markdown heading
        stop = re.search(r'^#{1,6}\s', after_heading, re.MULTILINE)
        section_text = after_heading[:stop.start()] if stop else after_heading

    raw = list(set(_IPV4_CIDR_RE.findall(section_text)))
    cidrs = []
    for c in sorted(raw):
        try:
            net = ipaddress.ip_network(c, strict=False)
            if net.prefixlen < 8:
                logger.warning("Rejecting suspiciously broad CIDR from doc: %s", c)
                continue
            cidrs.append(c)
        except ValueError:
            logger.warning("Rejecting invalid CIDR from doc: %s", c)
    rejected = len(raw) - len(cidrs)
    if rejected:
        logger.warning("Rejected %d invalid/overly-broad CIDRs from 'IP Subnets' section", rejected)
    logger.info("Parsed %d IPv4 CIDRs from 'IP Subnets' section", len(cidrs))
    return cidrs


def _fetch_latest_commit() -> Tuple[Optional[str], Optional[str]]:
    """Return (sha, html_url) of the latest commit for endpoints.md."""
    try:
        req = urllib.request.Request(
            _GITHUB_COMMITS_URL,
            headers={
                "User-Agent": "azure-udr-intune-automation",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if not data:
            logger.warning("GitHub API returned empty commit list")
            return None, None
        return data[0].get("sha"), data[0].get("html_url")
    except Exception as e:
        logger.warning("Failed to query GitHub API: %s", e)
        return None, None


def _get_blob_client(storage_account_name: str, container_name: str, blob_name: str):
    url = f"https://{storage_account_name}.blob.core.windows.net"
    service = BlobServiceClient(account_url=url, credential=DefaultAzureCredential())
    return service.get_blob_client(container=container_name, blob=blob_name)


def _read_stored(storage_account_name: str, container_name: str) -> Optional[dict]:
    try:
        blob = _get_blob_client(storage_account_name, container_name, _CIDRS_BLOB_NAME)
        return json.loads(blob.download_blob().readall())
    except Exception:
        return None


def _write_stored(
    storage_account_name: str,
    container_name: str,
    sha: str,
    cidrs: List[str],
    last_updated: str,
    commit_url: Optional[str],
) -> None:
    try:
        blob = _get_blob_client(storage_account_name, container_name, _CIDRS_BLOB_NAME)
        payload = json.dumps({
            "sha": sha,
            "cidrs": cidrs,
            "cidr_count": len(cidrs),
            "last_updated": last_updated,
            "commit_url": commit_url or "",
        }, indent=2).encode()
        blob.upload_blob(payload, overwrite=True)
    except Exception as e:
        logger.warning("Failed to write CIDR store to blob: %s", e)
