"""DuckDB storage layer.

Everything in the app reads and writes observations through here. The store
holds three tables:

  series_meta   one row per catalog entry, refreshed from YAML on every run
  observations  the tidy fact table — (series_id, obs_date) is the primary key
  refresh_log   one row per series per refresh run, powering freshness and
                the "what changed" feed

Upserts overwrite on conflict, which is what we want: statistical agencies
revise, and for v1 the latest published value is the value we keep.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date, datetime
from typing import Iterable, Sequence

import duckdb
import pandas as pd

from .catalog import SeriesSpec
from .config import DB_PATH, STALENESS_DAYS, ensure_dirs

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS series_meta (
    series_id         TEXT PRIMARY KEY,
    name              TEXT,
    country           TEXT,
    category          TEXT,
    subcategory       TEXT,
    source            TEXT,
    source_id         TEXT,
    frequency         TEXT,
    unit              TEXT,
    seasonal_adj      BOOLEAN,
    default_transform TEXT,
    scale             DOUBLE,
    notes             TEXT,
    active            BOOLEAN,
    source_ref        TEXT,
    start_date        TEXT,
    staleness_days    INTEGER
);

CREATE TABLE IF NOT EXISTS observations (
    series_id     TEXT NOT NULL,
    obs_date      DATE NOT NULL,
    value         DOUBLE,
    ingested_at   TIMESTAMP NOT NULL,
    source_run_id TEXT,
    PRIMARY KEY (series_id, obs_date)
);

CREATE TABLE IF NOT EXISTS refresh_log (
    run_id        TEXT,
    series_id     TEXT,
    source        TEXT,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    status        TEXT,
    rows_added    INTEGER,
    rows_updated  INTEGER,
    last_obs_date DATE,
    message       TEXT
);
"""

OBS_COLUMNS = ["series_id", "obs_date", "value"]

#: Columns added to series_meta after the first release, applied on open.
MIGRATIONS = [("staleness_days", "INTEGER")]


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def connect(read_only: bool = False, db_path=None):
    """Open the store, creating it and its schema on first use.

    `db_path` overrides the configured location — tests use it to work against
    a throwaway file rather than the real store.
    """
    path = db_path or DB_PATH
    if db_path is None:
        ensure_dirs()

    if read_only and not path.exists():
        # Opening a non-existent file read-only fails; create it first.
        con = duckdb.connect(str(path))
        con.execute(SCHEMA_SQL)
        con.close()

    con = duckdb.connect(str(path), read_only=read_only)
    try:
        if not read_only:
            con.execute(SCHEMA_SQL)
            # CREATE TABLE IF NOT EXISTS is a no-op on a store that already
            # exists, so columns added later have to be introduced explicitly.
            # Cheap, idempotent, and it keeps an existing macro.duckdb usable
            # instead of demanding a rebuild for a metadata field.
            for column, sql_type in MIGRATIONS:
                con.execute(
                    f"ALTER TABLE series_meta ADD COLUMN IF NOT EXISTS {column} {sql_type}"
                )
        yield con
    finally:
        con.close()


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

def sync_series_meta(con: duckdb.DuckDBPyConnection, specs: Iterable[SeriesSpec]) -> int:
    """Mirror the YAML catalog into series_meta. The YAML is the source of truth."""
    rows = [spec.as_row() for spec in specs]
    if not rows:
        return 0

    frame = pd.DataFrame(rows)
    con.register("incoming_meta", frame)
    con.execute(
        """
        INSERT INTO series_meta BY NAME
        SELECT * FROM incoming_meta
        ON CONFLICT (series_id) DO UPDATE SET
            name              = excluded.name,
            country           = excluded.country,
            category          = excluded.category,
            subcategory       = excluded.subcategory,
            source            = excluded.source,
            source_id         = excluded.source_id,
            frequency         = excluded.frequency,
            unit              = excluded.unit,
            seasonal_adj      = excluded.seasonal_adj,
            default_transform = excluded.default_transform,
            scale             = excluded.scale,
            notes             = excluded.notes,
            active            = excluded.active,
            source_ref        = excluded.source_ref,
            start_date        = excluded.start_date,
            staleness_days    = excluded.staleness_days
        """
    )
    con.unregister("incoming_meta")
    return len(rows)


def upsert_observations(
    con: duckdb.DuckDBPyConnection,
    frame: pd.DataFrame,
    run_id: str | None = None,
) -> tuple[int, int]:
    """Insert or update observations. Returns (rows_added, rows_updated).

    `frame` must carry series_id, obs_date, value. Rows whose value is null
    are dropped rather than stored — a gap and a zero are not the same thing.
    """
    missing = set(OBS_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"observations frame is missing columns: {sorted(missing)}")

    clean = frame.loc[:, OBS_COLUMNS].copy()
    clean["obs_date"] = pd.to_datetime(clean["obs_date"]).dt.date
    clean["value"] = pd.to_numeric(clean["value"], errors="coerce")
    clean = clean.dropna(subset=["value"])
    clean = clean.drop_duplicates(subset=["series_id", "obs_date"], keep="last")
    if clean.empty:
        return (0, 0)

    clean["ingested_at"] = datetime.now()
    clean["source_run_id"] = run_id or new_run_id()

    con.register("incoming_obs", clean)

    added = con.execute(
        """
        SELECT count(*) FROM incoming_obs i
        LEFT JOIN observations o USING (series_id, obs_date)
        WHERE o.series_id IS NULL
        """
    ).fetchone()[0]

    updated = con.execute(
        """
        SELECT count(*) FROM incoming_obs i
        JOIN observations o USING (series_id, obs_date)
        WHERE o.value IS DISTINCT FROM i.value
        """
    ).fetchone()[0]

    # The WHERE matters more than it looks. Without it, every refresh rewrites
    # every row it fetched — ~100k rows to change nothing — and ingested_at
    # degrades from "when this value last changed" to "when we last looked".
    # With it, ingested_at is a real revision timestamp.
    con.execute(
        """
        INSERT INTO observations BY NAME
        SELECT * FROM incoming_obs
        ON CONFLICT (series_id, obs_date) DO UPDATE SET
            value         = excluded.value,
            ingested_at   = excluded.ingested_at,
            source_run_id = excluded.source_run_id
        WHERE observations.value IS DISTINCT FROM excluded.value
        """
    )
    con.unregister("incoming_obs")
    return (int(added), int(updated))


def delete_observations(con: duckdb.DuckDBPyConnection, series_ids: Sequence[str]) -> int:
    """Drop all stored observations for these series.

    Needed whenever a series' `source_id` changes. The series_id is stable by
    design, so repointing it at a different upstream series would otherwise
    leave the old vendor's history interleaved with the new one's under a single
    id — two different series silently averaged into one chart.
    """
    if not series_ids:
        return 0
    before = con.execute(
        "SELECT count(*) FROM observations WHERE series_id IN (SELECT unnest(?))",
        [list(series_ids)],
    ).fetchone()[0]
    con.execute(
        "DELETE FROM observations WHERE series_id IN (SELECT unnest(?))",
        [list(series_ids)],
    )
    return int(before)


def prune_observations(
    con: duckdb.DuckDBPyConnection,
    series_id: str,
    keep_dates: Iterable,
) -> int:
    """Drop this series' observations that are not in `keep_dates`. Returns the count.

    For a file-backed series the file is the source of truth, and an upsert
    alone cannot express that: it adds and revises but never removes, so a row
    that disappears from the file lives on in the store forever. Re-dating the
    BI Rate from month-starts to actual announcement dates left twenty orphaned
    rows behind and drew a policy move that never happened — 5.25 to 5.75 on
    1 June, then "down" to 5.50 on the 9th.

    Deliberately NOT used for API-backed sources. There the vendor sends a
    window, not a complete history, and pruning to it would delete every
    observation outside that window.
    """
    keep = [pd.Timestamp(stamp).date() for stamp in keep_dates]
    if not keep:
        return 0

    removed = con.execute(
        """
        SELECT count(*) FROM observations
        WHERE series_id = ? AND obs_date NOT IN (SELECT unnest(?))
        """,
        [series_id, keep],
    ).fetchone()[0]

    if removed:
        con.execute(
            """
            DELETE FROM observations
            WHERE series_id = ? AND obs_date NOT IN (SELECT unnest(?))
            """,
            [series_id, keep],
        )
    return int(removed)


def log_refresh(con: duckdb.DuckDBPyConnection, records: Sequence[dict]) -> None:
    if not records:
        return
    frame = pd.DataFrame(records)
    con.register("incoming_log", frame)
    con.execute("INSERT INTO refresh_log BY NAME SELECT * FROM incoming_log")
    con.unregister("incoming_log")


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def read_series(
    con: duckdb.DuckDBPyConnection,
    series_ids: str | Sequence[str],
    start: str | date | None = None,
    end: str | date | None = None,
) -> pd.DataFrame:
    """Long-format observations for one or more series, oldest first."""
    if isinstance(series_ids, str):
        series_ids = [series_ids]
    if not series_ids:
        return pd.DataFrame(columns=OBS_COLUMNS)

    sql = """
        SELECT series_id, obs_date, value
        FROM observations
        WHERE series_id IN (SELECT unnest(?))
    """
    params: list = [list(series_ids)]
    if start:
        sql += " AND obs_date >= ?"
        params.append(pd.Timestamp(start).date())
    if end:
        sql += " AND obs_date <= ?"
        params.append(pd.Timestamp(end).date())
    sql += " ORDER BY series_id, obs_date"

    frame = con.execute(sql, params).df()
    if not frame.empty:
        frame["obs_date"] = pd.to_datetime(frame["obs_date"])
    return frame


def read_wide(
    con: duckdb.DuckDBPyConnection,
    series_ids: Sequence[str],
    start: str | date | None = None,
    end: str | date | None = None,
) -> pd.DataFrame:
    """Same data as read_series, pivoted to one column per series."""
    long = read_series(con, series_ids, start, end)
    if long.empty:
        return pd.DataFrame()
    wide = long.pivot(index="obs_date", columns="series_id", values="value")
    wide.columns.name = None
    return wide.sort_index()


def series_as_pandas(
    con: duckdb.DuckDBPyConnection,
    series_id: str,
    start: str | date | None = None,
) -> pd.Series:
    """A single series as a date-indexed pandas Series, ready for transforms."""
    frame = read_series(con, series_id, start=start)
    if frame.empty:
        return pd.Series(dtype="float64", name=series_id)
    out = frame.set_index("obs_date")["value"].astype("float64")
    out.name = series_id
    return out


def latest_values(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """One row per series: its most recent observation and the one before it."""
    return con.execute(
        """
        WITH ranked AS (
            SELECT series_id, obs_date, value,
                   row_number() OVER (PARTITION BY series_id ORDER BY obs_date DESC) AS rn
            FROM observations
        )
        SELECT
            m.series_id, m.name, m.country, m.category, m.frequency,
            m.unit, m.default_transform, m.source, m.staleness_days,
            cur.obs_date  AS last_obs_date,
            cur.value     AS last_value,
            prev.obs_date AS prev_obs_date,
            prev.value    AS prev_value
        FROM series_meta m
        LEFT JOIN ranked cur  ON cur.series_id  = m.series_id AND cur.rn  = 1
        LEFT JOIN ranked prev ON prev.series_id = m.series_id AND prev.rn = 2
        WHERE m.active
        ORDER BY m.country, m.category, m.series_id
        """
    ).df()


def freshness(con: duckdb.DuckDBPyConnection, today: date | None = None) -> pd.DataFrame:
    """Per-series staleness, judged against the frequency-aware thresholds."""
    frame = latest_values(con)
    if frame.empty:
        return frame

    today = today or date.today()
    frame["last_obs_date"] = pd.to_datetime(frame["last_obs_date"])
    frame["days_since"] = (pd.Timestamp(today) - frame["last_obs_date"]).dt.days
    # A per-series override wins over the frequency default. An event-dated
    # series — a policy rate, dated to the decisions that moved it — has gaps
    # that mean "no decision", not "no data", and the daily threshold would
    # read every quiet stretch as a fault.
    limits = frame["frequency"].map(STALENESS_DAYS).fillna(90)
    if "staleness_days" in frame.columns:
        override = pd.to_numeric(frame["staleness_days"], errors="coerce")
        limits = override.where(override.fillna(0) > 0, limits)

    def classify(row_days, limit) -> str:
        if pd.isna(row_days):
            return "empty"
        if row_days <= limit:
            return "fresh"
        if row_days <= limit * 2:
            return "ageing"
        return "stale"

    frame["status"] = [
        classify(d, limit) for d, limit in zip(frame["days_since"], limits, strict=True)
    ]
    return frame


def recent_changes(con: duckdb.DuckDBPyConnection, days: int = 7) -> pd.DataFrame:
    """Series that gained or revised data in the last N days — the Home feed."""
    return con.execute(
        """
        SELECT
            l.series_id,
            m.name, m.country, m.category,
            max(l.finished_at)  AS updated_at,
            sum(l.rows_added)   AS rows_added,
            sum(l.rows_updated) AS rows_updated,
            max(l.last_obs_date) AS last_obs_date
        FROM refresh_log l
        JOIN series_meta m USING (series_id)
        WHERE l.finished_at >= now() - INTERVAL (?) DAY
          AND (l.rows_added > 0 OR l.rows_updated > 0)
        GROUP BY l.series_id, m.name, m.country, m.category
        ORDER BY updated_at DESC
        """,
        [days],
    ).df()


def coverage(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Row counts and date span per series — the quick 'is anything empty?' check."""
    return con.execute(
        """
        SELECT
            m.series_id, m.name, m.country, m.category, m.source, m.frequency,
            count(o.value)   AS n_obs,
            min(o.obs_date)  AS first_obs,
            max(o.obs_date)  AS last_obs
        FROM series_meta m
        LEFT JOIN observations o USING (series_id)
        GROUP BY ALL
        ORDER BY m.country, m.category, m.series_id
        """
    ).df()
