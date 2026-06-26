"""Tests for the GUI settings page, persistence/override, and the API test probe."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import settings_store
from app.catalyst import CatalystError
from app.db import AppSetting, SessionLocal, init_db
from app.main import app
from app.settings_store import get_settings

# A complete, valid submission (the real form always posts every field).
BASE_FORM = {
    "catalyst_base_url": "https://dnac.example.com",
    "catalyst_username": "apiuser",
    "catalyst_password": "",            # blank => keep current
    "catalyst_verify_ssl": "on",
    "catalyst_timeout": "30",
    "switch_families": "Switches and Hubs",
    "region_hierarchy_level": "1",
    "site_code_regex": r"^[A-Za-z]?([A-Za-z]{3})",
    "sync_interval_minutes": "0",
    # sync_on_startup omitted => false
    "web_username": "",
    "web_password": "",                 # blank => keep current
    "ssh_connection_type": "SSHShell",
    "export_root": "Webasto",
    "export_unsorted_group": "_Review",
}


def _reset() -> None:
    init_db()
    s = SessionLocal()
    try:
        s.query(AppSetting).delete()
        s.commit()
    finally:
        s.close()
    settings_store.invalidate()


def _form(**overrides):
    return {**BASE_FORM, **overrides}


def test_settings_page_renders():
    _reset()
    with TestClient(app) as client:
        r = client.get("/settings")
        assert r.status_code == 200
        assert "Catalyst Center" in r.text
        assert "Test connection" in r.text
        # Storage field is shown read-only, not as an editable input name.
        assert "Database URL" in r.text


def test_save_persists_and_overrides_env():
    _reset()
    assert get_settings().export_unsorted_group == "_Review"
    with TestClient(app) as client:
        r = client.post("/settings", data=_form(
            export_unsorted_group="_NeedsReview", region_hierarchy_level="2"))
        assert r.status_code == 200  # followed redirect to ?saved=1
    cfg = get_settings()
    assert cfg.export_unsorted_group == "_NeedsReview"
    assert cfg.region_hierarchy_level == 2  # coerced to int


def test_secret_blank_keeps_current():
    _reset()
    with TestClient(app) as client:
        client.post("/settings", data=_form(catalyst_password="s3cret"))
        assert get_settings().catalyst_password == "s3cret"
        # Saving again with a blank password must not wipe it.
        client.post("/settings", data=_form(catalyst_password=""))
        assert get_settings().catalyst_password == "s3cret"


def test_invalid_int_is_rejected():
    _reset()
    with TestClient(app) as client:
        r = client.post("/settings", data=_form(region_hierarchy_level="abc"))
        assert r.status_code == 200
        assert "must be a whole number" in r.text
    # Nothing was persisted; effective value stays at the default.
    assert get_settings().region_hierarchy_level == 1


def test_connection_test_success():
    _reset()

    class _OkClient:
        def __init__(self, *a, **k):
            pass

        def probe(self):
            return {"device_count": 7}

        def close(self):
            pass

    with TestClient(app) as client, patch("app.main.CatalystClient", _OkClient):
        r = client.post("/settings/test", data={
            "catalyst_base_url": "https://dnac.example.com",
            "catalyst_username": "u", "catalyst_password": "p",
            "catalyst_verify_ssl": "on", "catalyst_timeout": "30"})
        body = r.json()
        assert body["ok"] is True
        assert "7 network devices" in body["message"]


def test_connection_test_failure_is_reported():
    _reset()

    class _FailClient:
        def __init__(self, *a, **k):
            pass

        def probe(self):
            raise CatalystError("Authentication failed: 401 Unauthorized")

        def close(self):
            pass

    with TestClient(app) as client, patch("app.main.CatalystClient", _FailClient):
        r = client.post("/settings/test", data={
            "catalyst_base_url": "https://dnac.example.com",
            "catalyst_username": "u", "catalyst_password": "bad",
            "catalyst_verify_ssl": "on", "catalyst_timeout": "30"})
        body = r.json()
        assert body["ok"] is False
        assert "Authentication failed" in body["message"]
