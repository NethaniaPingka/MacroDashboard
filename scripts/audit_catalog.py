"""Check every catalog id against its source before trusting a chart.

FRED retires and renames international series, and yfinance tickers go stale.
This tells you which ids are real, what the source actually calls them, and
whether our declared frequency matches what is published.

    python scripts/audit_catalog.py                  # everything it can check
    python scripts/audit_catalog.py --source fred
    python scripts/audit_catalog.py --fix-inactive   # deactivate broken ids

Requires FRED_API_KEY for the FRED half; the Yahoo half needs no key.
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd
import yaml

from macrodash.catalog import load_catalog
from macrodash.config import CATALOG_DIR, STALENESS_DAYS, get_secret
from macrodash.sources.base import FetchError, SeriesNotFound

FRED_FREQ_MAP = {
    "Daily": "D",
    "Weekly": "W",
    "Monthly": "M",
    "Quarterly": "Q",
    "Annual": "A",
    "Semiannual": "A",
}


def audit_fred(specs) -> list[dict]:
    from macrodash.sources.fred import FredFetcher

    if not get_secret("FRED_API_KEY"):
        print("! FRED_API_KEY not set — skipping the FRED half of the audit.\n"
              "  Get one free at https://fredaccount.stlouisfed.org/apikeys\n")
        return []

    fetcher = FredFetcher()
    rows = []
    for spec in specs:
        row = {"series_id": spec.series_id, "source_id": spec.source_id, "source": "fred"}
        try:
            meta = fetcher.describe(spec.source_id)
        except SeriesNotFound as exc:
            row |= {"status": "MISSING", "detail": str(exc)[:120]}
            rows.append(row)
            continue
        except FetchError as exc:
            # Reachability problem, not a verdict on the series. Never let this
            # feed --fix-inactive.
            row |= {"status": "UNKNOWN", "detail": str(exc)[:120]}
            rows.append(row)
            continue

        published_freq = FRED_FREQ_MAP.get(meta.get("frequency", ""), "?")
        notes = meta.get("notes", "") or ""
        discontinued = "DISCONTINUED" in meta.get("title", "").upper() or "discontinued" in notes.lower()

        problems = []
        if discontinued:
            problems.append("marked discontinued")
        if published_freq != "?" and published_freq != spec.frequency:
            problems.append(f"frequency is {published_freq}, catalog says {spec.frequency}")

        # An id that resolves is not the same as an id that still updates. FRED
        # keeps abandoned OECD/IMF mirrors online indefinitely — they answer the
        # API happily while their last observation recedes into history. Judge
        # the data, not the id.
        end = meta.get("observation_end", "")
        if end:
            days_behind = (pd.Timestamp.today().normalize() - pd.Timestamp(end)).days
            limit = STALENESS_DAYS.get(spec.frequency, 90)
            if days_behind > limit * 2:
                problems.append(f"ABANDONED — last observation {end}, {days_behind}d ago")
            elif days_behind > limit:
                problems.append(f"lagging — last observation {end}, {days_behind}d ago")

        row |= {
            "status": "PROBLEM" if problems else "OK",
            "title": meta.get("title", "")[:60],
            "last_updated": meta.get("last_updated", "")[:10],
            "detail": "; ".join(problems),
        }
        rows.append(row)
    return rows


def audit_yahoo(specs) -> list[dict]:
    from macrodash.sources.yahoo import YahooFetcher

    fetcher = YahooFetcher()
    rows = []
    for spec in specs:
        row = {"series_id": spec.series_id, "source_id": spec.source_id, "source": "yahoo"}
        try:
            frame = fetcher.fetch(spec, start="2024-01-01")
        except Exception as exc:  # noqa: BLE001
            row |= {"status": "MISSING", "detail": f"{type(exc).__name__}: {exc}"[:120]}
            rows.append(row)
            continue

        last = pd.Timestamp(frame["obs_date"].max())
        days_old = (pd.Timestamp.today().normalize() - last).days
        row |= {
            "status": "PROBLEM" if days_old > 10 else "OK",
            "title": spec.name[:60],
            "last_updated": str(last.date()),
            "detail": f"last price is {days_old} days old" if days_old > 10 else "",
        }
        rows.append(row)
    return rows


def deactivate(series_ids: set[str]) -> int:
    """Set active: false on the named series, in place, preserving comments
    is not possible with a YAML round-trip — so we rewrite only the flag lines."""
    changed = 0
    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        entries = yaml.safe_load(text) or []
        hits = [e["series_id"] for e in entries if e["series_id"] in series_ids]
        if not hits:
            continue

        lines = text.splitlines()
        out: list[str] = []
        current: str | None = None
        pending: set[str] = set(hits)

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- series_id:"):
                current = stripped.split(":", 1)[1].strip()
            if stripped.startswith("active:") and current in pending:
                out.append(line.split("active:")[0] + "active: false")
                pending.discard(current)
                changed += 1
                continue
            out.append(line)

        # Series with no explicit active: line need one inserted. It has to go
        # after the block's last *content* line, not at the block boundary —
        # appending at the boundary strands it after the blank separator, which
        # parses but reads as though it belongs to the next series.
        if pending:
            rebuilt: list[str] = []
            current: str | None = None
            block: list[str] = []

            def flush() -> int:
                """Emit the buffered block, inserting the flag before trailing blanks."""
                if not block:
                    return 0
                added = 0
                if current in pending:
                    tail = len(block)
                    while tail > 0 and not block[tail - 1].strip():
                        tail -= 1
                    block.insert(tail, "  active: false")
                    pending.discard(current)
                    added = 1
                rebuilt.extend(block)
                block.clear()
                return added

            for line in out:
                if line.strip().startswith("- series_id:"):
                    changed += flush()
                    current = line.strip().split(":", 1)[1].strip()
                block.append(line)
            changed += flush()
            out = rebuilt

        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate catalog ids against their sources.")
    parser.add_argument("--source", choices=["fred", "yahoo"], help="audit only this source")
    parser.add_argument("--fix-inactive", action="store_true",
                        help="set active: false on every id that failed")
    args = parser.parse_args(argv)

    catalog = load_catalog()
    rows: list[dict] = []

    if args.source in (None, "fred"):
        rows += audit_fred(catalog.select(source="fred", active_only=False))
    if args.source in (None, "yahoo"):
        rows += audit_yahoo(catalog.select(source="yahoo", active_only=False))

    if not rows:
        print("Nothing audited.")
        return 0

    frame = pd.DataFrame(rows)
    for status in ("MISSING", "UNKNOWN", "PROBLEM", "OK"):
        subset = frame[frame["status"] == status]
        if subset.empty:
            continue
        print(f"\n{status}  ({len(subset)})")
        print("-" * 78)
        for _, row in subset.iterrows():
            detail = f"  {row['detail']}" if row.get("detail") else ""
            print(f"  {row['series_id']:<26} {row['source_id']:<22}{detail}")

    broken = set(frame.loc[frame["status"] == "MISSING", "series_id"])
    unreachable = int((frame["status"] == "UNKNOWN").sum())
    print(f"\n{len(frame)} ids checked — "
          f"{(frame['status'] == 'OK').sum()} ok, "
          f"{(frame['status'] == 'PROBLEM').sum()} with problems, "
          f"{len(broken)} missing, {unreachable} unreachable.")
    if unreachable:
        print("Unreachable ids were NOT judged — re-run the audit before acting on them.")

    if broken and args.fix_inactive:
        count = deactivate(broken)
        print(f"Set active: false on {count} series. Re-run without --fix-inactive to confirm.")
    elif broken:
        print("Re-run with --fix-inactive to deactivate the missing ids.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
