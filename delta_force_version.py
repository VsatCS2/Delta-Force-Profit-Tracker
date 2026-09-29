"""
Delta Force Tracker — version string and changelog.

One place, imported by the GUI (title bar, sidebar footer, "What's New"
popup, update check), the CLI (--version), and anything else that ever
needs to know or display it - support requests are a lot easier to
handle when "what version are you on" has an actual answer.

Bump APP_VERSION by hand when you cut a release, and add a CHANGELOG
entry for it. There's no automated version-bumping here on purpose - a
hobby project with occasional releases doesn't need the machinery for
that, just a human remembering to update two things together.

CHANGELOG entries are shown verbatim in the "What's New" popup (see
delta_force_gui.py's _maybe_show_whats_new) to anyone whose last-seen
version is older than the current one - keep each line short, plain,
and user-facing (not a commit log).
"""

APP_VERSION = "1.3.0"


def version_tuple(v: str):
    """'1.10.2' -> (1, 10, 2), for a real numeric comparison rather than
    a string one (which would wrongly say '1.9.0' > '1.10.0'). Anything
    that doesn't parse cleanly sorts as (0,), i.e. never newer than a
    real version, so a malformed/missing version string can't falsely
    trigger an update or changelog notice."""
    try:
        return tuple(int(part) for part in v.strip().split("."))
    except (ValueError, AttributeError):
        return (0,)


CHANGELOG = {
    "1.3.0": [
        "New: the app can now download and install updates for you — no more manually re-downloading from GitHub",
        "Fixed: the Discord Rich Presence icon showing a profit (green) arrow on a loss day when you hadn't played yet",
        "Fixed: the weekly report endpoint, and Recent High-Value Items now also pulls from your weekly report as a second source",
    ],
    "1.2.0": [
        "New: Today's leaderboard — switch the Community tab to \"Today\" to see how you stack up against other players on today's matches alone; it resets at 00:00 UTC for everyone",
        "Improved: while you're joined and opted in, your leaderboard stats now update automatically after each match refresh (turn this off in the Community tab)",
    ],
    "1.1.0": [
        "New: Session tracking — track a play session separately from calendar days, so late-night sessions don't get split across two days",
        "New: Desktop overlay — a quick-glance HUD for today's or your session's profit, best raid, and top loot, toggled with a global hotkey",
        "New: Weekly Highlights — evacuation rate, K/D ratio, your best and worst squad-mate this week, and a 7-day net worth trend",
        "New: Launch at Windows startup, with an option to start minimized",
        "Improved: first-ever login now fetches your complete match history automatically instead of just the most recent 40 games",
        "Improved: scrollable pages support the mouse wheel, and several layout/legibility fixes",
    ],
}
