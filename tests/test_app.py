"""Smoke + regression tests.

Covers the structural fix (the app imports and every route renders), the
tree/export agreement, and the open-redirect guard.
"""
from fastapi.testclient import TestClient

from app.db import Device, Site, SessionLocal, init_db
from app.export import build_rows
from app.main import app, build_tree


def _seed() -> dict[str, int]:
    """Three switches: resolved, site-without-region, and unassigned."""
    init_db()
    s = SessionLocal()
    try:
        s.query(Device).delete()
        s.query(Site).delete()
        s.commit()

        resolved = Site(
            catalyst_id="b1", name="Munich-Plant",
            hierarchy="Global/EMEA/Munich-Plant", region="EMEA",
        )
        no_region = Site(
            catalyst_id="b2", name="Lab", hierarchy="Global/Lab", region="",
        )
        s.add_all([resolved, no_region])
        s.flush()

        d_ok = Device(catalyst_id="d1", hostname="sw-muc-01",
                      management_ip="10.0.0.1", site_id=resolved.id)
        d_noregion = Device(catalyst_id="d2", hostname="sw-lab-01",
                            management_ip="10.0.0.2", site_id=no_region.id)
        d_unassigned = Device(catalyst_id="d3", hostname="sw-x",
                              management_ip="10.0.0.3", site_id=None)
        s.add_all([d_ok, d_noregion, d_unassigned])
        s.commit()
        return {"resolved_site": resolved.id, "device": d_ok.id}
    finally:
        s.close()


def test_all_routes_render():
    ids = _seed()
    with TestClient(app) as client:
        for path in ["/", "/tree", "/sites", "/devices", "/devices?q=sw",
                     "/conflicts", "/status", "/export/devolutions.csv"]:
            assert client.get(path).status_code == 200, path
        assert client.get(f"/sites/{ids['resolved_site']}/edit").status_code == 200
        assert client.get(f"/devices/{ids['device']}/edit").status_code == 200
        # Unknown ids 404 cleanly.
        assert client.get("/sites/99999/edit").status_code == 404
        assert client.get("/devices/99999/edit").status_code == 404


def test_tree_matches_export():
    """The tree preview must place every device in the same RDM group as the CSV."""
    _seed()
    session = SessionLocal()
    try:
        # CSV: Name -> Group
        csv_group = {r["Name"]: r["Group"] for r in build_rows(session)}

        # Tree: Name -> "region" or "region\site"
        tree_group: dict[str, str] = {}
        for region in build_tree(session):
            for site in region["sites"]:
                path = region["region"]
                if site["name"]:
                    path = f"{path}\\{site['name']}"
                for d in site["devices"]:
                    tree_group[d.effective_hostname or d.management_ip] = path

        assert csv_group == tree_group
        # And the two unresolved switches share the flat review folder.
        assert csv_group["sw-lab-01"] == "_Review"
        assert csv_group["sw-x"] == "_Review"
        assert csv_group["sw-muc-01"] == "EMEA\\Munich-Plant"
    finally:
        session.close()


def test_open_redirect_is_blocked():
    ids = _seed()
    with TestClient(app) as client:
        evil = client.post(f"/devices/{ids['device']}",
                           data={"next": "//evil.example.com/x"},
                           follow_redirects=False)
        assert evil.status_code == 303
        assert evil.headers["location"] == "/devices"

        good = client.post(f"/devices/{ids['device']}",
                           data={"next": "/tree"},
                           follow_redirects=False)
        assert good.headers["location"] == "/tree"
