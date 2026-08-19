"""BPS response decoding — the fiddly part, tested without touching the network.

The BPS API returns a flat dict whose keys are five ids concatenated as strings.
Getting that wrong yields a series that is silently short or silently mixed with
an annual aggregate, so it is worth pinning down precisely.
"""

from __future__ import annotations

import pandas as pd
import pytest

from macrodash.sources.base import FetchError
from macrodash.sources.bps import (
    MAX_YEARS_PER_REQUEST,
    _decode,
    _year_chunks,
    parse_source_id,
)


def payload(datacontent: dict, periods: list[tuple[int, str]], years: list[tuple[int, str]]) -> dict:
    return {
        "datacontent": datacontent,
        "turtahun": [{"val": v, "label": lab} for v, lab in periods],
        "tahun": [{"val": v, "label": lab} for v, lab in years],
    }


MONTHS = [(i, str(i)) for i in range(1, 13)] + [(13, "Tahunan")]
QUARTERS = [(31, "Triwulan I"), (32, "Triwulan II"), (33, "Triwulan III"), (34, "Triwulan IV"),
            (35, "Tahunan")]


# --------------------------------------------------------------- source_id
def test_parse_source_id_defaults_to_national_and_no_breakdown():
    assert parse_source_id("2263") == (2263, "9999", "0")


def test_parse_source_id_accepts_explicit_region_and_turvar():
    assert parse_source_id("1956/800/0") == (1956, "800", "0")
    assert parse_source_id("2263/1100") == (2263, "1100", "0")


def test_parse_source_id_rejects_non_numeric_var():
    with pytest.raises(FetchError, match="numeric var_id"):
        parse_source_id("ihk-umum")


# ------------------------------------------------------------------ chunks
def test_year_chunks_respects_the_three_year_api_limit():
    years = list(range(110, 127))  # 1990..2006 as th_ids
    chunks = _year_chunks(years, MAX_YEARS_PER_REQUEST)
    assert all(len(c) <= 3 for c in chunks)
    assert [y for c in chunks for y in c] == years, "chunking must not lose a year"


def test_year_chunks_of_a_short_range():
    assert _year_chunks([124, 125, 126], 3) == [[124, 125, 126]]


# ------------------------------------------------------------------ decode
def test_decode_builds_keys_by_concatenation():
    # 9999 + 2263 + 0 + 126 + 7  ->  "9999226301267"
    content = {"9999226301267": 2.88, "9999226301266": 3.34}
    rows = _decode(payload(content, MONTHS, [(126, "2026")]), 2263, "9999", "0")
    assert dict(rows) == {
        pd.Timestamp("2026-07-01"): 2.88,
        pd.Timestamp("2026-06-01"): 3.34,
    }


def test_decode_handles_two_digit_months():
    """Key parts are variable width, which is why keys are built not parsed."""
    # 9999|2263|0|126|12 -> 14 chars, vs 9999|2263|0|126|1 -> 13 chars.
    # Read from the right these are ambiguous, which is the whole point.
    content = {"99992263012612": 9.9, "9999226301261": 1.1}
    rows = dict(_decode(payload(content, MONTHS, [(126, "2026")]), 2263, "9999", "0"))
    assert rows[pd.Timestamp("2026-12-01")] == 9.9
    assert rows[pd.Timestamp("2026-01-01")] == 1.1


def test_decode_excludes_the_annual_aggregate():
    """turtahun 13 is the year total and must never enter a monthly series."""
    content = {"9999226301261": 1.0, "99992263012613": 99.0}
    rows = _decode(payload(content, MONTHS, [(126, "2026")]), 2263, "9999", "0")
    assert len(rows) == 1
    assert rows[0][1] == 1.0


def test_decode_excludes_the_quarterly_annual_aggregate():
    """Quarterly variables use 35, not 13, for their annual total."""
    content = {"800195601263 1".replace(" ", ""): 100.0, "80019560126335": 999.0}
    rows = _decode(payload(content, QUARTERS, [(126, "2026")]), 1956, "800", "0")
    assert all(value != 999.0 for _, value in rows)


def test_decode_maps_quarters_to_period_start_months():
    content = {
        "80019560126 31".replace(" ", ""): 10.0,
        "80019560126 34".replace(" ", ""): 40.0,
    }
    rows = dict(_decode(payload(content, QUARTERS, [(126, "2026")]), 1956, "800", "0"))
    assert rows[pd.Timestamp("2026-01-01")] == 10.0
    assert rows[pd.Timestamp("2026-10-01")] == 40.0


def test_decode_ignores_other_regions():
    """A national request must not pick up provincial rows."""
    content = {"9999226301267": 2.88, "1100226301267": 4.20}
    rows = _decode(payload(content, MONTHS, [(126, "2026")]), 2263, "9999", "0")
    assert len(rows) == 1
    assert rows[0][1] == 2.88


def test_decode_of_empty_content_is_empty():
    assert _decode(payload({}, MONTHS, [(126, "2026")]), 2263, "9999", "0") == []
