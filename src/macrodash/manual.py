"""Hand-entered observations: the route for numbers no API publishes.

Bank Indonesia has no open API and FRED does not carry the BI Rate, so
``ID.POLICY.RATE`` can only ever arrive by someone typing it. This module is
that path, and it is deliberately more than a write to DuckDB.

**Entries are written to ``data/manual/<series_id>.csv`` first, and only then
upserted into the store.** The DuckDB file is gitignored and declared
rebuildable from the catalog plus the committed data directories; if a
hand-entered rate history lived only inside it, a clean rebuild — the very thing
the README tells you to do when something looks wrong — would destroy work that
exists nowhere else. The CSV is the durable copy. ``sources/manual.py`` reads it
back on every refresh, which is what makes a manual series survive ``--reset``
and behave like every other source downstream.

Nothing here imports Streamlit. The Data Manager page is one caller;
``refresh_all.py`` is another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from .catalog import SeriesSpec
from .config import MANUAL_DIR

#: Columns of a manual CSV. ``source_ref`` is not decoration: a number that was
#: typed rather than fetched has no upstream to re-query, so a pointer back to
#: the publication it came from is the only way a suspicious value can ever be
#: checked. ``entered_at`` records when it was typed, which the store's own
#: ``ingested_at`` cannot, since that is overwritten on every later revision.
MANUAL_COLUMNS = ["series_id", "obs_date", "value", "source_ref", "entered_at"]

#: Percent-quoted values outside this band are almost certainly a unit mistake —
#: a policy rate typed in basis points reads as 525, not 5.25. A warning, never
#: a block: negative policy rates and hyperinflation are both real.
RATE_PLAUSIBLE = (-25.0, 100.0)

#: How far outside its own historical range a value may sit before it is
#: queried, as a multiple of that range.
RANGE_TOLERANCE = 2.0
MIN_HISTORY_FOR_RANGE_CHECK = 8


class ManualError(ValueError):
    """A typed batch that cannot be stored as given."""


# ==========================================================================
# period alignment
# ==========================================================================

def normalise_date(value, frequency: str) -> pd.Timestamp:
    """Snap a typed date to the start of its period.

    Every fetcher in this project dates an observation to the START of the
    period it covers — June CPI is dated 1 June — and the transforms match on
    calendar offsets from that date. A monthly value filed under 2026-08-15
    would therefore never be found by a YoY lookup a year later: it would not
    error, it would quietly produce NaN. Snapping keeps hand entry on the same
    convention as every fetcher, and the page shows the snapped date in the
    preview before anything is committed, so the correction is never silent.
    """
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ManualError(f"{value!r} is not a date")
    stamp = stamp.normalize()

    if frequency == "M":
        return stamp.replace(day=1)
    if frequency == "Q":
        return stamp.replace(month=((stamp.month - 1) // 3) * 3 + 1, day=1)
    if frequency == "A":
        return stamp.replace(month=1, day=1)
    if frequency == "W":
        return stamp - pd.Timedelta(days=stamp.weekday())
    return stamp  # D — a daily observation is its own date


def next_period(last, frequency: str) -> pd.Timestamp:
    """The date the next observation of this series should carry.

    Used to prefill the entry grid: the overwhelmingly common manual edit is
    "one new row, one period on", and making someone work out whether the next
    quarter starts in July or in August is friction for no gain.
    """
    if frequency == "D":
        # An event-dated series is not "one more day"; the next thing you type is
        # a decision, and it is almost always today's or a recent one.
        return normalise_date(pd.Timestamp.today(), frequency)
    if last is None or pd.isna(pd.Timestamp(last) if last is not None else pd.NaT):
        return normalise_date(pd.Timestamp.today(), frequency)
    offsets = {
        "D": pd.DateOffset(days=1),
        "W": pd.DateOffset(weeks=1),
        "M": pd.DateOffset(months=1),
        "Q": pd.DateOffset(months=3),
        "A": pd.DateOffset(years=1),
    }
    step = offsets.get(frequency, offsets["M"])
    return normalise_date(pd.Timestamp(last) + step, frequency)


# ==========================================================================
# the CSV side — the durable copy
# ==========================================================================

def manual_path(series_id: str, manual_dir: Path | None = None) -> Path:
    return (manual_dir or MANUAL_DIR) / f"{series_id}.csv"


def read_manual(series_id: str, manual_dir: Path | None = None) -> pd.DataFrame:
    """What has been typed for this series so far. Empty frame if nothing has."""
    path = manual_path(series_id, manual_dir)
    if not path.exists():
        return pd.DataFrame(columns=MANUAL_COLUMNS)

    frame = pd.read_csv(path)
    missing = {"series_id", "obs_date", "value"} - set(frame.columns)
    if missing:
        raise ManualError(f"{path.name} is missing columns: {sorted(missing)}")
    for column in ("source_ref", "entered_at"):
        if column not in frame.columns:
            frame[column] = ""

    frame["obs_date"] = pd.to_datetime(frame["obs_date"], errors="coerce")
    if frame["obs_date"].isna().any():
        bad = int(frame["obs_date"].isna().sum())
        raise ManualError(f"{path.name} has {bad} unparseable date(s)")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame["source_ref"] = frame["source_ref"].fillna("").astype(str)
    frame["entered_at"] = frame["entered_at"].fillna("").astype(str)
    return frame.loc[:, MANUAL_COLUMNS].sort_values("obs_date").reset_index(drop=True)


def write_manual(series_id: str, frame: pd.DataFrame, manual_dir: Path | None = None) -> Path:
    """Replace this series' CSV. Written whole, sorted, one series per file."""
    target_dir = manual_dir or MANUAL_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = manual_path(series_id, manual_dir)

    out = frame.loc[:, MANUAL_COLUMNS].copy()
    out["obs_date"] = pd.to_datetime(out["obs_date"]).dt.strftime("%Y-%m-%d")
    out = out.sort_values("obs_date").reset_index(drop=True)

    # Written to a sibling and moved into place, so an interrupted write cannot
    # leave a truncated file where the only copy of hand-typed history was.
    scratch = path.with_name(path.name + ".tmp")
    out.to_csv(scratch, index=False)
    scratch.replace(path)
    return path


def load_manual_frame(manual_dir: Path | None = None) -> pd.DataFrame:
    """Every manual CSV concatenated — what the fetcher and coverage read."""
    target_dir = manual_dir or MANUAL_DIR
    empty = pd.DataFrame(columns=[*MANUAL_COLUMNS, "_file"])
    if not target_dir.exists():
        return empty

    frames = []
    for path in sorted(target_dir.glob("*.csv")):
        frame = read_manual(path.stem, manual_dir=target_dir)
        if frame.empty:
            continue
        frame["_file"] = path.name
        frames.append(frame)

    return pd.concat(frames, ignore_index=True) if frames else empty


def manual_coverage(manual_dir: Path | None = None) -> pd.DataFrame:
    """One row per manually-entered series: how much, how far, last touched."""
    frame = load_manual_frame(manual_dir)
    if frame.empty:
        return pd.DataFrame(
            columns=["series_id", "n_obs", "first_obs", "last_obs", "last_entered", "file"]
        )
    return (
        frame.groupby("series_id")
        .agg(
            n_obs=("value", "count"),
            first_obs=("obs_date", "min"),
            last_obs=("obs_date", "max"),
            last_entered=("entered_at", "max"),
            file=("_file", "first"),
        )
        .reset_index()
        .sort_values("series_id")
    )


# ==========================================================================
# validation and the diff
# ==========================================================================

@dataclass
class Prepared:
    """A typed batch, cleaned and judged, but not yet written anywhere."""

    entries: pd.DataFrame          # obs_date, value, source_ref — ready to store
    diff: pd.DataFrame             # one row per entry, with status and warnings
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.entries.empty

    @property
    def n_new(self) -> int:
        return int((self.diff["status"] == "new").sum()) if not self.diff.empty else 0

    @property
    def n_revised(self) -> int:
        return int((self.diff["status"] == "revision").sum()) if not self.diff.empty else 0

    @property
    def n_unchanged(self) -> int:
        return int((self.diff["status"] == "unchanged").sum()) if not self.diff.empty else 0

    @property
    def warnings(self) -> list[str]:
        if self.diff.empty:
            return []
        flagged = self.diff.loc[self.diff["warning"] != ""]
        return [
            f"{row.obs_date:%Y-%m-%d}: {row.warning}" for row in flagged.itertuples()
        ]


def prepare(
    spec: SeriesSpec,
    rows: pd.DataFrame,
    stored: pd.Series | None = None,
    source_ref: str = "",
) -> Prepared:
    """Clean a typed batch, classify each row against what is stored, and flag
    anything that looks wrong.

    Returns rather than raises for row-level trouble, because the page needs to
    show every problem at once — reporting them one exception at a time would
    make correcting ten pasted rows take ten round trips. ``problems`` blocks the
    commit; ``warning`` on a diff row does not.
    """
    problems: list[str] = []
    frame = rows.copy()

    for column in ("obs_date", "value"):
        if column not in frame.columns:
            raise ManualError(f"entry grid is missing the {column!r} column")
    if "source_ref" not in frame.columns:
        frame["source_ref"] = ""

    # A blank row is how an empty grid arrives; it is not an error, just nothing.
    frame = frame.loc[~(frame["obs_date"].isna() & frame["value"].isna())]

    undated = frame["obs_date"].isna().sum()
    if undated:
        problems.append(f"{undated} row(s) have a value but no date")
    valueless = frame.loc[frame["obs_date"].notna(), "value"].isna().sum()
    if valueless:
        problems.append(
            f"{valueless} row(s) have a date but no value — leave the row blank to "
            f"skip it, or delete it. A gap and a zero are not the same thing."
        )

    frame = frame.dropna(subset=["obs_date", "value"])
    if frame.empty:
        return Prepared(
            entries=pd.DataFrame(columns=["obs_date", "value", "source_ref"]),
            diff=pd.DataFrame(),
            problems=problems,
        )

    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    unparseable = frame["value"].isna().sum()
    if unparseable:
        problems.append(f"{unparseable} value(s) are not numeric")
        frame = frame.dropna(subset=["value"])

    typed = pd.to_datetime(frame["obs_date"])
    frame["obs_date"] = [normalise_date(stamp, spec.frequency) for stamp in typed]
    frame["moved_from"] = [
        typed_stamp.strftime("%Y-%m-%d")
        if typed_stamp.normalize() != snapped else ""
        for typed_stamp, snapped in zip(typed, frame["obs_date"], strict=True)
    ]

    duplicated = frame["obs_date"].duplicated(keep=False)
    if duplicated.any():
        clashes = sorted({stamp.strftime("%Y-%m-%d") for stamp in frame.loc[duplicated, "obs_date"]})
        problems.append(
            f"two or more rows land on the same period after alignment: "
            f"{', '.join(clashes)}. Which value wins is ambiguous, so nothing is stored."
        )

    frame["source_ref"] = frame["source_ref"].fillna("").astype(str).str.strip()
    if source_ref:
        frame.loc[frame["source_ref"] == "", "source_ref"] = source_ref

    frame = frame.sort_values("obs_date").reset_index(drop=True)

    history = stored if stored is not None else pd.Series(dtype="float64")
    diff = _diff(spec, frame, history)

    entries = frame.loc[:, ["obs_date", "value", "source_ref"]]
    return Prepared(entries=entries, diff=diff, problems=problems)


def _diff(spec: SeriesSpec, frame: pd.DataFrame, history: pd.Series) -> pd.DataFrame:
    """Label each entry new / revision / unchanged, and attach any warning."""
    lookup = {}
    if history is not None and not history.empty:
        lookup = {pd.Timestamp(idx).normalize(): value for idx, value in history.items()}

    rows = []
    for entry in frame.itertuples():
        previous = lookup.get(pd.Timestamp(entry.obs_date).normalize())
        if previous is None or pd.isna(previous):
            status = "new"
        elif float(previous) == float(entry.value):
            status = "unchanged"
        else:
            status = "revision"
        rows.append(
            {
                "obs_date": entry.obs_date,
                "value": float(entry.value),
                "stored_value": None if previous is None or pd.isna(previous) else float(previous),
                "status": status,
                "moved_from": entry.moved_from,
                "source_ref": entry.source_ref,
                "warning": _warning(spec, entry.obs_date, float(entry.value), history),
            }
        )
    return pd.DataFrame(rows)


def _history_is_sub_percent(history: pd.Series) -> bool:
    """True when this series genuinely lives below 1% — a ZIRP-era policy rate.

    Without this the decimal-fraction check would nag on every legitimate near-
    zero rate, and a validator that cries wolf gets ignored exactly when it is
    right.
    """
    if history is None or history.empty:
        return False
    clean = history.dropna()
    return not clean.empty and float(clean.abs().median()) < 1.0


def _warning(spec: SeriesSpec, obs_date, value: float, history: pd.Series) -> str:
    """Soft checks on a single typed value.

    The habit this project runs on is checking that a number is *right*, not
    merely present, and a hand-typed number has no upstream to disagree with it.
    Its own history is the only second opinion available, so that is what these
    compare against. All three are warnings: every one of them has a legitimate
    exception, and a validator that blocks real data is worse than one that asks.
    """
    unit = (spec.unit or "").lower()

    if pd.Timestamp(obs_date) > pd.Timestamp.today().normalize():
        return f"dated in the future ({pd.Timestamp(obs_date):%Y-%m-%d})"

    if "percent" in unit and not (RATE_PLAUSIBLE[0] <= value <= RATE_PLAUSIBLE[1]):
        return (
            f"{value:,.4g} is outside {RATE_PLAUSIBLE[0]:g}–{RATE_PLAUSIBLE[1]:g}% — "
            f"check the units (basis points instead of percent?)"
        )

    # The mirror image of the basis-points mistake, and the one that actually
    # happened: a 5.75% policy rate typed as 0.0575. It sits well inside the
    # plausible band above, so nothing caught it, and it is only wrong by the
    # factor that flips the sign of the real rate against 2.88% inflation.
    # Suppressed once a series' own history lives down here, because a genuine
    # near-zero rate is a real thing.
    if "percent" in unit and 0 < abs(value) < 1 and not _history_is_sub_percent(history):
        return (
            f"{value:,.4g} looks like a decimal fraction — {value * 100:,.4g}% would "
            f"be entered as {value * 100:,.4g}, not {value:,.4g}"
        )

    if history is not None and len(history.dropna()) >= MIN_HISTORY_FOR_RANGE_CHECK:
        clean = history.dropna()
        low, high = float(clean.min()), float(clean.max())
        span = high - low
        if span <= 0:
            span = abs(high) or 1.0
        if not (low - RANGE_TOLERANCE * span <= value <= high + RANGE_TOLERANCE * span):
            return (
                f"{value:,.4g} sits far outside this series' history "
                f"({low:,.4g} to {high:,.4g})"
            )

    return ""


# ==========================================================================
# committing
# ==========================================================================

#: Sources whose entire history lives in data/manual/. For these the CSV is the
#: source of truth and the store is reconciled to it; for anything else the
#: vendor owns the history and we only ever add to it.
MANUAL_SOURCES = ("manual", "bps_manual")


@dataclass
class SaveResult:
    series_id: str
    csv_path: Path
    rows_added: int
    rows_updated: int
    total_manual_rows: int
    rows_removed: int = 0

    def summary(self) -> str:
        parts = [f"{self.rows_added} added", f"{self.rows_updated} revised"]
        if self.rows_removed:
            parts.append(f"{self.rows_removed} stale row(s) removed")
        return (
            f"{self.series_id}: {', '.join(parts)}. "
            f"{self.total_manual_rows} row(s) now in {self.csv_path.name}."
        )


def save(
    spec: SeriesSpec,
    entries: pd.DataFrame,
    manual_dir: Path | None = None,
    db_path: Path | None = None,
) -> SaveResult:
    """Merge a prepared batch into the series' CSV, then into the store.

    CSV first, deliberately. If the DuckDB write fails — the file is locked by a
    running refresh, the disk is full — the typing is still on disk and the next
    refresh replays it. The reverse order would lose it.
    """
    from . import store  # local: keeps the module importable without duckdb

    if entries.empty:
        raise ManualError("nothing to save")

    existing = read_manual(spec.series_id, manual_dir)
    incoming = entries.loc[:, ["obs_date", "value", "source_ref"]].copy()
    incoming["obs_date"] = pd.to_datetime(incoming["obs_date"])
    incoming["series_id"] = spec.series_id
    incoming["entered_at"] = datetime.now().isoformat(timespec="seconds")

    merged = pd.concat([existing, incoming.loc[:, MANUAL_COLUMNS]], ignore_index=True)
    merged = merged.drop_duplicates(subset=["obs_date"], keep="last")
    path = write_manual(spec.series_id, merged, manual_dir)

    # The whole merged file goes to the store, not just the rows typed this
    # time. Together with the prune below it makes the store an exact projection
    # of the CSV — the same state a rebuild would produce — instead of a running
    # total of everything ever entered.
    #
    # The scale factor belongs to the catalog, not to the typist: a value is
    # entered as the publication prints it and scaled on the way in, exactly as
    # every fetcher's output is. It is therefore applied here and NOT stored in
    # the CSV, so the CSV keeps matching the source document.
    to_store = merged.loc[:, ["series_id", "obs_date", "value"]].copy()
    to_store["obs_date"] = pd.to_datetime(to_store["obs_date"])
    if spec.scale and spec.scale != 1.0:
        to_store["value"] = to_store["value"] * spec.scale

    run_id = store.new_run_id()
    started = datetime.now()
    with store.connect(db_path=db_path) as con:
        # Only for series whose history is entirely ours. On an API-backed
        # series the store holds a vendor history this file knows nothing about,
        # and pruning to the file would delete all of it.
        removed = (
            store.prune_observations(con, spec.series_id, to_store["obs_date"])
            if spec.source in MANUAL_SOURCES
            else 0
        )
        added, updated = store.upsert_observations(con, to_store, run_id=run_id)
        # Logged like any other source, so hand entry shows up in the Home
        # "what changed this week" feed instead of appearing from nowhere.
        store.log_refresh(
            con,
            [
                {
                    "run_id": run_id,
                    "series_id": spec.series_id,
                    "source": "manual",
                    "started_at": started,
                    "finished_at": datetime.now(),
                    "status": "ok" if (added or updated) else "no_new",
                    "rows_added": added,
                    "rows_updated": updated,
                    "last_obs_date": pd.Timestamp(to_store["obs_date"].max()).date(),
                    "message": (
                        f"entered through the Data Manager"
                        + (f"; {removed} stale row(s) pruned" if removed else "")
                    ),
                }
            ],
        )

    return SaveResult(
        series_id=spec.series_id,
        csv_path=path,
        rows_added=added,
        rows_updated=updated,
        total_manual_rows=len(merged),
        rows_removed=removed,
    )
