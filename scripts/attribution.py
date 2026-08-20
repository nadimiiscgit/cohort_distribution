#!/usr/bin/env python3
"""Attribution tooling: mint tracked links, read the funnel back out.

    python scripts/attribution.py link tg_group1
    python scripts/attribution.py link ig_drxyz --landing
    python scripts/attribution.py report
    python scripts/attribution.py report --since 2026-08-01 --csv

`link` prints a Telegram deep link (and optionally the landing-page URL that
forwards to it). Whatever code you pass is normalised the same way the bot
normalises the inbound ?start= payload, so the printed link is exactly what
shows up in the report.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import csv
import sys

from bot import attribution, config, db


def cmd_link(args: argparse.Namespace) -> int:
    code = attribution.normalize_source(args.source)
    if code != args.source:
        print(f"# normalised {args.source!r} -> {code!r}", file=sys.stderr)
    print(attribution.deep_link(code))
    if args.landing:
        print(attribution.landing_link(code))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn = db.connect()
    db.init_schema(conn)

    rows = db.source_funnel(conn, since=args.since)
    clicks = db.clicks_by_channel(conn, since=args.since)
    if not rows and not clicks:
        print("no users or clicks yet")
        return 0

    # A channel with clicks and no users is the most useful row in the table —
    # it is the one that is not working — but it has no users row to be found
    # by, so the click channels are unioned in rather than left out.
    by_channel = {r["source_channel"]: r for r in rows}
    channels = list(by_channel) + sorted(c for c in clicks if c not in by_channel)

    records = []
    for channel in channels:
        r = by_channel.get(channel)
        users = r["users"] if r else 0
        clicked = clicks.get(channel, 0)
        answered = r["answered"] if r else 0
        records.append(
            {
                "source_channel": channel,
                "clicks": clicked,
                "users": users,
                "active": r["active"] if r else 0,
                "served": r["served"] if r else 0,
                "answered": answered,
                # None rather than 0 when no clicks have been imported: an
                # unmeasured channel has an unknown conversion rate, not a zero
                # one, and 0% reads as "this link failed".
                "signup_pct": round(100 * users / clicked) if clicked else None,
                "answered_pct": round(100 * answered / users) if users else 0,
            }
        )

    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
        return 0

    labels = [r["source_channel"] for r in records] + ["channel"]
    width = max(len(label) for label in labels)
    header = (
        f"{'channel':<{width}}  {'clicks':>7} {'users':>6} {'active':>7} "
        f"{'served':>7} {'answered':>9} {'join%':>6} {'ans%':>5}"
    )
    print(header)
    print("-" * len(header))
    for r in records:
        join = "-" if r["signup_pct"] is None else f"{r['signup_pct']}%"
        print(
            f"{r['source_channel']:<{width}}  {r['clicks']:>7} {r['users']:>6} "
            f"{r['active']:>7} {r['served']:>7} {r['answered']:>9} "
            f"{join:>6} {r['answered_pct']:>4}%"
        )

    columns = ("clicks", "users", "active", "served", "answered")
    totals = {k: sum(r[k] for r in records) for k in columns}
    total_join = (
        f"{round(100 * totals['users'] / totals['clicks'])}%"
        if totals["clicks"]
        else "-"
    )
    print("-" * len(header))
    print(
        f"{'TOTAL':<{width}}  {totals['clicks']:>7} {totals['users']:>6} "
        f"{totals['active']:>7} {totals['served']:>7} {totals['answered']:>9} "
        f"{total_join:>6}"
    )
    print(
        "\nclicks   = landing-page hits on this tag, preview fetchers excluded\n"
        "users    = distinct people first seen on this channel"
        " (first touch, frozen)\n"
        "active   = have not stopped and have not blocked the bot\n"
        "served   = were sent at least one question\n"
        "answered = tapped an option at least once\n"
        "join%    = users per click; '-' means no clicks imported for this tag\n"
        "\nClicks come from the web server log via scripts/import_clicks.py —\n"
        "a channel shows '-' until that has run."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    link = sub.add_parser("link", help="print a tracked link for a source code")
    link.add_argument("source", help="channel tag, e.g. tg_group1 or ig_drxyz")
    link.add_argument(
        "--landing", action="store_true", help="also print the landing-page URL"
    )
    link.set_defaults(func=cmd_link)

    report = sub.add_parser("report", help="per-channel funnel")
    report.add_argument("--since", help="ISO date, e.g. 2026-08-01")
    report.add_argument("--csv", action="store_true", help="emit CSV instead of a table")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    config.load_env()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
