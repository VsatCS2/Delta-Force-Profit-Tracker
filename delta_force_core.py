"""
Delta Force Profit Tracker — core logic
=========================================
Shared by delta_force_cli.py and delta_force_gui.py. Not meant to be run
directly.

No credentials are shipped with the tracker. All auth (openid, token, s,
u, ts, a) comes from dftools_creds.json, which is produced by
delta_force_login.py (the embedded-browser login). Until a login has
happened, the placeholder dicts below stay empty and any API call raises
a friendly NotLoggedInError.

Rate limiting:
    The DfTools proxy returns HTTP 200 with `code=210200,
    msg="request too frequently"` when requests land too close together.
    Fetching is therefore sequential with a delay between pages, and each
    page has bounded exponential-backoff retries.
"""

import csv
import json
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from delta_force_paths import APP_DATA_DIR, migrate_legacy_file, resource_path

# =====================================================================
# CREDENTIALS
# =====================================================================
def _empty_creds() -> dict:
    return {
        "openid": "",
        "token": "",
        "game_id": "29158",
        "channel": "131",
        "account_type": 1,
        "lang_type": "en",
        "u": "",
        "a": "10005",
        "ts": "",
        "s": "",
    }


MATCHLIST_CREDENTIALS = _empty_creds()
MYDATA_CREDENTIALS = _empty_creds()
MATCHDETAIL_CREDENTIALS = _empty_creds()
ASSETCALENDAR_CREDENTIALS = _empty_creds()
WEEKLYREPORT_CREDENTIALS = _empty_creds()

_ALL_CRED_BLOCKS = (
    MATCHLIST_CREDENTIALS,
    MYDATA_CREDENTIALS,
    MATCHDETAIL_CREDENTIALS,
    ASSETCALENDAR_CREDENTIALS,
    WEEKLYREPORT_CREDENTIALS,
)

migrate_legacy_file(Path(__file__).resolve().parent, "dftools_creds.json")
DFTOOLS_CREDS_FILE = APP_DATA_DIR / "dftools_creds.json"

_dftools_state = {
    "last_mtime": None,
    "endpoints_loaded": set(),
    "headers_by_endpoint": {},
}


class NotLoggedInError(RuntimeError):
    """Raised when an API call is attempted before credentials exist."""
    pass


class RateLimitedError(RuntimeError):
    """Raised when the API returns the 'request too frequently' code."""
    pass


_CRED_BLOCKS_BY_ENDPOINT = {
    "matchlist":     MATCHLIST_CREDENTIALS,
    "mydata":        MYDATA_CREDENTIALS,
    "matchdetail":   MATCHDETAIL_CREDENTIALS,
    "assetcalendar": ASSETCALENDAR_CREDENTIALS,
    "weeklyreport":  WEEKLYREPORT_CREDENTIALS,
}

# Fixed priority order for the fallback in _resolve_credentials() below -
# arbitrary since any populated block works for any endpoint (confirmed
# by direct testing: all 5x5 endpoint/signature combinations succeed),
# but a fixed order keeps which block gets used predictable rather than
# depending on dict iteration happening to vary.
_CRED_FALLBACK_ORDER = ("matchlist", "mydata", "matchdetail",
                        "assetcalendar", "weeklyreport")


def _resolve_credentials(endpoint: str) -> dict:
    """Returns endpoint's own captured credentials if present, otherwise
    falls back to whichever other endpoint's credentials were captured.

    The signed request params (openid/token/u/a/ts/s) aren't actually
    bound to which specific endpoint they came from - confirmed by
    directly testing all 5x5 combinations of captured signature against
    endpoint URL, every one of which the server accepted. Practically,
    this means a login only has to successfully capture ONE endpoint,
    not all five, for every endpoint to work - the other four just
    borrow it. Each endpoint still gets its own credential block (rather
    than collapsing to one shared block) so this stays easy to revert if
    DfTools ever changes their signing scheme to actually bind it to a
    specific endpoint - a behavior change that would show up as this
    fallback suddenly causing failures, not as a silent data problem.
    """
    own = _CRED_BLOCKS_BY_ENDPOINT.get(endpoint)
    if own and own.get("openid") and own.get("token") and own.get("s"):
        return own
    for name in _CRED_FALLBACK_ORDER:
        block = _CRED_BLOCKS_BY_ENDPOINT.get(name)
        if block and block.get("openid") and block.get("token") and block.get("s"):
            return block
    return own or _empty_creds()


def have_credentials(endpoint: str = "matchlist") -> bool:
    """True if the given endpoint can be called right now - either its
    own credentials were captured, or another endpoint's were (see
    _resolve_credentials)."""
    c = _resolve_credentials(endpoint)
    return bool(c.get("openid") and c.get("token") and c.get("s"))


def current_openid() -> str:
    """The logged-in player's openid, or "" if not logged in.

    Read from whichever endpoint's credentials were captured (the openid
    is the same in all of them), never from one specific block - login
    stops as soon as ANY one request is captured (see _resolve_credentials),
    so which block ends up populated is down to what the page happened to
    request first. Anything that needs the player's identity should call
    this rather than reach into a particular *_CREDENTIALS block; the
    leaderboard's Join button did exactly that, reading only the matchlist
    block, and failed for people who were fully logged in.

    Loads the saved capture first, so it also works right after launch
    before any fetch has had a chance to.
    """
    try:
        refresh_credentials_from_browser()
    except Exception:
        pass
    return _resolve_credentials("matchlist").get("openid", "") or ""


def require_credentials(endpoint: str) -> None:
    """Raise NotLoggedInError with a helpful message if creds are missing."""
    if have_credentials(endpoint):
        return
    raise NotLoggedInError(
        "You're not logged in yet. Run the login flow first:\n"
        "  CLI:  python delta_force_cli.py --login\n"
        "  GUI:  click the 'Log In (browser)' button in the right sidebar.\n\n"
        "A browser window will open - log in, and the tool will pick up "
        "your session automatically from there."
    )


def _short_key(name: str):
    """Map either a short key or a long endpoint name to its short key."""
    if not name:
        return None
    n = str(name).lower()
    aliases = {
        "matchlist":     ("matchlist", "getmatchlist"),
        "mydata":        ("mydata", "getmydata"),
        "matchdetail":   ("matchdetail", "getmatchdetail"),
        "assetcalendar": ("assetcalendar", "getassetweekcalendar"),
        "weeklyreport":  ("weeklyreport", "getweeklyreportsoldata"),
    }
    for short, names in aliases.items():
        if n in names:
            return short
    return None


def _normalize_creds(data: dict) -> dict:
    """Accept either short keys ('matchlist') or long endpoint names
    ('GetMatchList') as the top-level keys and return a dict keyed by the
    short names core uses internally."""
    if not isinstance(data, dict):
        return {}

    canonical = {}
    for top_key, rec in data.items():
        short = _short_key(top_key)
        if short is None and isinstance(rec, dict):
            short = _short_key(rec.get("endpoint", ""))
        if short is not None and short not in canonical:
            canonical[short] = rec
    return canonical


def refresh_credentials_from_browser(force: bool = False) -> bool:
    """Read dftools_creds.json and overwrite each credential block with the
    captured values. Returns True if new values were applied.

    Pass force=True to reload even if the file's mtime hasn't changed —
    used right after delta_force_login writes a fresh capture.
    """
    if not DFTOOLS_CREDS_FILE.exists():
        if _debug:
            print(f"[core] no browser capture at {DFTOOLS_CREDS_FILE}")
        return False

    try:
        mtime = DFTOOLS_CREDS_FILE.stat().st_mtime
    except OSError:
        return False

    if not force and _dftools_state["last_mtime"] == mtime:
        return False

    try:
        raw = json.loads(DFTOOLS_CREDS_FILE.read_text())
    except (OSError, ValueError) as e:
        if _debug:
            print(f"[core] could not parse {DFTOOLS_CREDS_FILE}: {e}")
        return False

    data = _normalize_creds(raw)

    mapping = {
        "matchlist":     MATCHLIST_CREDENTIALS,
        "mydata":        MYDATA_CREDENTIALS,
        "matchdetail":   MATCHDETAIL_CREDENTIALS,
        "assetcalendar": ASSETCALENDAR_CREDENTIALS,
        "weeklyreport":  WEEKLYREPORT_CREDENTIALS,
    }

    applied_any = False
    loaded = set()
    headers_by_endpoint = {}
    for endpoint, block in mapping.items():
        rec = data.get(endpoint)
        if not rec or not rec.get("params"):
            continue
        params = rec["params"]
        for k, v in params.items():
            if v is not None and v != "":
                block[k] = v
        # account_type comes through as a string from the query string;
        # coerce it back to int for cleanliness.
        if isinstance(block.get("account_type"), str):
            try:
                block["account_type"] = int(block["account_type"])
            except ValueError:
                pass
        loaded.add(endpoint)
        applied_any = True
        if rec.get("headers"):
            headers_by_endpoint[endpoint] = dict(rec["headers"])

    _dftools_state["last_mtime"] = mtime
    _dftools_state["endpoints_loaded"] = loaded
    _dftools_state["headers_by_endpoint"] = headers_by_endpoint

    if applied_any and _debug:
        print(f"[core] browser creds loaded for: "
              f"{', '.join(sorted(loaded)) or 'none'}")

    return applied_any


def credential_source() -> str:
    """Describe where credentials came from, for status displays.

    Any one captured endpoint is now functionally enough for all five
    (see _resolve_credentials) - this just says "logged in" rather than
    implying something's missing when only one or two endpoints actually
    fired during login. sorted(loaded) is still shown as a detail since
    it's genuinely informative for debugging, just no longer framed as
    "incomplete" when it's short.
    """
    loaded = _dftools_state["endpoints_loaded"]
    if not loaded:
        return "not logged in"
    return f"logged in (captured: {', '.join(sorted(loaded))})"


def _endpoint_key(url: str):
    if "GetMatchList" in url:
        return "matchlist"
    if "GetMyData" in url:
        return "mydata"
    if "GetMatchDetail" in url:
        return "matchdetail"
    if "GetAssetWeekCalendar" in url:
        return "assetcalendar"
    if "GetWeeklyReportSolData" in url:
        return "weeklyreport"
    return None


SEASON_IDS = ["10001", "10003", "10004", "10005", "10006", "10007",
              "10008", "10009", "10010", "10011"]

MATCHLIST_URL = "https://sg-act.playerinfinite.com/api/proxy/logicial/DfTools/GetMatchList"
MYDATA_URL = "https://sg-act.playerinfinite.com/api/proxy/logicial/DfTools/GetMyData"
MATCHDETAIL_URL = "https://sg-act.playerinfinite.com/api/proxy/logicial/DfTools/GetMatchDetail"
ASSETCALENDAR_URL = "https://sg-act.playerinfinite.com/api/proxy/logicial/DfTools/GetAssetWeekCalendar"
WEEKLYREPORT_URL = "https://sg-act.playerinfinite.com/api/proxy/logicial/DfWeeklyReport/GetWeeklyReportSolData"

PAGE_SIZE = 20
REQUEST_DELAY_SECONDS = 0.8   # base delay between pages; throttle window is ~1s
MAX_PAGE_RETRIES = 4          # retries per page on 210200
QUICK_REFRESH_PAGES = 2
RATE_LIMIT_CODE = 210200

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "content-type": "application/json",
    "origin": "https://www.playdeltaforce.com",
    "referer": "https://www.playdeltaforce.com/",
    "priority": "u=1, i",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    ),
}

migrate_legacy_file(Path(__file__).resolve().parent, "match_history.json")
CACHE_FILE = APP_DATA_DIR / "match_history.json"

CURRENCY_DIVISOR = 1
CURRENCY_LABEL = "credits"

_debug = False


def set_debug(flag: bool):
    global _debug
    _debug = flag


def is_debug() -> bool:
    return _debug


# =====================================================================
# Reference lookups
# =====================================================================
SEASON_NAMES = {
    "10001": "S1 - Genesis",
    "10002": "S2",
    "10003": "S3 - Blazefall",
    "10004": "S4 - Eclipse Vigil",
    "10005": "S5 - Break",
    "10006": "S6 - War Ablaze",
    "10007": "S7 - Ahsarah",
    "10008": "S8 - Morphosis",
    "10009": "S9 - Echo",
    "10010": "S10 - Meltdown",
    "10011": "S11 - Reorientation",
}

MAP_INFO = {
    # --- Operations (SOL) ---
    "2201": ("Zero Dam", "Easy"),
    "2202": ("Zero Dam", "Normal"),
    "2212": ("Zero Dam", "Normal - Solo"),
    "2221": ("Zero Dam", "Underground"),
    "2222": ("Zero Dam", "Barracks"),
    "2223": ("Zero Dam", "Administrative Center"),
    "2232": ("Zero Dam", "Eternal Night"),

    "1901": ("Layali Grove", "Easy"),
    "1902": ("Layali Grove", "Normal"),
    "1911": ("Layali Grove", "Solo Easy"),
    "1912": ("Layali Grove", "Solo Normal"),
    "1921": ("Layali Grove", "Sparkling Empress Hotel"),
    "1922": ("Layali Grove", "Radar Station"),

    "3901": ("Space City", "Normal"),
    "3902": ("Space City", "Hard"),
    "3921": ("Space City", "CEO Office Area"),
    "3922": ("Space City", "Core Area"),

    "8102": ("Brakkesh", "Normal"),
    "8103": ("Brakkesh", "Hard"),
    "8121": ("Brakkesh", "Market"),
    "8122": ("Brakkesh", "New Tower of Babel"),
    "8123": ("Brakkesh", "Tower Top"),

    "8802": ("Tide Prison", "Adaptation"),
    "8803": ("Tide Prison", "Hard"),
    "8821": ("Tide Prison", "Core Area"),

    "8901": ("AZ3", "Easy"),
    "8902": ("AZ3", "Normal"),
    "8921": ("AZ3", "Academy of Sciences"),
    "8922": ("AZ3", "Core Reactor"),
    "8923": ("AZ3", "Pressurized Water Reactor"),

    # --- Warfare (MP) ---
    "108": ("Trench Lines", "KoH"),
    "107": ("Trench Lines", "A/D"),
    "2401": ("Trench Lines", "Hill of Iron PvE"),
    "605": ("Trench Lines", "Victory Unite"),
    "127": ("Trench Lines", "Blitz"),
    "128": ("Trench Lines", "Siege"),
    "227": ("Trench Lines", "HoI"),
    "585": ("Trench Lines", "Shotgun Storm"),
    "902": ("Trench Lines", "TDM"),
    "555": ("Trench Lines", "Strength to Spare"),
    "561": ("Trench Lines", "Dragon's Fury"),
    "579": ("Trench Lines", "Crossfire"),
    "567": ("Trench Lines", "Arrow Feather"),
    "1107": ("Trench Lines", "Vanguard A/D"),
    "966": ("Trench Lines", "Luck's Favors"),
    "971": ("Trench Lines", "Tactical TDM"),
    "985": ("Trench Lines", "Crazy Box"),
    "436": ("Trench Lines", "Ultra Overload"),

    "34": ("Cracked", "KoH"),
    "33": ("Cracked", "A/D"),
    "2403": ("Cracked", "Hill of Iron PvE"),
    "602": ("Cracked", "Victory Unite"),
    "109": ("Cracked", "Blitz"),
    "118": ("Cracked", "Siege"),
    "119": ("Cracked", "HoI"),
    "587": ("Cracked", "Shotgun Storm"),
    "903": ("Cracked", "TDM"),
    "557": ("Cracked", "Strength to Spare"),
    "563": ("Cracked", "Dragon Dance"),
    "581": ("Cracked", "Death Cross"),
    "569": ("Cracked", "Arrow Feather"),
    "1059": ("Cracked", "Tactical Flashpoint"),
    "959": ("Cracked", "Flashpoint"),
    "980": ("Cracked", "Overload"),
    "956": ("Cracked", "Armored Corps"),
    "900": ("Cracked", "Capture the Flag"),
    "965": ("Cracked", "Luck's Favors"),
    "972": ("Cracked", "TTDM"),
    "984": ("Cracked", "Crazy Box"),
    "445": ("Cracked", "Windchaser"),
    "700": ("Cracked", "Control"),
    "437": ("Cracked", "Ultra Overload"),

    "112": ("Trainwreck", "KoH"),
    "111": ("Trainwreck", "A/D"),
    "2404": ("Trainwreck", "Hill of Iron PvE"),
    "606": ("Trainwreck", "Victory Unite"),
    "526": ("Trainwreck", "HoI"),
    "583": ("Trainwreck", "Shotgun Storm"),
    "904": ("Trainwreck", "TDM"),
    "553": ("Trainwreck", "Strength to Spare"),
    "559": ("Trainwreck", "Dragon Dance"),
    "577": ("Trainwreck", "Crossfire"),
    "565": ("Trainwreck", "Arrow Feather"),
    "958": ("Trainwreck", "Armored Corps"),
    "967": ("Trainwreck", "Luck's Favors"),
    "973": ("Trainwreck", "Tactical TDM"),
    "986": ("Trainwreck", "Crazy Box"),

    "103": ("Ascension", "KoH"),
    "54": ("Ascension", "A/D"),
    "2402": ("Ascension", "Hill of Iron PvE"),
    "601": ("Ascension", "Victory Unite"),
    "105": ("Ascension", "Blitz"),
    "116": ("Ascension", "Siege"),
    "117": ("Ascension", "HoI"),
    "586": ("Ascension", "Shotgun Storm"),
    "901": ("Ascension", "TDM"),
    "551": ("Ascension", "Open Field Assault"),
    "556": ("Ascension", "Strength to Spare"),
    "562": ("Ascension", "Dragon Dance"),
    "580": ("Ascension", "Cross"),
    "568": ("Ascension", "Arrow Feather"),
    "957": ("Ascension", "Armored Corps"),
    "970": ("Ascension", "Tactical TDM"),
    "1002": ("Ascension", "Ace Hunt"),

    "122": ("Knife Edge", "KoH"),
    "121": ("Knife Edge", "A/D"),
    "2406": ("Knife Edge", "King of the Hill PvE"),
    "584": ("Knife Edge", "Shotgun Storm"),
    "514": ("Knife Edge", "TDM"),
    "554": ("Knife Edge", "Strength to Spare"),
    "560": ("Knife Edge", "Dragon Dance"),
    "566": ("Knife Edge", "Arrow Feather"),
    "969": ("Knife Edge", "Tactical TDM"),

    "152": ("Fault", "KoH"),
    "151": ("Fault", "A/D"),
    "611": ("Fault", "Victory Unite"),
    "552": ("Fault", "Open Field Assault"),
    "951": ("Fault", "Strength to Spare"),
    "961": ("Fault", "Flashpoint"),
    "946": ("Fault", "TDM"),
    "975": ("Fault", "Tactical TDM"),
    "950": ("Fault", "Arrow Clash"),
    "1061": ("Fault", "Tactical Flashpoint"),
    "706": ("Fault", "Control"),
    "1000": ("Fault", "Ace Hunt"),

    "303": ("Cyclone", "KoH"),
    "302": ("Cyclone", "A/D"),
    "608": ("Cyclone", "Victory Unite"),
    "1302": ("Cyclone", "Vanguard A/D"),
    "887": ("Cyclone", "CTF"),
    "1003": ("Cyclone", "Ace Hunt"),
    "446": ("Cyclone", "Windchaser"),
    "884": ("Cyclone", "Flagship Frenzy"),

    "139": ("Monument", "KoH"),
    "138": ("Monument", "A/D"),
    "949": ("Monument", "Strength to Spare"),
    "948": ("Monument", "Arrow Clash"),
    "1062": ("Monument", "Tactical Flashpoint"),
    "962": ("Monument", "Flashpoint"),
    "947": ("Monument", "TDM"),
    "609": ("Monument", "Victory Unite"),
    "979": ("Monument", "Overload"),
    "996": ("Monument", "Armored Corps"),
    "968": ("Monument", "Luck's Favors"),
    "703": ("Monument", "Control"),
    "1001": ("Monument", "Ace Hunt"),
    "435": ("Monument", "Ultra Overload"),

    "146": ("Aftershock", "KoH"),
    "145": ("Aftershock", "A/D"),
    "610": ("Aftershock", "Victory Unite"),
    "997": ("Aftershock", "Armored Corps"),
    "963": ("Aftershock", "Flashpoint"),
    "964": ("Aftershock", "TDM"),
    "1063": ("Aftershock", "Tactical Flashpoint"),
    "978": ("Aftershock", "Overload"),
    "977": ("Aftershock", "Tactical TDM"),

    "550": ("Island Warfare", "CAS"),

    "312": ("Akh Canal", "KoH"),
    "311": ("Akh Canal", "A/D"),
    "885": ("Akh Canal", "TDM"),
    "612": ("Akh Canal", "Victory Unite"),
    "889": ("Akh Canal", "CTF"),
    "981": ("Akh Canal", "Tactical TDM"),

    "114": ("Shafted", "KoH"),
    "113": ("Shafted", "A/D"),
    "2407": ("Shafted", "King of the Hill PvE"),
    "124": ("Shafted", "Blitz"),
    "999": ("Shafted", "Flashpoint"),

    "210": ("Threshold", "KoH"),
    "75": ("Threshold", "A/D"),
    "2405": ("Threshold", "King of the Hill PvE"),
    "603": ("Threshold", "Victory Unite"),
    "213": ("Threshold", "Blitz"),
    "126": ("Threshold", "Siege"),
    "588": ("Threshold", "Shotgun Storm"),
    "906": ("Threshold", "TDM"),
    "558": ("Threshold", "Strength to Spare"),
    "564": ("Threshold", "Dragon Dance"),
    "582": ("Threshold", "Crossfire"),
    "570": ("Threshold", "Arrow Feather"),

    "172": ("Coliseum", "KoH"),
    "171": ("Coliseum", "A/D"),
    "982": ("Coliseum", "TDM"),
    "613": ("Coliseum", "Victory Unite"),
    "983": ("Coliseum", "TTDM"),
    "2858": ("Coliseum", "Hill of Iron"),
    "447": ("Coliseum", "Windchaser"),
    "702": ("Coliseum", "Control"),
    "434": ("Coliseum", "Ultra Overload"),

    "262": ("The Mog", "KoH"),
    "261": ("The Mog", "A/D"),
    "433": ("The Mog", "TDM"),
    "614": ("The Mog", "Victory Unite"),
    "438": ("The Mog", "Overload"),
}

OPERATOR_NAMES = {
    "20003": "Stinger",
    "10010": "Vyron",
    "40005": "Luna",
    "40010": "Hackclaw",
    "30010": "Sineva",
    "10011": "Nox",
    "10012": "Tempest",
    "40011": "Raptor",
    "30011": "Gizmo",
    "20005": "Vlinder",
    "50001": "Fiery Owl",
    "50002": "Flamethrower",
    "50003": "Rocketeer",
    "40012": "Morse",
    "10007": "D-wolf",
    "30008": "Shepherd",
    "30009": "Uluru",
    "20004": "Toxik",
    "30012": "N-Two",
    "20006": "Rover",
    "60001": "Desmoulins",
    "60002": "Athos Leal",
    "60003": "Aramis Orgueil",
}

OPERATOR_AVATARS = {
    "20003": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_5885a60bbe460243367d887ed189017a.png",
    "10010": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_7505e1b13c4853701383137a85e137fe.png",
    "40005": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_8202065ae34a8abfc3d79ffd6f54ce9c.png",
    "40010": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_5f471d23766d3bcf18dead89fdce78cb.png",
    "30010": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_957daef75bfa5b45f4d2adf1679673d3.png",
    "10011": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_cac182c3d25e8c8a373900c494dac777.png",
    "10012": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_d0e582557e38b61e04e8c1fc7840c2c4.png",
    "40011": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_81a7f5273258b4a3b11a8ce7f17e0cd3.png",
    "30011": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_f3e71c8419d882ca5f627c695b6e332b.png",
    "20005": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_ebc2d019628555e3863f9b7d8dd3c9ae.png",
    "50001": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_2ed758f062b0f5cf8707b9fd1efe5c87.png",
    "50002": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_135619469e83b32760f7cf02ce187764.png",
    "50003": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_cd3964fe3c86d74f3c0d3b24597daa71.png",
    "40012": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_8fbc0d80d8e8d8547e8df1bcbf3a5814.png",
    "10007": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_16d64abf60519d84d9cacb1549a2b5ad.png",
    "30008": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_c3250ef6eec1365a60f95ebb8ba4ddef.png",
    "30009": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_4f94f4cbf6a6c7bfd661f3dde1896c43.png",
    "20004": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_ad153ac86d3f62f85d1b6600921df285.png",
    "30012": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_482e42262ef92f60c7cf69c20752f6a2.png",
    "20006": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_bdc60aa1efdf923db59769bab65a9214.png",
    "60001": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_983039d4ad381a877baf92f83162319b.png",
    "60002": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_bab41b0b6274cba24991598358d1f489.png",
    "60003": "https://www.playdeltaforce.com/basic_info/operator_daily_report_avatar_713b546ab21791323f9464186871c9be.png",
}

_MP_RANK_BANDS = [
    (0, "Private III"), (150, "Private II"), (300, "Private I"),
    (450, "Corporal III"), (600, "Corporal II"), (750, "Corporal I"),
    (900, "Sergeant IV"), (1100, "Sergeant III"), (1300, "Sergeant II"),
    (1500, "Sergeant I"), (1700, "Lieutenant IV"), (1900, "Lieutenant III"),
    (2100, "Lieutenant II"), (2300, "Lieutenant I"), (2500, "Colonel V"),
    (2750, "Colonel IV"), (3000, "Colonel III"), (3250, "Colonel II"),
    (3500, "Colonel I"), (3750, "General V"), (4000, "General IV"),
    (4250, "General III"), (4500, "General II"), (4750, "General I"),
    (5000, "Marshal"),
]

_SOL_RANK_BANDS = [
    (1000, "Bronze III"), (1150, "Bronze II"), (1300, "Bronze I"),
    (1450, "Silver III"), (1600, "Silver II"), (1750, "Silver I"),
    (1900, "Gold IV"), (2100, "Gold III"), (2300, "Gold II"),
    (2500, "Gold I"), (2700, "Platinum IV"), (2900, "Platinum III"),
    (3100, "Platinum II"), (3300, "Platinum I"), (3500, "Diamond V"),
    (3750, "Diamond IV"), (4000, "Diamond III"), (4250, "Diamond II"),
    (4500, "Diamond I"), (4750, "Black Hawk V"), (5000, "Black Hawk IV"),
    (5250, "Black Hawk III"), (5500, "Black Hawk II"), (5750, "Black Hawk I"),
    (6000, "DF Pinnacle"),
]


# ---------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------
def map_name(map_id) -> str:
    info = MAP_INFO.get(str(map_id))
    if not info:
        return f"Map {map_id}"
    base, variant = info[0], info[-1]
    return base if not variant else f"{base} ({variant})"


def map_base(map_id) -> str:
    info = MAP_INFO.get(str(map_id))
    return info[0] if info else f"Map {map_id}"


def map_variant(map_id) -> str:
    info = MAP_INFO.get(str(map_id))
    return info[-1] if info else ""


def operator_name(operator_id) -> str:
    return OPERATOR_NAMES.get(str(operator_id), f"Op {operator_id}")


def operator_avatar(operator_id) -> str:
    return OPERATOR_AVATARS.get(str(operator_id), "")


def season_name(season_id) -> str:
    return SEASON_NAMES.get(str(season_id), f"Season {season_id}")


def rank_for_score(score, mode: str = "SOL") -> str:
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "—"
    bands = _MP_RANK_BANDS if str(mode).upper() == "MP" else _SOL_RANK_BANDS
    name = bands[0][1]
    for threshold, label in bands:
        if s >= threshold:
            name = label
        else:
            break
    return name


def period_label(period: str, kind: str) -> str:
    if kind == "date":
        try:
            return datetime.strptime(period, "%Y-%m-%d").strftime("%a %b %d")
        except ValueError:
            return period
    if kind == "week":
        try:
            year, wk = period.split("-W")
            return f"Wk {wk}, {year}"
        except ValueError:
            return period
    if kind == "month":
        try:
            return datetime.strptime(period, "%Y-%m").strftime("%b %Y")
        except ValueError:
            return period
    return period


def _first_number(*vals):
    for v in vals:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip():
            try:
                return float(v)
            except ValueError:
                continue
    return None


def extract_rank_from_row(row: dict) -> str:
    if not isinstance(row, dict):
        return "—"
    score = _first_number(
        row.get("rank_score"), row.get("score"),
        row.get("operator_rank_score"), row.get("solorank_score"),
        row.get("solorank"), row.get("mp_rank_score"),
    )
    if score is None:
        return "—"
    mode = row.get("mode") or row.get("category") or "SOL"
    return rank_for_score(score, str(mode).upper())


def kill_breakdown(match: dict):
    if not isinstance(match, dict):
        return (None, None)
    ops = _first_number(
        match.get("kill_operator_count"), match.get("kill_player_count"),
        match.get("player_kill_count"), match.get("operators_killed"),
    )
    total = _first_number(match.get("kill_count"), match.get("kills"))
    if ops is None or total is None:
        return (None, None)
    return (int(ops), max(0, int(total) - int(ops)))


def extract_rank_from_profile(profile: dict) -> tuple:
    if not profile:
        return ("—", "SOL")

    rank_data = profile.get("rank_data") or {}
    for mode in ("SOL", "MP"):
        block = rank_data.get(mode) or rank_data.get(mode.lower())
        if isinstance(block, dict):
            score = _first_number(block.get("score"), block.get("rank_score"),
                                  block.get("current_score"))
            if score is not None:
                return (rank_for_score(score, mode), mode)

    score = _first_number(rank_data.get("score"), rank_data.get("rank_score"),
                          rank_data.get("current_rank_score"))
    if score is not None:
        return (rank_for_score(score, "SOL"), "SOL")
    return ("—", "SOL")


# ---------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------
def _headers_for(endpoint_key):
    """Prefer captured headers for this endpoint; fall back to HEADERS.
    Always force a few headers that requests/API semantics require."""
    captured = _dftools_state.get("headers_by_endpoint") or {}
    headers = dict(captured.get(endpoint_key) or HEADERS)
    headers["content-type"] = "application/json"
    headers.pop("content-length", None)
    headers.pop("host", None)
    return headers


def _post_json(url: str, params: dict, body: dict, on_debug=None) -> dict:
    def dbg(msg):
        if _debug and on_debug:
            on_debug(msg)

    # Safety net: refresh blocks even if a caller forgot to refresh first.
    refresh_credentials_from_browser()

    endpoint_key = _endpoint_key(url)
    headers = _headers_for(endpoint_key)

    dbg(f"POST {url}")
    dbg(f"params: {params}")
    dbg(f"body: {body}")
    dbg(f"headers (source: "
        f"{'capture' if endpoint_key in (_dftools_state.get('headers_by_endpoint') or {}) else 'fallback'}): "
        f"{headers}")

    try:
        resp = requests.post(url, params=params, headers=headers, json=body, timeout=(10, 20))
    except requests.exceptions.ConnectTimeout:
        raise RuntimeError(
            "Connection timed out reaching the API. A firewall, VPN, proxy, or "
            "antivirus may be blocking the request."
        )
    except requests.exceptions.ReadTimeout:
        raise RuntimeError("Connected, but the server never responded in time.")
    except requests.exceptions.SSLError as e:
        raise RuntimeError(f"SSL/TLS error talking to the API: {e}")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"Could not connect to the API at all: {e}")

    return _handle_json_response(resp, dbg)


def _handle_json_response(resp, dbg):
    dbg(f"HTTP {resp.status_code}")
    dbg(f"response headers: {dict(resp.headers)}")
    dbg(f"raw body: {resp.text[:2000]}")

    resp.raise_for_status()

    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"Response wasn't valid JSON. First 500 chars:\n{resp.text[:500]}")

    code = payload.get("code")
    if code == RATE_LIMIT_CODE:
        raise RateLimitedError(
            f"rate limited (code={code}, msg={payload.get('msg')})"
        )
    if code != 0:
        raise RuntimeError(
            f"API returned an error (code={code}, "
            f"msg={payload.get('msg')}, http={resp.status_code}). "
            "Your credentials may have expired — open the browser login "
            "(--login / Log In button) for fresh credentials."
        )
    return payload["data"]


def _get_json(url: str, params: dict, on_debug=None) -> dict:
    """GET counterpart to _post_json, for endpoints confirmed to need no
    signed params or request body at all (see fetch_weekly_report) -
    shares the same header selection, timeout handling, and error/rate-
    limit parsing via _handle_json_response."""
    def dbg(msg):
        if _debug and on_debug:
            on_debug(msg)

    refresh_credentials_from_browser()
    endpoint_key = _endpoint_key(url)
    headers = _headers_for(endpoint_key)

    dbg(f"GET {url}")
    dbg(f"params: {params}")
    dbg(f"headers (source: "
        f"{'capture' if endpoint_key in (_dftools_state.get('headers_by_endpoint') or {}) else 'fallback'}): "
        f"{headers}")

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=(10, 20))
    except requests.exceptions.ConnectTimeout:
        raise RuntimeError(
            "Connection timed out reaching the API. A firewall, VPN, proxy, or "
            "antivirus may be blocking the request."
        )
    except requests.exceptions.ReadTimeout:
        raise RuntimeError("Connected, but the server never responded in time.")
    except requests.exceptions.SSLError as e:
        raise RuntimeError(f"SSL/TLS error talking to the API: {e}")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"Could not connect to the API at all: {e}")

    return _handle_json_response(resp, dbg)


def fetch_page(page: int, page_size: int = PAGE_SIZE, on_debug=None) -> dict:
    refresh_credentials_from_browser()
    require_credentials("matchlist")

    c = _resolve_credentials("matchlist")
    params = {
        "openid": c["openid"], "token": c["token"], "game_id": c["game_id"],
        "channel": c["channel"], "account_type": c["account_type"], "lang_type": c["lang_type"],
        "u": c["u"], "a": c["a"], "ts": c["ts"], "s": c["s"],
    }
    body = {
        "needLogin": True, "map_id": [], "show_net_income": True,
        "page": page, "page_size": page_size, "report_type": 1,
        "openid": c["openid"], "token": c["token"], "game_id": c["game_id"],
        "account_type": c["account_type"], "lang_type": c["lang_type"],
    }
    return _post_json(MATCHLIST_URL, params, body, on_debug=on_debug)


def _fetch_page_with_retry(page: int, page_size: int = PAGE_SIZE,
                           on_debug=None, on_progress=None,
                           max_retries: int = MAX_PAGE_RETRIES) -> dict:
    """Fetch one page, retrying with exponential backoff on rate-limit."""
    def prog(msg):
        if on_progress:
            on_progress(msg)

    base = REQUEST_DELAY_SECONDS
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return fetch_page(page, page_size, on_debug=on_debug)
        except RateLimitedError as e:
            last_err = e
            if attempt >= max_retries:
                break
            wait = base * (2 ** (attempt - 1))
            prog(f"Page {page} throttled, waiting {wait:.1f}s "
                 f"(attempt {attempt}/{max_retries})…")
            time.sleep(wait)
    raise RateLimitedError(
        f"page {page} still rate-limited after {max_retries} attempts: {last_err}"
    )


def fetch_my_data(on_debug=None) -> dict:
    refresh_credentials_from_browser()
    require_credentials("mydata")

    c = _resolve_credentials("mydata")
    params = {
        "openid": c["openid"], "token": c["token"], "game_id": c["game_id"],
        "channel": c["channel"], "account_type": c["account_type"], "lang_type": c["lang_type"],
        "u": c["u"], "a": c["a"], "ts": c["ts"], "s": c["s"],
    }
    body = {
        "needLogin": True, "seasonno": SEASON_IDS, "report_type": 1,
        "openid": c["openid"], "token": c["token"], "game_id": c["game_id"],
        "account_type": c["account_type"], "lang_type": c["lang_type"],
    }
    return _post_json(MYDATA_URL, params, body, on_debug=on_debug)


def fetch_asset_calendar(on_debug=None) -> dict:
    """GetAssetWeekCalendar: recent high-value items extracted, plus a
    12-week rolling net-income summary. Response shape (from a captured
    sample):
        data.carry_out_items: [{item_id, item_value, carry_out_count}, ...]
            already sorted by item_value descending - this is the
            "recent high-value items" list.
        data.quarter_weeks: [{week_no, week_start_timestamp,
            carry_out_count, net_income}, ...] - last ~12 weeks.
        data.week_stat: {net_income, total_income} for the current week.
    """
    refresh_credentials_from_browser()
    require_credentials("assetcalendar")

    c = _resolve_credentials("assetcalendar")
    params = {
        "openid": c["openid"], "token": c["token"], "game_id": c["game_id"],
        "channel": c["channel"], "account_type": c["account_type"], "lang_type": c["lang_type"],
        "u": c["u"], "a": c["a"], "ts": c["ts"], "s": c["s"],
    }
    body = {
        "needLogin": True,
        "openid": c["openid"], "token": c["token"], "game_id": c["game_id"],
        "account_type": c["account_type"], "lang_type": c["lang_type"],
    }
    return _post_json(ASSETCALENDAR_URL, params, body, on_debug=on_debug)


def fetch_weekly_report(on_debug=None) -> dict:
    """GetWeeklyReportSolData: a handful of pre-computed weekly stats -
    extraction rate, K/D, the squad-mate you profited most/least with
    this week, and a 7-day total-stash-value trend, among other fields
    (see weekly_highlights() for which ones are actually surfaced and
    why - most of the rest look like indices into DfTools' own
    client-side text templates, not raw data)."""
    refresh_credentials_from_browser()
    require_credentials("weeklyreport")

    # GET, six plain fields, no u/a/ts/s signature and no request body at
    # all - confirmed working this way against the live API (unlike every
    # other endpoint here, which needs a full signed POST). Kept as its
    # own thing rather than folded into the general shape, since the other
    # endpoints have NOT been confirmed to accept this and shouldn't be
    # assumed to.
    c = _resolve_credentials("weeklyreport")
    params = {
        "openid": c["openid"], "token": c["token"], "game_id": c["game_id"],
        "channel": c["channel"], "account_type": c["account_type"],
        "lang_type": c["lang_type"],
    }
    return _get_json(WEEKLYREPORT_URL, params, on_debug=on_debug)


def fetch_match_detail(room_id: str, on_debug=None) -> dict:
    refresh_credentials_from_browser()
    require_credentials("matchdetail")

    c = _resolve_credentials("matchdetail")
    params = {
        "openid": c["openid"], "token": c["token"], "game_id": c["game_id"],
        "channel": c["channel"], "account_type": c["account_type"], "lang_type": c["lang_type"],
        "u": c["u"], "a": c["a"], "ts": c["ts"], "s": c["s"],
    }
    body = {
        "needLogin": True, "room_id": str(room_id), "report_type": 1,
        "openid": c["openid"], "token": c["token"], "game_id": c["game_id"],
        "account_type": c["account_type"], "lang_type": c["lang_type"],
    }
    return _post_json(MATCHDETAIL_URL, params, body, on_debug=on_debug)


_cache_lock = threading.Lock()


def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
    else:
        cache = {}
    cache.setdefault("matches", {})
    cache.setdefault("profile", None)
    cache.setdefault("details", {})
    return cache


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def pull_match_detail(room_id: str, on_debug=None, force: bool = False) -> dict:
    rid = str(room_id)

    if not force:
        with _cache_lock:
            cache = load_cache()
        cached = cache["details"].get(rid)
        if cached is not None:
            return cached

    data = fetch_match_detail(rid, on_debug=on_debug)

    with _cache_lock:
        cache = load_cache()
        cache["details"][rid] = data
        save_cache(cache)

    return data


def backfill_details(on_progress=None, on_debug=None, should_stop=None) -> int:
    def prog(msg):
        if on_progress:
            on_progress(msg)

    def stopped():
        return bool(should_stop()) if should_stop else False

    with _cache_lock:
        cache = load_cache()
    room_ids = [rid for rid in cache["matches"].keys() if rid not in cache["details"]]
    total = len(room_ids)

    if total == 0:
        prog("All cached matches already have detail.")
        return 0

    prog(f"Backfilling {total} match detail(s) at "
         f"{REQUEST_DELAY_SECONDS:.1f}s each…")
    count = 0
    rate_limit_hits = 0
    for i, rid in enumerate(room_ids, start=1):
        if stopped():
            prog(f"Backfill cancelled after {count}/{total} match(es).")
            break
        try:
            data = fetch_match_detail(rid, on_debug=on_debug)
            with _cache_lock:
                cache = load_cache()
                cache["details"][rid] = data
                save_cache(cache)
            count += 1
            prog(f"Backfilled {count}/{total} match(es)…")
        except RateLimitedError as e:
            rate_limit_hits += 1
            prog(f"Throttled on {rid}; skipping ({e}).")
        except Exception as e:
            prog(f"Skipped {rid} ({e})")
        if i < total:
            time.sleep(REQUEST_DELAY_SECONDS)

    if not stopped():
        extra = (f" ({rate_limit_hits} rate-limit hit(s) — re-run to retry)"
                 if rate_limit_hits else "")
        prog(f"Backfill finished: {count}/{total} fetched{extra}.")
    return count


def pull_matches(refresh_all: bool = False, on_progress=None, on_debug=None,
                 max_workers: int = None) -> dict:
    """Fetch new matches sequentially, with throttle-aware retries.

    Deliberately NOT parallelized. The DfTools proxy returns
    `code=210200, "request too frequently"` (HTTP 200) when two requests
    land within the same ~1s window, and parallel fetching leaves holes in
    the cache that subsequent incremental refreshes never repair.

    `max_workers` is accepted for signature compatibility but ignored.
    """
    def prog(msg):
        if on_progress:
            on_progress(msg)

    cache = {"matches": {}} if refresh_all else load_cache()
    known_room_ids = set(cache["matches"].keys())

    prog(f"Starting... ({len(known_room_ids)} match(es) already cached)")

    new_count = 0
    hit_known = False
    failed_pages = []

    def absorb(matches):
        nonlocal new_count, hit_known
        for m in matches:
            room_id = str(m["room_id"])
            if room_id in known_room_ids:
                hit_known = True
                return
            cache["matches"][room_id] = m
            new_count += 1

    prog("Fetching page 1...")
    try:
        first = _fetch_page_with_retry(1, PAGE_SIZE,
                                       on_debug=on_debug,
                                       on_progress=on_progress)
    except RateLimitedError as e:
        prog(f"Page 1 failed even after retries: {e}")
        first = {"list": [], "total": 0}

    total = first.get("total", 0)
    absorb(first.get("list", []))
    prog(f"Page 1: {new_count} new match(es) ({total} on server)")

    if not hit_known and total > PAGE_SIZE:
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        prog(f"Fetching {total_pages - 1} more page(s) sequentially "
             f"({REQUEST_DELAY_SECONDS:.1f}s between pages)…")

        for page in range(2, total_pages + 1):
            if hit_known:
                break
            time.sleep(REQUEST_DELAY_SECONDS)
            try:
                data = _fetch_page_with_retry(page, PAGE_SIZE,
                                              on_debug=on_debug,
                                              on_progress=on_progress)
                absorb(data.get("list", []))
                prog(f"Page {page}/{total_pages}: {new_count} new match(es) so far")
            except RateLimitedError as e:
                prog(f"Page {page} failed: {e}")
                failed_pages.append(page)

    with _cache_lock:
        fresh = load_cache()
        fresh["matches"] = cache["matches"]
        save_cache(fresh)

    if failed_pages:
        pages_str = ", ".join(str(p) for p in failed_pages)
        prog(f"Done with {len(failed_pages)} failed page(s) ({pages_str}). "
             f"Re-run Full Refresh in a minute to fill the gaps.")
    else:
        prog(f"Done. {new_count} new match(es) fetched, "
             f"{len(cache['matches'])} total in cache.")

    # Best-effort RPC update. Wrapped so an RPC failure can never
    # break the fetch.
    try:
        import delta_force_rpc
        if delta_force_rpc.is_enabled():
            rows_for_rpc = build_rows(fresh)
            delta_force_rpc.update_presence(
                rows_for_rpc,
                load_cache().get("profile"),
            )
    except Exception:
        pass
    return fresh


def refresh_today(on_progress=None, on_debug=None,
                  pages: int = QUICK_REFRESH_PAGES) -> dict:
    def prog(msg):
        if on_progress:
            on_progress(msg)

    cache = load_cache()
    known_room_ids = set(cache["matches"].keys())

    prog(f"Quick refresh (up to {pages} page(s))...")
    new_count = 0
    stop = False

    for page in range(1, pages + 1):
        if page > 1:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            data = _fetch_page_with_retry(page, PAGE_SIZE,
                                          on_debug=on_debug,
                                          on_progress=on_progress)
        except RateLimitedError as e:
            prog(f"Page {page} failed: {e}")
            break

        matches = data.get("list", [])
        if not matches:
            break

        for m in matches:
            room_id = str(m["room_id"])
            if room_id in known_room_ids:
                stop = True
                break
            cache["matches"][room_id] = m
            new_count += 1

        prog(f"Page {page}: {new_count} new match(es) so far")

        if stop:
            break
        if page * PAGE_SIZE >= data.get("total", 0):
            break

    with _cache_lock:
        fresh = load_cache()
        fresh["matches"] = cache["matches"]
        save_cache(fresh)
    prog(f"Quick refresh done. {new_count} new match(es), "
         f"{len(cache['matches'])} total cached.")

    try:
        import delta_force_rpc
        if delta_force_rpc.is_enabled():
            rows_for_rpc = build_rows(fresh)
            delta_force_rpc.update_presence(
                rows_for_rpc,
                load_cache().get("profile"),
            )
    except Exception:
        pass
    return fresh


def pull_profile(on_progress=None, on_debug=None) -> dict:
    def prog(msg):
        if on_progress:
            on_progress(msg)

    prog("Fetching profile...")
    data = fetch_my_data(on_debug=on_debug)

    with _cache_lock:
        cache = load_cache()
        cache["profile"] = data
        save_cache(cache)

    prog("Profile updated.")
    try:
        import delta_force_rpc
        if delta_force_rpc.is_enabled():
            rows_for_rpc = build_rows(load_cache())
            delta_force_rpc.update_presence(rows_for_rpc, data)
    except Exception:
        pass
    return data


def pull_asset_calendar(on_progress=None, on_debug=None) -> dict:
    def prog(msg):
        if on_progress:
            on_progress(msg)

    prog("Fetching recent high-value items...")
    data = fetch_asset_calendar(on_debug=on_debug)

    with _cache_lock:
        cache = load_cache()
        cache["asset_calendar"] = data
        cache["asset_calendar_updated_at"] = datetime.now().isoformat()
        save_cache(cache)

    prog("High-value items updated.")
    return data


def pull_weekly_report(on_progress=None, on_debug=None) -> dict:
    def prog(msg):
        if on_progress:
            on_progress(msg)

    prog("Fetching weekly report...")
    data = fetch_weekly_report(on_debug=on_debug)

    with _cache_lock:
        cache = load_cache()
        cache["weekly_report"] = data
        cache["weekly_report_updated_at"] = datetime.now().isoformat()
        save_cache(cache)

    prog("Weekly report updated.")
    return data


def format_relative_time(iso_str: str) -> str:
    """'2026-09-26T02:10:00' -> 'just now' / '5m ago' / '3h ago' /
    '2d ago', for the "Updated ..." labels on cards backed by data that
    only refreshes on login/startup or a manual click - so it's clear
    whether what's on screen is fresh or from a while back, without
    forcing anyone to go check Settings for a raw timestamp. Returns ''
    (render nothing) for a missing or unparseable timestamp, e.g. data
    that predates this feature or was never successfully fetched."""
    if not iso_str:
        return ""
    try:
        then = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return ""
    seconds = (datetime.now() - then).total_seconds()
    if seconds < 0:
        return "just now"  # clock skew or a stale cached negative delta
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours}h ago"
    days = int(hours // 24)
    return f"{days}d ago"


def weekly_highlights(cache: dict = None) -> dict:
    """Extracts the handful of GetWeeklyReportSolData fields worth
    showing in the GUI: extraction rate, K/D, the squad-mate profited
    most/least with this week, and the 7-day total-stash-value trend.

    Deliberately leaves out most of that response's other fields
    (highlight_index, keyword_index, keyword_index_list, and friends) -
    those look like indices into DfTools' own client-side text
    templates ("you extracted successfully in X% of raids" style
    sentences), not directly usable data without finding that template
    table first, the same way delta_force_items.json had to be found
    for item names.

    Returns None if nothing's been fetched yet - the GUI shows an empty
    state for that, same pattern as recent_high_value_items()."""
    cache = cache if cache is not None else load_cache()
    data = cache.get("weekly_report") or {}
    if not data:
        return None

    def _friend(key):
        f = data.get(key) or {}
        if not f.get("has_friend"):
            return None
        return {
            "name": f.get("role_name") or "—",
            "matches": int(f.get("match_num", 0) or 0),
            "value": int(f.get("value", 0) or 0),
        }

    trend = []
    for point in (data.get("total_price_weekly_list") or []):
        try:
            trend.append({
                "date": str(point.get("Date", "")),
                "value": int(point.get("Price", 0) or 0),
            })
        except (TypeError, ValueError):
            continue

    evac_rate = data.get("exacuation_rate")  # API's own typo, not ours
    kd_rate = data.get("kd_rate")

    def _items(id_list):
        # These lists are bare item IDs with no per-extraction value or
        # count attached (unlike GetAssetWeekCalendar's carry_out_items) -
        # "value" here is the item catalog's own reference value, not
        # what this specific extraction was actually worth.
        out = []
        for item_id in (id_list or [])[:3]:
            info = describe_item(str(item_id))
            out.append({
                "item_id": str(item_id), "name": info["name"],
                "grade": info["grade"], "image_url": info["image_url"],
                "value": info["value"],
            })
        return out

    highlight_value = data.get("highlight_max_gainedprice_gainedprice")

    return {
        "evac_rate": float(evac_rate) if evac_rate is not None else None,  # 0.0-1.0
        "kd_rate": float(kd_rate) if kd_rate is not None else None,
        "best_friend": _friend("best_friend"),
        "worst_friend": _friend("worst_friend"),
        "trend": trend,  # oldest first, [{date: "YYYYMMDD", value: int}, ...]
        # This week's most valuable extracted items, and separately the
        # items from this week's single best raid (the "Highlight Match" /
        # "Million Extract" section of the real weekly report) - a
        # reliable source of "recent valuable loot" independent of
        # GetAssetWeekCalendar, which can come back empty even when these
        # are populated.
        "top_items": _items(data.get("most_valuable_collection_id_list")),
        "highlight_items": _items(data.get("highlight_most_valuable_collection_id_list")),
        "highlight_value": int(highlight_value) if highlight_value else 0,
    }


# ---------------------------------------------------------------------
# Item catalog (name/grade/value/icon lookup by item_id)
# ---------------------------------------------------------------------
ITEM_CATALOG_FILE = resource_path("delta_force_items.json")
_item_catalog_cache = None


def load_item_catalog() -> dict:
    """Loads the prop_id -> {name, grade, value, image_url} lookup used
    to label carry_out_items from GetAssetWeekCalendar. Cached in memory
    after the first call. Returns {} if the catalog file is missing
    rather than raising - an unlabeled item_id should degrade to showing
    the raw ID, never break the fetch."""
    global _item_catalog_cache
    if _item_catalog_cache is not None:
        return _item_catalog_cache
    try:
        _item_catalog_cache = json.loads(ITEM_CATALOG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if _debug:
            print(f"[core] couldn't load item catalog from "
                  f"{ITEM_CATALOG_FILE}: {e}")
        _item_catalog_cache = {}
    return _item_catalog_cache


def describe_item(item_id: str) -> dict:
    """Looks up one item_id. Always returns a dict with name/grade/value/
    image_url, even for an unknown ID (name falls back to the raw ID)."""
    entry = load_item_catalog().get(str(item_id))
    if entry:
        return entry
    return {"name": f"Item {item_id}", "grade": None, "value": None, "image_url": ""}


# ---------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------
def to_local_date(match_time: str) -> datetime:
    return datetime.fromtimestamp(int(match_time))


def build_rows(cache: dict) -> list:
    rows = []
    for m in cache["matches"].values():
        dt = to_local_date(m["match_time"])
        op_kills, bot_kills = kill_breakdown(m)
        rows.append({
            "room_id": m["room_id"],
            "datetime": dt,
            "date": dt.strftime("%Y-%m-%d"),
            "week": dt.strftime("%G-W%V"),
            "month": dt.strftime("%Y-%m"),
            "net_income": int(m["net_income"]) / CURRENCY_DIVISOR,
            "kill_count": m.get("kill_count", 0),
            "operator_kills": op_kills,
            "bot_kills": bot_kills,
            "result": "win" if m.get("result") == 1 else "loss" if m.get("result") == 2 else "?",
            "map_id": m.get("map_id"),
            "map_name": map_name(m.get("map_id")),
            "map_base": map_base(m.get("map_id")),
            "map_variant": map_variant(m.get("map_id")),
            "operator_id": m.get("operator_id"),
            "operator_name": operator_name(m.get("operator_id")),
            "operator_avatar": operator_avatar(m.get("operator_id")),
            "rank_score": m.get("rank_score") or m.get("score"),
            "_raw": m,
        })
    rows.sort(key=lambda r: r["datetime"], reverse=True)
    return rows


def summarize(rows: list, key: str) -> list:
    groups = defaultdict(lambda: {"net_income": 0, "matches": 0, "wins": 0, "losses": 0})
    for r in rows:
        g = groups[r[key]]
        g["net_income"] += r["net_income"]
        g["matches"] += 1
        if r["result"] == "win":
            g["wins"] += 1
        elif r["result"] == "loss":
            g["losses"] += 1
    return sorted(
        [{"period": k, **v} for k, v in groups.items()],
        key=lambda x: x["period"],
        reverse=True,
    )


def overview(rows: list) -> dict:
    if not rows:
        return {"today": 0, "week": 0, "month": 0, "all_time": 0,
                "matches": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "all_time_matches": 0}

    today = datetime.now().strftime("%Y-%m-%d")
    this_week = datetime.now().strftime("%G-W%V")
    this_month = datetime.now().strftime("%Y-%m")

    total_matches = len(rows)
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")

    return {
        "today": sum(r["net_income"] for r in rows if r["date"] == today),
        "week": sum(r["net_income"] for r in rows if r["week"] == this_week),
        "month": sum(r["net_income"] for r in rows if r["month"] == this_month),
        "all_time": sum(r["net_income"] for r in rows),
        "matches": total_matches,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total_matches * 100) if total_matches else 0.0,
        "all_time_matches": total_matches,
    }


def best_map_and_operator(rows: list) -> tuple:
    """Highest total net income grouped by map / by operator name - the
    definition behind the rail's 'Best Map' / 'Best Operator' and the
    leaderboard's columns of the same name."""
    map_groups, op_groups = {}, {}
    for r in rows or []:
        map_groups[r["map_name"]] = map_groups.get(r["map_name"], 0) + r["net_income"]
        op_groups[r["operator_name"]] = op_groups.get(r["operator_name"], 0) + r["net_income"]
    best_map = max(map_groups.items(), key=lambda kv: kv[1])[0] if map_groups else ""
    best_op = max(op_groups.items(), key=lambda kv: kv[1])[0] if op_groups else ""
    return best_map, best_op


def daily_stats_utc(rows: list, now=None, reset_hour: int = 0) -> dict:
    """Totals for the current UTC calendar day - what the community
    daily leaderboard ranks on.

    A UTC day rather than the local calendar day used by the rest of the
    app, on purpose: a leaderboard needs one shared window and one shared
    reset moment, or a player in Manila and one in Los Angeles would be
    racing over different 24 hours. Each match's local timestamp is
    converted to UTC before it's bucketed; `now` (a timezone-aware UTC
    datetime) exists so tests can pin the clock.

    reset_hour is the UTC hour the server's daily board resets at (see
    server/app.py DAILY_RESET_HOUR_UTC); a "day" is the 24 hours from that
    hour, labelled by the UTC date it began on. Must match the server's or
    the totals cover the wrong 24 hours - the server refuses a mismatch and
    the client relearns the hour (see delta_force_community.sync_stats).
    """
    now = now or datetime.now(timezone.utc)
    shift = timedelta(hours=reset_hour)
    day = (now - shift).strftime("%Y-%m-%d")
    today = []
    for r in rows or []:
        try:
            if (r["datetime"].astimezone(timezone.utc) - shift).strftime("%Y-%m-%d") == day:
                today.append(r)
        except (OverflowError, OSError, ValueError):
            continue  # an unconvertible timestamp just doesn't count
    best_map, best_op = best_map_and_operator(today)
    return {
        "day": day,
        "net_income": sum(r["net_income"] for r in today),
        "matches": len(today),
        "wins": sum(1 for r in today if r["result"] == "win"),
        "losses": sum(1 for r in today if r["result"] == "loss"),
        "best_map": best_map,
        "best_operator": best_op,
    }


def best_single_match(rows: list) -> dict:
    """The single highest-net-income match in rows, or None if empty.
    Callers control scope by what they pass in - all-time, today's rows
    only, or a session's rows only."""
    if not rows:
        return None
    return max(rows, key=lambda r: r["net_income"])


def session_stats(rows: list, session_start) -> dict:
    """Aggregate stats for matches played at or after session_start (a
    datetime), regardless of calendar-day boundaries - this is what lets
    a play session that crosses midnight (10pm-2am, say) read as one
    block instead of being split between "yesterday" and "today". Same
    shape as one of overview()'s per-window dicts, plus best_match like
    best_single_match(). Returns a zeroed dict (not an error) if
    session_start is None or nothing's been played yet in the session -
    an empty/just-started session is a normal state, not a failure."""
    empty = {"net_income": 0, "matches": 0, "wins": 0, "losses": 0,
             "win_rate": 0.0, "best_match": None}
    if not rows or session_start is None:
        return empty

    session_rows = [r for r in rows if r["datetime"] >= session_start]
    if not session_rows:
        return empty

    matches = len(session_rows)
    wins = sum(1 for r in session_rows if r["result"] == "win")
    losses = sum(1 for r in session_rows if r["result"] == "loss")
    return {
        "net_income": sum(r["net_income"] for r in session_rows),
        "matches": matches,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / matches * 100) if matches else 0.0,
        "best_match": best_single_match(session_rows),
    }


def overlay_snapshot(rows: list = None, cache: dict = None,
                     session_start=None) -> dict:
    """Bundles everything delta_force_overlay.OverlayWindow needs into
    one call. Two modes, chosen by whether a session is active:

    session_start is None -> "daily" mode: net profit/loss and best raid
    are scoped to TODAY (calendar day), not all-time - a glance overlay
    answering "how's today going" is more useful than an all-time record
    that barely changes.

    session_start is a datetime -> "session" mode: net profit/loss and
    best raid are scoped to matches since session_start instead, so a
    play session spanning midnight reads as one block. See
    session_stats() above.

    Either way, top_items (recent high-value loot) is unaffected by
    session/daily scope - it's already its own "recent" window from the
    API, not something reasonable to further split by session.

    Never raises - every field falls back to a safe empty value, since
    the overlay's whole point is a lightweight glance that shouldn't
    itself become a source of errors popping up over someone's game.
    """
    rows = rows if rows is not None else build_rows(load_cache())

    if session_start is not None:
        sess = session_stats(rows, session_start)
        net = sess["net_income"]
        best = sess["best_match"]
        mode = "session"
    else:
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_rows = [r for r in rows if r["date"] == today_str]
        net = sum(r["net_income"] for r in today_rows)
        best = best_single_match(today_rows)
        mode = "daily"

    return {
        "mode": mode,
        "net": net,
        "best_match": best,  # None, or a row dict with net_income/map_name/etc.
        "top_items": recent_high_value_items(cache, limit=3),
        "session_start": session_start,
    }


def recent_high_value_items(cache: dict = None, limit: int = 12) -> list:
    """Enriched, display-ready version of GetAssetWeekCalendar's
    carry_out_items: [{item_id, name, grade, value, image_url, count,
    total_value}, ...], sorted by total_value descending. Returns []
    if nothing's been fetched yet - never raises, since this is a
    "nice to have" card that shouldn't block the rest of the Overview
    tab if it's empty or the fetch hasn't run."""
    cache = cache if cache is not None else load_cache()
    calendar = cache.get("asset_calendar") or {}
    raw_items = calendar.get("carry_out_items") or []

    enriched = []
    for it in raw_items:
        item_id = str(it.get("item_id", ""))
        count = int(it.get("carry_out_count", 1) or 1)
        # item_value from the API is already the line total (it isn't
        # unit price * count - e.g. a 3x line worth ~130k is close to
        # 3x a ~43k item, not 3x some other unit price), so use it as-is.
        total_value = int(it.get("item_value", 0) or 0)
        info = describe_item(item_id)
        enriched.append({
            "item_id": item_id,
            "name": info["name"],
            "grade": info["grade"],
            "unit_value": info["value"],
            "image_url": info["image_url"],
            "count": count,
            "total_value": total_value,
        })

    enriched.sort(key=lambda x: x["total_value"], reverse=True)
    return enriched[:limit]


def format_profile(profile: dict) -> list:
    if not profile:
        return []

    info = profile.get("player_info") or {}
    rank = profile.get("rank_data") or {}
    summary = profile.get("summary_data") or {}
    combat = summary.get("combat") or {}
    economy = summary.get("economy") or {}
    team = summary.get("team") or {}

    def money(v):
        try:
            return f"{int(float(v)):,}"
        except (TypeError, ValueError):
            return str(v) if v is not None else "—"

    def pct(v):
        try:
            return f"{float(v) * 100:.1f}%"
        except (TypeError, ValueError):
            return str(v) if v is not None else "—"

    def reg_date(ts):
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return str(ts) if ts is not None else "—"

    def s(v):
        return str(v) if v is not None else "—"

    current_rank_label = s(rank.get("current_rank"))
    score = _first_number(rank.get("current_rank_score"),
                          rank.get("rank_score"),
                          rank.get("score"))
    if score is not None:
        current_rank_label = rank_for_score(score, "SOL")

    highest_rank = rank.get("highest_rank")
    highest_rank = s(highest_rank)

    highest_season = rank.get("highest_rank_season_id")
    highest_season_label = season_name(highest_season) if highest_season else "—"

    return [
        ("Player", [
            ("Nickname", s(info.get("nickname"))),
            ("Level", s(info.get("level"))),
            ("Play Time", f"{info.get('play_duration', '—')} hrs"),
            ("Registered", reg_date(info.get("register_time"))),
        ]),
        ("Rank", [
            ("Current Rank", current_rank_label),
            ("Current Rank Score", s(rank.get("current_rank_score"))),
            ("Highest Rank", highest_rank),
            ("Highest Rank Season", highest_season_label),
        ]),
        ("Combat", [
            ("Total Matches", s(summary.get("total_match_count"))),
            ("Kills", s(combat.get("kill_operator_count"))),
            ("Headshot Rate", pct(combat.get("headshot_kill_rate"))),
            ("Hit Rate", pct(combat.get("hit_rate"))),
            ("K/D (Low Tier)", s(combat.get("low_kill_death_ratio"))),
            ("K/D (Med Tier)", s(combat.get("med_kill_death_ratio"))),
            ("K/D (High Tier)", s(combat.get("high_kill_death_ratio"))),
        ]),
        ("Economy", [
            (f"Total Extract Value ({CURRENCY_LABEL})", money(economy.get("extract_value"))),
            (f"Total Reward ({CURRENCY_LABEL})", money(economy.get("total_reward"))),
            ("Profit/Loss Ratio", s(economy.get("profit_loss_ratio"))),
            ("Mandel Bricks Extracted", s(economy.get("total_mandel_brick"))),
        ]),
        ("Team", [
            ("Teammates Revived", s(team.get("revive_teammate_count"))),
            ("Teammates Rescued", s(team.get("rescue_teammate_count"))),
            ("Retreat Rate", pct(team.get("retreat_rate"))),
            (f"Teammate Extract Value ({CURRENCY_LABEL})", money(team.get("teammate_extract_value"))),
        ]),
    ]


def fmt_money(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}{abs(v):,.0f} {CURRENCY_LABEL}"


def export_csv(rows: list, summaries: dict) -> list:
    paths = []

    matches_path = APP_DATA_DIR / "match_history_export.csv"
    with open(matches_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "date", "datetime", "room_id", "net_income", "result",
            "kill_count", "operator_kills", "bot_kills",
            "map_id", "map_name", "map_base", "map_variant",
            "operator_id", "operator_name", "week", "month",
        ])
        writer.writeheader()
        for r in rows:
            row = {k: r.get(k) for k in writer.fieldnames}
            row["datetime"] = r["datetime"].isoformat()
            writer.writerow(row)
    paths.append(matches_path)

    for period_name, summary in summaries.items():
        path = APP_DATA_DIR / f"summary_{period_name}.csv"
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["period", "matches", "wins", "losses", "net_income"])
            writer.writeheader()
            writer.writerows(summary)
        paths.append(path)

    return paths