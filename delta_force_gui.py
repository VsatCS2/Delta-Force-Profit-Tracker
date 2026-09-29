#!/usr/bin/env python3
"""
Delta Force Profit Tracker — desktop GUI

    pip install requests Pillow pypresence
    pip install playwright && playwright install chromium   # for browser login
    python delta_force_gui.py

Pillow is optional — without it, operator avatars stay blank.
Playwright is optional — without it, the "Log In (browser)" button errors.
pypresence is optional — without it, the Discord RPC toggle is disabled.

Layout/theme constants and settings persistence live in
delta_force_theme.py; the gradient background, Card, sidebar NavItem, and
a couple of small tk helpers live in delta_force_widgets.py. This file is
just the app itself — the sections, the data plumbing, and the workers.
"""

import io
import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import ttk, messagebox

import delta_force_core as core
import delta_force_community as comm
import delta_force_overlay as overlay_mod
import delta_force_startup as startup_mod
import delta_force_updater as updater_mod
import delta_force_legal as legal_mod
from delta_force_paths import APP_DATA_DIR, migrate_legacy_file, resource_path
from delta_force_version import APP_VERSION, CHANGELOG, version_tuple
from delta_force_theme import (
    THEMES, DEFAULT_THEME, OUTER_PAD_X, COL_GAP, SIDEBAR_W,
    RAIL_W, RAIL_INNER_W, SIDEBAR_INNER_W, AUTO_REFRESH_OPTIONS,
    DEFAULT_AUTO_REFRESH_MINUTES, DEFAULT_SETTINGS,
    load_settings as _load_settings, save_settings as _save_settings,
    safe_int as _safe_int,
)
from delta_force_widgets import (
    _Debouncer, make_sortable, round_rect, GradientFrame, Card, NavItem,
    bind_mousewheel_to_canvas,
)

try:
    from PIL import Image, ImageDraw, ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

# Old avatar cache was per-file (many small PNGs), not one migratable
# file, so it isn't worth carrying over — it repopulates itself on next
# fetch either way. Just point it at the stable location going forward.
AVATAR_DIR = APP_DATA_DIR / "avatars"
AVATAR_DIR.mkdir(exist_ok=True)


# =====================================================================
# Main app
# =====================================================================
class TrackerApp:
    # (key, label, icon_key) — icon_key is drawn by delta_force_widgets's
    # _draw_nav_icon, not rendered as a font glyph. See that module's
    # docstring for why.
    NAV_ITEMS = [
        ("overview",  "Overview",  "overview"),
        ("profile",   "Profile",   "profile"),
        ("maps",      "Maps",      "maps"),
        ("matches",   "Matches",   "matches"),
        ("community", "Community", "community"),
        ("settings",  "Settings",  "settings"),
    ]

    def __init__(self, root):
        self.root = root
        self.root.title(f"Delta Force Profit Tracker v{APP_VERSION}")
        self.root.geometry("1280x820")
        self.root.minsize(1080, 680)
        self._set_window_icon()

        self.msg_queue = queue.Queue()
        self.rows = []
        self.summaries = {}
        self.profile_data = None
        self.match_busy = False
        self.profile_busy = False
        self.quick_busy = False
        self._login_busy = False
        self._rebuilding = False

        self.active_section = "overview"

        self.matches_page = 0
        self.matches_page_size = 100
        self.matches_sort_key = "date"
        self.matches_sort_reverse = True
        self.maps_sort_key = "net"
        self.maps_sort_reverse = True
        self._highlight_map = None
        self._best_map_name = None
        self._best_op_name = None

        self._avatar_bytes = {}
        self._avatar_cache = {}
        self._avatar_pending = set()
        self._last_avatar_count = -1

        self._item_icon_cache = {}
        self._item_icon_pending = set()
        self._high_value_items = []   # last fetched, from core.recent_high_value_items
        self._weekly_highlights = None  # last fetched, from core.weekly_highlights

        self._detail_fetching = set()
        self._detail_windows = {}
        self._backfill_stop = False
        self._backfill_active = False

        # --- Community leaderboard state ---
        self._community_busy = False       # link/sync in flight
        self._community_leaderboard = []   # last fetched leaderboard rows
        self._community_status = None      # last fetch_my_status() result
        self.community_period = "all"      # "all" | "daily" - which board is showing
        self._community_meta = {}          # period/day/resets_at from the server
        self._last_auto_sync = datetime.min
        self._staged_update = None          # (version, staged_path) once downloaded+verified
        self._update_download_busy = False
        self.community_sort_key = "net"
        self.community_sort_reverse = True

        # --- Auto-refresh state ---
        self._auto_refresh_after_id = None
        self._last_refresh_ts = None       # datetime of last successful refresh
        self._last_refresh_new = 0         # matches added by last refresh
        self._status_reset_after_id = None

        # --- Load settings (theme + everything else) ---
        self.settings = _load_settings()
        self.theme_name = self.settings.get("theme", DEFAULT_THEME)
        if self.theme_name not in THEMES:
            self.theme_name = DEFAULT_THEME
        self.colors = THEMES[self.theme_name]

        # --- Session tracking ---
        # A manually-started/ended window of matches, independent of
        # calendar-day boundaries - see core.session_stats(). Restored
        # from settings so it survives an app restart mid-session.
        self.session_start = None
        if self.settings.get("session_active") and self.settings.get("session_start"):
            try:
                self.session_start = datetime.fromisoformat(
                    self.settings["session_start"])
            except (ValueError, TypeError):
                self.session_start = None

        self.search_var = tk.StringVar()
        self._search_debouncer = _Debouncer(
            self.root, 150, self._on_search_changed)
        self.search_var.trace_add(
            "write", lambda *_: self._search_debouncer.trigger())

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # --- Overlay / global hotkey / system tray ---
        # Built unconditionally (cheap - no thread starts until enabled),
        # so Settings can flip these on/off without re-creating anything.
        self.overlay = overlay_mod.OverlayWindow(self.root, self.colors)
        self.hotkey_listener = overlay_mod.HotkeyListener(
            on_trigger=lambda: self.root.after(0, self._toggle_overlay))
        self.tray_icon = overlay_mod.TrayIcon(
            self.root,
            icon_path=str(resource_path("app_icon.ico")),
            on_show=self._restore_from_tray,
            on_toggle_overlay=self._toggle_overlay,
            on_exit=self._exit_app,
        )
        self._tray_active = False
        self.root.bind("<Unmap>", self._on_window_minimized)
        if self.settings.get("overlay_enabled"):
            self._start_hotkey_listener()

        self._build_layout()
        self._load_cached_data(initial=True)
        self.root.after(100, self._poll_queue)
        self.root.after(350, lambda: self.start_profile_fetch(silent=True))
        self.root.after(400, lambda: self.start_asset_calendar_fetch(silent=True))
        self.root.after(450, lambda: self.start_weekly_report_fetch(silent=True))
        self.root.after(700, self._start_update_check)
        self.root.after(2500, self._community_startup_check)
        self.root.after(1000, self._repair_startup_entry)
        self.root.after(200, self._poll_avatars)
        self.root.after(300, self._startup_gate)
        self.root.after(900, self._restore_rpc_state)
        # Kick off the auto-refresh loop after the initial fetch settles.
        self.root.after(1500, self._schedule_next_auto_refresh)
        self.root.after(60000, self._session_tick)

        # --run-at-boot launched with --minimized: apply after the window
        # has fully constructed (deferred via after(), not done inline
        # here) rather than skip building it - the window still needs to
        # exist normally for iconify()/withdraw() to have anything to
        # act on, and for the tray icon path to have a real window behind it.
        if "--minimized" in sys.argv:
            self.root.after(150, self._apply_startup_minimized)

    def _apply_startup_minimized(self):
        if self.settings.get("minimize_to_tray") and overlay_mod.TrayIcon.available():
            self._hide_to_tray()
        else:
            self.root.iconify()

    # ------------------------------------------------------------------
    # ttk style
    # ------------------------------------------------------------------
    def _apply_theme_styles(self):
        c = self.colors
        self.root.configure(bg=c["BG_BOTTOM"])

        self.root.option_add("*TCombobox*Listbox.background", c["SURFACE"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["FG"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["SELECT_BG"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["SELECT_FG"])
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".",
                        background=c["BG_BOTTOM"], foreground=c["FG"],
                        fieldbackground=c["SURFACE"], bordercolor=c["BORDER"],
                        lightcolor=c["BG_BOTTOM"], darkcolor=c["BG_BOTTOM"],
                        focuscolor=c["ACCENT"], font=("Segoe UI", 10))
        style.configure("TFrame", background=c["BG_BOTTOM"])
        style.configure("TLabel", background=c["BG_BOTTOM"], foreground=c["FG"])

        style.configure("TScrollbar",
                        background=c["SURFACE_ALT"], troughcolor=c["SURFACE"],
                        bordercolor=c["SURFACE"], arrowcolor=c["SURFACE"],
                        gripcount=0, relief="flat", borderwidth=0,
                        width=10)
        style.map("TScrollbar",
                  background=[("active", c["ACCENT"]), ("pressed", c["ACCENT"])])
        # Flat, modern scrollbar: trough + thumb only, no up/down arrow
        # buttons at the ends. ttk's clam theme draws those by default,
        # which reads as a dated Windows-95-era control next to
        # everything else in this app; most modern apps (browsers,
        # editors) use exactly this thinner arrow-less style instead.
        for orientation in ("Vertical", "Horizontal"):
            style.layout(f"{orientation}.TScrollbar", [
                (f"{orientation}.Scrollbar.trough", {
                    "sticky": "ns" if orientation == "Vertical" else "ew",
                    "children": [
                        (f"{orientation}.Scrollbar.thumb", {
                            "expand": "1",
                            "sticky": "nswe",
                        }),
                    ],
                }),
            ])

        style.configure("TProgressbar",
                        background=c["ACCENT"], troughcolor=c["SURFACE_ALT"],
                        bordercolor=c["SURFACE_ALT"],
                        lightcolor=c["ACCENT"], darkcolor=c["ACCENT"],
                        thickness=3)

        style.configure("Treeview",
                        background=c["SURFACE"], fieldbackground=c["SURFACE"],
                        foreground=c["FG"], rowheight=36,
                        bordercolor=c["SURFACE"], borderwidth=0, relief="flat",
                        font=("Segoe UI", 10))
        style.map("Treeview",
                  background=[("selected", c["SELECT_BG"])],
                  foreground=[("selected", c["SELECT_FG"])])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        style.configure("Treeview.Heading",
                        background=c["SURFACE"], foreground=c["FG_MUTED"],
                        relief="flat", borderwidth=0, padding=(10, 12),
                        font=("Segoe UI", 9, "bold"))
        style.map("Treeview.Heading",
                  background=[("active", c["SURFACE"])],
                  foreground=[("active", c["FG_DIM"])])

        style.configure("TCombobox",
                        fieldbackground=c["SURFACE_ALT"], background=c["SURFACE_ALT"],
                        foreground=c["FG"], bordercolor=c["BORDER_SOFT"],
                        arrowcolor=c["FG_DIM"], padding=6, relief="flat")
        style.map("TCombobox",
                  fieldbackground=[("readonly", c["SURFACE_ALT"])],
                  foreground=[("readonly", c["FG"])],
                  arrowcolor=[("active", c["ACCENT"])],
                  bordercolor=[("focus", c["BORDER"])])

    def _change_theme(self, name: str):
        if name not in THEMES or name == self.theme_name:
            return
        self.theme_name = name
        self.colors = THEMES[name]
        _save_settings({"theme": name})
        self._rebuild_ui()

    def _rebuild_ui(self):
        debug_on = self.debug_var.get()
        self._rebuilding = True
        for child in self.root.winfo_children():
            child.destroy()
        self._build_layout()
        if debug_on:
            self.debug_frame.pack(side="bottom", fill="x")
        self._update_busy_state()
        self._rebuilding = False

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _set_window_icon(self):
        """Sets the actual running window's taskbar/title-bar icon.

        This is a separate thing from the .exe file's icon (which
        delta_force.spec's EXE(icon=...) already sets - that's what
        Explorer shows for the file itself, and what a shortcut displays
        before the app is running). Tkinter windows carry their own icon
        independent of that, set via the Win32 WM_SETICON message under
        the hood - without calling this, Tk falls back to a generic
        default icon in the taskbar and title bar even though the .exe
        itself has the right one. Never allowed to crash the app over a
        cosmetic detail: every platform/failure path here just leaves
        the default Tk icon in place.
        """
        try:
            icon_path = resource_path("app_icon.ico")
            if not icon_path.exists():
                return
            if sys.platform.startswith("win"):
                # .ico is native on Windows; iconbitmap(default=...) sets
                # it for this window AND every Toplevel opened from it
                # (match detail popups, etc.), not just the main window.
                self.root.iconbitmap(default=str(icon_path))
            elif HAVE_PIL:
                # X11 (Linux) and Aqua (macOS) Tk don't understand .ico -
                # iconphoto wants a real image, so decode a frame from it
                # with Pillow instead. Keep a reference on self: PhotoImage
                # is garbage-collected (and the icon silently vanishes)
                # the moment nothing holds onto it.
                img = Image.open(icon_path)
                self._window_icon_photo = ImageTk.PhotoImage(img)
                self.root.iconphoto(True, self._window_icon_photo)
        except Exception:
            pass

    def _build_layout(self):
        self._apply_theme_styles()
        c = self.colors

        self.bg = GradientFrame(self.root, c["BG_TOP"], c["BG_BOTTOM"])
        self.bg.pack(fill="both", expand=True)
        self.bg.body.pack(fill="both", expand=True)
        root_body = self.bg.body
        root_body.configure(bg=c["BG_TOP"])

        header = tk.Frame(root_body, bg=c["BG_TOP"], height=88)
        header.pack(side="top", fill="x", padx=OUTER_PAD_X, pady=(24, 0))
        header.pack_propagate(False)

        title = tk.Frame(header, bg=c["BG_TOP"])
        title.pack(side="left", fill="y")
        tk.Label(title, text="Delta Force", bg=c["BG_TOP"], fg=c["FG"],
                 font=("Segoe UI Semibold", 24)).pack(side="left")
        tk.Label(title, text="  Profit Tracker", bg=c["BG_TOP"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 24)).pack(side="left")

        right = tk.Frame(header, bg=c["BG_TOP"])
        right.pack(side="right", fill="y")
        tk.Label(right, text="THEME", bg=c["BG_TOP"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 10))
        self.theme_var = tk.StringVar(value=self.theme_name)
        theme_combo = ttk.Combobox(right, textvariable=self.theme_var,
                                   values=list(THEMES.keys()),
                                   state="readonly", width=10)
        theme_combo.bind("<<ComboboxSelected>>",
                         lambda e: self._change_theme(self.theme_var.get()))
        theme_combo.pack(side="left", pady=24)

        body = tk.Frame(root_body, bg=c["BG_TOP"])
        body.pack(side="top", fill="both", expand=True,
                  padx=OUTER_PAD_X, pady=(20, 20))

        self.sidebar = Card(body, c, padding=(0, 0), radius=16, shadow=True)
        self.sidebar.configure(width=SIDEBAR_W)
        self.sidebar.pack(side="left", fill="y", padx=(0, COL_GAP))
        self.sidebar.pack_propagate(False)
        self._build_sidebar(self.sidebar.body)

        self.rail = Card(body, c, padding=(24, 24), radius=16, shadow=True)
        self.rail.configure(width=RAIL_W)
        self.rail.pack(side="right", fill="y", padx=(COL_GAP, 0))
        self.rail.pack_propagate(False)
        self._build_rail(self.rail.body)

        self.content = tk.Frame(body, bg=c["BG_TOP"])
        self.content.pack(side="left", fill="both", expand=True)

        status = tk.Frame(root_body, bg=c["BG_TOP"], height=36)
        status.pack(side="bottom", fill="x", padx=OUTER_PAD_X, pady=(0, 16))
        status.pack_propagate(False)
        self.status_label = tk.Label(status, text="Ready.", bg=c["BG_TOP"],
                                     fg=c["FG_MUTED"], font=("Segoe UI", 9))
        self.status_label.pack(side="left", pady=8)
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=120)
        self.progress.pack(side="right", pady=14)

        self.debug_frame = tk.Frame(root_body, bg=c["BG_BOTTOM"])
        self.debug_text = tk.Text(self.debug_frame, height=8,
                                   bg=c["SURFACE"], fg=c["FG_DIM"],
                                   insertbackground=c["FG"],
                                   selectbackground=c["SELECT_BG"],
                                   selectforeground=c["SELECT_FG"],
                                   highlightbackground=c["BORDER"],
                                   highlightcolor=c["ACCENT"],
                                   highlightthickness=1, bd=0,
                                   font=("Consolas", 9), state="disabled")
        self.debug_text.pack(fill="both", expand=True,
                             padx=OUTER_PAD_X, pady=(0, 10))

        self._section_frames = {}
        self._build_overview_section()
        self._build_profile_section()
        self._build_maps_section()
        self._build_matches_section()
        self._build_community_section()
        self._build_settings_section()
        self._show_section(self.active_section)

    # ------------------------------------------------------------------
    # Sidebar
    # ------------------------------------------------------------------
    def _build_sidebar(self, parent):
        c = self.colors

        brand = tk.Frame(parent, bg=c["SURFACE"])
        brand.pack(fill="x", padx=22, pady=(26, 20))
        tk.Label(brand, text="◈", bg=c["SURFACE"], fg=c["ACCENT"],
                 font=("Segoe UI", 18)).pack(side="left")
        tk.Label(brand, text="  DF Tracker", bg=c["SURFACE"], fg=c["FG"],
                 font=("Segoe UI Semibold", 13)).pack(side="left")

        tk.Frame(parent, bg=c["BORDER_SOFT"], height=1).pack(
            fill="x", padx=22, pady=(0, 18))

        tk.Label(parent, text="MENU", bg=c["SURFACE"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
            fill="x", padx=22, pady=(0, 8))

        self._nav_items = {}
        nav_box = tk.Frame(parent, bg=c["SURFACE"])
        nav_box.pack(fill="x", padx=10)
        for key, label, icon_key in self.NAV_ITEMS:
            item = NavItem(nav_box, c, icon_key, label,
                           command=lambda k=key: self._show_section(k))
            item.pack(fill="x", pady=3)
            self._nav_items[key] = item

        footer = tk.Frame(parent, bg=c["SURFACE"])
        footer.pack(side="bottom", fill="x", padx=22, pady=22)

        tk.Frame(footer, bg=c["BORDER_SOFT"], height=1).pack(fill="x", pady=(0, 14))

        nick_row = tk.Frame(footer, bg=c["SURFACE"])
        nick_row.pack(fill="x")
        self.sidebar_status_dot = tk.Canvas(
            nick_row, width=8, height=8, bg=c["SURFACE"],
            highlightthickness=0, bd=0)
        self.sidebar_status_dot.pack(side="left", padx=(0, 7))
        self._sidebar_dot_id = self.sidebar_status_dot.create_oval(
            0, 0, 8, 8, fill=c["FG_MUTED"], outline="")
        self.sidebar_nick = tk.Label(nick_row, text="—", bg=c["SURFACE"],
                                     fg=c["FG"],
                                     font=("Segoe UI Semibold", 11),
                                     anchor="w")
        self.sidebar_nick.pack(side="left", fill="x", expand=True)
        self.sidebar_rank = tk.Label(footer, text="No profile", bg=c["SURFACE"],
                                     fg=c["FG_MUTED"],
                                     font=("Segoe UI", 9), anchor="w")
        self.sidebar_rank.pack(fill="x", pady=(4, 0))
        self.sidebar_creds = tk.Label(footer, text="", bg=c["SURFACE"],
                                      fg=c["FG_MUTED"],
                                      font=("Segoe UI", 8), anchor="w",
                                      wraplength=SIDEBAR_INNER_W, justify="left")
        self.sidebar_creds.pack(fill="x", pady=(8, 0))

        tk.Label(footer, text=f"v{APP_VERSION}", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 8),
                 anchor="w").pack(fill="x", pady=(4, 0))

    def _show_section(self, key: str):
        self.active_section = key
        for k, item in self._nav_items.items():
            item.set_active(k == key)

        for k, frame in self._section_frames.items():
            if k == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

        self._rerender_section(key)

    # ------------------------------------------------------------------
    # Right rail
    # ------------------------------------------------------------------
    def _build_rail(self, parent):
        c = self.colors

        # Wrapped in a scrolling canvas, same pattern as the Settings
        # tab: Quick Stats + actions can comfortably exceed the rail's
        # visible height on a smaller window, and a silently-clipped
        # button/caption is worse than an occasional scrollbar.
        scroll_holder = tk.Frame(parent, bg=c["SURFACE"])
        scroll_holder.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroll_holder, bg=c["SURFACE"], highlightthickness=0)
        scroll = ttk.Scrollbar(scroll_holder, orient="vertical",
                                command=canvas.yview)
        inner = tk.Frame(canvas, bg=c["SURFACE"])
        inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                   lambda e: canvas.itemconfig(inner_win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        # Pack the scrollbar before the canvas: with only the canvas
        # using expand=True, packing it first claims the *entire*
        # remaining cavity immediately, leaving the scrollbar a 0-width
        # sliver (present, but invisible and useless). Scrollbar-first
        # gives it its real width, and the canvas still expands to fill
        # whatever's left.
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        bind_mousewheel_to_canvas(canvas)

        tk.Label(inner, text="Quick Stats", bg=c["SURFACE"], fg=c["FG"],
                 font=("Segoe UI Semibold", 14), anchor="w").pack(
            fill="x", pady=(0, 18))

        self.rail_rows = {}
        for key, label in [
            ("matches",  "MATCHES"),
            ("wl",       "WINS / LOSSES"),
            ("winrate",  "WIN RATE"),
            ("avg",      "AVG PER MATCH"),
            ("best_map", "BEST MAP"),
            ("best_op",  "BEST OPERATOR"),
        ]:
            row = tk.Frame(inner, bg=c["SURFACE"])
            row.pack(fill="x", pady=7)
            tk.Label(row, text=label, bg=c["SURFACE"], fg=c["FG_MUTED"],
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")
            val = tk.Label(row, text="—", bg=c["SURFACE"], fg=c["FG"],
                           font=("Segoe UI Semibold", 13), anchor="w",
                           justify="left", wraplength=RAIL_INNER_W)
            val.pack(fill="x", pady=(4, 0))
            self.rail_rows[key] = val

        self.rail_rows["best_map"].configure(cursor="hand2")
        self.rail_rows["best_map"].bind("<Button-1>", lambda e: self._jump_to_map())
        self.rail_rows["best_op"].configure(cursor="hand2")
        self.rail_rows["best_op"].bind("<Button-1>", lambda e: self._jump_to_operator())

        actions = tk.Frame(inner, bg=c["SURFACE"])
        actions.pack(fill="x", pady=(20, 0))

        tk.Frame(actions, bg=c["BORDER_SOFT"], height=1).pack(fill="x", pady=(0, 16))

        # Only the actions you'll reach for constantly live here. Anything
        # slower, rarer, or more of a "settings" toggle (full re-fetch,
        # profile pull, backfilling match details, CSV export, Discord
        # RPC) lives in Settings -> Data & Export / Profile & Match
        # Details instead, so this rail doesn't turn into a wall of
        # look-alike buttons.
        self.login_btn = self._pill_button(actions, "Log In (browser)",
                                            self.start_browser_login, primary=True)
        self.login_btn.pack(fill="x", pady=4)

        self.quick_btn = self._pill_button(actions, "Refresh Today",
                                           self.start_quick_refresh, primary=False)
        self.quick_btn.pack(fill="x", pady=4)
        self.refresh_btn = self._pill_button(actions, "Sync New Matches",
                                             lambda: self.start_fetch(False))
        self.refresh_btn.pack(fill="x", pady=(4, 0))

        tk.Label(actions,
                 text="Today = fast, recent pages only. Sync = also "
                      "catches older matches you missed. Full re-pull "
                      "and profile refresh are in Settings.",
                 bg=c["SURFACE"], fg=c["FG_MUTED"], font=("Segoe UI", 8),
                 anchor="w", justify="left",
                 wraplength=RAIL_INNER_W).pack(fill="x", pady=(8, 4))

        settings = _load_settings()
        self.rpc_var = tk.BooleanVar(value=settings.get("discord_rpc", False))

        self.debug_var = tk.BooleanVar(value=core.is_debug())
        debug_chk = tk.Checkbutton(
            actions, text="Show debug log",
            variable=self.debug_var, command=self._toggle_debug_panel,
            background=c["SURFACE"], foreground=c["FG_MUTED"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"],
            highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 9), anchor="w",
        )
        debug_chk.pack(fill="x", pady=(6, 4))

    def _pill_button(self, parent, text, command, primary=False):
        c = self.colors
        bg = c["ACCENT"] if primary else c["SURFACE_ALT"]
        fg = c["BG_BOTTOM"] if primary else c["FG"]
        hover_bg = c["ACCENT_HI"] if primary else c["SURFACE_HOVER"]

        btn = tk.Label(parent, text=text, bg=bg, fg=fg,
                       font=("Segoe UI Semibold", 10),
                       padx=16, pady=10, anchor="center", cursor="hand2")
        btn._enabled = True
        btn._primary = primary

        def on_enter(_e):
            if btn._enabled:
                btn.configure(bg=hover_bg)

        def on_leave(_e):
            btn.configure(bg=bg if btn._enabled else c["SURFACE_ALT"])

        def on_click(_e):
            if btn._enabled:
                command()

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        btn.bind("<Button-1>", on_click)

        def set_state(state):
            enabled = (state != "disabled")
            btn._enabled = enabled
            btn.configure(bg=bg if enabled else c["SURFACE_ALT"],
                          fg=fg if enabled else c["FG_MUTED"],
                          cursor="hand2" if enabled else "arrow")

        btn.set_state = set_state
        return btn

    def _update_rail(self):
        ov = core.overview(self.rows)
        c = self.colors
        self.rail_rows["matches"].configure(text=str(ov["matches"]))
        self.rail_rows["wl"].configure(text=f"{ov['wins']} / {ov['losses']}")
        self.rail_rows["winrate"].configure(text=f"{ov['win_rate']:.1f}%")

        avg = (ov["all_time"] / ov["matches"]) if ov["matches"] else 0
        self.rail_rows["avg"].configure(text=core.fmt_money(avg))

        map_groups, op_groups = {}, {}
        for r in self.rows:
            map_groups[r["map_name"]] = map_groups.get(r["map_name"], 0) + r["net_income"]
            op_groups[r["operator_name"]] = op_groups.get(r["operator_name"], 0) + r["net_income"]

        if map_groups:
            name, val = max(map_groups.items(), key=lambda kv: kv[1])
            self._best_map_name = name
            self.rail_rows["best_map"].configure(
                text=f"{name}\n{core.fmt_money(val)}  ›",
                fg=c["POSITIVE"] if val >= 0 else c["NEGATIVE"])
        else:
            self._best_map_name = None
            self.rail_rows["best_map"].configure(text="—", fg=c["FG"])

        if op_groups:
            name, val = max(op_groups.items(), key=lambda kv: kv[1])
            self._best_op_name = name
            self.rail_rows["best_op"].configure(
                text=f"{name}\n{core.fmt_money(val)}  ›",
                fg=c["POSITIVE"] if val >= 0 else c["NEGATIVE"])
        else:
            self._best_op_name = None
            self.rail_rows["best_op"].configure(text="—", fg=c["FG"])

    def _jump_to_map(self):
        if not self._best_map_name:
            return
        self._highlight_map = self._best_map_name
        self._show_section("maps")

    def _jump_to_operator(self):
        if not self._best_op_name:
            return
        self.search_var.set(self._best_op_name)
        self._show_section("matches")

    # ------------------------------------------------------------------
    # First-run login prompt
    # ------------------------------------------------------------------
    def _maybe_prompt_login(self):
        # have_credentials() only looks at the in-memory credential
        # blocks, which start empty on every launch - they're populated
        # by refresh_credentials_from_browser() reading dftools_creds.json
        # off disk. The startup gate fires this prompt before any of the
        # fetches (which are what normally trigger that refresh) have
        # run, so without loading first this fired on every single launch
        # for someone who was already logged in. current_openid() already
        # uses this same load-before-check pattern; mirror it here.
        try:
            core.refresh_credentials_from_browser()
        except Exception:
            pass
        if core.have_credentials("matchlist"):
            return

        answer = messagebox.askyesno(
            "Welcome to DF Tracker",
            "Before the tracker can fetch your stats, you need to log in.\n\n"
            "Click Yes to open a browser window where you can sign in. "
            "Just log in normally — the tracker picks up everything it "
            "needs automatically from there.\n\n"
            "Open the login browser now?",
            parent=self.root,
        )
        if answer:
            self.start_browser_login()
        else:
            self.set_status(
                "Not logged in. Click 'Log In (browser)' in the right sidebar "
                "when you're ready.")

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------
    def _section_heading(self, parent, title, subtitle=""):
        c = self.colors
        box = tk.Frame(parent, bg=c["BG_TOP"])
        box.pack(fill="x", pady=(0, 22))
        tk.Label(box, text=title, bg=c["BG_TOP"], fg=c["FG"],
                 font=("Segoe UI Semibold", 22), anchor="w").pack(fill="x")
        if subtitle:
            tk.Label(box, text=subtitle, bg=c["BG_TOP"], fg=c["FG_MUTED"],
                     font=("Segoe UI", 10), anchor="w").pack(fill="x", pady=(6, 0))

    def _build_overview_section(self):
        c = self.colors
        frame = tk.Frame(self.content, bg=c["BG_TOP"])
        self._section_frames["overview"] = frame

        self._section_heading(frame, "Overview",
                              "Your profit across every window that matters.")

        # Scrollable, same pattern as Settings/Profile/Maps/Matches/rail:
        # the Daily/Weekly/Monthly tables use expand=True and happily
        # claim the whole window on a typical screen, which left no room
        # for anything packed after them (originally nothing was; the
        # Recent High-Value Items card below is) - without this, that
        # content would just render off the bottom of the window with no
        # way to reach it. The canvas-width binding (not used by Settings'
        # version of this pattern) keeps `inner` exactly as wide as the
        # visible canvas, which the stat-card grid's equal-column
        # stretching below depends on.
        scroll_holder = tk.Frame(frame, bg=c["BG_TOP"])
        scroll_holder.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroll_holder, bg=c["BG_TOP"], highlightthickness=0)
        scroll = ttk.Scrollbar(scroll_holder, orient="vertical",
                               command=canvas.yview)
        inner = tk.Frame(canvas, bg=c["BG_TOP"])
        inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                   lambda e: canvas.itemconfig(inner_win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        bind_mousewheel_to_canvas(canvas)

        grid = tk.Frame(inner, bg=c["BG_TOP"])
        grid.pack(fill="x", padx=(0, 6))

        self.stat_labels = {}
        self.stat_unit_labels = {}
        for i, (key, title) in enumerate([
            ("today", "TODAY"), ("week", "WEEK"),
            ("month", "MONTH"), ("all_time", "ALL-TIME"),
        ]):
            # Padding (16 vs the old 22) and a slightly smaller number
            # (20pt vs 22pt) aren't just cosmetic — at 4 equal columns in
            # this rail width, the old sizing left ~95px of usable text
            # width per card, which silently clipped a 6-digit number
            # ("-4,352" -> "-4,35") and even "THIS WEEK" itself. Shorter
            # labels ("WEEK" not "THIS WEEK") fix the label; the tighter
            # padding/font fix the number.
            card = Card(grid, c, padding=(16, 18), radius=14)
            card.grid(row=0, column=i, padx=(0 if i == 0 else 12, 0),
                      sticky="nsew")
            tk.Label(card.body, text=title, bg=c["SURFACE"],
                     fg=c["FG_MUTED"], font=("Segoe UI", 9, "bold"),
                     anchor="w").pack(fill="x")
            # The number and its unit are separate labels at different
            # sizes, not one string — "-1,234,567 credits" at the big
            # font would silently get clipped by the card's fixed width
            # (Label doesn't wrap or ellipsize when its geometry-manager
            # cell is narrower than the text; it just cuts it off).
            val = tk.Label(card.body, text="—", bg=c["SURFACE"], fg=c["FG"],
                           font=("Segoe UI Semibold", 20), anchor="w")
            val.pack(fill="x", pady=(8, 0))
            unit = tk.Label(card.body, text=core.CURRENCY_LABEL, bg=c["SURFACE"],
                            fg=c["FG_MUTED"], font=("Segoe UI", 9), anchor="w")
            unit.pack(fill="x")
            self.stat_labels[key] = val
            self.stat_unit_labels[key] = unit

            if key == "all_time":
                self.all_time_note = tk.Label(
                    card.body, text="", bg=c["SURFACE"],
                    fg=c["FG_MUTED"], font=("Segoe UI", 8),
                    anchor="w", justify="left")
                self.all_time_note.pack(fill="x", pady=(3, 0))
                # Wrap to whatever width this label actually ends up
                # with (card width varies with the window), instead of
                # a guessed static wraplength that clips on a narrower
                # window or wraps needlessly on a wider one.
                self.all_time_note.bind(
                    "<Configure>",
                    lambda e: e.widget.configure(wraplength=e.width))
            grid.columnconfigure(i, weight=1)

        self.record_label = tk.Label(inner, text="No data loaded yet.",
                                     bg=c["BG_TOP"], fg=c["FG_DIM"],
                                     font=("Segoe UI", 10), anchor="w")
        self.record_label.pack(fill="x", pady=(22, 24), padx=(0, 6))

        tables = tk.Frame(inner, bg=c["BG_TOP"])
        tables.pack(fill="both", expand=True, padx=(0, 6))

        self.summary_trees = {}
        for i, (key, title, kind) in enumerate([
            ("daily", "Daily", "date"),
            ("weekly", "Weekly", "week"),
            ("monthly", "Monthly", "month"),
        ]):
            col = tk.Frame(tables, bg=c["BG_TOP"])
            col.grid(row=0, column=i, sticky="nsew",
                     padx=(0 if i == 0 else 14, 0))
            # uniform=, not just weight=1: weight alone only distributes
            # *extra* space beyond each column's own natural minimum,
            # and Daily's tree (which sits next to the sparkline canvas)
            # was reporting a much larger natural width than Weekly/
            # Monthly's - confirmed by measuring actual rendered widths
            # (343px vs ~130px) during testing, which is exactly the
            # clipping the user reported. uniform forces all three
            # columns to the same actual width regardless of what their
            # content asks for.
            tables.columnconfigure(i, weight=1, uniform="overview_period_cols")
            tables.rowconfigure(0, weight=1)

            tk.Label(col, text=title, bg=c["BG_TOP"], fg=c["FG_DIM"],
                     font=("Segoe UI Semibold", 11), anchor="w").pack(
                fill="x", pady=(0, 10))

            if key == "daily":
                self.spark_canvas = tk.Canvas(
                    col, bg=c["BG_TOP"], height=88,
                    highlightthickness=0, bd=0)
                self.spark_canvas.pack(fill="x", pady=(0, 10))
                self.spark_canvas.bind(
                    "<Configure>",
                    lambda e: self._render_sparkline(
                        getattr(self, "summaries", {}).get("daily", [])))
            else:
                tk.Frame(col, bg=c["BG_TOP"], height=88).pack(
                    fill="x", pady=(0, 10))

            card = Card(col, c, padding=(4, 4), radius=12, shadow=False)
            card.pack(fill="both", expand=True)

            # Just Period + Net, not the fuller Matches/W-L breakdown -
            # three of these sit side by side, so each only ever gets a
            # narrow slice of the page (confirmed by measuring actual
            # rendered widths during testing: ~190-210px each). Four
            # columns doesn't fit that regardless of how fairly the
            # space is divided; two comfortably does, and the detailed
            # per-match breakdown this drops already lives on the
            # Matches tab, not lost information.
            tree = ttk.Treeview(card.body,
                                columns=("period", "net"),
                                show="headings")
            tree.heading("period", text="Period", anchor="w")
            tree.heading("net", text="Net", anchor="e")
            tree.column("period", width=104, anchor="w")
            tree.column("net", width=88, anchor="e")

            if key == "daily":
                # Daily genuinely can outgrow any fixed height for an
                # active player, so it keeps its scrollbar and Tk's
                # default row count.
                tree.pack(fill="both", expand=True, side="left")
                scroll = ttk.Scrollbar(card.body, orient="vertical",
                                       command=tree.yview)
                tree.configure(yscrollcommand=scroll.set)
                scroll.pack(side="right", fill="y")
            else:
                # Weekly/monthly are bounded by the API's own retention
                # window (a rolling quarter's worth of weeks, and even
                # fewer months) - there's essentially never enough real
                # data here to need scrolling, so _refresh_overview()
                # sizes this tree's height to match its actual row count
                # each refresh instead of leaving a mostly-empty
                # scrollbar sitting next to a handful of rows.
                tree.pack(fill="both", expand=True)

            self.summary_trees[key] = (tree, kind)

        # ---- Session ----
        tk.Label(inner, text="Session", bg=c["BG_TOP"], fg=c["FG"],
                 font=("Segoe UI Semibold", 14), anchor="w").pack(
            fill="x", pady=(24, 10), padx=(0, 6))

        session_card = Card(inner, c, padding=(22, 18), radius=14)
        session_card.pack(fill="x", pady=(0, 6), padx=(0, 6))

        # Two mutually-exclusive panels, same pattern as Community's
        # linked/not-linked - toggled by _refresh_session_card() rather
        # than rebuilt each time.
        self._session_inactive = tk.Frame(session_card.body, bg=c["SURFACE"])
        session_intro = tk.Label(
            self._session_inactive,
            text="Track a play session separately from calendar days — "
                 "handy if you play past midnight and don't want profit "
                 "split between \"yesterday\" and \"today\".",
            bg=c["SURFACE"], fg=c["FG_DIM"], font=("Segoe UI", 10),
            anchor="w", justify="left")
        session_intro.pack(fill="x", pady=(0, 12))
        # Dynamic, not a hardcoded wraplength=NNN: this label's actual
        # rendered width depends on the window size and how much the
        # Card/padding/scrollbar eat into it, which doesn't reliably
        # match any single guessed number - a wraplength wider than the
        # real width doesn't re-wrap shorter, it clips text off outright
        # (same fix already used for the ALL-TIME card's subtitle).
        session_intro.bind(
            "<Configure>", lambda e: e.widget.configure(wraplength=e.width))
        self.start_session_btn = self._pill_button(
            self._session_inactive, "Start Session",
            self._start_session, primary=True)
        self.start_session_btn.pack(anchor="w")

        self._session_active_panel = tk.Frame(session_card.body, bg=c["SURFACE"])
        self.session_started_label = tk.Label(
            self._session_active_panel, text="", bg=c["SURFACE"],
            fg=c["FG_MUTED"], font=("Segoe UI", 9), anchor="w")
        self.session_started_label.pack(fill="x")

        session_stats_row = tk.Frame(self._session_active_panel, bg=c["SURFACE"])
        session_stats_row.pack(fill="x", pady=(10, 0))

        session_net_col = tk.Frame(session_stats_row, bg=c["SURFACE"])
        session_net_col.pack(side="left", padx=(0, 32))
        tk.Label(session_net_col, text="NET", bg=c["SURFACE"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x")
        self.session_net_label = tk.Label(
            session_net_col, text="—", bg=c["SURFACE"], fg=c["FG"],
            font=("Segoe UI Semibold", 16), anchor="w")
        self.session_net_label.pack(fill="x")

        session_best_col = tk.Frame(session_stats_row, bg=c["SURFACE"])
        session_best_col.pack(side="left")
        tk.Label(session_best_col, text="BEST RAID", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x")
        self.session_best_label = tk.Label(
            session_best_col, text="—", bg=c["SURFACE"], fg=c["FG"],
            font=("Segoe UI Semibold", 16), anchor="w")
        self.session_best_label.pack(fill="x")

        self.session_matches_label = tk.Label(
            self._session_active_panel, text="", bg=c["SURFACE"],
            fg=c["FG_MUTED"], font=("Segoe UI", 9), anchor="w")
        self.session_matches_label.pack(fill="x", pady=(10, 12))

        session_buttons = tk.Frame(self._session_active_panel, bg=c["SURFACE"])
        session_buttons.pack(anchor="w")

        self.copy_session_btn = self._pill_button(
            session_buttons, "Copy Summary",
            self._copy_session_summary, primary=False)
        self.copy_session_btn.pack(side="left", padx=(0, 8))

        self.end_session_btn = self._pill_button(
            session_buttons, "End Session",
            self._end_session, primary=False)
        self.end_session_btn.pack(side="left")

        self._refresh_session_card()

        # ---- Recent High-Value Items ----
        hvi_head = tk.Frame(inner, bg=c["BG_TOP"])
        hvi_head.pack(fill="x", pady=(24, 10), padx=(0, 6))
        tk.Label(hvi_head, text="Recent High-Value Items", bg=c["BG_TOP"],
                 fg=c["FG"], font=("Segoe UI Semibold", 14),
                 anchor="w").pack(side="left")
        self.hvi_updated_label = tk.Label(
            hvi_head, text="", bg=c["BG_TOP"], fg=c["FG_MUTED"],
            font=("Segoe UI", 9), anchor="e")
        self.hvi_updated_label.pack(side="right")

        hvi_card = Card(inner, c, padding=(16, 12), radius=14, shadow=False)
        hvi_card.pack(fill="x", pady=(0, 24), padx=(0, 6))

        self.hvi_list_frame = tk.Frame(hvi_card.body, bg=c["SURFACE"])
        self.hvi_list_frame.pack(fill="both", expand=True)
        self._render_high_value_items()

        # ---- Weekly Highlights ----
        wh_head = tk.Frame(inner, bg=c["BG_TOP"])
        wh_head.pack(fill="x", pady=(24, 10), padx=(0, 6))
        tk.Label(wh_head, text="Weekly Highlights", bg=c["BG_TOP"], fg=c["FG"],
                 font=("Segoe UI Semibold", 14), anchor="w").pack(side="left")
        self.wh_updated_label = tk.Label(
            wh_head, text="", bg=c["BG_TOP"], fg=c["FG_MUTED"],
            font=("Segoe UI", 9), anchor="e")
        self.wh_updated_label.pack(side="right")

        wh_card = Card(inner, c, padding=(22, 18), radius=14, shadow=False)
        wh_card.pack(fill="x", pady=(0, 24), padx=(0, 6))

        # Empty-state label and the real content are mutually exclusive,
        # same pattern as the Session card's two panels - toggled by
        # _render_weekly_highlights() rather than rebuilt each time.
        self.wh_empty_label = tk.Label(
            wh_card.body, text="", bg=c["SURFACE"], fg=c["FG_MUTED"],
            font=("Segoe UI", 9), anchor="w", justify="left", wraplength=700)

        self.wh_content = tk.Frame(wh_card.body, bg=c["SURFACE"])

        stats_row = tk.Frame(self.wh_content, bg=c["SURFACE"])
        stats_row.pack(fill="x")

        evac_col = tk.Frame(stats_row, bg=c["SURFACE"])
        evac_col.pack(side="left", padx=(0, 32))
        tk.Label(evac_col, text="EVAC RATE", bg=c["SURFACE"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x")
        self.wh_evac_label = tk.Label(
            evac_col, text="—", bg=c["SURFACE"], fg=c["FG"],
            font=("Segoe UI Semibold", 16), anchor="w")
        self.wh_evac_label.pack(fill="x")

        kd_col = tk.Frame(stats_row, bg=c["SURFACE"])
        kd_col.pack(side="left")
        tk.Label(kd_col, text="K/D RATIO", bg=c["SURFACE"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x")
        self.wh_kd_label = tk.Label(
            kd_col, text="—", bg=c["SURFACE"], fg=c["FG"],
            font=("Segoe UI Semibold", 16), anchor="w")
        self.wh_kd_label.pack(fill="x")

        tk.Frame(self.wh_content, bg=c["BORDER_SOFT"], height=1).pack(
            fill="x", pady=(14, 14))

        friends_row = tk.Frame(self.wh_content, bg=c["SURFACE"])
        friends_row.pack(fill="x")

        best_col = tk.Frame(friends_row, bg=c["SURFACE"])
        best_col.pack(side="left", fill="x", expand=True)
        tk.Label(best_col, text="BEST SQUAD-MATE THIS WEEK", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x")
        self.wh_best_friend_label = tk.Label(
            best_col, text="—", bg=c["SURFACE"], fg=c["FG"],
            font=("Segoe UI Semibold", 12), anchor="w")
        self.wh_best_friend_label.pack(fill="x")
        self.wh_best_friend_sub_label = tk.Label(
            best_col, text="", bg=c["SURFACE"], fg=c["FG_MUTED"],
            font=("Segoe UI", 8), anchor="w")
        self.wh_best_friend_sub_label.pack(fill="x")

        worst_col = tk.Frame(friends_row, bg=c["SURFACE"])
        worst_col.pack(side="left", fill="x", expand=True)
        tk.Label(worst_col, text="WORST SQUAD-MATE THIS WEEK", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x")
        self.wh_worst_friend_label = tk.Label(
            worst_col, text="—", bg=c["SURFACE"], fg=c["FG"],
            font=("Segoe UI Semibold", 12), anchor="w")
        self.wh_worst_friend_label.pack(fill="x")
        self.wh_worst_friend_sub_label = tk.Label(
            worst_col, text="", bg=c["SURFACE"], fg=c["FG_MUTED"],
            font=("Segoe UI", 8), anchor="w")
        self.wh_worst_friend_sub_label.pack(fill="x")

        tk.Frame(self.wh_content, bg=c["BORDER_SOFT"], height=1).pack(
            fill="x", pady=(14, 14))

        tk.Label(self.wh_content, text="7-DAY NET WORTH", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x")
        self.weekly_trend_canvas = tk.Canvas(
            self.wh_content, bg=c["SURFACE"], height=64,
            highlightthickness=0, bd=0)
        self.weekly_trend_canvas.pack(fill="x", pady=(4, 0))
        self.weekly_trend_canvas.bind(
            "<Configure>", lambda e: self._render_weekly_trend_chart())

        tk.Frame(self.wh_content, bg=c["BORDER_SOFT"], height=1).pack(
            fill="x", pady=(14, 14))

        tk.Label(self.wh_content, text="MOST VALUABLE THIS WEEK", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x")
        self.wh_top_items_row = tk.Frame(self.wh_content, bg=c["SURFACE"])
        self.wh_top_items_row.pack(fill="x", pady=(6, 0))

        tk.Frame(self.wh_content, bg=c["BORDER_SOFT"], height=1).pack(
            fill="x", pady=(14, 14))

        tk.Label(self.wh_content, text="BEST RAID THIS WEEK", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x")
        self.wh_highlight_value_label = tk.Label(
            self.wh_content, text="—", bg=c["SURFACE"], fg=c["FG"],
            font=("Segoe UI Semibold", 14), anchor="w")
        self.wh_highlight_value_label.pack(fill="x", pady=(2, 6))
        self.wh_highlight_items_row = tk.Frame(self.wh_content, bg=c["SURFACE"])
        self.wh_highlight_items_row.pack(fill="x")

        self._render_weekly_highlights()

    def _render_weekly_highlights(self):
        """Repaints the Weekly Highlights card from self._weekly_highlights
        (core.weekly_highlights() output). Toggles between the empty-state
        label and the real content Frame rather than destroying/rebuilding
        widgets, so the canvas-based trend chart doesn't need to be
        recreated (and re-bound) on every refresh."""
        if not hasattr(self, "wh_content"):
            return
        c = self.colors
        wh = self._weekly_highlights

        if hasattr(self, "wh_updated_label"):
            ts = core.load_cache().get("weekly_report_updated_at", "")
            rel = core.format_relative_time(ts)
            self.wh_updated_label.configure(text=f"Updated {rel}" if rel else "")

        if not wh:
            self.wh_content.pack_forget()
            # have_credentials() is True as soon as ANY endpoint's been
            # captured (see core._resolve_credentials) - the "log in"
            # case below is now genuinely "never logged in at all", not
            # "missed a specific tab".
            msg = ("No weekly report data yet - click \"Refresh Weekly "
                   "Report\" in Settings." if core.have_credentials("weeklyreport")
                   else "Not logged in yet — click \"Log In (browser)\".")
            self.wh_empty_label.configure(text=msg)
            self.wh_empty_label.pack(fill="x")
            return
        self.wh_empty_label.pack_forget()
        self.wh_content.pack(fill="x")

        evac = wh.get("evac_rate")
        self.wh_evac_label.configure(
            text=f"{evac * 100:.0f}%" if evac is not None else "—")

        kd = wh.get("kd_rate")
        self.wh_kd_label.configure(text=f"{kd:.2f}" if kd is not None else "—")

        def _set_friend(label, sub_label, friend):
            if friend:
                label.configure(text=friend["name"], fg=c["FG"])
                sub_label.configure(
                    text=f"{friend['matches']} matches together · "
                         f"{core.fmt_money(friend['value'])}")
            else:
                label.configure(text="No data this week", fg=c["FG_MUTED"])
                sub_label.configure(text="")

        _set_friend(self.wh_best_friend_label, self.wh_best_friend_sub_label,
                   wh.get("best_friend"))
        _set_friend(self.wh_worst_friend_label, self.wh_worst_friend_sub_label,
                   wh.get("worst_friend"))

        self._render_weekly_trend_chart()
        self._render_item_strip(self.wh_top_items_row, wh.get("top_items") or [])
        hv = wh.get("highlight_value", 0)
        self.wh_highlight_value_label.configure(
            text=core.fmt_money(hv) if hv else "No raid highlighted this week")
        self._render_item_strip(self.wh_highlight_items_row, wh.get("highlight_items") or [])

    def _render_item_strip(self, container, items):
        """Up to 3 items side by side with icon + name only (no per-item
        value - unlike Recent High-Value Items, these lists are bare item
        IDs with no per-extraction value or count attached). Shared by
        Weekly Highlights' "Most Valuable This Week" and "Best Raid This
        Week" sections."""
        c = self.colors
        for w in container.winfo_children():
            w.destroy()
        if not items:
            tk.Label(container, text="—", bg=c["SURFACE"], fg=c["FG_MUTED"],
                     font=("Segoe UI", 9), anchor="w").pack(anchor="w")
            return
        for it in items:
            col = tk.Frame(container, bg=c["SURFACE"])
            col.pack(side="left", padx=(0, 18))
            icon_label = tk.Label(col, bg=c["SURFACE"])
            icon_label.pack()
            photo = self._item_icon_cache.get(it["item_id"])
            if photo:
                icon_label.configure(image=photo)
                icon_label.image = photo
            else:
                icon_label.configure(width=6, height=2)
                self._queue_item_icon(it["item_id"], it["image_url"])
            tk.Label(col, text=it["name"], bg=c["SURFACE"], fg=c["FG"],
                     font=("Segoe UI", 9), anchor="w", wraplength=110,
                     justify="left").pack()

    def _render_weekly_trend_chart(self):
        """7-day total stash value trend, from core.weekly_highlights()'s
        'trend' list. Min/max-scaled, unlike the Daily card's zero-
        baseline bar chart - these are large, always-positive wealth
        totals with small day-to-day variation, so a zero baseline would
        make every bar look nearly full; scaling to the week's own
        min/max actually shows the shape of the trend."""
        cvs = getattr(self, "weekly_trend_canvas", None)
        if cvs is None:
            return
        try:
            cvs.delete("all")
        except tk.TclError:
            return

        c = self.colors
        w = cvs.winfo_width()
        h = cvs.winfo_height()
        if w <= 4 or h <= 4:
            return

        trend = (self._weekly_highlights or {}).get("trend") or []
        if not trend:
            cvs.create_text(w / 2, h / 2, text="No data yet",
                            fill=c["FG_MUTED"], font=("Segoe UI", 9))
            return

        values = [p["value"] for p in trend]
        n = len(values)
        vmin, vmax = min(values), max(values)
        span = (vmax - vmin) or 1.0

        # Cap the plotted width and center it, rather than stretching to
        # fill the card's full width - on a wide window this card can be
        # 800px+, and letting 7 bars spread across all of that just
        # makes them thin, far apart, and hard to read as a shape (the
        # actual complaint this fixes). A fixed cap keeps the chart's
        # proportions - and the Low/High labels' legibility - consistent
        # regardless of how wide the surrounding window is.
        MAX_PLOT_W = 420
        plot_w = min(w - 12, MAX_PLOT_W)
        pad_l = (w - plot_w) / 2
        pad_top, pad_bottom = 20, 6
        plot_top = pad_top
        plot_bottom = h - pad_bottom
        plot_h = plot_bottom - plot_top

        def y_for(v):
            return plot_bottom - ((v - vmin) / span) * plot_h

        slot_w = plot_w / n
        bar_w = max(4.0, min(slot_w * 0.55, 20))

        for i, v in enumerate(values):
            cx = pad_l + slot_w * (i + 0.5)
            y = y_for(v)
            is_today = (i == n - 1)
            color = c["ACCENT"] if is_today else c["POSITIVE"]
            top = min(y, plot_bottom - 1.5)  # a flat/all-equal week still shows a sliver
            round_rect(cvs, cx - bar_w / 2, top, cx + bar_w / 2, plot_bottom,
                      min(3, bar_w / 2), fill=color, outline="")

        cvs.create_line(pad_l, plot_bottom, pad_l + plot_w, plot_bottom,
                        fill=c["BORDER_SOFT"])
        # Bumped from 7pt to 9pt, and anchored to the plot area's own
        # edges (not the full canvas) so they stay close to the bars
        # they describe instead of drifting into empty margin on a wide
        # card.
        cvs.create_text(pad_l, 2, text=f"Low {core.fmt_money(vmin)}",
                        fill=c["FG_MUTED"], font=("Segoe UI", 9), anchor="nw")
        cvs.create_text(pad_l + plot_w, 2, text=f"High {core.fmt_money(vmax)}",
                        fill=c["FG_MUTED"], font=("Segoe UI", 9), anchor="ne")

    def _render_high_value_items(self):
        """Repaints the Recent High-Value Items card from
        self._high_value_items (core.recent_high_value_items() output).
        Rebuilds the row widgets from scratch each call - the list is
        short (top ~12 items) so this is cheap, and it keeps this in
        sync with icons arriving asynchronously without separate
        per-row update plumbing."""
        if not hasattr(self, "hvi_list_frame"):
            return
        c = self.colors
        for w in self.hvi_list_frame.winfo_children():
            w.destroy()

        if hasattr(self, "hvi_updated_label"):
            ts = core.load_cache().get("asset_calendar_updated_at", "")
            rel = core.format_relative_time(ts)
            self.hvi_updated_label.configure(text=f"Updated {rel}" if rel else "")

        items = self._high_value_items
        if not items:
            msg = ("No high-value items captured yet." if core.have_credentials("assetcalendar")
                   else "Not logged in yet — click \"Log In (browser)\".")
            tk.Label(self.hvi_list_frame, text=msg, bg=c["SURFACE"],
                     fg=c["FG_MUTED"], font=("Segoe UI", 9), anchor="w",
                     justify="left", wraplength=760).pack(fill="x")
            return

        grade_colors = {"6": c["ACCENT"], "5": c["POSITIVE"]}

        for i, it in enumerate(items):
            row = tk.Frame(self.hvi_list_frame, bg=c["SURFACE"])
            row.pack(fill="x", pady=(0 if i == 0 else 8, 0))
            if i > 0:
                tk.Frame(self.hvi_list_frame, bg=c["BORDER_SOFT"],
                        height=1).pack(fill="x", pady=(8, 0), before=row)

            icon_label = tk.Label(row, bg=c["SURFACE"])
            icon_label.pack(side="left", padx=(0, 12), pady=4)
            photo = self._item_icon_cache.get(it["item_id"])
            if photo:
                icon_label.configure(image=photo)
                icon_label.image = photo
            else:
                icon_label.configure(width=6, height=2)
                self._queue_item_icon(it["item_id"], it["image_url"])

            val_label = tk.Label(row, text=core.fmt_money(it["total_value"]),
                                 bg=c["SURFACE"], fg=c["POSITIVE"],
                                 font=("Segoe UI Semibold", 11), anchor="e")
            val_label.pack(side="right")

            name_col = tk.Frame(row, bg=c["SURFACE"])
            name_col.pack(side="left", fill="both", expand=True)
            name_text = it["name"] + (f"  ×{it['count']}" if it["count"] > 1 else "")
            tk.Label(name_col, text=name_text, bg=c["SURFACE"], fg=c["FG"],
                     font=("Segoe UI", 10), anchor="w").pack(fill="x", anchor="w")
            grade = it.get("grade")
            if grade:
                tk.Label(name_col, text=f"Grade {grade}", bg=c["SURFACE"],
                         fg=grade_colors.get(str(grade), c["FG_MUTED"]),
                         font=("Segoe UI", 8), anchor="w").pack(fill="x", anchor="w")

    # ------------------------------------------------------------------
    # Session tracking
    # ------------------------------------------------------------------
    def _session_tick(self):
        # Keeps the "Xh Ym ago" elapsed display current even if no new
        # match data comes in for a while - purely cosmetic (doesn't
        # touch stats), so only bother repainting when a session is
        # actually active and someone's looking at the Overview tab.
        if self.session_start is not None and self.active_section == "overview":
            self._refresh_session_card()
        self.root.after(60000, self._session_tick)

    def _start_session(self):
        self.session_start = datetime.now()
        _save_settings({
            "session_active": True,
            "session_start": self.session_start.isoformat(),
        })
        self._refresh_session_card()
        self._refresh_overlay_if_visible()
        self.set_status("Session started.")

    def _end_session(self):
        self.session_start = None
        _save_settings({"session_active": False, "session_start": None})
        self._refresh_session_card()
        self._refresh_overlay_if_visible()
        self.set_status("Session ended.")

    def _copy_session_summary(self):
        """Formats the CURRENT session's stats fresh (not whatever was
        last rendered on the card, which could be up to a minute stale
        thanks to _session_tick's update interval) and copies them as
        plain text, for pasting into Discord or wherever - the app
        already has a social angle (community leaderboard, best/worst
        squad-mate this week), so sharing a quick result is a natural
        fit rather than a stretch."""
        if self.session_start is None:
            return
        stats = core.session_stats(self.rows, self.session_start)
        started_str = self.session_start.strftime("%I:%M %p").lstrip("0")
        elapsed = datetime.now() - self.session_start
        hours, rem = divmod(int(elapsed.total_seconds()), 3600)
        minutes = rem // 60

        lines = [
            "Delta Force session summary",
            f"Started {started_str} ({hours}h {minutes}m)",
            f"Net: {core.fmt_money(stats['net_income'])}",
        ]
        best = stats["best_match"]
        if best:
            lines.append(f"Best raid: {core.fmt_money(best['net_income'])} "
                         f"· {best['map_name']}")
        lines.append(f"{stats['matches']} matches — "
                     f"{stats['wins']}W-{stats['losses']}L "
                     f"({stats['win_rate']:.1f}% win rate)")
        text = "\n".join(lines)

        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            # Tk defers clipboard ownership until the next idle cycle;
            # without this, quitting or losing focus right after copying
            # can drop the clipboard contents before another app reads
            # them.
            self.root.update_idletasks()
            self._show_toast("Copied", "Session summary copied to clipboard.")
        except tk.TclError:
            self.set_status("Couldn't copy to clipboard.")

    def _refresh_session_card(self):
        if not hasattr(self, "_session_inactive"):
            return
        c = self.colors

        if self.session_start is None:
            self._session_active_panel.pack_forget()
            self._session_inactive.pack(fill="x")
            return
        self._session_inactive.pack_forget()
        self._session_active_panel.pack(fill="x")

        started = self.session_start
        elapsed = datetime.now() - started
        hours, rem = divmod(int(elapsed.total_seconds()), 3600)
        minutes = rem // 60
        # %-I (no leading zero on the hour) is a glibc/BSD strftime
        # extension - it doesn't exist on Windows' C runtime and raises
        # exactly "ValueError: Invalid format string" there. %I always
        # gives a leading zero instead (01-12); .lstrip("0") drops it -
        # safe here specifically because %I never produces "00" the way
        # %H can, so there's nothing else a leading-zero-strip could
        # accidentally eat.
        started_str = started.strftime("%I:%M %p").lstrip("0")
        self.session_started_label.configure(
            text=f"Started {started_str} ({hours}h {minutes}m ago)")

        stats = core.session_stats(self.rows, self.session_start)
        net = stats["net_income"]
        self.session_net_label.configure(
            text=core.fmt_money(net),
            fg=c["POSITIVE"] if net >= 0 else c["NEGATIVE"])

        best = stats["best_match"]
        if best:
            self.session_best_label.configure(
                text=f"{core.fmt_money(best['net_income'])} · {best['map_name']}",
                fg=c["POSITIVE"] if best["net_income"] >= 0 else c["NEGATIVE"])
        else:
            # "0 credits", not a bare dash - matches NET's zero-state
            # instead of looking like the field is broken/empty, and the
            # matches line below makes clear this is "started, nothing
            # played yet" rather than actually having no data.
            self.session_best_label.configure(text="0 credits", fg=c["FG"])

        if stats["matches"] == 0:
            self.session_matches_label.configure(
                text="Tracking started — stats will fill in after your next match.")
        else:
            self.session_matches_label.configure(
                text=f"{stats['matches']} matches — "
                     f"{stats['wins']}W-{stats['losses']}L "
                     f"({stats['win_rate']:.1f}% win rate)")

    def _build_profile_section(self):
        c = self.colors
        frame = tk.Frame(self.content, bg=c["BG_TOP"])
        self._section_frames["profile"] = frame

        self._section_heading(frame, "Profile",
                              "Pulled live from GetMyData — same numbers the game shows.")

        scroll_holder = tk.Frame(frame, bg=c["BG_TOP"])
        scroll_holder.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroll_holder, bg=c["BG_TOP"], highlightthickness=0)
        scroll = ttk.Scrollbar(scroll_holder, orient="vertical", command=canvas.yview)
        self.profile_inner = tk.Frame(canvas, bg=c["BG_TOP"])
        self.profile_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.profile_inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        # Pack the scrollbar before the canvas: with only the canvas
        # using expand=True, packing it first claims the *entire*
        # remaining cavity immediately, leaving the scrollbar a 0-width
        # sliver (present, but invisible and useless). Scrollbar-first
        # gives it its real width, and the canvas still expands to fill
        # whatever's left.
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        bind_mousewheel_to_canvas(canvas)

    def _build_maps_section(self):
        c = self.colors
        frame = tk.Frame(self.content, bg=c["BG_TOP"])
        self._section_frames["maps"] = frame

        self._section_heading(frame, "Maps",
                              "Where your profit is really coming from. "
                              "Click a column header to sort.")

        card = Card(frame, c, padding=(4, 4), radius=14, shadow=False)
        card.pack(fill="both", expand=True)

        map_columns = [
            ("map", "Map"), ("matches", "Matches"),
            ("wl", "W-L"), ("net", "Net Income"),
        ]

        self.maps_tree = ttk.Treeview(
            card.body, columns=tuple(cid for cid, _ in map_columns),
            show="headings")

        for col, _ in map_columns:
            anchor = "e" if col == "net" else (
                "center" if col in ("matches", "wl") else "w")
            self.maps_tree.heading(col, text="", anchor=anchor)
            self.maps_tree.column(col, anchor=anchor)
        self.maps_tree.column("map", width=280, anchor="w")
        self.maps_tree.column("matches", width=100, anchor="center")
        self.maps_tree.column("wl", width=100, anchor="center")
        self.maps_tree.column("net", width=180, anchor="e")

        make_sortable(self.maps_tree, map_columns, self._on_maps_sort,
                      initial_col=self.maps_sort_key,
                      initial_reverse=self.maps_sort_reverse)

        self.maps_tree.pack(fill="both", expand=True, side="left")
        scroll = ttk.Scrollbar(card.body, orient="vertical",
                                command=self.maps_tree.yview)
        self.maps_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

    def _build_matches_section(self):
        c = self.colors
        frame = tk.Frame(self.content, bg=c["BG_TOP"])
        self._section_frames["matches"] = frame

        head_row = tk.Frame(frame, bg=c["BG_TOP"])
        head_row.pack(fill="x")
        self._section_heading(head_row, "Matches",
                              "Double-click any row for the full squad breakdown. "
                              "Click a column header to sort.")

        search_card = Card(frame, c, padding=(16, 10), radius=12, shadow=False)
        search_card.pack(fill="x", pady=(0, 18))
        tk.Label(search_card.body, text="⌕", bg=c["SURFACE"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 13)).pack(side="left", padx=(0, 10))
        self.search_entry = tk.Entry(
            search_card.body, textvariable=self.search_var, bd=0,
            bg=c["SURFACE"], fg=c["FG"], insertbackground=c["FG"],
            font=("Segoe UI", 10), highlightthickness=0)
        self.search_entry.pack(side="left", fill="x", expand=True)

        card = Card(frame, c, padding=(4, 4), radius=14, shadow=False)
        card.pack(fill="both", expand=True)

        match_columns = [
            ("date", "Date / Time"), ("result", "Result"),
            ("kills", "Kills"), ("map", "Map"),
            ("operator", "Operator"), ("rank", "Rank"),
            ("net", "Net Income"),
        ]
        widths = {"date": 160, "result": 80, "kills": 90, "map": 220,
                  "operator": 130, "rank": 120, "net": 160}
        anchors = {"date": "w", "result": "center", "kills": "center",
                   "map": "w", "operator": "w", "rank": "w", "net": "e"}

        self.matches_tree = ttk.Treeview(
            card.body, columns=tuple(cid for cid, _ in match_columns),
            show="tree headings")
        self.matches_tree.heading("#0", text="", anchor="center")
        self.matches_tree.column("#0", width=44, minwidth=44, stretch=False,
                                  anchor="center")

        for col, _ in match_columns:
            self.matches_tree.heading(col, text="", anchor=anchors[col])
            self.matches_tree.column(col, width=widths[col], anchor=anchors[col])

        make_sortable(self.matches_tree, match_columns, self._on_matches_sort,
                      initial_col=self.matches_sort_key,
                      initial_reverse=self.matches_sort_reverse)

        self.matches_tree.pack(fill="both", expand=True, side="left")
        scroll = ttk.Scrollbar(card.body, orient="vertical",
                                command=self.matches_tree.yview)
        self.matches_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        self.matches_tree.bind("<Double-1>", self._on_match_double_click)

        pager = tk.Frame(frame, bg=c["BG_TOP"])
        pager.pack(fill="x", pady=(12, 0))

        self.prev_btn = tk.Label(pager, text="◀ Prev", bg=c["SURFACE_ALT"],
                                 fg=c["FG"], font=("Segoe UI Semibold", 9),
                                 padx=14, pady=6, cursor="hand2")
        self.prev_btn.pack(side="left")
        self.prev_btn.bind("<Button-1>", lambda e: self._matches_page(-1))

        self.page_label = tk.Label(pager, text="Page 1 of 1",
                                   bg=c["BG_TOP"], fg=c["FG_DIM"],
                                   font=("Segoe UI", 10))
        self.page_label.pack(side="left", padx=16)

        self.next_btn = tk.Label(pager, text="Next ▶", bg=c["SURFACE_ALT"],
                                 fg=c["FG"], font=("Segoe UI Semibold", 9),
                                 padx=14, pady=6, cursor="hand2")
        self.next_btn.pack(side="left")
        self.next_btn.bind("<Button-1>", lambda e: self._matches_page(+1))

        tk.Label(pager, text="Per page:", bg=c["BG_TOP"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 9)).pack(side="right", padx=(0, 6))
        self.page_size_var = tk.StringVar(value=str(self.matches_page_size))
        size_combo = ttk.Combobox(pager, textvariable=self.page_size_var,
                                  values=("50", "100", "250", "All"),
                                  state="readonly", width=6)
        size_combo.bind("<<ComboboxSelected>>",
                        lambda e: self._on_page_size_changed())
        size_combo.pack(side="right")

        self.match_count_label = tk.Label(pager, text="", bg=c["BG_TOP"],
                                          fg=c["FG_MUTED"],
                                          font=("Segoe UI", 9))
        self.match_count_label.pack(side="right", padx=(0, 16))

        self.prev_btn._enabled = False
        self.next_btn._enabled = False

    # ------------------------------------------------------------------
    # Community section
    # ------------------------------------------------------------------
    def _build_community_section(self):
        c = self.colors
        frame = tk.Frame(self.content, bg=c["BG_TOP"])
        self._section_frames["community"] = frame

        self._section_heading(
            frame, "Community",
            "See how your stats stack up against other opted-in players.")

        # Scrollable, matching every other section: the leaderboard table
        # below has its own expand=True and native Treeview scrolling,
        # but without this outer wrapper the status card + leaderboard
        # together could still overflow a shorter window with nothing
        # able to reach the bottom - the same failure mode the Overview
        # tab had before it got this same fix.
        scroll_holder = tk.Frame(frame, bg=c["BG_TOP"])
        scroll_holder.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroll_holder, bg=c["BG_TOP"], highlightthickness=0)
        scroll = ttk.Scrollbar(scroll_holder, orient="vertical",
                               command=canvas.yview)
        inner = tk.Frame(canvas, bg=c["BG_TOP"])
        inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                   lambda e: canvas.itemconfig(inner_win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        bind_mousewheel_to_canvas(canvas)

        # ---- Account status card ----
        status_card = Card(inner, c, padding=(22, 18), radius=14)
        status_card.pack(fill="x", pady=(0, 16))

        # Two mutually-exclusive panels (not linked / linked), toggled by
        # _refresh_community_status_ui() rather than rebuilt each time.
        self._community_not_linked = tk.Frame(status_card.body, bg=c["SURFACE"])
        _join_label = tk.Label(
            self._community_not_linked,
            text="Join using your Delta Force login — no separate "
                 "account needed. Shares only the aggregate stats "
                 "already on your Overview tab, under your in-game "
                 "nickname. Never your DfTools login or match history.",
            bg=c["SURFACE"], fg=c["FG_DIM"], font=("Segoe UI", 10),
            anchor="w", justify="left", wraplength=700)
        _join_label.pack(fill="x", pady=(0, 12))
        # Force this label's wrapped layout to fully resolve before the
        # canvas-based button below is created - packing them back to
        # back without a settle point occasionally produced visibly
        # corrupted (chunks-of-text-dropped) wrapped text under the
        # X11/Xvfb environment used to test this build; harmless either
        # way, so leaving it in as cheap insurance.
        self.root.update_idletasks()
        self.community_link_btn = self._pill_button(
            self._community_not_linked, "Join Leaderboard",
            self.start_community_join, primary=True)
        self.community_link_btn.pack(anchor="w")

        self._community_linked = tk.Frame(status_card.body, bg=c["SURFACE"])
        self.community_name_label = tk.Label(
            self._community_linked, text="—", bg=c["SURFACE"], fg=c["FG"],
            font=("Segoe UI Semibold", 13), anchor="w")
        self.community_name_label.pack(fill="x")

        opt_row = tk.Frame(self._community_linked, bg=c["SURFACE"])
        opt_row.pack(fill="x", pady=(10, 0))
        self.community_optin_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            opt_row, text="Show my stats on the public leaderboard",
            variable=self.community_optin_var,
            command=self._on_community_optin_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"], highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w",
        ).pack(side="left")

        auto_row = tk.Frame(self._community_linked, bg=c["SURFACE"])
        auto_row.pack(fill="x", pady=(4, 0))
        self.community_autosync_var = tk.BooleanVar(
            value=self.settings.get("community_auto_sync", True))
        tk.Checkbutton(
            auto_row, text="Keep my stats up to date automatically "
                           "(after each match refresh)",
            variable=self.community_autosync_var,
            command=self._on_community_autosync_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"], highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w",
        ).pack(side="left")

        btn_row = tk.Frame(self._community_linked, bg=c["SURFACE"])
        btn_row.pack(fill="x", pady=(14, 0))
        self.community_sync_btn = self._pill_button(
            btn_row, "Sync My Stats Now", self.start_community_sync,
            primary=True)
        self.community_sync_btn.pack(side="left", padx=(0, 8))
        self.community_unlink_btn = self._pill_button(
            btn_row, "Leave Leaderboard", self.start_community_unlink,
            primary=False)
        self.community_unlink_btn.pack(side="left")

        self.community_status_label = tk.Label(
            status_card.body, text="", bg=c["SURFACE"], fg=c["FG_MUTED"],
            font=("Segoe UI", 9), anchor="w", wraplength=740, justify="left")
        # Not packed here - _refresh_community_status_ui() (called at the
        # end of this method) owns packing order for the whole card, so
        # this label always ends up below whichever content frame is
        # showing instead of racing it.

        # ---- Leaderboard ----
        board_head = tk.Frame(inner, bg=c["BG_TOP"])
        board_head.pack(fill="x", pady=(0, 10))
        self.community_board_title = tk.Label(
            board_head, text="Leaderboard", bg=c["BG_TOP"], fg=c["FG"],
            font=("Segoe UI Semibold", 14))
        self.community_board_title.pack(side="left")
        self.community_refresh_btn = self._pill_button(
            board_head, "Refresh", self.start_community_leaderboard_fetch,
            primary=False)
        self.community_refresh_btn.pack(side="right")
        self.community_period_var = tk.StringVar(value="All-Time")
        period_combo = ttk.Combobox(
            board_head, textvariable=self.community_period_var,
            values=["All-Time", "Today"], state="readonly", width=9)
        period_combo.pack(side="right", padx=(0, 10))
        period_combo.bind("<<ComboboxSelected>>",
                          lambda e: self._on_community_period_change())
        self.community_period_note = tk.Label(
            inner, text="", bg=c["BG_TOP"], fg=c["FG_MUTED"],
            font=("Segoe UI", 9), anchor="w", justify="left")
        self.community_period_note.bind(
            "<Configure>", lambda e: e.widget.configure(wraplength=e.width))

        board_card = Card(inner, c, padding=(4, 4), radius=14, shadow=False)
        board_card.pack(fill="both", expand=True)
        self.community_board_card = board_card  # the daily note packs just above it

        columns = [
            ("rank", "#"), ("player", "Player"), ("net", "Net Income"),
            ("matches", "Matches"), ("wl", "W-L"), ("winrate", "Win Rate"),
            ("map", "Best Map"), ("operator", "Best Operator"),
        ]
        # These add up to ~600px: the space the table actually gets at
        # normal window sizes (the previous set added up to ~750px, so
        # headers and the "credits" suffix were being cut off). stretch=True
        # below lets them grow into a wider window instead.
        widths = {"rank": 28, "player": 96, "net": 104, "matches": 66,
                  "wl": 46, "winrate": 66, "map": 84, "operator": 100}
        anchors = {"rank": "center", "player": "w", "net": "e",
                   "matches": "center", "wl": "center", "winrate": "center",
                   "map": "w", "operator": "w"}

        self.community_tree = ttk.Treeview(
            board_card.body, columns=tuple(cid for cid, _ in columns),
            show="headings")
        for cid, _ in columns:
            self.community_tree.heading(cid, text="", anchor=anchors[cid])
            self.community_tree.column(cid, width=widths[cid],
                                       anchor=anchors[cid], stretch=True)
        make_sortable(self.community_tree, columns, self._on_community_sort,
                      initial_col="net", initial_reverse=True)
        self.community_tree.pack(fill="both", expand=True, side="left")

        board_scroll = ttk.Scrollbar(board_card.body, orient="vertical",
                                     command=self.community_tree.yview)
        self.community_tree.configure(yscrollcommand=board_scroll.set)
        board_scroll.pack(side="right", fill="y")

        self._refresh_community_status_ui()

    def _refresh_community_status_ui(self):
        if not hasattr(self, "_community_not_linked"):
            return

        # Always start from a clean slate and re-pack in the right order -
        # otherwise re-showing a frame after pack_forget() appends it to
        # the END of the pack order, landing it below (or above) widgets
        # it shouldn't, depending on what else got (re)packed since.
        self._community_not_linked.pack_forget()
        self._community_linked.pack_forget()
        self.community_status_label.pack_forget()

        if not comm.SERVER_URL:
            self.community_link_btn.set_state("disabled")
            self.community_status_label.configure(
                text="This build isn't pointed at a community server yet "
                     "— see server/README.md.")
            self.community_status_label.pack(fill="x")
            return

        if comm.is_linked():
            self._community_linked.pack(fill="x")
            account = comm.load_account() or {}
            self.community_name_label.configure(
                text=f"Linked as {account.get('display_name', 'Player')}")
            status = self._community_status or {}
            self.community_optin_var.set(bool(status.get("opted_in", False)))
        else:
            self.community_link_btn.set_state(
                "disabled" if self._community_busy else "normal")
            self._community_not_linked.pack(fill="x")

        self.community_status_label.configure(text="")
        self.community_status_label.pack(fill="x", pady=(12, 0))

    def _on_community_sort(self, col, reverse):
        self.community_sort_key = col
        self.community_sort_reverse = reverse
        self._render_community_leaderboard()

    def _render_community_leaderboard(self):
        if not hasattr(self, "community_tree"):
            return
        tree = self.community_tree
        for iid in tree.get_children():
            tree.delete(iid)

        def row_net(p):
            # Daily rows carry net_income; all-time rows net_income_all_time.
            return p.get("net_income", p.get("net_income_all_time", 0))

        daily = self.community_period == "daily"
        self.community_board_title.configure(
            text="Today's Leaderboard" if daily else "Leaderboard")
        if daily:
            meta = self._community_meta or {}
            resets_at = meta.get("resets_at")
            left = self._format_time_until(resets_at)
            when = self._format_local_clock(resets_at)
            note = "Ranked on today's matches only - one shared day for everyone."
            if when:
                note += f" Resets at {when} your time" + (f" (in {left})." if left else ".")
            personal = self._daily_status_line(self._community_leaderboard, meta)
            if personal:
                note += "\n" + personal
            self.community_period_note.configure(text=note)
            self.community_period_note.pack(fill="x", pady=(0, 8),
                                            before=self.community_board_card)
        else:
            self.community_period_note.pack_forget()

        key_fns = {
            "player": lambda p: (p.get("display_name") or "").lower(),
            "net": row_net,
            "matches": lambda p: p.get("matches", 0),
            "wl": lambda p: p.get("wins", 0) - p.get("losses", 0),
            "winrate": lambda p: p.get("win_rate", 0),
            "map": lambda p: (p.get("best_map") or "").lower(),
            "operator": lambda p: (p.get("best_operator") or "").lower(),
        }
        keyfn = key_fns.get(self.community_sort_key, key_fns["net"])
        rows = sorted(self._community_leaderboard, key=keyfn,
                      reverse=self.community_sort_reverse)

        for i, p in enumerate(rows, start=1):
            tree.insert("", "end", values=(
                i,
                p.get("display_name") or "—",
                f"{int(row_net(p)):,}",  # bare number: "credits" on every row just clipped
                p.get("matches", 0),
                f"{p.get('wins', 0)}-{p.get('losses', 0)}",
                f"{p.get('win_rate', 0):.1f}%",
                p.get("best_map") or "—",
                p.get("best_operator") or "—",
            ))

    # ------------------------------------------------------------------
    # Settings section
    # ------------------------------------------------------------------
    def _build_settings_section(self):
        c = self.colors
        frame = tk.Frame(self.content, bg=c["BG_TOP"])
        self._section_frames["settings"] = frame

        self._section_heading(frame, "Settings",
                              "Tune auto-refresh, notifications, and data.")

        scroll_holder = tk.Frame(frame, bg=c["BG_TOP"])
        scroll_holder.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroll_holder, bg=c["BG_TOP"], highlightthickness=0)
        scroll = ttk.Scrollbar(scroll_holder, orient="vertical",
                                command=canvas.yview)
        self.settings_inner = tk.Frame(canvas, bg=c["BG_TOP"])
        self.settings_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        # Width-bound to the canvas like Overview/Community: without this
        # the inner frame kept its own natural width, wider than the
        # visible area at ordinary window sizes, so cards ran off the
        # right edge and wrapped text was cut off instead of wrapping.
        settings_win = canvas.create_window((0, 0), window=self.settings_inner, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(settings_win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        # Pack the scrollbar before the canvas: with only the canvas
        # using expand=True, packing it first claims the *entire*
        # remaining cavity immediately, leaving the scrollbar a 0-width
        # sliver (present, but invisible and useless). Scrollbar-first
        # gives it its real width, and the canvas still expands to fill
        # whatever's left.
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        bind_mousewheel_to_canvas(canvas)

        def section_title(text, hint=""):
            tk.Label(self.settings_inner, text=text.upper(),
                     bg=c["BG_TOP"], fg=c["FG_MUTED"],
                     font=("Segoe UI", 9, "bold"),
                     anchor="w").pack(fill="x", pady=(18, 6))
            if hint:
                tk.Label(self.settings_inner, text=hint,
                         bg=c["BG_TOP"], fg=c["FG_MUTED"],
                         font=("Segoe UI", 8), anchor="w",
                         wraplength=760, justify="left").pack(fill="x", pady=(0, 8))

        # ---- Auto-refresh ----
        section_title("Auto-Refresh",
                      "Automatically fetch new matches on an interval so the "
                      "Discord presence and match history stay current.")

        auto_card = Card(self.settings_inner, c, padding=(22, 18), radius=12)
        auto_card.pack(fill="x", pady=(0, 6))

        row = tk.Frame(auto_card.body, bg=c["SURFACE"])
        row.pack(fill="x", pady=4)
        self.auto_refresh_var = tk.BooleanVar(
            value=self.settings.get("auto_refresh_enabled", True))
        tk.Checkbutton(
            row, text="Enable automatic refresh",
            variable=self.auto_refresh_var,
            command=self._on_auto_refresh_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"],
            highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w",
        ).pack(side="left")

        row2 = tk.Frame(auto_card.body, bg=c["SURFACE"])
        row2.pack(fill="x", pady=(10, 4))
        tk.Label(row2, text="Interval", bg=c["SURFACE"], fg=c["FG_DIM"],
                 font=("Segoe UI", 10)).pack(side="left", padx=(0, 12))
        self.auto_refresh_combo_var = tk.StringVar(
            value=str(self.settings.get("auto_refresh_minutes",
                                         DEFAULT_AUTO_REFRESH_MINUTES)))
        combo = ttk.Combobox(
            row2, textvariable=self.auto_refresh_combo_var,
            values=[str(n) for n in AUTO_REFRESH_OPTIONS],
            state="readonly", width=6)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>",
                   lambda e: self._on_auto_refresh_interval_change())
        tk.Label(row2, text="minutes", bg=c["SURFACE"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))

        self.auto_status_label = tk.Label(
            auto_card.body, text="", bg=c["SURFACE"], fg=c["FG_MUTED"],
            font=("Segoe UI", 9), anchor="w", justify="left",
            wraplength=760)
        self.auto_status_label.pack(fill="x", pady=(10, 0))

        btn_row = tk.Frame(auto_card.body, bg=c["SURFACE"])
        btn_row.pack(fill="x", pady=(12, 0))
        refresh_now = tk.Label(btn_row, text="Refresh now",
                               bg=c["SURFACE_ALT"], fg=c["FG"],
                               font=("Segoe UI Semibold", 9),
                               padx=14, pady=7, cursor="hand2")
        refresh_now.pack(side="left")
        refresh_now.bind("<Button-1>",
                         lambda e: self.start_quick_refresh())

        # ---- Notifications ----
        section_title("Notifications",
                      "Get a small popup when auto-refresh finds new matches.")

        notif_card = Card(self.settings_inner, c, padding=(22, 16), radius=12)
        notif_card.pack(fill="x", pady=(0, 6))
        self.notify_var = tk.BooleanVar(
            value=self.settings.get("auto_refresh_notify", False))
        tk.Checkbutton(
            notif_card.body,
            text="Show a toast when new matches are fetched",
            variable=self.notify_var,
            command=self._on_notify_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"],
            highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w",
        ).pack(anchor="w")

        # ---- Discord ----
        section_title("Discord Rich Presence",
                      "Show your today's profit / W-L on your Discord profile.")

        discord_card = Card(self.settings_inner, c, padding=(22, 16), radius=12)
        discord_card.pack(fill="x", pady=(0, 6))
        self.settings_rpc_var = tk.BooleanVar(
            value=self.settings.get("discord_rpc", False))
        tk.Checkbutton(
            discord_card.body,
            text="Enable Discord Rich Presence",
            variable=self.settings_rpc_var,
            command=self._on_settings_rpc_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"],
            highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w",
        ).pack(anchor="w")
        tk.Label(discord_card.body,
                 text="Requires pypresence and DISCORD_CLIENT_ID in "
                      "delta_force_rpc.py. The toggle in the right rail stays "
                      "in sync with this one.",
                 bg=c["SURFACE"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=740).pack(fill="x", pady=(8, 0))

        # ---- Overlay ----
        section_title("Overlay",
                      "A quick-glance HUD (today's profit/loss, best "
                      "raid, top loot) toggled by a keyboard shortcut "
                      "that works even while the game has focus — so "
                      "you don't have to alt-tab to check. Windows only.")

        overlay_card = Card(self.settings_inner, c, padding=(22, 16), radius=12)
        overlay_card.pack(fill="x", pady=(0, 6))

        self.settings_overlay_var = tk.BooleanVar(
            value=self.settings.get("overlay_enabled", False))
        tk.Checkbutton(
            overlay_card.body,
            text="Enable overlay hotkey",
            variable=self.settings_overlay_var,
            command=self._on_overlay_enabled_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"],
            highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w",
        ).pack(anchor="w")

        hotkey_row = tk.Frame(overlay_card.body, bg=c["SURFACE"])
        hotkey_row.pack(fill="x", pady=(10, 0))
        tk.Label(hotkey_row, text="Shortcut", bg=c["SURFACE"], fg=c["FG_DIM"],
                 font=("Segoe UI", 10)).pack(side="left", padx=(0, 12))
        self.overlay_hotkey_var = tk.StringVar(
            value=self.settings.get("overlay_hotkey",
                                    overlay_mod.DEFAULT_HOTKEY_LABEL))
        hotkey_combo = ttk.Combobox(
            hotkey_row, textvariable=self.overlay_hotkey_var,
            values=[label for label, _, _ in overlay_mod.HOTKEY_PRESETS],
            state="readonly", width=16)
        hotkey_combo.pack(side="left")
        hotkey_combo.bind("<<ComboboxSelected>>",
                          lambda e: self._on_overlay_hotkey_change())

        self.overlay_status_label = tk.Label(
            overlay_card.body, text="", bg=c["SURFACE"], fg=c["FG_MUTED"],
            font=("Segoe UI", 8), anchor="w", justify="left", wraplength=740)
        self.overlay_status_label.pack(fill="x", pady=(10, 0))
        self._update_overlay_status_label()

        tk.Frame(overlay_card.body, bg=c["BORDER_SOFT"], height=1).pack(
            fill="x", pady=(12, 12))

        self.settings_tray_var = tk.BooleanVar(
            value=self.settings.get("minimize_to_tray", False))
        tk.Checkbutton(
            overlay_card.body,
            text="Minimize to system tray instead of the taskbar",
            variable=self.settings_tray_var,
            command=self._on_minimize_to_tray_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"],
            highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w",
        ).pack(anchor="w")
        if not overlay_mod.TrayIcon.available():
            tk.Label(overlay_card.body,
                     text="pystray isn't installed in this build, so this "
                          "option won't do anything even if checked.",
                     bg=c["SURFACE"], fg=c["FG_MUTED"], font=("Segoe UI", 8),
                     anchor="w").pack(fill="x", pady=(4, 0))

        # ---- Startup ----
        section_title("Startup",
                      "Launch automatically when Windows starts. Windows only.")

        startup_card = Card(self.settings_inner, c, padding=(22, 16), radius=12)
        startup_card.pack(fill="x", pady=(0, 6))

        self.settings_boot_var = tk.BooleanVar(
            value=self.settings.get("start_on_boot", False))
        tk.Checkbutton(
            startup_card.body,
            text="Start automatically when Windows starts",
            variable=self.settings_boot_var,
            command=self._on_start_on_boot_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"],
            highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w",
        ).pack(anchor="w")

        self.settings_boot_minimized_var = tk.BooleanVar(
            value=self.settings.get("start_minimized", False))
        tk.Checkbutton(
            startup_card.body,
            text="Start minimized (to tray if enabled above, otherwise "
                 "just minimized)",
            variable=self.settings_boot_minimized_var,
            command=self._on_start_minimized_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"],
            highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w",
        ).pack(anchor="w", pady=(6, 0))

        self.startup_status_label = tk.Label(
            startup_card.body, text="", bg=c["SURFACE"], fg=c["FG_MUTED"],
            font=("Segoe UI", 8), anchor="w", justify="left", wraplength=740)
        self.startup_status_label.pack(fill="x", pady=(10, 0))
        self._update_startup_status_label()

        # ---- Updates ----
        section_title("Updates", f"You're running version {APP_VERSION}.")

        update_card = Card(self.settings_inner, c, padding=(22, 16), radius=12)
        update_card.pack(fill="x", pady=(0, 6))

        # Windows-and-frozen-only: this whole install mechanism replaces
        # the running .exe (see delta_force_updater.py), which is only
        # meaningful for the real built app, not a dev run of the source.
        can_self_install = updater_mod.IS_WINDOWS and getattr(sys, "frozen", False)

        self.autodownload_var = tk.BooleanVar(
            value=self.settings.get("auto_download_updates", True))
        autodownload_check = tk.Checkbutton(
            update_card.body,
            text="Automatically download updates in the background "
                 "(never installs without asking)",
            variable=self.autodownload_var, command=self._on_autodownload_toggle,
            background=c["SURFACE"], foreground=c["FG"],
            activebackground=c["SURFACE"], activeforeground=c["FG"],
            selectcolor=c["SURFACE_ALT"], highlightthickness=0, borderwidth=0,
            font=("Segoe UI", 10), anchor="w")
        autodownload_check.pack(anchor="w", pady=(0, 10))
        if not can_self_install:
            autodownload_check.configure(state="disabled")

        update_buttons = tk.Frame(update_card.body, bg=c["SURFACE"])
        update_buttons.pack(anchor="w")

        self.check_updates_btn = self._pill_button(
            update_buttons, "Check for Updates",
            lambda: self._start_update_check(manual=True))
        self.check_updates_btn.pack(side="left", padx=(0, 8))

        self.install_update_btn = self._pill_button(
            update_buttons, "Install Downloaded Update",
            self._install_staged_update, primary=True)
        self.install_update_btn.pack(side="left")
        self.install_update_btn.set_state(
            "normal" if (can_self_install and self._staged_update) else "disabled")

        self.update_status_label = tk.Label(
            update_card.body, text="", bg=c["SURFACE"], fg=c["FG_MUTED"],
            font=("Segoe UI", 9), anchor="w", justify="left")
        self.update_status_label.pack(fill="x", pady=(10, 0))
        self.update_status_label.bind(
            "<Configure>", lambda e: e.widget.configure(wraplength=e.width))
        if not comm.update_check_configured():
            self.update_status_label.configure(
                text="This build isn't set up to check for updates.")

        # ---- About & Legal ----
        section_title("About & Legal", "DF Tracker is an unofficial, fan-made tool.")

        about_card = Card(self.settings_inner, c, padding=(22, 16), radius=12)
        about_card.pack(fill="x", pady=(0, 6))

        about_text = tk.Label(
            about_card.body, text=legal_mod.ABOUT_SHORT, bg=c["SURFACE"],
            fg=c["FG_DIM"], font=("Segoe UI", 9), anchor="w", justify="left")
        about_text.pack(fill="x")
        about_text.bind(
            "<Configure>", lambda e: e.widget.configure(wraplength=e.width))

        about_buttons = tk.Frame(about_card.body, bg=c["SURFACE"])
        about_buttons.pack(anchor="w", pady=(12, 0))
        self._pill_button(
            about_buttons, "Disclaimer",
            lambda: self._show_disclaimer_dialog()).pack(side="left", padx=(0, 8))
        self._pill_button(
            about_buttons, "License",
            lambda: self._show_text_window(
                "License", legal_mod.read_bundled_text("LICENSE"))
        ).pack(side="left", padx=(0, 8))
        self._pill_button(
            about_buttons, "Third-Party Notices",
            lambda: self._show_text_window(
                "Third-Party Notices",
                legal_mod.read_bundled_text("THIRD_PARTY_NOTICES.txt"))
        ).pack(side="left")

        # ---- Profile & Match Details ----
        section_title("Profile & Match Details",
                      "Pull your live profile card, or backfill the full "
                      "squad/kill breakdown for older matches that don't "
                      "have it yet. Backfilling is slow and rate-limited "
                      "since it fetches one match at a time.")

        detail_card = Card(self.settings_inner, c, padding=(22, 18), radius=12)
        detail_card.pack(fill="x", pady=(0, 6))

        detail_buttons = tk.Frame(detail_card.body, bg=c["SURFACE"])
        detail_buttons.pack(fill="x")

        self.refresh_profile_btn = self._pill_button(
            detail_buttons, "Refresh Profile", self.start_profile_fetch)
        self.refresh_profile_btn.pack(side="left", padx=(0, 8))

        self.refresh_items_btn = self._pill_button(
            detail_buttons, "Refresh High-Value Items",
            lambda: self.start_asset_calendar_fetch(silent=False))
        self.refresh_items_btn.pack(side="left", padx=(0, 8))

        self.refresh_weekly_btn = self._pill_button(
            detail_buttons, "Refresh Weekly Report",
            lambda: self.start_weekly_report_fetch(silent=False))
        self.refresh_weekly_btn.pack(side="left", padx=(0, 8))

        detail_buttons2 = tk.Frame(detail_card.body, bg=c["SURFACE"])
        detail_buttons2.pack(fill="x", pady=(8, 0))

        self.backfill_btn = self._pill_button(
            detail_buttons2, "Fetch All Match Details", self.start_backfill)
        self.backfill_btn.pack(side="left", padx=(0, 8))

        self.cancel_backfill_btn = self._pill_button(
            detail_buttons2, "Cancel", self.cancel_backfill)
        self.cancel_backfill_btn.pack(side="left")

        # ---- Data & Export ----
        section_title("Data & Export",
                      "Export the match history as CSVs, force a full "
                      "re-pull ignoring the cache, or open the folder "
                      "where the cache lives.")

        data_card = Card(self.settings_inner, c, padding=(22, 18), radius=12)
        data_card.pack(fill="x", pady=(0, 6))

        self.data_stats_label = tk.Label(
            data_card.body, text="", bg=c["SURFACE"], fg=c["FG_DIM"],
            font=("Segoe UI", 9), anchor="w", justify="left",
            wraplength=740)
        self.data_stats_label.pack(fill="x")

        data_buttons = tk.Frame(data_card.body, bg=c["SURFACE"])
        data_buttons.pack(fill="x", pady=(12, 0))

        def small_btn(parent, text, command, primary=False):
            bg = c["ACCENT"] if primary else c["SURFACE_ALT"]
            fg = c["BG_BOTTOM"] if primary else c["FG"]
            b = tk.Label(parent, text=text, bg=bg, fg=fg,
                         font=("Segoe UI Semibold", 9),
                         padx=14, pady=7, cursor="hand2")
            b.pack(side="left", padx=(0, 8))
            b.bind("<Button-1>", lambda e: command())
            return b

        small_btn(data_buttons, "Export CSVs", self.do_export, primary=True)
        small_btn(data_buttons, "Open Data Folder", self._open_data_folder)
        small_btn(data_buttons, "Refresh Data Stats", self._refresh_data_stats)

        self.refresh_all_btn = self._pill_button(
            data_card.body, "Re-fetch All Matches (ignores cache, slow)",
            lambda: self.start_fetch(True))
        self.refresh_all_btn.pack(fill="x", pady=(10, 0))

        self._refresh_data_stats()

    # ------------------------------------------------------------------
    # Settings actions
    # ------------------------------------------------------------------

        self._make_labels_wrap_dynamic(self.settings_inner)

    def _on_auto_refresh_toggle(self):
        enabled = self.auto_refresh_var.get()
        _save_settings({"auto_refresh_enabled": enabled})
        self.settings["auto_refresh_enabled"] = enabled
        self._update_auto_status_label()
        if enabled:
            self._schedule_next_auto_refresh()
            self.set_status(f"Auto-refresh enabled (every "
                            f"{self.settings.get('auto_refresh_minutes', DEFAULT_AUTO_REFRESH_MINUTES)} min).")
        else:
            self._cancel_auto_refresh()
            self.set_status("Auto-refresh disabled.")

    def _on_auto_refresh_interval_change(self):
        try:
            mins = int(self.auto_refresh_combo_var.get())
        except ValueError:
            return
        _save_settings({"auto_refresh_minutes": mins})
        self.settings["auto_refresh_minutes"] = mins
        self._update_auto_status_label()
        if self.auto_refresh_var.get():
            self._schedule_next_auto_refresh()
        self.set_status(f"Auto-refresh interval set to {mins} min.")

    def _on_notify_toggle(self):
        v = self.notify_var.get()
        _save_settings({"auto_refresh_notify": v})
        self.settings["auto_refresh_notify"] = v

    def _on_settings_rpc_toggle(self):
        """Sync the settings-tab RPC checkbox with the sidebar one."""
        target = self.settings_rpc_var.get()
        # Update the sidebar checkbox first so _toggle_rpc reads the right state.
        if hasattr(self, "rpc_var"):
            self.rpc_var.set(target)
        self._toggle_rpc()
        # If the toggle failed, _toggle_rpc already reset rpc_var to False.
        # Mirror whatever the sidebar ended up with back into the settings tab.
        if hasattr(self, "rpc_var"):
            self.settings_rpc_var.set(self.rpc_var.get())

    def _update_overlay_status_label(self):
        if not hasattr(self, "overlay_status_label"):
            return
        c = self.colors
        if not self.settings.get("overlay_enabled"):
            self.overlay_status_label.configure(
                text="Off. Turn on to check your stats without leaving the game.",
                fg=c["FG_MUTED"])
        elif not overlay_mod.IS_WINDOWS:
            self.overlay_status_label.configure(
                text="This build isn't running on Windows, so the global "
                     "shortcut can't be registered here.", fg=c["NEGATIVE"])
        elif self.hotkey_listener.is_registered():
            label = self.settings.get("overlay_hotkey", overlay_mod.DEFAULT_HOTKEY_LABEL)
            self.overlay_status_label.configure(
                text=f"Active — press {label} anywhere to show or hide it.",
                fg=c["POSITIVE"])
        else:
            err = self.hotkey_listener.last_error or "Couldn't register the shortcut."
            self.overlay_status_label.configure(text=err, fg=c["NEGATIVE"])

    def _on_overlay_enabled_toggle(self):
        enabled = self.settings_overlay_var.get()
        _save_settings({"overlay_enabled": enabled})
        self.settings["overlay_enabled"] = enabled
        if enabled:
            self._start_hotkey_listener()
        else:
            self._stop_hotkey_listener()
            self.overlay.hide()
        self._update_overlay_status_label()

    def _on_overlay_hotkey_change(self):
        label = self.overlay_hotkey_var.get()
        _save_settings({"overlay_hotkey": label})
        self.settings["overlay_hotkey"] = label
        if self.settings.get("overlay_enabled"):
            self._start_hotkey_listener()  # .start() already stops any previous one
        self._update_overlay_status_label()

    def _on_minimize_to_tray_toggle(self):
        enabled = self.settings_tray_var.get()
        _save_settings({"minimize_to_tray": enabled})
        self.settings["minimize_to_tray"] = enabled

    def _repair_startup_entry(self):
        """If Start-on-boot is on but the Run entry points at an old
        location (app folder moved or re-extracted), fix it quietly."""
        if self.settings.get("start_on_boot"):
            startup_mod.repair_if_stale(self.settings.get("start_minimized", False))

    def _update_startup_status_label(self):
        if not hasattr(self, "startup_status_label"):
            return
        c = self.colors
        if not startup_mod.IS_WINDOWS:
            self.startup_status_label.configure(
                text="This build isn't running on Windows, so this can't "
                     "register a startup entry here.", fg=c["NEGATIVE"])
        elif self.settings.get("start_on_boot") and not startup_mod.is_enabled():
            self.startup_status_label.configure(
                text="Couldn't write the startup entry — this account may "
                     "not have permission to. Nothing else is affected.",
                fg=c["NEGATIVE"])
        elif self.settings.get("start_on_boot"):
            self.startup_status_label.configure(
                text="Will launch automatically at Windows login.",
                fg=c["POSITIVE"])
        else:
            self.startup_status_label.configure(text="Off.", fg=c["FG_MUTED"])

    def _on_start_on_boot_toggle(self):
        enabled = self.settings_boot_var.get()
        minimized = self.settings_boot_minimized_var.get()
        ok = startup_mod.set_enabled(enabled, minimized=minimized)
        # Reflect reality: if the registry write failed, don't claim the
        # setting is on just because the checkbox is checked - leave the
        # checkbox as the user left it (their intent), but the status
        # label (updated below) will say it didn't actually take.
        _save_settings({"start_on_boot": enabled})
        self.settings["start_on_boot"] = enabled
        self._update_startup_status_label()
        if enabled and not ok:
            messagebox.showerror(
                "Couldn't enable startup",
                "Windows didn't let this write to the startup registry "
                "entry. This is sometimes restricted on managed/work "
                "computers.", parent=self.root)

    def _on_start_minimized_toggle(self):
        minimized = self.settings_boot_minimized_var.get()
        _save_settings({"start_minimized": minimized})
        self.settings["start_minimized"] = minimized
        # Keep the registry command line in sync immediately, not just
        # the next time start_on_boot itself gets toggled - otherwise
        # flipping this alone would silently not take effect until the
        # user also re-toggled the main checkbox.
        if self.settings.get("start_on_boot"):
            startup_mod.set_enabled(True, minimized=minimized)

    def _open_data_folder(self):
        try:
            folder = APP_DATA_DIR
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(folder)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            messagebox.showerror("Couldn't open folder", str(e), parent=self.root)

    def _refresh_data_stats(self):
        if not hasattr(self, "data_stats_label"):
            return
        cache = core.load_cache()
        n_matches = len(cache.get("matches", {}))
        n_details = len(cache.get("details", {}))
        size = 0
        try:
            size = core.CACHE_FILE.stat().st_size
        except OSError:
            pass

        last = (self._last_refresh_ts.strftime("%Y-%m-%d %H:%M:%S")
                if self._last_refresh_ts else "never")
        src = core.credential_source()

        self.data_stats_label.configure(
            text=(
                f"Cached matches: {n_matches}\n"
                f"Cached match details: {n_details}\n"
                f"Cache file size: {size / 1024:.1f} KB\n"
                f"Last refresh: {last}\n"
                f"Credentials: {src}"
            )
        )

    def _update_auto_status_label(self):
        if not hasattr(self, "auto_status_label"):
            return
        if not self.auto_refresh_var.get():
            self.auto_status_label.configure(
                text="Auto-refresh is off. Use Quick Refresh manually.")
            return
        mins = self.settings.get("auto_refresh_minutes",
                                 DEFAULT_AUTO_REFRESH_MINUTES)
        nxt = ""
        if self._auto_refresh_after_id is not None:
            nxt = f"  ·  next in ~{mins} min"
        last = (self._last_refresh_ts.strftime("%H:%M:%S")
                if self._last_refresh_ts else "—")
        self.auto_status_label.configure(
            text=f"Running every {mins} min.  Last refresh: {last}{nxt}")

    # ------------------------------------------------------------------
    # Auto-refresh loop
    # ------------------------------------------------------------------
    def _schedule_next_auto_refresh(self):
        self._cancel_auto_refresh()
        if not self.auto_refresh_var.get():
            self._update_auto_status_label()
            return
        try:
            mins = int(self.settings.get("auto_refresh_minutes",
                                          DEFAULT_AUTO_REFRESH_MINUTES))
        except (TypeError, ValueError):
            mins = DEFAULT_AUTO_REFRESH_MINUTES
        ms = mins * 60 * 1000
        self._auto_refresh_after_id = self.root.after(ms, self._auto_refresh_tick)
        self._update_auto_status_label()

    def _cancel_auto_refresh(self):
        if self._auto_refresh_after_id is not None:
            try:
                self.root.after_cancel(self._auto_refresh_after_id)
            except tk.TclError:
                pass
            self._auto_refresh_after_id = None

    def _auto_refresh_tick(self):
        self._auto_refresh_after_id = None
        if not self.auto_refresh_var.get():
            return

        # Don't step on an in-flight fetch or login. Reschedule for a short
        # delay instead of skipping the cycle entirely.
        if (self.match_busy or self.quick_busy
                or self.profile_busy or self._login_busy):
            self._auto_refresh_after_id = self.root.after(
                30_000, self._auto_refresh_tick)
            self.set_status("Auto-refresh skipped (another fetch is running).")
            return

        self.set_status("Auto-refresh: fetching new matches…")
        core.set_debug(self.debug_var.get())
        threading.Thread(target=self._auto_refresh_worker,
                         daemon=True).start()

    def _auto_refresh_worker(self):
        try:
            cache = core.refresh_today(
                on_progress=lambda m: self.msg_queue.put(("auto_progress", m)),
                on_debug=lambda m: self.msg_queue.put(("debug", m)),
            )
            self.msg_queue.put(("auto_done", cache))
        except Exception as e:
            self.msg_queue.put(("auto_error", str(e)))

    def _on_auto_refresh_done(self, cache):
        """Called from the queue handler when an auto-refresh finishes."""
        prev_count = len(self.rows)
        self.rows = core.build_rows(cache)
        new_count = max(0, len(self.rows) - prev_count)
        self._last_refresh_ts = datetime.now()
        self._last_refresh_new = new_count

        self._rerender_section(self.active_section)

        if new_count:
            msg = f"Auto-refresh: +{new_count} new match{'es' if new_count != 1 else ''}."
        else:
            msg = "Auto-refresh: no new matches."
        self.set_status(msg)

        if new_count and self.notify_var.get():
            self._show_toast(
                "New matches fetched",
                f"{new_count} new match{'es' if new_count != 1 else ''} added "
                f"to your history.")

        # Discord presence needs to reflect new data. It's fine to push
        # immediately; the RPC layer will rate-limit as needed.
        try:
            import delta_force_rpc
            if delta_force_rpc.is_enabled():
                delta_force_rpc.update_presence(self.rows, self.profile_data)
        except Exception:
            pass

        self._refresh_data_stats()
        self._schedule_next_auto_refresh()
        self._maybe_auto_sync()

    def _on_auto_refresh_error(self, err):
        self.set_status(f"Auto-refresh failed: {err}")
        self._schedule_next_auto_refresh()

    def _show_toast(self, title: str, message: str, duration_ms: int = 4000):
        """Lightweight toast. Toplevel near bottom-right, auto-fades."""
        try:
            c = self.colors
            toast = tk.Toplevel(self.root)
            toast.overrideredirect(True)
            toast.configure(bg=c["BORDER"])
            toast.attributes("-topmost", True)

            inner = tk.Frame(toast, bg=c["SURFACE"])
            inner.pack(padx=1, pady=1, fill="both", expand=True)
            tk.Label(inner, text=title, bg=c["SURFACE"], fg=c["FG"],
                     font=("Segoe UI Semibold", 10), anchor="w").pack(
                fill="x", padx=14, pady=(10, 2))
            tk.Label(inner, text=message, bg=c["SURFACE"], fg=c["FG_DIM"],
                     font=("Segoe UI", 9), anchor="w", wraplength=300,
                     justify="left").pack(fill="x", padx=14, pady=(0, 12))

            toast.update_idletasks()
            w = toast.winfo_width()
            h = toast.winfo_height()
            x = self.root.winfo_rootx() + self.root.winfo_width() - w - 24
            y = self.root.winfo_rooty() + self.root.winfo_height() - h - 60
            toast.geometry(f"+{x}+{y}")

            def close_toast():
                try:
                    toast.destroy()
                except tk.TclError:
                    pass
            toast.after(duration_ms, close_toast)
        except Exception:
            pass

    def _toggle_debug_panel(self):
        core.set_debug(self.debug_var.get())
        if self.debug_var.get():
            self.debug_frame.pack(side="bottom", fill="x")
        else:
            self.debug_frame.pack_forget()

    # ------------------------------------------------------------------
    # Discord Rich Presence
    # ------------------------------------------------------------------
    def _toggle_rpc(self):
        on = self.rpc_var.get()
        try:
            import delta_force_rpc
        except ImportError:
            self.rpc_var.set(False)
            messagebox.showinfo(
                "Discord RPC unavailable",
                "delta_force_rpc.py is missing or pypresence isn't installed.\n\n"
                "Install with:\n    pip install pypresence\n\n"
                "Then set DISCORD_CLIENT_ID in delta_force_rpc.py.",
                parent=self.root,
            )
            return

        if not delta_force_rpc.is_available():
            self.rpc_var.set(False)
            messagebox.showinfo(
                "Discord RPC unavailable",
                "pypresence isn't installed.\n\n"
                "    pip install pypresence",
                parent=self.root,
            )
            return

        if on:
            ok = delta_force_rpc.enable()
            if ok:
                self.set_status("Discord Rich Presence enabled.")
                try:
                    delta_force_rpc.update_presence(
                        self.rows, self.profile_data)
                except Exception:
                    pass
            else:
                err = delta_force_rpc.last_error() or "unknown error"
                self.rpc_var.set(False)
                messagebox.showinfo(
                    "Discord RPC couldn't connect",
                    f"{err}\n\n"
                    "Is Discord running? Did you set DISCORD_CLIENT_ID "
                    "in delta_force_rpc.py?",
                    parent=self.root,
                )
        else:
            delta_force_rpc.disable()
            self.set_status("Discord Rich Presence disabled.")

        _save_settings({"discord_rpc": self.rpc_var.get()})

    def _restore_rpc_state(self):
        if not getattr(self, "rpc_var", None) or not self.rpc_var.get():
            return
        try:
            import delta_force_rpc
        except ImportError:
            self.rpc_var.set(False)
            return
        if not delta_force_rpc.is_available():
            self.rpc_var.set(False)
            return
        if delta_force_rpc.enable():
            try:
                delta_force_rpc.update_presence(self.rows, self.profile_data)
            except Exception:
                pass
            self.set_status("Discord Rich Presence enabled.")
        else:
            self.rpc_var.set(False)
            self.set_status("Discord RPC couldn't connect (is Discord running?).")

    # ------------------------------------------------------------------
    # Login (embedded browser)
    # ------------------------------------------------------------------
    def start_browser_login(self):
        if self._login_busy:
            return
        self._login_busy = True
        self.set_status("Opening browser for login…")
        self._update_busy_state()

        def worker():
            try:
                import delta_force_login
                delta_force_login.run(headless=False, persistent=False)
                self.msg_queue.put(("login_done", None))
            except ImportError as e:
                self.msg_queue.put((
                    "login_error",
                    "Playwright isn't installed.\n\n"
                    "Run:\n  pip install playwright\n"
                    "  playwright install chromium\n\n"
                    f"({e})"
                ))
            except Exception as e:
                self.msg_queue.put(("login_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _startup_gate(self):
        """First thing after launch. Until the disclaimer has been
        acknowledged once, nothing else (login prompt, changelog) happens
        - existing installs upgrading into this see it one time too."""
        if not self.settings.get("disclaimer_accepted"):
            self._show_disclaimer_dialog(on_accept=self._after_startup_gate)
            return
        self._after_startup_gate()

    def _after_startup_gate(self):
        self._maybe_prompt_login()
        self._maybe_show_whats_new()

    def _show_disclaimer_dialog(self, on_accept=None):
        """on_accept given: the first-run gate (I Understand / Quit -
        closing the window counts as Quit). on_accept None: a read-only
        viewer for Settings > About & Legal."""
        try:
            c = self.colors
            gate = on_accept is not None
            win = tk.Toplevel(self.root)
            win.title(legal_mod.DISCLAIMER_TITLE if gate else "Disclaimer")
            win.configure(bg=c["BG_TOP"])
            win.resizable(False, False)
            win.transient(self.root)
            win.geometry("500x1")  # fixed width; static wraplength below matches it

            body = tk.Frame(win, bg=c["BG_TOP"], padx=28, pady=24)
            body.pack(fill="both", expand=True)
            tk.Label(body, text="Unofficial fan-made tool", bg=c["BG_TOP"],
                     fg=c["FG"], font=("Segoe UI Semibold", 14),
                     anchor="w").pack(fill="x", pady=(0, 12))
            for i, para in enumerate(legal_mod.DISCLAIMER_PARAGRAPHS):
                tk.Label(body, text=para, bg=c["BG_TOP"],
                         fg=c["FG"] if i == 0 else c["FG_DIM"],
                         font=("Segoe UI", 10), anchor="w", justify="left",
                         wraplength=440).pack(fill="x", pady=(0, 10))

            buttons = tk.Frame(body, bg=c["BG_TOP"])
            buttons.pack(anchor="e", pady=(8, 0))

            def accept():
                self.settings["disclaimer_accepted"] = True
                _save_settings({"disclaimer_accepted": True})
                win.destroy()
                on_accept()

            def decline():
                win.destroy()
                self._on_close()

            if gate:
                self._pill_button(buttons, "Quit", decline).pack(side="left", padx=(0, 8))
                self._pill_button(buttons, "I Understand", accept,
                                  primary=True).pack(side="left")
                win.protocol("WM_DELETE_WINDOW", decline)
                win.accept, win.decline = accept, decline  # exposed for tests
            else:
                self._pill_button(buttons, "Close", win.destroy,
                                  primary=True).pack(side="left")

            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
            if gate:
                try:
                    win.grab_set()  # block the main window until answered
                except tk.TclError:
                    pass
                win.lift()
                win.focus_force()
        except Exception:
            pass

    def _show_text_window(self, title: str, text: str):
        """Read-only scrollable text (License, Third-Party Notices)."""
        try:
            c = self.colors
            win = tk.Toplevel(self.root)
            win.title(title)
            win.configure(bg=c["BG_TOP"])
            win.geometry("700x540")
            win.transient(self.root)
            frame = tk.Frame(win, bg=c["BG_TOP"], padx=16, pady=16)
            frame.pack(fill="both", expand=True)
            scroll = ttk.Scrollbar(frame, orient="vertical")
            box = tk.Text(frame, wrap="word", bg=c["SURFACE"], fg=c["FG_DIM"],
                          font=("Consolas", 9), relief="flat",
                          highlightthickness=0, padx=12, pady=10,
                          yscrollcommand=scroll.set)
            scroll.config(command=box.yview)
            scroll.pack(side="right", fill="y")
            box.pack(side="left", fill="both", expand=True)
            box.insert("1.0", text)
            box.configure(state="disabled")
        except Exception:
            pass

    def _make_labels_wrap_dynamic(self, container):
        """Binds each wrapped Label's wraplength to its actual rendered
        width. A fixed wraplength larger than the real width doesn't
        re-wrap, it clips the text (the recurring bug fixed one label at
        a time elsewhere in this file); doing it for a whole page at once
        means labels added later can't reintroduce it. Skips labels that
        already have a <Configure> binding of their own."""
        for w in container.winfo_children():
            if isinstance(w, tk.Label):
                try:
                    if int(w.cget("wraplength")) > 0 and not w.bind("<Configure>"):
                        w.bind("<Configure>",
                               lambda e: e.widget.configure(wraplength=e.width))
                except (tk.TclError, ValueError):
                    pass
            self._make_labels_wrap_dynamic(w)

    def _show_first_login_popup(self):
        """Shown once, specifically on someone's very first login (empty
        match cache) - the status bar message about "fetching your full
        match history" works fine for someone who already knows to look
        there, but a brand-new user has no reason to expect it or know
        where to look. This puts the same information somewhere they
        can't miss it: centered, with a real title bar, and dismissed
        manually rather than auto-fading like _show_toast - missing this
        one matters more than missing a routine "new matches" notice.

        Non-modal on purpose (no grab_set()) - the message itself says
        they can keep using the app while this runs, so it shouldn't
        block them from doing that.
        """
        try:
            c = self.colors
            win = tk.Toplevel(self.root)
            win.title("Fetching Your Match History")
            win.configure(bg=c["BG_TOP"])
            win.resizable(False, False)
            win.transient(self.root)
            # Fixed width, set before packing content: lets the message
            # label below use a wraplength computed to exactly match
            # this dialog's own (fixed, known) width, rather than
            # needing the dynamic <Configure>-based wraplength trick
            # used elsewhere in this app for labels whose container
            # width isn't known in advance (e.g. inside a scrollable
            # card). Here there's no such uncertainty - this dialog's
            # width is simply whatever this line sets.
            win.geometry("420x1")

            body = tk.Frame(win, bg=c["BG_TOP"], padx=28, pady=24)
            body.pack(fill="both", expand=True)

            tk.Label(body, text="Fetching Your Match History", bg=c["BG_TOP"],
                     fg=c["FG"], font=("Segoe UI Semibold", 14),
                     anchor="w").pack(fill="x", pady=(0, 12))

            tk.Label(
                body,
                text="This is your first time logging in, so the tracker "
                     "is pulling your complete match history from the "
                     "server. Depending on how many matches you've "
                     "played, this can take a little while.\n\n"
                     "You can keep using the app while it finishes — "
                     "watch the status bar in the bottom-left corner for "
                     "progress.",
                bg=c["BG_TOP"], fg=c["FG_DIM"], font=("Segoe UI", 10),
                anchor="w", justify="left", wraplength=364,
            ).pack(fill="x", pady=(0, 20))

            btn = self._pill_button(body, "Got It", win.destroy, primary=True)
            btn.pack(anchor="e")

            win.update_idletasks()
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _maybe_show_whats_new(self):
        """Shown at startup when the running version is newer than
        whatever this settings file last recorded - collects every
        CHANGELOG entry newer than that, not just the current version,
        so someone who skipped a few releases sees everything they
        missed rather than just the latest one.

        An empty last_seen_version (never recorded before - a brand-new
        install, or an existing one upgrading into this feature for the
        first time) shows nothing: there's no meaningful "since when" to
        compare against, and dumping the entire historical changelog on
        an existing user the first time this code runs would be noise,
        not news. Either way, this always ends by recording the current
        version as seen, establishing the baseline for next time.
        """
        last_seen = self.settings.get("last_seen_version", "")
        if last_seen and version_tuple(last_seen) < version_tuple(APP_VERSION):
            entries = [(v, bullets) for v, bullets in CHANGELOG.items()
                      if version_tuple(v) > version_tuple(last_seen)]
            entries.sort(key=lambda pair: version_tuple(pair[0]))
            if entries:
                self._show_whats_new_popup(entries)
        self.settings["last_seen_version"] = APP_VERSION
        _save_settings({"last_seen_version": APP_VERSION})

    def _show_whats_new_popup(self, entries):
        try:
            c = self.colors
            win = tk.Toplevel(self.root)
            win.title("What's New")
            win.configure(bg=c["BG_TOP"])
            win.resizable(False, False)
            win.transient(self.root)
            win.geometry("460x1")  # fixed width - see _show_first_login_popup

            body = tk.Frame(win, bg=c["BG_TOP"], padx=28, pady=24)
            body.pack(fill="both", expand=True)

            tk.Label(body, text=f"What's New in v{APP_VERSION}", bg=c["BG_TOP"],
                     fg=c["FG"], font=("Segoe UI Semibold", 14),
                     anchor="w").pack(fill="x", pady=(0, 14))

            multi = len(entries) > 1
            for version, bullets in entries:
                if multi:
                    tk.Label(body, text=f"v{version}", bg=c["BG_TOP"],
                             fg=c["FG_MUTED"], font=("Segoe UI", 9, "bold"),
                             anchor="w").pack(fill="x", pady=(0, 4))
                for bullet in bullets:
                    row = tk.Frame(body, bg=c["BG_TOP"])
                    row.pack(fill="x", pady=(0, 8))
                    tk.Label(row, text="•", bg=c["BG_TOP"], fg=c["ACCENT"],
                             font=("Segoe UI", 10), anchor="nw",
                             width=2).pack(side="left")
                    tk.Label(row, text=bullet, bg=c["BG_TOP"], fg=c["FG_DIM"],
                             font=("Segoe UI", 10), anchor="w",
                             justify="left", wraplength=380).pack(
                        side="left", fill="x", expand=True)

            btn = self._pill_button(body, "Got It", win.destroy, primary=True)
            btn.pack(anchor="e", pady=(8, 0))

            win.update_idletasks()
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _on_login_done(self):
        self._login_busy = False
        refreshed = core.refresh_credentials_from_browser(force=True)
        self._update_sidebar_profile()
        if refreshed:
            self.start_profile_fetch(silent=True)
            self.start_asset_calendar_fetch(silent=True)
            self.start_weekly_report_fetch(silent=True)
            self._maybe_prompt_community_optin()

            # First-ever login (nothing cached locally yet) gets the full
            # match history, not just Quick Refresh's first couple pages
            # (40 matches) - a brand new install showing only the 40 most
            # recent games looked broken/incomplete, and the person
            # shouldn't have to know to go find "Fetch All" themselves.
            # Returning users keep the lighter Quick Refresh on login,
            # since they already have their history and a full re-fetch
            # every login would just be slow for no benefit.
            cache = core.load_cache()
            if not cache.get("matches"):
                self.set_status(
                    f"Login captured ({core.credential_source()}) — this "
                    "is your first login, so fetching your full match "
                    "history now (this may take a little while)…")
                self._show_first_login_popup()
                self.start_fetch(True)
            else:
                self.set_status(f"Login captured ({core.credential_source()}) — "
                                "refreshing data…")
                self.start_quick_refresh()
        else:
            self.set_status("Login window closed without capturing credentials.")
        self._update_busy_state()

    def _maybe_prompt_community_optin(self):
        """One-time (per device) ask, right after a successful login,
        whether to join the community leaderboard. Never shown again
        after the first answer either way - a 'no' is respected, and a
        'yes' just leads to the normal Community tab from then on."""
        if not comm.SERVER_URL:
            return
        settings = _load_settings()
        if settings.get("community_prompted"):
            return
        _save_settings({"community_prompted": True})

        if comm.is_linked():
            return  # already joined some other way (e.g. via the tab)

        if not messagebox.askyesno(
                "Join the community leaderboard?",
                "Want to share your stats with other opted-in Delta "
                "Force Tracker players on a public leaderboard? This "
                "only shares the aggregate numbers already on your "
                "Overview tab, under your in-game nickname.\n\n"
                "You can turn this on or off anytime from the "
                "Community tab.", parent=self.root):
            return

        nickname = None
        if self.profile_data:
            nickname = (self.profile_data.get("player_info") or {}).get("nickname")
        self._set_community_busy(True)
        threading.Thread(target=self._community_join_worker,
                         args=(nickname,), daemon=True).start()

    # ------------------------------------------------------------------
    # Avatar loading
    # ------------------------------------------------------------------
    def _avatar_disk_path(self, operator_id: str) -> Path:
        return AVATAR_DIR / f"{operator_id}.png"

    def _load_avatar_bytes(self, operator_id: str):
        if not operator_id or not HAVE_PIL:
            return None

        disk = self._avatar_disk_path(operator_id)
        if disk.exists():
            try:
                return disk.read_bytes()
            except Exception:
                pass

        url = core.operator_avatar(operator_id)
        if not url:
            return None

        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = resp.read()
            try:
                disk.write_bytes(data)
            except Exception:
                pass
            return data
        except Exception:
            return None

    def _install_avatar_bytes(self, operator_id: str, data, size: int = 26):
        if not operator_id or data is None or not HAVE_PIL:
            self._avatar_cache[operator_id] = None
            return
        if operator_id in self._avatar_cache:
            return

        try:
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize((size, size), Image.LANCZOS)

            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            img.putalpha(mask)

            photo = ImageTk.PhotoImage(img)
            self._avatar_cache[operator_id] = photo
        except Exception:
            self._avatar_cache[operator_id] = None

    def _queue_avatar(self, operator_id: str):
        if not operator_id or not HAVE_PIL:
            return
        if operator_id in self._avatar_cache or operator_id in self._avatar_pending:
            return
        self._avatar_pending.add(operator_id)

        def worker(op_id=operator_id):
            data = self._load_avatar_bytes(op_id)
            self.msg_queue.put(("avatar_bytes", (op_id, data)))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Item icon loading (Recent High-Value Items card)
    # ------------------------------------------------------------------
    ITEM_ICON_DIR = APP_DATA_DIR / "item_icons"

    def _item_icon_disk_path(self, item_id: str) -> Path:
        return self.ITEM_ICON_DIR / f"{item_id}.png"

    def _load_item_icon_bytes(self, item_id: str, image_url: str):
        if not item_id or not image_url or not HAVE_PIL:
            return None

        disk = self._item_icon_disk_path(item_id)
        if disk.exists():
            try:
                return disk.read_bytes()
            except Exception:
                pass

        try:
            with urllib.request.urlopen(image_url, timeout=8) as resp:
                data = resp.read()
            try:
                self.ITEM_ICON_DIR.mkdir(exist_ok=True)
                disk.write_bytes(data)
            except Exception:
                pass
            return data
        except Exception:
            return None

    def _install_item_icon_bytes(self, item_id: str, data, size: int = 40):
        if not item_id or data is None or not HAVE_PIL:
            self._item_icon_cache[item_id] = None
            return
        if item_id in self._item_icon_cache:
            return
        try:
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            img.thumbnail((size, size), Image.LANCZOS)
            # Item art isn't square (see length/width in the catalog), so
            # letterbox onto a square canvas instead of cropping - cropping
            # a narrow item icon the way avatars are cropped would chop
            # off real content, not just background.
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
            self._item_icon_cache[item_id] = ImageTk.PhotoImage(canvas)
        except Exception:
            self._item_icon_cache[item_id] = None

    def _queue_item_icon(self, item_id: str, image_url: str):
        if not item_id or not HAVE_PIL:
            return
        if item_id in self._item_icon_cache or item_id in self._item_icon_pending:
            return
        self._item_icon_pending.add(item_id)

        def worker(iid=item_id, url=image_url):
            data = self._load_item_icon_bytes(iid, url)
            self.msg_queue.put(("item_icon_bytes", (iid, data)))

        threading.Thread(target=worker, daemon=True).start()

    def _prewarm_avatars(self):
        for r in self.rows[:500]:
            op_id = r.get("operator_id")
            if not op_id:
                continue
            self._queue_avatar(op_id)

    def _poll_avatars(self):
        if self._rebuilding:
            self.root.after(800, self._poll_avatars)
            return

        if self.active_section == "matches":
            for r in self.rows[:500]:
                op_id = r.get("operator_id")
                if op_id and op_id not in self._avatar_cache:
                    self._queue_avatar(op_id)

        loaded = sum(1 for v in self._avatar_cache.values() if v is not None)
        if loaded != self._last_avatar_count:
            self._last_avatar_count = loaded
            if self.active_section == "matches":
                self._refresh_matches()

        self.root.after(800, self._poll_avatars)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _rerender_section(self, key):
        if key == "overview":
            self._refresh_overview()
        elif key == "profile":
            self._render_profile()
        elif key == "maps":
            self._refresh_maps()
        elif key == "matches":
            self._refresh_matches()
        elif key == "community":
            self._refresh_community_status_ui()
            self._render_community_leaderboard()
            self.start_community_leaderboard_fetch()
        elif key == "settings":
            self._refresh_data_stats()
            self._update_auto_status_label()
        self._update_rail()
        self._update_sidebar_profile()

    def _on_search_changed(self):
        if self.active_section == "matches":
            self.matches_page = 0
            self._refresh_matches()

    def _on_maps_sort(self, col, reverse):
        self.maps_sort_key = col
        self.maps_sort_reverse = reverse
        self._refresh_maps()

    def _on_matches_sort(self, col, reverse):
        self.matches_sort_key = col
        self.matches_sort_reverse = reverse
        self.matches_page = 0
        self._refresh_matches()

    def _matches_page(self, delta):
        if delta < 0 and not getattr(self.prev_btn, "_enabled", False):
            return
        if delta > 0 and not getattr(self.next_btn, "_enabled", False):
            return
        self.matches_page += delta
        self._refresh_matches()

    def _on_page_size_changed(self):
        val = self.page_size_var.get()
        if val == "All":
            self.matches_page_size = 10 ** 9
        else:
            try:
                self.matches_page_size = int(val)
            except ValueError:
                self.matches_page_size = 100
        self.matches_page = 0
        self._refresh_matches()

    def _render_sparkline(self, daily_rows):
        """Last-30-days net income as a zero-baseline bar chart.

        Replaces the old line+dot sparkline, which read as noisy for data
        that's fundamentally daily buckets (a line implies continuous
        interpolation between days that isn't really there). Bars from a
        zero baseline, colored win/loss, with today called out, map more
        directly onto "how'd each day go".
        """
        cvs = getattr(self, "spark_canvas", None)
        if cvs is None:
            return
        try:
            cvs.delete("all")
        except tk.TclError:
            return

        c = self.colors
        w = cvs.winfo_width()
        h = cvs.winfo_height()
        if w <= 4 or h <= 4:
            return

        if not daily_rows:
            cvs.create_text(w / 2, h / 2, text="No data yet",
                            fill=c["FG_MUTED"], font=("Segoe UI", 9))
            return

        series = list(reversed(daily_rows[:30]))
        values = [r["net_income"] for r in series]
        n = len(values)
        if not n:
            return

        pad_l, pad_r, pad_top, pad_bottom = 4, 4, 16, 16
        vmin = min(values + [0])
        vmax = max(values + [0])
        span = (vmax - vmin) or 1.0

        plot_top = pad_top
        plot_bottom = h - pad_bottom
        plot_h = plot_bottom - plot_top

        def y_for(v):
            return plot_bottom - ((v - vmin) / span) * plot_h

        zero_y = y_for(0)

        # Faint reference lines: chart top, zero baseline, chart bottom.
        for gy in (plot_top, zero_y, plot_bottom):
            cvs.create_line(pad_l, gy, w - pad_r, gy, fill=c["BORDER_SOFT"])

        plot_w = w - pad_l - pad_r
        slot_w = plot_w / n
        bar_w = max(2.0, min(slot_w * 0.6, 16))

        for i, v in enumerate(values):
            cx = pad_l + slot_w * (i + 0.5)
            y = y_for(v)
            top, bottom = min(y, zero_y), max(y, zero_y)
            is_today = (i == n - 1)

            if v >= 0:
                color = c["ACCENT"] if is_today else c["POSITIVE"]
            else:
                color = c["NEGATIVE"]

            if bottom - top < 1.5:
                # A ~zero day would otherwise vanish; draw a visible tick.
                cvs.create_line(cx - bar_w / 2, zero_y, cx + bar_w / 2, zero_y,
                                fill=color, width=2)
            else:
                round_rect(cvs, cx - bar_w / 2, top, cx + bar_w / 2, bottom,
                          min(3, bar_w / 2), fill=color, outline="")

        # Marker + label under today's bar. Today is always the last/
        # rightmost bar, which sits right at the canvas edge — a
        # center-anchored label there would get clipped, so the label
        # is right-anchored to the edge instead (the tick mark still
        # points at the actual bar). The tick sits just above the plot
        # area, below the Best/Worst text row, so the two don't collide.
        today_cx = pad_l + slot_w * (n - 0.5)
        cvs.create_line(today_cx, plot_top - 4, today_cx, plot_top - 1,
                        fill=c["ACCENT"], width=2, capstyle="round")
        cvs.create_text(w - pad_r, h - 2, text="Today",
                        fill=c["ACCENT"], font=("Segoe UI", 7, "bold"),
                        anchor="se")

        cvs.create_text(pad_l, 1, text=f"Best {core.fmt_money(vmax)}",
                        fill=c["FG_MUTED"], font=("Segoe UI", 7), anchor="nw")
        cvs.create_text(w - pad_r, 1, text=f"Worst {core.fmt_money(vmin)}",
                        fill=c["FG_MUTED"], font=("Segoe UI", 7), anchor="ne")

    def _refresh_overview(self):
        ov = core.overview(self.rows)

        def fmt_number(v):
            sign = "-" if v < 0 else ""
            return f"{sign}{abs(v):,.0f}"

        self.stat_labels["today"].configure(text=fmt_number(ov["today"]))
        self.stat_labels["week"].configure(text=fmt_number(ov["week"]))
        self.stat_labels["month"].configure(text=fmt_number(ov["month"]))
        self.stat_labels["all_time"].configure(text=fmt_number(ov["all_time"]))

        for key in ("today", "week", "month", "all_time"):
            v = ov[key]
            self.stat_labels[key].configure(
                fg=self.colors["POSITIVE"] if v >= 0 else self.colors["NEGATIVE"])

        n = ov["all_time_matches"]
        if hasattr(self, "all_time_note"):
            self.all_time_note.configure(
                text=f"({n} match{'es' if n != 1 else ''} — API history limit)")

        self.record_label.configure(
            text=f"{ov['matches']} matches — {ov['wins']}W-{ov['losses']}L "
                 f"({ov['win_rate']:.1f}% win rate)")

        summaries = {
            "daily":   core.summarize(self.rows, "date"),
            "weekly":  core.summarize(self.rows, "week"),
            "monthly": core.summarize(self.rows, "month"),
        }
        self.summaries = summaries

        self._render_sparkline(summaries["daily"])

        today_str = datetime.now().strftime("%Y-%m-%d")

        def fmt_net_compact(v):
            # Bare number, no " credits" suffix - this table is only
            # ~190px wide for two columns combined, and the unit is
            # already implied by every other number on this page.
            sign = "-" if v < 0 else ""
            return f"{sign}{abs(v):,.0f}"

        # Daily's cap (30) matters - that one really can have more rows
        # than fit. Weekly/monthly caps are really just a safety ceiling:
        # the API's own retention window (a rolling quarter of weeks, an
        # even smaller handful of months) means real data essentially
        # never reaches these, which is exactly why they no longer carry
        # a scrollbar - see the height= line below instead.
        limits = {"daily": 30, "weekly": 12, "monthly": 6}
        for key, (tree, kind) in self.summary_trees.items():
            tree.delete(*tree.get_children())
            period_rows = summaries[key][:limits[key]]
            for i, row in enumerate(period_rows):
                label = core.period_label(row["period"], kind)
                net = row["net_income"]
                stripe = "even" if i % 2 == 0 else "odd"
                tags = [stripe, "pos" if net >= 0 else "neg"]
                if kind == "date" and row["period"] == today_str:
                    tags.append("today")
                tree.insert("", "end", values=(label, fmt_net_compact(net)),
                           tags=tuple(tags))
            if key != "daily":
                # Exactly as tall as the real data, so there's nothing to
                # scroll - at least 1 row tall even when empty, so the
                # column doesn't collapse to nothing next to Daily's chart.
                tree.configure(height=max(1, len(period_rows)))
            tree.tag_configure("even", background=self.colors["SURFACE"])
            tree.tag_configure("odd", background=self.colors["SURFACE_ALT"])
            tree.tag_configure("pos", foreground=self.colors["POSITIVE"])
            tree.tag_configure("neg", foreground=self.colors["NEGATIVE"])
            tree.tag_configure("today",
                               background=self.colors["ACCENT_SOFT"],
                               foreground=self.colors["ACCENT_HI"])

        self._high_value_items = core.recent_high_value_items()
        self._render_high_value_items()
        self._weekly_highlights = core.weekly_highlights()
        self._render_weekly_highlights()
        self._refresh_session_card()

    def _refresh_maps(self):
        tree = self.maps_tree
        tree.delete(*tree.get_children())
        groups = {}
        for r in self.rows:
            g = groups.setdefault(r["map_name"], {"matches": 0, "wins": 0,
                                                   "losses": 0, "net_income": 0})
            g["matches"] += 1
            g["net_income"] += r["net_income"]
            if r["result"] == "win":
                g["wins"] += 1
            elif r["result"] == "loss":
                g["losses"] += 1

        def sort_val(item):
            name, g = item
            key = self.maps_sort_key
            if key == "map":
                return name.lower()
            if key == "matches":
                return g["matches"]
            if key == "wl":
                return g["wins"] - g["losses"]
            return g["net_income"]

        sorted_rows = sorted(groups.items(), key=sort_val,
                             reverse=self.maps_sort_reverse)

        highlight = self._highlight_map
        self._highlight_map = None
        seen_iid = None

        for i, (name, g) in enumerate(sorted_rows):
            net = g["net_income"]
            stripe = "even" if i % 2 == 0 else "odd"
            tags = [stripe, "pos" if net >= 0 else "neg"]
            is_target = bool(highlight and name == highlight)
            if is_target:
                tags.append("highlight")
            iid = tree.insert("", "end", values=(
                name, g["matches"], f"{g['wins']}-{g['losses']}",
                core.fmt_money(net)), tags=tuple(tags))
            if is_target:
                seen_iid = iid

        tree.tag_configure("even", background=self.colors["SURFACE"])
        tree.tag_configure("odd", background=self.colors["SURFACE_ALT"])
        tree.tag_configure("pos", foreground=self.colors["POSITIVE"])
        tree.tag_configure("neg", foreground=self.colors["NEGATIVE"])
        tree.tag_configure("highlight",
                           background=self.colors["ACCENT_SOFT"],
                           foreground=self.colors["ACCENT_HI"])
        if seen_iid is not None:
            tree.see(seen_iid)

    def _refresh_matches(self):
        tree = self.matches_tree
        tree.delete(*tree.get_children())

        self._prewarm_avatars()

        needle = self.search_var.get().strip().lower()

        filtered = []
        for r in self.rows:
            if needle:
                hay = " ".join([
                    r["datetime"].strftime("%Y-%m-%d %H:%M"),
                    r["result"], str(r["kill_count"]),
                    r["map_name"], r["operator_name"],
                ]).lower()
                if needle not in hay:
                    continue
            filtered.append(r)

        key = self.matches_sort_key
        rev = self.matches_sort_reverse

        def sort_val(r):
            if key == "date":
                return r["datetime"]
            if key == "result":
                return r["result"]
            if key == "kills":
                return r["kill_count"]
            if key == "map":
                return r["map_name"].lower()
            if key == "operator":
                return r["operator_name"].lower()
            if key == "rank":
                return core.extract_rank_from_row(r["_raw"])
            return r["net_income"]

        filtered.sort(key=sort_val, reverse=rev)

        total = len(filtered)
        page_size = self.matches_page_size
        pages = max(1, (total + page_size - 1) // page_size) if page_size else 1
        if self.matches_page >= pages:
            self.matches_page = pages - 1
        if self.matches_page < 0:
            self.matches_page = 0
        start = self.matches_page * page_size
        page_rows = filtered[start:start + page_size]

        for i, r in enumerate(page_rows):
            rank = core.extract_rank_from_row(r["_raw"])
            net = r["net_income"]
            avatar = self._avatar_cache.get(r["operator_id"])

            if (r.get("operator_kills") is not None
                    and r.get("bot_kills") is not None):
                kills_display = f"{r['operator_kills']} / {r['bot_kills']}"
            else:
                kills_display = str(r["kill_count"])

            stripe = "even" if i % 2 == 0 else "odd"
            values = (
                r["datetime"].strftime("%Y-%m-%d %H:%M"),
                r["result"], kills_display,
                r["map_name"], r["operator_name"], rank,
                core.fmt_money(net),
            )
            tags = (stripe, "pos" if net >= 0 else "neg")
            try:
                tree.insert("", "end", iid=str(r["room_id"]),
                            image=avatar if avatar else "",
                            values=values, tags=tags)
            except tk.TclError:
                tree.insert("", "end",
                            image=avatar if avatar else "",
                            values=values, tags=tags)

        tree.tag_configure("even", background=self.colors["SURFACE"])
        tree.tag_configure("odd", background=self.colors["SURFACE_ALT"])
        tree.tag_configure("pos", foreground=self.colors["POSITIVE"])
        tree.tag_configure("neg", foreground=self.colors["NEGATIVE"])

        self.page_label.configure(
            text=f"Page {self.matches_page + 1} of {pages}")
        count_text = f"{total} match{'es' if total != 1 else ''}"
        if needle:
            count_text += f" (filtered from {len(self.rows)})"
        self.match_count_label.configure(text=count_text)

        def enable(btn, on):
            btn.configure(
                bg=self.colors["SURFACE_ALT"] if on else self.colors["SURFACE"],
                fg=self.colors["FG"] if on else self.colors["FG_MUTED"],
                cursor="hand2" if on else "arrow")
            btn._enabled = on

        enable(self.prev_btn, self.matches_page > 0)
        enable(self.next_btn, self.matches_page < pages - 1)

    def _render_profile(self):
        for child in self.profile_inner.winfo_children():
            child.destroy()

        c = self.colors
        sections = core.format_profile(self.profile_data)
        if not sections:
            tk.Label(self.profile_inner,
                     text="No profile data yet. Click \"Refresh Profile\".",
                     bg=c["BG_TOP"], fg=c["FG_MUTED"],
                     font=("Segoe UI", 10), pady=24).pack(anchor="w")
            return

        row_frame = None
        for i, (title, stats) in enumerate(sections):
            if i % 2 == 0:
                row_frame = tk.Frame(self.profile_inner, bg=c["BG_TOP"])
                row_frame.pack(fill="x", pady=8)
            card = Card(row_frame, c, padding=(22, 20), radius=14)
            card.pack(side="left", fill="both", expand=True,
                      padx=(0 if i % 2 == 0 else 14, 0))

            tk.Label(card.body, text=title.upper(), bg=c["SURFACE"],
                     fg=c["FG_MUTED"], font=("Segoe UI", 9, "bold"),
                     anchor="w").pack(fill="x", pady=(0, 12))

            for name, value in stats:
                line = tk.Frame(card.body, bg=c["SURFACE"])
                line.pack(fill="x", pady=4)
                tk.Label(line, text=name, bg=c["SURFACE"], fg=c["FG_DIM"],
                         font=("Segoe UI", 10), anchor="w").pack(side="left")
                tk.Label(line, text=str(value), bg=c["SURFACE"], fg=c["FG"],
                         font=("Segoe UI Semibold", 10),
                         anchor="e").pack(side="right")

    def _update_sidebar_profile(self):
        c = self.colors
        profile = self.profile_data or {}
        info = profile.get("player_info") or {}
        nickname = info.get("nickname") or "—"
        rank_label, mode = core.extract_rank_from_profile(profile)
        self.sidebar_nick.configure(text=nickname)
        if rank_label == "—":
            self.sidebar_rank.configure(text="No profile", fg=c["FG_MUTED"])
        else:
            self.sidebar_rank.configure(text=f"{rank_label}  ·  {mode}",
                                        fg=c["FG_DIM"])

        logged_in = core.have_credentials("matchlist")
        if hasattr(self, "sidebar_status_dot"):
            dot_color = c["POSITIVE"] if logged_in else c["FG_MUTED"]
            try:
                self.sidebar_status_dot.itemconfig(
                    self._sidebar_dot_id, fill=dot_color)
            except tk.TclError:
                pass

        if hasattr(self, "sidebar_creds"):
            src = core.credential_source()
            if src == "not logged in":
                self.sidebar_creds.configure(
                    text="Not logged in — click Log In above.")
            else:
                self.sidebar_creds.configure(text=f"Creds: {src}")

        if hasattr(self, "login_btn"):
            if logged_in:
                self.login_btn.configure(bg=c["SURFACE_ALT"], fg=c["FG"])
            else:
                self.login_btn.configure(bg=c["ACCENT"], fg=c["BG_BOTTOM"])

    # ------------------------------------------------------------------
    # Match detail modal
    # ------------------------------------------------------------------
    def _on_match_double_click(self, event):
        tree = self.matches_tree
        iid = tree.identify_row(event.y)
        if not iid:
            return
        for r in self.rows:
            if str(r["room_id"]) == str(iid):
                self.root.after_idle(lambda rr=r: self._open_match_detail(rr))
                return

    def _open_match_detail(self, row):
        c = self.colors
        room_id = str(row["room_id"])

        if room_id in self._detail_windows:
            try:
                self._detail_windows[room_id].lift()
                return
            except tk.TclError:
                pass

        win = tk.Toplevel(self.root)
        win.title("Match Detail")
        win.configure(bg=c["BG_TOP"])
        win.geometry("880x900")
        win.minsize(720, 620)
        self._detail_windows[room_id] = win

        outer = tk.Frame(win, bg=c["BG_TOP"])
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, bg=c["BG_TOP"], highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=c["BG_TOP"])

        _scroll_sched = [False]
        def _schedule_scrollregion(_e=None):
            if _scroll_sched[0]:
                return
            _scroll_sched[0] = True
            def _do():
                _scroll_sched[0] = False
                try:
                    canvas.configure(scrollregion=canvas.bbox("all"))
                except tk.TclError:
                    pass
            canvas.after_idle(_do)

        inner.bind("<Configure>", _schedule_scrollregion)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        # Pack the scrollbar before the canvas: with only the canvas
        # using expand=True, packing it first claims the *entire*
        # remaining cavity immediately, leaving the scrollbar a 0-width
        # sliver (present, but invisible and useless). Scrollbar-first
        # gives it its real width, and the canvas still expands to fill
        # whatever's left.
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        tk.Label(inner, text="Loading match details…",
                 bg=c["BG_TOP"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 11), pady=60, padx=40).pack(fill="x")

        def _on_wheel(e):
            try:
                canvas.yview_scroll(int(-e.delta / 120), "units")
            except tk.TclError:
                pass
            return "break"

        win.bind("<MouseWheel>", _on_wheel)

        def close():
            self._close_detail(room_id)
        win.bind("<Escape>", lambda e: close())
        win.protocol("WM_DELETE_WINDOW", close)

        try:
            win.update_idletasks()
        except tk.TclError:
            return

        win.after(20, lambda: self._populate_detail(win, inner, row, canvas))

    def _close_detail(self, room_id):
        win = self._detail_windows.pop(str(room_id), None)
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _populate_detail(self, win, inner, row, canvas):
        try:
            if not win.winfo_exists():
                return
        except tk.TclError:
            return

        try:
            self._populate_detail_inner(win, inner, row, canvas)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            try:
                for child in inner.winfo_children():
                    child.destroy()
                tk.Label(
                    inner,
                    text=f"Error rendering detail:\n\n{exc}\n\n{tb[:800]}",
                    bg=self.colors["BG_TOP"],
                    fg=self.colors["NEGATIVE"],
                    font=("Consolas", 9),
                    pady=20, padx=20, justify="left",
                    anchor="w", wraplength=760,
                ).pack(fill="x")
            except tk.TclError:
                pass

    def _populate_detail_inner(self, win, inner, row, canvas):
        c = self.colors

        for child in inner.winfo_children():
            child.destroy()

        detail = row.get("_detail")

        header = tk.Frame(inner, bg=c["BG_TOP"])
        header.pack(fill="x", padx=32, pady=(32, 20))

        tk.Label(header, text=row["datetime"].strftime("%A · %B %d, %Y · %H:%M"),
                 bg=c["BG_TOP"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 10), anchor="w").pack(fill="x")
        tk.Label(header, text=row["map_name"],
                 bg=c["BG_TOP"], fg=c["FG"],
                 font=("Segoe UI Semibold", 24), anchor="w").pack(
            fill="x", pady=(8, 0))

        top = tk.Frame(inner, bg=c["BG_TOP"])
        top.pack(fill="x", padx=32, pady=(0, 20))

        def top_card(label, value, fg=None):
            card = Card(top, c, padding=(20, 16), radius=12)
            card.pack(side="left", fill="both", expand=True, padx=(0, 12))
            tk.Label(card.body, text=label, bg=c["SURFACE"],
                     fg=c["FG_MUTED"], font=("Segoe UI", 9, "bold"),
                     anchor="w").pack(fill="x")
            tk.Label(card.body, text=value, bg=c["SURFACE"],
                     fg=fg if fg else c["FG"],
                     font=("Segoe UI Semibold", 18),
                     anchor="w").pack(fill="x", pady=(8, 0))

        net = row["net_income"]
        top_card("NET INCOME", core.fmt_money(net),
                 fg=c["POSITIVE"] if net >= 0 else c["NEGATIVE"])
        top_card("CARRIED OUT",
                 core.fmt_money(row.get("carry_out_value", 0)))
        result_fg = (c["POSITIVE"] if row["result"] == "win"
                     else c["NEGATIVE"] if row["result"] == "loss"
                     else c["FG"])
        top_card("RESULT", row["result"].upper(), fg=result_fg)

        if detail is None:
            error_msg = row.get("_detail_error")
            if error_msg:
                err_card = Card(inner, c, padding=(24, 22), radius=12)
                err_card.pack(fill="x", padx=32, pady=(0, 18))
                tk.Label(err_card.body, text="Couldn't load squad details",
                         bg=c["SURFACE"], fg=c["NEGATIVE"],
                         font=("Segoe UI Semibold", 11), anchor="w").pack(fill="x")
                tk.Label(err_card.body, text=error_msg, bg=c["SURFACE"],
                         fg=c["FG_DIM"], font=("Segoe UI", 9), anchor="w",
                         wraplength=760, justify="left").pack(fill="x", pady=(6, 12))

                def _retry(_e, rr=row):
                    rr.pop("_detail_error", None)
                    self._start_detail_fetch(rr["room_id"])
                    self._populate_detail(win, inner, rr, canvas)

                retry_btn = tk.Label(err_card.body, text="Retry", bg=c["ACCENT"],
                                     fg=c["BG_BOTTOM"], font=("Segoe UI Semibold", 10),
                                     padx=18, pady=8, cursor="hand2")
                retry_btn.pack(anchor="w")
                retry_btn.bind("<Button-1>", _retry)
                return

            loading_card = Card(inner, c, padding=(24, 22), radius=12)
            loading_card.pack(fill="x", padx=32, pady=(0, 18))
            tk.Label(loading_card.body,
                     text="Fetching squad details…",
                     bg=c["SURFACE"], fg=c["FG_DIM"],
                     font=("Segoe UI", 11), anchor="w").pack(fill="x")
            self._start_detail_fetch(row["room_id"])
            return

        members = detail.get("members") or []
        self_member = next((mm for mm in members if mm.get("is_self")), None)
        match_duration = detail.get("match_duration")

        for mm in members:
            op_id = mm.get("operator_id")
            if op_id:
                self._queue_avatar(op_id)

        facts_card = Card(inner, c, padding=(22, 20), radius=12)
        facts_card.pack(fill="x", padx=32, pady=(0, 18))
        tk.Label(facts_card.body, text="MATCH FACTS", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 14))

        facts = []
        if match_duration is not None:
            facts.append(("Duration", f"{match_duration} min"))
        facts.append(("Result", row["result"].title()))
        if row.get("is_leave"):
            facts.append(("Left early", "Yes"))
        ts = row["_raw"].get("match_time")
        if ts is not None:
            try:
                local = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
                facts.append(("Started at", local))
            except (ValueError, TypeError, OSError):
                pass
        facts.append(("Map ID", str(row["map_id"])))
        facts.append(("Room ID", str(row["room_id"])))

        self._render_fact_rows(facts_card.body, facts)

        if self_member:
            you_card = Card(inner, c, padding=(22, 20), radius=12)
            you_card.pack(fill="x", padx=32, pady=(0, 18))

            head = tk.Frame(you_card.body, bg=c["SURFACE"])
            head.pack(fill="x", pady=(0, 14))

            avatar = self._avatar_cache.get(self_member.get("operator_id"))
            if avatar:
                tk.Label(head, image=avatar, bg=c["SURFACE"]).pack(
                    side="left", padx=(0, 14))
            else:
                tk.Label(head, text="◍", bg=c["SURFACE"], fg=c["FG_MUTED"],
                         font=("Segoe UI", 22)).pack(side="left", padx=(0, 10))

            yt = tk.Frame(head, bg=c["SURFACE"])
            yt.pack(side="left", fill="x", expand=True)
            tk.Label(yt, text="YOU", bg=c["SURFACE"], fg=c["FG_MUTED"],
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")
            tk.Label(yt, text=core.operator_name(self_member.get("operator_id")),
                     bg=c["SURFACE"], fg=c["FG"],
                     font=("Segoe UI Semibold", 14), anchor="w").pack(
                fill="x", pady=(2, 0))

            yfacts = [
                ("Kills (operators / bots)",
                 f"{self_member.get('kill_operator', 0)} / {self_member.get('kill_other', 0)}"),
                ("Total kills", str(self_member.get("kill_count", 0))),
                ("Deaths", str(self_member.get("death", 0))),
                ("Assists", str(self_member.get("assist", 0))),
                ("Revives", str(self_member.get("revive", 0))),
                ("Rescues", str(self_member.get("rescue_count", 0))),
                ("Carried out",
                 core.fmt_money(_safe_int(self_member.get("carry_out_value"))
                                / core.CURRENCY_DIVISOR)),
                ("Survival", f"{self_member.get('survival_duration', '—')} min"),
            ]
            self._render_fact_rows(you_card.body, yfacts)

        if members:
            squad_card = Card(inner, c, padding=(22, 20), radius=12)
            squad_card.pack(fill="x", padx=32, pady=(0, 18))
            tk.Label(squad_card.body, text="SQUAD", bg=c["SURFACE"],
                     fg=c["FG_MUTED"], font=("Segoe UI", 9, "bold"),
                     anchor="w").pack(fill="x", pady=(0, 14))

            self._render_squad_table(squad_card.body, members)

        raw_card = Card(inner, c, padding=(22, 20), radius=12)
        raw_card.pack(fill="x", padx=32, pady=(0, 18))
        tk.Label(raw_card.body, text="RAW DETAIL PAYLOAD", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 12))

        try:
            pretty = json.dumps(detail, indent=2)
        except Exception:
            pretty = str(detail)

        raw_text = tk.Text(raw_card.body, height=10, bd=0,
                            bg=c["SURFACE_ALT"], fg=c["FG_DIM"],
                            insertbackground=c["FG"],
                            selectbackground=c["SELECT_BG"],
                            selectforeground=c["SELECT_FG"],
                            highlightthickness=0, wrap="word",
                            font=("Consolas", 9))
        raw_text.insert("1.0", pretty)
        raw_text.configure(state="disabled")
        raw_text.pack(fill="x")

        close_row = tk.Frame(inner, bg=c["BG_TOP"])
        close_row.pack(fill="x", padx=32, pady=(8, 32))
        close_btn = tk.Label(close_row, text="Close", bg=c["ACCENT"],
                             fg=c["BG_BOTTOM"],
                             font=("Segoe UI Semibold", 10),
                             padx=24, pady=10, cursor="hand2")
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>",
                       lambda e: self._close_detail(str(row["room_id"])))

    def _render_squad_table(self, parent, members):
        c = self.colors

        sorted_members = sorted(
            members,
            key=lambda mm: (not mm.get("is_self"),
                            -_safe_int(mm.get("carry_out_value"))),
        )

        table = tk.Frame(parent, bg=c["SURFACE"])
        table.pack(fill="x")

        columns = [
            ("player",   "PLAYER",   "w"),
            ("operator", "OPERATOR", "w"),
            ("ops",      "OPS",      "center"),
            ("bots",     "BOTS",     "center"),
            ("kills",    "KILLS",    "center"),
            ("deaths",   "DEATHS",   "center"),
            ("assists",  "ASSISTS",  "center"),
            ("rev",      "REV",      "center"),
            ("resc",     "RESC",     "center"),
            ("extract",  "EXTRACT",  "e"),
        ]
        min_widths = [120, 100, 46, 46, 52, 58, 62, 44, 48, 130]

        for col_idx, ((key, label, anchor), min_w) in enumerate(zip(columns, min_widths)):
            table.grid_columnconfigure(col_idx, minsize=min_w)
            tk.Label(table, text=label, bg=c["SURFACE"], fg=c["FG_MUTED"],
                     font=("Segoe UI", 8, "bold"), anchor=anchor).grid(
                row=0, column=col_idx, sticky="ew", padx=8, pady=(0, 12))

        for row_idx, mm in enumerate(sorted_members, start=1):
            is_self = bool(mm.get("is_self"))
            row_bg = c["SURFACE_ALT"] if is_self else c["SURFACE"]
            table.grid_rowconfigure(row_idx, minsize=40)

            nick = mm.get("nickname", "—")
            if is_self:
                nick = f"{nick}  ★"
            name_fg = c["ACCENT"] if is_self else c["FG"]
            name_font = ("Segoe UI Semibold", 10) if is_self else ("Segoe UI", 10)

            op_k = mm.get("kill_operator", 0)
            bot_k = mm.get("kill_other", 0)
            carry = _safe_int(mm.get("carry_out_value")) / core.CURRENCY_DIVISOR

            cells = [
                (nick, name_fg, name_font, "w"),
                (core.operator_name(mm.get("operator_id")), c["FG_DIM"], ("Segoe UI", 10), "w"),
                (str(op_k), c["POSITIVE"] if op_k else c["FG_MUTED"], ("Segoe UI Semibold", 10), "center"),
                (str(bot_k), c["FG_DIM"], ("Segoe UI", 10), "center"),
                (str(mm.get("kill_count", 0)), c["FG"], ("Segoe UI Semibold", 10), "center"),
                (str(mm.get("death", 0)), c["FG_DIM"], ("Segoe UI", 10), "center"),
                (str(mm.get("assist", 0)), c["FG_DIM"], ("Segoe UI", 10), "center"),
                (str(mm.get("revive", 0)), c["FG_DIM"], ("Segoe UI", 10), "center"),
                (str(mm.get("rescue_count", 0)), c["FG_DIM"], ("Segoe UI", 10), "center"),
                (core.fmt_money(carry), c["FG"], ("Segoe UI", 10), "e"),
            ]

            for col_idx, (text, fg, font, anchor) in enumerate(cells):
                tk.Label(table, text=text, bg=row_bg, fg=fg, font=font,
                         anchor=anchor).grid(
                    row=row_idx, column=col_idx, sticky="nsew", padx=8, pady=6)

    def _render_fact_rows(self, parent, rows):
        c = self.colors
        for label, value in rows:
            line = tk.Frame(parent, bg=c["SURFACE"])
            line.pack(fill="x", pady=4)
            tk.Label(line, text=label, bg=c["SURFACE"], fg=c["FG_DIM"],
                     font=("Segoe UI", 10), anchor="w").pack(side="left")
            tk.Label(line, text=str(value), bg=c["SURFACE"], fg=c["FG"],
                     font=("Segoe UI Semibold", 10),
                     anchor="e", justify="right").pack(side="right")

    def _start_detail_fetch(self, room_id):
        rid = str(room_id)
        if rid in self._detail_fetching:
            return

        self._detail_fetching.add(rid)

        def worker():
            try:
                detail = core.pull_match_detail(
                    rid, on_debug=lambda m: self.msg_queue.put(("debug", m)))
                self.msg_queue.put(("detail_done", (rid, detail, None)))
            except Exception as e:
                self.msg_queue.put(("detail_done", (rid, None, str(e))))

        threading.Thread(target=worker, daemon=True).start()

    def _on_detail_fetched(self, room_id, detail, error):
        rid = str(room_id)
        self._detail_fetching.discard(rid)

        if detail is not None:
            for r in self.rows:
                if str(r["room_id"]) == rid:
                    r["_detail"] = detail
                    r["has_detail"] = True
                    r.pop("_detail_error", None)
                    if detail.get("members"):
                        self_member = next(
                            (mm for mm in detail["members"] if mm.get("is_self")),
                            None)
                        if self_member:
                            r["operator_kills"] = self_member.get("kill_operator")
                            r["bot_kills"] = self_member.get("kill_other")
                    break

            for mm in detail.get("members", []):
                op_id = mm.get("operator_id")
                if op_id:
                    self._queue_avatar(op_id)
        else:
            for r in self.rows:
                if str(r["room_id"]) == rid:
                    r["_detail_error"] = error or "Unknown error."
                    break

        win = self._detail_windows.get(rid)
        if win is not None:
            try:
                if win.winfo_exists():
                    inner = self._find_inner_frame(win)
                    row = next((rr for rr in self.rows
                                if str(rr["room_id"]) == rid), None)
                    if inner and row:
                        canvas = self._find_canvas(win)
                        self._populate_detail(win, inner, row, canvas)
            except tk.TclError:
                pass

        if self.active_section == "matches":
            self._refresh_matches()

    def _find_inner_frame(self, win):
        try:
            outer = win.winfo_children()[0]
            for ch in outer.winfo_children():
                if isinstance(ch, tk.Canvas):
                    for cch in ch.winfo_children():
                        if isinstance(cch, tk.Frame):
                            return cch
        except (IndexError, tk.TclError):
            return None
        return None

    def _find_canvas(self, win):
        try:
            outer = win.winfo_children()[0]
            for ch in outer.winfo_children():
                if isinstance(ch, tk.Canvas):
                    return ch
        except (IndexError, tk.TclError):
            return None
        return None

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------
    def start_backfill(self):
        if self._backfill_active:
            return
        self._backfill_active = True
        self._backfill_stop = False
        core.set_debug(self.debug_var.get())
        threading.Thread(target=self._backfill_worker, daemon=True).start()

    def cancel_backfill(self):
        if self._backfill_active:
            self._backfill_stop = True
            self.set_status("Cancelling backfill…")

    def _backfill_worker(self):
        try:
            count = core.backfill_details(
                on_progress=lambda m: self.msg_queue.put(("backfill_progress", m)),
                on_debug=lambda m: self.msg_queue.put(("debug", m)),
                should_stop=lambda: self._backfill_stop,
            )
            self.msg_queue.put(("backfill_done", count))
        except Exception as e:
            self.msg_queue.put(("backfill_done", f"error: {e}"))

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------
    def _load_cached_data(self, initial=False):
        cache = core.load_cache()
        self.profile_data = cache.get("profile")
        self._update_sidebar_profile()

        if cache["matches"]:
            self.rows = core.build_rows(cache)
            self._rerender_section(self.active_section)
            if initial:
                self._last_refresh_ts = datetime.now()
                self.set_status(f"Loaded {len(self.rows)} cached match(es).")
        elif initial:
            self.set_status("No cached data yet. Click Log In, then Refresh.")

    def start_fetch(self, refresh_all: bool):
        if self.match_busy:
            return
        self._set_busy(match=True)
        core.set_debug(self.debug_var.get())
        threading.Thread(target=self._fetch_worker,
                         args=(refresh_all,), daemon=True).start()

    def _fetch_worker(self, refresh_all: bool):
        try:
            cache = core.pull_matches(
                refresh_all=refresh_all,
                on_progress=lambda m: self.msg_queue.put(("progress", m)),
                on_debug=lambda m: self.msg_queue.put(("debug", m)),
            )
            self.msg_queue.put(("done", cache))
        except Exception as e:
            self.msg_queue.put(("error", str(e)))

    def start_quick_refresh(self):
        if self.quick_busy:
            return
        self._set_busy(quick=True)
        core.set_debug(self.debug_var.get())
        threading.Thread(target=self._quick_worker, daemon=True).start()

    def _quick_worker(self):
        try:
            cache = core.refresh_today(
                on_progress=lambda m: self.msg_queue.put(("progress", m)),
                on_debug=lambda m: self.msg_queue.put(("debug", m)),
            )
            self.msg_queue.put(("done", cache))
        except Exception as e:
            self.msg_queue.put(("error", str(e)))

    def start_profile_fetch(self, silent=False):
        if self.profile_busy:
            return
        self._set_busy(profile=True)
        core.set_debug(self.debug_var.get())
        threading.Thread(target=self._profile_worker,
                         args=(silent,), daemon=True).start()

    def _profile_worker(self, silent: bool):
        try:
            data = core.pull_profile(
                on_progress=lambda m: self.msg_queue.put(("progress", m)),
                on_debug=lambda m: self.msg_queue.put(("debug", m)),
            )
            self.msg_queue.put(("profile_done", data))
        except Exception as e:
            kind = "error_silent" if silent else "error"
            self.msg_queue.put((kind, str(e)))

    def start_asset_calendar_fetch(self, silent=True):
        """silent=True by default: this card is a nice-to-have on the
        Overview tab, not a core feature, so a failure (not logged in
        yet, endpoint not captured during login, network hiccup) should
        just leave the card empty rather than popping an error dialog.
        The Settings button calls this with silent=False so a manual
        click gets real feedback instead of silently doing nothing."""
        if not core.have_credentials("assetcalendar"):
            if not silent:
                messagebox.showinfo(
                    "Not logged in",
                    "Click \"Log In (browser)\" first.", parent=self.root)
            return
        core.set_debug(self.debug_var.get())
        threading.Thread(target=self._asset_calendar_worker,
                         args=(silent,), daemon=True).start()

    def _asset_calendar_worker(self, silent: bool):
        try:
            core.pull_asset_calendar(
                on_debug=lambda m: self.msg_queue.put(("debug", m)),
            )
            self.msg_queue.put(("asset_calendar_done", None))
        except Exception as e:
            if not silent:
                self.msg_queue.put(("error", str(e)))

    def start_weekly_report_fetch(self, silent=True):
        """Same fail-soft shape as start_asset_calendar_fetch() above -
        see that one's docstring for the reasoning."""
        if not core.have_credentials("weeklyreport"):
            if not silent:
                messagebox.showinfo(
                    "Not logged in",
                    "Click \"Log In (browser)\" first.", parent=self.root)
            return
        core.set_debug(self.debug_var.get())
        threading.Thread(target=self._weekly_report_worker,
                         args=(silent,), daemon=True).start()

    def _weekly_report_worker(self, silent: bool):
        try:
            core.pull_weekly_report(
                on_debug=lambda m: self.msg_queue.put(("debug", m)),
            )
            self.msg_queue.put(("weekly_report_done", None))
        except Exception as e:
            if not silent:
                self.msg_queue.put(("error", str(e)))

    # ------------------------------------------------------------------
    # Community leaderboard
    # ------------------------------------------------------------------
    def _set_community_busy(self, busy: bool):
        self._community_busy = busy
        state = "disabled" if busy else "normal"
        for attr in ("community_link_btn", "community_sync_btn",
                     "community_unlink_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.set_state(state)

    def start_community_join(self):
        if self._community_busy:
            return
        if not comm.SERVER_URL:
            messagebox.showinfo(
                "Not configured",
                "This build isn't pointed at a community server yet. "
                "See server/README.md.", parent=self.root)
            return
        self._set_community_busy(True)
        self.set_status("Joining the community leaderboard…")
        nickname = None
        if self.profile_data:
            nickname = (self.profile_data.get("player_info") or {}).get("nickname")
        threading.Thread(target=self._community_join_worker,
                         args=(nickname,), daemon=True).start()

    def _community_join_worker(self, nickname):
        try:
            account = comm.opt_in(nickname=nickname, want_visible=True)
            self.msg_queue.put(("community_link_done", account))
        except Exception as e:
            self.msg_queue.put(("community_link_error", str(e)))

    def start_community_sync(self):
        if self._community_busy:
            return
        self._set_community_busy(True)
        self.set_status("Syncing stats to the community server…")
        threading.Thread(target=self._community_sync_worker, daemon=True).start()

    def _community_sync_worker(self):
        try:
            comm.sync_stats(self.rows, self.profile_data)
            self.msg_queue.put(("community_sync_done", None))
        except comm.LinkExpiredError as e:
            self.msg_queue.put(("community_link_expired", str(e)))
        except Exception as e:
            self.msg_queue.put(("community_sync_error", str(e)))

    def start_community_unlink(self):
        if self._community_busy:
            return
        if not messagebox.askyesno(
                "Leave leaderboard",
                "This removes your stats from the community server "
                "entirely and forgets your leaderboard entry on this "
                "device. Continue?", parent=self.root):
            return
        self._set_community_busy(True)
        threading.Thread(target=self._community_unlink_worker, daemon=True).start()

    def _community_unlink_worker(self):
        try:
            comm.leave(delete_on_server=True)
            self.msg_queue.put(("community_unlink_done", None))
        except Exception as e:
            self.msg_queue.put(("community_unlink_error", str(e)))

    def start_community_leaderboard_fetch(self):
        # Period is read here, on the UI thread, and carried through: the
        # person can flip the dropdown while a request is in flight, and a
        # slow reply for the board they just left must not overwrite the
        # one they're now looking at.
        threading.Thread(target=self._community_leaderboard_worker,
                         args=(self.community_period,), daemon=True).start()

    def _community_leaderboard_worker(self, period="all"):
        players, meta = comm.fetch_leaderboard_full(period=period)
        status = comm.fetch_my_status() if comm.is_linked() else None
        self.msg_queue.put(("community_leaderboard_done",
                            (players, status, meta, period)))

    def _on_community_period_change(self):
        self.community_period = ("daily" if self.community_period_var.get() == "Today"
                                 else "all")
        self._community_leaderboard = []   # don't show one board's rows under the other's title
        self._community_meta = {}
        self._render_community_leaderboard()
        self.start_community_leaderboard_fetch()

    @staticmethod
    def _parse_utc_iso(iso_utc):
        try:
            return datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _format_local_clock(cls, iso_utc, with_date=False):
        """A UTC timestamp as the viewer's own local time, e.g. '8:00 PM'
        (or 'Sep 28, 8:00 PM'); '' if unusable. The reset time means
        nothing to someone unless it's in their own clock."""
        dt = cls._parse_utc_iso(iso_utc)
        if dt is None:
            return ""
        local = dt.astimezone()
        clock = local.strftime("%I:%M %p").lstrip("0")
        return f"{local.strftime('%b')} {local.day}, {clock}" if with_date else clock

    def _daily_status_line(self, players, meta):
        """One sentence on why the signed-in player is or isn't on today's
        board, worked out from their own local data - so an empty board
        after a day of playing explains itself instead of looking broken.
        Empty string when there's nothing useful to add."""
        if not meta or not comm.is_linked():
            return ""        # no successful fetch, or not joined: nothing reliable to say
        me = ((comm.load_account() or {}).get("display_name") or "").strip().lower()
        if me and any((p.get("display_name") or "").strip().lower() == me
                      for p in players):
            return ""        # they're on it
        status = self._community_status
        if status and status.get("opted_in") is False:
            return ("You're not on this board because \"Show my stats on the "
                    "public leaderboard\" is off.")

        today = core.daily_stats_utc(self.rows, reset_hour=comm.daily_reset_hour())
        end = self._parse_utc_iso(meta.get("resets_at"))
        if today["matches"] == 0:
            local_today = sum(1 for r in self.rows
                              if r.get("date") == datetime.now().strftime("%Y-%m-%d"))
            if local_today and end:
                start_s = self._format_local_clock(
                    (end - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), True)
                end_s = self._format_local_clock(meta.get("resets_at"), True)
                many = local_today != 1
                return (f"Today's board covers {start_s} to {end_s} (your time). "
                        f"Your {local_today} match{'es' if many else ''} from earlier "
                        f"today {'were' if many else 'was'} played before {start_s}, so "
                        f"{'they count' if many else 'it counts'} toward yesterday's board.")
            return "You haven't played a match in today's window yet."
        n = today["matches"]
        return (f"You have {n} match{'es' if n != 1 else ''} in today's window but "
                "aren't on the board yet - your stats haven't synced. Click "
                "Sync My Stats Now.")

    @staticmethod
    def _format_time_until(iso_utc):
        """'2026-09-29T00:00:00Z' -> '5h 12m' (or '12m'); '' if unusable."""
        try:
            target = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return ""
        secs = int((target - datetime.now(timezone.utc)).total_seconds())
        if secs <= 0:
            return "moments"
        hours, rem = divmod(secs, 3600)
        return f"{hours}h {rem // 60}m" if hours else f"{max(1, rem // 60)}m"

    # ---- automatic stat updates while opted in ----
    def _community_startup_check(self):
        """Learn the opted-in state at launch (a small authenticated
        request) so auto-sync can work without the Community tab having
        been opened first. No-op unless already joined."""
        if comm.is_linked():
            self.start_community_leaderboard_fetch()

    def _maybe_auto_sync(self):
        """Called after match data refreshes. Sends only when ALL hold:
        the setting is on, this device is joined, the server says the
        player is opted in (known from the last status fetch - never
        assumed), no manual sync is running, and at least two minutes
        have passed since the last one (auto-refresh can fire in bursts)."""
        if not self.settings.get("community_auto_sync", True):
            return
        if not comm.is_linked() or self._community_busy:
            return
        if not (self._community_status or {}).get("opted_in"):
            return
        if (datetime.now() - self._last_auto_sync).total_seconds() < 120:
            return
        self._last_auto_sync = datetime.now()
        self._start_auto_sync()

    def _start_auto_sync(self):
        threading.Thread(target=self._auto_sync_worker, daemon=True).start()

    def _auto_sync_worker(self):
        try:
            comm.sync_stats(self.rows, self.profile_data)
            self.msg_queue.put(("community_auto_sync_done", None))
        except comm.LinkExpiredError:
            self.msg_queue.put(("community_auto_sync_expired", None))
        except Exception:
            pass  # background courtesy: never pop an error over someone's game

    def _on_community_autosync_toggle(self):
        value = bool(self.community_autosync_var.get())
        self.settings["community_auto_sync"] = value
        _save_settings({"community_auto_sync": value})

    def _on_autodownload_toggle(self):
        value = bool(self.autodownload_var.get())
        self.settings["auto_download_updates"] = value
        _save_settings({"auto_download_updates": value})

    def _start_update_check(self, manual: bool = False):
        """manual=True (the Settings button) always reports back, even
        when there's nothing new, so a click gets real feedback instead
        of silently doing nothing. The silent startup check only ever
        speaks up when there's actually an update."""
        if manual and hasattr(self, "update_status_label"):
            self.update_status_label.configure(
                text="Checking...", fg=self.colors["FG_MUTED"])
        threading.Thread(target=self._update_check_worker,
                         args=(manual,), daemon=True).start()

    def _maybe_auto_download_update(self, info: dict):
        """Starts a background download once an update's been detected -
        never silently INSTALLS anything (that always needs an explicit
        click on the "ready" dialog or the Settings button), just fetches
        and stages it ahead of time so accepting the install is instant.
        """
        asset_url = (info or {}).get("asset_url") or ""
        latest = (info or {}).get("latest_version", "")
        if not asset_url or not self.settings.get("auto_download_updates", True):
            return
        if not updater_mod.IS_WINDOWS or not getattr(sys, "frozen", False):
            return  # nothing to self-install on this platform/dev run
        if self._update_download_busy:
            return
        if self._staged_update and self._staged_update[0] == latest:
            return  # already downloaded this exact version
        self._update_download_busy = True
        threading.Thread(target=self._update_download_worker,
                         args=(asset_url, latest), daemon=True).start()

    def _update_download_worker(self, asset_url: str, version: str):
        last_reported = [0]

        def progress(downloaded, total):
            # Throttled to roughly once per 2MB - on_progress fires every
            # 256KB chunk, and posting a status-bar update that often
            # would flood the queue for no visible benefit.
            if downloaded - last_reported[0] >= 2_000_000 or downloaded == total:
                last_reported[0] = downloaded
                self.msg_queue.put(("update_download_progress", (downloaded, total)))
        try:
            zip_path = updater_mod.download_update(asset_url, on_progress=progress)
            staged = updater_mod.stage_update(zip_path)
            zip_path.unlink(missing_ok=True)
            self.msg_queue.put(("update_ready", (version, staged)))
        except updater_mod.UpdateError as e:
            self.msg_queue.put(("update_download_failed", str(e)))
        except Exception as e:
            self.msg_queue.put(("update_download_failed", f"{type(e).__name__}: {e}"))

    def _show_update_ready_dialog(self, version: str):
        try:
            c = self.colors
            win = tk.Toplevel(self.root)
            win.title("Update Ready")
            win.configure(bg=c["BG_TOP"])
            win.resizable(False, False)
            win.transient(self.root)
            win.geometry("420x1")

            body = tk.Frame(win, bg=c["BG_TOP"], padx=28, pady=24)
            body.pack(fill="both", expand=True)
            tk.Label(body, text=f"Version {version} is ready to install",
                     bg=c["BG_TOP"], fg=c["FG"], font=("Segoe UI Semibold", 14),
                     anchor="w").pack(fill="x", pady=(0, 12))
            tk.Label(body, text="The app will close and reopen automatically. "
                                "This only takes a few seconds.",
                     bg=c["BG_TOP"], fg=c["FG_DIM"], font=("Segoe UI", 10),
                     anchor="w", justify="left", wraplength=364).pack(
                fill="x", pady=(0, 20))

            buttons = tk.Frame(body, bg=c["BG_TOP"])
            buttons.pack(anchor="e")

            def later():
                win.destroy()  # the staged update stays available - see Settings

            def install_now():
                win.destroy()
                self._install_staged_update()

            self._pill_button(buttons, "Later", later).pack(side="left", padx=(0, 8))
            self._pill_button(buttons, "Restart & Update", install_now,
                              primary=True).pack(side="left")

            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass

    def _install_staged_update(self):
        if not self._staged_update:
            return
        _version, staged_path = self._staged_update
        try:
            updater_mod.launch_installer_and_exit(staged_path)
        except updater_mod.UpdateError as e:
            messagebox.showerror("Couldn't start the update", str(e), parent=self.root)
            return
        self._on_close()  # normal clean shutdown; the launched script relaunches us

    def _update_check_worker(self, manual: bool):
        status, detail = comm.check_for_update_status()
        self.msg_queue.put(("update_check_done", (status, detail, manual)))

    def _on_community_optin_toggle(self):
        value = self.community_optin_var.get()
        threading.Thread(target=self._community_optin_worker,
                         args=(value,), daemon=True).start()

    def _community_optin_worker(self, value: bool):
        try:
            comm.set_opt_in(value)
            self.msg_queue.put(("community_optin_done", value))
        except comm.LinkExpiredError as e:
            self.msg_queue.put(("community_link_expired", str(e)))
        except Exception as e:
            self.msg_queue.put(("community_optin_error", str(e)))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()

                if kind == "progress":
                    self.set_status(payload)
                elif kind == "auto_progress":
                    self.set_status(payload)
                elif kind == "debug":
                    self._append_debug(payload)
                elif kind == "avatar_bytes":
                    op_id, data = payload
                    self._avatar_pending.discard(op_id)
                    self._install_avatar_bytes(op_id, data)
                    if self.active_section == "matches":
                        self._last_avatar_count = -1
                elif kind == "item_icon_bytes":
                    item_id, data = payload
                    self._item_icon_pending.discard(item_id)
                    self._install_item_icon_bytes(item_id, data)
                    if self.active_section == "overview":
                        self._render_high_value_items()
                        self._render_weekly_highlights()
                elif kind == "done":
                    self.rows = core.build_rows(payload)
                    self._last_refresh_ts = datetime.now()
                    self._rerender_section(self.active_section)
                    self.set_status(f"Ready. Last updated "
                                    f"{datetime.now().strftime('%H:%M:%S')}.  "
                                    f"[creds: {core.credential_source()}]")
                    self._set_busy(match=False, quick=False)
                    self._refresh_data_stats()
                    self._schedule_next_auto_refresh()
                    self._maybe_auto_sync()
                elif kind == "auto_done":
                    self._on_auto_refresh_done(payload)
                elif kind == "auto_error":
                    self._on_auto_refresh_error(payload)
                elif kind == "profile_done":
                    self.profile_data = payload
                    if self.active_section == "profile":
                        self._render_profile()
                    self._update_sidebar_profile()
                    self.set_status(f"Profile updated "
                                    f"{datetime.now().strftime('%H:%M:%S')}.")
                    self._set_busy(profile=False)
                elif kind == "asset_calendar_done":
                    self._high_value_items = core.recent_high_value_items()
                    if self.active_section == "overview":
                        self._render_high_value_items()
                    self.set_status(f"High-value items updated "
                                    f"{datetime.now().strftime('%H:%M:%S')}.")
                elif kind == "weekly_report_done":
                    self._weekly_highlights = core.weekly_highlights()
                    if self.active_section == "overview":
                        self._render_weekly_highlights()
                    self.set_status(f"Weekly report updated "
                                    f"{datetime.now().strftime('%H:%M:%S')}.")
                elif kind == "detail_done":
                    rid, detail, error = payload
                    self._on_detail_fetched(rid, detail, error)
                elif kind == "backfill_progress":
                    self.set_status(payload)
                elif kind == "backfill_done":
                    self._backfill_active = False
                    self.set_status(f"Backfill complete: {payload}")
                    if self.active_section == "matches":
                        self._refresh_matches()
                elif kind == "login_done":
                    self._on_login_done()
                elif kind == "login_error":
                    self._login_busy = False
                    messagebox.showerror("Login failed", payload)
                    self.set_status("Login failed.")
                    self._update_busy_state()
                elif kind == "error":
                    messagebox.showerror("Fetch failed", payload)
                    self.set_status("Error — see message box.")
                    self._set_busy(match=False, profile=False, quick=False)
                    self._schedule_next_auto_refresh()
                elif kind == "error_silent":
                    self.set_status(f"Profile fetch failed: {payload}")
                    self._set_busy(profile=False)
                elif kind == "community_link_done":
                    self._set_community_busy(False)
                    self._community_status = None
                    self._refresh_community_status_ui()
                    self.set_status(
                        f"Joined the community leaderboard as "
                        f"{payload.get('display_name', 'Player')}.")
                    self.start_community_leaderboard_fetch()
                elif kind == "community_link_error":
                    self._set_community_busy(False)
                    messagebox.showerror("Couldn't join leaderboard", payload,
                                         parent=self.root)
                    self.set_status("Joining the leaderboard failed — see message box.")
                elif kind == "community_sync_done":
                    self._set_community_busy(False)
                    self.set_status(f"Synced to community server "
                                    f"{datetime.now().strftime('%H:%M:%S')}.")
                    self.start_community_leaderboard_fetch()
                elif kind == "community_sync_error":
                    self._set_community_busy(False)
                    messagebox.showerror("Sync failed", payload, parent=self.root)
                    self.set_status("Community sync failed — see message box.")
                elif kind == "community_link_expired":
                    # comm._request_authed() already cleared the stale
                    # local account file by the time this fires - just
                    # reflect that in the UI so "Linked as X" doesn't
                    # keep showing a link that no longer works.
                    self._set_community_busy(False)
                    self._community_status = None
                    self._refresh_community_status_ui()
                    messagebox.showinfo("Community link expired", payload,
                                        parent=self.root)
                    self.set_status("Community link expired — click "
                                    "\"Join Leaderboard\" to relink.")
                elif kind == "community_unlink_done":
                    self._set_community_busy(False)
                    self._community_status = None
                    self._community_leaderboard = []
                    self._refresh_community_status_ui()
                    self._render_community_leaderboard()
                    self.set_status("Unlinked community account.")
                elif kind == "community_unlink_error":
                    self._set_community_busy(False)
                    messagebox.showerror("Unlink failed", payload, parent=self.root)
                elif kind == "community_leaderboard_done":
                    players, status, meta, period = payload
                    # status is about the account, not the board - always keep it
                    self._community_status = status
                    if period == self.community_period:
                        self._community_leaderboard = players
                        self._community_meta = meta
                        self._render_community_leaderboard()
                    self._refresh_community_status_ui()
                    # First moment the opted-in state is known after launch:
                    # send current stats now, rather than waiting for the
                    # next match refresh. (Otherwise updating the app and
                    # opening the board sent nothing, so the daily board
                    # stayed empty.) Same gates + 2-minute throttle apply.
                    self._maybe_auto_sync()
                elif kind == "community_auto_sync_done":
                    if self.active_section == "community":
                        self.start_community_leaderboard_fetch()
                elif kind == "community_auto_sync_expired":
                    self._community_status = None
                    self._refresh_community_status_ui()
                elif kind == "update_check_done":
                    status, detail, manual = payload
                    if status == "update":
                        info = detail
                        latest = info.get("latest_version", "?")
                        url = info.get("download_url") or ""
                        msg = f"Version {latest} is available (you're on {APP_VERSION})."
                        if url:
                            msg += f"\n{url}"
                        self._show_toast("Update Available", msg, duration_ms=10000)
                        if hasattr(self, "update_status_label"):
                            self.update_status_label.configure(
                                text=msg, fg=self.colors["ACCENT_HI"])
                        self._maybe_auto_download_update(info)
                    elif manual and hasattr(self, "update_status_label"):
                        if status == "current":
                            self.update_status_label.configure(
                                text=f"You're on the latest version ({APP_VERSION}).",
                                fg=self.colors["POSITIVE"])
                        elif status == "failed":
                            self.update_status_label.configure(
                                text=f"Couldn't check for updates ({detail}). "
                                     "Check UPDATE_INFO_URL in delta_force_config.py.",
                                fg=self.colors["NEGATIVE"])
                        else:
                            self.update_status_label.configure(
                                text="Update checking isn't set up in this build.",
                                fg=self.colors["FG_MUTED"])
                elif kind == "update_download_progress":
                    downloaded, total = payload
                    mb = downloaded / 1_000_000
                    if total:
                        self.set_status(f"Downloading update... {mb:.1f} / "
                                        f"{total / 1_000_000:.1f} MB")
                    else:
                        self.set_status(f"Downloading update... {mb:.1f} MB")
                elif kind == "update_ready":
                    version, staged_path = payload
                    self._update_download_busy = False
                    self._staged_update = (version, staged_path)
                    self.set_status(f"Update {version} downloaded and ready to install.")
                    if hasattr(self, "install_update_btn"):
                        self.install_update_btn.set_state("normal")
                    self._show_update_ready_dialog(version)
                elif kind == "update_download_failed":
                    self._update_download_busy = False
                    self.set_status(f"Update download failed: {payload}")
                elif kind == "community_optin_done":
                    self.set_status("Leaderboard visibility updated.")
                    self.start_community_leaderboard_fetch()
                elif kind == "community_optin_error":
                    messagebox.showerror("Couldn't update", payload, parent=self.root)
                    # Refetch so the checkbox reflects the server's actual
                    # state rather than the click that just failed.
                    self.start_community_leaderboard_fetch()
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    # ------------------------------------------------------------------
    # Busy state
    # ------------------------------------------------------------------
    def _set_busy(self, match=None, profile=None, quick=None):
        if match is not None:
            self.match_busy = match
        if profile is not None:
            self.profile_busy = profile
        if quick is not None:
            self.quick_busy = quick
        self._update_busy_state()

    def _update_busy_state(self):
        if self.match_busy or self.profile_busy or self.quick_busy or self._login_busy:
            try:
                self.progress.start(12)
            except tk.TclError:
                pass
        else:
            try:
                self.progress.stop()
            except tk.TclError:
                pass

        def state(flag):
            return "normal" if not flag else "disabled"

        self.login_btn.set_state(state(self._login_busy))
        self.refresh_btn.set_state(state(self.match_busy))
        self.refresh_all_btn.set_state(state(self.match_busy))
        self.refresh_profile_btn.set_state(state(self.profile_busy))
        self.quick_btn.set_state(state(self.quick_busy))
        self.backfill_btn.set_state(
            "disabled" if self._backfill_active else "normal")
        self.cancel_backfill_btn.set_state(
            "normal" if self._backfill_active else "disabled")

    def _append_debug(self, msg: str):
        self.debug_text.configure(state="normal")
        self.debug_text.insert("end", msg + "\n")
        self.debug_text.see("end")
        self.debug_text.configure(state="disabled")

    def set_status(self, msg: str):
        self.status_label.configure(text=msg)

    def do_export(self):
        if not self.rows:
            messagebox.showinfo("Nothing to export", "Fetch some data first.")
            return
        paths = core.export_csv(self.rows, self.summaries)
        messagebox.showinfo("Exported",
                            "Wrote:\n" + "\n".join(p.name for p in paths))

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Overlay / global hotkey / system tray
    # ------------------------------------------------------------------
    def _toggle_overlay(self):
        try:
            snapshot = core.overlay_snapshot(
                self.rows, core.load_cache(), session_start=self.session_start)
            self.overlay.toggle(snapshot)
        except Exception:
            pass  # the overlay is a glance-only convenience, never worth a crash

    def _refresh_overlay_if_visible(self):
        """Pushes a fresh snapshot to the overlay if it's currently on
        screen, without toggling it open/closed. Called when starting or
        ending a session so the overlay switches between SESSION/TODAY
        mode immediately - otherwise it would keep showing whatever mode
        was active when it was last opened until someone happened to
        toggle it again."""
        if not self.overlay.is_visible():
            return
        try:
            snapshot = core.overlay_snapshot(
                self.rows, core.load_cache(), session_start=self.session_start)
            self.overlay.show(snapshot)
        except Exception:
            pass

    def _start_hotkey_listener(self):
        mods, vk = overlay_mod.hotkey_by_label(
            self.settings.get("overlay_hotkey", overlay_mod.DEFAULT_HOTKEY_LABEL))
        self.hotkey_listener.start(mods, vk)

    def _stop_hotkey_listener(self):
        self.hotkey_listener.stop()

    def _on_window_minimized(self, event):
        if event.widget is not self.root:
            return
        if self.root.state() != "iconic":
            return
        if not self.settings.get("minimize_to_tray"):
            return
        if not overlay_mod.TrayIcon.available():
            return
        # Deferred slightly: acting synchronously inside the <Unmap>
        # handler itself can race with Windows' own minimize animation.
        self.root.after(10, self._hide_to_tray)

    def _hide_to_tray(self):
        self.root.withdraw()
        if not self._tray_active:
            self.tray_icon.start()
            self._tray_active = True

    def _restore_from_tray(self):
        if self._tray_active:
            self.tray_icon.stop()
            self._tray_active = False
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _exit_app(self):
        self._on_close()

    def _on_close(self):
        """Cancel timers, close Discord RPC, stop the hotkey listener and
        tray icon (both are background threads - the app process
        wouldn't actually exit with one still running), destroy window."""
        self._cancel_auto_refresh()
        if self._status_reset_after_id is not None:
            try:
                self.root.after_cancel(self._status_reset_after_id)
            except tk.TclError:
                pass
            self._status_reset_after_id = None
        try:
            self.hotkey_listener.stop()
        except Exception:
            pass
        try:
            self.tray_icon.stop()
        except Exception:
            pass
        try:
            self.overlay.hide()
        except Exception:
            pass
        try:
            import delta_force_rpc
            delta_force_rpc.disable()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _remove_startup_and_exit():
    """DeltaForceTracker.exe --remove-startup: undoes the Windows startup
    entry without opening the app, for someone who wants to delete the
    folder and not leave that entry behind."""
    ok = startup_mod.set_enabled(False)
    _save_settings({"start_on_boot": False})
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(
        "DF Tracker",
        "The Windows startup entry was removed." if ok else
        "Couldn't change the startup entry (it may not exist, or this "
        "isn't Windows).")
    root.destroy()


def main():
    if "--remove-startup" in sys.argv:
        _remove_startup_and_exit()
        return

    # Must happen before Tk() creates the actual window - Windows decides
    # whether this process gets DPI-scaled (blurry, since it's really
    # just bitmap-stretched) or renders at native resolution based on
    # this flag, and that decision is made at window-creation time, not
    # adjustable after the fact. No effect on non-Windows platforms;
    # never allowed to block startup over a cosmetic detail if it fails.
    if sys.platform.startswith("win"):
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()  # older Windows fallback
        except Exception:
            pass

    root = tk.Tk()
    TrackerApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            import tkinter.messagebox as mb
            mb.showerror("Startup error", str(e))
        except Exception:
            pass
        print(f"[fatal] {e}", file=sys.stderr)
        raise