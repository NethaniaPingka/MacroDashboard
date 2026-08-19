"""Refresh the store from the command line.

This is what Task Scheduler runs, and what you run after editing the catalog.

    python scripts/refresh_all.py                    # everything
    python scripts/refresh_all.py --source yahoo     # one source
    python scripts/refresh_all.py --country ID       # one country
    python scripts/refresh_all.py --dry-run          # fetch, report, write nothing
    python scripts/refresh_all.py --series US.CPI.HEADLINE.IDX
    python scripts/refresh_all.py --include-manual   # replay data/manual/, inactive included
"""

from __future__ import annotations

import argparse
import sys

from macrodash.catalog import load_catalog
from macrodash.refresh import refresh
from macrodash.store import connect, coverage

STATUS_MARK = {"ok": "+", "no_new": "=", "error": "!", "skipped": "-"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the MacroDashboard store.")
    parser.add_argument("--source", help="only this source (fred, yahoo, seed, ...)")
    parser.add_argument("--country", help="only this country code (ID, US, EA, ...)")
    parser.add_argument("--series", nargs="+", help="only these series ids")
    parser.add_argument("--start", help="earliest observation date to request (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    parser.add_argument("--reset", action="store_true",
                        help="wipe stored observations for the selected series first; "
                             "use when a source_id has changed")
    parser.add_argument("--include-seed", action="store_true",
                        help="also load data/seed/, including series marked inactive")
    parser.add_argument("--include-manual", action="store_true",
                        help="also load data/manual/, including series marked inactive")
    parser.add_argument("--quiet", action="store_true", help="summary line only")
    args = parser.parse_args(argv)

    catalog = load_catalog()

    def emit(index: int, total: int, result) -> None:
        if args.quiet:
            return
        mark = STATUS_MARK.get(result.status, "?")
        detail = result.message if result.status == "error" else (
            f"{result.rows_added} new, {result.rows_updated} revised"
            f"{f', through {result.last_obs_date.date()}' if result.last_obs_date is not None else ''}"
        )
        print(f"  [{index:>3}/{total}] {mark} {result.series_id:<28} {detail}")

    report = refresh(
        series_ids=args.series,
        source=args.source,
        country=args.country,
        catalog=catalog,
        start=args.start,
        dry_run=args.dry_run,
        reset=args.reset,
        progress=emit,
    )

    # File-backed sources read from the repo rather than from a vendor, and they
    # are typically active: false until their data lands — which makes them
    # invisible to the normal selection at exactly the moment you want to load
    # them. ID.POLICY.RATE is the case in point: it is inactive *because* it is
    # empty, so a rebuild that respected `active` could never fill it.
    file_backed: list[str] = []
    if args.include_seed:
        file_backed.append("seed")
    if args.include_manual:
        file_backed += ["manual", "bps_manual"]

    if file_backed and not args.series:
        extra_ids = [
            spec.series_id
            for source in file_backed
            for spec in catalog.select(source=source, active_only=False)
        ]
        if extra_ids:
            print(f"\nLoading {len(extra_ids)} file-backed series "
                  f"({', '.join(file_backed)})...")
            extra_report = refresh(
                series_ids=extra_ids,
                catalog=catalog,
                dry_run=args.dry_run,
                progress=emit,
            )
            report.results.extend(extra_report.results)

    print(f"\n{report.summary()}")

    if report.errors:
        print("\nErrors:")
        for result in report.errors:
            print(f"  {result.series_id:<28} {result.message}")

    if not args.quiet and not args.dry_run:
        with connect(read_only=True) as con:
            populated = coverage(con)
        populated = populated[populated["n_obs"] > 0]
        print(f"\nStore now holds {len(populated)} populated series, "
              f"{int(populated['n_obs'].sum()):,} observations.")

    # Errors are reported but do not fail the run: a scheduled refresh that
    # exits non-zero because one ticker was down would page you for nothing.
    # Only a total wipeout is worth signalling.
    return 1 if report.results and not any(r.ok for r in report.results) else 0


if __name__ == "__main__":
    sys.exit(main())
