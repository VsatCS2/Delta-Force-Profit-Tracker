"""Which features a user gets, by tier. All data-driven: the `features` table
(editable from the admin page) decides, so moving a feature between tiers
never needs an app update."""
from datetime import datetime, timezone

TIERS = ("free", "pro")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

# Used to seed an empty database, and as a safe fallback if the table is empty.
DEFAULT_FEATURES = [
    dict(key="player_lookup", label="Player lookup",
         description="Look up any player by name (search, overlay and page). "
                     "Limits count paid upstream requests; repeat lookups are cached and free.",
         min_tier="free", enabled=True, free_limit=60, pro_limit=600, sort=10),
    dict(key="price_alerts", label="Price alerts",
         description="Notify me when an item's auction price drops to my target.",
         min_tier="free", enabled=True, free_limit=None, pro_limit=None, sort=20),
    dict(key="community", label="Community leaderboard",
         description="Join and browse the community leaderboard.",
         min_tier="free", enabled=True, free_limit=None, pro_limit=None, sort=30),
]


def parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def effective_tier(profile, now=None):
    """The tier a user actually has right now. A paid tier with an expiry date
    drops back to free by itself once that date passes (trials, giveaways,
    and - later - lapsed subscriptions)."""
    now = now or datetime.now(timezone.utc)
    tier = profile.get("tier") or "free"
    if tier not in TIER_RANK:
        tier = "free"
    expiry = parse_ts(profile.get("tier_expires_at"))
    if tier != "free" and expiry is not None and expiry <= now:
        return "free"
    if profile.get("role") == "admin":
        return TIERS[-1]                         # admins can use everything
    return tier


def resolve(profile, features, now=None):
    """-> {"tier": ..., "features": {key: {enabled, limit, min_tier, label}}}"""
    tier = effective_tier(profile, now)
    out = {}
    for f in (features or DEFAULT_FEATURES):
        min_tier = f.get("min_tier") if f.get("min_tier") in TIER_RANK else "free"
        allowed = bool(f.get("enabled", True)) and TIER_RANK[tier] >= TIER_RANK[min_tier]
        limit = f.get("pro_limit") if TIER_RANK[tier] >= TIER_RANK["pro"] else f.get("free_limit")
        out[f["key"]] = dict(label=f.get("label") or f["key"], enabled=allowed,
                             limit=limit, min_tier=min_tier,
                             globally_disabled=not bool(f.get("enabled", True)))
    return {"tier": tier, "features": out}
