"""Source registry.

Fetchers are constructed lazily so that a missing FRED key does not stop a
markets-only or seed-only refresh from running.
"""

from __future__ import annotations

from .base import FetchError, Fetcher, TIDY_COLUMNS, empty_frame, tidy


def get_fetcher(source: str) -> Fetcher:
    if source == "fred":
        from .fred import FredFetcher

        return FredFetcher()
    if source == "yahoo":
        from .yahoo import YahooFetcher

        return YahooFetcher()
    if source == "seed":
        from .seed import SeedFetcher

        return SeedFetcher()
    if source == "manual":
        from .manual import ManualFetcher

        return ManualFetcher()
    if source == "bps_manual":
        # Retained alias — see sources/bps_manual.py.
        from .bps_manual import BpsManualFetcher

        return BpsManualFetcher()
    if source == "bps_api":
        from .bps import BpsFetcher

        return BpsFetcher()
    raise FetchError(f"no fetcher registered for source {source!r}")


__all__ = [
    "FetchError",
    "Fetcher",
    "TIDY_COLUMNS",
    "empty_frame",
    "get_fetcher",
    "tidy",
]
