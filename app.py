"""
Delta Force Tracker — community leaderboard backend.

A small FastAPI service you host yourself. It does two things:
  1. Registers a player (one direct API call, no browser/OAuth involved -
     see module note below on why) and hands back a bearer api_key.
  2. Accepts opt-in stat uploads and serves a leaderboard of everyone
     who's opted in.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env
    uvicorn app:app --reload

See README.md for deploying it somewhere real.

Why there's no Discord/OAuth here:
    The desktop app already requires a real, verified login to the game's
    own DfTools backend before it can do anything (that's the browser
    login flow in delta_force_login.py) - so by the time a player opts
    into this leaderboard, the desktop app already holds their `openid`,
    captured from that authenticated session, not typed in by hand. That
    openid is what identifies a player here, instead of a separate
    Discord account. See delta_force_community.py's module docstring for
    the full reasoning and the one rule that keeps this trustworthy (the
    desktop app only ever sends the openid IT captured, never a
    user-editable field).

Security notes (read this before deploying):
  - openid is hashed (SHA-256) before it's ever stored or looked up here.
    This service never sees or stores the raw value, only a one-way
    digest of it - even a full database leak wouldn't reveal it.
  - api_key is a per-player bearer credential (like a password): treat it
    as one. It's generated with secrets.token_urlsafe - a capability
    token for THIS leaderboard only.
  - Stats stored here are exactly the aggregate numbers the desktop app
    already shows locally (net income, win/loss, best map/operator) -
    never raw match history, never DfTools/game credentials.
"""

import hashlib
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

# Where the SQLite file lives, in order of precedence:
#   1. DATABASE_PATH, if set explicitly.
#   2. A Railway volume, if one is attached. Railway sets
#      RAILWAY_VOLUME_MOUNT_PATH by itself for a service that has a volume,
#      so attaching a volume is all it takes for data to survive deploys.
#   3. ./community.db - local development. On most hosts that is erased
#      whenever the service redeploys or restarts.
_VOLUME_PATH = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "")
DATABASE_PATH = (
    os.environ.get("DATABASE_PATH")
    or (os.path.join(_VOLUME_PATH, "community.db") if _VOLUME_PATH else "./community.db")
)
IS_ON_VOLUME = bool(_VOLUME_PATH) and os.path.abspath(DATABASE_PATH).startswith(
    os.path.abspath(_VOLUME_PATH) + os.sep)
_ON_RAILWAY = any(k.startswith("RAILWAY_") for k in os.environ)

os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)
if _ON_RAILWAY and not IS_ON_VOLUME:
    # Loud on purpose: this is the one misconfiguration that looks
    # completely fine until the first redeploy silently empties the leaderboard.
    print("[community] WARNING: running on Railway but the database is NOT on a "
          f"volume ({DATABASE_PATH}). It will be ERASED on every deploy. "
          "Attach a volume to this service - see README.", flush=True)
else:
    print(f"[community] database: {DATABASE_PATH} (on a volume: {IS_ON_VOLUME})", flush=True)

app = FastAPI(title="Delta Force Tracker Community API")


# ---------------------------------------------------------------------
# Rate limiting
#
# In-memory, per-process sliding window - deliberately not backed by
# Redis or anything external, matching this backend's "small enough to
# actually understand" philosophy. That means limits reset on restart
# and don't share state across multiple instances, which is fine for a
# single small deployment and not fine if this ever needs to scale
# horizontally - cross that bridge if it actually comes up.
#
# Keyed by client IP for the unauthenticated endpoint (/register) and by
# the caller's api_key for authenticated ones - api_key is more precise
# (multiple real players can share an IP behind NAT/a router; they can't
# share a key) and also means a flood of garbage/invalid keys gets
# throttled by the same mechanism as a flood of valid ones.
#
# request.client.host is the direct TCP peer, not an X-Forwarded-For
# header - if this ever sits behind a reverse proxy that matters, that's
# a deliberate simplification for a hobby-scale deployment, not an
# oversight.
# ---------------------------------------------------------------------
_RATE_LIMITS = {  # bucket -> (max requests, window seconds)
    "register": (5, 60),
    "sync": (20, 60),
    "toggle": (20, 60),
    "read": (120, 60),
}
_request_log: dict = defaultdict(deque)


def _enforce_rate_limit(bucket: str, identifier: str):
    limit, window = _RATE_LIMITS[bucket]
    now = time.time()
    key = f"{bucket}:{identifier}"
    dq = _request_log[key]
    while dq and dq[0] <= now - window:
        dq.popleft()
    if len(dq) >= limit:
        raise HTTPException(429, "Too many requests — slow down and try again shortly.")
    dq.append(now)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_today() -> str:
    """The daily leaderboard's day: a UTC calendar date, the same moment of
    reset for every player wherever they are."""
    return _utc_now().strftime("%Y-%m-%d")


def _next_reset_iso() -> str:
    nxt = (_utc_now() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return nxt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_DAILY_COLUMNS = [
    ("daily_date", "TEXT"),                       # UTC date these daily numbers are for
    ("daily_net_income", "INTEGER NOT NULL DEFAULT 0"),
    ("daily_matches", "INTEGER NOT NULL DEFAULT 0"),
    ("daily_wins", "INTEGER NOT NULL DEFAULT 0"),
    ("daily_losses", "INTEGER NOT NULL DEFAULT 0"),
    ("daily_best_map", "TEXT"),
    ("daily_best_operator", "TEXT"),
]


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_key TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                api_key TEXT UNIQUE NOT NULL,
                opted_in INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                net_income_all_time INTEGER NOT NULL DEFAULT 0,
                matches INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                win_rate REAL NOT NULL DEFAULT 0,
                best_map TEXT,
                best_operator TEXT,
                rank_label TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        # Daily-leaderboard columns, added to existing databases in place
        # (the Railway volume means there IS an existing database to keep).
        # Checked column by column rather than "if the table is old", so it
        # is safe to run on every start and to re-run after a partial one.
        have = {r[1] for r in conn.execute("PRAGMA table_info(stats)")}
        for name, decl in _DAILY_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE stats ADD COLUMN {name} {decl}")


init_db()


def _hash_openid(openid: str) -> str:
    return hashlib.sha256(openid.strip().encode("utf-8")).hexdigest()


def upsert_player(openid: str, display_name: str, opt_in: bool) -> dict:
    """Create the player if new, or refresh their display name (and,
    only on their very first registration, their opt-in choice) if they
    already registered before. Re-registering always returns the SAME
    api_key, so a reinstall or a second opt-in click never orphans an
    existing leaderboard entry."""
    player_key = _hash_openid(openid)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db() as conn:
        row = conn.execute(
            "SELECT api_key, opted_in FROM users WHERE player_key = ?",
            (player_key,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET display_name=?, updated_at=? WHERE player_key=?",
                (display_name, now, player_key),
            )
            return {"api_key": row["api_key"], "opted_in": bool(row["opted_in"])}

        api_key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO users (player_key, display_name, api_key, opted_in, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (player_key, display_name, api_key, int(opt_in), now, now),
        )
        return {"api_key": api_key, "opted_in": opt_in}


def get_user_by_api_key(api_key: str):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE api_key = ?", (api_key,)
        ).fetchone()


def require_user(authorization: str = Header(None), bucket: str = "read"):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header")
    api_key = authorization.split(" ", 1)[1].strip()
    # Rate-limited on the raw key, before we even look it up - a flood of
    # garbage/invalid keys gets throttled the same way a flood of valid
    # ones would, rather than sailing through because it never resolves
    # to a real user.
    _enforce_rate_limit(bucket, api_key)
    user = get_user_by_api_key(api_key)
    if not user:
        raise HTTPException(401, "Invalid API key")
    return user


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------
# Field length limits below aren't arbitrary caps for their own sake -
# they exist because this data renders directly into the leaderboard
# table with no other sanitization layer between "what the client sent"
# and "what every viewer sees". Nothing here needs to be generous: real
# in-game nicknames, map names, and operator names are all short.
class RegisterPayload(BaseModel):
    openid: str = Field(..., min_length=1, max_length=256)
    nickname: str = Field("Player", max_length=64)
    opt_in: bool = True

    @field_validator("nickname", mode="before")
    @classmethod
    def _clean_nickname(cls, v):
        v = "".join(ch for ch in (v or "").strip() if ch.isprintable())
        return v[:64] or "Player"


@app.post("/register")
def register(payload: RegisterPayload, request: Request):
    _enforce_rate_limit("register", _client_ip(request))
    if not payload.openid.strip():
        raise HTTPException(400, "openid is required")
    result = upsert_player(payload.openid, payload.nickname, payload.opt_in)
    return result


# ---------------------------------------------------------------------
# Stats + leaderboard
# ---------------------------------------------------------------------
class StatsPayload(BaseModel):
    # Bounds generous enough for a dedicated player's real all-time
    # total, tight enough that a garbage/malicious value can't sail
    # through unchecked. Real observed weekly net_income values are in
    # the tens of millions; all-time in the hundreds of millions to low
    # billions is plausible for a long-time player, so the cap sits
    # comfortably above that rather than right against it.
    net_income_all_time: int = Field(0, ge=-10_000_000_000, le=10_000_000_000)
    matches: int = Field(0, ge=0, le=1_000_000)
    wins: int = Field(0, ge=0, le=1_000_000)
    losses: int = Field(0, ge=0, le=1_000_000)
    win_rate: float = Field(0.0, ge=0.0, le=100.0)
    best_map: str = Field("", max_length=64)
    best_operator: str = Field("", max_length=64)
    rank_label: str = Field("", max_length=64)

    # Today's totals, for the daily leaderboard. Optional: older desktop
    # builds don't send them and their sync must keep working untouched.
    # daily_date is the UTC date the client computed them for; the server
    # only accepts them if that is still its own "today" (see sync_stats).
    # The daily net cap is far below the all-time one - a real day's net
    # is a small fraction of it, so this only keeps absurd values off the board.
    daily_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    daily_net_income: int = Field(0, ge=-2_000_000_000, le=2_000_000_000)
    daily_matches: int = Field(0, ge=0, le=5_000)
    daily_wins: int = Field(0, ge=0, le=5_000)
    daily_losses: int = Field(0, ge=0, le=5_000)
    daily_best_map: str = Field("", max_length=64)
    daily_best_operator: str = Field("", max_length=64)

    @field_validator("best_map", "best_operator", "rank_label",
                     "daily_best_map", "daily_best_operator", mode="before")
    @classmethod
    def _clean_text(cls, v):
        return "".join(ch for ch in (v or "").strip() if ch.isprintable())[:64]


@app.get("/me")
def me(request: Request, authorization: str = Header(None)):
    u = require_user(authorization, bucket="read")
    with db() as conn:
        stats = conn.execute(
            "SELECT * FROM stats WHERE user_id = ?", (u["id"],)
        ).fetchone()
    return {
        "display_name": u["display_name"],
        "opted_in": bool(u["opted_in"]),
        "stats": dict(stats) if stats else None,
    }


@app.post("/stats/sync")
def sync_stats(payload: StatsPayload, request: Request,
               authorization: str = Header(None)):
    u = require_user(authorization, bucket="sync")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db() as conn:
        conn.execute(
            """
            INSERT INTO stats (user_id, net_income_all_time, matches, wins,
                                losses, win_rate, best_map, best_operator,
                                rank_label, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                net_income_all_time=excluded.net_income_all_time,
                matches=excluded.matches,
                wins=excluded.wins,
                losses=excluded.losses,
                win_rate=excluded.win_rate,
                best_map=excluded.best_map,
                best_operator=excluded.best_operator,
                rank_label=excluded.rank_label,
                updated_at=excluded.updated_at
            """,
            (u["id"], payload.net_income_all_time, payload.matches,
             payload.wins, payload.losses, payload.win_rate,
             payload.best_map, payload.best_operator, payload.rank_label, now),
        )
        # A stale or wrong-clock date is ignored rather than stored: it
        # would either never show (yesterday) or sit on tomorrow's board.
        daily_accepted = (payload.daily_date is not None
                          and payload.daily_date == _utc_today())
        if daily_accepted:
            conn.execute(
                """UPDATE stats SET daily_date=?, daily_net_income=?,
                       daily_matches=?, daily_wins=?, daily_losses=?,
                       daily_best_map=?, daily_best_operator=?
                   WHERE user_id=?""",
                (payload.daily_date, payload.daily_net_income,
                 payload.daily_matches, payload.daily_wins,
                 payload.daily_losses, payload.daily_best_map,
                 payload.daily_best_operator, u["id"]))
    return {"ok": True, "daily_accepted": daily_accepted}


@app.post("/opt-in")
def opt_in(request: Request, authorization: str = Header(None)):
    u = require_user(authorization, bucket="toggle")
    with db() as conn:
        conn.execute("UPDATE users SET opted_in=1 WHERE id=?", (u["id"],))
    return {"opted_in": True}


@app.post("/opt-out")
def opt_out(request: Request, authorization: str = Header(None)):
    u = require_user(authorization, bucket="toggle")
    with db() as conn:
        conn.execute("UPDATE users SET opted_in=0 WHERE id=?", (u["id"],))
    return {"opted_in": False}


@app.delete("/me")
def delete_me(request: Request, authorization: str = Header(None)):
    """Right-to-be-forgotten: wipes this player's account and stats
    entirely. The desktop app calls this and then deletes its own local
    api_key file - after this, nothing about the player remains here."""
    u = require_user(authorization, bucket="toggle")
    with db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (u["id"],))
    return {"deleted": True}


_SORT_COLUMNS = {
    "net_income": "s.net_income_all_time",
    "matches": "s.matches",
    "win_rate": "s.win_rate",
}

_DAILY_WIN_RATE = ("(CASE WHEN s.daily_matches > 0 "
                   "THEN 100.0 * s.daily_wins / s.daily_matches ELSE 0 END)")
_DAILY_SORT_COLUMNS = {
    "net_income": "s.daily_net_income",
    "matches": "s.daily_matches",
    "win_rate": _DAILY_WIN_RATE,
}


@app.get("/leaderboard")
def leaderboard(request: Request, sort: str = "net_income", limit: int = 50,
                period: str = "all"):
    """period=all (default, exactly what older desktop builds expect) or
    period=daily: only players who opted in AND have played today (UTC),
    ranked on today's numbers alone. Daily rows use the field names
    net_income/matches/wins/losses/win_rate/best_map/best_operator - the
    values are today's, not all-time."""
    _enforce_rate_limit("read", _client_ip(request))
    limit = max(1, min(limit, 200))

    if period == "daily":
        sort_col = _DAILY_SORT_COLUMNS.get(sort, _DAILY_SORT_COLUMNS["net_income"])
        today = _utc_today()
        with db() as conn:
            rows = conn.execute(
                f"""
                SELECT u.display_name, s.daily_net_income AS net_income,
                       s.daily_matches AS matches, s.daily_wins AS wins,
                       s.daily_losses AS losses,
                       ROUND({_DAILY_WIN_RATE}, 1) AS win_rate,
                       s.daily_best_map AS best_map,
                       s.daily_best_operator AS best_operator,
                       s.rank_label, s.updated_at
                FROM users u
                JOIN stats s ON s.user_id = u.id
                WHERE u.opted_in = 1 AND s.daily_date = ? AND s.daily_matches > 0
                ORDER BY {sort_col} DESC
                LIMIT ?
                """,
                (today, limit),
            ).fetchall()
        return {"period": "daily", "day": today, "resets_at": _next_reset_iso(),
                "sort": sort, "players": [dict(r) for r in rows]}

    sort_col = _SORT_COLUMNS.get(sort, _SORT_COLUMNS["net_income"])
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT u.display_name, s.net_income_all_time,
                   s.matches, s.wins, s.losses, s.win_rate, s.best_map,
                   s.best_operator, s.rank_label, s.updated_at
            FROM users u
            JOIN stats s ON s.user_id = u.id
            WHERE u.opted_in = 1
            ORDER BY {sort_col} DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"period": "all", "sort": sort, "players": [dict(r) for r in rows]}


@app.get("/")
def health():
    # database_on_volume lets you confirm from a browser that a Railway
    # volume is really attached, without digging through logs.
    return {"ok": True, "service": "delta-force-tracker-community",
            "database_on_volume": IS_ON_VOLUME}


# Optional: the desktop app can read its latest-version info from here
# (set LATEST_VERSION and DOWNLOAD_URL as environment variables in your
# host's dashboard). If you serve a static version.json from GitHub instead
# (UPDATE_INFO_URL in delta_force_community.py), this endpoint goes unused
# and is harmless. Changing an environment variable restarts the service.
CURRENT_VERSION = os.environ.get("LATEST_VERSION", "1.2.0")
DOWNLOAD_URL = os.environ.get("DOWNLOAD_URL", "")  # e.g. https://github.com/you/repo/releases/latest


@app.get("/version")
def version(request: Request):
    _enforce_rate_limit("read", _client_ip(request))
    return {"latest_version": CURRENT_VERSION, "download_url": DOWNLOAD_URL}

