#!/usr/bin/env python3
"""Attribution tooling: mint tracked links, read the funnel back out.

    python scripts/attribution.py link reddit-r-medicine
    python scripts/attribution.py link whatsapp-batch-2024 --landing
    python scripts/attribution.py report
    python scripts/attribution.py report --since 2026-01-01 --csv

`link` prints a Telegram deep link (and optionally the landing-page URL that
forwards to it). Whatever code you pass is normalised the same way the bot
normalises the inbound payload, so the printed link is exactly what will show
up in the report.
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

    rows = db.attribution_summary(conn, since=args.since)
    opens = db.attribution_opens(conn)
    if not rows:
        print("no subscribers yet")
        return 0

    records = []
    for row in rows:
        signups = row["signups"] or 0
        opened = opens.get(row["source"], 0)
        engaged = row["engaged"] or 0
        records.append(
            {
                "source": row["source"],
                "opens": opened,
                "signups": signups,
                "active": row["active"] or 0,
                "engaged": engaged,
                "engaged_pct": round(100 * engaged / signups) if signups else 0,
            }
        )

    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
        return 0

    width = max(len(r["source"]) for r in records + [{"source": "source"}])
    print(
        f"{'source':<{width}}  {'opens':>6} {'signups':>8} {'active':>7} "
        f"{'engaged':>8} {'eng%':>5}"
    )
    print("-" * (width + 38))
    for r in records:
        print(
            f"{r['source']:<{width}}  {r['opens']:>6} {r['signups']:>8} "
            f"{r['active']:>7} {r['engaged']:>8} {r['engaged_pct']:>4}%"
        )

    totals = {k: sum(r[k] for r in records) for k in ("opens", "signups", "active", "engaged")}
    print("-" * (width + 38))
    print(
        f"{'TOTAL':<{width}}  {totals['opens']:>6} {totals['signups']:>8} "
        f"{totals['active']:>7} {totals['engaged']:>8}"
    )
    print(
        "\nopens   = /start presses carrying this code (includes returning users)\n"
        "signups = distinct subscribers first seen on this code\n"
        "engaged = subscribers who answered at least one question"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    link = sub.add_parser("link", help="print a tracked link for a source code")
    link.add_argument("source", help="campaign / channel name, e.g. reddit-r-medicine")
    link.add_argument(
        "--landing", action="store_true", help="also print the landing-page URL"
    )
    link.set_defaults(func=cmd_link)

    report = sub.add_parser("report", help="per-source funnel")
    report.add_argument("--since", help="ISO date, e.g. 2026-01-01")
    report.add_argument("--csv", action="store_true", help="emit CSV instead of a table")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    config.load_env()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
