# Delta Force Profit Tracker v2.0.0

A big one: DF Tracker accounts, seven themes, an Operators page, and a proper Windows installer.

## What's new

**Sign in with Discord**
Player Lookup and the Search Overlay now use a free DF Tracker account: **Settings → Account → Sign in with Discord**. Each account gets a daily lookup allowance, shown on the same page, that resets at midnight UTC. Everything else (Overview, Profile, Maps, Operators, Matches, Community, Price Alerts and the desktop overlay) still works without an account.

**Seven themes**
Midnight, Obsidian, Ember, Glacier, Forest, Dusk and Daylight. Pick one with the colored dots at the top of **Settings → Appearance**. Each theme comes with its own banner artwork, or you can choose your own picture, and switch back any time with **Use theme artwork**.

**Operators page**
A new tab showing what each operator has earned you, in credits. Click a column to sort, and double-click an operator to see its matches.

**Player Lookup runs through DF Tracker's server**
Lookups no longer go straight from your PC to a third party. They go through DF Tracker's own server, which is what lets each account have a fair daily allowance and keeps that service's key off your computer.

**A proper Windows installer**
New downloads come with `DeltaForceTracker-Setup-2.0.0.exe`: it installs for your user (no administrator prompt), adds a Start menu entry and an optional desktop shortcut, and can be removed under **Settings → Apps**. Your settings and match history are stored separately from the program, so installing or uninstalling never touches them.

**Scrolling fix**
Mouse-wheel scrolling now goes to the list under your cursor, and passes through to the page when that list has nothing to scroll.

## Which download do I need?

- **New to DF Tracker, or coming from 1.4.0 or older:** `DeltaForceTracker-Setup-2.0.0.exe`.
- **On 1.5.0:** open **Settings → Check for Updates** and let the app install 2.0.0 for you.
- **`DeltaForceTracker.zip`** is the same program as a plain folder (this is also what the in-app updater downloads). Extract the **whole** folder, not just the `.exe`, and run `DeltaForceTracker.exe`.

Coming from an older version by hand? Your settings and match history carry over automatically. After installing, you can delete the old copy of the program.

<!-- KEEP the next section only while the installer is NOT code-signed. Delete it once it is. -->
## If Windows warns you

DF Tracker is a small independent app, so Windows may show "Windows protected your PC" (SmartScreen). Choose **More info → Run anyway**. Windows 11's **Smart App Control**, if it's switched on, can't allow individual apps and may block the installer; the checksum file next to the download lets you confirm the file is the one published here.
<!-- END of the unsigned-only section -->

## About your account and your data

A DF Tracker account stores what Discord provides when you sign in (your Discord ID, email address, display name and avatar), your plan, how many lookups you've used each day, and any admin notes about the account, plus timestamps. It exists to run Player Lookup and its daily allowance. Full details, and how to delete your account, are on the [privacy page](https://vsatcs2.github.io/Delta-Force-Profit-Tracker/privacy.html).

## About the third-party data

- **Player Lookup and the Search Overlay** now get their data from a third-party game-data API (served through RapidAPI), reached through DF Tracker's own server. What gets sent is the name you search, together with your DF Tracker account so your daily lookups can be counted.
- **Extended Stats and Price Alerts** still use [deltaforceapi.com](https://deltaforceapi.com), an independent third-party service that isn't affiliated with the game's publisher. Extended Stats sends your in-game name when the app starts; Price Alerts send the item names you search for and the items you track. Prices are snapshots, so they can trail the live auction house slightly; each alert shows how fresh its last price is.
- Everything else (your matches, profile, and rank) still comes from the game's own web API.

DF Tracker is an independent, fan-made project and isn't affiliated with the game's publisher.
