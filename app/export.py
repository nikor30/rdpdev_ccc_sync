"""Build the Devolutions RDM import (CSV) and the shared folder-placement logic.

We deliberately export NO credentials. RDM session entries inherit their SSH
username/password from a parent folder, so the operator sets one credential at
the root of the tree and every switch below inherits it.

The RDM folder tree is::

    Root (Webasto) / Region / Country / Site (CODE) / Building / Device

* Region   - derived from the Catalyst hierarchy (REGION_HIERARCHY_LEVEL).
* Country  - derived from the building's street address (or overridden).
* Site     - the building's parent area, labelled "Area (CODE)"; the 3-letter
             CODE comes from the device hostname (SITE_CODE_REGEX) or override.
* Building - the Catalyst building name.
* Device   - effective hostname (fallback management IP).

CSV columns:
    Name           -> RDM session name (effective hostname, fallback IP)
    Group          -> RDM folder path, backslash separated (the tree above)
    Host           -> management IP (SSH target)
    ConnectionType -> "SSHShell" by default
    Description    -> discovered asset info (model, IOS, serial, ...)
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .config import Settings
from .db import Device
from .settings_store import get_settings

CSV_FIELDS = ["Name", "Group", "Host", "ConnectionType", "Description"]


def _sanitize_segment(value: str) -> str:
    """RDM uses backslash as the group separator, so strip it from names."""
    return (value or "").replace("\\", "-").replace("/", "-").strip()


@dataclass(frozen=True)
class Placement:
    """Where a device lands in the RDM tree."""

    resolved: bool       # True only when region + country + code + building exist
    region: str
    country: str
    site_label: str      # "Stockholm (STO)"
    building: str
    site_code: str


def site_code_for(device: Device, cfg: Settings) -> str:
    """The device's 3-letter site code: explicit override, else regex on hostname."""
    if device.site_code_override:
        return device.site_code_override.strip().upper()
    try:
        match = re.search(cfg.site_code_regex, device.effective_hostname or "")
    except re.error:
        match = None
    if match and match.groups():
        return (match.group(1) or "").upper()
    return ""


def device_placement(device: Device, cfg: Settings | None = None) -> Placement:
    """Resolve a device's full placement. Single source of truth for tree + CSV."""
    cfg = cfg or get_settings()
    site = device.effective_site
    if site is None:
        return Placement(False, "", "", "", "", "")
    region = site.effective_region
    country = site.effective_country
    building = site.effective_name
    area = site.area_name
    code = site_code_for(device, cfg)
    if area and code:
        site_label = f"{area} ({code})"
    else:
        site_label = code or area or ""
    resolved = bool(region and country and code and building)
    return Placement(resolved, region, country, site_label, building, code)


def group_segments(device: Device, cfg: Settings | None = None) -> list[str]:
    """The RDM folder path as a list of segments (review-folder when unresolved)."""
    cfg = cfg or get_settings()
    placement = device_placement(device, cfg)
    root = (cfg.export_root or "").strip()
    if placement.resolved:
        raw = [root, placement.region, placement.country, placement.site_label,
               placement.building]
    else:
        raw = [root, cfg.export_unsorted_group]
    return [_sanitize_segment(s) for s in raw if s and s.strip()]


def group_path(device: Device, cfg: Settings | None = None) -> str:
    """The RDM ``Group`` column value (backslash-separated)."""
    return "\\".join(group_segments(device, cfg))


def build_rows(session: Session, cfg: Settings | None = None) -> list[dict]:
    cfg = cfg or get_settings()
    rows: list[dict] = []
    for d in session.query(Device).all():
        if d.excluded:
            continue
        # Nothing is silently dropped; unresolved devices land in a review folder.
        rows.append(
            {
                "Name": d.effective_hostname or d.management_ip,
                "Group": group_path(d, cfg),
                "Host": d.management_ip,
                "ConnectionType": cfg.ssh_connection_type,
                "Description": d.asset_summary,
            }
        )
    rows.sort(key=lambda r: (r["Group"], r["Name"]))
    return rows


def to_csv(session: Session, cfg: Settings | None = None) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(build_rows(session, cfg))
    return buffer.getvalue()
