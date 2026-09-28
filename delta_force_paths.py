"""
Delta Force — shared writable data directory, and bundled read-only
resource lookup.

Every module that reads or writes a persistent file (match cache, login
credentials, GUI settings, avatar cache, CSV exports, the Playwright login
profile) gets its path from here instead of computing something relative
to its own __file__.

That distinction matters once the app is bundled with PyInstaller: a
frozen module's __file__ points into a temporary extraction folder
(sys._MEIPASS) that gets created fresh - and deleted - on every single
launch. Writing data "next to the script" therefore silently vanishes the
moment the app closes instead of persisting between runs, which is why
login never appeared to save once this was packaged. This module instead
resolves to the normal per-OS user-data location, which is stable across
launches, upgrades, and reinstalls:

    Windows:  %APPDATA%\\DeltaForceTracker
    macOS:    ~/Library/Application Support/DeltaForceTracker
    Linux:    $XDG_DATA_HOME/DeltaForceTracker (or ~/.local/share/...)

This works identically whether the app is run as `python delta_force_gui.py`
or as the bundled .exe - same folder either way - which also means dev runs
and packaged runs now share cache/login data automatically.

resource_path() below is a *different* concept, for a different problem:
locating a read-only file that ships WITH the app (delta_force_items.json,
the item catalog) rather than one the app writes at runtime. Frozen,
that file lives inside sys._MEIPASS (wherever the PyInstaller spec's
`datas` list put it) - not APP_DATA_DIR, and not necessarily the same
directory as the bundled .py modules either, depending on PyInstaller
version/mode. Use resource_path() for anything added to `datas` in
delta_force.spec.
"""

import os
import sys
from pathlib import Path

APP_NAME = "DeltaForceTracker"


def _compute_app_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


APP_DATA_DIR = _compute_app_data_dir()
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)


def resource_path(filename: str) -> Path:
    """Locates a bundled read-only resource file (e.g.
    delta_force_items.json), whether running frozen (PyInstaller) or as
    plain .py files.

    Frozen: PyInstaller extracts everything - the app's own code and any
    file listed in the spec's `datas` - into sys._MEIPASS at launch, so
    that's where bundled resources actually are. Dev mode: no
    sys._MEIPASS exists, so this falls back to the directory this file
    (delta_force_paths.py) itself lives in, which is the project folder
    when running unbundled.

    This does NOT guarantee the file exists - callers still need their
    own fallback for "shipped but somehow missing" (see
    core.load_item_catalog for the pattern: catch the read error, degrade
    gracefully, never crash the app over a missing optional data file).
    """
    base = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
    return base / filename


def migrate_legacy_file(old_dir, filename: str) -> None:
    """One-time copy of a pre-fix data file into the new location.

    Anyone who used an earlier dev build has their real match history and
    login sitting next to the old .py files (that part worked fine
    unbundled - only the frozen .exe silently lost data). Call this once
    per filename, per module, right where the old __file__-relative
    constant used to be defined, passing that same directory as old_dir,
    so upgrading doesn't quietly wipe out someone's cached matches or make
    them log in again for no reason. No-op if the new file already exists
    or the old one is missing (e.g. every frozen-app launch, where the old
    "directory" is just that run's temp extraction folder).
    """
    new_path = APP_DATA_DIR / filename
    if new_path.exists():
        return
    try:
        old_path = Path(old_dir) / filename
        if old_path.is_file():
            new_path.write_bytes(old_path.read_bytes())
    except OSError:
        pass

    """One-time copy of a pre-fix data file into the new location.

    Anyone who used an earlier dev build has their real match history and
    login sitting next to the old .py files (that part worked fine
    unbundled - only the frozen .exe silently lost data). Call this once
    per filename, per module, right where the old __file__-relative
    constant used to be defined, passing that same directory as old_dir,
    so upgrading doesn't quietly wipe out someone's cached matches or make
    them log in again for no reason. No-op if the new file already exists
    or the old one is missing (e.g. every frozen-app launch, where the old
    "directory" is just that run's temp extraction folder).
    """
    new_path = APP_DATA_DIR / filename
    if new_path.exists():
        return
    try:
        old_path = Path(old_dir) / filename
        if old_path.is_file():
            new_path.write_bytes(old_path.read_bytes())
    except OSError:
        pass
