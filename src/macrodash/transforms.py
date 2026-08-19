"""Standard macro transforms.

All of these take and return a date-indexed pandas Series.

Transforms are **calendar-based, not position-based**. YoY on monthly data means
"the observation dated twelve months earlier", not "twelve rows back". The
distinction is not academic: US CPI has no observation for October 2025, and a
row-counting YoY silently compares July 2026 against June 2025 instead of July
2025 — a 0.24pp error that looks entirely plausible on a chart. Where the base
period genuinely has no observation, the result is NaN, which is the honest
answer.

Daily and weekly series match to the nearest date within a tolerance, since
markets do not trade on the anniversary of every date.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRANSFORM_LABELS = {
    "level": "Level",
    "yoy": "YoY %",
    "mom": "Period-on-period %",
    "qoq_saar": "QoQ % annualised",
    "ann_3m": "3m/3m % annualised",
}

#: One period, per catalog frequency.
PERIOD_OFFSET = {
    "D": lambda n: pd.DateOffset(days=n),
    "W": lambda n: pd.DateOffset(weeks=n),
    "M": lambda n: pd.DateOffset(months=n),
    "Q": lambda n: pd.DateOffset(months=3 * n),
    "A": lambda n: pd.DateOffset(years=n),
}

#: How far off an exact anniversary a match may be. Only irregular frequencies
#: need this — a monthly series either has the month or it does not.
MATCH_TOLERANCE = {"D": pd.Timedelta(days=5), "W": pd.Timedelta(days=10)}


def _check_freq(freq: str) -> None:
    if freq not in PERIOD_OFFSET:
        raise ValueError(f"unknown frequency {freq!r}; expected one of {list(PERIOD_OFFSET)}")


def _lagged(series: pd.Series, offset: pd.DateOffset, freq: str) -> pd.Series:
    """The value from `offset` earlier, aligned to the current index by date."""
    prior = series.sort_index()
    prior = prior[~prior.index.duplicated(keep="last")]
    prior = pd.Series(prior.to_numpy(), index=prior.index + offset)
    prior = prior[~prior.index.duplicated(keep="last")]

    tolerance = MATCH_TOLERANCE.get(freq)
    if tolerance is not None:
        return prior.reindex(series.index, method="nearest", tolerance=tolerance)
    return prior.reindex(series.index)


def _percent_change(series: pd.Series, offset: pd.DateOffset, freq: str) -> pd.Series:
    base = _lagged(series, offset, freq)
    return (series / base.replace(0, np.nan) - 1.0) * 100.0


def level(series: pd.Series, freq: str = "M") -> pd.Series:
    return series


def yoy(series: pd.Series, freq: str = "M") -> pd.Series:
    """Percent change against the observation dated one year earlier."""
    _check_freq(freq)
    return _percent_change(series, pd.DateOffset(years=1), freq)


def mom(series: pd.Series, freq: str = "M") -> pd.Series:
    """Percent change against one period earlier, per the series' own frequency."""
    _check_freq(freq)
    return _percent_change(series, PERIOD_OFFSET[freq](1), freq)


def qoq_saar(series: pd.Series, freq: str = "Q") -> pd.Series:
    """Quarterly change expressed as an annual rate — the US convention for GDP."""
    _check_freq(freq)
    growth = _percent_change(series, pd.DateOffset(months=3), freq) / 100.0
    return ((1.0 + growth) ** 4 - 1.0) * 100.0


def ann_3m(series: pd.Series, freq: str = "M") -> pd.Series:
    """Three-month change annualised. Catches inflation turns long before YoY does."""
    _check_freq(freq)
    growth = _percent_change(series, pd.DateOffset(months=3), freq) / 100.0
    return ((1.0 + growth) ** 4 - 1.0) * 100.0


TRANSFORMS = {
    "level": level,
    "yoy": yoy,
    "mom": mom,
    "qoq_saar": qoq_saar,
    "ann_3m": ann_3m,
}


def apply_transform(series: pd.Series, name: str, freq: str = "M") -> pd.Series:
    try:
        func = TRANSFORMS[name]
    except KeyError as exc:
        raise ValueError(f"unknown transform {name!r}; expected one of {list(TRANSFORMS)}") from exc
    out = func(series, freq)
    out.name = series.name
    return out


# --------------------------------------------------------------------------
# comparison helpers
# --------------------------------------------------------------------------

def rebase(series: pd.Series, base_date: str | pd.Timestamp, base_value: float = 100.0) -> pd.Series:
    """Rescale so the base date equals base_value. For comparing shapes, not levels."""
    stamp = pd.Timestamp(base_date)
    trimmed = series.loc[series.index >= stamp].dropna()
    if trimmed.empty:
        return pd.Series(dtype="float64", name=series.name)
    anchor = trimmed.iloc[0]
    if anchor == 0 or pd.isna(anchor):
        return pd.Series(dtype="float64", name=series.name)
    out = series / anchor * base_value
    out.name = series.name
    return out


def zscore(series: pd.Series, window: int | None = None) -> pd.Series:
    """Standardised score — full-sample by default, rolling if a window is given."""
    if window:
        min_periods = max(2, window // 2)
        mean = series.rolling(window, min_periods=min_periods).mean()
        std = series.rolling(window, min_periods=min_periods).std().replace(0, np.nan)
    else:
        mean = series.mean()
        std = series.std()
        if not std:
            std = np.nan
    out = (series - mean) / std
    out.name = series.name
    return out


def align(*series: pd.Series) -> pd.DataFrame:
    """Join series on their dates. Mixed frequencies are forward-filled to the finest."""
    frame = pd.concat(series, axis=1, sort=False).sort_index()
    return frame.ffill()


def real_rate(nominal: pd.Series, inflation_yoy: pd.Series) -> pd.Series:
    """Policy rate less YoY inflation. Both must already be in percent."""
    joined = align(nominal.rename("nominal"), inflation_yoy.rename("inflation"))
    out = joined["nominal"] - joined["inflation"]
    out.name = f"{nominal.name} real"
    return out.dropna()


def spread(long_leg: pd.Series, short_leg: pd.Series) -> pd.Series:
    """Difference between two rate series — 2s10s and friends."""
    joined = align(long_leg.rename("long"), short_leg.rename("short"))
    out = joined["long"] - joined["short"]
    out.name = f"{long_leg.name} - {short_leg.name}"
    return out.dropna()


def contribution(component: pd.Series, weight: float) -> pd.Series:
    """Weighted contribution of a component to a headline change, in percentage points."""
    out = component * weight
    out.name = component.name
    return out


def growth_contribution(
    component: pd.Series,
    total: pd.Series,
    freq: str = "Q",
    periods: int = 1,
) -> pd.Series:
    """Percentage points of the total's YoY growth accounted for by one component.

    ``(component_t - component_{t-1yr}) / total_{t-1yr} * 100``. The denominator
    is the *total* one year earlier, not the component, which is what makes the
    results additive: contributions computed this way sum to the total's own YoY
    growth, so a stacked chart of them reconciles to the headline line drawn over
    it.

    Dividing by the component's own base instead would give its growth rate — a
    useful number, but one that cannot be stacked. Inventories make the
    difference obvious: a swing from -5 to +5 has no meaningful growth rate at
    all, yet a perfectly well-defined contribution.

    Calendar-based like every other transform here, so a missing base quarter
    yields NaN rather than a silent comparison against the wrong period. Pass a
    negated series for components that subtract, such as imports.

    ``periods`` is measured in years, matching ``yoy``; it exists so an annual
    series can be handed the same treatment.
    """
    _check_freq(freq)
    offset = pd.DateOffset(years=periods)
    base_component = _lagged(component, offset, freq)
    base_total = _lagged(total.reindex(component.index.union(total.index)).ffill(), offset, freq)
    base_total = base_total.reindex(component.index)

    out = (component - base_component) / base_total.replace(0, np.nan) * 100.0
    out.name = component.name
    return out


def contribution_table(
    components: dict[str, pd.Series],
    total: pd.Series,
    freq: str = "Q",
) -> pd.DataFrame:
    """`growth_contribution` applied across a dict of components, one column each.

    Negate a component before passing it in if it subtracts from the total.
    """
    if not components:
        return pd.DataFrame()
    columns = {
        label: growth_contribution(series, total, freq)
        for label, series in components.items()
    }
    return pd.DataFrame(columns).sort_index()

def resample_to(series: pd.Series, freq: str, how: str = "last") -> pd.Series:
    """Downsample to a coarser frequency. 'last' for stocks, 'mean' for flows."""
    rule = {"D": "D", "W": "W", "M": "ME", "Q": "QE", "A": "YE"}[freq]
    resampled = series.resample(rule)
    out = resampled.mean() if how == "mean" else resampled.last()
    out.name = series.name
    return out.dropna()
