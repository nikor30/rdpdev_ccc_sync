"""Test setup: point the app at a throwaway SQLite DB before it is imported."""
import os
import pathlib
import tempfile

# Must be set before `app.config` / `app.db` are imported (engine is built at
# import time from DATABASE_URL).
_TMP_DB = pathlib.Path(tempfile.gettempdir()) / "catalyst_rdm_test.db"
if _TMP_DB.exists():
    _TMP_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["SYNC_ON_STARTUP"] = "false"
os.environ["SYNC_INTERVAL_MINUTES"] = "0"
os.environ["WEB_USERNAME"] = ""
