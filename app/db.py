"""Database models and session management (SQLAlchemy 2.0, sync)."""
from __future__ import annotations

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
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import settings


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
    reachability: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    site_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sites.id"), nullable=True
    )
    site_override_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sites.id"), nullable=True
    )
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


def init_db() -> None:
    Base.metadata.create_all(engine)
