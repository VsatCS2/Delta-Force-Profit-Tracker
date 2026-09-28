"""
Delta Force — run-at-Windows-startup toggle.

Uses the standard per-user "Run" registry key:
    HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run

This is the same mechanism most ordinary consumer Windows apps use for a
"start with Windows" checkbox - not a scheduled task, not a service, just
one value that Windows itself reads at login for this specific user.
HKEY_CURRENT_USER (not HKEY_LOCAL_MACHINE) means no admin elevation is
ever needed, and nothing here can affect other accounts on the machine.

Windows-only, like delta_force_overlay.py's hotkey/tray pieces - every
function here is a harmless no-op on macOS/Linux rather than pretending
to support platforms that don't have this concept.
"""

import sys

IS_WINDOWS = sys.platform.startswith("win")

_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "DeltaForceTracker"


def _command_line(minimized: bool) -> str:
    """What Windows should actually launch at login.

    Frozen (the real .exe most users run): sys.executable IS that exe,
    so this is just its own path.

    Dev mode (python delta_force_gui.py): sys.executable is the Python
    interpreter and sys.argv[0] is this script - registering that is
    mostly a testing convenience, not the expected real-world path
    (that's the built .exe), but it works.
    """
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        cmd = f'"{sys.executable}" "{sys.argv[0]}"'
    if minimized:
        cmd += " --minimized"
    return cmd


def is_enabled() -> bool:
    """True if this app currently has a Run-key entry. Reflects reality
    (reads the registry) rather than trusting a stored setting, so a
    settings checkbox built on this can't drift out of sync with what
    Windows will actually do at next login."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
            return True
    except OSError:
        return False


def get_registered_command():
    """The raw command line currently stored in the Run key, or None."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as key:
            value, _type = winreg.QueryValueEx(key, _VALUE_NAME)
            return value
    except OSError:
        return None


def repair_if_stale(minimized: bool) -> bool:
    """If an entry exists but points somewhere other than this running
    exe (the person moved or re-extracted the app), rewrite it to point
    here instead of leaving Windows launching a path that no longer
    exists. Frozen builds only: running from source would otherwise
    overwrite a real exe's entry with a dev-mode python command. Returns
    True only if it actually rewrote something. (It can't help if the
    folder was simply deleted - nothing is running then - which is what
    the --remove-startup flag and Settings checkbox are for.)"""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return False
    current = get_registered_command()
    if current is None or current == _command_line(minimized):
        return False
    return set_enabled(True, minimized=minimized)


def set_enabled(enabled: bool, minimized: bool = False) -> bool:
    """Adds/updates or removes the Run-key entry. Returns True on
    success. Never raises - a write failure (e.g. a locked-down/managed
    machine restricting HKCU writes, rare but real) should just leave
    the setting off and let the caller show that, not crash the
    settings panel over what's ultimately a convenience feature."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH,
                            0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ,
                                  _command_line(minimized))
            else:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
