"""Conflict / data-quality checks computed on demand from the staging DB."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .db import Device, Site
from .export import device_placement, site_code_for
from .settings_store import get_settings


@dataclass
class Issue:
    severity: str  # "error" | "warning" | "info"
    category: str
    message: str
    device_id: int | None = None
    site_id: int | None = None


@dataclass
class ConflictReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "info"]

    @property
    def count(self) -> int:
        return len(self.issues)

    @property
    def blocking_count(self) -> int:
        return len(self.errors)


def compute_conflicts(session: Session) -> ConflictReport:
    report = ConflictReport()
    cfg = get_settings()
    devices = session.query(Device).all()
    sites = session.query(Site).all()
    active = [d for d in devices if not d.excluded]

    # Devices that can't be placed into the full Region/Country/Site/Building tree.
    for d in active:
        site = d.effective_site
        label = d.effective_hostname or d.management_ip
        if site is None:
            report.issues.append(
                Issue(
                    "error",
                    "Unassigned device",
                    f"{label} has no site. Assign a site override or exclude it.",
                    device_id=d.id,
                )
            )
            continue
        if device_placement(d, cfg).resolved:
            continue
        missing = []
        if not site.effective_region:
            missing.append("region")
        if not site.effective_country:
            missing.append("country")
        if not site_code_for(d, cfg):
            missing.append("site code")
        if not site.effective_name:
            missing.append("building name")
        report.issues.append(
            Issue(
                "error",
                "Device can't be placed",
                f"{label} is missing {', '.join(missing)}; it will land in "
                f"'{cfg.export_unsorted_group}'. Fix via the site/device overrides.",
                device_id=d.id,
                site_id=site.id,
            )
        )

    # Sites missing the geo fields that build the tree.
    for s in sites:
        gaps = []
        if not s.effective_region:
            gaps.append("region")
        if not s.effective_country:
            gaps.append("country")
        if gaps:
            report.issues.append(
                Issue(
                    "warning",
                    "Site missing geo data",
                    f"Site '{s.effective_name}' ({s.hierarchy}) has no "
                    f"{' or '.join(gaps)}. Set an override.",
                    site_id=s.id,
                )
            )

    # Duplicate management IPs (would collide in RDM).
    by_ip: dict[str, list[Device]] = defaultdict(list)
    for d in active:
        if d.management_ip:
            by_ip[d.management_ip].append(d)
    for ip, group in by_ip.items():
        if len(group) > 1:
            names = ", ".join(g.effective_hostname or "?" for g in group)
            for g in group:
                report.issues.append(
                    Issue(
                        "error",
                        "Duplicate IP",
                        f"IP {ip} is shared by: {names}.",
                        device_id=g.id,
                    )
                )

    # Duplicate effective hostnames (RDM entries should be uniquely named).
    by_name: dict[str, list[Device]] = defaultdict(list)
    for d in active:
        name = d.effective_hostname
        if name:
            by_name[name].append(d)
    for name, group in by_name.items():
        if len(group) > 1:
            for g in group:
                report.issues.append(
                    Issue(
                        "warning",
                        "Duplicate hostname",
                        f"Hostname '{name}' is used by {len(group)} devices. "
                        "Set a hostname override to disambiguate.",
                        device_id=g.id,
                    )
                )

    # Stale rows (present before, missing from the latest sync).
    for d in devices:
        if not d.seen_in_last_sync and not d.excluded:
            report.issues.append(
                Issue(
                    "info",
                    "Stale device",
                    f"{d.effective_hostname or d.management_ip} was not in the last "
                    "sync. It may have been removed from Catalyst Center.",
                    device_id=d.id,
                )
            )

    return report
