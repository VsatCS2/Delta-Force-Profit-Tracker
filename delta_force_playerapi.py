"""
Player lookup backend for the private Delta Force API (x-secret header).

Drop-in replacement for the player half of delta_force_thirdparty: same
function name, same return shape, so the overlay and the Player Lookup page
need no changes.  Two transports, chosen by delta_force_config.py:

  PLAYERAPI_PROXY_URL  -> calls YOUR server (playerapi_proxy.py); the secret
                          lives only on that server.   <-- use this for any
                          build you give to other people
  PLAYERAPI_URL + PLAYERAPI_SECRET -> calls the API directly (local dev only;
                          a secret inside a distributed app can be extracted).
                          Use https://delta-force-api.p.rapidapi.com with your
                          RapidAPI key as the secret.
  PLAYERAPI_DETAIL = False  skips the 3rd (season-detail) call: 1/3 fewer paid
                          requests per lookup, but Scores/Revives/Pickups show 0

Never raises from search_player(): returns None on any failure, exactly like
the module it replaces.

Probe (shows the raw API responses so field mapping can be verified):
    python delta_force_playerapi.py --probe SomeNickname
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from urllib.parse import urlparse

import requests

try:
    import delta_force_config as _cfg
except ImportError:
    _cfg = None


def _c(name, default=""):
    return os.environ.get(name) or getattr(_cfg, name, default) or default


def _url(value):
    """Trim an address from the config and add https:// if it was left off."""
    u = (value or "").strip().rstrip("/")
    if u and "://" not in u:
        u = ("http://" if u.startswith(("localhost", "127.")) else "https://") + u
    return u


API_URL = _url(_c("PLAYERAPI_URL"))
API_SECRET = _c("PLAYERAPI_SECRET")
PROXY_URL = _url(_c("PLAYERAPI_PROXY_URL"))
PROXY_TOKEN = _c("PLAYERAPI_PROXY_TOKEN")

_DETAIL = str(_c("PLAYERAPI_DETAIL", "1")).lower() not in ("0", "false", "no", "")
# Optional rank names. The API only returns rank POINTS, not a tier name, so
# names have to come from a table: put RANK_TIERS in delta_force_config.py as
# [(0, "Bronze"), (2000, "Silver"), ...] (minimum points for each tier).
RANK_TIERS = sorted((int(p), str(n)) for p, n in (getattr(_cfg, "RANK_TIERS", []) or []))


def rank_tier(points):
    name = ""
    for minimum, label in RANK_TIERS:
        if points >= minimum:
            name = label
    return name


# Accounts: when set, every proxy call carries the signed-in user's token
# (see delta_force_accounts.py). None -> the old shared client-token behaviour.
_AUTH_PROVIDER = None
_QUOTA_LISTENER = None


def set_auth_provider(fn):
    """fn() -> access token string, or None when signed out."""
    global _AUTH_PROVIDER
    _AUTH_PROVIDER = fn


def set_quota_listener(fn):
    """fn(used, limit) is called with the server's usage numbers after a lookup."""
    global _QUOTA_LISTENER
    _QUOTA_LISTENER = fn


_TIMEOUT = (3, 8)
# Results are cached in memory AND on disk (survives restarts). Every
# uncached lookup costs paid upstream requests, and player stats only change
# per match, so a long-ish TTL is the right trade.
_CACHE_TTL = int(_c("PLAYERAPI_CACHE_SECONDS", "1800"))
_cache = {}
_disk_loaded = False
FAILURE = {"reason": ""}   # why the last search_player() returned None
LAST = {}          # last transport result, for --probe diagnostics (never has secrets)
_session = requests.Session()
_session.headers["User-Agent"] = "DFTracker/1.0"


def is_configured() -> bool:
    return bool(PROXY_URL or (API_URL and API_SECRET))


def auto_warmup_enabled() -> bool:
    """The app looks up the signed-in user's own extended stats at launch.
    That spends paid requests for every user on every launch - turn it off
    (PLAYERAPI_AUTO_WARMUP = False) while the request quota is small."""
    explicit = str(_c("PLAYERAPI_AUTO_WARMUP", "")).strip().lower()
    if explicit:
        return explicit not in ("0", "false", "no")
    # Not set: with accounts on, a launch-time lookup (about 3 requests) would
    # eat into every user's daily budget, so it is off unless you turn it on.
    return not _c("ACCOUNTS_GATEWAY_URL", "")


# ------------------------------------------------------------ disk cache
def _cache_path():
    try:
        from delta_force_paths import APP_DATA_DIR
        return APP_DATA_DIR / "playerapi_cache.json"
    except Exception:
        return None


def _load_disk_cache():
    global _disk_loaded
    if _disk_loaded:
        return
    _disk_loaded = True
    path = _cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
    except Exception:
        return
    now = time.time()
    for key, (ts, result) in (raw or {}).items():
        if now - ts < _CACHE_TTL and isinstance(result, dict):
            _cache.setdefault(key, (ts, result))


def _save_disk_cache():
    path = _cache_path()
    if not path:
        return
    now = time.time()
    fresh = {k: [ts, r] for k, (ts, r) in _cache.items() if now - ts < _CACHE_TTL}
    if len(fresh) > 200:                       # keep the newest 200
        fresh = dict(sorted(fresh.items(), key=lambda kv: kv[1][0])[-200:])
    try:
        path.write_text(json.dumps(fresh), encoding="utf-8")
    except Exception:
        pass                                   # a cache must never break a lookup


# --------------------------------------------------------- failure reasons
def _gateway_error():
    """(error_code, body) from the last response, if it was the gateway's JSON."""
    try:
        body = json.loads(LAST.get("body") or "{}")
        return (body.get("error") or ""), (body if isinstance(body, dict) else {})
    except (ValueError, TypeError, AttributeError):
        return "", {}


def _classify_failure() -> str:
    """Map the last transport problem to a reason the UI can explain."""
    problem = LAST.get("problem") or ""
    if not problem:
        return "not_found"
    http = LAST.get("http")
    upstream = 0
    err, body = _gateway_error()
    # The accounts gateway talks about the USER's situation; handle that before
    # the generic "upstream" cases below.
    if err in ("sign_in_required", "invalid_token", "token_expired"):
        return "signin"
    if err == "feature_locked":
        return "disabled" if body.get("disabled") else "locked"
    if err == "banned":
        return "banned"
    if err == "quota":
        return "quota_daily"
    if err == "rate_limited":
        return "rate"
    if err == "service_busy":
        return "busy"
    try:                                       # proxy wraps upstream errors in a 502
        upstream = int(body.get("status") or 0)
    except (ValueError, TypeError, AttributeError):
        pass
    if http == 429 or upstream == 429:
        return "quota"
    if http in (401, 403) or upstream in (401, 403):
        return "auth"
    if problem.startswith("network error"):
        return "unreachable"
    return "error"


_MESSAGES = {
    "signin": "Sign in to your DF Tracker account (Settings, Account) to look up players.",
    "locked": "Player lookup is part of Pro.",
    "disabled": "Player lookup is temporarily unavailable.",
    "banned": "This account has been suspended.",
    "quota_daily": "You've reached today's lookup limit. It resets at midnight UTC.",
    "rate": "You're searching too fast. Try again in a minute.",
    "busy": "Lookups are busy right now. Try again later.",
    "quota": "Lookup limit reached for now - try again in a while.",
    "auth": "The player service rejected the request (API key problem).",
    "unreachable": "Couldn't reach the player service. Check your connection.",
    "error": "The player service returned an error - try again in a moment.",
    "no_stats": "Found the player, but no stats are available for them.",
}


def failure_message(name: str) -> str:
    """Human-readable reason for the last failed search_player(name)."""
    reason = FAILURE.get("reason") or "not_found"
    if reason == "locked":
        _, body = _gateway_error()
        return f"Player lookup is part of {str(body.get('min_tier') or 'pro').title()}."
    return _MESSAGES.get(reason, f'No player found for "{name}".')


def failure_reason() -> str:
    return FAILURE.get("reason") or ""


# ----------------------------------------------------------------- transport
def auth_headers(base_url: str, secret: str) -> dict:
    host = urlparse(base_url).netloc
    if host.endswith("rapidapi.com"):
        return {"x-rapidapi-key": secret, "x-rapidapi-host": host}
    return {"x-secret": secret}


def _call(path: str, body: dict):
    """POST one whitelisted endpoint. Parsed JSON dict, or None on failure."""
    try:
        if PROXY_URL:
            headers = {}
            if _AUTH_PROVIDER is not None:
                token = _AUTH_PROVIDER()
                if not token:                  # signed out: don't waste a round trip
                    LAST.update(path=path, via="proxy", http=401,
                                body='{"error":"sign_in_required"}', problem="HTTP 401")
                    return None
                headers["Authorization"] = f"Bearer {token}"
            elif PROXY_TOKEN:
                headers["x-client-token"] = PROXY_TOKEN
            r = _session.post(f"{PROXY_URL}/v1/call", headers=headers,
                              json={"path": path, "body": body},
                              timeout=_TIMEOUT)
        elif API_URL and API_SECRET:
            r = _session.post(f"{API_URL}{path}", json=body,
                              headers=auth_headers(API_URL, API_SECRET),
                              timeout=_TIMEOUT)
        else:
            LAST.update(path=path, problem="not configured")
            return None
        LAST.update(path=path, via="proxy" if PROXY_URL else "direct",
                    http=r.status_code, body=r.text[:300], problem=None)
        if r.status_code != 200:
            LAST["problem"] = f"HTTP {r.status_code}"
            return None
        data = r.json()
        used = r.headers.get("X-DF-Quota-Used")
        if used is not None and _QUOTA_LISTENER:
            try:
                lim = r.headers.get("X-DF-Quota-Limit")
                _QUOTA_LISTENER(int(used), int(lim) if lim else None)
            except (ValueError, TypeError):
                pass
        if isinstance(data, dict) and data.get("result", 0) not in (0, None):
            LAST["problem"] = f"API error code result={data.get('result')}"
            return None          # API-level error code (Err enum), e.g. not found
        return data
    except requests.exceptions.RequestException as e:
        LAST.update(path=path, problem=f"network error: {type(e).__name__}")
        return None
    except ValueError:
        LAST["problem"] = "response was not JSON"
        return None


# ------------------------------------------------------------------- helpers
def _i(v, d=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _ratio(a, b) -> float:
    return (a / b) if b else 0.0


def _iso(ts) -> str:
    ts = _i(ts)
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# Career SOL totals. mode is tried empty first, then the SOL values from the
# API's enums (WorldGameMode=1 / SOLGameRule=4), because the spec doesn't say
# which one the server expects.
_SOL_MODES = ({}, {"game_mode": 1, "game_rule": 4})


def _career(pid: int):
    for mode in _SOL_MODES:
        d = _call("/player/get/season",
                  {"player_id": pid, "is_career": True, "mode": mode})
        if d and (d.get("sol_data") or {}).get("total_fight"):
            return d
    return None


def _season_detail(pid: int):
    for mode in _SOL_MODES:
        d = _call("/player/get/stats",
                  {"player_id": pid, "season_no": 0, "is_rank": False,
                   "mode": mode})
        if d and d.get("sol_data"):
            return d
    return None


# ------------------------------------------------------------------- mapping
def _radar_scores(dsol: dict) -> dict:
    """Five category scores. Real responses carry season_avg_radar_dims as
    [{dim, score}, ...]; the spec also lists a named season_avg_radar object.
    Assumed dim order (matches the old gateway's field order):
    1 combat, 2 survival, 3 coop, 4 search, 5 wealth.  VERIFY in-game."""
    named = dsol.get("season_avg_radar") or {}
    if named:
        return {"combat": _i(named.get("fight")), "survival": _i(named.get("survive")),
                "coop": _i(named.get("team")), "search": _i(named.get("search")),
                "wealth": _i(named.get("wealth"))}
    by_dim = {_i(d.get("dim")): _i(d.get("score"))
              for d in (dsol.get("season_avg_radar_dims") or [])}
    return {"combat": by_dim.get(1, 0), "survival": by_dim.get(2, 0),
            "coop": by_dim.get(3, 0), "search": by_dim.get(4, 0),
            "wealth": by_dim.get(5, 0)}


def build_result(info: dict, career: dict, detail: dict) -> dict:
    """Map API payloads onto the dict shape the UI already consumes.
    Every key the UI indexes is always present (0 when the API lacks it).
    The career call has stash + quit counts; the season-detail call has
    bullets/knocks/revives/pickups/radar - each field is read from whichever
    response carries it."""
    info, career, detail = info or {}, career or {}, detail or {}
    sol = career.get("sol_data") or {}
    dsol = detail.get("sol_data") or {}

    def pick(key):                      # career first, season detail as fallback
        return sol.get(key) if sol.get(key) not in (None, "") else dsol.get(key)

    fights = _i(pick("total_fight") or career.get("total_fight"))
    escaped = _i(pick("total_escape") or career.get("total_escaped"))
    kills = _i(pick("total_kill"))
    deaths = _i(pick("total_killed"))
    shoot = _i(pick("total_shoot"))
    hit = _i(pick("total_shoot_hit"))
    knocks = _i(pick("total_shoot_down"))
    head = _i(pick("total_shoot_head_down"))
    scores = _radar_scores(dsol)

    stats = {}
    if sol or dsol:
        stats = {
            "matches_played": fights,
            "matches_extracted": escaped,
            "matches_lost": deaths,
            "matches_quit": _i(pick("total_quit")),
            "extraction_rate": _ratio(escaped, fights),
            "kd_ratio": _f(career.get("kd")) or _ratio(kills, max(deaths, 1)),
            "total_kills": kills,
            "total_deaths": deaths,
            "kd_ratio_easy": _ratio(_i(pick("kill_low_stakes")),
                                    max(_i(pick("killed_low_stakes")), 1)),
            "kd_ratio_medium": _ratio(_i(pick("kill_med_stakes")),
                                      max(_i(pick("killed_med_stakes")), 1)),
            "kd_ratio_hard": _ratio(_i(pick("kill_high_stakes")),
                                    max(_i(pick("killed_high_stakes")), 1)),
            "kills_easy": _i(pick("kill_low_stakes")),
            "kills_medium": _i(pick("kill_med_stakes")),
            "kills_hard": _i(pick("kill_high_stakes")),
            "deaths_easy": _i(pick("killed_low_stakes")),
            "deaths_medium": _i(pick("killed_med_stakes")),
            "deaths_hard": _i(pick("killed_high_stakes")),
            "bullets_discharged": shoot,
            "bullets_hit": hit,
            "bullets_missed": max(shoot - hit, 0),
            "bullet_hit_rate": _ratio(hit, shoot),
            "bullets_per_knock": _ratio(shoot, knocks),
            "bullets_hit_per_knock": _ratio(hit, knocks),
            "knocked_count": knocks,
            "knocked_headshots": head,
            "headshot_rate": _ratio(head, knocks),
            "revives": _i(pick("total_revive_teammate")),
            "pickups": _i(pick("total_pickup_teammate")),
            "score_combat": scores["combat"],
            "score_survival": scores["survival"],
            "score_coop": scores["coop"],
            "score_search": scores["search"],
            "score_wealth": scores["wealth"],
            "extracted_value": _i(pick("total_gained_price")),
            "extracted_teammate_value": _i(pick("carry_teammate_assets")
                                           or career.get("carry_teammate_assets")),
            "mandelbricks_extracted": _i(pick("total_bring_mander_box")),
            "ranked_points": _i(pick("rank_score") or info.get("sol_rank_score")),
            "ranked_points_peak": _i(pick("rank_score_max")),
            "play_time_seconds": _i(pick("total_game_time")
                                    or career.get("total_game_time")),
        }

    stash = {}
    cur, non_cur = _i(sol.get("current_asset")), _i(sol.get("non_current_assets"))
    coll = _i(pick("total_collection_price"))
    if cur or non_cur or coll:
        # verified on a real response: total_price == current + non_current
        stash = {"liquid": cur, "fixed": non_cur, "collection": coll,
                 "net": cur + non_cur}

    pid = _i(info.get("player_id"))
    return {
        "name": info.get("nick_name") or "",
        "player_id": str(pid),
        "delta_force_id": "",
        # Real search responses have `level` and `season_lvl` (no account_lvl).
        # Assumed: season_lvl = Operations, level = Warfare. VERIFY in-game.
        "level_operations": _i(info.get("season_lvl")),
        "level_warfare": _i(info.get("level") or info.get("account_lvl")),
        "registered_at": _iso(info.get("register_time")),
        "stats": stats,
        "stash": stash,
        "rank_tier": rank_tier(stats.get("ranked_points", 0)) if stats else "",
        "_source": "playerapi",
    }


# --------------------------------------------------------------------- public
_info_cache = {}                       # name -> (time, search hit); rarely changes
_INFO_TTL = 3600


def _find(name_or_id: str):
    q = name_or_id.strip()
    hit = _info_cache.get(q.lower())
    if hit and time.time() - hit[0] < _INFO_TTL:
        return hit[1]
    found = _find_remote(q)
    if found:
        _info_cache[q.lower()] = (time.time(), found)
    return found


def _find_remote(q: str):
    body = ({"search_player_id_str": q} if q.isdigit()
            else {"search_player_nick": q})
    d = _call("/player/search", body)
    players = (d or {}).get("player_info") or []
    if not players:
        return None
    for p in players:                      # exact (case-insensitive) match wins
        if (p.get("nick_name") or "").lower() == q.lower():
            return p
    return players[0]


def search_player(name_or_id: str, use_cache: bool = True):
    """Same contract as delta_force_thirdparty.search_player."""
    FAILURE["reason"] = ""
    if not name_or_id or not name_or_id.strip() or not is_configured():
        FAILURE["reason"] = "not_found"
        return None
    _load_disk_cache()
    key = name_or_id.strip().lower()
    hit = _cache.get(key)
    if use_cache and hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    LAST.clear()
    info = _find(name_or_id)
    if not info or not info.get("player_id"):
        FAILURE["reason"] = _classify_failure()
        return None                       # not cached: can't tell typo from outage
    pid = _i(info["player_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:      # both calls at once
        f_career = pool.submit(_career, pid)
        f_detail = (pool.submit(_season_detail, pid) if _DETAIL
                    else pool.submit(lambda: None))
        career, detail = f_career.result(), f_detail.result()

    # One of the two parallel calls can fail on a burst (upstream rate limit,
    # timeout). Retry a missing half once, sequentially - unless the failure
    # was quota/auth, where a retry can't help.
    missing_career, missing_detail = career is None, (_DETAIL and detail is None)
    if (missing_career or missing_detail) and _classify_failure() not in (
            "quota", "auth", "signin", "locked", "disabled", "banned",
            "quota_daily", "rate", "busy"):
        time.sleep(1.2)
        if missing_career:
            career = _career(pid)
        if missing_detail:
            detail = _season_detail(pid)
    parts = []
    if career is None:
        parts.append("totals and stash")
    if _DETAIL and detail is None:
        parts.append("accuracy, knocks and scores")

    result = build_result(info, career, detail)
    if parts:
        why = _MESSAGES.get(_classify_failure()) if LAST.get("problem") else ""
        result["_partial_note"] = ("Some stats couldn't be loaded (" + ", ".join(parts)
                                   + "). " + (why + " " if why else "")
                                   + "Search again in a minute.")
    if result["stats"] or result["stash"]:
        # A partial result must not sit in the cache for 30 minutes: backdate
        # it so it expires after ~60s and the missing part is fetched again.
        ts = time.time() - (_CACHE_TTL - 60 if parts else 0)
        _cache[key] = (ts, result)
        _cache[str(pid)] = (ts, result)
        _save_disk_cache()
    else:
        FAILURE["reason"] = _classify_failure() if LAST.get("problem") else "no_stats"
        # The old service "queued" players, so an empty result meant "check
        # back later". This API answers immediately: empty means the stat
        # calls failed, or the player's career data isn't available.
        result["_no_stats_note"] = (
            _MESSAGES.get(FAILURE["reason"]) or _MESSAGES["no_stats"]) + (
            "" if FAILURE["reason"] != "no_stats" else
            " Their career data may be set to private in the game.")
    return result


# ----------------------------------------------------------------------- probe
def _probe(name: str):
    if not is_configured():
        print("Not configured: set PLAYERAPI_PROXY_URL, or PLAYERAPI_URL + "
              "PLAYERAPI_SECRET (env vars or delta_force_config.py)")
        return 1
    t0 = time.time()
    info = _find(name)
    print(f"search            {time.time()-t0:5.2f}s ->",
          json.dumps(info, indent=1)[:1800] if info else "NOT FOUND")
    if not info:
        print("\nwhy:", json.dumps(LAST, indent=1))
        if not LAST.get("problem"):
            print("(request succeeded but the API returned no players for that "
                  "nickname - check spelling/case, or try the numeric UID)")
        return 1
    pid = _i(info["player_id"])
    # Show every attempt (endpoint x mode) with what the API actually said, so
    # an "empty" result can be told apart: error code, private profile, or
    # genuinely no data.
    tries = [
        ("season (career)", "/player/get/season",
         lambda m: {"player_id": pid, "is_career": True, "mode": m}),
        ("stats (season)", "/player/get/stats",
         lambda m: {"player_id": pid, "season_no": 0, "is_rank": False, "mode": m}),
    ]
    for label, path, mk in tries:
        for mode in _SOL_MODES:
            LAST.clear()
            t = time.time()
            d = _call(path, mk(mode))
            tag = f"{label} mode={json.dumps(mode)}"
            if d:
                has = bool((d.get("sol_data") or {}).get("total_fight"))
                print(f"\n{tag:46} {time.time()-t:5.2f}s -> OK, sol_data has "
                      f"{'DATA' if has else 'NO total_fight (empty)'}")
                if has or d.get("sol_data"):
                    print(json.dumps(d, indent=1)[:900])
            else:
                print(f"\n{tag:46} {time.time()-t:5.2f}s -> FAILED: "
                      f"{LAST.get('problem')} | http={LAST.get('http')} "
                      f"body={LAST.get('body')}")
    t = time.time()
    r = search_player(name, use_cache=False)
    print(f"\nfull search_player {time.time()-t:5.2f}s ->")
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--probe":
        sys.exit(_probe(" ".join(sys.argv[2:])))
    print(__doc__)
