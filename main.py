"""FastAPI application: web UI, sync trigger, and Devolutions export."""
from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .conflicts import compute_conflicts
from .db import Device, Site, SyncRun, SessionLocal, init_db
from .export import to_csv
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.sync_on_startup:
        threading.Thread(target=_safe_sync, daemon=True).start()
    if settings.sync_interval_minutes > 0:
        scheduler.add_job(
            _safe_sync,
            "interval",
            minutes=settings.sync_interval_minutes,
            id="catalyst_sync",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        log.info("Scheduler started: every %d min", settings.sync_interval_minutes)
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Catalyst -> RDM Sync", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# --- Optional HTTP Basic auth -------------------------------------------------
_security = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_security)) -> None:
    if not settings.web_username:
        return
    ok = (
        creds is not None
        and secrets.compare_digest(creds.username, settings.web_username)
        and secrets.compare_digest(creds.password, settings.web_password)
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


# --- Helpers ------------------------------------------------------------------

def _all_sites(session) -> list[Site]:
    sites = session.query(Site).all()
    return sorted(sites, key=lambda s: (s.effective_region.lower(), s.effective_name.lower()))


def build_tree(session) -> list[dict]:
    """Group active devices exactly the way the export will: Region -> Site -> Device."""
    buckets: dict[str, dict[str, dict]] = {}
    for d in session.query(Device).all():
        if d.excluded:
            continue
        site = d.effective_site
        region = (site.effective_region if site else "") or settings.export_unsorted_group
        site_name = (site.effective_name if site else "") or "(no site)"
        region_bucket = buckets.setdefault(region, {})
        entry = region_bucket.setdefault(
            site_name, {"name": site_name, "site": site, "devices": []}
        )
        entry["devices"].append(d)

    result: list[dict] = []
    for region in sorted(
        buckets, key=lambda r: (r == settings.export_unsorted_group, r.lower())
    ):
        sites = []
        for site_name in sorted(buckets[region], key=lambda n: n.lower()):
            entry = buckets[region][site_name]
            entry["devices"].sort(
                key=lambda x: (x.effective_hostname or x.management_ip).lower()
            )
            sites.append(entry)
        result.append({"region": region, "sites": sites})
    return result


def render(request: Request, template: str, session, **ctx) -> HTMLResponse:
    report = compute_conflicts(session)
    base = {
        "request": request,
        "nav_conflicts": report.count,
        "nav_blocking": report.blocking_count,
        "review_group": settings.export_unsorted_group,
    }
    base.update(ctx)
    return templates.TemplateResponse(template, base)


# --- Routes -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: None = Depends(require_auth)):
    session = SessionLocal()
    try:
        device_count = session.query(Device).count()
        excluded_count = (
            session.query(Device).filter(Device.excluded == True).count()  # noqa: E712
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
def site_edit(site_id: int, request: Request, _: None = Depends(require_auth)):
    session = SessionLocal()
    try:
        site = session.get(Site, site_id)
        if site is None:
            raise HTTPException(404, "Site not found")
        return render(request, "site_edit.html", session, site=site)
    finally:
        session.close()


@app.post("/sites/{site_id}")
def site_save(
    site_id: int,
    region_override: str = Form(""),
    name_override: str = Form(""),
    next: str = Form("/sites"),
    _: None = Depends(require_auth),
):
    session = SessionLocal()
    try:
        site = session.get(Site, site_id)
        if site is None:
            raise HTTPException(404, "Site not found")
        site.region_override = region_override.strip() or None
        site.name_override = name_override.strip() or None
        session.commit()
    finally:
        session.close()
    return RedirectResponse(next or "/sites", status_code=303)


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
def device_edit(device_id: int, request: Request, _: None = Depends(require_auth)):
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
        )
    finally:
        session.close()


@app.post("/devices/{device_id}")
def device_save(
    device_id: int,
    hostname_override: str = Form(""),
    site_override_id: str = Form(""),
    excluded: str = Form(""),
    next: str = Form("/devices"),
    _: None = Depends(require_auth),
):
    session = SessionLocal()
    try:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(404, "Device not found")
        device.hostname_override = hostname_override.strip() or None
        device.site_override_id = int(site_override_id) if site_override_id else None
        device.excluded = excluded == "on"
        session.commit()
    finally:
        session.close()
    return RedirectResponse(next or "/devices", status_code=303)


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
