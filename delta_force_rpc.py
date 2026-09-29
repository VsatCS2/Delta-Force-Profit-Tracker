"""
Delta Force — Discord Rich Presence.

Optional. If pypresence isn't installed, or Discord isn't running, every
function here becomes a no-op so the tracker keeps working normally.

Setup (one-time):
    1. Visit https://discord.com/developers/applications
    2. Click "New Application". Give it any name.
    3. Copy the "Application ID" from the General Information page.
    4. Paste it into DISCORD_CLIENT_ID below.
    5. Under "Rich Presence" -> "Art Assets", upload two images:
         df_logo_win.png   (DF logo with a green up-arrow in the corner)
         df_logo_loss.png  (DF logo with a red down-arrow in the corner)
       Give them the same keys as the filenames (without .png).

Design note:
    We no longer use `small_image`. Discord always composites it into the
    bottom-right of `large_image`, and a bare transparent arrow looks
    stray. Instead, the arrow is baked into the large image, and the
    tracker picks df_logo_win or df_logo_loss based on today's result.

Requires:
    pip install pypresence
"""

import threading
import time
from typing import Optional

# ---- Fill this in with your own application ID. ---------------------
DISCORD_CLIENT_ID = "1552115272301813861"   # e.g. "1234567890123456789"

# ---- Asset keys uploaded on the Dev Portal. ------------------------
ASSET_LARGE_WIN = "df_logo_win"
ASSET_LARGE_LOSS = "df_logo_loss"

# How often to push an update to Discord (seconds). Discord rate-limits
# updates to once per ~15 seconds, so don't go below that.
_UPDATE_INTERVAL = 15

_state = {
    "client": None,
    "lock": threading.Lock(),
    "last_update": 0.0,
    "last_payload": None,
    "enabled": False,
    "start_ts": int(time.time()),
    "error": None,
}


def _log(msg: str) -> None:
    if _debug_enabled():
        print(f"[rpc] {msg}")


def _debug_enabled() -> bool:
    try:
        import delta_force_core as core
        return core.is_debug()
    except Exception:
        return False


def is_available() -> bool:
    """True if pypresence is importable (Discord itself may still be down)."""
    try:
        import pypresence  # noqa: F401
        return True
    except ImportError:
        return False


def _connect() -> Optional[object]:
    """Open a Discord RPC connection. Returns the client or None."""
    if not is_available():
        _log("pypresence not installed")
        return None
    if not DISCORD_CLIENT_ID:
        _log("DISCORD_CLIENT_ID is empty — set it in delta_force_rpc.py")
        return None
    try:
        from pypresence import Presence
        client = Presence(DISCORD_CLIENT_ID)
        client.connect()
        _log("connected to Discord")
        return client
    except Exception as e:
        _log(f"could not connect: {e}")
        return None


def enable() -> bool:
    """Turn RPC on. Returns True if we successfully connected."""
    with _state["lock"]:
        if _state["enabled"] and _state["client"]:
            return True
        client = _connect()
        if client is None:
            _state["enabled"] = False
            _state["error"] = "Could not connect to Discord"
            return False
        _state["client"] = client
        _state["enabled"] = True
        _state["error"] = None
        _state["last_update"] = 0.0   # force next update through
        _state["last_payload"] = None
        return True


def disable() -> None:
    """Turn RPC off and close the connection."""
    with _state["lock"]:
        client = _state["client"]
        _state["client"] = None
        _state["enabled"] = False
        _state["last_payload"] = None
    if client is not None:
        try:
            client.close()
            _log("disconnected")
        except Exception:
            pass


def is_enabled() -> bool:
    return _state["enabled"]


def last_error() -> Optional[str]:
    return _state["error"]


def _abbrev_number(n: float) -> str:
    """Format a number compactly for the small RPC text.

    +2,218,746  ->  +2.22M
    -1,312,000  ->  -1.31M
    +83,392     ->  +83.4K
    +1,234      ->  +1,234
    """
    sign = "+" if n >= 0 else "-"
    v = abs(n)
    if v >= 1_000_000_000:
        return f"{sign}{v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"{sign}{v / 1_000_000:.2f}M"
    if v >= 10_000:
        return f"{sign}{v / 1_000:.1f}K"
    if v >= 1_000:
        return f"{sign}{v:,.0f}"
    return f"{sign}{v:.0f}"


def _build_payload(rows: list, profile: Optional[dict]) -> dict:
    import delta_force_core as core
    from datetime import datetime

    # Nickname only goes to large_text now, so profit can take details.
    nickname = "Delta Force player"
    if profile:
        info = profile.get("player_info") or {}
        if info.get("nickname"):
            nickname = str(info["nickname"])

    rank_label = None
    if profile:
        label, mode = core.extract_rank_from_profile(profile)
        if label and label != "—":
            rank_label = f"{label}"

    ov = core.overview(rows or [])
    today_net = ov.get("today", 0)
    all_net = ov.get("all_time", 0)

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_rows = [r for r in (rows or []) if r["date"] == today_str]
    today_wins = sum(1 for r in today_rows if r["result"] == "win")
    today_losses = sum(1 for r in today_rows if r["result"] == "loss")
    today_matches = len(today_rows)

    # --- Line 1: profit (today, or all-time fallback) ---
    # display_net is whichever figure ends up on screen - the icon below
    # keys off THIS, not today_net directly, or a zero-matches day with a
    # negative all-time total showed the win icon (today_net is 0, which
    # is not < 0, even though the line actually displayed was negative).
    if today_matches:
        display_net = today_net
        details_line = f"Today: {_abbrev_number(today_net)}"
    else:
        display_net = all_net
        details_line = f"All-time: {_abbrev_number(all_net)}"

    # --- Line 2: W-L and rank ---
    wl = f"{today_wins}W - {today_losses}L" if today_matches else "No matches today"
    if rank_label:
        state_line = f"{wl} · {rank_label}"
    else:
        state_line = wl

    # --- Large image picks win/loss variant, matching whichever profit
    # figure is actually shown on line 1 (see display_net above) ---
    large_key = ASSET_LARGE_LOSS if display_net < 0 else ASSET_LARGE_WIN

    # large_text shows the nickname on hover.
    hover = nickname
    if rank_label:
        hover = f"{nickname} · {rank_label}"

    payload = {
        "details": details_line[:128],
        "state": state_line[:128],
        "start": _state["start_ts"],
        "large_image": large_key,
        "large_text": hover[:128],
    }
    return payload

def update_presence(rows: Optional[list] = None,
                    profile: Optional[dict] = None) -> None:
    """Push the current stats to Discord. Cheap no-op if RPC is disabled,
    no data was provided, or we updated less than _UPDATE_INTERVAL ago.

    Never raises — RPC is cosmetic and must never break the tracker.
    """
    if not _state["enabled"] or _state["client"] is None:
        return

    now = time.time()
    if now - _state["last_update"] < _UPDATE_INTERVAL:
        return

    try:
        payload = _build_payload(rows or [], profile)
    except Exception as e:
        _log(f"payload build failed: {e}")
        return

    if payload == _state["last_payload"]:
        return

    try:
        _state["client"].update(**payload)
        _state["last_update"] = now
        _state["last_payload"] = payload
        _log(f"updated: {payload.get('state')} [{payload.get('large_image')}]")
    except Exception as e:
        _log(f"update failed, disabling RPC: {e}")
        _state["error"] = str(e)
        _state["enabled"] = False
        _state["client"] = None