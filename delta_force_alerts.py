"""
Delta Force Tracker - price alerts: the logic and the persistence, with
no UI in it.

Kept apart from delta_force_gui.py on purpose. The question this module
answers - "given this item's new price, should the person be notified?" -
is the one place a bug costs real value in one of two directions: notify
too eagerly and the alert becomes noise that gets ignored (or switched
off), notify too rarely and someone misses the exact deal they set the
alert up for. Pure functions over plain dicts are testable exhaustively
in a way logic buried in widget callbacks never is.

An alert is a plain dict:
    {
      "item_id":      deltaforceapi item id,
      "name":         display name,
      "target":       int, notify when the price is at or below this,
      "last_price":   int or None, the most recent price actually seen,
      "price_as_of":  the API's own timestamp for that price ("" if none),
      "last_checked": local ISO timestamp of the last successful check,
      "triggered":    bool, True while the price is at/below target and
                      the person has already been told about it,
    }
"""

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from delta_force_paths import APP_DATA_DIR

ALERTS_FILE = APP_DATA_DIR / "price_alerts.json"

# Each tracked item costs one request per poll against a third-party
# service nobody here has a relationship with - a cap keeps that
# courteous without needing the person to think about it.
MAX_ALERTS = 20

_MAX_PRICE = 10 ** 12
_SUFFIXES = {"": 1, "k": 10 ** 3, "m": 10 ** 6, "b": 10 ** 9}


def parse_price(text):
    """'6,945,730' -> 6945730, '6.9m' -> 6900000, '750k' -> 750000.

    Prices in this game are routinely in the millions, so typing out
    seven or eight digits for every alert is the kind of friction that
    makes a feature feel worse than it is. Returns None for anything
    that isn't a clear positive amount (blank, zero, negative, garbage) -
    callers treat None as "ask again", never as a price."""
    if text is None:
        return None
    cleaned = str(text).strip().lower().replace("_", "").replace(" ", "")
    if "," in cleaned:
        # Commas are only accepted as real thousands grouping
        # (6,945,730). Stripping them blindly would read a typo like
        # "1,2,3m" as 123,000,000 - and a silently wrong target is worse
        # for an alert than being asked to retype it.
        if not re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?[kmb]?", cleaned):
            return None
        cleaned = cleaned.replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmb]?)", cleaned)
    if not match:
        return None
    try:
        value = int(Decimal(match.group(1)) * _SUFFIXES[match.group(2)])
    except (InvalidOperation, ValueError):
        return None
    if value <= 0 or value > _MAX_PRICE:
        return None
    return value


def evaluate(alert: dict, price_info, now=None):
    """Applies a fresh price to an alert. Returns (updated_alert, fired).

    fired is True exactly once per drop: the moment the price reaches the
    target. While it stays at or below the target the alert stays
    "triggered" and does not fire again - otherwise the same deal would
    re-notify on every poll for as long as it lasted. Once the price goes
    back above the target the alert re-arms, so the next drop notifies
    again; a price that bounces around the target notifies on each fresh
    drop, which is what "tell me when it drops to X" actually means.

    price_info None (a failed or empty lookup) changes nothing at all: it
    must neither fire nor re-arm, or one flaky request in the middle of a
    good deal would reset it and re-notify on the next success.
    """
    updated = dict(alert)
    if not price_info or not price_info.get("price"):
        return updated, False

    price = int(price_info["price"])
    updated["last_price"] = price
    updated["price_as_of"] = price_info.get("as_of", "") or ""
    updated["last_checked"] = (now or datetime.now()).isoformat(timespec="seconds")

    if price <= int(updated["target"]):
        if updated.get("triggered"):
            return updated, False
        updated["triggered"] = True
        return updated, True

    updated["triggered"] = False
    return updated, False


def add_alert(alerts: list, item_id: str, name: str, target: int,
              price_info=None):
    """Returns (new_alerts, status) with status 'added', 'updated', or
    'full'. Tracking an item that's already on the list updates its target
    rather than adding a second entry - two alerts on one item would just
    notify twice for the same price. Changing the target also re-arms it,
    since the old 'already told you' no longer applies to a new number."""
    alerts = [dict(a) for a in alerts]
    for a in alerts:
        if a["item_id"] == item_id:
            a["target"] = int(target)
            a["name"] = name or a["name"]
            a["triggered"] = False
            return alerts, "updated"
    if len(alerts) >= MAX_ALERTS:
        return alerts, "full"
    alerts.append({
        "item_id": item_id,
        "name": name,
        "target": int(target),
        "last_price": (price_info or {}).get("price"),
        "price_as_of": (price_info or {}).get("as_of", "") or "",
        "last_checked": "",
        "triggered": False,
    })
    return alerts, "added"


def remove_alert(alerts: list, item_id: str) -> list:
    return [dict(a) for a in alerts if a["item_id"] != item_id]


def age_text(iso_utc: str) -> str:
    """'2026-10-03T06:03:57Z' -> 'just now' / '5m ago' / '3h ago' /
    '2d ago', or '' if there's nothing usable to show.

    Not core.format_relative_time: that one compares naive local
    timestamps, and the price timestamps from the API are UTC with a
    trailing 'Z' - feeding one into the other would either misreport the
    age by the person's whole UTC offset or raise on mixing aware and
    naive datetimes. This parses it as the UTC it is."""
    if not iso_utc:
        return ""
    try:
        then = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _clean(entry):
    """One loaded entry -> a safe alert dict, or None if it's unusable."""
    if not isinstance(entry, dict):
        return None
    item_id, name = entry.get("item_id"), entry.get("name")
    if not isinstance(item_id, str) or not item_id or not isinstance(name, str):
        return None
    try:
        target = int(entry.get("target"))
    except (TypeError, ValueError):
        return None
    if target <= 0:
        return None
    last_price = entry.get("last_price")
    try:
        last_price = int(last_price) if last_price is not None else None
    except (TypeError, ValueError):
        last_price = None
    return {
        "item_id": item_id,
        "name": name,
        "target": target,
        "last_price": last_price,
        "price_as_of": str(entry.get("price_as_of") or ""),
        "last_checked": str(entry.get("last_checked") or ""),
        "triggered": bool(entry.get("triggered")),
    }


def load_alerts(path=None) -> list:
    """Never raises: a missing, unreadable, or corrupted file is just an
    empty list, and individual bad entries are dropped rather than
    failing the whole load - losing one malformed alert is better than
    the feature refusing to start."""
    try:
        with open(path or ALERTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    alerts, seen = [], set()
    for entry in data:
        cleaned = _clean(entry)
        if cleaned and cleaned["item_id"] not in seen:
            seen.add(cleaned["item_id"])
            alerts.append(cleaned)
    return alerts[:MAX_ALERTS]


def save_alerts(alerts: list, path=None) -> bool:
    """Writes atomically (temp file, then replace) so a crash or power
    loss mid-write can't leave a half-written file that loses every
    alert. Returns False rather than raising on failure."""
    target_path = str(path or ALERTS_FILE)
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=os.path.dirname(target_path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(alerts, f, indent=2)
        os.replace(tmp_name, target_path)
        return True
    except Exception:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
        return False
