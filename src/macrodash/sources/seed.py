"""Seed data: committed CSVs that give the store real history on a fresh clone.

Any CSV in data/seed/ with columns series_id, obs_date, value is picked up.
An optional source_ref column records where each number was taken from —
required for anything transcribed by hand, so a suspicious value can always be
traced back to its publication.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from ..catalog import SeriesSpec
from ..config import SEED_DIR
from .base import FetchError, tidy

REQUIRED_COLUMNS = {"series_id", "obs_date", "value"}


@lru_cache(maxsize=1)
def load_seed_frame(seed_dir: Path | None = None) -> pd.DataFrame:
    """Concatenate every seed CSV. Cached — this is read once per process."""
    seed_dir = seed_dir or SEED_DIR
    frames = []

    for path in sorted(seed_dir.glob("*.csv")):
        frame = pd.read_csv(path)
        missing = REQUIRED_COLUMNS - set(frame.columns)
        if missing:
            raise FetchError(f"{path.name} is missing columns: {sorted(missing)}")
        frame["_file"] = path.name
        frames.append(frame)

    if not frames:
        return pd.DataFrame(columns=[*REQUIRED_COLUMNS, "source_ref", "_file"])

    combined = pd.concat(frames, ignore_index=True)
    combined["obs_date"] = pd.to_datetime(combined["obs_date"], errors="coerce")
    bad_dates = combined["obs_date"].isna().sum()
    if bad_dates:
        raise FetchError(f"{bad_dates} seed rows have unparseable dates")
    return combined


class SeedFetcher:
    name = "seed"

    def fetch(self, spec: SeriesSpec, start: str | None = None) -> pd.DataFrame:
        seed = load_seed_frame()
        rows = seed.loc[seed["series_id"] == spec.series_id]
        if rows.empty:
            raise FetchError(
                f"no seed rows for {spec.series_id} — expected a CSV in data/seed/ "
                f"containing it"
            )
        if start:
            rows = rows.loc[rows["obs_date"] >= pd.Timestamp(start)]
        return tidy(spec, rows["obs_date"], rows["value"])


def seed_coverage(seed_dir: Path | None = None) -> pd.DataFrame:
    """What the seed files actually contain — used to verify Phase 2."""
    seed = load_seed_frame(seed_dir)
    if seed.empty:
        return seed
    return (
        seed.groupby("series_id")
        .agg(
            n_obs=("value", "count"),
            first_obs=("obs_date", "min"),
            last_obs=("obs_date", "max"),
            file=("_file", "first"),
        )
        .reset_index()
    )
