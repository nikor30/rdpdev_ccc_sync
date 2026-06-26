"""Configuration loaded from environment variables / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- Catalyst Center connection ---
    catalyst_base_url: str = "https://sandboxdnac.cisco.com"
    catalyst_username: str = ""
    catalyst_password: str = ""
    catalyst_verify_ssl: bool = True
    catalyst_timeout: int = 30

    # --- Inventory filtering / mapping ---
    # Comma-separated list of Catalyst Center device families to keep.
    switch_families: str = "Switches and Hubs"
    # Which element of the site hierarchy (after "Global") is the region.
    # "Global/EMEA/Munich/Plant-A" -> level 1 == "EMEA".
    region_hierarchy_level: int = 1
    # Regex (with one capture group) that pulls the 3-letter site code out of a
    # device hostname. "SSTO010CIS" -> "STO". The captured group is upper-cased.
    site_code_regex: str = r"^[A-Za-z]?([A-Za-z]{3})"

    # --- Storage ---
    database_url: str = "sqlite:////data/catalyst_rdm.db"

    # --- Scheduling ---
    sync_interval_minutes: int = 0  # 0 disables the background scheduler
    sync_on_startup: bool = False

    # --- Web UI auth (optional HTTP Basic). Leave username empty to disable. ---
    web_username: str = ""
    web_password: str = ""

    # --- Devolutions export ---
    ssh_connection_type: str = "SSHShell"
    export_unsorted_group: str = "_Review"
    # Top-level RDM folder the whole tree hangs under. Blank it if you import
    # under an existing root folder in RDM (to avoid Webasto\Webasto).
    export_root: str = "Webasto"

    @property
    def switch_family_list(self) -> list[str]:
        return [s.strip() for s in self.switch_families.split(",") if s.strip()]


settings = Settings()
