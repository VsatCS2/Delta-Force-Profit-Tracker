"""
Delta Force Tracker — deltaforceapi.com client.

A different kind of data source from everything else in this app.
delta_force_core.py's fetch_* functions all talk to
sg-act.playerinfinite.com - the same backend the official
playdeltaforce.com HQ page itself uses, reached with a login session
the person creates themselves, for their own account only.

This module talks to deltaforceapi.com instead - an independent,
third-party service, not run by the game's publisher. Confirmed earlier
that its GetPlayer endpoint takes just a name or ID with no proof of
ownership, which is what makes the feature this powers possible at all
(looking up teammates and opponents, not just yourself) but also means
its data is sourced by aggregating player records at a scale an
individual login session never could. Keep that distinction in mind
anywhere this module's data reaches the UI: it's a different kind of
trust than the rest of this app, and should always be presented as
such, not blended into the official-API-sourced cards as if it were the
same kind of thing.

Needs a gateway key (DELTAFORCEAPI_KEY in delta_force_config.py) to do
anything - every function here fails soft to None/a reason string
without one, the same convention delta_force_community.py uses for an
unconfigured SERVER_URL.
"""

import time

import requests

try:
    from delta_force_config import DELTAFORCEAPI_KEY as _CFG_KEY
except ImportError:
    _CFG_KEY = ""

API_BASE = "https://api.deltaforceapi.com/deltaforceapi.gateway.ApiService"
_REQUEST_TIMEOUT_SECONDS = 10

# Looking a player up mid-match means this can get called several times
# in a short window (checking each squad-mate, then an opponent) - this
# cache just avoids re-fetching the exact same name within a few
# minutes, both out of courtesy to a third-party service with no
# relationship to this project and because match context doesn't change
# that fast. Not persisted - process memory only, cleared on restart.
_CACHE_TTL_SECONDS = 180
_cache = {}  # name.lower() -> (fetched_at, result_dict_or_None)


def is_configured() -> bool:
    return bool(_CFG_KEY)


def _headers() -> dict:
    return {
        "Connect-Protocol-Version": "1",
        "deltaforceapi-gateway-key": _CFG_KEY,
        "Content-Type": "application/json",
    }


def _post(endpoint: str, body: dict):
    """POSTs to one ApiService endpoint. Returns the parsed JSON dict on
    a real 200, or None on anything else (not configured, not found,
    network error, bad response) - every caller in this module already
    expects "no result" to mean exactly that, not an exception to
    catch."""
    if not _CFG_KEY:
        return None
    try:
        resp = requests.post(f"{API_BASE}/{endpoint}", json=body,
                             headers=_headers(),
                             timeout=_REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return None


def _as_int(value, default=0) -> int:
    """Several fields in these responses come back as JSON strings for
    large numbers (extractedAssets, the stash fields) rather than
    numbers - presumably to avoid precision loss in languages where a
    bare JSON number can't safely hold a value that large. Normalizes
    either shape to a real int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def search_player(name_or_id: str, use_cache: bool = True):
    """Looks up another player by display name (or their deltaforceapi
    player_id, if you already have one from a previous search) and
    returns their profile, combat stats, and stash value in one dict -
    or None if not configured, not found, or the lookup failed for any
    reason (never raises; this is meant to be safe to call directly
    from UI code without a try/except at the call site).

    Returns:
        {
            "name", "player_id", "delta_force_id",
            "level_operations", "level_warfare", "registered_at",
            "stats": {
                # Matches
                "matches_played", "matches_extracted", "matches_lost",
                "matches_quit", "extraction_rate",      # rate is 0.0-1.0
                # Combat, overall
                "kd_ratio", "total_kills", "total_deaths",
                # Combat, by enemy difficulty tier
                "kd_ratio_easy", "kd_ratio_medium", "kd_ratio_hard",
                "kills_easy", "kills_medium", "kills_hard",
                "deaths_easy", "deaths_medium", "deaths_hard",
                # Accuracy
                "bullets_discharged", "bullets_hit", "bullets_missed",
                "bullet_hit_rate",                      # 0.0-1.0
                "bullets_per_knock", "bullets_hit_per_knock",
                # Knocks
                "knocked_count", "knocked_headshots",
                "headshot_rate",                        # 0.0-1.0, of knocks
                # Team play
                "revives", "pickups",
                # Scores (this API's own five category scores)
                "score_combat", "score_survival", "score_coop",
                "score_search", "score_wealth",
                # Extraction value
                "extracted_value", "extracted_teammate_value",
                "mandelbricks_extracted",
                # Ranked / misc
                "ranked_points", "play_time_seconds",
            },
            "stash": {
                "liquid", "fixed", "collection", "net",  # all ints
            },
        }
    stats/stash are each {} (not missing) if that particular lookup
    failed but the player itself was found - the player's existence and
    their stats/stash are three separate calls, and a transient failure
    on one shouldn't hide a successful result from the other two.
    """
    if not name_or_id or not name_or_id.strip():
        return None

    key = name_or_id.strip().lower()
    if use_cache and key in _cache:
        fetched_at, cached = _cache[key]
        if time.time() - fetched_at < _CACHE_TTL_SECONDS:
            return cached

    # name_or_id is a UUID-shaped deltaforceapi player_id iff this is a
    # re-lookup from a result we already returned (see player_id above);
    # a plain player-chosen display name never collides with that shape.
    is_id = len(key) == 36 and key.count("-") == 4
    player_payload = _post("GetPlayer", {"id": name_or_id} if is_id
                           else {"name": name_or_id})
    player = (player_payload or {}).get("player")
    if not player or not player.get("id"):
        # Deliberately NOT cached. _post returns None for a timeout or a
        # 5xx exactly as it does for "no such player", so this can't tell
        # a typo from a flaky request - and caching it for the whole TTL
        # meant one transient failure was replayed for three minutes,
        # including to anything retrying it. An uncached miss just costs
        # one more request next time.
        return None

    pid = player["id"]
    stats_payload = _post("GetPlayerOperationStats",
                          {"playerId": pid, "ranked": False})
    stash_payload = _post("GetPlayerOperationStashValue", {"playerId": pid})

    s = (stats_payload or {}).get("stats") or {}
    stats = {}
    if s:
        stats = {
            # Matches
            "matches_played": _as_int(s.get("matchesPlayed")),
            "matches_extracted": _as_int(s.get("matchesExtracted")),
            "matches_lost": _as_int(s.get("matchesLost")),
            "matches_quit": _as_int(s.get("matchesQuit")),
            "extraction_rate": float(s.get("extractionRate") or 0.0),
            # Combat, overall
            "kd_ratio": float(s.get("kdRatio") or 0.0),
            "total_kills": _as_int(s.get("totalKills")),
            "total_deaths": _as_int(s.get("totalDeaths")),
            # Combat, by enemy difficulty tier - "easy"/"medium"/"hard"
            # are this API's own names for the three tiers, kept as-is
            # rather than renamed, since that's the vocabulary a player
            # already knows from the game itself.
            "kd_ratio_easy": float(s.get("kdRatioEasy") or 0.0),
            "kd_ratio_medium": float(s.get("kdRatioMedium") or 0.0),
            "kd_ratio_hard": float(s.get("kdRatioHard") or 0.0),
            "kills_easy": _as_int(s.get("totalKillsEasy")),
            "kills_medium": _as_int(s.get("totalKillsMedium")),
            "kills_hard": _as_int(s.get("totalKillsHard")),
            "deaths_easy": _as_int(s.get("totalDeathsEasy")),
            "deaths_medium": _as_int(s.get("totalDeathsMedium")),
            "deaths_hard": _as_int(s.get("totalDeathsHard")),
            # Accuracy
            "bullets_discharged": _as_int(s.get("bulletsDischarged")),
            "bullets_hit": _as_int(s.get("bulletsDischargedHit")),
            "bullets_missed": _as_int(s.get("bulletsDischargedMissed")),
            # bulletDischargedHitRatio - no "s" after "bullet" - is the
            # API's own field name, not a typo introduced here.
            "bullet_hit_rate": float(s.get("bulletDischargedHitRatio") or 0.0),
            "bullets_per_knock": float(s.get("bulletsDischargedPerKnock") or 0.0),
            "bullets_hit_per_knock": float(s.get("bulletsDischargedHitPerKnock") or 0.0),
            # Knocks
            "knocked_count": _as_int(s.get("knockedCount")),
            "knocked_headshots": _as_int(s.get("knockedHeadshotCount")),
            "headshot_rate": float(s.get("knockedHeadshotRatio") or 0.0),
            # Team play
            "revives": _as_int(s.get("revives")),
            "pickups": _as_int(s.get("pickups")),
            # Scores - this API's own five category scores, not something
            # computed here; kept under the names it already uses them by.
            "score_combat": _as_int(s.get("scoreCombat")),
            "score_survival": _as_int(s.get("scoreSurvival")),
            "score_coop": _as_int(s.get("scoreCoop")),
            "score_search": _as_int(s.get("scoreSearch")),
            "score_wealth": _as_int(s.get("scoreWealth")),
            # Extraction value - extractedAssets/extractedTeammateAssets
            # come back as strings for the same large-number reason the
            # stash fields do; extractedMandlebricks is already a plain
            # int in the raw response, unlike those two.
            "extracted_value": _as_int(s.get("extractedAssets")),
            "extracted_teammate_value": _as_int(s.get("extractedTeammateAssets")),
            "mandelbricks_extracted": _as_int(s.get("extractedMandlebricks")),
            # Ranked / misc
            "ranked_points": _as_int(s.get("rankedPoints")),
            "play_time_seconds": _as_int(s.get("playTime")),
        }

    h = (stash_payload or {}).get("stash") or {}
    stash = {}
    if h:
        stash = {
            "liquid": _as_int(h.get("assetsLiquid")),
            "fixed": _as_int(h.get("assetsFixed")),
            "collection": _as_int(h.get("assetsCollection")),
            "net": _as_int(h.get("assetsNet")),
        }

    result = {
        "name": player.get("name", name_or_id),
        "player_id": pid,
        "delta_force_id": player.get("deltaForceId", ""),
        "level_operations": player.get("levelOperations", 0),
        "level_warfare": player.get("levelWarfare", 0),
        "registered_at": player.get("registeredAt", ""),
        "stats": stats,
        "stash": stash,
    }
    # Only cache a result that actually has something in it. A player
    # who was found but whose stats/stash came back empty is the
    # service's queue still working (or a transient failure on just
    # those two calls) - caching that froze "no stats" for the whole
    # TTL, so a retry a minute later got the same empty answer back
    # without ever asking the service again.
    if stats or stash:
        _cache[key] = (time.time(), result)
        # The lookup almost certainly came in by display name; cache the
        # resolved player_id too, so a second search field (or the
        # overlay re-checking the same person) that already has the id
        # from a prior result can hit the cache without needing the name.
        _cache[pid.lower()] = (time.time(), result)
    return result


# ---------------------------------------------------------------------
# Item search + auction prices (for Price Alerts)
# ---------------------------------------------------------------------

# The one page size actually seen working in a real captured request -
# deliberately not raised on a guess. Some matches are non-auctionable
# duplicates (a "(Copy) ..." keycard sits right next to the real one in
# the captured response) that get filtered out below, so a very broad
# search can show fewer than this many usable results; typing a more
# specific name is the fix. If a larger page is confirmed to work, this
# is the only line that needs to change.
SEARCH_PAGE_SIZE = 10
_MAX_QUERY_LEN = 64


def _escape_filter_text(text: str) -> str:
    """The ListItems filter is a quoted-string expression
    (name:"Reactor Master"), so a quote or backslash in what someone
    types would otherwise end the string early and change the meaning of
    the filter itself. Escape both, and drop non-printable characters."""
    cleaned = "".join(ch for ch in text if ch.isprintable())[:_MAX_QUERY_LEN]
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def search_items(query: str):
    """Finds tradeable items by (partial) name.

    Returns:
        None   - not configured, or the request failed (callers should
                 say "couldn't search", not "no results")
        []     - the search worked and nothing tradeable matched
        [ {id, name, description, type, type_sub, quality, icon_url}, ...]

    Only auctionable items are returned: a price alert on something that
    can't be listed on the auction house could never trigger, and the
    real response includes exactly that kind of entry (a "(Copy)"
    keycard with auctionable=false next to the real one).
    """
    query = (query or "").strip()
    if not query:
        return []
    if not _CFG_KEY:
        return None
    escaped = _escape_filter_text(query)
    if not escaped.strip():
        return []
    payload = _post("ListItems", {
        "language": "LANGUAGE_EN",
        "filter": f'name:"{escaped}"',
        "pageSize": SEARCH_PAGE_SIZE,
    })
    if payload is None:
        return None
    # `payload is None` above, not `not payload`: a search with zero
    # matches comes back as an empty object (protobuf-JSON omits empty
    # lists), which is falsy but is a perfectly successful "no results".
    results = []
    for item in payload.get("items") or []:
        if not item.get("auctionable") or not item.get("id"):
            continue
        results.append({
            "id": item["id"],
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "type": item.get("type", ""),
            "type_sub": item.get("typeSub", ""),
            "quality": item.get("quality", 0),
            "icon_url": item.get("iconUrl", ""),
        })
    return results


def get_item_price(item_id: str):
    """Current auction price for one item, or None if there isn't a
    usable one (not configured, request failed, no listing).

    Returns {"price": int, "reference_price": int, "as_of": iso-string}.

    A missing or zero price is None, never 0: protobuf-JSON omits zero
    values entirely, so "no price" and "0" look the same on the wire, and
    treating that as a real price of 0 would make every price alert look
    like it had just dropped to its target.

    reference_price is passed through as the API reports it, but nothing
    here claims to know exactly what it measures - it's not shown in the
    UI for that reason.
    """
    if not item_id or not _CFG_KEY:
        return None
    payload = _post("GetItemAuctionPrice", {"itemId": item_id})
    p = (payload or {}).get("price")
    if not p:
        return None
    price = _as_int(p.get("price"), default=0)
    if price <= 0:
        return None
    return {
        "price": price,
        "reference_price": _as_int(p.get("referencePrice"), default=0),
        "as_of": p.get("createdAt", ""),
    }
