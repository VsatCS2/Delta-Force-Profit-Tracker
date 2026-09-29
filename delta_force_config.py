"""
Delta Force Tracker - your deployment settings.

The two web addresses that are specific to YOUR setup live here, and
nowhere else, so replacing any other .py file with a newer version can
never overwrite them. Edit this file once; leave it alone otherwise.

Either value may be written with or without the leading "https://" and
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
