"""Transforms checked against hand-computed values.

Every expected number here is worked out by hand in the docstring or comment.
A transform test that just re-implements the transform proves nothing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from macrodash.transforms import (
    align,
    ann_3m,
    apply_transform,
    contribution_table,
    growth_contribution,
    mom,
    qoq_saar,
    real_rate,
    rebase,
    resample_to,
    spread,
    yoy,
    zscore,
)


def monthly(values, start="2023-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="MS"))


def quarterly(values, start="2023-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="QS"))


def test_yoy_monthly_uses_twelve_periods():
    # 13 months of an index rising exactly 1% per month.
    # 1.01**12 = 1.126825..., so YoY at month 13 is 12.6825%.
    series = monthly([100 * 1.01**i for i in range(13)])
    result = yoy(series, "M")
    assert np.isnan(result.iloc[:12]).all(), "no YoY before a full year of history"
    assert result.iloc[12] == pytest.approx(12.68250301, rel=1e-6)


def test_yoy_quarterly_uses_four_periods():
    # 100 -> 110 over four quarters is exactly 10%.
    series = quarterly([100.0, 102.0, 104.0, 106.0, 110.0])
    assert yoy(series, "Q").iloc[4] == pytest.approx(10.0)


def test_mom_is_single_period_change():
    series = monthly([100.0, 102.0, 101.0])
    result = mom(series)
    assert result.iloc[1] == pytest.approx(2.0)
    assert result.iloc[2] == pytest.approx(-0.98039216, rel=1e-6)


def test_qoq_saar_annualises_the_quarterly_rate():
    # 1% in a quarter compounds to 1.01**4 - 1 = 4.060401%.
    series = quarterly([100.0, 101.0])
    assert qoq_saar(series).iloc[1] == pytest.approx(4.060401, rel=1e-6)


def test_ann_3m_annualises_the_three_month_change():
    # 100 -> 103 over three months; 1.03**4 - 1 = 12.550881%.
    series = monthly([100.0, 101.0, 102.0, 103.0])
    assert ann_3m(series).iloc[3] == pytest.approx(12.550881, rel=1e-6)


def test_negative_growth_annualises_correctly():
    # -2% in a quarter: 0.98**4 = 0.92236816, so a 7.763184% annualised decline.
    series = quarterly([100.0, 98.0])
    assert qoq_saar(series).iloc[1] == pytest.approx(-7.763184, rel=1e-6)


def test_yoy_is_calendar_based_not_row_based():
    """A missing month must not shift the comparison base.

    Regression for a real bug: US CPI has no October 2025 observation, and a
    row-counting YoY compared July 2026 against June 2025, overstating
    inflation by 0.24pp with no visible sign anything was wrong.
    """
    # 25 months of an index rising exactly 1% per month, then delete month 10.
    full = monthly([100 * 1.01**i for i in range(25)], start="2024-01-01")
    gapped = full.drop(full.index[9])

    result = yoy(gapped, "M")

    # Every surviving point still compares against its true anniversary, so the
    # answer stays 1.01**12 - 1 regardless of the hole.
    expected = (1.01**12 - 1) * 100
    comparable = result.dropna()
    assert len(comparable) > 0
    assert np.allclose(comparable.to_numpy(), expected), (
        "YoY must be unaffected by a gap elsewhere in the series"
    )


def test_yoy_is_nan_when_the_base_month_is_missing():
    """No base observation means no answer — not a silently wrong one."""
    full = monthly([100 * 1.01**i for i in range(25)], start="2024-01-01")
    gapped = full.drop(full.index[2])  # remove 2024-03

    result = yoy(gapped, "M")
    assert pd.isna(result.loc["2025-03-01"]), "2025-03 has no 2024-03 to compare against"


def test_mom_skips_rather_than_spans_a_gap():
    series = monthly([100.0, 101.0, 102.0, 103.0], start="2024-01-01")
    gapped = series.drop(series.index[1])  # remove 2024-02
    result = mom(gapped, "M")
    assert pd.isna(result.loc["2024-03-01"]), "March has no February to compare against"
    assert result.loc["2024-04-01"] == pytest.approx(100 * (103 / 102 - 1))


def test_daily_yoy_tolerates_non_trading_anniversaries():
    """A year ago may have been a weekend; nearest-within-tolerance still matches."""
    index = pd.bdate_range("2023-01-02", "2025-01-31")
    series = pd.Series(np.linspace(100, 200, len(index)), index=index)
    result = yoy(series, "D")
    assert result.dropna().shape[0] > 200, "daily YoY should resolve for most of year two"


def test_apply_transform_dispatches_and_keeps_name():
    series = monthly([100.0, 101.0], start="2023-01-01")
    series.name = "ID.CPI"
    assert apply_transform(series, "mom", "M").name == "ID.CPI"
    assert apply_transform(series, "level", "M").equals(series)


def test_apply_transform_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown transform"):
        apply_transform(monthly([1.0, 2.0]), "nonsense", "M")


def test_yoy_rejects_unknown_frequency():
    with pytest.raises(ValueError, match="unknown frequency"):
        yoy(monthly([1.0, 2.0]), "Z")


def test_rebase_anchors_at_the_base_date():
    series = monthly([50.0, 55.0, 60.0], start="2023-01-01")
    result = rebase(series, "2023-02-01")
    assert result.loc["2023-02-01"] == pytest.approx(100.0)
    assert result.loc["2023-03-01"] == pytest.approx(60 / 55 * 100)


def test_rebase_on_out_of_range_date_returns_empty():
    series = monthly([50.0, 55.0], start="2023-01-01")
    assert rebase(series, "2030-01-01").empty


def test_zscore_full_sample():
    series = monthly([1.0, 2.0, 3.0, 4.0, 5.0])
    result = zscore(series)
    # mean 3, sample std 1.5811; (5-3)/1.5811 = 1.2649
    assert result.iloc[2] == pytest.approx(0.0)
    assert result.iloc[4] == pytest.approx(1.264911, rel=1e-5)


def test_zscore_of_a_flat_series_is_not_infinite():
    result = zscore(monthly([2.0, 2.0, 2.0]))
    assert result.isna().all(), "zero variance must give NaN, never a divide-by-zero"


def test_real_rate_subtracts_inflation():
    rate = monthly([6.0, 6.0, 5.75]).rename("BI Rate")
    inflation = monthly([2.5, 3.0, 3.5]).rename("CPI YoY")
    result = real_rate(rate, inflation)
    assert list(result.round(2)) == [3.5, 3.0, 2.25]


def test_spread_is_long_minus_short():
    ten = monthly([4.5, 4.6]).rename("10y")
    two = monthly([4.0, 4.8]).rename("2y")
    result = spread(ten, two)
    assert result.iloc[0] == pytest.approx(0.5)
    assert result.iloc[1] == pytest.approx(-0.2), "inversion must come through negative"


def test_align_forward_fills_the_coarser_series():
    fast = pd.Series([1.0, 2.0, 3.0], index=pd.date_range("2024-01-01", periods=3, freq="MS"))
    slow = pd.Series([10.0], index=pd.to_datetime(["2024-01-01"]))
    joined = align(fast.rename("fast"), slow.rename("slow"))
    assert joined["slow"].tolist() == [10.0, 10.0, 10.0]


def test_resample_to_monthly_takes_last_by_default():
    # 2024 is a leap year: 31 days of January + 29 of February = exactly 60.
    daily = pd.Series(range(1, 61), index=pd.date_range("2024-01-01", periods=60, freq="D"))
    result = resample_to(daily.astype(float), "M")
    assert len(result) == 2
    assert result.iloc[0] == 31.0, "January should end on its last daily value"
    assert result.iloc[1] == 60.0


# ------------------------------------------------------------------ contributions

def test_growth_contribution_divides_by_the_total_not_the_component():
    """A component going 20 -> 25 inside a total going 100 -> 110.

    The component grew 25%, but it contributed (25-20)/100 = 5pp of the total's
    10% growth. Dividing by the component's own base would give 25 and make the
    stack overshoot the headline by a factor of five.
    """
    component = quarterly([20, 20, 20, 20, 25])
    total = quarterly([100, 100, 100, 100, 110])
    result = growth_contribution(component, total, "Q")
    assert result.iloc[-1] == pytest.approx(5.0)


def test_growth_contributions_sum_to_the_headline_growth_rate():
    """The property the stacked chart depends on: parts add up to the whole.

    Three components summing to the total at every date. Their contributions
    must sum to the total's own YoY, or the chart silently misattributes growth.
    """
    a = quarterly([50, 52, 54, 56, 60])
    b = quarterly([30, 30, 31, 32, 33])
    c = quarterly([20, 21, 21, 22, 24])
    total = a + b + c

    table = contribution_table({"a": a, "b": b, "c": c}, total, "Q")
    summed = table.sum(axis=1, min_count=3)
    expected = yoy(total, "Q")

    assert summed.iloc[-1] == pytest.approx(expected.iloc[-1])
    assert summed.dropna().shape[0] == 1  # only the fifth quarter has a base


def test_growth_contribution_handles_a_component_that_crosses_zero():
    """Inventories swing negative; a growth *rate* would be nonsense there.

    -5 -> +5 against a total base of 200 is (5 - -5)/200 = 5pp. A percent change
    off a negative base would come out -200% and flip the sign of the stack.
    """
    component = quarterly([-5, -5, -5, -5, 5])
    total = quarterly([200, 200, 200, 200, 210])
    result = growth_contribution(component, total, "Q")
    assert result.iloc[-1] == pytest.approx(5.0)


def test_growth_contribution_is_nan_when_the_base_year_is_missing():
    # Same calendar-based guarantee as yoy: no base quarter, no answer.
    index = pd.to_datetime(["2023-01-01", "2023-04-01", "2023-07-01", "2024-01-01"])
    component = pd.Series([20.0, 21.0, 22.0, 26.0], index=index)
    total = pd.Series([100.0, 101.0, 102.0, 110.0], index=index)

    result = growth_contribution(component, total, "Q")
    assert result.loc["2024-01-01"] == pytest.approx(6.0)  # base 2023-01 exists

    # Drop the base quarter and the answer must become NaN, not shift to 2023-04.
    gapped_component = component.drop(pd.Timestamp("2023-01-01"))
    gapped_total = total.drop(pd.Timestamp("2023-01-01"))
    gapped = growth_contribution(gapped_component, gapped_total, "Q")
    assert np.isnan(gapped.loc["2024-01-01"])


def test_negated_component_contributes_negatively():
    """Imports are stored positive and subtract, so the caller negates them.

    Imports rising 40 -> 48 against a total base of 400 must read as -2pp.
    """
    imports = quarterly([40, 40, 40, 40, 48])
    total = quarterly([400, 400, 400, 400, 420])
    result = growth_contribution(-imports, total, "Q")
    assert result.iloc[-1] == pytest.approx(-2.0)


def test_contribution_table_of_nothing_is_empty():
    assert contribution_table({}, quarterly([1, 2, 3]), "Q").empty
