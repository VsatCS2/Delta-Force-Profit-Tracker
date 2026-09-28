"""
Delta Force — credential harvester via embedded browser.

Opens a real Chromium-based browser window pointed at the DfTools site. The
user logs in normally. We watch the network for the three DfTools endpoints
and copy their full request params + body + headers into a single JSON
file.

Why this works when manual copy-paste is painful:
    The `s` signature is computed by the DfTools page's own JS. Instead of
    reproducing it, we let the page compute it and read it off the wire.

Which browser it opens:
    This drives your already-installed Google Chrome or Microsoft Edge
    (via Playwright's "channel" option) instead of downloading Playwright's
    own bundled Chromium (~300MB). Almost everyone already has one of the
    two installed, so there's normally nothing extra to download. If
    neither is found, it falls back to Playwright's bundled Chromium *if*
    that happens to be installed separately, and otherwise raises a clear
    error asking the person to install Chrome or Edge.

Requires:
    pip install playwright
    (no `playwright install chromium` needed — see above)

Usage:
    python delta_force_login.py                # interactive
    python delta_force_login.py --persistent   # keep cookies between runs
"""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright

import delta_force_core as core
from delta_force_paths import APP_DATA_DIR

DFTOOLS_URL = "https://www.playdeltaforce.com/events/hq/en/"

CREDS_FILE = core.DFTOOLS_CREDS_FILE
# Not migrated from the old __file__-relative location like the other
# data files: it's a whole browser-profile directory tree (cookies,
# local storage, etc.), not a single file, so a one-time copy is more
# code than it's worth for what just means one extra login if someone
# used --login-persistent before this fix.
PROFILE_DIR = APP_DATA_DIR / "playwright_profile"

# Tried in order. "chrome"/"msedge" use the browser already installed on
# the machine (no download); None means Playwright's own bundled Chromium,
# tried last and only works if someone separately ran
# `playwright install chromium` on this machine.
_BROWSER_CHANNELS = ("chrome", "msedge", None)

TARGET_ENDPOINTS = {
    "GetMatchList":            "matchlist",
    "GetMyData":               "mydata",
    "GetMatchDetail":          "matchdetail",
    "GetAssetWeekCalendar":    "assetcalendar",
    "GetWeeklyReportSolData":  "weeklyreport",
}

LOGIN_TIMEOUT_SECONDS = 600

INTERESTING_PARAMS = (
    "openid", "token", "game_id", "channel",
    "account_type", "lang_type", "u", "a", "ts", "s",
)

_HEADERS_TO_STRIP = {
    ":authority", ":method", ":path", ":scheme",
    "content-length", "host", "cookie",
}


def _extract_params(url: str) -> dict:
    q = parse_qs(urlparse(url).query)
    out = {}
    for key in INTERESTING_PARAMS:
        if key in q and q[key]:
            out[key] = q[key][0]
    return out


def _clean_headers(raw: dict) -> dict:
    out = {}
    for k, v in (raw or {}).items():
        if k.lower() in _HEADERS_TO_STRIP:
            continue
        out[k] = v
    return out


def _short_key(name: str):
    """Map a long endpoint name or short key to its short key. Returns
    None if it doesn't match any known endpoint."""
    if not name:
        return None
    n = str(name).lower()
    for long, short in TARGET_ENDPOINTS.items():
        if n == long.lower() or n == short:
            return short
    return None


async def _launch_context(p, headless: bool, persistent: bool):
    """Try each browser channel in turn and return (ctx, browser_or_None).

    browser is None for the persistent-profile path, where the context IS
    the top-level handle (Playwright's launch_persistent_context has no
    separate Browser object to close later).
    """
    last_err = None
    for channel in _BROWSER_CHANNELS:
        launch_kwargs = {"headless": headless}
        if channel:
            launch_kwargs["channel"] = channel
        try:
            if persistent:
                PROFILE_DIR.mkdir(exist_ok=True)
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR), **launch_kwargs)
                browser_obj = None
            else:
                browser_obj = await p.chromium.launch(**launch_kwargs)
                ctx = await browser_obj.new_context()
        except Exception as e:
            last_err = e
            continue
        print(f"[login] using {_channel_label(channel)}")
        return ctx, browser_obj

    raise RuntimeError(
        "Couldn't find a browser to log in with. This tool drives your "
        "own Google Chrome or Microsoft Edge — please install one of "
        "those and try again.\n"
        f"(last error: {last_err})"
    )


def _channel_label(channel):
    return {
        "chrome": "Google Chrome",
        "msedge": "Microsoft Edge",
    }.get(channel, "Playwright's bundled Chromium")


async def _run(headless: bool = False, persistent: bool = False) -> dict:
    captured: dict = {}
    seen: set = set()

    async with async_playwright() as p:
        ctx, browser = await _launch_context(p, headless, persistent)

        pages = ctx.pages
        page = pages[0] if pages else await ctx.new_page()

        async def on_request(req):
            if req.method != "POST":
                return
            url = req.url
            # Which long endpoint name appears in the URL?
            long_name = next((n for n in TARGET_ENDPOINTS if n in url), None)
            if long_name is None:
                return

            params = _extract_params(url)
            if not (params.get("openid") and params.get("token")
                    and params.get("s")):
                return

            try:
                body = req.post_data_json
            except Exception:
                body = None

            headers = _clean_headers(dict(req.headers))

            record = {
                "url": url,
                "method": req.method,
                "params": params,
                "body": body,
                "endpoint": long_name,
                "headers": headers,
            }
            # Store under the short key so core's mapping finds it directly.
            short = TARGET_ENDPOINTS[long_name]
            captured[short] = record

            if short not in seen:
                seen.add(short)
                print(f"[login] captured {long_name}: "
                      f"openid={params.get('openid')} "
                      f"token={params.get('token', '')[:12]}… "
                      f"s={params.get('s', '')[:12]}…")

        page.on("request", lambda r: asyncio.create_task(on_request(r)))

        print(f"[login] opening {DFTOOLS_URL}")
        print("[login] log in normally - that's it. The very first signed "
              "request the page makes is enough (confirmed: a signature "
              "captured from any one of these endpoints works for all the "
              "others too), so this closes itself within a couple seconds "
              "of you logging in.")
        try:
            await page.goto(DFTOOLS_URL, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[login] navigation error: {e}")

        elapsed = 0.0
        poll = 0.5
        while elapsed < LOGIN_TIMEOUT_SECONDS:
            try:
                if persistent:
                    if not ctx.pages:
                        break
                    if page.is_closed():
                        page = ctx.pages[0]
                else:
                    if page.is_closed():
                        break
            except Exception:
                break
            # Any ONE captured endpoint is enough - see
            # core._resolve_credentials for why. Closing as soon as we
            # have it (no post-capture grace period anymore - there's
            # nothing left to wait for) is what makes login finish in
            # seconds instead of needing to visit every section.
            if len(captured) >= 1:
                break
            await asyncio.sleep(poll)
            elapsed += poll

        try:
            if persistent:
                await ctx.close()
            else:
                await browser.close()
        except Exception:
            pass

    if captured:
        CREDS_FILE.write_text(json.dumps(captured, indent=2))
        print(f"[login] wrote {CREDS_FILE} ({len(captured)} endpoint(s) "
              f"captured directly)")

        # Purely informational now, not a problem to fix: any one
        # captured endpoint's signature works for all five (see
        # core._resolve_credentials), so "missing" here just means those
        # particular ones will borrow another endpoint's credentials at
        # request time instead of using their own - nothing to act on.
        captured_short = {_short_key(k) for k in captured.keys()} - {None}
        expected = set(TARGET_ENDPOINTS.values())
        missing = expected - captured_short
        if missing:
            print(f"[login] {', '.join(sorted(missing))} weren't captured "
                  "directly, but that's fine - they'll use one of the "
                  "endpoints above instead.")
    else:
        print("[login] nothing captured — did you log in?")

    return captured


def run(headless: bool = False, persistent: bool = False) -> dict:
    return asyncio.run(_run(headless=headless, persistent=persistent))


def load() -> dict:
    if not CREDS_FILE.exists():
        return {}
    try:
        return json.loads(CREDS_FILE.read_text())
    except Exception:
        return {}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Log in to Delta Force DfTools via embedded browser.")
    parser.add_argument("--persistent", action="store_true",
                        help="Keep cookies between runs (no re-login).")
    parser.add_argument("--headless", action="store_true",
                        help="Run headless (only useful with --persistent).")
    args = parser.parse_args()
    run(headless=args.headless, persistent=args.persistent)