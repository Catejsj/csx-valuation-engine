"""Where the engine keeps its data, on Linux, Windows and macOS.

Running from source: <project>/data/
Running as a packaged app: the OS's per-user data folder.
CSX_DATA_DIR overrides both (used by tests; the older name WSE2_DATA_DIR still works).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "csx-valuation-engine"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _user_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def data_dir() -> Path:
    """Return the data folder, creating it if needed."""
    override = os.environ.get("CSX_DATA_DIR") or os.environ.get("WSE2_DATA_DIR")
    if override:
        path = Path(override)
    elif getattr(sys, "frozen", False):
        path = _user_data_dir()
    else:
        path = PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "facts.sqlite"
