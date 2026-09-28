"""
Delta Force Tracker - disclaimer text and bundled legal documents.

One place for the wording shown in the first-run disclaimer dialog and the
Settings > About & Legal section, so the two can never drift apart, plus a
loader for the LICENSE and THIRD_PARTY_NOTICES.txt files that ship inside
the build (see delta_force.spec's `datas` list).

DISCLAIMER.md (in the repo / release zip) is the fuller written version of
the same statements - keep them consistent when editing either.
"""

from delta_force_paths import resource_path

DISCLAIMER_TITLE = "Before you continue"

DISCLAIMER_PARAGRAPHS = [
    "DF Tracker is an unofficial, fan-made tool. It is not affiliated with, "
    "endorsed by, or sponsored by Garena, Tencent, TiMi Studio Group, or any "
    "other developer or publisher of Delta Force. \"Delta Force\" and related "
    "names, logos, and game content belong to their respective owners.",

    "It reads your own account's stats from the same web service that Delta "
    "Force's official HQ web page uses, through a login session you create "
    "yourself in a browser window. Your session details stay on your "
    "computer. It does not modify the game or give any in-game advantage.",

    "The game's terms may restrict third-party tools, and the service this "
    "relies on can change or block it at any time. You use DF Tracker "
    "entirely at your own risk, including any risk to your account.",

    "The community leaderboard is optional and opt-in. If you join, your "
    "display name, summary stats, and an account identifier (stored by the "
    "server only as a one-way hash) are sent to a server run by this tool's "
    "author. You can leave and delete that data any time.",

    "Provided \"as is\", without warranty of any kind. Full terms are in "
    "Settings > About & Legal.",
]

ABOUT_SHORT = (
    "Not affiliated with, endorsed by, or sponsored by Garena, Tencent, TiMi "
    "Studio Group, or any other developer or publisher of Delta Force. Game "
    "names and marks belong to their owners. Use at your own risk; provided "
    "\"as is\" without warranty. Released under the MIT License."
)


def read_bundled_text(filename: str) -> str:
    """Contents of a bundled legal file, or a plain explanation if it
    can't be read - never raises, since this only feeds a read-only viewer
    window and shouldn't be able to break the Settings page."""
    try:
        return resource_path(filename).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (f"{filename} couldn't be loaded from this build.\n\n"
                "If you downloaded this app from an official release, the "
                "same file is included alongside it in the download.")
