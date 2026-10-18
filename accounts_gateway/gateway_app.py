"""DF Tracker accounts gateway (FastAPI).

  GET  /v1/me          who am I, what tier, which features, how much used today
  POST /v1/call        player-data proxy: requires sign-in + the player_lookup
                       feature, enforces the tier's daily budget
  GET  /admin          admin page (data comes from /admin/api/*, token-protected)
  GET  /health

Authentication: Supabase access tokens (verified locally, see auth.py).
Authorisation: our own database (tier, role, banned) + the `features` table.
"""
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from auth import AuthError, Verifier
from entitlements import DEFAULT_FEATURES, TIERS, parse_ts, resolve
from store import MemoryStore, SupabaseStore
from usage import UsageTracker, today

ALLOWED_PATHS = {"/player/search", "/player/get/season", "/player/get/stats",
                 "/player/get/status", "/player/get/info"}
ROLES = ("user", "admin")
HERE = Path(__file__).parent


# ---------------------------------------------------------------- settings
@dataclass
class Settings:
    supabase_url: str = ""
    supabase_secret_key: str = ""
    jwt_secret: str = ""
    admin_token: str = ""
    playerapi_url: str = ""
    playerapi_secret: str = ""
    store: str = "supabase"          # "memory" for local development only
    cache_ttl: int = 1800
    rate_per_min: int = 30
    global_daily_cap: int = 0        # 0 = unlimited (RapidAPI's own limit still applies)

    @classmethod
    def from_env(cls, env=None):
        e = env if env is not None else os.environ
        g = lambda *names, default="": next((e[n] for n in names if e.get(n)), default)
        return cls(
            supabase_url=g("SUPABASE_URL"),
            supabase_secret_key=g("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_KEY"),
            jwt_secret=g("SUPABASE_JWT_SECRET"),
            admin_token=g("ADMIN_TOKEN"),
            playerapi_url=g("PLAYERAPI_URL").rstrip("/"),
            playerapi_secret=g("PLAYERAPI_SECRET"),
            store=g("STORE", default="supabase"),
            cache_ttl=int(g("CACHE_TTL", default="1800")),
            rate_per_min=int(g("RATE_PER_MIN", default="30")),
            global_daily_cap=int(g("GLOBAL_DAILY_CAP", "DAILY_CAP", default="0")))


def auth_headers(base_url, secret):
    """RapidAPI wants x-rapidapi-key/host; a self-hosted API uses x-secret."""
    host = urlparse(base_url).netloc
    if host.endswith("rapidapi.com"):
        return {"x-rapidapi-key": secret, "x-rapidapi-host": host}
    return {"x-secret": secret}


class HttpUpstream:
    def __init__(self, base, secret, client=None):
        self.base, self.secret = base, secret
        self.http = client or httpx.Client(timeout=10)

    def post(self, path, body):
        r = self.http.post(self.base + path, json=body,
                           headers=auth_headers(self.base, self.secret))
        try:
            data = r.json()
        except ValueError:
            data = {"_raw": r.text[:200]}
        return r.status_code, data


MIN_ADMIN_TOKEN = 20
MAX_BODY = 16 * 1024


def client_ip(request):
    """The caller's address. Behind Railway's proxy, request.client is the
    PROXY, so use X-Forwarded-For - but only its LAST entry, which the trusted
    proxy appended. Earlier entries are whatever the caller chose to send."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip() or "?"
    return request.client.host if request.client else "?"


class ApiError(Exception):
    def __init__(self, http_status, code, **extra):
        self.http_status, self.code, self.extra = http_status, code, extra


# ================================================================== factory
def create_app(settings, store=None, verifier=None, upstream=None, clock=time.time):
    if store is None:
        if settings.store == "memory":
            store = MemoryStore()
        elif settings.supabase_url and settings.supabase_secret_key:
            store = SupabaseStore(settings.supabase_url, settings.supabase_secret_key)
        else:
            raise RuntimeError("Set SUPABASE_URL and SUPABASE_SECRET_KEY "
                               "(or STORE=memory for local development).")
    if verifier is None:
        verifier = Verifier(settings.supabase_url, settings.jwt_secret)
    if upstream is None and settings.playerapi_url and settings.playerapi_secret:
        upstream = HttpUpstream(settings.playerapi_url, settings.playerapi_secret)

    tracker = UsageTracker(store, clock)
    lock = threading.RLock()
    cache, rate_windows, admin_fails = {}, defaultdict(deque), defaultdict(deque)
    profile_cache, features_cache = {}, {"at": 0, "rows": None}
    global_used = {"day": today(clock), "n": 0}
    last_touch = {}

    try:
        if not store.list_features():
            for f in DEFAULT_FEATURES:
                store.upsert_feature(f)
    except Exception:
        pass                                    # DB not ready yet: the app can still boot

    @asynccontextmanager
    async def lifespan(_app):
        tracker.start()
        yield
        tracker.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.tracker = store, tracker
    app.state.invalidate = lambda uid=None: (profile_cache.pop(uid, None) if uid
                                             else profile_cache.clear())

    # ----------------------------------------------------------- errors
    @app.exception_handler(AuthError)
    async def _auth_err(_r, e: AuthError):
        return JSONResponse({"error": e.code, "message": e.message}, e.status,
                            headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def _limit_body(request: Request, call_next):
        try:
            too_big = int(request.headers.get("content-length") or 0) > MAX_BODY
        except ValueError:
            too_big = True
        if too_big:
            return JSONResponse({"error": "payload_too_large"}, 413)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def _bad_body(_r, _e):
        return JSONResponse({"error": "bad_request"}, 400)

    @app.exception_handler(ApiError)
    async def _api_err(_r, e: ApiError):
        return JSONResponse({"error": e.code, **e.extra}, e.http_status,
                            headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------- helpers
    def features():
        now = clock()
        if features_cache["rows"] is None or now - features_cache["at"] > 30:
            try:
                features_cache["rows"] = store.list_features() or DEFAULT_FEATURES
                features_cache["at"] = now
            except Exception:
                features_cache["rows"] = features_cache["rows"] or DEFAULT_FEATURES
        return features_cache["rows"]

    def authenticate(request: Request):
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h.lower().startswith("bearer ") else ""
        claims = verifier.verify(token)
        now = clock()
        with lock:
            hit = profile_cache.get(claims.sub)
        if hit and now - hit[0] < 30:
            profile = hit[1]
        else:
            profile = store.ensure_profile(claims.sub, claims.email, claims.name,
                                           claims.avatar, claims.provider)
            with lock:
                profile_cache[claims.sub] = (now, profile)
        if profile.get("banned"):
            raise AuthError("banned", 403, profile.get("ban_reason") or "")
        if now - last_touch.get(claims.sub, 0) > 600:
            last_touch[claims.sub] = now
            try:
                store.touch(claims.sub)
            except Exception:
                pass
        return claims, profile

    def public_me(claims, profile):
        ent = resolve(profile, features(), datetime.fromtimestamp(clock(), timezone.utc))
        used = tracker.snapshot(claims.sub)
        for key, f in ent["features"].items():
            f["used"] = used.get(key, 0)
        return {"user": {"id": claims.sub, "email": profile.get("email") or claims.email,
                         "name": profile.get("display_name") or claims.name,
                         "avatar": profile.get("avatar_url") or claims.avatar},
                "tier": ent["tier"], "role": profile.get("role", "user"),
                "tier_expires_at": profile.get("tier_expires_at"),
                "features": ent["features"],
                "server_time": datetime.fromtimestamp(clock(), timezone.utc).isoformat()}

    # ------------------------------------------------------------ public
    @app.get("/health")
    def health():
        return {"ok": True, "store": store.name, "auth": verifier.mode,
                "upstream": bool(upstream),
                "admin": len(settings.admin_token or "") >= MIN_ADMIN_TOKEN}

    @app.get("/v1/me")
    def me(request: Request):
        claims, profile = authenticate(request)
        return JSONResponse(public_me(claims, profile), headers={"Cache-Control": "no-store"})

    @app.post("/v1/call")
    def call(request: Request, data: dict = Body(...)):
        # A plain (sync) handler: FastAPI runs it in a worker thread, so the
        # blocking database/upstream calls below never stall other requests.
        claims, profile = authenticate(request)
        path, body = data.get("path"), data.get("body") or {}
        if path not in ALLOWED_PATHS or not isinstance(body, dict):
            raise ApiError(400, "endpoint_not_allowed")

        ent = resolve(profile, features(), datetime.fromtimestamp(clock(), timezone.utc))
        feat = ent["features"].get("player_lookup")
        if not feat or not feat["enabled"]:
            raise ApiError(403, "feature_locked", feature="player_lookup",
                           min_tier=(feat or {}).get("min_tier", "pro"),
                           disabled=bool((feat or {}).get("globally_disabled")))
        limit = feat["limit"]

        key = path + json.dumps(body, sort_keys=True)
        now = clock()
        with lock:
            hit = cache.get(key)
        used = tracker.used(claims.sub, "player_lookup")
        quota_headers = {"X-DF-Quota-Used": str(used),
                         "X-DF-Quota-Limit": "" if limit is None else str(limit)}
        if hit and now - hit[0] < settings.cache_ttl:
            return JSONResponse(hit[1], headers=quota_headers)   # free: no upstream cost

        if not upstream:
            raise ApiError(503, "upstream_not_configured")
        with lock:                                              # per-minute burst limit
            w = rate_windows[claims.sub]
            while w and now - w[0] > 60:
                w.popleft()
            if len(w) >= settings.rate_per_min:
                raise ApiError(429, "rate_limited")
            w.append(now)

        ok, used = tracker.consume(claims.sub, "player_lookup", limit)
        if not ok:
            raise ApiError(429, "quota", feature="player_lookup", limit=limit, used=used)
        with lock:
            day = today(clock)
            if global_used["day"] != day:
                global_used.update(day=day, n=0)
            if settings.global_daily_cap and global_used["n"] >= settings.global_daily_cap:
                tracker.refund(claims.sub, "player_lookup")
                raise ApiError(503, "service_busy")
            global_used["n"] += 1

        try:
            status, payload = upstream.post(path, body)
        except httpx.HTTPError:
            tracker.refund(claims.sub, "player_lookup")
            raise ApiError(502, "upstream_unreachable")
        if status != 200:
            tracker.refund(claims.sub, "player_lookup")        # failed calls are free
            extra = {"detail": json.dumps(payload)[:200]} if status in (401, 403, 429) else {}
            raise ApiError(502, "upstream_error", status=status, **extra)
        with lock:
            cache[key] = (now, payload)
            if len(cache) > 2000:
                for k in sorted(cache, key=lambda k: cache[k][0])[:200]:
                    cache.pop(k, None)
        quota_headers["X-DF-Quota-Used"] = str(used)
        return JSONResponse(payload, headers=quota_headers)

    # ------------------------------------------------------------- admin
    @app.get("/admin", response_class=HTMLResponse)
    def admin_page():
        html = (HERE / "admin_page.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                       "style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; "
                                       "connect-src 'self'; frame-ancestors 'none'",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store"})

    def admin_required(request: Request):
        if len(settings.admin_token or "") < MIN_ADMIN_TOKEN:
            raise ApiError(503, "admin_disabled")        # unset, or too weak to expose
        ip = client_ip(request)
        now = clock()
        with lock:
            fails = admin_fails[ip]
            while fails and now - fails[0] > 300:
                fails.popleft()
            if len(fails) >= 10:
                raise ApiError(429, "too_many_attempts")
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(token.encode(), settings.admin_token.encode()):
            with lock:
                admin_fails[ip].append(now)
            raise ApiError(401, "bad_admin_token")

    def admin_dep(request: Request):
        admin_required(request)

    admin_only = [Depends(admin_dep)]

    def clean_patch(body):
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        out = {}
        for k, v in body.items():
            if k == "tier":
                if v not in TIERS:
                    raise ValueError(f"tier must be one of {TIERS}")
            elif k == "role":
                if v not in ROLES:
                    raise ValueError(f"role must be one of {ROLES}")
            elif k == "banned":
                if not isinstance(v, bool):
                    raise ValueError("banned must be true or false")
            elif k == "tier_expires_at":
                if v in (None, ""):
                    v = None
                else:
                    dt = parse_ts(v)
                    if dt is None:
                        raise ValueError("tier_expires_at must be an ISO date/time")
                    v = dt.isoformat()
            elif k in ("ban_reason", "notes"):
                if v is not None and (not isinstance(v, str) or len(v) > (200 if k == "ban_reason" else 500)):
                    raise ValueError(f"{k} is too long")
                v = v or None
            else:
                raise ValueError(f"unknown field {k}")
            out[k] = v
        return out

    def enrich(rows):
        tracker.flush()
        usage = store.usage_for_users(today(clock), [r["id"] for r in rows])
        now = datetime.fromtimestamp(clock(), timezone.utc)
        for r in rows:
            r["effective_tier"] = resolve(r, features(), now)["tier"]
            r["usage_today"] = usage.get(r["id"], {})
        return rows

    @app.get("/admin/api/users", dependencies=admin_only)
    def a_users(request: Request, q: str = "", tier: str = "", offset: int = 0, limit: int = 50):
        rows, total = store.list_profiles(q, tier if tier in TIERS else "",
                                          max(0, offset), min(max(1, limit), 200))
        return JSONResponse({"users": enrich(rows), "total": total},
                            headers={"Cache-Control": "no-store"})

    @app.patch("/admin/api/users/{uid}", dependencies=admin_only)
    def a_user_patch(uid: str, request: Request, body: dict = Body(...)):
        try:
            patch = clean_patch(body)
        except ValueError as e:
            raise ApiError(400, "invalid", detail=str(e))
        before = store.get_profile(uid)
        if not before:
            raise ApiError(404, "not_found")
        after = store.update_profile(uid, patch)
        if "banned" in patch and patch["banned"] != bool(before.get("banned")):
            try:
                store.set_auth_ban(uid, patch["banned"])
            except Exception:
                pass
        app.state.invalidate(uid)
        store.log_audit("admin", "update_user", uid,
                        {k: {"from": before.get(k), "to": v} for k, v in patch.items()
                         if before.get(k) != v})
        return enrich([after])[0]

    @app.delete("/admin/api/users/{uid}", dependencies=admin_only)
    def a_user_delete(uid: str, request: Request):
        before = store.get_profile(uid)
        if not before:
            raise ApiError(404, "not_found")
        if not store.delete_user(uid):
            raise ApiError(502, "delete_failed")
        app.state.invalidate(uid)
        store.log_audit("admin", "delete_user", uid, {"email": before.get("email")})
        return {"ok": True}

    @app.get("/admin/api/features", dependencies=admin_only)
    def a_features(request: Request):
        return {"features": store.list_features(), "tiers": list(TIERS)}

    @app.put("/admin/api/features/{key}", dependencies=admin_only)
    def a_feature_put(key: str, request: Request, b: dict = Body(...)):
        if not re.fullmatch(r"[a-z0-9_]{2,40}", key):
            raise ApiError(400, "invalid", detail="key must be 2-40 chars: a-z, 0-9, _")
        try:
            if b.get("min_tier") not in TIERS:
                raise ValueError(f"min_tier must be one of {TIERS}")
            limits = {}
            for lk in ("free_limit", "pro_limit"):
                v = b.get(lk)
                limits[lk] = None if v in (None, "") else int(v)
                if limits[lk] is not None and limits[lk] < 0:
                    raise ValueError(f"{lk} can't be negative")
        except (ValueError, TypeError) as e:
            raise ApiError(400, "invalid", detail=str(e))
        row = store.upsert_feature(dict(
            key=key, label=str(b.get("label") or key)[:60],
            description=str(b.get("description") or "")[:300],
            min_tier=b["min_tier"], enabled=bool(b.get("enabled", True)),
            sort=int(b.get("sort") or 0), **limits))
        features_cache["rows"] = None
        store.log_audit("admin", "update_feature", key, {k: row.get(k) for k in row})
        return row

    @app.delete("/admin/api/features/{key}", dependencies=admin_only)
    def a_feature_delete(key: str, request: Request):
        store.delete_feature(key)
        features_cache["rows"] = None
        store.log_audit("admin", "delete_feature", key)
        return {"ok": True}

    @app.get("/admin/api/stats", dependencies=admin_only)
    def a_stats(request: Request):
        tracker.flush()
        return {"counts": store.counts(), "usage_today": store.usage_totals(today(clock)),
                "global_used": global_used["n"], "global_cap": settings.global_daily_cap,
                "store": store.name, "auth": verifier.mode}

    @app.get("/admin/api/audit", dependencies=admin_only)
    def a_audit(request: Request, limit: int = 100):
        return {"audit": store.list_audit(min(max(1, limit), 500))}

    return app
