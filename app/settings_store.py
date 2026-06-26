"""Effective settings = env defaults overlaid with GUI overrides from the DB.

The env / ``.env`` values (via :class:`Settings`) are the bootstrap defaults.
Anything edited on the Settings page is stored in the ``app_settings`` table and
wins over the env, so changes survive restarts (the DB lives on the volume).

``database_url`` is deliberately NOT editable here: it is needed to open the DB
in the first place, so it can only come from the environment.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .db import AppSetting, SessionLocal


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    section: str
    kind: str  # "text" | "int" | "bool" | "secret" | "readonly"
    help: str = ""


# Order matters: fields are grouped into <fieldset>s by their (contiguous) section.
FIELDS: list[Field] = [
    Field("catalyst_base_url", "Base URL", "Catalyst Center", "text",
          "e.g. https://dnac.example.com"),
    Field("catalyst_username", "Username", "Catalyst Center", "text",
          "A read-only OBSERVER-ROLE account is enough."),
    Field("catalyst_password", "Password", "Catalyst Center", "secret"),
    Field("catalyst_verify_ssl", "Verify TLS certificate", "Catalyst Center", "bool",
          "Uncheck only for lab appliances with self-signed certs."),
    Field("catalyst_timeout", "Request timeout (seconds)", "Catalyst Center", "int"),

    Field("switch_families", "Switch families", "Inventory mapping", "text",
          "Comma-separated Catalyst families to import."),
    Field("region_hierarchy_level", "Region hierarchy level", "Inventory mapping", "int",
          "Index after 'Global' that is the region (Global/EMEA/... = 1)."),
    Field("site_code_regex", "Site-code regex", "Inventory mapping", "text",
          "Regex with one capture group pulling the 3-letter code from a "
          "hostname. SSTO010CIS -> STO. The capture is upper-cased."),

    Field("sync_interval_minutes", "Background sync interval (minutes)", "Scheduling", "int",
          "0 disables the scheduler. Applied immediately on save."),
    Field("sync_on_startup", "Sync once on startup", "Scheduling", "bool",
          "Only takes effect on the next restart."),

    Field("web_username", "Web username", "Web UI login", "text",
          "Set username AND password to require HTTP Basic auth on this UI."),
    Field("web_password", "Web password", "Web UI login", "secret"),

    Field("ssh_connection_type", "RDM connection type", "Devolutions export", "text"),
    Field("export_root", "Root folder", "Devolutions export", "text",
          "Top-level RDM folder the whole tree hangs under (e.g. Webasto). "
          "Leave blank if you import under an existing root folder."),
    Field("export_unsorted_group", "Review folder name", "Devolutions export", "text",
          "Folder for devices that can't be placed (missing region/country/code)."),

    Field("database_url", "Database URL", "Storage", "readonly",
          "Set via the DATABASE_URL env var; changing it requires a restart."),
]

FIELDS_BY_NAME: dict[str, Field] = {f.name: f for f in FIELDS}
EDITABLE_NAMES: set[str] = {f.name for f in FIELDS if f.kind != "readonly"}

# Cached effective settings; invalidated whenever overrides are saved.
_cache: Settings | None = None


class SettingsError(ValueError):
    """Raised when a submitted value is invalid (e.g. a non-numeric int field)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def _overrides() -> dict[str, str]:
    session = SessionLocal()
    try:
        return {
            row.key: row.value
            for row in session.query(AppSetting).all()
            if row.key in EDITABLE_NAMES
        }
    finally:
        session.close()


def invalidate() -> None:
    global _cache
    _cache = None


def get_settings() -> Settings:
    """Return the effective settings (env defaults + DB overrides), cached."""
    global _cache
    if _cache is None:
        # Explicit kwargs take precedence over env in pydantic-settings, and the
        # string values are coerced to each field's declared type.
        _cache = Settings(**_overrides())
    return _cache


def save(form: dict[str, str]) -> None:
    """Validate and persist the editable fields from a submitted form.

    Bool fields follow the checkbox convention ("on" == checked). Secret fields
    left blank keep their current value (so the page never has to echo secrets).
    """
    cleaned: dict[str, str] = {}
    for f in FIELDS:
        if f.kind == "readonly":
            continue
        if f.kind == "bool":
            cleaned[f.name] = "true" if form.get(f.name) == "on" else "false"
        elif f.kind == "secret":
            raw = (form.get(f.name) or "").strip()
            if raw:  # blank => keep current; don't write an override
                cleaned[f.name] = raw
        elif f.kind == "int":
            raw = (form.get(f.name) or "").strip()
            try:
                cleaned[f.name] = str(int(raw))
            except ValueError:
                raise SettingsError(f.name, f"{f.label} must be a whole number.")
        else:
            cleaned[f.name] = (form.get(f.name) or "").strip()

    session = SessionLocal()
    try:
        existing = {row.key: row for row in session.query(AppSetting).all()}
        for key, value in cleaned.items():
            row = existing.get(key)
            if row is None:
                session.add(AppSetting(key=key, value=value))
            else:
                row.value = value
        session.commit()
    finally:
        session.close()
    invalidate()


def grouped_fields() -> list[tuple[str, list[Field]]]:
    """Fields grouped into ordered (section, fields) pairs for rendering."""
    groups: list[tuple[str, list[Field]]] = []
    for f in FIELDS:
        if not groups or groups[-1][0] != f.section:
            groups.append((f.section, []))
        groups[-1][1].append(f)
    return groups


def view_model(form: dict[str, str] | None = None) -> dict:
    """Values for rendering the form.

    Without ``form`` it shows the effective settings. With ``form`` (a rejected
    submission) it echoes what the user typed so nothing is lost on error.
    Secret values are never sent to the browser; ``secret_set`` only says whether
    one is currently configured.
    """
    cfg = get_settings()
    values: dict[str, object] = {}
    secret_set: dict[str, bool] = {}
    for f in FIELDS:
        effective = getattr(cfg, f.name, "")
        if f.kind == "secret":
            secret_set[f.name] = bool(effective)
            values[f.name] = ""  # never echo secrets
        elif f.kind == "bool":
            if form is not None:
                values[f.name] = form.get(f.name) == "on"
            else:
                values[f.name] = bool(effective)
        else:
            if form is not None and f.kind != "readonly":
                values[f.name] = form.get(f.name, "")
            else:
                values[f.name] = effective
    return {"groups": grouped_fields(), "values": values, "secret_set": secret_set}
