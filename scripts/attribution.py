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
    if not rows:
        print("no users yet")
        return 0

    records = [
        {
            "source_channel": r["source_channel"],
            "users": r["users"],
            "active": r["active"],
            "served": r["served"],
            "answered": r["answered"],
            "answered_pct": (
                round(100 * r["answered"] / r["users"]) if r["users"] else 0
            ),
        }
        for r in rows
    ]

    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
        return 0

    labels = [r["source_channel"] for r in records] + ["channel"]
    width = max(len(label) for label in labels)
    header = (
        f"{'channel':<{width}}  {'users':>6} {'active':>7} {'served':>7} "
        f"{'answered':>9} {'ans%':>5}"
    )
    print(header)
    print("-" * len(header))
    for r in records:
        print(
            f"{r['source_channel']:<{width}}  {r['users']:>6} {r['active']:>7} "
            f"{r['served']:>7} {r['answered']:>9} {r['answered_pct']:>4}%"
        )

    columns = ("users", "active", "served", "answered")
    totals = {k: sum(r[k] for r in records) for k in columns}
    print("-" * len(header))
    print(
        f"{'TOTAL':<{width}}  {totals['users']:>6} {totals['active']:>7} "
        f"{totals['served']:>7} {totals['answered']:>9}"
    )
    print(
        "\nusers    = distinct people first seen on this channel"
        " (first touch, frozen)\n"
        "active   = have not stopped and have not blocked the bot\n"
        "served   = were sent at least one question\n"
        "answered = tapped an option at least once"
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
