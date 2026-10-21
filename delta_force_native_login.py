"""
Native email/password login for Delta Force (web-widget flow, sdk 1.31.1).

Flow (from your captures):
  1. POST /account/login with machine_check_type=3, no tencent_response
  2. Server answers ret=2170 "machine check err!" and gives the captcha appid
     in extra_json.machine_check_conf.appid
  3. Show Tencent's OWN captcha widget to the user (a small pywebview window)
  4. Re-POST /account/login with tencent_response = {"appid","ret","ticket","randstr"}
  5. ret=0 -> token, uid, expire

Security: credentials go only to the official host over HTTPS. The password is
MD5'd locally (as the SDK does), never logged, never stored. Persist only the
returned token/uid/expire.

Run `python delta_force_native_login.py --captcha <appid>` is used internally
to pop the captcha window in a subprocess (keeps pywebview's main-thread
requirement away from your Tk/Qt GUI).
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid

import requests

# ---------------------------------------------------------------------------
# Config - values taken from your capture
# ---------------------------------------------------------------------------
HOST = "https://li-sg.intlgame.com"
LOGIN_PATH = "/account/login"
APP_ID = "b8828b216fcc56910b17d4b8d3e1838f"
ACCOUNT_PLAT_TYPE = "131"
SDK_VERSION = "1.31.1"
SOURCE = "66"

# Intlgame appkeys. The key depends on the endpoint family (verified against
# captured signatures for getsid, get_conf, account/login and v2/auth/login).
APPKEY_ACCOUNT = "be83e12d807ed10f5cdcb3144773ee56"
APPKEY_DR = "0d88135dd851f81f9601e477b261a137"
_KEY_BY_PREFIX = (("/v2/dr/", APPKEY_DR),
                  ("/v2/conf/", APPKEY_ACCOUNT),
                  ("/account/", APPKEY_ACCOUNT))


def appkey_for(path: str) -> str:
    for prefix, key in _KEY_BY_PREFIX:
        if path.startswith(prefix):
            return key
    return APPKEY_ACCOUNT   # /v2/auth/login verified with this one

# Tencent Captcha loader, as seen in the page's own network traffic.
TCAPTCHA_JS = "https://global.captcha.gtimg.com/TCaptcha-global.js"

PAGE = "https%3A%2F%2Fwww.playdeltaforce.com%2Fevents%2Fhq%2Fen%2F"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
# Set DF_LOGIN_DEBUG=1 to print each login step (never prints tokens).
DEBUG = bool(os.environ.get("DF_LOGIN_DEBUG"))


def _dbg(*a):
    if DEBUG:
        print("[native-login]", *a)


RET_MACHINE_CHECK = 2170
RET_NEED_CAPTCHA = 1005          # "require valid tencent_response"
# Appid of the invisible (no-perception) check the page runs first.
FIRST_CAPTCHA_APPID = "188981228"


class LoginError(Exception):
    def __init__(self, msg, ret=None, payload=None):
        super().__init__(msg)
        self.ret, self.payload = ret, payload


class CaptchaRequired(LoginError):
    def __init__(self, appid, payload=None):
        super().__init__("captcha required", RET_MACHINE_CHECK, payload)
        self.appid = appid


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _json(obj) -> str:
    # Compact, key order preserved, exactly the string we sign AND send.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def build_query(ts: int) -> str:
    return (f"account_plat_type={ACCOUNT_PLAT_TYPE}&app_id={APP_ID}"
            f"&lang_type=en&os=3&sdk_version={SDK_VERSION}"
            f"&source={SOURCE}&ts={ts}")


def build_body(email, password_md5, guest_id, body_ts, tencent=None) -> str:
    body = {
        "account": email,
        "password": password_md5,
        "account_type": 1,
        "support_captcha": 1,
        "machine_check_type": 3,
    }
    if tencent:
        # Nested JSON *string*, key order as the page sends it.
        body["tencent_response"] = _json({
            "appid": str(tencent["appid"]), "ret": 0,
            "ticket": tencent["ticket"], "randstr": tencent["randstr"],
        })
    body["device_info"] = {
        "guest_id": guest_id, "lang_type": "en",
        "app_version": "WebWidget_1.32.0",
        "screen_height": 1440, "screen_width": 1920,
        "device_brand": "Google Inc.",
        "device_model": UA.replace("Mozilla/", "", 1),
        "network_type": "4g", "ram_total": 150, "rom_total": 150,
        "cpu_name": "Win32", "android_imei": "", "ios_idfa": "",
        "page": PAGE, "page_with_search": PAGE, "ts": body_ts,
    }
    return _json(body)


def sign(path: str, params: dict, body: str = "", secret: str = None) -> str:
    """md5(path + '?' + sorted 'k=v' joined by '&' (no sig) + raw body + appkey)."""
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return md5(path + "?" + qs + body + (secret or appkey_for(path)))


def _post_login(email, password, guest_id, tencent=None, timeout=20) -> dict:
    now = int(time.time() * 1000)
    params = {
        "account_plat_type": ACCOUNT_PLAT_TYPE, "app_id": APP_ID,
        "lang_type": "en", "os": "3", "sdk_version": SDK_VERSION,
        "source": SOURCE, "ts": str(now),
    }
    body = build_body(email, md5(password), guest_id, now - 1, tencent)
    params["sig"] = sign(LOGIN_PATH, {k: v for k, v in params.items()}, body)
    url = f"{HOST}{LOGIN_PATH}?" + "&".join(
        f"{k}={params[k]}" for k in sorted(params))
    # Send the exact string that was signed, not a re-serialised dict.
    r = requests.post(url, data=body.encode("utf-8"), timeout=timeout, headers={
        "Content-Type": "application/json", "User-Agent": UA,
        "Origin": "https://www.playdeltaforce.com",
        "Referer": "https://www.playdeltaforce.com/",
    })
    try:
        return r.json()
    except Exception:
        raise LoginError(f"non-JSON response (HTTP {r.status_code})")


def selftest(login_body_file: str = None) -> bool:
    """Checks against real captured signatures. Optionally pass a file holding
    the byte-exact body of a captured /account/login (query ts=1791870859439,
    sig=856e37c2fdc7c7182ce658fe265984e8)."""
    checks = [
        ("getsid", "/v2/dr/getsid",
         {"channelid": "131", "gameid": "29158", "os": "3",
          "sdk_version": "1.5.2", "seq": "1", "source": "32",
          "ts": "1791869951690"}, "{}",
         "7990fda5667d1af0adb730a4b6cc54ba"),
        ("get_conf", "/v2/conf/get_conf",
         {"channelid": "25", "conn": "0", "gameid": "29158", "os": "3",
          "sdk_version": "1.31.1", "seq": "", "source": "66",
          "ts": "1791870831553"},
         '{"project":"LIPASS_WEB","device_info":'
         '{"webID":"c949ac56-b07a-4bdc-9f6b-54274c9fc677"}}',
         "881fbf7fd7f873345677ac20e1cebc8f"),
    ]
    if login_body_file:
        body = open(login_body_file, encoding="utf-8").read()
        checks.append(("account/login", LOGIN_PATH,
                       {"account_plat_type": "131", "app_id": APP_ID,
                        "lang_type": "en", "os": "3",
                        "sdk_version": "1.31.1", "source": "66",
                        "ts": "1791870859439"}, body,
                       "856e37c2fdc7c7182ce658fe265984e8"))
    ok = True
    for name, path, params, body, expected in checks:
        got = sign(path, params, body)
        print(f"{name:14}", "OK" if got == expected else f"FAIL (got {got})")
        ok = ok and got == expected
    return ok


def _interpret(data: dict) -> dict:
    ret = data.get("ret")
    if ret == 0:
        return {"token": data["token"], "uid": data["uid"],
                "expire": data.get("expire"), "raw": data}
    if ret == RET_MACHINE_CHECK:
        appid = (data.get("extra_json", {})
                     .get("machine_check_conf", {}).get("appid"))
        raise CaptchaRequired(appid, data)
    raise LoginError(data.get("msg", "login failed"), ret, data)


# ---------------------------------------------------------------------------
# Captcha: Tencent's official widget in a pywebview window (subprocess)
# ---------------------------------------------------------------------------
_CAPTCHA_HTML = """<!doctype html><meta charset=utf-8>
<body style="font-family:sans-serif;background:#111;color:#ddd;text-align:center">
<p id=m>Loading verification...</p>
<script src="%(js)s"></script>
<script>
function start(){
  var c = new TencentCaptcha("%(appid)s", function(res){
    pywebview.api.done(JSON.stringify(res));
  }, %(opts)s);
  c.show();
}
window.addEventListener('pywebviewready', start);
</script>"""


def _captcha_window_main(appid: str, invisible: bool = False):
    import webview  # pip install pywebview

    result = {}

    class Api:
        def done(self, payload):
            result.update(json.loads(payload))
            win.destroy()

    win = webview.create_window(
        "Verify", html=_CAPTCHA_HTML % {"js": TCAPTCHA_JS, "appid": appid,
                              "opts": json.dumps({"userLanguage": "en",
                                                  "enableNonPerception": bool(invisible)})},
        js_api=Api(), width=420, height=520)
    webview.start()
    print(json.dumps(result))


def solve_captcha(appid: str, invisible: bool = False) -> dict:
    """Blocks until the user finishes (or cancels) the captcha."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--captcha", appid]
    else:
        cmd = [sys.executable, __file__, "--captcha", appid]
    if invisible:
        cmd.append("invisible")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        raise LoginError("captcha window failed: " + (out.stderr or "")[-200:])
    _dbg("captcha widget result: ret=%r errorCode=%r errorMessage=%r"
         % (res.get("ret"), res.get("errorCode"), res.get("errorMessage")))
    if res.get("ret") != 0:
        raise LoginError("captcha not completed (widget ret=%r code=%r)"
                         % (res.get("ret"), res.get("errorCode")))
    return {"appid": appid, "ticket": res["ticket"], "randstr": res["randstr"]}



# ---------------------------------------------------------------------------
# Step 2: exchange the passport token for the game session (openid + token).
# Captured: POST sg.intlgame.com/v2/auth/login. Its response carries the
# `openid` and `token` that every DfTools request uses.
# ---------------------------------------------------------------------------
AUTH_HOST = "https://sg.intlgame.com"
AUTH_PATH = "/v2/auth/login"
AUTH_CHANNEL = "131"
GAME_ID = "29158"


def exchange_session(email: str, login_result: dict, guest_id: str,
                     timeout: int = 20) -> dict:
    now = int(time.time() * 1000)
    params = {"channelid": AUTH_CHANNEL, "conn": "0", "gameid": GAME_ID,
              "os": "3", "sdk_version": SDK_VERSION, "seq": "",
              "source": SOURCE, "ts": str(now)}
    body = _json({
        "device_info": {
            "guest_id": guest_id, "lang_type": "en",
            "app_version": "WebWidget_1.32.0",
            "screen_height": 1440, "screen_width": 2560,
            "device_brand": "Google Inc.",
            "device_model": UA.replace("Mozilla/", "", 1),
            "network_type": "4g", "ram_total": 111, "rom_total": 111,
            "cpu_name": "Win32", "android_imei": "", "ios_idfa": "",
            "page": PAGE, "page_with_search": PAGE, "ts": now - 1},
        "channel_dis": "00000000",
        "channel_info": {
            "openid": login_result["uid"], "token": login_result["token"],
            "account_plat_type": int(ACCOUNT_PLAT_TYPE), "account": email,
            "account_type": 1, "lang_type": "en"},
        "auto_bind_lip": 1,
    })
    params["sig"] = sign(AUTH_PATH, dict(params), body)
    url = f"{AUTH_HOST}{AUTH_PATH}?" + "&".join(
        f"{k}={params[k]}" for k in sorted(params))
    r = requests.post(url, data=body.encode("utf-8"), timeout=timeout, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": UA, "Origin": "https://www.playdeltaforce.com",
        "Referer": "https://www.playdeltaforce.com/"})
    data = r.json()
    if data.get("ret") != 0:
        raise LoginError(data.get("msg", "auth/login failed"),
                         data.get("ret"), data)
    return {"openid": data["openid"], "token": data["token"],
            "expire": data.get("token_expire_time"), "raw": data}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def login(email: str, password: str, guest_id: str | None = None) -> dict:
    """Returns {"token","uid","expire","raw"}. Blocking - run it in a worker thread."""
    guest_id = guest_id or str(uuid.uuid4())
    tencent = None
    for attempt in range(1, 5):              # normally 3 attempts at most
        _dbg(f"login attempt {attempt}, "
             f"ticket={'yes (appid ' + str(tencent['appid']) + ')' if tencent else 'none'}")
        try:
            res = _interpret(_post_login(email, password, guest_id, tencent))
            _dbg("login ok")
            break
        except CaptchaRequired as c:         # 2170: interactive captcha
            _dbg(f"server: ret=2170 -> interactive captcha appid={c.appid}")
            if not c.appid:
                raise LoginError("captcha required but no appid in response",
                                 payload=c.payload)
            tencent = solve_captcha(c.appid)
        except LoginError as e:
            _dbg(f"server: ret={e.ret} msg={e}")
            if e.ret == RET_NEED_CAPTCHA:    # 1005: run the invisible check
                tencent = solve_captcha(FIRST_CAPTCHA_APPID, invisible=True)
            else:
                raise
    else:
        raise LoginError("captcha loop did not converge")
    # Step 2 -> game openid/token (what DfTools requests carry)
    res["session"] = exchange_session(email, res, guest_id)
    return res



# ---------------------------------------------------------------------------
# Session persistence (token only - the password is never stored)
# ---------------------------------------------------------------------------
def _session_file():
    from delta_force_paths import APP_DATA_DIR
    return APP_DATA_DIR / "native_session.json"


def save_session(result: dict) -> None:
    sess = result.get("session") or {}
    _session_file().write_text(json.dumps({
        "token": result["token"], "uid": result["uid"],
        "expire": result.get("expire"), "saved_at": int(time.time()),
        "openid": sess.get("openid"), "game_token": sess.get("token"),
    }, indent=2))


def load_session() -> dict:
    try:
        sess = json.loads(_session_file().read_text())
    except Exception:
        return {}
    if sess.get("expire") and sess["expire"] < time.time():
        return {}
    return sess


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        # optional: path to a file holding the EXACT raw request body of the
        # captured login (sig 840e86f9...)
        raw = open(sys.argv[2], encoding="utf-8").read() if len(sys.argv) > 2 else None
        sys.exit(0 if selftest(raw) else 1)
    if len(sys.argv) >= 2 and sys.argv[1] == "--login":
        # Interactive test: python delta_force_native_login.py --login
        import getpass
        email = input("Email: ").strip()
        pw = getpass.getpass("Password: ")          # not echoed, not stored
        try:
            res = login(email, pw)
        except LoginError as e:
            print("Login failed:", e, "| ret =", e.ret)
            print("Server said:", json.dumps(e.payload, indent=2)[:1500])
            sys.exit(1)
        sess = res["session"]
        print("OK  uid:", res["uid"], "| openid:", sess["openid"])
        print("passport token:", res["token"][:8] + "...",
              "| game token:", sess["token"][:8] + "...")
        save_session(res)
        print("Saved token/openid (not the password) to native_session.json")
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--captcha":
        _captcha_window_main(sys.argv[2], invisible="invisible" in sys.argv[3:])
