"""Hand-entered series, read back from data/manual/.

This is what makes manual entry a real source rather than a one-off write. The
Data Manager page saves to ``data/manual/<series_id>.csv``; this fetcher reads
that file on every refresh, so a manually maintained series is replayed by
``refresh_all.py``, survives ``--reset``, and rebuilds from a deleted DuckDB
file exactly like a FRED series does.

Values in the CSV are stored as the publication prints them. The catalog's
``scale`` is applied here by ``tidy``, on the same footing as every other
source — which is why the CSV can still be compared line by line against the
document it was typed from.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..catalog import SeriesSpec
from ..manual import ManualError, read_manual
from .base import FetchError, tidy


class ManualFetcher:
    name = "manual"

    def __init__(self, manual_dir: Path | None = None) -> None:
        self.manual_dir = manual_dir

    def fetch(self, spec: SeriesSpec, start: str | None = None) -> pd.DataFrame:
        try:
            rows = read_manual(spec.series_id, self.manual_dir)
        except ManualError as exc:
            raise FetchError(str(exc)) from exc

        if rows.empty:
            # Not SeriesNotFound. "Nobody has typed this yet" is a statement
            # about us, not about the series, and only SeriesNotFound justifies
            # audit_catalog.py deactivating a catalog entry.
            raise FetchError(
                f"no manual entries for {spec.series_id} — add them on the Data "
                f"Manager page, or drop a CSV in data/manual/."
            )

        if start:
            rows = rows.loc[rows["obs_date"] >= pd.Timestamp(start)]

        return tidy(spec, rows["obs_date"], rows["value"])
