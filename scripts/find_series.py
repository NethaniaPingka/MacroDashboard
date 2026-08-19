"""Search FRED for series to add to the catalog.

Finding the right FRED id is otherwise a browser hunt. This queries the search
endpoint and prints ids with their frequency, units, span, and popularity, so a
catalog entry can be written straight from the output.

    python scripts/find_series.py "Singapore consumer price index"
    python scripts/find_series.py "Thailand CPI" --freq M
    python scripts/find_series.py "policy rate Malaysia" --freq M --limit 15
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from macrodash.sources.fred import FredFetcher

FREQ_SHORT = {
    "Daily": "D", "Weekly": "W", "Monthly": "M",
    "Quarterly": "Q", "Annual": "A", "Semiannual": "SA",
}


def search(fetcher: FredFetcher, text: str, limit: int = 20) -> pd.DataFrame:
    payload = fetcher._get(
        "series/search",
        {
            "search_text": text,
            "limit": limit,
            "order_by": "popularity",
            "sort_order": "desc",
        },
    )
    rows = payload.get("seriess", [])
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    keep = ["id", "title", "frequency", "units_short", "observation_start",
            "observation_end", "popularity", "seasonal_adjustment_short"]
    frame = frame.loc[:, [c for c in keep if c in frame.columns]]
    frame["freq"] = frame["frequency"].map(FREQ_SHORT).fillna(frame["frequency"])
    return frame


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search FRED for catalog candidates.")
    parser.add_argument("query", help="free-text search, e.g. 'Thailand consumer price index'")
    parser.add_argument("--freq", help="keep only this frequency code (D/W/M/Q/A)")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--since", default="2020-01-01",
                        help="drop series whose data ends before this date (default 2020-01-01)")
    args = parser.parse_args(argv)

    frame = search(FredFetcher(), args.query, args.limit)
    if frame.empty:
        print("No results.")
        return 0

    if args.freq:
        frame = frame[frame["freq"] == args.freq]
    if args.since:
        frame = frame[frame["observation_end"] >= args.since]

    if frame.empty:
        print("No results after filtering. Loosen --freq or --since.")
        return 0

    for _, row in frame.iterrows():
        sa = row.get("seasonal_adjustment_short", "")
        print(f"{row['id']:<24} {row['freq']:<3} {str(row.get('units_short',''))[:16]:<16} "
              f"{row['observation_start'][:7]}..{row['observation_end'][:7]}  {sa:<4} "
              f"pop={int(row.get('popularity', 0)):>3}")
        print(f"  {row['title'][:100]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
