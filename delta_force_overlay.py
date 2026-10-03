"""
Delta Force — desktop overlay.

A small always-on-top HUD (today's profit/loss, best single raid, top 3
recent high-value items) that a player can toggle with a global hotkey
without alt-tabbing out of the game, plus a system tray icon so the main
window can be minimized out of the way entirely.

Windows-only. Both pieces here are genuinely Windows-specific concepts -
a hotkey that fires no matter which application has focus, and a
notification-area tray icon - so on macOS/Linux this module degrades to
inert no-ops (HotkeyListener silently fails to register, TrayIcon simply
doesn't start) rather than pretending to support platforms it doesn't.

Why RegisterHotKey (a Win32 API, used here via ctypes) instead of a
library like `keyboard` or `pynput`:
    Those typically work via a low-level keyboard hook (SetWindowsHookEx
    + WH_KEYBOARD_LL) that observes every keystroke system-wide before
    deciding whether to act on it - the exact mechanism a keylogger
    uses, which is a real part of why antivirus/Smart App Control
    heuristics are already a live concern for this app (see BUILD.md).
    RegisterHotKey is different and narrower: you register one specific
    key combination, and Windows itself only delivers a message when
    THAT combination is pressed. It's what ordinary Windows software
    (clipboard managers, push-to-talk apps, screenshot tools) already
    uses for global shortcuts, and reads very differently to a heuristic
    scanner than a raw keyboard hook does.

OverlayWindow itself is plain Tkinter and has no OS-specific dependency -
it's just a borderless Toplevel. Only the hotkey capture and the
click-through window styling need Windows APIs.
"""

import ctypes
import sys
import threading
import time
import tkinter as tk

IS_WINDOWS = sys.platform.startswith("win")

# --- Win32 hotkey constants (only meaningful on Windows) ---
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
WM_HOTKEY = 0x0312
_HOTKEY_ID = 0xBEEF  # arbitrary, only needs to be unique within this process
_SEARCH_HOTKEY_ID = 0xBEE5  # the Player Lookup overlay's own, separate hotkey

_VK = {
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "D": 0x44, "O": 0x4F, "L": 0x4C,
}

# Preset choices offered in Settings, rather than free-form key capture -
# far less code, and sidesteps having to handle every possible key/locale
# edge case for a feature that just needs a handful of safe, memorable
# defaults. (label, modifiers, vk_name) - vk_name indexes into _VK above.
HOTKEY_PRESETS = [
    ("Ctrl+Shift+D", MOD_CONTROL | MOD_SHIFT, "D"),
    ("Ctrl+Shift+O", MOD_CONTROL | MOD_SHIFT, "O"),
    ("Ctrl+Alt+D", MOD_CONTROL | MOD_ALT, "D"),
    ("Ctrl+Shift+F9", MOD_CONTROL | MOD_SHIFT, "F9"),
    ("Ctrl+Shift+F10", MOD_CONTROL | MOD_SHIFT, "F10"),
]
DEFAULT_HOTKEY_LABEL = "Ctrl+Shift+D"


def hotkey_by_label(label: str):
    """Returns (modifiers, vk_code) for a HOTKEY_PRESETS label, falling
    back to the default if the label is unrecognized (e.g. an old
    settings file referencing a preset that no longer exists)."""
    for lbl, mods, vk_name in HOTKEY_PRESETS:
        if lbl == label:
            return mods, _VK[vk_name]
    for lbl, mods, vk_name in HOTKEY_PRESETS:
        if lbl == DEFAULT_HOTKEY_LABEL:
            return mods, _VK[vk_name]
    return MOD_CONTROL | MOD_SHIFT, _VK["D"]


def resolve_hotkey(settings: dict, prefix: str = "overlay",
                   default_label: str = DEFAULT_HOTKEY_LABEL):
    """Returns (modifiers, vk_code, display_label) for whatever's
    currently configured - a preset picked from the dropdown, or a
    custom combination someone recorded themselves (see
    delta_force_gui._show_hotkey_capture_dialog). settings["<prefix>_hotkey_source"]
    ("preset" or "custom") decides which one is actually in effect,
    since a person can have both a preset selection AND a previously
    recorded custom combo sitting in settings at once, and only one can
    be the real answer.

    prefix lets this same function serve more than one independent
    hotkey (the overlay's own, and the Player Lookup search overlay's) -
    each reads and writes its own "<prefix>_hotkey*" settings keys,
    entirely independent of the other's. default_label matters for the
    same reason: two features both silently defaulting to the exact
    same combination would be a confusing way to discover that only one
    of them can actually hold it.

    Falls back to the default preset if the requested source's data is
    missing or malformed (e.g. a settings file from before custom
    hotkeys existed, or a corrupted value) - the overlay should always
    end up with SOME working hotkey rather than none.
    """
    source = settings.get(f"{prefix}_hotkey_source", "preset")
    if source == "custom":
        mods = settings.get(f"{prefix}_hotkey_mods")
        vk = settings.get(f"{prefix}_hotkey_vk")
        label = settings.get(f"{prefix}_hotkey_custom_label")
        if isinstance(mods, int) and isinstance(vk, int) and mods and vk and label:
            return mods, vk, label
        # fall through to the preset default below - malformed custom data

    label = settings.get(f"{prefix}_hotkey", default_label)
    mods, vk = hotkey_by_label(label)
    for lbl, _, _ in HOTKEY_PRESETS:
        if lbl == label:
            return mods, vk, label
    return mods, vk, default_label


# ---------------------------------------------------------------------
# Global hotkey listener
# ---------------------------------------------------------------------
class HotkeyListener:
    """Runs RegisterHotKey plus a Win32 message loop on its own
    background thread, calling on_trigger() every time the registered
    combination fires.

    RegisterHotKey ties the registration to the calling THREAD, not the
    process - that's exactly why this needs a dedicated thread with its
    own message loop, rather than registering from whichever thread
    happens to call start(). That per-thread scoping also means two
    separate HotkeyListener instances (each on their own thread, as this
    class always runs) could safely reuse the same numeric hotkey_id
    without colliding - but relying on that rather than just giving each
    instance its own id is the kind of subtlety that's easy to forget
    later, so hotkey_id is a constructor argument, not a shared module
    constant, even though the single-overlay-hotkey case this class
    originally shipped for never needed more than one.

    on_trigger() runs on this background thread. It must never touch
    Tkinter widgets directly (Tkinter isn't thread-safe) - callers
    should hop back to the main thread themselves, e.g. via
    root.after() or the app's existing msg_queue pattern.
    """

    def __init__(self, on_trigger, hotkey_id: int = _HOTKEY_ID):
        self._on_trigger = on_trigger
        self._hotkey_id = hotkey_id
        self._thread = None
        self._stop_event = threading.Event()
        self._registered_ok = False
        self.last_error = None

    def start(self, modifiers: int, vk_code: int):
        self.stop()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, args=(modifiers, vk_code), daemon=True)
        self._thread.start()
        # Give registration a brief moment so callers checking
        # is_registered()/last_error right after start() get a real
        # answer instead of always seeing the pre-registration state.
        time.sleep(0.15)

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=2)
        self._thread = None
        self._registered_ok = False

    def is_registered(self) -> bool:
        return self._registered_ok

    def _run(self, modifiers, vk_code):
        if not IS_WINDOWS:
            self.last_error = "Global hotkeys are only supported on Windows."
            return
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
        except Exception as e:
            self.last_error = f"Couldn't access Windows hotkey APIs: {e}"
            return

        if not user32.RegisterHotKey(None, self._hotkey_id, modifiers, vk_code):
            self.last_error = (
                "Couldn't register that hotkey — another application "
                "may already be using it. Pick a different one.")
            return

        self._registered_ok = True
        self.last_error = None
        try:
            msg = wintypes.MSG()
            while not self._stop_event.is_set():
                # PeekMessage (non-blocking), not GetMessage (blocks
                # forever) - specifically so _stop_event actually gets
                # checked and this thread can exit cleanly when the app
                # closes or the hotkey changes, instead of sitting
                # blocked on a message that may never come.
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                        try:
                            self._on_trigger()
                        except Exception:
                            pass
                else:
                    time.sleep(0.05)
        finally:
            user32.UnregisterHotKey(None, self._hotkey_id)
            self._registered_ok = False


# ---------------------------------------------------------------------
# The overlay HUD itself
# ---------------------------------------------------------------------
class OverlayWindow:
    """Borderless, always-on-top, click-through (on Windows) popup
    showing a snapshot: today's profit/loss, the best single raid ever,
    and the top 3 recent high-value items.

    Purely a view over data the caller already has - show()/toggle()
    take a snapshot dict (see core.overlay_snapshot()) rather than
    fetching anything themselves, so displaying it is instant and never
    touches the network or blocks the UI thread.
    """

    def __init__(self, root, colors):
        self._root = root
        self._colors = colors
        self._win = None
        self._net_title_label = None
        self._today_label = None
        self._best_label = None
        self._item_labels = []

    def is_visible(self) -> bool:
        return self._win is not None and bool(self._win.winfo_exists())

    def toggle(self, snapshot: dict):
        if self.is_visible():
            self.hide()
        else:
            self.show(snapshot)

    def show(self, snapshot: dict):
        import delta_force_core as core
        if not self.is_visible():
            self._build()
        c = self._colors

        mode = snapshot.get("mode", "daily")
        self._net_title_label.configure(
            text="SESSION" if mode == "session" else "TODAY")

        net = snapshot.get("net", 0)
        self._today_label.configure(
            text=core.fmt_money(net),
            fg=c["POSITIVE"] if net >= 0 else c["NEGATIVE"])

        best = snapshot.get("best_match")
        if best:
            self._best_label.configure(
                text=f"{core.fmt_money(best['net_income'])} · {best['map_name']}",
                fg=c["POSITIVE"] if best["net_income"] >= 0 else c["NEGATIVE"])
        else:
            self._best_label.configure(text="—", fg=c["FG_MUTED"])

        items = snapshot.get("top_items") or []
        for i, (name_lbl, val_lbl) in enumerate(self._item_labels):
            if i < len(items):
                it = items[i]
                name_lbl.configure(text=it["name"], fg=c["FG"])
                val_lbl.configure(text=core.fmt_money(it["total_value"]))
            else:
                name_lbl.configure(text="—", fg=c["FG_MUTED"])
                val_lbl.configure(text="")

        self._win.update_idletasks()
        self._position()
        self._win.deiconify()
        self._make_click_through()

    def hide(self):
        if self.is_visible():
            self._win.destroy()
        self._win = None
        self._net_title_label = None
        self._today_label = None
        self._best_label = None
        self._item_labels = []

    def _build(self):
        c = self._colors
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        if not IS_WINDOWS:
            # Windows handles alpha itself, atomically, alongside the
            # click-through styling in _make_click_through() - see the
            # comment there for why setting it here via Tkinter and
            # later modifying GWL_EXSTYLE doesn't work correctly.
            try:
                win.attributes("-alpha", 0.94)
            except tk.TclError:
                pass
        win.configure(bg=c["BORDER"])

        body = tk.Frame(win, bg=c["SURFACE"], padx=16, pady=14)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(body, bg=c["SURFACE"])
        header.pack(fill="x")
        tk.Label(header, text="◈", bg=c["SURFACE"], fg=c["ACCENT"],
                 font=("Segoe UI", 12)).pack(side="left")
        tk.Label(header, text=" Delta Force Tracker", bg=c["SURFACE"],
                 fg=c["FG"], font=("Segoe UI Semibold", 10)).pack(side="left")

        tk.Frame(body, bg=c["BORDER_SOFT"], height=1).pack(fill="x", pady=(10, 10))

        self._net_title_label, self._today_label = self._stat_row(body, c, "TODAY")
        _, self._best_label = self._stat_row(body, c, "BEST RAID")

        tk.Label(body, text="RECENT LOOT", bg=c["SURFACE"], fg=c["FG_MUTED"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
            fill="x", pady=(12, 4))
        self._item_labels = []
        for _ in range(3):
            row = tk.Frame(body, bg=c["SURFACE"])
            row.pack(fill="x", pady=1)
            name_lbl = tk.Label(row, text="—", bg=c["SURFACE"], fg=c["FG"],
                                font=("Segoe UI", 9), anchor="w")
            name_lbl.pack(side="left", fill="x", expand=True)
            val_lbl = tk.Label(row, text="", bg=c["SURFACE"], fg=c["POSITIVE"],
                               font=("Segoe UI", 9), anchor="e")
            val_lbl.pack(side="right")
            self._item_labels.append((name_lbl, val_lbl))

        self._win = win

    def _stat_row(self, parent, c, title):
        row = tk.Frame(parent, bg=c["SURFACE"])
        row.pack(fill="x", pady=(0, 6))
        title_lbl = tk.Label(row, text=title, bg=c["SURFACE"], fg=c["FG_MUTED"],
                             font=("Segoe UI", 8, "bold"), anchor="w")
        title_lbl.pack(fill="x")
        value_lbl = tk.Label(row, text="—", bg=c["SURFACE"], fg=c["FG"],
                             font=("Segoe UI Semibold", 13), anchor="w")
        value_lbl.pack(fill="x")
        return title_lbl, value_lbl

    def _position(self):
        # Top-right corner with a small margin: clear of the taskbar/
        # tray (bottom corner) and out of the way of most games' own
        # HUD elements, which more often sit along the bottom or left
        # edge of the screen.
        w = self._win.winfo_reqwidth()
        h = self._win.winfo_reqheight()
        sw = self._win.winfo_screenwidth()
        x = sw - w - 24
        y = 24
        self._win.geometry(f"{w}x{h}+{x}+{y}")

    def _make_click_through(self):
        """Windows only: lets clicks pass through to whatever's beneath
        the overlay (the game) instead of the overlay intercepting them.
        Correct behavior for a glance-only HUD with no interactive
        controls - the only way to dismiss it is the hotkey, by design.
        No-op, harmlessly, everywhere else.

        Also where alpha transparency gets set on Windows (not in
        _build(), via Tkinter's own `-alpha`) - confirmed by testing
        that setting `-alpha` there and then modifying GWL_EXSTYLE here
        renders the window as a blank, content-less box instead of its
        actual content. Windows resets a layered window's alpha/
        composited-content state on any SetWindowLongW call that
        touches GWL_EXSTYLE, so the alpha has to be (re-)established
        with SetLayeredWindowAttributes immediately after, in the same
        place, or the window ends up in that reset state."""
        if not IS_WINDOWS:
            return
        try:
            hwnd = self._win.winfo_id()
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOOLWINDOW = 0x00000080  # also keeps it off the taskbar/alt-tab
            LWA_ALPHA = 0x00000002

            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)
            user32.SetLayeredWindowAttributes(hwnd, 0, 240, LWA_ALPHA)  # 240/255 ~= 0.94
        except Exception:
            pass


class SearchOverlayWindow:
    """A second, independent overlay for looking up another player by
    name while in-game (teammates, an opponent who just killed you while
    you spectate, anyone) - deliberately NOT built like OverlayWindow,
    because it needs to accept typed input, which that class's design
    explicitly never allows (see its own _make_click_through docstring:
    "the only way to dismiss it is the hotkey, by design"). This class
    is the one considered exception to that rule elsewhere in this
    module, not a loosening of it - see _make_interactive below for
    exactly what that exception involves on Windows.

    Opened and closed by its own hotkey (see overlay_mod.resolve_hotkey's
    prefix="search", and delta_force_gui.py's search_hotkey_listener),
    entirely independent of the main stats overlay - either can be open
    without the other, and they're positioned in different screen
    corners so they don't visually collide if both are.

    search_fn isn't called from here directly - this class only ever
    renders whatever state the caller tells it to (show_searching(),
    show_result(), show_error()), via the exact same thread-safety
    boundary as OverlayWindow: nothing in here reaches across to a
    background thread, and nothing outside should touch these widgets
    off the main thread.
    """

    WIDTH = 360

    def __init__(self, root, colors):
        self._root = root
        self._colors = colors
        self._win = None
        self._entry_var = None
        self._entry = None
        self._status_label = None
        self._results_frame = None
        self._on_submit = None  # set per-show(), called with the typed name on Enter

    def is_visible(self) -> bool:
        return self._win is not None and bool(self._win.winfo_exists())

    def toggle(self, on_submit):
        if self.is_visible():
            self.hide()
        else:
            self.show(on_submit)

    def show(self, on_submit):
        self._on_submit = on_submit
        if not self.is_visible():
            self._build()
        self._win.deiconify()
        self._position()
        self._make_interactive()
        self._win.lift()
        self._win.focus_force()
        self._entry.focus_set()

    def hide(self):
        if self.is_visible():
            self._win.destroy()
        self._win = None
        self._entry_var = None
        self._entry = None
        self._status_label = None
        self._results_frame = None

    def show_searching(self, name: str):
        if not self.is_visible():
            return
        self._set_status(f"Searching for \"{name}\"...", self._colors["FG_MUTED"])
        self._clear_results()
        self._resize_to_content()

    def show_error(self, message: str):
        if not self.is_visible():
            return
        self._set_status(message, self._colors["NEGATIVE"])
        self._clear_results()
        self._resize_to_content()

    def show_result(self, result: dict):
        """A condensed version of the main app's Player Lookup card -
        enough to glance at during downtime, not the full page. Name,
        levels, and the stats/stash numbers most worth a quick check
        (K/D, extraction rate, net stash) rather than all ten fields the
        full page shows."""
        if not self.is_visible():
            return
        import delta_force_core as core
        c = self._colors
        self._set_status("", c["FG_MUTED"])
        self._clear_results()

        name_row = tk.Frame(self._results_frame, bg=c["SURFACE"])
        name_row.pack(fill="x")
        tk.Label(name_row, text=result["name"], bg=c["SURFACE"], fg=c["FG"],
                 font=("Segoe UI Semibold", 13), anchor="w").pack(side="left")
        tk.Label(name_row,
                 text=f"  Lv.{result['level_operations']}/{result['level_warfare']}",
                 bg=c["SURFACE"], fg=c["FG_MUTED"], font=("Segoe UI", 9)
                 ).pack(side="left")

        stats = result.get("stats") or {}
        stash = result.get("stash") or {}
        if not stats and not stash:
            # The likely explanation, not a confirmed one - search_player
            # can't fully distinguish "this player hasn't been queued for
            # analysis yet" from a transient error on just the stats
            # call, but the former is the common, expected case this
            # service's own queue-based design produces, so it's worth
            # naming rather than leaving this looking like a bare failure.
            tk.Label(self._results_frame,
                     text="No stats yet - this service queues players "
                          "it hasn't seen before. Try again in a bit.",
                     bg=c["SURFACE"], fg=c["FG_MUTED"], font=("Segoe UI", 9),
                     anchor="w", justify="left",
                     wraplength=self.WIDTH - 32).pack(fill="x", pady=(6, 0))
            self._resize_to_content()
            return

        grid = tk.Frame(self._results_frame, bg=c["SURFACE"])
        grid.pack(fill="x", pady=(8, 0))
        cells = []
        if stats:
            cells.append(("K/D", f"{stats['kd_ratio']:.2f}"))
            cells.append(("EXTRACT", f"{stats['extraction_rate'] * 100:.0f}%"))
            cells.append(("HEADSHOT", f"{stats['headshot_rate'] * 100:.0f}%"))
        if stash:
            cells.append(("NET STASH", core.fmt_money(stash.get("net", 0))))
        for i, (label, value) in enumerate(cells):
            col = tk.Frame(grid, bg=c["SURFACE"])
            col.grid(row=i // 2, column=i % 2, sticky="w",
                    padx=(0, 20), pady=(0, 6))
            tk.Label(col, text=label, bg=c["SURFACE"], fg=c["FG_MUTED"],
                     font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x")
            tk.Label(col, text=value, bg=c["SURFACE"], fg=c["FG"],
                     font=("Segoe UI Semibold", 12), anchor="w").pack(fill="x")
        self._resize_to_content()

    def _set_status(self, text, color):
        self._status_label.configure(text=text, fg=color)

    def _clear_results(self):
        for w in self._results_frame.winfo_children():
            w.destroy()

    def _resize_to_content(self):
        """Re-fits the window to whatever's currently displayed. Needed
        because every state after the initial one (searching, an error,
        a result with its stats grid) is taller than the plain entry box
        _build() first sized the window for - without this, the window
        stays clipped to that original, shorter geometry and newer,
        taller content silently runs off the bottom edge instead of
        being visible (confirmed directly: show_result()'s stats grid
        rendered completely cut off until this was added). Keeps the
        same top-left corner _position() set, rather than recentering,
        since this is a corner-anchored overlay, not a centered dialog -
        only the size should change as content does, not where it sits.
        """
        self._win.update_idletasks()
        w = self._win.winfo_reqwidth()
        h = self._win.winfo_reqheight()
        x = self._win.winfo_x()
        y = self._win.winfo_y()
        self._win.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self):
        c = self._colors
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        if not IS_WINDOWS:
            try:
                win.attributes("-alpha", 0.96)
            except tk.TclError:
                pass
        win.configure(bg=c["BORDER"])

        body = tk.Frame(win, bg=c["SURFACE"], padx=16, pady=14)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(body, bg=c["SURFACE"])
        header.pack(fill="x")
        tk.Label(header, text="⌕", bg=c["SURFACE"], fg=c["ACCENT"],
                 font=("Segoe UI", 12)).pack(side="left")
        tk.Label(header, text=" Player Lookup", bg=c["SURFACE"],
                 fg=c["FG"], font=("Segoe UI Semibold", 10)).pack(side="left")
        tk.Label(header, text="Esc to close", bg=c["SURFACE"],
                 fg=c["FG_MUTED"], font=("Segoe UI", 8)).pack(side="right")

        tk.Frame(body, bg=c["BORDER_SOFT"], height=1).pack(fill="x", pady=(10, 10))

        entry_row = tk.Frame(body, bg=c["SURFACE_ALT"])
        entry_row.pack(fill="x")
        self._entry_var = tk.StringVar()
        self._entry = tk.Entry(
            entry_row, textvariable=self._entry_var, bd=0,
            bg=c["SURFACE_ALT"], fg=c["FG"], insertbackground=c["FG"],
            font=("Segoe UI", 11), highlightthickness=0)
        self._entry.pack(fill="x", padx=10, pady=8)
        self._entry.bind("<Return>", self._submit)
        win.bind("<Escape>", lambda e: self.hide())

        self._status_label = tk.Label(
            body, text="Type a name and press Enter.", bg=c["SURFACE"],
            fg=c["FG_MUTED"], font=("Segoe UI", 9), anchor="w",
            justify="left", wraplength=self.WIDTH - 32)
        self._status_label.pack(fill="x", pady=(10, 0))

        self._results_frame = tk.Frame(body, bg=c["SURFACE"])
        self._results_frame.pack(fill="x", pady=(4, 0))

        self._win = win

    def _submit(self, _event=None):
        name = self._entry_var.get().strip()
        if name and self._on_submit:
            self._on_submit(name)
        return "break"

    def _position(self):
        # Top-left, deliberately the opposite corner from OverlayWindow's
        # top-right - so the two never overlap if both happen to be open
        # at once, since they're independent features with independent
        # hotkeys.
        self._win.update_idletasks()
        w = self._win.winfo_reqwidth()
        h = self._win.winfo_reqheight()
        self._win.geometry(f"{w}x{h}+24+24")

    def _make_interactive(self):
        """The deliberate exception to this module's click-through
        design (see this class's own docstring) - removes
        WS_EX_TRANSPARENT so the window actually receives mouse/keyboard
        input, while keeping WS_EX_LAYERED (the alpha blending) and
        WS_EX_TOOLWINDOW (stays off the taskbar/alt-tab) exactly as
        OverlayWindow._make_click_through does. Same SetLayeredWindow-
        Attributes-right-after-SetWindowLongW ordering for the same
        reason documented there: Windows resets a layered window's
        alpha/composited-content state on any GWL_EXSTYLE-touching call,
        so alpha has to be re-established immediately after, every time.

        This changes WHETHER the window can be interacted with, not
        whether it still APPEARS on top of the game - -topmost (set once
        in _build) is unrelated to WS_EX_TRANSPARENT and stays in effect
        either way."""
        if not IS_WINDOWS:
            return
        try:
            hwnd = self._win.winfo_id()
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TOOLWINDOW = 0x00000080
            LWA_ALPHA = 0x00000002

            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TOOLWINDOW)
            user32.SetLayeredWindowAttributes(hwnd, 0, 245, LWA_ALPHA)
        except Exception:
            pass


# ---------------------------------------------------------------------
# System tray icon ("minimize to hidden icons")
# ---------------------------------------------------------------------
class TrayIcon:
    """Wraps pystray so the main window can be hidden to the
    notification area instead of the taskbar. Runs pystray's own loop on
    a dedicated background thread; menu actions run on THAT thread too,
    so they hop back to the Tk main thread via root.after() rather than
    touching widgets directly.

    Entirely optional: if pystray isn't installed, every method here is
    a harmless no-op and the app behaves exactly as if this class didn't
    exist - minimize-to-tray just isn't offered.
    """

    def __init__(self, root, icon_path, on_show, on_toggle_overlay, on_exit):
        self._root = root
        self._icon_path = icon_path
        self._on_show = on_show
        self._on_toggle_overlay = on_toggle_overlay
        self._on_exit = on_exit
        self._icon = None
        self._thread = None

    @staticmethod
    def available() -> bool:
        try:
            import pystray  # noqa: F401
            return True
        except Exception:
            # Not just ImportError: pystray picks a platform backend at
            # import time (GTK/AppIndicator on Linux, win32 on Windows,
            # etc.) and a missing backend dependency surfaces as
            # whatever error that backend's own import raises - observed
            # ValueError from a missing GTK namespace during testing,
            # and there's no guarantee every platform/environment
            # combination fails the same way. Any failure here means
            # "tray isn't usable," full stop - never worth crashing the
            # whole settings panel over.
            return False

    def start(self):
        if self._icon is not None:
            return
        try:
            import pystray
            from PIL import Image
        except Exception:
            return

        try:
            image = Image.open(self._icon_path)
        except Exception:
            return

        try:
            menu = pystray.Menu(
                pystray.MenuItem("Show Delta Force Tracker",
                                 lambda: self._root.after(0, self._on_show),
                                 default=True),
                pystray.MenuItem("Toggle Overlay",
                                 lambda: self._root.after(0, self._on_toggle_overlay)),
                pystray.MenuItem("Exit",
                                 lambda: self._root.after(0, self._on_exit)),
            )
            self._icon = pystray.Icon("DeltaForceTracker", image,
                                      "Delta Force Tracker", menu)
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
        except Exception:
            self._icon = None
            self._thread = None

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
        self._icon = None
        self._thread = None
