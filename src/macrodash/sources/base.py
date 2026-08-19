"""The fetcher contract.

Every source — FRED, Yahoo, a BPS spreadsheet, a hand-built seed CSV — returns
the same three columns. That single agreement is what lets the rest of the app
stay ignorant of where a number came from.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from ..catalog import SeriesSpec

TIDY_COLUMNS = ["series_id", "obs_date", "value"]


class FetchError(RuntimeError):
    """Raised when a source cannot supply a series. Caught per-series by the
    refresher, so one dead ticker never takes down a whole run."""


class SeriesNotFound(FetchError):
    """The source is reachable and says this series does not exist.

    Kept distinct from a plain FetchError on purpose: only this justifies
    deactivating a catalog entry. A timeout, a rate limit, or a 502 means we
    simply do not know, and treating "do not know" as "does not exist" would
    let one bad afternoon quietly empty the catalog.
    """


class Fetcher(Protocol):
    name: str

    def fetch(self, spec: SeriesSpec, start: str | None = None) -> pd.DataFrame:
        """Return a tidy frame of series_id, obs_date, value."""
        ...


def tidy(spec: SeriesSpec, dates, values) -> pd.DataFrame:
    """Build the standard frame, applying the catalog's scale factor.

    Scale exists so a source publishing in thousands and another in millions can
    be stored in the same units without editing either fetcher.
    """
    frame = pd.DataFrame({"obs_date": pd.to_datetime(list(dates)), "value": list(values)})
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["value"])

    if spec.scale and spec.scale != 1.0:
        frame["value"] = frame["value"] * spec.scale

    frame["series_id"] = spec.series_id
    frame = frame.loc[:, TIDY_COLUMNS].sort_values("obs_date").reset_index(drop=True)
    return frame


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=TIDY_COLUMNS)
