"""
Delta Force Tracker — auto-update: download, stage, and self-replace.

The design deliberately avoids the GitHub API for the actual download.
GitHub release assets have a stable, predictable URL of the form
    https://github.com/{owner}/{repo}/releases/download/{tag}/{filename}
which serves the raw file with a plain unauthenticated GET - no API call,
no api.github.com rate limit (60/hour/IP, shared across however many
people run this app from the same network), no User-Agent requirement.
The release process just has to publish that one URL in version.json
alongside the version bump it already does (see
delta_force_community.check_for_update_status's "asset_url" field) -
nothing here ever calls the GitHub API.

Splits into three steps with a hard boundary between them:
    download_update()   - network only, safe to run anytime in the
                           background regardless of what the app is doing
    stage_update()       - extracts and validates the zip into a fresh
                           temp folder, still touches nothing about the
                           real install
    launch_installer_and_exit() - the only step that touches the actual
                           install directory, and only after the caller
                           has gotten explicit confirmation from the user

That last step can't just overwrite the running .exe - Windows keeps an
open file locked while it executes. But it DOES allow renaming one, which
is the whole trick: a small helper script (see INSTALL_SCRIPT) waits a
bounded time for this process to exit, renames the old exe to <name>.old,
copies the new files in under the real name, and starts the new version -
and if anything goes wrong it puts the old exe back, restarts it, and
leaves a marker so the app can tell the person on its next start.
Renaming instead of waiting for the file to unlock matters because a
PyInstaller onefile build keeps a second bootloader process holding the
exe open for a moment after the app itself has exited, so "wait until it's
free" is unreliable in exactly the build this ships as.

Things that went wrong in earlier versions, and are handled on purpose:
the helper used to be launched DETACHED_PROCESS, which gives cmd no
console, so every tool it ran opened its own visible console window
(see _spawn_hidden); and it inherited this app's PyInstaller environment
variables, which can stop the relaunched exe from starting (see
clean_pyinstaller_env).

Windows-only, like delta_force_overlay.py/delta_force_startup.py -
importing this on another OS works fine, but every function here is
inert unless IS_WINDOWS and the app is actually frozen (see
launch_installer_and_exit's docstring for why "frozen" matters too).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests

from delta_force_paths import APP_DATA_DIR

IS_WINDOWS = sys.platform.startswith("win")

_DOWNLOAD_TIMEOUT_SECONDS = (10, 120)  # (connect, read) - a full build is ~100MB
_CHUNK_SIZE = 262144  # 256 KiB


class UpdateError(RuntimeError):
    """Anything that should stop the update and show the person a plain
    reason, rather than a raw exception - a bad download, a corrupt zip,
    a missing exe inside it, or this platform/build not supporting
    self-install at all."""
    pass


def download_update(asset_url: str, on_progress=None) -> Path:
    """Streams asset_url to a temp file and returns its path.

    on_progress(downloaded_bytes, total_bytes_or_None) is called
    periodically - total is None if the server didn't send a
    Content-Length, which callers should treat as "show a spinner, not a
    percentage" rather than guessing a fake total.

    Verifies the final size against Content-Length when one was sent
    (not a cryptographic check - this project doesn't sign its
    releases - but it does catch a truncated or interrupted download,
    which is the far more likely failure mode for a plain HTTP GET).
    Raises UpdateError on any failure; never leaves a partial file
    behind for a caller to accidentally treat as complete.
    """
    if not asset_url:
        raise UpdateError("No download URL was provided.")

    fd, tmp_path = tempfile.mkstemp(prefix="dftracker_update_", suffix=".zip")
    os.close(fd)
    dest = Path(tmp_path)

    try:
        resp = requests.get(asset_url, stream=True, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
        resp.raise_for_status()
        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None

        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, total)

        if total is not None and downloaded != total:
            raise UpdateError(
                f"Download was incomplete ({downloaded:,} of {total:,} bytes) - "
                "the connection may have dropped. Try again.")
        if downloaded == 0:
            raise UpdateError("Downloaded file was empty.")
        return dest
    except requests.exceptions.RequestException as e:
        dest.unlink(missing_ok=True)
        raise UpdateError(f"Download failed: {e}")
    except UpdateError:
        dest.unlink(missing_ok=True)
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def stage_update(zip_path: Path, expected_exe_name: str = "DeltaForceTracker.exe") -> Path:
    """Extracts zip_path into a fresh temp folder and returns that
    folder's path - never the real install directory, so a bad zip
    can't damage a working install. Raises UpdateError if it isn't a
    valid zip or doesn't contain the expected .exe (a release zip built
    some other way, or a download that returned an HTML error page
    instead of the actual asset, would otherwise fail silently later
    with a half-applied update instead of clearly here)."""
    staging = Path(tempfile.mkdtemp(prefix="dftracker_staged_"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise UpdateError(f"Downloaded file is corrupt (bad entry: {bad}).")
            zf.extractall(staging)
    except zipfile.BadZipFile:
        raise UpdateError("Downloaded file isn't a valid zip archive.")

    if not any(p.name == expected_exe_name for p in staging.rglob("*.exe")):
        raise UpdateError(
            f"The downloaded update doesn't contain {expected_exe_name} - "
            "this doesn't look like a real release build.")
    return staging


def current_install_dir() -> Path:
    """Where the running .exe actually lives - the self-replace target.
    Only meaningful when frozen; see launch_installer_and_exit."""
    return Path(sys.executable).resolve().parent


def _find_staged_root(staging: Path, expected_exe_name: str) -> Path:
    """The folder *inside* staging that directly contains the .exe - a
    zip built as `zip -r release.zip DeltaForceTracker/` extracts one
    level deeper than one built as `zip release.zip *`, and copying the
    wrong level would nest a stale copy instead of actually updating
    anything. Whichever level directly holds the .exe is copied from."""
    for exe in staging.rglob(expected_exe_name):
        return exe.parent
    raise UpdateError(f"{expected_exe_name} not found in the staged update.")


# ---------------------------------------------------------------------
# Applying the update
# ---------------------------------------------------------------------

# Written by the install script if an update couldn't be applied, read (and
# deleted) by the app on its next start so the failure is SHOWN to the
# person instead of the update just quietly not happening.
FAIL_MARKER = APP_DATA_DIR / "update_failed.txt"

_SCRIPT_GLOB = "dftracker_update_*.bat"
_LOG_GLOB = "dftracker_update_*.log"

# The helper script. Static text: every input arrives as a DFT_* environment
# variable set by launch_installer_and_exit, so nothing in here depends on
# path text, quoting, or the console code page (a username with an accent or
# an & in it can't break it), and the file is plain ASCII.
#
# Deliberately flat - labels and goto, no ( ) blocks, no delayed expansion:
# a path containing ! or ) breaks either of those.
#
# The key idea: Windows won't let you OVERWRITE a running .exe, but it does
# let you RENAME one. So instead of waiting for the old exe to unlock (which
# a PyInstaller onefile build makes unreliable - a second bootloader process
# keeps the file open for a moment after the app itself has exited), the old
# exe is renamed to <name>.old, the new files are copied in under the real
# name, and the new version is started. If anything fails, the old exe is
# put back and restarted, and a marker file tells the app to say so.
INSTALL_SCRIPT = r"""@echo off
rem Delta Force Tracker self-update helper (written by delta_force_updater.py).
rem Inputs: DFT_PID DFT_EXE DFT_SRC DFT_STAGING DFT_DEST DFT_LOG DFT_FAIL
rem Optional: DFT_MAX_WAITS DFT_MAX_TRIES (retry limits, for testing).

if "%DFT_MAX_WAITS%"=="" set DFT_MAX_WAITS=30
if "%DFT_MAX_TRIES%"=="" set DFT_MAX_TRIES=20

call :log "Update started. Waiting for process %DFT_PID% to exit."
set WAITS=0

:waitloop
tasklist /FI "PID eq %DFT_PID%" /NH 2>NUL | find "%DFT_PID%" >NUL
if errorlevel 1 goto exited
set /a WAITS+=1
if %WAITS% GEQ %DFT_MAX_WAITS% goto waitgaveup
ping -n 2 127.0.0.1 >NUL
goto waitloop

:waitgaveup
call :log "Gave up waiting for the old process. Continuing anyway - a running exe can still be renamed."
goto swap

:exited
call :log "Old process has exited."

:swap
if not exist "%DFT_DEST%\%DFT_EXE%" goto moved
set TRIES=0

:moveloop
move /Y "%DFT_DEST%\%DFT_EXE%" "%DFT_DEST%\%DFT_EXE%.old" >> "%DFT_LOG%" 2>&1
if not errorlevel 1 goto moved
set /a TRIES+=1
call :log "Could not move the old exe aside - attempt %TRIES%."
if %TRIES% GEQ %DFT_MAX_TRIES% goto failed_nochange
ping -n 2 127.0.0.1 >NUL
goto moveloop

:moved
call :log "Old exe is out of the way. Copying the new files."
set TRIES=0

:copyloop
xcopy "%DFT_SRC%\*" "%DFT_DEST%\" /Y /E /I /Q >> "%DFT_LOG%" 2>&1
if not errorlevel 1 goto copied
set /a TRIES+=1
call :log "Copy attempt %TRIES% failed."
if %TRIES% GEQ %DFT_MAX_TRIES% goto failed_rollback
ping -n 2 127.0.0.1 >NUL
goto copyloop

:copied
if not exist "%DFT_DEST%\%DFT_EXE%" goto failed_rollback
call :log "New files are in place. Starting the new version."
start "" /D "%DFT_DEST%" "%DFT_DEST%\%DFT_EXE%"
call :log "Relaunch issued."
rem The old exe is deliberately LEFT as <name>.old. The new app deletes it
rem itself on its first successful start, which doubles as proof that the
rem new version really launched - and until then the previous version is
rem still on disk if something unforeseen stops the new one starting.
rmdir /S /Q "%DFT_STAGING%" >NUL 2>&1
call :log "Finished."
goto end

:failed_rollback
call :log "Install failed - putting the previous version back."
if exist "%DFT_DEST%\%DFT_EXE%.old" move /Y "%DFT_DEST%\%DFT_EXE%.old" "%DFT_DEST%\%DFT_EXE%" >> "%DFT_LOG%" 2>&1

:failed_nochange
call :log "UPDATE FAILED."
echo failed > "%DFT_FAIL%"
tasklist /FI "PID eq %DFT_PID%" /NH 2>NUL | find "%DFT_PID%" >NUL
if not errorlevel 1 goto end
call :log "Restarting the previous version."
start "" /D "%DFT_DEST%" "%DFT_DEST%\%DFT_EXE%"
goto end

:end
exit /b 0

:log
echo [%date% %time%] %~1 >> "%DFT_LOG%"
exit /b 0
"""


def write_install_script(pid: int, temp_dir=None) -> Path:
    """Writes the static helper script and returns its path. CRLF line
    endings written explicitly (not left to text-mode translation, which
    differs by platform): cmd.exe can mis-handle labels and goto in a batch
    file with bare LF endings."""
    script_path = Path(temp_dir or tempfile.gettempdir()) / f"dftracker_update_{pid}.bat"
    text = INSTALL_SCRIPT.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(script_path, "w", encoding="ascii", newline="") as f:
        f.write(text)
    return script_path


def clean_pyinstaller_env(env: dict) -> dict:
    """Returns a copy of env that's safe to start a NEW instance of this
    app from.

    A PyInstaller onefile app records where it unpacked itself in
    environment variables that every child process inherits (_MEIPASS2
    before PyInstaller 6.9, the _PYI_* family after). A new copy of the
    exe launched with those still set mistakes itself for a child of the
    running app and tries to run out of the OLD app's temp folder - which
    is deleted the moment that app exits - so the relaunched app can fail
    to start even when the file replacement itself went perfectly.
    Removing them, and setting PYINSTALLER_RESET_ENVIRONMENT=1 (which
    newer PyInstaller documents for exactly this "launch myself again"
    case), gives the new exe a clean start under either generation.
    """
    cleaned = {k: v for k, v in env.items()
               if k.upper() != "_MEIPASS2" and not k.upper().startswith("_PYI_")}
    cleaned["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return cleaned


def build_install_env(staged_root: Path, staging: Path, install_dir: Path,
                      exe_name: str, pid: int, log_path: Path,
                      base_env=None) -> dict:
    env = clean_pyinstaller_env(dict(base_env if base_env is not None else os.environ))
    env.update({
        "DFT_PID": str(pid),
        "DFT_EXE": exe_name,
        "DFT_SRC": str(staged_root),
        "DFT_STAGING": str(staging),
        "DFT_DEST": str(install_dir),
        "DFT_LOG": str(log_path),
        "DFT_FAIL": str(FAIL_MARKER),
    })
    return env


def _check_install_dir_writable(install_dir: Path) -> None:
    """Fails BEFORE the app closes if it can't write where it lives (e.g.
    installed under Program Files without admin rights) - otherwise the
    app would shut down, the update would fail, and the only trace would
    be a log nobody knows to look for."""
    try:
        with tempfile.NamedTemporaryFile(dir=install_dir, prefix=".dft_write_test_"):
            pass
    except OSError as e:
        raise UpdateError(
            f"Delta Force Tracker can't write to its own folder ({install_dir}), "
            f"so it can't replace itself ({e.__class__.__name__}). Move the app "
            "to a folder you own (like your Desktop or Documents), or download "
            "the new version manually.")


def launch_installer_and_exit(staging: Path,
                              expected_exe_name: str = "DeltaForceTracker.exe") -> None:
    """The only step that touches the real install directory. Writes and
    launches the helper script (hidden, detached, so it outlives this
    process), then returns - the CALLER is responsible for actually
    exiting the app right after (this function does not call sys.exit()
    itself, so the GUI can run its own normal shutdown - closing Discord
    RPC, stopping the hotkey listeners - before the process ends).

    Raises UpdateError, without launching anything, if this isn't a
    frozen Windows build (running from source has no "install exe" to
    replace - sys.executable is the Python interpreter, not this app, so
    silently doing nothing would be far more confusing than refusing
    clearly), or if the install folder isn't writable.
    """
    if not IS_WINDOWS:
        raise UpdateError("Auto-install is only supported on Windows.")
    if not getattr(sys, "frozen", False):
        raise UpdateError(
            "Auto-install only works in a built .exe, not when running from source.")

    install_dir = current_install_dir()
    _check_install_dir_writable(install_dir)
    staged_root = _find_staged_root(staging, expected_exe_name)
    pid = os.getpid()

    temp = Path(tempfile.gettempdir())
    log_path = temp / f"dftracker_update_{pid}.log"
    log_path.unlink(missing_ok=True)       # a fresh log per attempt
    FAIL_MARKER.unlink(missing_ok=True)    # the marker only ever describes THIS attempt

    script_path = write_install_script(pid, temp)
    env = build_install_env(staged_root, staging, install_dir,
                            expected_exe_name, pid, log_path)
    _spawn_hidden(["cmd.exe", "/d", "/c", str(script_path)], env, str(temp))


def _spawn_hidden(cmd: list, env: dict, cwd: str) -> None:
    """Starts cmd with no visible window that survives this process exiting.

    CREATE_NO_WINDOW, not DETACHED_PROCESS (what this used to use): a
    detached cmd has no console at all, so every console tool it runs
    (tasklist, find, xcopy...) allocates its OWN new console - on Windows
    11 each one opens as a Windows Terminal window, which is exactly the
    blank 'find "29748"' window that showed up on screen. With
    CREATE_NO_WINDOW cmd gets one hidden console and its children share
    it. Standard handles are explicitly the null device rather than
    whatever a windowed app happens to have inherited.

    Tries to break away from the parent's job object first, so a launcher
    that kills child processes when its job closes can't take the updater
    down with the app. If the job doesn't permit that, CreateProcess
    refuses outright, so fall back to the same launch without it.
    """
    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

    def start(flags):
        return subprocess.Popen(
            cmd, creationflags=flags, env=env, cwd=cwd, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    try:
        start(base | CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        try:
            start(base)
        except OSError as e:
            raise UpdateError(f"Couldn't start the update helper: {e}")


# ---------------------------------------------------------------------
# Housekeeping, run by the app on every start
# ---------------------------------------------------------------------

def consume_failure_marker(path=None) -> bool:
    """True (once) if the last update attempt failed - and removes the
    marker so it's only ever reported one time."""
    marker = Path(path or FAIL_MARKER)
    if not marker.exists():
        return False
    try:
        marker.unlink()
    except OSError:
        pass
    return True


def latest_update_log(temp_dir=None):
    """The newest update log, so a failure message can say where it is."""
    logs = list(Path(temp_dir or tempfile.gettempdir()).glob(_LOG_GLOB))
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def cleanup_leftovers(exe_path=None, temp_dir=None, now=None) -> None:
    """Best-effort tidy-up after an update; never raises.

    - <exe>.old: the previous version, renamed aside during the swap. The
      script deliberately leaves it: removing it here, on the new app's
      first successful start, is what proves the new version launched, and
      until then it's the way back if something stopped it starting.
    - Old helper scripts (after 2 minutes, so a script that's still
      finishing its own cleanup is left alone) and old logs (after 14
      days; recent ones are kept because they're the diagnostic trail).
    - Staging folders and half-downloaded zips older than a day: a failed
      update deliberately leaves its staging folder behind, and a crash
      mid-download leaves a partial zip. (Only swept at start-up, so the
      one staged in THIS session is never touched.)
    """
    try:
        exe = Path(exe_path) if exe_path else (
            Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None)
        if exe is not None:
            (exe.parent / (exe.name + ".old")).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        temp = Path(temp_dir or tempfile.gettempdir())
        now = now if now is not None else time.time()
        for glob, max_age in ((_SCRIPT_GLOB, 120), (_LOG_GLOB, 14 * 86400),
                              ("dftracker_update_*.zip", 86400)):
            for f in temp.glob(glob):
                try:
                    if now - f.stat().st_mtime > max_age:
                        f.unlink()
                except OSError:
                    pass
        for d in temp.glob("dftracker_staged_*"):
            try:
                if d.is_dir() and now - d.stat().st_mtime > 86400:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass
