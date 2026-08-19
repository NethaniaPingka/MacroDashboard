"""The shared vocabulary every page is drawn with: cards, charts, and slicers.

Both context pages are built from this module rather than from Streamlit
directly, so the Indonesia and Global tabs stay genuinely identical in layout
instead of drifting apart as one gets a tweak the other never receives. A tab
is three bands, always in this order:

    kpi_row(...)          four big cards — current, previous, change
    ...explaining charts  the series behind those four numbers
    ...detail             breakdowns and tables, folded away by default

The module splits deliberately into a pure half and a Streamlit half. Formatting
and figure construction take plain pandas and return plain figures, so they are
testable without a browser; only the `render_*` and `*_row` helpers touch `st`.

This is the one place in the codebase allowed to import Streamlit besides the
pages themselves — `config.py` and everything under `sources/` must keep running
from Task Scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from . import store, transforms
from .catalog import Catalog, SeriesSpec, load_catalog
from .config import COUNTRY_COLORS, STALENESS_DAYS

#: Cache lifetime for every query below. The refresher runs at most daily, so a
#: five-minute window costs nothing in staleness and keeps tab-switching instant.
CACHE_TTL = 300

#: Categorical palette for breakdowns, where the distinction is between
#: components of one country rather than between countries. Ordered so adjacent
#: slices of a stack stay distinguishable.
COMPONENT_COLORS = (
    "#4C8DF6", "#E4572E", "#17BEBB", "#F2B705", "#9B5DE5",
    "#00BB7E", "#F15BB5", "#7A8B99", "#FF8C42", "#5C7AEA",
)

#: Grey for a reference line that is context rather than a series in its own
#: right — the headline drawn over a stack of contributions.
REFERENCE_LINE = "#6B7280"

STATUS_BADGE = {
    "fresh": ("🟢", "fresh"),
    "ageing": ("🟡", "ageing"),
    "stale": ("🔴", "stale"),
    "empty": ("⚪", "no data"),
}

#: The four indicator groups, in the order every context page must present them.
#: Defined here rather than in the pages so the two cannot drift apart — flipping
#: between Indonesia and Global is only a comparison if tab three means the same
#: thing on both.
TABS = ("Inflation", "Growth", "Rates & Money", "External & Markets")

DATE_PRESETS = {
    "1Y": 365,
    "3Y": 3 * 365,
    "5Y": 5 * 365,
    "10Y": 10 * 365,
    "Max": None,
}


# ==========================================================================
# pure helpers — no Streamlit, so tests can reach them
# ==========================================================================

def is_rate_unit(unit: str) -> bool:
    """True for series already expressed in percent or percentage points.

    These are the series where a *change* means arithmetic difference, not
    percent growth: inflation going 3.34 -> 2.88 fell 0.46pp, and calling that
    -13.8% would be true of the number but wrong about the economics.
    """
    lowered = (unit or "").lower()
    return "percent" in lowered or lowered.startswith("pp")


def is_fx_unit(unit: str) -> bool:
    """True for a currency cross, whose unit reads "X per Y"."""
    return " per " in (unit or "").lower()


def format_value(value: float | None, unit: str = "") -> str:
    """A number sized for a card, with precision chosen from its magnitude.

    FX crosses are the exception the magnitude rule gets wrong on its own:
    EUR/USD at 1.1577 rounds to "1.16", which hides the two decimal places the
    rate actually moves in. Anything quoted as "X per Y" and smaller than ten
    therefore keeps four.
    """
    if value is None or pd.isna(value):
        return "—"

    if is_rate_unit(unit):
        suffix = " pp" if "point" in (unit or "").lower() else "%"
        return f"{value:,.2f}{suffix}"

    magnitude = abs(value)
    if is_fx_unit(unit) and magnitude < 10:
        return f"{value:,.4f}"
    if magnitude >= 10_000:
        return f"{value:,.0f}"
    if magnitude >= 1_000:
        return f"{value:,.1f}"
    if magnitude >= 1:
        return f"{value:,.2f}"
    return f"{value:,.4f}"


def format_change(current: float | None, previous: float | None, unit: str = "") -> str:
    """The card's change line: percentage points for rates, growth for levels."""
    if current is None or previous is None or pd.isna(current) or pd.isna(previous):
        return ""

    if is_rate_unit(unit):
        return f"{current - previous:+,.2f} pp"

    if previous == 0:
        return f"{current - previous:+,.2f}"

    return f"{(current / previous - 1.0) * 100.0:+,.2f}%"


def format_period(stamp, frequency: str = "M") -> str:
    """Label an observation by the period it covers, not its raw start date.

    A quarterly value dated 2026-04-01 is Q2 2026; printing "2026-04-01" on a
    card invites reading it as an April number.
    """
    if stamp is None or pd.isna(stamp):
        return "—"
    stamp = pd.Timestamp(stamp)
    if frequency == "Q":
        return f"Q{stamp.quarter} {stamp.year}"
    if frequency == "M":
        return stamp.strftime("%b %Y")
    if frequency == "A":
        return str(stamp.year)
    return stamp.strftime("%d %b %Y")


@dataclass(frozen=True)
class Kpi:
    """One card's worth of state, resolved from the catalog and the store."""

    series_id: str
    label: str
    unit: str
    frequency: str
    current: float | None
    previous: float | None
    current_period: str
    previous_period: str
    status: str = "fresh"
    caption: str = ""

    @property
    def value_text(self) -> str:
        return format_value(self.current, self.unit)

    @property
    def previous_text(self) -> str:
        return format_value(self.previous, self.unit)

    @property
    def change_text(self) -> str:
        return format_change(self.current, self.previous, self.unit)

    @property
    def direction(self) -> str:
        """up / down / flat — drives the arrow and colour, never the wording."""
        if self.current is None or self.previous is None:
            return "flat"
        if pd.isna(self.current) or pd.isna(self.previous):
            return "flat"
        if self.current > self.previous:
            return "up"
        if self.current < self.previous:
            return "down"
        return "flat"


def build_kpi(row: pd.Series, label: str | None = None, caption: str = "") -> Kpi:
    """Turn one `store.freshness` / `store.latest_values` row into a card."""
    frequency = row.get("frequency", "M")
    return Kpi(
        series_id=row["series_id"],
        label=label or row.get("name", row["series_id"]),
        unit=row.get("unit", "") or "",
        frequency=frequency,
        current=row.get("last_value"),
        previous=row.get("prev_value"),
        current_period=format_period(row.get("last_obs_date"), frequency),
        previous_period=format_period(row.get("prev_obs_date"), frequency),
        status=row.get("status", "fresh"),
        caption=caption,
    )


def slice_dates(frame: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    """Window a date-indexed frame, tolerating either bound being absent."""
    if frame.empty:
        return frame
    out = frame
    if start is not None:
        out = out.loc[out.index >= pd.Timestamp(start)]
    if end is not None:
        out = out.loc[out.index <= pd.Timestamp(end)]
    return out


def transform_frame(
    frame: pd.DataFrame,
    transform: str,
    frequencies: dict[str, str],
) -> pd.DataFrame:
    """Apply one transform column-wise, each column at its own frequency.

    The frequency matters: `mom` on a quarterly column means the previous
    quarter, and feeding it the monthly offset would compare against a date
    that series never publishes.
    """
    if frame.empty or transform == "level":
        return frame
    return pd.DataFrame(
        {
            column: transforms.apply_transform(
                frame[column].dropna(), transform, frequencies.get(column, "M")
            )
            for column in frame.columns
        }
    ).sort_index()


def series_colors(series_ids, catalog: Catalog) -> dict[str, str]:
    """Colour by country where countries differ, by position where they do not.

    A US-versus-euro-area chart should read as two countries, so it takes
    COUNTRY_COLORS. A chart of four US series would then be four identical reds,
    so in that case the categorical palette takes over.
    """
    countries = []
    for series_id in series_ids:
        spec = catalog.get(series_id)
        countries.append(spec.country if spec else "")

    if len(set(countries)) > 1:
        return {
            series_id: COUNTRY_COLORS.get(country, COMPONENT_COLORS[i % len(COMPONENT_COLORS)])
            for i, (series_id, country) in enumerate(zip(series_ids, countries, strict=True))
        }

    return {
        series_id: COMPONENT_COLORS[i % len(COMPONENT_COLORS)]
        for i, series_id in enumerate(series_ids)
    }


def _axis_title(unit: str, transform: str) -> str:
    if transform != "level":
        return transforms.TRANSFORM_LABELS.get(transform, transform)
    return unit or ""


def line_figure(
    frame: pd.DataFrame,
    labels: dict[str, str] | None = None,
    colors: dict[str, str] | None = None,
    unit: str = "",
    transform: str = "level",
    zero_line: bool = False,
    height: int = 380,
    step: bool | tuple[str, ...] = (),
) -> go.Figure:
    """Multi-series line chart. Gaps stay gaps — no interpolation across them.

    `step` names the columns to draw as a step ("hv"), or True for all of them.
    An administered rate holds flat between decisions and then jumps; joining
    its points with a sloping line draws a gradual move that never happened, and
    reads it back as a trend. Per-column rather than per-figure because a policy
    rate and a monthly average of a market rate legitimately share an axis and
    want different shapes.
    """
    labels = labels or {}
    colors = colors or {}
    stepped = set(frame.columns) if step is True else set(step or ())
    figure = go.Figure()

    for column in frame.columns:
        column_data = frame[column].dropna()
        if column_data.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=column_data.index,
                y=column_data.to_numpy(),
                name=labels.get(column, column),
                mode="lines",
                line={
                    "width": 2,
                    "color": colors.get(column),
                    "shape": "hv" if column in stepped else "linear",
                },
                connectgaps=False,
                hovertemplate="%{x|%b %Y}<br>%{y:,.2f}<extra>"
                + labels.get(column, column)
                + "</extra>",
            )
        )

    if zero_line:
        figure.add_hline(y=0, line_width=1, line_color=REFERENCE_LINE, opacity=0.5)

    return _style(figure, _axis_title(unit, transform), height)


def bar_figure(
    series: pd.Series,
    name: str = "",
    color: str | None = None,
    unit: str = "",
    height: int = 320,
    color_by_sign: bool = True,
) -> go.Figure:
    """Single-series bars. Sign-coloured, because a negative print is the story."""
    clean = series.dropna()
    if color_by_sign and color is None:
        marker_color = ["#E4572E" if v < 0 else "#4C8DF6" for v in clean]
    else:
        marker_color = color

    figure = go.Figure(
        go.Bar(
            x=clean.index,
            y=clean.to_numpy(),
            name=name,
            marker_color=marker_color,
            hovertemplate="%{x|%b %Y}<br>%{y:,.2f}<extra>" + name + "</extra>",
        )
    )
    figure.add_hline(y=0, line_width=1, line_color=REFERENCE_LINE, opacity=0.5)
    return _style(figure, unit, height, showlegend=False)


def stacked_contribution_figure(
    contributions: pd.DataFrame,
    headline: pd.Series | None = None,
    headline_name: str = "Total",
    unit: str = "Percentage points",
    height: int = 420,
) -> go.Figure:
    """Contributions as a stack, with the headline it reconciles to drawn over it.

    The overlaid line is the whole point: it is what makes the stack checkable by
    eye. If the bars do not reach the line, either a component is missing from
    the chart or the contribution maths is wrong — both have happened here, which
    is why `transforms.contribution_table` is tested for exactly this property.
    """
    figure = go.Figure()

    for i, column in enumerate(contributions.columns):
        column_data = contributions[column].dropna()
        if column_data.empty:
            continue
        figure.add_trace(
            go.Bar(
                x=column_data.index,
                y=column_data.to_numpy(),
                name=column,
                marker_color=COMPONENT_COLORS[i % len(COMPONENT_COLORS)],
                hovertemplate="%{x|%b %Y}<br>%{y:+,.2f} pp<extra>" + column + "</extra>",
            )
        )

    if headline is not None and not headline.dropna().empty:
        clean = headline.dropna()
        figure.add_trace(
            go.Scatter(
                x=clean.index,
                y=clean.to_numpy(),
                name=headline_name,
                mode="lines+markers",
                line={"width": 2.5, "color": REFERENCE_LINE},
                marker={"size": 5},
                hovertemplate="%{x|%b %Y}<br>%{y:,.2f}%<extra>" + headline_name + "</extra>",
            )
        )

    figure.update_layout(barmode="relative")
    figure.add_hline(y=0, line_width=1, line_color=REFERENCE_LINE, opacity=0.5)
    return _style(figure, unit, height)


def heatmap_figure(
    frame: pd.DataFrame,
    unit: str = "",
    height: int = 320,
    colorscale: str = "RdBu_r",
) -> go.Figure:
    """A year-by-period grid, for reading seasonality down a column.

    Diverging around zero on purpose: for the series this is used on — monthly
    inflation — the sign is the thing, and a sequential scale would bury it.
    """
    figure = go.Figure(
        go.Heatmap(
            z=frame.to_numpy(),
            x=list(frame.columns),
            y=[str(index) for index in frame.index],
            colorscale=colorscale,
            zmid=0,
            hovertemplate="%{y} %{x}<br>%{z:+.2f}<extra></extra>",
            colorbar={"title": unit, "thickness": 12},
            xgap=1,
            ygap=1,
        )
    )
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 20, "b": 10},
        yaxis={"autorange": "reversed", "type": "category"},
        xaxis={"side": "top"},
    )
    return figure


def dual_axis_figure(
    left: pd.Series,
    right: pd.Series,
    left_name: str,
    right_name: str,
    left_unit: str = "",
    right_unit: str = "",
    left_color: str = "#4C8DF6",
    right_color: str = "#E4572E",
    height: int = 380,
) -> go.Figure:
    """Two series on their own scales — a rate against a level, for instance.

    Used sparingly. Two axes can manufacture a correlation out of nothing by
    choice of scale, so this is for pairs whose relationship is already known,
    not for hunting new ones.
    """
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=left.dropna().index,
            y=left.dropna().to_numpy(),
            name=left_name,
            line={"width": 2, "color": left_color},
            connectgaps=False,
            hovertemplate="%{x|%b %Y}<br>%{y:,.2f}<extra>" + left_name + "</extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=right.dropna().index,
            y=right.dropna().to_numpy(),
            name=right_name,
            yaxis="y2",
            line={"width": 2, "color": right_color, "dash": "dot"},
            connectgaps=False,
            hovertemplate="%{x|%b %Y}<br>%{y:,.2f}<extra>" + right_name + "</extra>",
        )
    )
    figure = _style(figure, left_unit, height)
    figure.update_layout(
        yaxis2={
            "title": right_unit,
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
        }
    )
    return figure


def _style(figure: go.Figure, y_title: str, height: int, showlegend: bool = True) -> go.Figure:
    """House style. Colours stay unset so Streamlit's theme drives light/dark."""
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        hovermode="x unified",
        showlegend=showlegend,
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
            "title": None,
        },
        yaxis={"title": y_title, "zeroline": False},
        xaxis={"title": None, "showgrid": False},
    )
    return figure


# ==========================================================================
# cached reads
# ==========================================================================

@st.cache_resource
def catalog() -> Catalog:
    return load_catalog()


@st.cache_data(ttl=CACHE_TTL)
def load_wide(series_ids: tuple[str, ...], start=None, end=None) -> pd.DataFrame:
    """Wide frame for a set of series. Tuple argument so the cache key is hashable."""
    if not series_ids:
        return pd.DataFrame()
    with store.connect(read_only=True) as con:
        return store.read_wide(con, list(series_ids), start=start, end=end)


@st.cache_data(ttl=CACHE_TTL)
def load_freshness() -> pd.DataFrame:
    with store.connect(read_only=True) as con:
        return store.freshness(con)


@st.cache_data(ttl=CACHE_TTL)
def load_recent_changes(days: int = 7) -> pd.DataFrame:
    with store.connect(read_only=True) as con:
        return store.recent_changes(con, days=days)


@st.cache_data(ttl=CACHE_TTL)
def load_coverage() -> pd.DataFrame:
    with store.connect(read_only=True) as con:
        return store.coverage(con)


@st.cache_data(ttl=CACHE_TTL)
def load_series(series_id: str, start=None) -> pd.Series:
    with store.connect(read_only=True) as con:
        return store.series_as_pandas(con, series_id, start=start)


def transformed(series_id: str, transform: str = "level") -> pd.Series:
    """A stored series with its transform applied at its own catalog frequency."""
    series = load_series(series_id)
    if series.empty or transform == "level":
        return series
    spec = catalog().get(series_id)
    return transforms.apply_transform(series, transform, spec.frequency if spec else "M")


def clear_caches() -> None:
    """Drop the cached reads. Does NOT touch the catalog — see `reload_data`."""
    st.cache_data.clear()


def reload_data() -> str | None:
    """Pick up every kind of change: new observations *and* an edited catalog.

    `clear_caches` alone is not enough for a YAML edit. `catalog()` is
    `@st.cache_resource`, which `st.cache_data.clear()` deliberately leaves
    alone, so a running app went on serving the old catalog until it was
    restarted — and separately, the KPI cards read `series_meta` in DuckDB,
    which only a refresh run rewrites. Activating a series therefore used to
    need a CLI refresh *and* a bounce. This does both here.

    Returns an error message if the store could not be written, else None.
    """
    # Order matters: the catalog has to go first, or the sync below would
    # faithfully re-mirror the stale copy it just declined to drop.
    catalog.clear()          # targeted, not st.cache_resource.clear()
    st.cache_data.clear()    # every cached read was derived from that catalog

    try:
        with store.connect() as con:
            store.sync_series_meta(con, catalog())
    except Exception as exc:  # noqa: BLE001 — reported, never raised at a reader
        # This is the only write a plain reader performs, and a concurrent
        # refresh_all.py holds DuckDB's write lock. Degrade to "caches cleared,
        # catalog not synced" rather than tracebacking on a page someone was
        # only trying to look at.
        return (
            f"Caches cleared, but the catalog could not be synced to the store: "
            f"{type(exc).__name__}: {exc}. If a refresh is running, try again "
            f"once it finishes."
        )
    return None


def specs_for(series_ids) -> dict[str, SeriesSpec]:
    known = catalog()
    return {sid: known[sid] for sid in series_ids if known.get(sid) is not None}


def frequencies_for(series_ids) -> dict[str, str]:
    return {sid: spec.frequency for sid, spec in specs_for(series_ids).items()}


def labels_for(series_ids) -> dict[str, str]:
    return {sid: spec.name for sid, spec in specs_for(series_ids).items()}


def available(series_ids) -> list[str]:
    """Drop ids the store has never populated, so a page never renders a blank axis.

    Catalog membership is not the same as data: a series can be catalogued,
    inactive, and empty. Pages call this before charting so an unfetched series
    quietly disappears instead of drawing an empty legend entry.
    """
    frame = load_freshness()
    if frame.empty:
        return []
    populated = set(frame.loc[frame["last_value"].notna(), "series_id"])
    return [sid for sid in series_ids if sid in populated]


# ==========================================================================
# Streamlit rendering
# ==========================================================================

CARD_CSS = """
<style>
.kpi-card {
    border: 1px solid rgba(128, 128, 128, 0.25);
    border-radius: 0.6rem;
    padding: 0.85rem 1rem 0.75rem 1rem;
    height: 100%;
    background: rgba(128, 128, 128, 0.04);
}
.kpi-card .kpi-label {
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    opacity: 0.65;
    line-height: 1.25;
    min-height: 2.1em;
}
.kpi-card .kpi-value {
    font-size: 2.05rem;
    font-weight: 700;
    line-height: 1.15;
    margin: 0.25rem 0 0.1rem 0;
}
.kpi-card .kpi-period { font-size: 0.75rem; opacity: 0.6; }
.kpi-card .kpi-foot {
    margin-top: 0.55rem;
    padding-top: 0.5rem;
    border-top: 1px solid rgba(128, 128, 128, 0.2);
    font-size: 0.8rem;
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
}
.kpi-card .kpi-prev { opacity: 0.7; }
.kpi-card .kpi-change { font-weight: 700; white-space: nowrap; }
.kpi-up { color: #16a34a; }
.kpi-down { color: #dc2626; }
.kpi-flat { opacity: 0.6; }
.kpi-card .kpi-note { font-size: 0.72rem; opacity: 0.55; margin-top: 0.35rem; }
</style>
"""

ARROWS = {"up": "▲", "down": "▼", "flat": "■"}


def inject_css() -> None:
    """Once per page run. Streamlit re-executes top to bottom, so this is cheap."""
    st.markdown(CARD_CSS, unsafe_allow_html=True)


def render_kpi_card(kpi: Kpi) -> None:
    """One big card: current value, the period it belongs to, previous, change.

    Colour follows direction only — up is green, down is red — and never tries to
    judge whether the move is good. Rising unemployment is not a green number,
    but nor is this module the right place to encode which way is "good" for
    thirty different indicators.
    """
    arrow = ARROWS[kpi.direction]
    change = kpi.change_text
    change_html = (
        f'<span class="kpi-change kpi-{kpi.direction}">{arrow} {change}</span>'
        if change
        else '<span class="kpi-change kpi-flat">—</span>'
    )
    note = f'<div class="kpi-note">{kpi.caption}</div>' if kpi.caption else ""
    badge = "" if kpi.status == "fresh" else f" {STATUS_BADGE.get(kpi.status, ('', ''))[0]}"

    st.markdown(
        f"""
        <div class="kpi-card">
          <div class="kpi-label">{kpi.label}{badge}</div>
          <div class="kpi-value">{kpi.value_text}</div>
          <div class="kpi-period">{kpi.current_period}</div>
          <div class="kpi-foot">
            <span class="kpi-prev">prev {kpi.previous_text}</span>
            {change_html}
          </div>
          {note}
        </div>
        """,
        unsafe_allow_html=True,
    )


def kpi_row(kpis: list[Kpi], columns: int = 4) -> None:
    """The first band of every tab. Four cards, one row, equal width."""
    if not kpis:
        st.info("No data for these indicators yet — run `scripts/refresh_all.py`.")
        return
    for start in range(0, len(kpis), columns):
        row = kpis[start : start + columns]
        for column, kpi in zip(st.columns(columns, gap="small"), row, strict=False):
            with column:
                render_kpi_card(kpi)


def kpis_from(series_ids, labels: dict[str, str] | None = None,
              captions: dict[str, str] | None = None) -> list[Kpi]:
    """Look up a list of series in the freshness view and build their cards."""
    labels = labels or {}
    captions = captions or {}
    frame = load_freshness()
    if frame.empty:
        return []
    indexed = frame.set_index("series_id")

    out = []
    for series_id in series_ids:
        if series_id not in indexed.index:
            continue
        row = indexed.loc[series_id].copy()
        row["series_id"] = series_id
        out.append(build_kpi(row, labels.get(series_id), captions.get(series_id, "")))
    return out


def kpi_from_series(
    label: str,
    series: pd.Series,
    unit: str = "Percent",
    frequency: str = "M",
    caption: str = "",
    series_id: str = "",
) -> Kpi | None:
    """A card built from a computed series rather than a stored one.

    Most headline numbers are transforms: "inflation" is the YoY of a CPI index,
    "growth" the QoQ of a GDP level. Storing the derived series instead would
    duplicate history that transforms.py already computes correctly, and would
    then need its own refresh path to stay in step.

    Returns None when there is not enough history for a current-and-previous
    pair, which is what the caller filters on — a card with no comparison is
    worse than no card.
    """
    clean = series.dropna()
    if len(clean) < 2:
        return None
    return Kpi(
        series_id=series_id or str(series.name or label),
        label=label,
        unit=unit,
        frequency=frequency,
        current=float(clean.iloc[-1]),
        previous=float(clean.iloc[-2]),
        current_period=format_period(clean.index[-1], frequency),
        previous_period=format_period(clean.index[-2], frequency),
        caption=caption,
    )


def transformed_kpi(
    series_id: str,
    transform: str = "yoy",
    label: str | None = None,
    caption: str = "",
    unit: str = "Percent",
) -> Kpi | None:
    """`kpi_from_series` for the common case: one catalogued series, one transform."""
    spec = catalog().get(series_id)
    if spec is None:
        return None
    series = transformed(series_id, transform)
    suffix = transforms.TRANSFORM_LABELS.get(transform, transform)
    return kpi_from_series(
        label or f"{spec.name}, {suffix}",
        series,
        unit=unit if transform != "level" else spec.unit,
        frequency=spec.frequency,
        caption=caption,
        series_id=series_id,
    )


@dataclass(frozen=True)
class ViewControls:
    """What the sidebar slicers resolved to, passed down into every chart."""

    start: date | None
    end: date | None
    preset: str

    def apply(self, frame: pd.DataFrame) -> pd.DataFrame:
        return slice_dates(frame, self.start, self.end)


def sidebar_controls(key: str, default_preset: str = "5Y") -> ViewControls:
    """Date slicers, shared by every tab on a page.

    Deliberately in the sidebar rather than per-chart: the comparison between
    Indonesia and Global only works if both pages are looking at the same window,
    and a per-chart control makes that impossible to guarantee.
    """
    with st.sidebar:
        st.subheader("Filters")
        preset = st.segmented_control(
            "Period",
            options=list(DATE_PRESETS),
            default=default_preset,
            key=f"{key}_preset",
        ) or default_preset

        today = date.today()
        span = DATE_PRESETS[preset]
        start = today - timedelta(days=span) if span else None

        if st.toggle("Custom dates", key=f"{key}_custom"):
            chosen = st.date_input(
                "Range",
                value=(start or date(1990, 1, 1), today),
                key=f"{key}_range",
            )
            if isinstance(chosen, tuple) and len(chosen) == 2:
                return ViewControls(chosen[0], chosen[1], "Custom")

        st.divider()
        if st.button("Reload data", width="stretch", key=f"{key}_reload"):
            # On success rerun straight into fresh data. On failure stay put and
            # say why — a rerun would discard the warning before it was read.
            problem = reload_data()
            if problem:
                st.warning(problem, icon="⚠️")
            else:
                st.rerun()
        st.caption(f"Cached for {CACHE_TTL // 60} min.")

    return ViewControls(start, today, preset)


def chart_block(title: str, figure: go.Figure, explanation: str = "") -> None:
    """A chart with its heading and the sentence that says what to look for.

    Deliberately unkeyed. `st.plotly_chart` only needs a key to carry selection
    state, which nothing here uses, and a keyed chart becomes a widget whose
    figure cannot be read back — which is exactly what tests/test_app.py has to
    do to check that a partial stack drops its reconciliation line.
    """
    st.markdown(f"##### {title}")
    if explanation:
        st.caption(explanation)
    st.plotly_chart(figure, width="stretch")


def series_multiselect(
    label: str,
    series_ids: list[str],
    default: list[str] | None = None,
    key: str = "",
) -> list[str]:
    """Per-chart slicer: which of these series to draw."""
    names = labels_for(series_ids)
    chosen = st.multiselect(
        label,
        options=series_ids,
        default=default if default is not None else series_ids,
        format_func=lambda sid: names.get(sid, sid),
        key=key,
    )
    return chosen


def transform_selector(
    key: str,
    options: tuple[str, ...] = ("level", "yoy", "mom"),
    default: str = "level",
    label: str = "Transform",
) -> str:
    """Transform slicer, labelled from `transforms.TRANSFORM_LABELS`."""
    index = options.index(default) if default in options else 0
    return st.selectbox(
        label,
        options=options,
        index=index,
        format_func=lambda name: transforms.TRANSFORM_LABELS.get(name, name),
        key=key,
    )


def render_detail_table(
    frame: pd.DataFrame,
    unit_column: str | None = None,
    height: int = 320,
) -> None:
    """The third band: the numbers themselves, for reading rather than scanning."""
    st.dataframe(frame, width="stretch", hide_index=True, height=height)


def latest_table(series_ids, labels: dict[str, str] | None = None) -> pd.DataFrame:
    """A tidy current/previous/change table for the detail band of a tab."""
    labels = labels or {}
    frame = load_freshness()
    if frame.empty:
        return pd.DataFrame()

    indexed = frame.set_index("series_id")
    rows = []
    for series_id in series_ids:
        if series_id not in indexed.index:
            continue
        row = indexed.loc[series_id]
        unit = row.get("unit", "") or ""
        rows.append(
            {
                "Indicator": labels.get(series_id, row.get("name", series_id)),
                "Period": format_period(row.get("last_obs_date"), row.get("frequency", "M")),
                "Latest": format_value(row.get("last_value"), unit),
                "Previous": format_value(row.get("prev_value"), unit),
                "Change": format_change(row.get("last_value"), row.get("prev_value"), unit),
                "Unit": unit,
                "Status": STATUS_BADGE.get(row.get("status", "fresh"), ("", ""))[1],
                "Series ID": series_id,
            }
        )
    return pd.DataFrame(rows)


def freshness_caption(series_ids) -> str:
    """One line summarising how current a group of series is."""
    frame = load_freshness()
    if frame.empty:
        return ""
    subset = frame[frame["series_id"].isin(list(series_ids))]
    if subset.empty:
        return ""
    counts = subset["status"].value_counts().to_dict()
    parts = [
        f"{STATUS_BADGE[status][0]} {count} {STATUS_BADGE[status][1]}"
        for status, count in counts.items()
        if status in STATUS_BADGE
    ]
    return " · ".join(parts)


def page_header(title: str, subtitle: str, series_ids) -> None:
    st.title(title)
    caption = freshness_caption(series_ids)
    st.caption(f"{subtitle}  ·  {caption}" if caption else subtitle)


def note(text: str) -> None:
    """A standing caveat about a series, kept visible next to the chart itself.

    The interbank-rate-is-not-the-BI-Rate warning is worthless in a README that
    nobody reads while looking at a chart.
    """
    st.caption(f":grey[ℹ️ {text}]")


__all__ = [
    "CACHE_TTL",
    "COMPONENT_COLORS",
    "Kpi",
    "ViewControls",
    "available",
    "bar_figure",
    "catalog",
    "chart_block",
    "clear_caches",
    "reload_data",
    "dual_axis_figure",
    "format_change",
    "format_period",
    "format_value",
    "freshness_caption",
    "heatmap_figure",
    "frequencies_for",
    "inject_css",
    "is_rate_unit",
    "kpi_from_series",
    "kpi_row",
    "kpis_from",
    "labels_for",
    "latest_table",
    "line_figure",
    "load_coverage",
    "load_freshness",
    "load_recent_changes",
    "load_series",
    "load_wide",
    "note",
    "page_header",
    "render_detail_table",
    "series_colors",
    "series_multiselect",
    "sidebar_controls",
    "slice_dates",
    "stacked_contribution_figure",
    "transform_frame",
    "transformed",
    "transformed_kpi",
    "transform_selector",
]
