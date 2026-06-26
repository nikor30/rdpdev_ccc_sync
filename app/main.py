"""FastAPI application: web UI, sync trigger, and Devolutions export."""
from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .catalyst import CatalystClient
from .conflicts import compute_conflicts
from .db import Device, Site, SyncRun, SessionLocal, init_db
from .export import device_placement, site_code_for, to_csv
from .settings_store import SettingsError, get_settings, save as save_settings, view_model
from .sync import run_sync, sync_in_progress

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("catalyst_rdm")

scheduler = BackgroundScheduler(daemon=True)


def _safe_sync() -> None:
    try:
        run_sync()
    except RuntimeError:
        log.info("Background sync skipped (already running).")
    except Exception:  # pragma: no cover - defensive
        log.exception("Background sync crashed.")


def apply_schedule() -> None:
    """Add/reschedule/remove the background sync job from the current settings.

    Called at startup and whenever settings are saved, so changing the interval
    in the UI takes effect without a restart.
    """
    if not scheduler.running:
        return
    minutes = get_settings().sync_interval_minutes
    job = scheduler.get_job("catalyst_sync")
    if minutes and minutes > 0:
        if job is None:
            scheduler.add_job(
                _safe_sync,
                "interval",
                minutes=minutes,
                id="catalyst_sync",
                max_instances=1,
                coalesce=True,
            )
        else:
            scheduler.reschedule_job("catalyst_sync", trigger="interval", minutes=minutes)
        log.info("Background sync scheduled every %d min", minutes)
    elif job is not None:
        scheduler.remove_job("catalyst_sync")
        log.info("Background sync schedule cleared")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if get_settings().sync_on_startup:
        threading.Thread(target=_safe_sync, daemon=True).start()
    scheduler.start()
    apply_schedule()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Catalyst -> RDM Sync", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# --- Optional HTTP Basic auth -------------------------------------------------
_security = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_security)) -> None:
    cfg = get_settings()
    if not cfg.web_username:
        return
    ok = (
        creds is not None
        and secrets.compare_digest(creds.username, cfg.web_username)
        and secrets.compare_digest(creds.password, cfg.web_password)
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


# --- Helpers ------------------------------------------------------------------

def _safe_next(target: str, fallback: str) -> str:
    """Only allow same-site relative redirects (block //evil.com and schemes)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _all_sites(session) -> list[Site]:
    sites = session.query(Site).all()
    return sorted(sites, key=lambda s: (s.effective_region.lower(), s.effective_name.lower()))


def build_tree(session) -> dict:
    """Nested preview of the exact RDM tree:
    Root -> Region -> Country -> Site (CODE) -> Building -> Device.

    Placement comes from ``device_placement`` so this preview can never disagree
    with the CSV. Devices that can't be fully placed are listed under the flat
    review group, exactly as the export files them.
    """
    cfg = get_settings()
    nested: dict[str, dict[str, dict[str, dict[str, list]]]] = {}
    review: list = []
    for d in session.query(Device).all():
        if d.excluded:
            continue
        p = device_placement(d, cfg)
        if not p.resolved:
            review.append(d)
            continue
        (
            nested.setdefault(p.region, {})
            .setdefault(p.country, {})
            .setdefault(p.site_label, {})
            .setdefault(p.building, [])
            .append(d)
        )

    def _dev_key(x):
        return (x.effective_hostname or x.management_ip).lower()

    regions = []
    for region in sorted(nested, key=str.lower):
        countries = []
        for country in sorted(nested[region], key=str.lower):
            site_list = []
            for site_label in sorted(nested[region][country], key=str.lower):
                buildings = [
                    {"building": b, "devices": sorted(devs, key=_dev_key)}
                    for b, devs in sorted(
                        nested[region][country][site_label].items(),
                        key=lambda kv: kv[0].lower(),
                    )
                ]
                site_list.append({"site": site_label, "buildings": buildings})
            countries.append({"country": country, "sites": site_list})
        regions.append({"region": region, "countries": countries})

    review.sort(key=_dev_key)
    return {
        "root": (cfg.export_root or "").strip(),
        "regions": regions,
        "review": review,
        "review_group": cfg.export_unsorted_group,
    }


def render(request: Request, template: str, session, **ctx) -> HTMLResponse:
    report = compute_conflicts(session)
    base = {
        "nav_conflicts": report.count,
        "nav_blocking": report.blocking_count,
        "review_group": get_settings().export_unsorted_group,
    }
    base.update(ctx)
    return templates.TemplateResponse(request, template, base)


# --- Routes -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: None = Depends(require_auth)):
    session = SessionLocal()
    try:
        device_count = session.query(Device).count()
        excluded_count = (
            session.query(Device).filter(Device.excluded.is_(True)).count()
        )
        site_count = session.query(Site).count()
        regions = {s.effective_region for s in session.query(Site).all() if s.effective_region}
        last_run = (
            session.query(SyncRun).order_by(SyncRun.started_at.desc()).first()
        )
        report = compute_conflicts(session)
        return render(
            request,
            "dashboard.html",
            session,
            device_count=device_count,
            excluded_count=excluded_count,
            site_count=site_count,
            region_count=len(regions),
            last_run=last_run,
            running=sync_in_progress(),
            report=report,
        )
    finally:
        session.close()


@app.get("/tree", response_class=HTMLResponse)
def tree(request: Request, _: None = Depends(require_auth)):
    session = SessionLocal()
    try:
        return render(request, "tree.html", session, tree=build_tree(session))
    finally:
        session.close()


@app.get("/sites", response_class=HTMLResponse)
def sites_page(request: Request, _: None = Depends(require_auth)):
    session = SessionLocal()
    try:
        sites = _all_sites(session)
        counts = {
            s.id: len([d for d in s.devices if not d.excluded]) for s in sites
        }
        return render(request, "sites.html", session, sites=sites, counts=counts)
    finally:
        session.close()


@app.get("/sites/{site_id}/edit", response_class=HTMLResponse)
def site_edit(
    site_id: int,
    request: Request,
    next_url: str = Query("/sites", alias="next"),
    _: None = Depends(require_auth),
):
    session = SessionLocal()
    try:
        site = session.get(Site, site_id)
        if site is None:
            raise HTTPException(404, "Site not found")
        return render(
            request,
            "site_edit.html",
            session,
            site=site,
            next_to=_safe_next(next_url, "/sites"),
        )
    finally:
        session.close()


@app.post("/sites/{site_id}")
def site_save(
    site_id: int,
    region_override: str = Form(""),
    name_override: str = Form(""),
    country_override: str = Form(""),
    next_url: str = Form("/sites", alias="next"),
    _: None = Depends(require_auth),
):
    session = SessionLocal()
    try:
        site = session.get(Site, site_id)
        if site is None:
            raise HTTPException(404, "Site not found")
        site.region_override = region_override.strip() or None
        site.name_override = name_override.strip() or None
        site.country_override = country_override.strip() or None
        session.commit()
    finally:
        session.close()
    return RedirectResponse(_safe_next(next_url, "/sites"), status_code=303)


@app.get("/devices", response_class=HTMLResponse)
def devices_page(
    request: Request, q: str = "", _: None = Depends(require_auth)
):
    session = SessionLocal()
    try:
        query = session.query(Device)
        devices = query.all()
        if q:
            needle = q.lower()
            devices = [
                d
                for d in devices
                if needle in (d.effective_hostname or "").lower()
                or needle in (d.management_ip or "").lower()
                or needle in ((d.effective_site.effective_name if d.effective_site else "")).lower()
            ]
        devices.sort(
            key=lambda d: (d.effective_hostname or d.management_ip or "").lower()
        )
        return render(request, "devices.html", session, devices=devices, q=q)
    finally:
        session.close()


@app.get("/devices/{device_id}/edit", response_class=HTMLResponse)
def device_edit(
    device_id: int,
    request: Request,
    next_url: str = Query("/devices", alias="next"),
    _: None = Depends(require_auth),
):
    session = SessionLocal()
    try:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(404, "Device not found")
        return render(
            request,
            "device_edit.html",
            session,
            device=device,
            sites=_all_sites(session),
            derived_code=site_code_for(device, get_settings()),
            next_to=_safe_next(next_url, "/devices"),
        )
    finally:
        session.close()


@app.post("/devices/{device_id}")
def device_save(
    device_id: int,
    hostname_override: str = Form(""),
    site_override_id: str = Form(""),
    site_code_override: str = Form(""),
    excluded: str = Form(""),
    next_url: str = Form("/devices", alias="next"),
    _: None = Depends(require_auth),
):
    session = SessionLocal()
    try:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(404, "Device not found")
        device.hostname_override = hostname_override.strip() or None
        device.site_override_id = int(site_override_id) if site_override_id else None
        device.site_code_override = (site_code_override.strip().upper() or None)
        device.excluded = excluded == "on"
        session.commit()
    finally:
        session.close()
    return RedirectResponse(_safe_next(next_url, "/devices"), status_code=303)


@app.get("/conflicts", response_class=HTMLResponse)
def conflicts_page(request: Request, _: None = Depends(require_auth)):
    session = SessionLocal()
    try:
        report = compute_conflicts(session)
        return render(request, "conflicts.html", session, report=report)
    finally:
        session.close()


@app.post("/sync")
def sync_now(background: BackgroundTasks, _: None = Depends(require_auth)):
    background.add_task(_safe_sync)
    return RedirectResponse("/", status_code=303)


@app.get("/status")
def status(_: None = Depends(require_auth)):
    return {"running": sync_in_progress()}


# --- Settings -----------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, _: None = Depends(require_auth)):
    session = SessionLocal()
    try:
        return render(
            request, "settings.html", session,
            saved=bool(saved), error=None, **view_model(),
        )
    finally:
        session.close()


@app.post("/settings")
async def settings_save(request: Request, _: None = Depends(require_auth)):
    form = dict((await request.form()).items())
    session = SessionLocal()
    try:
        try:
            save_settings(form)
        except SettingsError as exc:
            return render(
                request, "settings.html", session,
                saved=False, error=exc.message, **view_model(form),
            )
    finally:
        session.close()
    apply_schedule()  # interval changes take effect immediately
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/test")
def settings_test(
    catalyst_base_url: str = Form(""),
    catalyst_username: str = Form(""),
    catalyst_password: str = Form(""),
    catalyst_verify_ssl: str = Form(""),
    catalyst_timeout: str = Form(""),
    _: None = Depends(require_auth),
):
    """Probe Catalyst Center with the values in the form (falling back to saved
    settings for blanks). Returns JSON for the inline "Test connection" button."""
    cfg = get_settings()
    base_url = catalyst_base_url.strip() or cfg.catalyst_base_url
    username = catalyst_username.strip() or cfg.catalyst_username
    password = catalyst_password or cfg.catalyst_password  # blank => use saved
    verify = catalyst_verify_ssl == "on"
    try:
        timeout = int(catalyst_timeout)
    except (TypeError, ValueError):
        timeout = cfg.catalyst_timeout

    if not base_url:
        return JSONResponse({"ok": False, "message": "Set a base URL first."})

    client = CatalystClient(base_url, username, password, verify_ssl=verify, timeout=timeout)
    try:
        count = client.probe().get("device_count")
        detail = (
            f"{count} network devices reported"
            if count is not None
            else "inventory API reachable"
        )
        return JSONResponse(
            {"ok": True, "message": f"Connected to {base_url}. Authentication OK; {detail}."}
        )
    except Exception as exc:  # surface the failure text to the UI
        return JSONResponse({"ok": False, "message": str(exc)})
    finally:
        client.close()


@app.get("/export/devolutions.csv")
def export_csv(_: None = Depends(require_auth)):
    session = SessionLocal()
    try:
        csv_data = to_csv(session)
    finally:
        session.close()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=devolutions_switches.csv"
        },
    )
