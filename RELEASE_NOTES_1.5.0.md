# Delta Force Profit Tracker v1.5.0

## What's new

**Price Alerts**
Pick an auction-house item and the price you'd buy it at. When its price drops that low you get a pop-up and a sound. Prices are checked every minute, and it keeps working while the app is minimized or in the tray. Track up to 20 items. Type targets as `5000000`, `5,000,000`, or shorthand like `5m` and `750k`.

**Player Lookup**
Search any player by name for their combat stats and stash value. Handy for checking a teammate or the player who just killed you.

**Search Overlay**
The same lookup, available in-game as a small overlay with its own shortcut (`Ctrl+Shift+O` by default, changeable in Settings). Turn it on under Settings → Search Overlay.

**Extended Stats**
Your Profile tab now shows bullets fired, hit and missed, kills and deaths by tier, knocks, category scores, and a stash value breakdown, below the numbers the game already shows.

**Profile tab layout**
The Profile tab now uses the full window width (three cards per row), so there's less scrolling on wide screens.

**Auto-updater**
The updater is more reliable at replacing the app and reopening it, and keeps a log in your temp folder if an install ever fails.

## About the third-party data

Player Lookup, the Search Overlay, Extended Stats and Price Alerts get their data from [deltaforceapi.com](https://deltaforceapi.com), an independent third-party service that isn't affiliated with the game's publisher. Everything else (your matches, profile, and rank) still comes from the game's own web API.

What gets sent to that service:
- **Extended Stats** sends your in-game name when the app starts.
- **Player Lookup and the Search Overlay** send whatever name you search.
- **Price Alerts** send the item names you search for and the items you track.

This service queues players it hasn't analyzed before, so a player nobody has looked up yet can take a few minutes to show stats. Prices are snapshots, so they can trail the live auction house slightly; each alert shows how fresh its last price is.

## Updating

Already on 1.3.0 or later? Go to **Settings → Check for Updates** and the app can download and install it for you. Otherwise, download `DeltaForceTracker.zip` below, extract it, and replace your old copy.
