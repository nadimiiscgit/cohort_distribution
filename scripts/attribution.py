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
    clicks = db.attribution_clicks(conn)
    if not rows and not clicks:
        print("no subscribers or clicks yet")
        return 0

    # A code with clicks and no signups is the most useful row in the table —
    # it is the channel that is not working — but it has no subscribers row to
    # be found by, so the click sources are unioned in rather than left out.
    by_source = {row["source"]: row for row in rows}
    sources = list(by_source) + sorted(s for s in clicks if s not in by_source)

    records = []
    for source in sources:
        row = by_source.get(source)
        signups = (row["signups"] or 0) if row else 0
        opened = opens.get(source, 0)
        clicked = clicks.get(source, 0)
        engaged = (row["engaged"] or 0) if row else 0
        records.append(
            {
                "source": source,
                "clicks": clicked,
                "opens": opened,
                "signups": signups,
                "active": (row["active"] or 0) if row else 0,
                "engaged": engaged,
                # None rather than 0 when there are no clicks: a channel nobody
                # has imported logs for has an unknown conversion rate, not a
                # zero one, and printing 0% would read as "this link failed".
                "conv_pct": round(100 * signups / clicked) if clicked else None,
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
        f"{'source':<{width}}  {'clicks':>7} {'opens':>6} {'signups':>8} "
        f"{'active':>7} {'engaged':>8} {'conv%':>6} {'eng%':>5}"
    )
    print("-" * (width + 55))
    for r in records:
        conv = "-" if r["conv_pct"] is None else f"{r['conv_pct']}%"
        print(
            f"{r['source']:<{width}}  {r['clicks']:>7} {r['opens']:>6} "
            f"{r['signups']:>8} {r['active']:>7} {r['engaged']:>8} "
            f"{conv:>6} {r['engaged_pct']:>4}%"
        )

    totals = {
        k: sum(r[k] for r in records)
        for k in ("clicks", "opens", "signups", "active", "engaged")
    }
    total_conv = (
        f"{round(100 * totals['signups'] / totals['clicks'])}%"
        if totals["clicks"]
        else "-"
    )
    print("-" * (width + 55))
    print(
        f"{'TOTAL':<{width}}  {totals['clicks']:>7} {totals['opens']:>6} "
        f"{totals['signups']:>8} {totals['active']:>7} {totals['engaged']:>8} "
        f"{total_conv:>6}"
    )
    print(
        "\nclicks  = landing-page hits on this code, preview fetchers excluded\n"
        "opens   = /start presses carrying this code (includes returning users)\n"
        "signups = distinct subscribers first seen on this code\n"
        "engaged = subscribers who answered at least one question\n"
        "conv%   = signups per click; '-' means no clicks imported for this code\n"
        "\nclicks and opens are lifetime totals and ignore --since; only the\n"
        "subscriber columns are filtered by it. Clicks come from the web server\n"
        "log via scripts/import_clicks.py — a code shows '-' until that runs."
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
