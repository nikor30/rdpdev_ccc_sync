"""Smoke + regression tests for routes, the 6-level placement, and asset info."""
from fastapi.testclient import TestClient

from app import settings_store
from app.db import AppSetting, Device, Site, SessionLocal, init_db
from app.export import build_rows, device_placement, group_path, site_code_for
from app.main import app, build_tree
from app.settings_store import get_settings

EXPECTED_GROUP = "Webasto\\EMEA\\Sweden\\Stockholm (STO)\\Building-1"


def _seed() -> dict[str, int]:
    """One fully-placeable switch, one sibling, and one that lands in review."""
    init_db()
    s = SessionLocal()
    try:
        s.query(Device).delete()
        s.query(Site).delete()
        s.query(AppSetting).delete()  # tests run on env defaults, no overrides
        s.commit()
        settings_store.invalidate()

        bld = Site(
            catalyst_id="b1", name="Building-1",
            hierarchy="Global/EMEA/Sweden/Stockholm/Building-1",
            region="EMEA", country="Sweden",
            address="Kungsgatan 1, 111 43 Stockholm, Sweden",
        )
        no_country = Site(
            catalyst_id="b2", name="Building-9",
            hierarchy="Global/EMEA/Munich/Building-9", region="EMEA", country="",
        )
        s.add_all([bld, no_country])
        s.flush()

        d1 = Device(catalyst_id="d1", hostname="SSTO010CIS", management_ip="10.0.0.1",
                    site_id=bld.id, platform="C9300-48", software_version="17.9.4",
                    serial_number="FOC123", role="ACCESS")
        d2 = Device(catalyst_id="d2", hostname="SSTO011CIS", management_ip="10.0.0.2",
                    site_id=bld.id)
        # missing country on its site -> not placeable -> review
        d3 = Device(catalyst_id="d3", hostname="SMUC001CIS", management_ip="10.0.0.3",
                    site_id=no_country.id)
        s.add_all([d1, d2, d3])
        s.commit()
        return {"device": d1.id, "site": bld.id}
    finally:
        s.close()


def test_all_routes_render():
    ids = _seed()
    with TestClient(app) as client:
        for path in ["/", "/tree", "/sites", "/devices", "/devices?q=STO",
                     "/conflicts", "/settings", "/export/devolutions.csv"]:
            assert client.get(path).status_code == 200, path
        assert client.get(f"/sites/{ids['site']}/edit").status_code == 200
        assert client.get(f"/devices/{ids['device']}/edit").status_code == 200


def test_site_code_and_placement():
    _seed()
    session = SessionLocal()
    try:
        cfg = get_settings()
        d1 = session.query(Device).filter_by(catalyst_id="d1").one()
        assert site_code_for(d1, cfg) == "STO"
        p = device_placement(d1, cfg)
        assert p.resolved
        assert p.region == "EMEA" and p.country == "Sweden"
        assert p.site_label == "Stockholm (STO)" and p.building == "Building-1"
        assert group_path(d1, cfg) == EXPECTED_GROUP

        # Site missing a country can't be placed -> review folder.
        d3 = session.query(Device).filter_by(catalyst_id="d3").one()
        assert device_placement(d3, cfg).resolved is False
        assert group_path(d3, cfg) == "Webasto\\_Review"
    finally:
        session.close()


def test_site_code_override_wins():
    _seed()
    session = SessionLocal()
    try:
        d1 = session.query(Device).filter_by(catalyst_id="d1").one()
        d1.site_code_override = "ABC"
        session.commit()
        assert site_code_for(d1, get_settings()) == "ABC"
        assert "Stockholm (ABC)" in group_path(d1, get_settings())
    finally:
        session.close()


def test_asset_summary_and_csv_description():
    _seed()
    session = SessionLocal()
    try:
        rows = {r["Name"]: r for r in build_rows(session)}
        desc = rows["SSTO010CIS"]["Description"]
        assert "Model: C9300-48" in desc
        assert "IOS: 17.9.4" in desc
        assert "S/N: FOC123" in desc
        assert rows["SSTO010CIS"]["Group"] == EXPECTED_GROUP
    finally:
        session.close()


def test_tree_matches_export():
    """Every device must land in the same folder in the tree preview and the CSV."""
    _seed()
    session = SessionLocal()
    try:
        csv_group = {r["Name"]: r["Group"] for r in build_rows(session)}

        tree = build_tree(session)
        root = tree["root"]
        tree_group: dict[str, str] = {}
        for region in tree["regions"]:
            for country in region["countries"]:
                for site in country["sites"]:
                    for building in site["buildings"]:
                        segs = [root, region["region"], country["country"],
                                site["site"], building["building"]]
                        path = "\\".join(x for x in segs if x)
                        for d in building["devices"]:
                            tree_group[d.effective_hostname or d.management_ip] = path
        for d in tree["review"]:
            segs = [root, tree["review_group"]]
            tree_group[d.effective_hostname or d.management_ip] = "\\".join(x for x in segs if x)

        assert csv_group == tree_group
        assert csv_group["SSTO010CIS"] == EXPECTED_GROUP
        assert csv_group["SMUC001CIS"] == "Webasto\\_Review"
    finally:
        session.close()


def test_migration_backfills_missing_columns():
    """An existing volume from an older schema must auto-upgrade, not 500.

    Reproduces the production bug: create_all leaves an existing table alone, so
    a DB predating the geo/asset columns was missing them and every query failed
    with 'no such column'. init_db() must add them.
    """
    from sqlalchemy import inspect, text
    from app.db import engine, init_db

    with engine.begin() as conn:  # simulate the pre-hierarchy devices table
        conn.execute(text("DROP TABLE IF EXISTS devices"))
        conn.execute(text(
            "CREATE TABLE devices (id INTEGER PRIMARY KEY, catalyst_id VARCHAR, "
            "hostname VARCHAR, hostname_override VARCHAR, management_ip VARCHAR, "
            "family VARCHAR, role VARCHAR, platform VARCHAR, series VARCHAR, "
            "software_version VARCHAR, reachability VARCHAR, site_id INTEGER, "
            "site_override_id INTEGER, excluded BOOLEAN, seen_in_last_sync BOOLEAN, "
            "synced_at DATETIME)"
        ))

    init_db()  # should ALTER TABLE ADD COLUMN the missing ones

    cols = {c["name"] for c in inspect(engine).get_columns("devices")}
    assert {"serial_number", "site_code_override"} <= cols
    with TestClient(app) as client:
        assert client.get("/").status_code == 200  # no 'no such column' 500


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
