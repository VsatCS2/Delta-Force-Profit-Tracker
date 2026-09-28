"""
Delta Force — community leaderboard client.

Talks to your self-hosted community backend (see server/README.md) to:
  - opt into the public leaderboard (one direct API call - no browser
    popup, no OAuth, see "Why no Discord/OAuth" below)
  - upload this player's aggregate stats (the same numbers already shown
    in the Overview tab - never raw match history or DfTools credentials)
  - fetch the current leaderboard
  - opt out, or leave entirely (deletes the account and stats server-side)

Every public function here fails soft: if the server is unreachable, or
nobody has opted in yet, callers get None/[]/False back rather than an
exception. This feature is entirely optional and must never block the
core tracker.

Why no Discord/OAuth:
    A player only ever reaches this module after delta_force_login.py has
    already completed a real, verified login to the game's own DfTools
    backend - core.current_openid() is only ever populated
    from THAT authenticated session, never typed in by hand. That's a
    reasonable identity signal on its own, so registering here is just
    "tell the server the openid and nickname the game itself already gave
    you" - one direct API call, no separate account, no browser redirect
    dance, no client secret to protect.

    The one rule that keeps this trustworthy: opt_in() below must always
    read the openid from core.current_openid(), never accept it as a
    free-text argument from a caller. If that ever changes, this
    identity model no longer holds.

    The server hashes the openid (SHA-256) before it's ever stored, so it
    never persists the raw value either - see server/app.py.
"""

import json

import requests

import delta_force_core as core
from delta_force_paths import APP_DATA_DIR
from delta_force_version import APP_VERSION, version_tuple

def normalize_url(url: str) -> str:
    """Tolerates the usual hand-typing slips: surrounding whitespace, a
    missing scheme, a trailing slash. A bare domain gets https:// - a
    scheme-less URL is otherwise a hard error in requests ("No scheme
    supplied"), which is exactly how the leaderboard's Join button first
    failed. An explicit http:// is left alone (local testing)."""
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


# Deployment-specific addresses live in delta_force_config.py - see there.
# Left blank, every function below just fails soft (is_linked() stays
# False, fetch_leaderboard() returns []), so an unconfigured build still
# works fine as a purely local tracker.
try:
    from delta_force_config import SERVER_URL as _CFG_SERVER_URL
    from delta_force_config import UPDATE_INFO_URL as _CFG_UPDATE_INFO_URL
except ImportError:
    _CFG_SERVER_URL = _CFG_UPDATE_INFO_URL = ""

SERVER_URL = normalize_url(_CFG_SERVER_URL)

ACCOUNT_FILE = APP_DATA_DIR / "community_account.json"

_REQUEST_TIMEOUT_SECONDS = 10


class NotLoggedInError(RuntimeError):
    """Raised by opt_in() when there's no DfTools login yet to identify
    the player with - opting into the leaderboard needs a captured login
    first (i.e. the person has already used 'Log In (browser)' at least
    once)."""
    pass


class NotLinkedError(RuntimeError):
    """Raised by calls that need a registered account when there isn't
    one yet (nobody has called opt_in() successfully)."""
    pass


class LinkExpiredError(RuntimeError):
    """Raised when the server no longer recognizes this account's
    api_key (a 401 from an authenticated call). The most common cause
    on a free-tier host with no persistent disk (see server/README.md):
    the backend restarted and its database reset, so every previously
    issued api_key - including this one - stopped existing server-side.
    The stale local link is cleared automatically whenever this is
    raised (see _request_authed below), so the fix from the GUI's side
    is just: show this message and let the person click "Join
    Leaderboard" again to re-register."""
    pass


class ServerNotConfiguredError(RuntimeError):
    """Raised when SERVER_URL is blank - this build hasn't been pointed
    at a community backend."""
    pass


def _request_authed(method: str, url: str, **kwargs):
    """requests.request() for an authenticated call, with one piece of
    shared behavior: a 401 means the server doesn't recognize our
    api_key anymore (most likely it lost its database - see
    LinkExpiredError above, not that the credential was wrong to begin
    with). Clearing the stale local link here, in one place, means
    every caller (sync, opt-in/out, status check) recovers the same
    way instead of each needing its own copy of this logic - and means
    the very next "Join Leaderboard" click starts clean rather than
    trying to reuse a key the server will just reject again."""
    resp = requests.request(method, url, **kwargs)
    if resp.status_code == 401:
        try:
            ACCOUNT_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        raise LinkExpiredError(
            "Your community link has expired — the server may have "
            "restarted and lost track of it. Click \"Join Leaderboard\" "
            "to relink.")
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------
# Local account file
# ---------------------------------------------------------------------
def load_account() -> dict:
    if not ACCOUNT_FILE.exists():
        return None
    try:
        return json.loads(ACCOUNT_FILE.read_text())
    except (OSError, ValueError):
        return None


def is_linked() -> bool:
    return load_account() is not None


def leave(delete_on_server: bool = True) -> None:
    """Forget the local account. If delete_on_server, also asks the
    server to erase this player's row and stats first (best-effort - the
    local file is removed either way, since "forget me" should work even
    if the server happens to be unreachable right now)."""
    if delete_on_server:
        try:
            account = load_account()
            if account and SERVER_URL:
                requests.delete(
                    f"{SERVER_URL}/me",
                    headers=_auth_headers(account),
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
        except Exception:
            pass
    try:
        ACCOUNT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _auth_headers(account: dict = None) -> dict:
    account = account or load_account()
    if not account or not account.get("api_key"):
        raise NotLinkedError("Not opted into the community leaderboard yet.")
    return {"Authorization": f"Bearer {account['api_key']}"}


def _require_server() -> str:
    if not SERVER_URL:
        raise ServerNotConfiguredError(
            "This build isn't pointed at a community server yet "
            "(SERVER_URL is blank in delta_force_community.py).")
    return SERVER_URL


# ---------------------------------------------------------------------
# Opt in / register
# ---------------------------------------------------------------------
def opt_in(nickname: str = None, want_visible: bool = True, timeout: int = _REQUEST_TIMEOUT_SECONDS) -> dict:
    """Registers (or re-registers) this player using the openid from
    their own completed DfTools login, and opts them into the
    leaderboard. Safe to call again later (e.g. after a reinstall) - the
    server recognizes the same openid and hands back the same api_key
    rather than creating a duplicate entry.

    nickname defaults to whatever's in the last-fetched profile data, if
    any; falls back to "Player" if there's nothing better. want_visible
    controls the initial opted_in state server-side (True = show me on
    the leaderboard immediately; False = register but stay hidden until
    set_opt_in(True) is called).

    Raises NotLoggedInError / ServerNotConfiguredError /
    requests.RequestException on failure.
    """
    server_url = _require_server()

    openid = core.current_openid()
    if not openid:
        raise NotLoggedInError(
            "Log in to Delta Force Tracker first (the 'Log In (browser)' "
            "button) — the leaderboard identifies you using the same "
            "login, so there's nothing to opt in with yet.")

    resp = requests.post(
        f"{server_url}/register",
        json={"openid": openid, "nickname": nickname or "Player",
              "opt_in": want_visible},
        timeout=timeout,
    )
    resp.raise_for_status()
    result = resp.json()

    account = {
        "api_key": result["api_key"],
        "display_name": nickname or "Player",
    }
    ACCOUNT_FILE.write_text(json.dumps(account, indent=2))
    return {**account, "opted_in": result.get("opted_in", want_visible)}


# ---------------------------------------------------------------------
# Stats + leaderboard
# ---------------------------------------------------------------------
def _best_map_and_operator(rows: list) -> tuple:
    """Same definition as the GUI's rail 'Best Map' / 'Best Operator':
    highest total net income grouped by map/operator name."""
    return core.best_map_and_operator(rows)


def sync_stats(rows: list, profile: dict = None, timeout: int = _REQUEST_TIMEOUT_SECONDS) -> dict:
    """Uploads this player's current aggregate stats. rows is the same
    list core.build_rows() / the GUI's self.rows already holds; profile
    is the optional GetMyData payload (for the rank label). Raises
    NotLinkedError / ServerNotConfiguredError / LinkExpiredError, or
    requests.RequestException on a network/server error - callers should
    catch and show a status message rather than let this crash a
    background thread."""
    server_url = _require_server()
    headers = _auth_headers()

    ov = core.overview(rows or [])
    best_map, best_operator = _best_map_and_operator(rows)

    rank_label = ""
    if profile:
        label, _mode = core.extract_rank_from_profile(profile)
        if label and label != "—":
            rank_label = label

    payload = {
        "net_income_all_time": int(ov["all_time"]),
        "matches": int(ov["matches"]),
        "wins": int(ov["wins"]),
        "losses": int(ov["losses"]),
        "win_rate": float(ov["win_rate"]),
        "best_map": best_map,
        "best_operator": best_operator,
        "rank_label": rank_label,
    }
    # Today's totals (UTC day) for the daily leaderboard. An older server
    # simply ignores these fields, so this is safe against any backend.
    daily = core.daily_stats_utc(rows)
    payload.update({
        "daily_date": daily["day"],
        "daily_net_income": int(daily["net_income"]),
        "daily_matches": int(daily["matches"]),
        "daily_wins": int(daily["wins"]),
        "daily_losses": int(daily["losses"]),
        "daily_best_map": daily["best_map"],
        "daily_best_operator": daily["best_operator"],
    })
    resp = _request_authed("POST", f"{server_url}/stats/sync", json=payload,
                           headers=headers, timeout=timeout)
    return resp.json()


def fetch_leaderboard_full(sort: str = "net_income", limit: int = 50,
                           period: str = "all"):
    """(players, meta). period is "all" or "daily". meta carries the
    server's period/day/resets_at for the daily view ({} otherwise).
    Fails soft to ([], {}) like everything else here - safe to call from
    a GUI refresh without a try/except.

    Rows differ by period: all-time rows use net_income_all_time, daily
    rows use net_income (see delta_force_gui's row_net())."""
    if not SERVER_URL:
        return [], {}
    try:
        resp = requests.get(
            f"{SERVER_URL}/leaderboard",
            params={"sort": sort, "limit": limit, "period": period},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        meta = {k: data[k] for k in ("period", "day", "resets_at") if k in data}
        return data.get("players", []), meta
    except Exception:
        return [], {}


def fetch_leaderboard(sort: str = "net_income", limit: int = 50) -> list:
    """The all-time board as a plain list (kept for existing callers)."""
    return fetch_leaderboard_full(sort, limit, "all")[0]


# Optional (set in delta_force_config.py). Where to read the latest-version
# info from; blank falls back to SERVER_URL + "/version". A static file
# (for example version.json in a GitHub repo, via its
# raw.githubusercontent.com URL) is the most reliable choice - it never
# sleeps and doesn't depend on the leaderboard server being up.
# Expected JSON: {"latest_version": "1.2.0", "download_url": "https://..."}
UPDATE_INFO_URL = normalize_url(_CFG_UPDATE_INFO_URL)

# Generous because the check runs on a background thread, and a sleeping
# free-tier server can take about a minute to answer its first request.
_UPDATE_CHECK_TIMEOUT_SECONDS = 60


def update_check_configured() -> bool:
    return bool(UPDATE_INFO_URL or SERVER_URL)


def check_for_update_status():
    """(status, detail). status is one of:
      "update"       - detail is {'latest_version', 'download_url'}
      "current"      - the server answered; nothing newer
      "failed"       - couldn't check; detail is a short reason
      "unconfigured" - no URL set at all
    Kept separate from check_for_update() so a manual "Check for Updates"
    click can say "couldn't check" instead of claiming you're up to date
    when the address is wrong or the site is unreachable."""
    url = UPDATE_INFO_URL or (f"{SERVER_URL}/version" if SERVER_URL else "")
    if not url:
        return "unconfigured", None
    try:
        resp = requests.get(url, timeout=_UPDATE_CHECK_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        latest = data.get("latest_version", "")
        if version_tuple(latest) == (0,):
            return "failed", "the reply didn't include a valid latest_version"
        if version_tuple(latest) > version_tuple(APP_VERSION):
            return "update", data
        return "current", None
    except requests.exceptions.HTTPError as e:
        return "failed", f"the server answered {e.response.status_code}"
    except requests.exceptions.ConnectionError:
        return "failed", "couldn't connect"
    except requests.exceptions.Timeout:
        return "failed", "timed out"
    except ValueError:
        return "failed", "the reply wasn't valid JSON"
    except Exception as e:
        return "failed", type(e).__name__


def check_for_update() -> dict:
    """{'latest_version', 'download_url'} if something newer exists, else
    None (including when the check fails). For callers that only care
    whether to announce an update; see check_for_update_status()."""
    status, detail = check_for_update_status()
    return detail if status == "update" else None


def fetch_my_status() -> dict:
    """Returns {'display_name', 'opted_in', 'stats'} for the registered
    account, or None if not registered / server unreachable / the link
    turned out to be expired (in which case it's also been cleared
    locally by the time this returns - see _request_authed)."""
    if not SERVER_URL:
        return None
    try:
        resp = _request_authed("GET", f"{SERVER_URL}/me",
                               headers=_auth_headers(),
                               timeout=_REQUEST_TIMEOUT_SECONDS)
        return resp.json()
    except Exception:
        return None


def set_opt_in(value: bool) -> bool:
    """Returns the new opted_in state on success. Raises NotLinkedError /
    ServerNotConfiguredError / LinkExpiredError / requests.RequestException
    on failure."""
    server_url = _require_server()
    headers = _auth_headers()
    endpoint = "opt-in" if value else "opt-out"
    resp = _request_authed("POST", f"{server_url}/{endpoint}",
                           headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
    return resp.json().get("opted_in", value)
