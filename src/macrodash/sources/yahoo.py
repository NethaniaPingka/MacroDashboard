"""Market data via yfinance.

yfinance scrapes an undocumented endpoint, so it breaks from time to time and
its return shape varies with the ticker (tz-aware indices, occasional
MultiIndex columns). This wrapper normalises all of that and raises FetchError
on anything unexpected, so the refresher can skip one bad ticker and carry on.
"""

from __future__ import annotations

import pandas as pd

from ..catalog import SeriesSpec
from .base import FetchError, tidy

#: Which price column to store. Close is what a macro dashboard wants — these
#: are FX rates, commodity prices, and index levels, not positions to mark.
PRICE_COLUMN = "Close"


class YahooFetcher:
    name = "yahoo"

    def fetch(self, spec: SeriesSpec, start: str | None = None) -> pd.DataFrame:
        import yfinance  # imported lazily; it is slow and only markets need it

        ticker = yfinance.Ticker(spec.source_id)
        try:
            history = ticker.history(
                period="max" if not start else None,
                start=start,
                interval="1d",
                auto_adjust=True,
            )
        except Exception as exc:  # yfinance raises a wide variety of things
            raise FetchError(f"yfinance failed for {spec.source_id}: {exc}") from exc

        if history is None or history.empty:
            raise FetchError(f"yfinance returned nothing for {spec.source_id}")

        frame = self._normalise(history, spec.source_id)

        # Weekly and monthly series in the catalog get downsampled here rather
        # than storing every trading day for something charted monthly.
        if spec.frequency in ("W", "M"):
            rule = {"W": "W", "M": "ME"}[spec.frequency]
            frame = frame.resample(rule).last().dropna()

        return tidy(spec, frame.index, frame.values)

    @staticmethod
    def _normalise(history: pd.DataFrame, source_id: str) -> pd.Series:
        columns = history.columns
        if isinstance(columns, pd.MultiIndex):
            # Single-ticker downloads sometimes come back with (field, ticker).
            history = history.xs(PRICE_COLUMN, axis=1, level=0)
            series = history.iloc[:, 0]
        elif PRICE_COLUMN in columns:
            series = history[PRICE_COLUMN]
        else:
            raise FetchError(
                f"yfinance gave {source_id} columns {list(columns)}, no {PRICE_COLUMN!r}"
            )

        index = pd.DatetimeIndex(series.index)
        if index.tz is not None:
            index = index.tz_localize(None)
        series.index = index.normalize()
        return series.dropna()
