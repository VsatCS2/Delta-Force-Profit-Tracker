"""
Delta Force - one copy at a time.

If DF Tracker is already running (in the tray, minimized, or open) and the
person launches it again - the Start menu, a shortcut, or Windows starting it
at login while they also open it by hand - the second copy asks the first to
come to the front and exits, instead of opening a second window.

Without this, two copies would each show a tray icon, each sync to the
community server and each fight over Discord Rich Presence.

How it works (Windows only; a no-op everywhere else):
  * A named MUTEX marks "a copy is running". Whoever creates it first wins; the
    operating system removes it automatically when that process ends, however
    it ends (a crash can't leave a stale lock behind).
  * A named EVENT is how a second launch says "please show yourself". The
    running copy waits on it in a background thread.
  * No network port, no file, no admin rights. The names start with "Local\\"
    so they are per Windows login session: other people logged in on the same
    PC are unaffected.

It FAILS OPEN: if anything here goes wrong, acquire() says "go ahead and start",
which is exactly how the app behaved before this module existed.

Set the environment variable DF_ALLOW_MULTIPLE=1 to turn it off (development).
"""

import os
import sys
import threading

IS_WINDOWS = sys.platform.startswith("win")

_MUTEX_NAME = "Local\\DeltaForceTracker.SingleInstance"
_EVENT_NAME = "Local\\DeltaForceTracker.ShowWindow"

_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0x00000000

_held = {"mutex": None}          # keeps our mutex handle alive for the life of the process


class _Win32:
    """The handful of kernel32 calls we need, with their argument types spelled out
    (ctypes guesses wrong for 64-bit handles otherwise)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenEventW.restype = wintypes.HANDLE
        k.SetEvent.argtypes = [wintypes.HANDLE]
        k.SetEvent.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        self._k = k

    def create_mutex(self, name):
        handle = self._k.CreateMutexW(None, False, name)
        return handle, self._ctypes.get_last_error()

    def create_event(self, name):
        return self._k.CreateEventW(None, False, False, name)       # auto-reset, starts unsignalled

    def open_event(self, name):
        return self._k.OpenEventW(_EVENT_MODIFY_STATE, False, name)

    def set_event(self, handle):
        return bool(self._k.SetEvent(handle))

    def wait(self, handle, timeout_ms):
        return self._k.WaitForSingleObject(handle, timeout_ms)

    def close(self, handle):
        self._k.CloseHandle(handle)


def _enabled() -> bool:
    return IS_WINDOWS and os.environ.get("DF_ALLOW_MULTIPLE") != "1"


def acquire(backend=None, signal_existing: bool = True) -> bool:
    """True: this is the only copy (or we couldn't tell) - carry on and start.
    False: another copy is already running - the caller should exit quietly. If
    signal_existing is True (a person launched it) the running copy is also asked
    to come to the front; for a quiet launch at Windows startup it is left alone.
    Never raises."""
    if backend is None:
        if not _enabled():
            return True
        try:
            backend = _Win32()
        except Exception:
            return True
    try:
        handle, err = backend.create_mutex(_MUTEX_NAME)
        if not handle:
            return True                                   # couldn't create it: don't block the app
        if err != _ERROR_ALREADY_EXISTS:
            _held["mutex"] = handle                       # we are the first copy
            return True
        # Someone else already holds it: ask them to come forward, then step aside.
        try:
            backend.close(handle)
            if signal_existing:
                ev = backend.open_event(_EVENT_NAME)
                if ev:
                    backend.set_event(ev)
                    backend.close(ev)
        except Exception:
            pass
        return False
    except Exception:
        return True


def start_listener(on_show, backend=None):
    """In the running copy: call on_show() (from a background thread) whenever a
    second launch asks. Returns a stop() function, or None if nothing was started.
    on_show must be thread-safe - the GUI passes one that just posts to its queue."""
    if backend is None:
        if not _enabled():
            return None
        try:
            backend = _Win32()
        except Exception:
            return None
    try:
        ev = backend.create_event(_EVENT_NAME)
        if not ev:
            return None
    except Exception:
        return None
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                if backend.wait(ev, 500) == _WAIT_OBJECT_0:
                    on_show()
            except Exception:
                break

    t = threading.Thread(target=loop, name="df-single-instance", daemon=True)
    t.start()

    def stop_listener():
        stop.set()
    return stop_listener
