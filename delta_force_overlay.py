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


def resolve_hotkey(settings: dict):
    """Returns (modifiers, vk_code, display_label) for whatever's
    currently configured - a preset picked from the dropdown, or a
    custom combination someone recorded themselves (see
    delta_force_gui._show_hotkey_capture_dialog). settings["overlay_hotkey_source"]
    ("preset" or "custom") decides which one is actually in effect,
    since a person can have both a preset selection AND a previously
    recorded custom combo sitting in settings at once, and only one can
    be the real answer.

    Falls back to the default preset if the requested source's data is
    missing or malformed (e.g. a settings file from before custom
    hotkeys existed, or a corrupted value) - the overlay should always
    end up with SOME working hotkey rather than none.
    """
    source = settings.get("overlay_hotkey_source", "preset")
    if source == "custom":
        mods = settings.get("overlay_hotkey_mods")
        vk = settings.get("overlay_hotkey_vk")
        label = settings.get("overlay_hotkey_custom_label")
        if isinstance(mods, int) and isinstance(vk, int) and mods and vk and label:
            return mods, vk, label
        # fall through to the preset default below - malformed custom data

    label = settings.get("overlay_hotkey", DEFAULT_HOTKEY_LABEL)
    mods, vk = hotkey_by_label(label)
    for lbl, _, _ in HOTKEY_PRESETS:
        if lbl == label:
            return mods, vk, label
    return mods, vk, DEFAULT_HOTKEY_LABEL


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
    happens to call start().

    on_trigger() runs on this background thread. It must never touch
    Tkinter widgets directly (Tkinter isn't thread-safe) - callers
    should hop back to the main thread themselves, e.g. via
    root.after() or the app's existing msg_queue pattern.
    """

    def __init__(self, on_trigger):
        self._on_trigger = on_trigger
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

        if not user32.RegisterHotKey(None, _HOTKEY_ID, modifiers, vk_code):
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
                    if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                        try:
                            self._on_trigger()
                        except Exception:
                            pass
                else:
                    time.sleep(0.05)
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
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
