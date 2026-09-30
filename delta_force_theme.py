"""
Delta Force GUI — theme palettes, layout constants, and settings I/O.

Split out of delta_force_gui.py so the color/layout system and the
settings.json read/write can be reused or tweaked without wading through
2000+ lines of widget code.
"""

import json
from pathlib import Path

from delta_force_paths import APP_DATA_DIR, migrate_legacy_file

# =====================================================================
# Themes
# =====================================================================
THEMES = {
    "Midnight": {
        "BG_TOP": "#0f1a2e", "BG_BOTTOM": "#0a1220", "SHADOW": "#040810",
        "SURFACE": "#15223a", "SURFACE_ALT": "#1a2a44",
        "SURFACE_HOVER": "#1e3050", "SURFACE_ACTIVE": "#24405f",
        "BORDER": "#1c2c46", "BORDER_SOFT": "#18263e",
        "FG": "#e6ecf7", "FG_DIM": "#94a6c2", "FG_MUTED": "#5c6e8d",
        "ACCENT": "#5fd089", "ACCENT_HI": "#7ee4a4", "ACCENT_SOFT": "#1c3d2d",
        "POSITIVE": "#5fd089", "NEGATIVE": "#f27488",
        "SELECT_BG": "#1e3a5c", "SELECT_FG": "#eef5ff",
    },
    "Steel": {
        "BG_TOP": "#131823", "BG_BOTTOM": "#0c1018", "SHADOW": "#04060a",
        "SURFACE": "#1a212e", "SURFACE_ALT": "#202837",
        "SURFACE_HOVER": "#262f42", "SURFACE_ACTIVE": "#2d3850",
        "BORDER": "#242e40", "BORDER_SOFT": "#1e2736",
        "FG": "#e8ecf1", "FG_DIM": "#94a0b5", "FG_MUTED": "#5e687a",
        "ACCENT": "#f0a868", "ACCENT_HI": "#ffbe82", "ACCENT_SOFT": "#3a2a1a",
        "POSITIVE": "#7ed09a", "NEGATIVE": "#ef7a8c",
        "SELECT_BG": "#3a2f1c", "SELECT_FG": "#fff3e3",
    },
    "Void": {
        "BG_TOP": "#15101f", "BG_BOTTOM": "#0c0814", "SHADOW": "#050308",
        "SURFACE": "#1d1730", "SURFACE_ALT": "#241d3a",
        "SURFACE_HOVER": "#2c2445", "SURFACE_ACTIVE": "#352b52",
        "BORDER": "#2a2144", "BORDER_SOFT": "#221a38",
        "FG": "#ece6f7", "FG_DIM": "#a598c4", "FG_MUTED": "#6b6088",
        "ACCENT": "#b58cff", "ACCENT_HI": "#c9a8ff", "ACCENT_SOFT": "#2d1f4a",
        "POSITIVE": "#7ed9b0", "NEGATIVE": "#ff7fa5",
        "SELECT_BG": "#3a2860", "SELECT_FG": "#f7f1ff",
    },
    "Day": {
        "BG_TOP": "#f8fafd", "BG_BOTTOM": "#eef2f8", "SHADOW": "#c8d2e0",
        "SURFACE": "#ffffff", "SURFACE_ALT": "#f4f7fb",
        "SURFACE_HOVER": "#eef3f9", "SURFACE_ACTIVE": "#e3ebf5",
        "BORDER": "#dfe6ef", "BORDER_SOFT": "#e8edf4",
        "FG": "#0f1a2e", "FG_DIM": "#55647e", "FG_MUTED": "#93a0b5",
        "ACCENT": "#2d6fd6", "ACCENT_HI": "#4a86e6", "ACCENT_SOFT": "#dce8fa",
        "POSITIVE": "#17996b", "NEGATIVE": "#d63355",
        "SELECT_BG": "#dce8fa", "SELECT_FG": "#0d2649",
    },
}

DEFAULT_THEME = "Midnight"

# =====================================================================
# Layout constants
# =====================================================================
OUTER_PAD_X = 40
COL_GAP = 24
SIDEBAR_W = 220
RAIL_W = 280
CARD_RADIUS = 14
NAV_HEIGHT = 46

# Interior width actually available for text inside the right rail, after
# the Card's shadow inset (radius=16 -> inset=6px/side), the rail body's
# own padx (24px each side, set where the rail Card is built), and the
# rail's scrollbar (~18px, since the rail content sits in a scrolling
# canvas — see _build_rail). Anything wrapped to fit the rail (captions,
# hints) should use this rather than RAIL_W itself, or it clips against
# the card edge.
RAIL_INNER_W = RAIL_W - 2 * 6 - 2 * 24 - 18

# Same idea for the sidebar: Card inset (radius=16 -> 6px/side) plus the
# footer frame's own padx (22px each side).
SIDEBAR_INNER_W = SIDEBAR_W - 2 * 6 - 2 * 22

# =====================================================================
# Settings persistence
# =====================================================================
migrate_legacy_file(Path(__file__).resolve().parent, "gui_settings.json")
SETTINGS_FILE = APP_DATA_DIR / "gui_settings.json"

# Auto-refresh interval options (in minutes).
AUTO_REFRESH_OPTIONS = [5, 10, 15, 30, 60]
DEFAULT_AUTO_REFRESH_MINUTES = 15

# Default settings dict; missing keys are filled from here on load.
DEFAULT_SETTINGS = {
    "theme": DEFAULT_THEME,
    "discord_rpc": False,
    "auto_refresh_enabled": True,
    "auto_refresh_minutes": DEFAULT_AUTO_REFRESH_MINUTES,
    "auto_refresh_notify": False,
    "community_prompted": False,
    "overlay_enabled": False,
    "overlay_hotkey": "Ctrl+Shift+D",  # matches delta_force_overlay.DEFAULT_HOTKEY_LABEL
    # "preset" (use overlay_hotkey above) or "custom" (use the three
    # fields below, set by recording a combination in Settings). See
    # delta_force_overlay.resolve_hotkey for how these combine.
    "overlay_hotkey_source": "preset",
    "overlay_hotkey_mods": None,
    "overlay_hotkey_vk": None,
    "overlay_hotkey_custom_label": None,
    "minimize_to_tray": False,
    # start_on_boot mirrors delta_force_startup's registry Run-key state
    # (that module reads the registry directly as the source of truth -
    # this setting is really just "what to show as checked" and "what
    # minimized preference to re-apply if the user re-enables it later").
    "start_on_boot": False,
    "start_minimized": False,
    # Persisted (not just in-memory) so a session survives an app
    # restart mid-play - e.g. an update installed while someone's
    # mid-session shouldn't silently split their session in two.
    # session_start is an ISO datetime string, or None when no session
    # is active.
    "session_active": False,
    "session_start": None,
    # Empty string, not APP_VERSION, so _maybe_show_whats_new() in
    # delta_force_gui.py can tell "genuinely never recorded a version
    # before" (first run of THIS feature, on either a brand-new install
    # or an existing one upgrading into it) apart from "already caught
    # up as of last launch".
    "last_seen_version": "",
    # Background-downloads a detected update ahead of time so accepting
    # the install is instant - never installs/replaces anything without
    # an explicit click, regardless of this setting.
    "auto_download_updates": True,
    # Whether the person has seen and acknowledged that leaderboard
    # sharing includes match history (not just aggregate stats). A fresh
    # opt-in already covers this via the checkbox's own wording; this
    # flag exists for people who opted in before that wording did.
    "community_match_history_ack": False,
    # True once the person has clicked through the first-run disclaimer.
    # Existing installs upgrading into this start False, so they see it once.
    "disclaimer_accepted": False,
    # While joined AND opted in, quietly push fresh stats after each match
    # refresh so the daily leaderboard reflects today rather than whenever
    # "Sync My Stats Now" was last clicked. Never sends anything for someone
    # who isn't opted in, whatever this says.
    "community_auto_sync": True,
}


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            loaded = json.load(f)
    except Exception:
        loaded = {}
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(loaded, dict):
        merged.update(loaded)
    return merged


def save_settings(s: dict) -> None:
    """Merge the provided keys into the existing settings file.

    Using a merge (rather than overwrite) means callers can save just the
    keys they changed and won't clobber auto-refresh or RPC preferences.
    """
    current = load_settings()
    current.update(s)
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(current, f, indent=2)
    except Exception:
        pass


def safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
