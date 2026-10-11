"""Where phonemic keeps its certificate, token, settings and log."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_config_dir() -> Path:
    """Per-user configuration directory for phonemic."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "phonemic"
