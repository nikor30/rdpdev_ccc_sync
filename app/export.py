"""Build the Devolutions RDM import (CSV).

We deliberately export NO credentials. RDM session entries inherit their SSH
username/password from a parent folder, so the operator sets one credential at
the top of the tree and every switch below inherits it.

Columns:
    Name           -> the RDM session name (effective hostname, fallback IP)
    Group          -> RDM folder path, backslash separated: Region\\Site
    Host           -> management IP (SSH target)
    ConnectionType -> "SSHShell" by default
"""
from __future__ import annotations

import csv
import io

from sqlalchemy.orm import Session

from .config import Settings, settings as default_settings
from .db import Device

CSV_FIELDS = ["Name", "Group", "Host", "ConnectionType"]


def _sanitize_segment(value: str) -> str:
    """RDM uses backslash as the group separator, so strip it from names."""
    return (value or "").replace("\\", "-").replace("/", "-").strip()


def device_placement(device: Device, cfg: Settings | None = None) -> tuple[str, str | None]:
    """Where a device lands in RDM, as ``(region, site_name)``.

    A device is only "resolved" when its effective site yields BOTH a region
    and a site name. Otherwise it falls into the flat review group with no site
    sub-folder (``site_name`` is ``None``).

    This is the single source of truth for placement: both the CSV export and
    the tree preview call it, so the preview can never disagree with the export.
    """
    cfg = cfg or default_settings
    site = device.effective_site
    region = site.effective_region if site else ""
    site_name = site.effective_name if site else ""
    if region and site_name:
        return region, site_name
    return cfg.export_unsorted_group, None


def group_path(device: Device, cfg: Settings | None = None) -> str:
    """The RDM ``Group`` column value (backslash-separated) for a device."""
    region, site_name = device_placement(device, cfg)
    if site_name:
        return f"{_sanitize_segment(region)}\\{_sanitize_segment(site_name)}"
    return _sanitize_segment(region)


def build_rows(session: Session, cfg: Settings | None = None) -> list[dict]:
    cfg = cfg or default_settings
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
