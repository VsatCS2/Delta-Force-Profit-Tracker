#!/usr/bin/env python3
"""
Delta Force Profit Tracker — command line

    pip install requests
    python delta_force_cli.py                # fetch + report
    python delta_force_cli.py --login         # open browser to log in, capture creds
    python delta_force_cli.py --quick         # only recent pages (fast)
    python delta_force_cli.py --export        # also write CSVs
    python delta_force_cli.py --refresh-all   # ignore cache, re-pull everything
    python delta_force_cli.py --debug         # verbose request/response logging
    python delta_force_cli.py --no-fetch      # just report on cached data

For a GUI instead, run delta_force_gui.py.
"""

import argparse
import sys
import traceback

import delta_force_core as core
from delta_force_version import APP_VERSION


def cli_progress(msg: str):
    pad = " " * 20
    print(f"\r{msg}{pad}", end="", flush=True)


def cli_debug(msg: str):
    print(f"\n    [debug] {msg}", flush=True)


def print_table(title: str, summary: list, kind: str, limit: int = None):
    print(f"\n{title}")
    print("-" * len(title))
    rows_to_show = summary[:limit] if limit else summary
    if not rows_to_show:
        print("(no data)")
        return
    header = f"{'Period':<18} {'Matches':>8} {'W-L':>8} {'Net Income':>18}"
    print(header)
    print("-" * len(header))
    for row in rows_to_show:
        wl = f"{row['wins']}-{row['losses']}"
        label = core.period_label(row["period"], kind)
        print(f"{label:<18} {row['matches']:>8} {wl:>8} {core.fmt_money(row['net_income']):>18}")


def print_overview(rows: list):
    ov = core.overview(rows)
    print("\n\n=== Overview ===")
    print(f"Today:      {core.fmt_money(ov['today'])}")
    print(f"This week:  {core.fmt_money(ov['week'])}")
    print(f"This month: {core.fmt_money(ov['month'])}")
    print(f"All-time:   {core.fmt_money(ov['all_time'])}  ({ov['matches']} matches, "
          f"{ov['wins']}W-{ov['losses']}L, {ov['win_rate']:.1f}% win rate)")


def print_maps(rows: list, limit: int = 10):
    groups = {}
    for r in rows:
        g = groups.setdefault(r["map_name"], {"matches": 0, "wins": 0,
                                               "losses": 0, "net_income": 0})
        g["matches"] += 1
        g["net_income"] += r["net_income"]
        if r["result"] == "win":
            g["wins"] += 1
        elif r["result"] == "loss":
            g["losses"] += 1

    print("\nTop Maps by Net Income")
    print("----------------------")
    if not groups:
        print("(no data)")
        return
    header = f"{'Map':<32} {'Matches':>8} {'W-L':>8} {'Net Income':>18}"
    print(header)
    print("-" * len(header))
    for name, g in sorted(groups.items(), key=lambda kv: -kv[1]["net_income"])[:limit]:
        print(f"{name:<32} {g['matches']:>8} {g['wins']}-{g['losses']:>6} "
              f"{core.fmt_money(g['net_income']):>18}")


def main():
    parser = argparse.ArgumentParser(description="Delta Force profit tracker (CLI)")
    parser.add_argument("--version", action="version",
                        version=f"Delta Force Profit Tracker v{APP_VERSION}")
    parser.add_argument("--refresh-all", action="store_true")
    parser.add_argument("--quick", action="store_true",
                        help="Only pull the most recent pages (fast today-check)")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose request/response logging")
    parser.add_argument("--login", action="store_true",
                        help="Open a browser to log in and capture fresh credentials.")
    parser.add_argument("--login-persistent", action="store_true",
                        help="Like --login but keep cookies between runs.")
    args = parser.parse_args()

    core.set_debug(args.debug)

    if args.login or args.login_persistent:
        try:
            import delta_force_login
        except ImportError:
            sys.exit(
                "delta_force_login.py is missing or Playwright isn't installed.\n"
                "Install with:\n"
                "    pip install playwright\n"
                "    playwright install chromium"
            )
        delta_force_login.run(
            headless=False,
            persistent=args.login_persistent,
        )
        # force=True: reload even if the OS hasn't updated the mtime yet.
        core.refresh_credentials_from_browser(force=True)
        print(f"Credentials captured. Source: {core.credential_source()}")
        print("Run again without --login to fetch data.")
        return

    if args.no_fetch:
        cache = core.load_cache()
        if not cache["matches"]:
            sys.exit("No cached data yet — run without --no-fetch first.")
    elif args.quick:
        cache = core.refresh_today(
            on_progress=cli_progress,
            on_debug=cli_debug if args.debug else None,
        )
        print()
    else:
        cache = core.pull_matches(
            refresh_all=args.refresh_all,
            on_progress=cli_progress,
            on_debug=cli_debug if args.debug else None,
        )
        print()

    rows = core.build_rows(cache)
    daily = core.summarize(rows, "date")
    weekly = core.summarize(rows, "week")
    monthly = core.summarize(rows, "month")

    print_overview(rows)
    print_table("Last 14 Days", daily, "date", limit=14)
    print_table("Last 8 Weeks", weekly, "week", limit=8)
    print_table("Last 12 Months", monthly, "month", limit=12)
    print_maps(rows, limit=10)

    print(f"\nCredentials: {core.credential_source()}")

    if args.export:
        paths = core.export_csv(rows, {"daily": daily, "weekly": weekly, "monthly": monthly})
        print(f"\nExported: {', '.join(p.name for p in paths)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted by user.")
    except core.NotLoggedInError as e:
        print(f"\n\n{e}", flush=True)
        sys.exit(2)
    except Exception as e:
        print(f"\n\n[error] {e}", flush=True)
        if "--debug" in sys.argv:
            traceback.print_exc()
        sys.exit(1)