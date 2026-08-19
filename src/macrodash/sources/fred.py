"""FRED fetcher, built directly on the JSON API.

Using requests rather than the fredapi wrapper: the endpoint is two lines of
code, and going direct means clearer errors (FRED reports a real message on a
bad key or a retired series id) and no third-party pandas-compatibility risk.
"""

from __future__ import annotations

import time

import pandas as pd
import requests

from ..catalog import SeriesSpec
from ..config import DEFAULT_HISTORY_START, FRED_BASE_URL, require_secret
from .base import FetchError, SeriesNotFound, tidy

TIMEOUT = 30
MAX_RETRIES = 3


class FredFetcher:
    name = "fred"

    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or require_secret("FRED_API_KEY")
        self.session = session or requests.Session()

    def _get(self, endpoint: str, params: dict) -> dict:
        url = f"{FRED_BASE_URL}/{endpoint}"
        params = {**params, "api_key": self.api_key, "file_type": "json"}

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self.session.get(url, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            # Rate limits and gateway errors are transient — back off and retry.
            # FRED's edge returns 502/503 fairly often under load, and treating
            # that as a verdict on the series would be wrong.
            if response.status_code == 429 or response.status_code >= 500:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                time.sleep(2.0 * (attempt + 1))
                continue

            if response.status_code >= 400:
                # FRED puts a human-readable reason in the body; surface it.
                detail = response.text[:300]
                target = params.get("series_id", "")
                if "does not exist" in detail.lower():
                    raise SeriesNotFound(f"FRED has no series {target!r}")
                raise FetchError(f"FRED {response.status_code} for {target}: {detail}")

            return response.json()

        raise FetchError(
            f"FRED unreachable for {params.get('series_id', '')} after "
            f"{MAX_RETRIES} attempts: {last_error}"
        )

    def fetch(self, spec: SeriesSpec, start: str | None = None) -> pd.DataFrame:
        payload = self._get(
            "series/observations",
            {
                "series_id": spec.source_id,
                "observation_start": start or DEFAULT_HISTORY_START,
            },
        )
        observations = payload.get("observations", [])
        if not observations:
            raise FetchError(f"FRED returned no observations for {spec.source_id}")

        frame = pd.DataFrame(observations)
        # FRED encodes missing values as "." — to_numeric turns those into NaN,
        # and tidy() drops them.
        return tidy(spec, frame["date"], frame["value"])

    def describe(self, source_id: str) -> dict:
        """Series metadata — title, units, frequency, last update.

        Used by the catalog audit script to confirm a series id is still live
        and that our YAML frequency matches what FRED actually publishes.
        """
        payload = self._get("series", {"series_id": source_id})
        entries = payload.get("seriess", [])
        if not entries:
            raise SeriesNotFound(f"FRED has no series {source_id!r}")
        return entries[0]
