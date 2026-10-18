"""Data access. MemoryStore for tests and local runs; SupabaseStore for real.

SupabaseStore talks to Supabase's REST interface with the project's SECRET key
(server only - it bypasses row security). Tables are created by
supabase_schema.sql; row security is on with no public policies, so nothing
here is reachable from the app or the internet except through this server.
"""
import re
import threading
from datetime import datetime, timedelta, timezone

import httpx

PROFILE_EDITABLE = ("tier", "tier_expires_at", "role", "banned", "ban_reason", "notes")
FEATURE_FIELDS = ("key", "label", "description", "min_tier", "enabled",
                  "free_limit", "pro_limit", "sort")
_UUID = re.compile(r"^[0-9a-fA-F-]{32,36}$")


def iso_now():
    return datetime.now(timezone.utc).isoformat()


class StoreError(Exception):
    pass


def _clean_feature(f):
    out = {k: f.get(k) for k in FEATURE_FIELDS}
    out["enabled"] = bool(f.get("enabled", True))
    out["sort"] = int(f.get("sort") or 0)
    return out


# ======================================================================
class MemoryStore:
    name = "memory"

    def __init__(self):
        self._lock = threading.RLock()
        self.profiles, self.features, self.usage, self.audit = {}, {}, {}, []

    # ---- profiles
    def get_profile(self, uid):
        with self._lock:
            p = self.profiles.get(uid)
            return dict(p) if p else None

    def ensure_profile(self, uid, email="", name="", avatar="", provider=""):
        with self._lock:
            p = self.profiles.get(uid)
            if p is None:
                p = dict(id=uid, email=email, display_name=name, avatar_url=avatar,
                         provider=provider, tier="free", tier_expires_at=None,
                         role="user", banned=False, ban_reason=None, notes=None,
                         created_at=iso_now(), last_seen_at=None)
                self.profiles[uid] = p
            return dict(p)

    def touch(self, uid):
        with self._lock:
            if uid in self.profiles:
                self.profiles[uid]["last_seen_at"] = iso_now()

    def list_profiles(self, q="", tier="", offset=0, limit=50):
        with self._lock:
            rows = list(self.profiles.values())
        q = (q or "").strip().lower()
        if q:
            rows = [r for r in rows if q in (r.get("email") or "").lower()
                    or q in (r.get("display_name") or "").lower() or q == r["id"].lower()]
        if tier:
            rows = [r for r in rows if r.get("tier") == tier]
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]], len(rows)

    def update_profile(self, uid, fields):
        with self._lock:
            p = self.profiles.get(uid)
            if p is None:
                return None
            for k, v in fields.items():
                if k in PROFILE_EDITABLE:
                    p[k] = v
            return dict(p)

    def delete_user(self, uid):
        with self._lock:
            return self.profiles.pop(uid, None) is not None

    def set_auth_ban(self, uid, banned):
        pass

    def counts(self):
        with self._lock:
            rows = list(self.profiles.values())
        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        return dict(
            users=len(rows),
            pro=sum(1 for r in rows if r.get("tier") == "pro"),
            banned=sum(1 for r in rows if r.get("banned")),
            signups_7d=sum(1 for r in rows if (r.get("created_at") or "") >= week),
            active_24h=sum(1 for r in rows if (r.get("last_seen_at") or "") >= day))

    # ---- features
    def list_features(self):
        with self._lock:
            return sorted((dict(f) for f in self.features.values()),
                          key=lambda f: (f.get("sort") or 0, f["key"]))

    def upsert_feature(self, f):
        with self._lock:
            self.features[f["key"]] = _clean_feature(f)
            return dict(self.features[f["key"]])

    def delete_feature(self, key):
        with self._lock:
            return self.features.pop(key, None) is not None

    # ---- usage
    def usage_for_user(self, day, uid):
        with self._lock:
            return {f: n for (d, u, f), n in self.usage.items() if d == day and u == uid}

    def usage_for_users(self, day, uids):
        out = {u: {} for u in uids}
        with self._lock:
            for (d, u, f), n in self.usage.items():
                if d == day and u in out:
                    out[u][f] = n
        return out

    def usage_totals(self, day):
        out = {}
        with self._lock:
            for (d, u, f), n in self.usage.items():
                if d == day:
                    out[f] = out.get(f, 0) + n
        return out

    def add_usage(self, day, rows):
        with self._lock:
            for uid, feature, n in rows:
                k = (day, uid, feature)
                self.usage[k] = self.usage.get(k, 0) + n

    # ---- audit
    def log_audit(self, actor, action, target="", detail=None):
        with self._lock:
            self.audit.append(dict(at=iso_now(), actor=actor, action=action,
                                   target=target, detail=detail or {}))

    def list_audit(self, limit=100):
        with self._lock:
            return list(reversed(self.audit))[:limit]


# ======================================================================
class SupabaseStore:
    name = "supabase"

    def __init__(self, url, secret_key, client=None, timeout=10):
        self.base = url.rstrip("/")
        self.key = secret_key
        self.http = client or httpx.Client(timeout=timeout)

    def _headers(self, extra=None):
        h = {"apikey": self.key, "Content-Type": "application/json"}
        if self.key.startswith("eyJ"):          # legacy service_role key is a JWT
            h["Authorization"] = f"Bearer {self.key}"
        h.update(extra or {})
        return h

    def _req(self, method, path, params=None, json=None, prefer=None):
        headers = self._headers({"Prefer": prefer} if prefer else None)
        r = self.http.request(method, self.base + path, params=params, json=json,
                              headers=headers)
        if r.status_code >= 300:
            raise StoreError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        return r

    @staticmethod
    def _total(resp):
        cr = resp.headers.get("content-range", "")
        return int(cr.rsplit("/", 1)[1]) if "/" in cr and cr.rsplit("/", 1)[1].isdigit() else 0

    # ---- profiles
    def get_profile(self, uid):
        rows = self._req("GET", "/rest/v1/profiles", {"id": f"eq.{uid}", "select": "*"}).json()
        return rows[0] if rows else None

    def ensure_profile(self, uid, email="", name="", avatar="", provider=""):
        p = self.get_profile(uid)
        if p:
            return p
        self._req("POST", "/rest/v1/profiles", {"on_conflict": "id"},
                  json=dict(id=uid, email=email, display_name=name,
                            avatar_url=avatar, provider=provider),
                  prefer="resolution=ignore-duplicates")
        return self.get_profile(uid) or dict(id=uid, tier="free", role="user", banned=False)

    def touch(self, uid):
        self._req("PATCH", "/rest/v1/profiles", {"id": f"eq.{uid}"},
                  json={"last_seen_at": iso_now()})

    def list_profiles(self, q="", tier="", offset=0, limit=50):
        params = {"select": "*", "order": "created_at.desc",
                  "limit": limit, "offset": offset}
        q = re.sub(r"[,()*%\\\"]", "", (q or "")).strip()
        if q:
            ors = [f"email.ilike.*{q}*", f"display_name.ilike.*{q}*"]
            if _UUID.match(q):
                ors.append(f"id.eq.{q}")
            params["or"] = "(" + ",".join(ors) + ")"
        if tier:
            params["tier"] = f"eq.{tier}"
        r = self._req("GET", "/rest/v1/profiles", params, prefer="count=exact")
        return r.json(), self._total(r)

    def update_profile(self, uid, fields):
        body = {k: v for k, v in fields.items() if k in PROFILE_EDITABLE}
        if not body:
            return self.get_profile(uid)
        rows = self._req("PATCH", "/rest/v1/profiles", {"id": f"eq.{uid}"}, json=body,
                         prefer="return=representation").json()
        return rows[0] if rows else None

    def delete_user(self, uid):
        # Removing the auth user cascades to the profile (see the schema).
        r = self.http.request("DELETE", f"{self.base}/auth/v1/admin/users/{uid}",
                              headers=self._headers())
        return r.status_code < 300

    def set_auth_ban(self, uid, banned):
        """Also stop Supabase issuing new tokens (our own `banned` flag blocks
        the current token immediately; this blocks renewal)."""
        self.http.request("PUT", f"{self.base}/auth/v1/admin/users/{uid}",
                          json={"ban_duration": "876000h" if banned else "none"},
                          headers=self._headers())

    def _count(self, **filters):
        r = self._req("GET", "/rest/v1/profiles", {"select": "id", "limit": 1, **filters},
                      prefer="count=exact")
        return self._total(r)

    def counts(self):
        z = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
        now = datetime.now(timezone.utc)
        return dict(
            users=self._count(),
            pro=self._count(tier="eq.pro"),
            banned=self._count(banned="eq.true"),
            signups_7d=self._count(created_at=f"gte.{z(now - timedelta(days=7))}"),
            active_24h=self._count(last_seen_at=f"gte.{z(now - timedelta(days=1))}"))

    # ---- features
    def list_features(self):
        return self._req("GET", "/rest/v1/features",
                         {"select": "*", "order": "sort.asc,key.asc"}).json()

    def upsert_feature(self, f):
        rows = self._req("POST", "/rest/v1/features", {"on_conflict": "key"},
                         json=_clean_feature(f),
                         prefer="resolution=merge-duplicates,return=representation").json()
        return rows[0] if rows else _clean_feature(f)

    def delete_feature(self, key):
        self._req("DELETE", "/rest/v1/features", {"key": f"eq.{key}"})
        return True

    # ---- usage
    def usage_for_user(self, day, uid):
        rows = self._req("GET", "/rest/v1/usage_daily",
                         {"day": f"eq.{day}", "user_id": f"eq.{uid}",
                          "select": "feature,calls"}).json()
        return {r["feature"]: r["calls"] for r in rows}

    def usage_for_users(self, day, uids):
        out = {u: {} for u in uids}
        if not uids:
            return out
        rows = self._req("GET", "/rest/v1/usage_daily",
                         {"day": f"eq.{day}", "user_id": "in.(" + ",".join(uids) + ")",
                          "select": "user_id,feature,calls"}).json()
        for r in rows:
            out.setdefault(r["user_id"], {})[r["feature"]] = r["calls"]
        return out

    def usage_totals(self, day):
        rows = self._req("GET", "/rest/v1/usage_daily",
                         {"day": f"eq.{day}", "select": "feature,calls", "limit": 100000}).json()
        out = {}
        for r in rows:
            out[r["feature"]] = out.get(r["feature"], 0) + r["calls"]
        return out

    def add_usage(self, day, rows):
        payload = [dict(user_id=u, feature=f, calls=n) for u, f, n in rows if n]
        if payload:
            self._req("POST", "/rest/v1/rpc/add_usage", json={"p_day": day, "p_rows": payload})

    # ---- audit
    def log_audit(self, actor, action, target="", detail=None):
        self._req("POST", "/rest/v1/audit_log",
                  json=dict(actor=actor, action=action, target=target, detail=detail or {}))

    def list_audit(self, limit=100):
        return self._req("GET", "/rest/v1/audit_log",
                         {"select": "*", "order": "at.desc", "limit": limit}).json()
