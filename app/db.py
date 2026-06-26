"""Database models and session management (SQLAlchemy 2.0, sync)."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Site(Base):
    """A located place (a Catalyst Center *building*). Devices hang off these."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalyst_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String, default="", index=True)
    hierarchy: Mapped[str] = mapped_column(String, default="")

    region: Mapped[str] = mapped_column(String, default="")  # derived from hierarchy
    region_override: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    name_override: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    country: Mapped[str] = mapped_column(String, default="")  # derived from address
    country_override: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    seen_in_last_sync: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    devices: Mapped[list["Device"]] = relationship(
        back_populates="site", foreign_keys="Device.site_id"
    )

    @property
    def effective_region(self) -> str:
        return (self.region_override or self.region or "").strip()

    @property
    def effective_name(self) -> str:
        return (self.name_override or self.name or "").strip()

    @property
    def effective_country(self) -> str:
        return (self.country_override or self.country or "").strip()

    @property
    def area_name(self) -> str:
        """The campus/area this building sits in (the 'Site' level in the tree).

        From the Catalyst hierarchy ``Global/EMEA/Sweden/Stockholm/Building-1``
        this is ``Stockholm`` — i.e. the segment just above the building, with
        the ``Global`` root and the building's own name removed.
        """
        parts = [p for p in (self.hierarchy or "").split("/") if p]
        if parts and parts[0].lower() == "global":
            parts = parts[1:]
        if parts and parts[-1] == self.name:
            parts = parts[:-1]
        return parts[-1] if parts else ""


class Device(Base):
    """A network switch pulled from Catalyst Center."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalyst_id: Mapped[str] = mapped_column(String, unique=True, index=True)

    hostname: Mapped[str] = mapped_column(String, default="", index=True)
    hostname_override: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    management_ip: Mapped[str] = mapped_column(String, default="", index=True)

    family: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    role: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    series: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    software_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reachability: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    site_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sites.id"), nullable=True
    )
    site_override_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sites.id"), nullable=True
    )
    # Three-letter site code (e.g. STO). Derived from the hostname at export time;
    # this column only holds a manual override for the rare mismatch.
    site_code_override: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)

    seen_in_last_sync: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    site: Mapped[Optional["Site"]] = relationship(
        foreign_keys=[site_id], back_populates="devices"
    )
    site_override: Mapped[Optional["Site"]] = relationship(
        foreign_keys=[site_override_id]
    )

    @property
    def effective_site(self) -> Optional["Site"]:
        return self.site_override or self.site

    @property
    def effective_hostname(self) -> str:
        return (self.hostname_override or self.hostname or "").strip()

    @property
    def asset_summary(self) -> str:
        """Human-readable asset block for the RDM entry's Description field."""
        bits = [
            ("Model", self.platform),
            ("Series", self.series),
            ("IOS", self.software_version),
            ("S/N", self.serial_number),
            ("Role", self.role),
            ("Reachability", self.reachability),
        ]
        return " | ".join(f"{label}: {value}" for label, value in bits if value)


class SyncRun(Base):
    """One execution of the sync engine, for status/history."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")  # running/success/error
    message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sites_synced: Mapped[int] = mapped_column(Integer, default=0)
    devices_synced: Mapped[int] = mapped_column(Integer, default=0)


class AppSetting(Base):
    """A single GUI-editable setting, overriding the env default of the same name.

    Stored as text; pydantic coerces it back to the field's type when the
    effective settings are rebuilt (see ``settings_store``). Only keys that have
    been edited in the UI are present; everything else falls back to the env.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, default="")


# --- Engine / session ---------------------------------------------------------

def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite"):
        path = urlparse(url).path  # e.g. //data/catalyst_rdm.db -> /data/...
        # urlparse keeps a leading slash for absolute paths
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)
engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _default_literal(column) -> str:
    """SQL literal for a NOT NULL column's default, for ALTER TABLE ADD COLUMN."""
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        val = default.arg
        if isinstance(val, bool):
            return "1" if val else "0"
        if isinstance(val, (int, float)):
            return str(val)
        return "'" + str(val).replace("'", "''") + "'"
    if isinstance(column.type, (Integer, Boolean, Float)):
        return "0"
    return "''"


def _migrate_sqlite_columns() -> None:
    """Add any mapped columns missing from existing SQLite tables.

    ``create_all`` creates missing *tables* but never alters existing ones, so a
    database from an earlier release (the /data volume survives image rebuilds)
    is missing newer columns and every query fails with 'no such column'. SQLite
    supports ADD COLUMN, so backfill them here. Idempotent.
    """
    if not str(settings.database_url).startswith("sqlite"):
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue  # freshly created by create_all, already complete
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                coltype = column.type.compile(dialect=engine.dialect)
                clause = f'"{column.name}" {coltype}'
                if not column.nullable:
                    clause += f" NOT NULL DEFAULT {_default_literal(column)}"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {clause}'))
                log.info("DB migration: added column %s.%s", table.name, column.name)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_sqlite_columns()
