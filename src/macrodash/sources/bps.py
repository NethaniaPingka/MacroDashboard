"""BPS (Statistics Indonesia) WebAPI.

Written directly against the REST endpoints rather than using the official
`stadata` package, which hard-caps pandas<3 and lxml<5 and declares pytest as a
runtime dependency — adopting it would have dictated this project's pandas and
test-framework versions for the sake of a thin JSON wrapper.

The API's data model takes some explaining. A response carries five dimensions:

    vervar     region      9999 is INDONESIA; provinces are 1100, 1200, ...
    var        variable    the indicator itself
    turvar     derived     a sub-breakdown; "0" means none
    tahun      year        th_id = calendar year - 1900
    turtahun   period      1..12 for months, 13 for the annual figure

and a flat `datacontent` dict whose keys are those five ids concatenated as
strings — `9999` + `2263` + `0` + `126` + `7` = `"9999226301267"`. Keys are
*constructed* here rather than parsed, because the parts are variable width
(month 7 is one character, month 12 is two) and parsing from the right is
ambiguous.

One thing to know before trusting a chart: BPS rebases the CPI every few years
and issues a NEW var_id each time, retiring the old one. The headline index has
been 2012=100 (var 2, ends 2019), 2018=100, and now 2022=100 (starts 2024). No
single var_id spans the whole history.
"""

from __future__ import annotations

import time
from functools import lru_cache

import pandas as pd
import requests

from ..catalog import SeriesSpec
from ..config import require_secret
from .base import FetchError, SeriesNotFound, tidy

BASE_URL = "https://webapi.bps.go.id/v1/api/list"
NATIONAL = "9999"
TIMEOUT = 60
MAX_RETRIES = 3

#: turtahun ids 1-12 are calendar months, 31-34 are quarters.
MONTH_PERIODS = {i: i for i in range(1, 13)}
QUARTER_PERIODS = {31: 1, 32: 2, 33: 3, 34: 4}

#: The annual aggregate, which must never be mixed into a periodic series. Its
#: id depends on the variable's own frequency: 13 alongside months, 35 alongside
#: quarters. Both are excluded by the whitelists above, but they are named here
#: so the exclusion is deliberate rather than incidental.
ANNUAL_PERIODS = {13, 35}

#: BPS rejects any `th` range wider than this, so long histories are chunked.
MAX_YEARS_PER_REQUEST = 3


def _year_chunks(th_ids: list[int], size: int) -> list[list[int]]:
    return [th_ids[i : i + size] for i in range(0, len(th_ids), size)]


def _decode(payload: dict, var_id: int, vervar: str, turvar: str) -> list[tuple]:
    """Pull one region's series out of a response's flat datacontent dict."""
    content = payload.get("datacontent") or {}
    if not content:
        return []

    periods = {p["val"]: p["label"] for p in payload.get("turtahun", [])}
    rows: list[tuple[pd.Timestamp, float]] = []

    for year_entry in payload.get("tahun", []):
        th_id = year_entry["val"]
        year = int(year_entry["label"])
        for period_id in periods:
            if period_id in ANNUAL_PERIODS:
                continue  # never mix the annual figure into a periodic series

            # Keys are constructed, not parsed: the parts are variable width
            # (month 7 is one character, month 12 is two), so reading a key
            # from the right is ambiguous.
            key = f"{vervar}{var_id}{turvar}{th_id}{period_id}"
            if key not in content:
                continue

            if period_id in MONTH_PERIODS:
                date = pd.Timestamp(year=year, month=MONTH_PERIODS[period_id], day=1)
            elif period_id in QUARTER_PERIODS:
                date = pd.Timestamp(year=year, month=QUARTER_PERIODS[period_id] * 3 - 2, day=1)
            else:
                continue

            rows.append((date, content[key]))

    return rows


class BpsClient:
    """Thin client over the BPS WebAPI list endpoints."""

    def __init__(self, app_key: str | None = None, session: requests.Session | None = None):
        self.app_key = app_key or require_secret("BPS_APP_KEY")
        self.session = session or requests.Session()

    # ---------------------------------------------------------------- http
    def _get(self, model: str, extra: str = "", page: int | None = None) -> dict:
        url = f"{BASE_URL}/model/{model}/domain/0000{extra}/key/{self.app_key}"
        if page is not None:
            url += f"/page/{page}"

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self.session.get(url, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            if response.status_code >= 500:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                time.sleep(2.0 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise FetchError(f"BPS {response.status_code}: {response.text[:200]}")

            payload = response.json()
            if payload.get("status") == "Error":
                message = str(payload.get("message", ""))
                # BPS returns status=Error with HTTP 200 for everything, so the
                # message text is the only signal. A malformed request is our
                # bug and must NOT be reported as a missing series — that would
                # let audit --fix-inactive deactivate a perfectly good variable.
                lowered = message.lower()
                if "parameter" in lowered or "maximum allowed" in lowered:
                    raise FetchError(f"BPS rejected the request: {message}")
                raise SeriesNotFound(f"BPS: {message}")
            return payload

        raise FetchError(f"BPS unreachable after {MAX_RETRIES} attempts: {last_error}")

    def _paged(self, model: str, extra: str = "") -> list[dict]:
        """Walk every page of a list endpoint. Pages are 10 records each."""
        records: list[dict] = []
        page = 1
        while True:
            payload = self._get(model, extra, page=page)
            data = payload.get("data")
            if not isinstance(data, list) or len(data) < 2:
                break
            records.extend(data[1])
            if page >= data[0].get("pages", 1):
                break
            page += 1
        return records

    # ------------------------------------------------------------ metadata
    def list_subjects(self) -> dict[int, str]:
        return {r["sub_id"]: r["title"] for r in self._paged("subject")}

    def list_variables(self, subject: int | None = None) -> list[dict]:
        extra = f"/subject/{subject}" if subject else ""
        return self._paged("var", extra)

    @lru_cache(maxsize=128)
    def list_years(self, var_id: int) -> dict[int, str]:
        """{th_id: "2026"} for a variable. th_id is the calendar year - 1900."""
        return {r["th_id"]: r["th"] for r in self._paged("th", f"/var/{var_id}")}

    # ---------------------------------------------------------------- data
    def fetch_raw(self, var_id: int, th_from: int, th_to: int) -> dict:
        return self._get("data", f"/var/{var_id}/th/{th_from}:{th_to}")

    def observations(
        self,
        var_id: int,
        vervar: str = NATIONAL,
        turvar: str = "0",
    ) -> pd.DataFrame:
        """All available observations for one variable, as date/value rows.

        Requests are chunked: BPS refuses any `th` range wider than three years,
        so a variable with eighteen years of history takes six calls.
        """
        years = self.list_years(var_id)
        if not years:
            raise SeriesNotFound(f"BPS variable {var_id} reports no years")

        rows: list[tuple[pd.Timestamp, float]] = []
        for chunk in _year_chunks(sorted(years), MAX_YEARS_PER_REQUEST):
            payload = self.fetch_raw(var_id, chunk[0], chunk[-1])
            rows.extend(_decode(payload, var_id, vervar, turvar))

        if not rows:
            raise SeriesNotFound(
                f"BPS variable {var_id} has no observations for vervar={vervar}, "
                f"turvar={turvar} — check the region and breakdown ids"
            )

        frame = pd.DataFrame(rows, columns=["obs_date", "value"]).sort_values("obs_date")
        return frame.drop_duplicates(subset="obs_date", keep="last").reset_index(drop=True)

    def describe(self, var_id: int) -> dict:
        """Everything needed to decide whether a variable belongs in the catalog."""
        years = self.list_years(var_id)
        # Structure only, so the most recent chunk is enough and avoids paying
        # for the whole history just to list the dimensions.
        recent = sorted(years)[-MAX_YEARS_PER_REQUEST:]
        payload = self.fetch_raw(var_id, recent[0], recent[-1]) if years else {}
        content = payload.get("datacontent") or {}
        regions = payload.get("vervar", [])
        meta = (payload.get("var") or [{}])[0]
        periods = {p["val"]: p["label"] for p in payload.get("turtahun", [])}

        try:
            national = self.observations(var_id)
            sample = [(str(d.date()), v) for d, v in national.tail(6).to_numpy()]
            n_national = len(national)
        except FetchError:
            sample, n_national = [], 0

        labels = sorted(int(y) for y in years.values()) if years else []
        return {
            "title": meta.get("label", ""),
            "unit": meta.get("unit", ""),
            "decimal": meta.get("decimal", ""),
            "subject": meta.get("subj", ""),
            "last_update": payload.get("last_update", ""),
            "year_span": f"{labels[0]}..{labels[-1]}" if labels else "none",
            "periods": periods,
            "n_regions": len(regions),
            "has_national": any(str(r.get("val")) == NATIONAL for r in regions),
            "turvar": [(t.get("val"), t.get("label")) for t in payload.get("turvar", [])],
            "n_national_obs": n_national,
            "sample": sample,
            "n_datapoints": len(content),
        }


def parse_source_id(source_id: str) -> tuple[int, str, str]:
    """Catalog `source_id` -> (var_id, vervar, turvar).

    Accepts "2263", "2263/9999", or "2263/9999/0". Region defaults to national
    and turvar to none, which is what almost every headline series wants.
    """
    parts = str(source_id).split("/")
    try:
        var_id = int(parts[0])
    except ValueError as exc:
        raise FetchError(
            f"BPS source_id must start with a numeric var_id, got {source_id!r}"
        ) from exc

    vervar = parts[1] if len(parts) > 1 and parts[1] else NATIONAL
    turvar = parts[2] if len(parts) > 2 and parts[2] else "0"
    return var_id, vervar, turvar


class BpsFetcher:
    name = "bps_api"

    def __init__(self, client: BpsClient | None = None):
        self._client = client

    @property
    def client(self) -> BpsClient:
        if self._client is None:
            self._client = BpsClient()
        return self._client

    def fetch(self, spec: SeriesSpec, start: str | None = None) -> pd.DataFrame:
        var_id, vervar, turvar = parse_source_id(spec.source_id)
        frame = self.client.observations(var_id, vervar=vervar, turvar=turvar)
        if start:
            frame = frame.loc[frame["obs_date"] >= pd.Timestamp(start)]
        return tidy(spec, frame["obs_date"], frame["value"])
