"""
DF Tracker account sign-in and feature access.

* Sign-in is Discord via Supabase, using the browser and PKCE: the app starts a
  tiny web server on 127.0.0.1, sends you to Supabase's login page, and gets a
  one-time code back on that address. No secret is stored in the app - only
  Supabase's PUBLIC (publishable) key.
* The session (refresh token) is stored encrypted with Windows' own per-user
  protection (DPAPI). Tokens refresh themselves.
* The server decides what each account may use (GET /v1/me). This module caches
  that answer so the app can grey out locked features, and keeps working on a
  flaky connection. The SERVER enforces every limit; nothing here is a lock.

Settings (delta_force_config.py or environment):
    ACCOUNTS_SUPABASE_URL   https://<project>.supabase.co
    ACCOUNTS_SUPABASE_KEY   the project's publishable (anon) key
    ACCOUNTS_GATEWAY_URL    https://<your-gateway>.up.railway.app
    ACCOUNTS_UPGRADE_URL    (optional) where "Upgrade" should send people
If the first three aren't set, accounts are switched off and every feature is
available exactly as before.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

try:
    import delta_force_config as _cfg
except ImportError:                      # pragma: no cover
    _cfg = None


def _c(name, default=""):
    return os.environ.get(name) or getattr(_cfg, name, default) or default


def _url(value):
    """Tidy an address from the config: trim spaces and any trailing slash, and add
    https:// if it was left off (a bare 'name.up.railway.app' can't be requested).
    Local addresses (localhost / 127.x) get http:// for development."""
    u = (value or "").strip().rstrip("/")
    if u and "://" not in u:
        u = ("http://" if u.startswith(("localhost", "127.")) else "https://") + u
    return u


# Features that need an account because they run through the server. Others
# stay usable without signing in until they are moved behind the server too.
REQUIRES_ACCOUNT = {"player_lookup"}

CALLBACK_PORTS = (53682, 53683, 53684, 53685, 53686)   # all must be allow-listed in Supabase
ENTITLEMENT_TTL = 600            # seconds before the cached answer is refreshed
FLAGS_TTL = 600                  # how often the public on/off switches are re-read
FLAGS_GRACE = 7 * 86400          # switches older than this are ignored (fail closed)
OFFLINE_GRACE = 7 * 86400        # how long a cached answer is trusted with no connection


class SignInError(Exception):
    pass


class SignInCancelled(SignInError):
    pass


@dataclass
class Decision:
    allowed: bool
    reason: str = "ok"        # ok | signin | locked | disabled | banned
    message: str = ""

    def __bool__(self):
        return self.allowed


# ---------------------------------------------------------------- DPAPI
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data: bytes):
    """-> (bytes, is_protected). Falls back to plain bytes off Windows."""
    if sys.platform == "win32":
        try:
            return _dpapi(data, True), True
        except Exception:
            pass
    return data, False


def unprotect(data: bytes, was_protected: bool) -> bytes:
    return _dpapi(data, False) if was_protected else data


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ============================================================== callback page
_DONE_PAGE = (b"<!doctype html><meta charset=utf-8><title>DF Tracker</title>"
              b"<body style='font:16px Segoe UI,sans-serif;background:#0b1424;color:#e8eef9;"
              b"display:grid;place-items:center;height:100vh;margin:0'><div style='text-align:center'>"
              b"<h2>%s</h2><p style='color:#9aaccc'>%s</p></div>")


class _CallbackServer(socketserver.TCPServer):
    # On Windows SO_REUSEADDR would let ANOTHER program share the port, so it
    # stays off there. Elsewhere it only permits re-binding after a recent run.
    allow_reuse_address = sys.platform != "win32"
    result = None

    def __init__(self, port):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                url = urlparse(self.path)
                if url.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if q.get("code") or q.get("error") or q.get("error_description"):
                    self.server.result = q
                    ok = bool(q.get("code"))
                    body = _DONE_PAGE % ((b"You're signed in" if ok else b"Sign-in didn't finish"),
                                         (b"You can close this tab and return to DF Tracker." if ok
                                          else b"Return to DF Tracker and try again."))
                else:
                    body = _DONE_PAGE % (b"Nothing to do", b"This page is used for signing in.")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(("127.0.0.1", port), Handler)


# ===================================================================== Accounts
class Accounts:
    def __init__(self, storage_dir=None, supabase_url=None, supabase_key=None,
                 gateway_url=None, http=None, clock=time.time, open_url=webbrowser.open):
        self.supabase_url = _url(supabase_url if supabase_url is not None else _c("ACCOUNTS_SUPABASE_URL"))
        self.supabase_key = (supabase_key if supabase_key is not None else _c("ACCOUNTS_SUPABASE_KEY")).strip()
        self.gateway_url = _url(gateway_url if gateway_url is not None else _c("ACCOUNTS_GATEWAY_URL"))
        self.upgrade_url = _url(_c("ACCOUNTS_UPGRADE_URL"))
        if storage_dir is None:
            try:
                from delta_force_paths import APP_DATA_DIR
                storage_dir = APP_DATA_DIR
            except Exception:
                storage_dir = Path.home() / ".df_tracker"
        self.dir = Path(storage_dir)
        self.http, self.clock, self.open_url = http or requests.Session(), clock, open_url
        self._lock = threading.RLock()
        self._listeners = []
        self._session = None         # {access_token, refresh_token, expires_at, user}
        self._ent = None             # last /v1/me answer
        self._ent_at = 0
        self._banned = ""            # ban message if the server said so
        self._flags, self._flags_at = {}, 0
        self._load_session()
        self._load_entitlements()
        self._load_flags()

    # -------------------------------------------------------------- basics
    @property
    def enabled(self):
        return bool(self.supabase_url and self.supabase_key and self.gateway_url)

    @property
    def signed_in(self):
        return bool(self._session)

    def on_change(self, fn):
        self._listeners.append(fn)

    def _changed(self):
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    def _headers(self):
        return {"apikey": self.supabase_key, "Content-Type": "application/json"}

    # ------------------------------------------------------------ storage
    @property
    def _session_file(self):
        return self.dir / "accounts_session.json"

    @property
    def _ent_file(self):
        return self.dir / "accounts_cache.json"

    def _save_session(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            raw, prot = protect(json.dumps(self._session).encode())
            self._session_file.write_text(json.dumps(
                {"v": 1, "protected": prot, "data": base64.b64encode(raw).decode()}))
        except OSError:
            pass

    def _load_session(self):
        try:
            blob = json.loads(self._session_file.read_text())
            raw = unprotect(base64.b64decode(blob["data"]), blob.get("protected", False))
            self._session = json.loads(raw)
        except Exception:
            self._session = None

    def _clear_session(self):
        self._session = None
        self._ent, self._ent_at, self._banned = None, 0, ""
        for f in (self._session_file, self._ent_file):
            try:
                f.unlink()
            except OSError:
                pass

    def _save_entitlements(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._ent_file.write_text(json.dumps({"at": self._ent_at, "ent": self._ent,
                                                  "uid": (self._session or {}).get("user", {}).get("id")}))
        except OSError:
            pass

    def _load_entitlements(self):
        try:
            d = json.loads(self._ent_file.read_text())
            if (self._session or {}).get("user", {}).get("id") == d.get("uid"):
                self._ent, self._ent_at = d["ent"], d["at"]
        except Exception:
            pass

    # ------------------------------------------------- public on/off switches
    @property
    def _flags_file(self):
        return self.dir / "accounts_flags.json"

    def _load_flags(self):
        try:
            d = json.loads(self._flags_file.read_text())
            self._flags, self._flags_at = dict(d["flags"]), float(d["at"])
        except Exception:
            pass

    def flag(self, name, default=False):
        """A server-side on/off switch that needs no sign-in (e.g. email_login).
        FAILS CLOSED: if the switch has never been read, or hasn't been read for
        a week, the answer is `default` (False)."""
        if self.clock() - self._flags_at > FLAGS_GRACE:
            return default
        return bool(self._flags.get(name, default))

    def refresh_flags(self, force=False):
        if not self.gateway_url:
            return self._flags
        if not force and self.clock() - self._flags_at < FLAGS_TTL:
            return self._flags
        try:
            r = self.http.get(f"{self.gateway_url}/v1/flags", timeout=8)
            flags = r.json().get("flags") if r.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return self._flags                        # offline: keep the last answer
        if not isinstance(flags, dict):
            return self._flags
        changed = flags != self._flags
        self._flags, self._flags_at = {k: bool(v) for k, v in flags.items()}, self.clock()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._flags_file.write_text(json.dumps({"at": self._flags_at, "flags": self._flags}))
        except OSError:
            pass
        if changed:
            self._changed()
        return self._flags

    def refresh_flags_async(self, force=False):
        threading.Thread(target=self.refresh_flags, args=(force,), daemon=True,
                         name="accounts-flags").start()

    # ------------------------------------------------------------- sign in
    def sign_in(self, timeout=180, cancel=None):
        """Blocking: opens the browser, waits for the login to finish. Run it
        in a worker thread. Raises SignInCancelled / SignInError."""
        if not self.enabled:
            raise SignInError("Accounts aren't set up in this version.")
        server = None
        for port in CALLBACK_PORTS:
            try:
                server = _CallbackServer(port)
                break
            except OSError:
                continue
        if server is None:
            raise SignInError("Couldn't start the sign-in listener (ports are busy). "
                              "Close other copies of DF Tracker and try again.")
        try:
            server.timeout = 0.25
            verifier = _b64(secrets.token_bytes(48))                # 64 chars
            challenge = _b64(hashlib.sha256(verifier.encode()).digest())
            redirect = f"http://127.0.0.1:{server.server_address[1]}/callback"
            url = f"{self.supabase_url}/auth/v1/authorize?" + urlencode({
                "provider": "discord", "redirect_to": redirect,
                "code_challenge": challenge, "code_challenge_method": "s256"})
            self.open_url(url)
            deadline = time.monotonic() + timeout          # not the wall clock
            while server.result is None:
                if cancel is not None and cancel.is_set():
                    raise SignInCancelled("Sign-in cancelled.")
                if time.monotonic() > deadline:
                    raise SignInError("Sign-in timed out. Please try again.")
                server.handle_request()
            result = server.result
        finally:
            server.server_close()

        if not result.get("code"):
            raise SignInError(result.get("error_description") or result.get("error") or "Sign-in failed.")
        r = self.http.post(f"{self.supabase_url}/auth/v1/token", params={"grant_type": "pkce"},
                           headers=self._headers(), timeout=15,
                           data=json.dumps({"auth_code": result["code"], "code_verifier": verifier}))
        if r.status_code != 200:
            raise SignInError("The sign-in code wasn't accepted. Please try again.")
        with self._lock:
            self._adopt(r.json())
            self._ent, self._ent_at, self._banned = None, 0, ""
        self.refresh_entitlements(force=True)
        self._changed()
        return self.status()

    def _adopt(self, data):
        meta = (data.get("user") or {}).get("user_metadata") or {}
        user = data.get("user") or {}
        self._session = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or (self._session or {}).get("refresh_token", ""),
            "expires_at": self.clock() + int(data.get("expires_in") or 3600),
            "user": {"id": user.get("id", ""), "email": user.get("email", ""),
                     "name": meta.get("full_name") or meta.get("name") or meta.get("user_name") or "",
                     "avatar": meta.get("avatar_url") or meta.get("picture") or ""}}
        self._save_session()

    def sign_out(self):
        with self._lock:
            token = (self._session or {}).get("access_token")
            self._clear_session()
        if token and self.enabled:
            try:
                self.http.post(f"{self.supabase_url}/auth/v1/logout", timeout=5,
                               headers={**self._headers(), "Authorization": f"Bearer {token}"})
            except requests.RequestException:
                pass
        self._changed()

    # ------------------------------------------------------------ tokens
    def access_token(self):
        """A currently valid access token, refreshed if needed; None if signed out."""
        with self._lock:
            s = self._session
            if not s:
                return None
            if s["expires_at"] - self.clock() > 60:
                return s["access_token"]
            return self._refresh_locked()

    def _refresh_locked(self):
        s = self._session
        try:
            r = self.http.post(f"{self.supabase_url}/auth/v1/token", params={"grant_type": "refresh_token"},
                               headers=self._headers(), timeout=15,
                               data=json.dumps({"refresh_token": s["refresh_token"]}))
        except requests.RequestException:
            return s["access_token"] if s["expires_at"] > self.clock() else None   # offline
        if r.status_code in (400, 401, 403):
            self._clear_session()                    # refresh token revoked/expired: sign in again
            self._changed()
            return None
        if r.status_code != 200:
            return s["access_token"] if s["expires_at"] > self.clock() else None
        self._adopt(r.json())
        return self._session["access_token"]

    # ------------------------------------------------------- entitlements
    def refresh_entitlements(self, force=False):
        """Ask the server what this account may do. Returns the answer, the
        cached one if the server can't be reached, or None."""
        if not (self.enabled and self.signed_in):
            return None
        with self._lock:
            if not force and self._ent and self.clock() - self._ent_at < ENTITLEMENT_TTL:
                return self._ent
        for attempt in (1, 2):
            token = self.access_token()
            if not token:
                return None
            try:
                r = self.http.get(f"{self.gateway_url}/v1/me", timeout=10,
                                  headers={"Authorization": f"Bearer {token}"})
            except requests.RequestException:
                return self._ent                                  # offline: keep what we have
            if r.status_code == 401 and attempt == 1:
                with self._lock:                                  # token rejected: force a refresh
                    if self._session:
                        self._session["expires_at"] = 0
                continue
            break
        if r.status_code == 200:
            with self._lock:
                changed = self._ent != r.json() or self._banned
                self._ent, self._ent_at, self._banned = r.json(), self.clock(), ""
                self._save_entitlements()
            if changed:
                self._changed()
            return self._ent
        if r.status_code == 403 and _err(r) == "banned":
            self._banned = _msg(r) or "This account has been suspended."
            self._changed()
        elif r.status_code == 401:
            self._clear_session()
            self._changed()
        return self._ent

    def refresh_async(self, force=False):
        threading.Thread(target=self.refresh_entitlements, args=(force,), daemon=True,
                         name="accounts-refresh").start()

    @property
    def tier(self):
        return (self._ent or {}).get("tier", "free")

    def usage(self, feature):
        f = ((self._ent or {}).get("features") or {}).get(feature) or {}
        return f.get("used", 0), f.get("limit")

    def note_quota(self, used, limit):
        """Server reply headers carry fresh usage after each lookup."""
        if self._ent and "player_lookup" in self._ent.get("features", {}):
            self._ent["features"]["player_lookup"]["used"] = used

    def can(self, feature) -> Decision:
        if not self.enabled:
            return Decision(True)
        if not self.signed_in:
            if feature in REQUIRES_ACCOUNT:
                return Decision(False, "signin", "Sign in to your DF Tracker account to use this.")
            return Decision(True)
        if self._banned:
            return Decision(False, "banned", self._banned)
        f = ((self._ent or {}).get("features") or {}).get(feature)
        if f is None or f.get("enabled"):
            return Decision(True)               # unknown / no answer yet: the server still decides
        if f.get("globally_disabled"):
            return Decision(False, "disabled", f"{f.get('label') or feature} is temporarily unavailable.")
        plan = str(f.get("min_tier") or "pro").title()
        return Decision(False, "locked", f"{f.get('label') or feature} is part of {plan}.")

    def status(self):
        s = self._session or {}
        u = s.get("user") or {}
        used, limit = self.usage("player_lookup")
        return {"enabled": self.enabled, "signed_in": self.signed_in,
                "name": (self._ent or {}).get("user", {}).get("name") or u.get("name", ""),
                "email": u.get("email", ""), "avatar": u.get("avatar", ""),
                "tier": self.tier, "banned": self._banned,
                "lookups_used": used, "lookups_limit": limit,
                "stale": bool(self._ent) and self.clock() - self._ent_at > OFFLINE_GRACE,
                "upgrade_url": self.upgrade_url}


def _err(r):
    try:
        return r.json().get("error", "")
    except ValueError:
        return ""


def _msg(r):
    try:
        return r.json().get("message", "")
    except ValueError:
        return ""
