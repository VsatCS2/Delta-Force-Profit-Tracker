"""
Delta Force Tracker - your deployment settings.

The settings specific to YOUR setup live here, and nowhere else, so
replacing any other .py file with a newer version can never overwrite
them. Edit this file once; leave it alone otherwise.

Web addresses may be written with or without the leading "https://" and
with or without a trailing slash - the app tidies it up.
"""

# Your community leaderboard server (the Railway domain). Leave "" to run
# as a purely local tracker with no leaderboard.
SERVER_URL = "https://dfstatscl-production.up.railway.app"

# Where the app looks for the latest version, e.g. the raw URL of the
# version.json in your GitHub repo:
#   https://raw.githubusercontent.com/YOUR-USERNAME/YOUR-REPO/main/version.json
# Leave "" to turn update checks off (or to fall back on SERVER_URL + "/version").
UPDATE_INFO_URL = "https://raw.githubusercontent.com/VsatCS2/Delta-Force-Profit-Tracker/refs/heads/main/version.json"

# Optional: a deltaforceapi.com gateway key (looks like "sk_live_...").
# Powers looking up OTHER players (teammates/opponents) by name - a
# third-party, opt-in data source, kept separate from everything the
# official API already powers. Leave "" to turn this off entirely; every
# function in delta_force_thirdparty.py fails soft with it unset.
DELTAFORCEAPI_KEY = "sk_live_6ebd98aa06547110c198b4e1539f527d30489aa90096d3a5323049a4cd3447fd"
