"""Refresh orchestration.

One rule governs this module: a failure is always scoped to a single series.
A retired FRED id, a rate limit, or a yfinance outage marks that series as
errored in refresh_log and the run continues. A refresh that stops at the first
problem would leave the store in a half-updated state with no record of why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .catalog import Catalog, SeriesSpec, load_catalog
from .sources import FetchError, get_fetcher
from .store import (
    connect,
    delete_observations,
    log_refresh,
    new_run_id,
    sync_series_meta,
    upsert_observations,
)


@dataclass
class SeriesResult:
    series_id: str
    source: str
    status: str  # ok | no_new | error | skipped
    rows_added: int = 0
    rows_updated: int = 0
    last_obs_date: pd.Timestamp | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "no_new")


@dataclass
class RunReport:
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    results: list[SeriesResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def errors(self) -> list[SeriesResult]:
        return [r for r in self.results if r.status == "error"]

    @property
    def rows_added(self) -> int:
        return sum(r.rows_added for r in self.results)

    @property
    def rows_updated(self) -> int:
        return sum(r.rows_updated for r in self.results)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([vars(r) for r in self.results])

    def summary(self) -> str:
        ok = sum(1 for r in self.results if r.ok)
        prefix = "DRY RUN — nothing written. " if self.dry_run else ""
        return (
            f"{prefix}{ok}/{len(self.results)} series ok, "
            f"{self.rows_added} rows added, {self.rows_updated} updated, "
            f"{len(self.errors)} errors"
        )


def refresh(
    series_ids: list[str] | None = None,
    *,
    source: str | None = None,
    country: str | None = None,
    catalog: Catalog | None = None,
    start: str | None = None,
    dry_run: bool = False,
    reset: bool = False,
    progress=None,
) -> RunReport:
    """Fetch and store the selected series.

    Selection is by explicit ids, or by source/country filter, or everything.
    `reset` wipes existing observations for the selected series first — use it
    when a series' source_id has changed, so the old upstream's history does not
    survive underneath the new one's.
    `progress` is an optional callable taking (index, total, SeriesResult) —
    the Streamlit refresh buttons use it to draw a live log.
    """
    catalog = catalog or load_catalog()

    if series_ids:
        specs = [catalog[sid] for sid in series_ids]
    else:
        specs = catalog.select(source=source, country=country)

    report = RunReport(run_id=new_run_id(), started_at=datetime.now(), dry_run=dry_run)
    if not specs:
        report.finished_at = datetime.now()
        return report

    fetchers: dict[str, object] = {}
    log_records: list[dict] = []

    with connect() as con:
        sync_series_meta(con, catalog)

        if reset and not dry_run:
            dropped = delete_observations(con, [spec.series_id for spec in specs])
            if dropped:
                print(f"Reset: cleared {dropped:,} existing observations.")

        for index, spec in enumerate(specs, start=1):
            started = datetime.now()
            result = _refresh_one(con, spec, fetchers, start, dry_run, report.run_id)
            report.results.append(result)

            log_records.append(
                {
                    "run_id": report.run_id,
                    "series_id": spec.series_id,
                    "source": spec.source,
                    "started_at": started,
                    "finished_at": datetime.now(),
                    "status": result.status,
                    "rows_added": result.rows_added,
                    "rows_updated": result.rows_updated,
                    "last_obs_date": (
                        result.last_obs_date.date() if result.last_obs_date is not None else None
                    ),
                    "message": result.message[:500],
                }
            )

            if progress:
                progress(index, len(specs), result)

        if not dry_run:
            log_refresh(con, log_records)

    report.finished_at = datetime.now()
    return report


def _refresh_one(
    con,
    spec: SeriesSpec,
    fetchers: dict,
    start: str | None,
    dry_run: bool,
    run_id: str,
) -> SeriesResult:
    try:
        if spec.source not in fetchers:
            fetchers[spec.source] = get_fetcher(spec.source)
        fetcher = fetchers[spec.source]

        frame = fetcher.fetch(spec, start=start)

        # Applied here rather than in each fetcher so every source honours it.
        if spec.start_date and not frame.empty:
            cutoff = pd.Timestamp(spec.start_date)
            frame = frame.loc[pd.to_datetime(frame["obs_date"]) >= cutoff]

        if frame.empty:
            return SeriesResult(spec.series_id, spec.source, "no_new", message="source returned no rows")

        last_obs = pd.Timestamp(frame["obs_date"].max())

        if dry_run:
            return SeriesResult(
                spec.series_id,
                spec.source,
                "ok",
                rows_added=len(frame),
                last_obs_date=last_obs,
                message=f"would write {len(frame)} rows",
            )

        added, updated = upsert_observations(con, frame, run_id=run_id)
        status = "ok" if (added or updated) else "no_new"
        return SeriesResult(
            spec.series_id,
            spec.source,
            status,
            rows_added=added,
            rows_updated=updated,
            last_obs_date=last_obs,
        )

    except FetchError as exc:
        return SeriesResult(spec.series_id, spec.source, "error", message=str(exc))
    except Exception as exc:  # noqa: BLE001 — one bad series must not end the run
        return SeriesResult(
            spec.series_id, spec.source, "error", message=f"{type(exc).__name__}: {exc}"
        )
