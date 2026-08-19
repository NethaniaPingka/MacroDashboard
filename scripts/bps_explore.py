"""Explore the BPS WebAPI: find variables and inspect their shape.

BPS ids are not guessable and its variables are rebased and retired often (the
headline CPI index has moved 2012=100 -> 2018=100 -> 2022=100, each a new
var_id), so this is the tool for working out what to put in the catalog.

    python scripts/bps_explore.py subjects
    python scripts/bps_explore.py search inflasi --subject 3
    python scripts/bps_explore.py profile 2263
"""

from __future__ import annotations

import argparse
import sys

from macrodash.sources.bps import BpsClient


def cmd_subjects(client: BpsClient, args) -> None:
    for sub_id, title in sorted(client.list_subjects().items()):
        print(f"  {sub_id:<5} {title}")


def cmd_search(client: BpsClient, args) -> None:
    variables = client.list_variables(subject=args.subject)
    needle = args.text.lower()
    hits = [v for v in variables if needle in v["title"].lower()]
    print(f"{len(hits)} of {len(variables)} variables match {args.text!r}")

    for v in hits[: args.limit]:
        years = client.list_years(v["var_id"]) if args.years else {}
        span = ""
        if years:
            labels = sorted(int(y) for y in years.values())
            span = f"  [{labels[0]}..{labels[-1]}]"
        print(f"  var={v['var_id']:<6}{span}  {v['title'][:88]}")


def cmd_profile(client: BpsClient, args) -> None:
    meta = client.describe(args.var_id)
    print(f"var {args.var_id}: {meta['title']}")
    print(f"  unit={meta['unit']!r}  decimals={meta['decimal']}  subject={meta['subject']!r}")
    print(f"  last_update={meta['last_update']}")
    print(f"  years: {meta['year_span']}")
    print(f"  period labels ({len(meta['periods'])}): "
          f"{', '.join(f'{k}={v}' for k, v in list(meta['periods'].items())[:14])}")
    print(f"  regions: {meta['n_regions']}  national vervar present: {meta['has_national']}")
    print(f"  turvar options: {meta['turvar']}")
    print(f"  national observations: {meta['n_national_obs']}")
    if meta["sample"]:
        print("  most recent:")
        for date, value in meta["sample"]:
            print(f"    {date}  {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explore the BPS WebAPI.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("subjects", help="list subject categories")

    p_search = sub.add_parser("search", help="find variables by title")
    p_search.add_argument("text")
    p_search.add_argument("--subject", type=int, help="restrict to a subject id")
    p_search.add_argument("--limit", type=int, default=25)
    p_search.add_argument("--years", action="store_true", help="also fetch year coverage (slow)")

    p_profile = sub.add_parser("profile", help="inspect one variable in detail")
    p_profile.add_argument("var_id", type=int)

    args = parser.parse_args(argv)
    client = BpsClient()

    {"subjects": cmd_subjects, "search": cmd_search, "profile": cmd_profile}[args.command](
        client, args
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
