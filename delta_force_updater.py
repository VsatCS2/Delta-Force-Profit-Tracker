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

That last step can't just overwrite the running .exe directly - Windows
keeps an open file locked while it's executing. The standard way around
this (used by most self-updating Windows software that doesn't ship a
separate installer): write a small batch script that waits for this
process to exit, THEN copies the new files over the old ones and
relaunches, launch it detached so it survives this process ending, and
exit. No new compiled binary, no separate updater .exe to maintain or
get flagged by antivirus on its own - a batch script performing "wait,
copy, relaunch" is about as unremarkable as file operations get.

Windows-only, like delta_force_overlay.py/delta_force_startup.py -
importing this on another OS works fine, but every function here is
inert unless IS_WINDOWS and the app is actually frozen (see
launch_installer_and_exit's docstring for why "frozen" matters too).
"""

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

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


def build_install_script(staged_root: Path, install_dir: Path,
                         exe_name: str, pid: int) -> Path:
    """Writes the batch script that performs the actual update once this
    process has exited, and returns its path. Doesn't run it - see
    launch_installer_and_exit.

    Every path is double-quoted throughout: both staged_root and
    install_dir can and often do contain spaces (a real reported install
    lived under "...\\DF Stats\\Public Release\\"), and an unquoted path
    with a space silently truncates at the first one in batch, which
    would make this copy from or to the wrong place instead of failing
    loudly.
    """
    script_path = Path(tempfile.gettempdir()) / f"dftracker_update_{pid}.bat"
    # /FI "PID eq N" is the documented tasklist filter for matching a
    # specific process id; findc against the header-less CSV output
    # (/NH) is the standard idiom for "is this PID still running".
    script = f'''@echo off
setlocal
set "SRC={staged_root}"
set "DEST={install_dir}"

:waitloop
tasklist /FI "PID eq {pid}" /NH 2>NUL | find "{pid}" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak >NUL
    goto waitloop
)

xcopy "%SRC%\\*" "%DEST%\\" /Y /E /I >NUL
if errorlevel 1 (
    echo Update failed to copy files. Your previous version is still in place.
    pause
    exit /b 1
)

start "" "%DEST%\\{exe_name}"
rmdir /S /Q "%SRC%" 2>NUL
del "%~f0"
'''
    script_path.write_text(script, encoding="utf-8")
    return script_path


def launch_installer_and_exit(staging: Path,
                              expected_exe_name: str = "DeltaForceTracker.exe") -> None:
    """The only step that touches the real install directory. Writes and
    launches the self-replace batch script (detached, so it outlives
    this process), then returns - the CALLER is responsible for actually
    exiting the app right after (this function does not call sys.exit()
    itself, so the GUI can run its own normal shutdown - closing Discord
    RPC, stopping the hotkey listener - before the process ends).

    Raises UpdateError, without launching anything, if this isn't a
    frozen Windows build: running from source has no "install exe" to
    replace (sys.executable is the Python interpreter, not this app),
    so silently doing nothing would be far more confusing than refusing
    clearly.
    """
    if not IS_WINDOWS:
        raise UpdateError("Auto-install is only supported on Windows.")
    if not getattr(sys, "frozen", False):
        raise UpdateError(
            "Auto-install only works in a built .exe, not when running from source.")

    install_dir = current_install_dir()
    staged_root = _find_staged_root(staging, expected_exe_name)
    pid = os.getpid()
    script_path = build_install_script(staged_root, install_dir,
                                       expected_exe_name, pid)

    # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP: the script must keep
    # running after this Python process exits, and must not be tied to
    # this process's console (there usually isn't one - this is the
    # windowed GUI build) or its process group (so closing this app's
    # window can't take the updater down with it).
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["cmd.exe", "/c", str(script_path)],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
