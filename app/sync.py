"""Sync engine: pull Catalyst Center inventory into the staging database.

Mapping rules
-------------
* Catalyst Center *buildings* become our Sites (they carry address + coordinates).
* Region is derived from the site hierarchy: "Global/EMEA/Munich/Plant-A" with
  REGION_HIERARCHY_LEVEL=1 yields region "EMEA".
* Country is derived from the building's street address (last component).
* The RDM tree is Root/Region/Country/Site(code)/Building/Device, where the
  "Site" is the building's parent area and the 3-letter code comes from the
  device hostname (see export.device_placement).
* Devices assigned to a floor are rolled up to that floor's parent building.
* User overrides (region_override, name_override, country_override,
  site_override_id, site_code_override, excluded, hostname_override) are NEVER
  touched by a sync.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .catalyst import CatalystClient
from .config import Settings
from .db import Device, Site, SyncRun, SessionLocal
from .settings_store import get_settings

log = logging.getLogger(__name__)

# Guards against overlapping sync runs (manual button + scheduler).
_sync_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_site(raw: dict) -> dict:
    """Extract type / hierarchy / location from a raw Catalyst site object."""
    attrs: dict = {}
    for ai in raw.get("additionalInfo", []) or []:
        if ai.get("nameSpace") == "Location":
            attrs = ai.get("attributes", {}) or {}
            break

    def _float(key: str):
        val = attrs.get(key)
        try:
            return float(val) if val not in (None, "") else None
        except (TypeError, ValueError):
            return None

    address = attrs.get("address")
    return {
        "catalyst_id": raw.get("id", ""),
        "name": raw.get("name", "") or "",
        "hierarchy": raw.get("siteNameHierarchy", "") or "",
        "type": (attrs.get("type") or "").lower(),  # area / building / floor
        "latitude": _float("latitude"),
        "longitude": _float("longitude"),
        "address": address,
        "country": _country_from_address(address),
    }


def _country_from_address(address: str | None) -> str:
    """Best-effort country from a freeform building address (its last part).

    Catalyst stores the address as one string ("Karl-Marx-Str 1, 80331 Munich,
    Germany"); the country is conventionally the final comma-separated segment.
    Wrong guesses are fixable with a country override on the site.
    """
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    return parts[-1] if parts else ""


def _derive_region(hierarchy: str, level: int, own_name: str) -> str:
    parts = [p for p in hierarchy.split("/") if p]  # ["Global", "EMEA", ...]
    if len(parts) > level:
        candidate = parts[level]
        # If the building sits so shallow that the "region" slot is the building
        # itself, treat region as unknown so it surfaces as a conflict.
        if candidate and candidate != own_name:
            return candidate
    return ""


def _member_device_id(member: dict) -> str:
    return member.get("instanceUuid") or member.get("id") or ""


def run_sync(cfg: Settings | None = None) -> SyncRun:
    """Execute a full sync. Returns the completed SyncRun record (detached)."""
    cfg = cfg or get_settings()

    if not _sync_lock.acquire(blocking=False):
        log.info("Sync already running; skipping this trigger.")
        raise RuntimeError("A sync is already in progress.")

    session = SessionLocal()
    run = SyncRun(started_at=_now(), status="running")
    session.add(run)
    session.commit()

    client: CatalystClient | None = None
    try:
        client = CatalystClient(
            cfg.catalyst_base_url,
            cfg.catalyst_username,
            cfg.catalyst_password,
            verify_ssl=cfg.catalyst_verify_ssl,
            timeout=cfg.catalyst_timeout,
        )
        client.authenticate()

        raw_sites = client.get_sites()
        raw_devices = client.get_devices()

        parsed = {s["id"]: _parse_site(s) for s in raw_sites if s.get("id")}

        # --- 1. Upsert building sites (preserve overrides) -------------------
        existing_sites = {s.catalyst_id: s for s in session.query(Site).all()}
        building_ids: set[str] = set()
        building_by_hierarchy: dict[str, Site] = {}

        for cid, p in parsed.items():
            if p["type"] != "building":
                continue
            building_ids.add(cid)
            site = existing_sites.get(cid)
            if site is None:
                site = Site(catalyst_id=cid)
                session.add(site)
                existing_sites[cid] = site
            site.name = p["name"]
            site.hierarchy = p["hierarchy"]
            site.region = _derive_region(
                p["hierarchy"], cfg.region_hierarchy_level, p["name"]
            )
            site.country = p["country"]
            site.latitude = p["latitude"]
            site.longitude = p["longitude"]
            site.address = p["address"]
            site.seen_in_last_sync = True
            site.synced_at = _now()
            building_by_hierarchy[p["hierarchy"]] = site

        session.flush()  # assign IDs

        def building_for_site(cid: str) -> Site | None:
            p = parsed.get(cid)
            if not p:
                return None
            if p["type"] == "building":
                return existing_sites.get(cid)
            if p["type"] == "floor":
                parent_hierarchy = "/".join(p["hierarchy"].split("/")[:-1])
                return building_by_hierarchy.get(parent_hierarchy)
            return None

        # --- 2. Upsert devices (switches only, preserve overrides) ----------
        existing_devices = {d.catalyst_id: d for d in session.query(Device).all()}
        families = cfg.switch_family_list
        device_by_cid: dict[str, Device] = {}
        seen_device_ids: set[str] = set()

        for d in raw_devices:
            family = d.get("family")
            if families and family not in families:
                continue
            cid = d.get("id")
            if not cid:
                continue
            seen_device_ids.add(cid)
            dev = existing_devices.get(cid)
            if dev is None:
                dev = Device(catalyst_id=cid)
                session.add(dev)
                existing_devices[cid] = dev
            dev.hostname = d.get("hostname") or ""
            dev.management_ip = d.get("managementIpAddress") or ""
            dev.family = family
            dev.role = d.get("role")
            dev.platform = d.get("platformId")
            dev.series = d.get("series")
            dev.software_version = d.get("softwareVersion")
            dev.serial_number = d.get("serialNumber")
            dev.reachability = d.get("reachabilityStatus")
            dev.seen_in_last_sync = True
            dev.synced_at = _now()
            device_by_cid[cid] = dev

        session.flush()

        # --- 3. Map devices to building sites via membership ----------------
        member_site_ids = [
            cid for cid, p in parsed.items() if p["type"] in ("building", "floor")
        ]
        device_site_map: dict[str, Site] = {}
        for site_cid in member_site_ids:
            building = building_for_site(site_cid)
            if building is None:
                continue
            try:
                members = client.get_site_members(site_cid)
            except Exception as exc:  # one bad site shouldn't kill the whole sync
                log.warning("Could not fetch members for site %s: %s", site_cid, exc)
                continue
            for m in members:
                dcid = _member_device_id(m)
                if dcid:
                    device_site_map[dcid] = building

        for cid, dev in device_by_cid.items():
            mapped = device_site_map.get(cid)
            dev.site_id = mapped.id if mapped else None

        # --- 4. Flag rows not seen this run ---------------------------------
        for site in session.query(Site).all():
            site.seen_in_last_sync = site.catalyst_id in building_ids
        for dev in session.query(Device).all():
            dev.seen_in_last_sync = dev.catalyst_id in seen_device_ids

        run.status = "success"
        run.sites_synced = len(building_ids)
        run.devices_synced = len(device_by_cid)
        run.finished_at = _now()
        session.commit()
        log.info(
            "Sync complete: %d sites, %d switches",
            run.sites_synced,
            run.devices_synced,
        )
        return run

    except Exception as exc:
        log.exception("Sync failed")
        run.status = "error"
        run.message = str(exc)[:1000]
        run.finished_at = _now()
        session.commit()
        return run
    finally:
        if client is not None:
            client.close()
        session.close()
        _sync_lock.release()


def sync_in_progress() -> bool:
    locked = _sync_lock.acquire(blocking=False)
    if locked:
        _sync_lock.release()
        return False
    return True
