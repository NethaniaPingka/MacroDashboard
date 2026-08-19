"""The pure half of charts.py — formatting, windowing, colour, figure shape.

Everything here runs without a Streamlit runtime. The rendering half is covered
by tests/test_app.py, which drives the real pages.
"""

from __future__ import annotations

import pandas as pd
import pytest

from macrodash.catalog import Catalog, SeriesSpec
from macrodash.charts import (
    COMPONENT_COLORS,
    TABS,
    Kpi,
    bar_figure,
    format_change,
    format_period,
    format_value,
    heatmap_figure,
    is_fx_unit,
    is_rate_unit,
    line_figure,
    series_colors,
    slice_dates,
    stacked_contribution_figure,
    transform_frame,
)
from macrodash.config import COUNTRY_COLORS


def monthly(values, start="2024-01-01"):
    return pd.Series(
        values, index=pd.date_range(start, periods=len(values), freq="MS"), dtype="float64"
    )


def spec(series_id, country, frequency="M"):
    return SeriesSpec(
        series_id=series_id,
        name=series_id,
        country=country,
        category="inflation",
        source="fred",
        source_id="X",
        frequency=frequency,
    )


# ----------------------------------------------------------------- formatting

def test_rate_units_are_recognised():
    assert is_rate_unit("Percent")
    assert is_rate_unit("Percentage points, SAAR")
    assert not is_rate_unit("Bn IDR, constant 2010 prices")
    assert not is_rate_unit("")


def test_format_value_picks_precision_from_magnitude():
    assert format_value(17832.0, "IDR per USD") == "17,832"      # >= 10k: no decimals
    assert format_value(6401.888, "Index") == "6,401.9"          # thousands: one
    assert format_value(4.63, "Index") == "4.63"                 # units: two
    assert format_value(0.8642, "Index") == "0.8642"             # sub-unit: four


def test_fx_crosses_keep_the_decimals_they_move_in():
    """EUR/USD at 1.1577 must not print as "1.16".

    Magnitude alone would give it two decimals like any other small number, and
    the two digits it loses are the ones the rate actually trades in. Crosses
    quoted in the thousands are unaffected — a rupiah figure gains nothing from
    decimals.
    """
    assert format_value(1.157675, "USD per EUR") == "1.1577"
    assert format_value(17832.0, "IDR per USD") == "17,832"
    assert not is_fx_unit("Index")


def test_format_value_suffixes_rates():
    assert format_value(2.88, "Percent") == "2.88%"
    assert format_value(-0.51, "Percentage points") == "-0.51 pp"


def test_format_value_of_nothing_is_a_dash():
    assert format_value(None) == "—"
    assert format_value(float("nan")) == "—"


def test_change_on_a_rate_is_percentage_points_not_percent():
    """Inflation 3.34 -> 2.88 fell 0.46pp. Calling it -13.77% would be arithmetic
    applied to a number that is already a rate — true of the digits, wrong about
    the economics, and the kind of thing that ends up in a headline."""
    assert format_change(2.88, 3.34, "Percent") == "-0.46 pp"


def test_change_on_a_level_is_growth():
    assert format_change(110.0, 100.0, "Bn USD") == "+10.00%"


def test_change_from_zero_falls_back_to_difference():
    # No percent change is definable from a zero base; report the move instead.
    assert format_change(2.0, 0.0, "Mn USD") == "+2.00"


def test_change_needs_both_sides():
    assert format_change(1.0, None, "Percent") == ""
    assert format_change(float("nan"), 1.0, "Percent") == ""


def test_period_is_labelled_by_the_period_it_covers():
    """A quarterly value dated 2026-04-01 is Q2 2026, not "April"."""
    assert format_period(pd.Timestamp("2026-04-01"), "Q") == "Q2 2026"
    assert format_period(pd.Timestamp("2026-04-01"), "M") == "Apr 2026"
    assert format_period(pd.Timestamp("2026-04-01"), "A") == "2026"
    assert format_period(pd.Timestamp("2026-04-01"), "D") == "01 Apr 2026"
    assert format_period(None) == "—"


# ------------------------------------------------------------------------ Kpi

def test_kpi_direction_and_derived_text():
    kpi = Kpi(
        series_id="ID.CPI.INFLATION.YOY",
        label="Inflation",
        unit="Percent",
        frequency="M",
        current=2.88,
        previous=3.34,
        current_period="Jul 2026",
        previous_period="Jun 2026",
    )
    assert kpi.direction == "down"
    assert kpi.value_text == "2.88%"
    assert kpi.previous_text == "3.34%"
    assert kpi.change_text == "-0.46 pp"


def test_kpi_with_no_comparison_is_flat_rather_than_wrong():
    kpi = Kpi("X", "X", "Percent", "M", 2.0, None, "Jul 2026", "—")
    assert kpi.direction == "flat"
    assert kpi.change_text == ""


# --------------------------------------------------------------------- slicing

def test_slice_dates_is_inclusive_at_both_ends():
    frame = monthly(range(12)).to_frame("v")
    windowed = slice_dates(frame, "2024-03-01", "2024-05-01")
    assert len(windowed) == 3
    assert windowed.index[0] == pd.Timestamp("2024-03-01")
    assert windowed.index[-1] == pd.Timestamp("2024-05-01")


def test_slice_dates_tolerates_open_bounds_and_empty_frames():
    frame = monthly(range(4)).to_frame("v")
    assert len(slice_dates(frame, None, None)) == 4
    assert slice_dates(pd.DataFrame(), "2024-01-01").empty


def test_transform_frame_uses_each_column_own_frequency():
    """A quarterly and a monthly column in one frame must not share an offset.

    Both columns rise 1% per period. `mom` on the monthly column compares against
    one month back and on the quarterly column against one quarter back, so both
    read 1% — a shared monthly offset would leave the quarterly column all NaN.
    """
    index_m = pd.date_range("2024-01-01", periods=6, freq="MS")
    index_q = pd.date_range("2024-01-01", periods=6, freq="QS")
    frame = pd.concat(
        [
            pd.Series([100 * 1.01**i for i in range(6)], index=index_m, name="M"),
            pd.Series([200 * 1.01**i for i in range(6)], index=index_q, name="Q"),
        ],
        axis=1,
    )
    out = transform_frame(frame, "mom", {"M": "M", "Q": "Q"})
    assert out["M"].dropna().iloc[-1] == pytest.approx(1.0)
    assert out["Q"].dropna().iloc[-1] == pytest.approx(1.0)


def test_transform_frame_passes_levels_through_untouched():
    frame = monthly(range(4)).to_frame("v")
    assert transform_frame(frame, "level", {"v": "M"}).equals(frame)


# ---------------------------------------------------------------------- colour

def test_colours_follow_country_when_countries_differ():
    catalog = Catalog({"A": spec("A", "ID"), "B": spec("B", "US")})
    colors = series_colors(["A", "B"], catalog)
    assert colors["A"] == COUNTRY_COLORS["ID"]
    assert colors["B"] == COUNTRY_COLORS["US"]


def test_colours_fall_back_to_the_palette_within_one_country():
    """Four US series would otherwise be four identical reds."""
    catalog = Catalog({name: spec(name, "US") for name in "ABCD"})
    colors = series_colors(list("ABCD"), catalog)
    assert len(set(colors.values())) == 4
    assert list(colors.values()) == list(COMPONENT_COLORS[:4])


# --------------------------------------------------------------------- figures

def test_line_figure_drops_empty_columns_but_keeps_gaps():
    frame = pd.DataFrame(
        {
            "kept": monthly([1.0, float("nan"), 3.0]),
            "empty": monthly([float("nan")] * 3),
        }
    )
    figure = line_figure(frame, labels={"kept": "Kept"})
    assert [trace.name for trace in figure.data] == ["Kept"]
    # connectgaps must stay off: a bridged gap invents observations.
    assert figure.data[0].connectgaps is False


def test_stacked_figure_overlays_the_headline_it_reconciles_to():
    contributions = pd.DataFrame(
        {"a": monthly([1.0, 1.5]), "b": monthly([0.5, 0.5])}
    )
    headline = monthly([1.5, 2.0])
    figure = stacked_contribution_figure(contributions, headline, "Total")

    assert figure.layout.barmode == "relative"
    assert [trace.type for trace in figure.data] == ["bar", "bar", "scatter"]
    assert figure.data[-1].name == "Total"


def test_stacked_figure_without_a_headline_draws_bars_only():
    figure = stacked_contribution_figure(pd.DataFrame({"a": monthly([1.0, 2.0])}))
    assert [trace.type for trace in figure.data] == ["bar"]


def test_bar_figure_colours_by_sign():
    figure = bar_figure(monthly([1.0, -1.0, 2.0]), name="MoM")
    assert list(figure.data[0].marker.color) == ["#4C8DF6", "#E4572E", "#4C8DF6"]


def test_heatmap_centres_its_scale_on_zero():
    # A diverging scale that is not centred would paint a +0.1 print as "hot".
    grid = pd.DataFrame({"Jan": [0.5, -0.2], "Feb": [0.1, 0.3]}, index=[2025, 2026])
    figure = heatmap_figure(grid)
    assert figure.data[0].zmid == 0


def test_figures_survive_having_nothing_to_draw():
    empty = pd.Series(dtype="float64")
    assert len(line_figure(pd.DataFrame()).data) == 0
    assert len(bar_figure(empty).data) == 1  # one empty trace, not a crash
    assert len(stacked_contribution_figure(pd.DataFrame()).data) == 0


# ----------------------------------------------------------------------- layout

def test_tab_order_is_fixed_for_both_context_pages():
    """The user's requirement, encoded once so the two pages cannot drift apart."""
    assert TABS == ("Inflation", "Growth", "Rates & Money", "External & Markets")
