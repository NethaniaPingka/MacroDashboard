"""Data Manager — type observations that no API publishes.

The BI Rate is the reason this page exists: Bank Indonesia has no open API and
FRED does not carry the policy rate, so `ID.POLICY.RATE` can only arrive by
someone typing it. Nothing here is specific to it, though — any series in the
catalog can be filled in by hand.

The page is built around one rule: **nothing is written until you have seen what
it would change.** The grid produces a diff — new rows, revised rows, rows that
change nothing — and the commit button acts on that diff rather than on the raw
typing. Dates are snapped to the start of their period first, and the snap is
shown, because a monthly value filed under the 15th does not error, it silently
fails to match the year-earlier lookup a YoY transform makes.

Entries are saved to `data/manual/<series_id>.csv` and only then upserted into
DuckDB. That order is deliberate: the database is rebuildable and gitignored,
so hand-typed history that lived only inside it would not survive the rebuild
the README recommends whenever something looks wrong.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from macrodash import charts, manual, store
from macrodash.config import CATEGORIES

st.set_page_config(page_title="Data Manager · Macro Dashboard", page_icon="✏️", layout="wide")

#: Sources that fetch on their own. Entering values for these is allowed — a
#: provisional print before the agency publishes is a legitimate thing to want —
#: but the next refresh overwrites them from upstream, and the page says so
#: rather than letting the value quietly disappear a day later.
AUTOMATED_SOURCES = {"fred", "yahoo", "bps_api"}

#: Rows the empty grid opens with. Enough to paste a short run into without
#: adding rows by hand; the grid is dynamic, so more can be added.
BLANK_ROWS = 8

charts.inject_css()
st.title("✏️ Data Manager")
st.caption(
    "Type observations for series no API publishes. Saved to data/manual/ first, "
    "then to the store — so a rebuild cannot lose them."
)

catalog = charts.catalog()
coverage = charts.load_coverage()
covered = (
    coverage.set_index("series_id") if not coverage.empty else pd.DataFrame()
)


# ==========================================================================
# picking a series
# ==========================================================================

with st.sidebar:
    st.subheader("Find a series")
    countries = ["All", *catalog.countries()]
    country = st.selectbox("Country", countries, key="dm_country")
    category = st.selectbox("Category", ["All", *CATEGORIES], key="dm_category")
    only_manual = st.toggle(
        "Manual-entry series only",
        value=False,
        help="Hide series that already fetch themselves from FRED, Yahoo or BPS.",
        key="dm_only_manual",
    )
    st.divider()
    if st.button("Reload data", width="stretch", key="dm_reload"):
        problem = charts.reload_data()
        if problem:
            st.warning(problem, icon="⚠️")
        else:
            st.rerun()

# Inactive series are deliberately included: ID.POLICY.RATE is inactive
# *because* it has no data, and a picker that hid it would hide the one series
# this page was built for.
candidates = [
    spec
    for spec in catalog
    if (country == "All" or spec.country == country)
    and (category == "All" or spec.category == category)
    and (not only_manual or spec.source not in AUTOMATED_SOURCES)
]
candidates.sort(key=lambda s: (s.country, s.category, s.series_id))

if not candidates:
    st.warning("No series match those filters.")
    st.stop()


def describe(series_id: str) -> str:
    spec = catalog[series_id]
    marks = []
    if not spec.active:
        marks.append("inactive")
    if spec.source not in AUTOMATED_SOURCES:
        marks.append("manual")
    suffix = f"  ·  {', '.join(marks)}" if marks else ""
    return f"{spec.series_id} — {spec.name}{suffix}"


chosen_id = st.selectbox(
    "Series",
    options=[spec.series_id for spec in candidates],
    format_func=describe,
    key="dm_series",
)
spec = catalog[chosen_id]
history = charts.load_series(chosen_id)
last_obs = history.index.max() if not history.empty else None

info = st.columns(5)
info[0].metric("Frequency", spec.frequency)
info[1].metric("Unit", spec.unit or "—")
info[2].metric("Source", spec.source)
info[3].metric("Stored rows", f"{len(history):,}")
info[4].metric(
    "Last observation",
    f"{pd.Timestamp(last_obs):%Y-%m-%d}" if last_obs is not None else "—",
)

if spec.source in AUTOMATED_SOURCES:
    st.warning(
        f"**{spec.series_id} fetches itself from `{spec.source}`.** You can type "
        f"values for it, and they will be stored — but the next "
        f"`refresh_all.py` run overwrites them with whatever upstream says. Use "
        f"this for a provisional print, not for a correction you need to keep.",
        icon="⚠️",
    )
if not spec.active:
    st.info(
        f"**{spec.series_id} is inactive**, so it does not appear on the dashboard "
        f"yet. Entering data here does not change that — `active: true` in "
        f"`catalog/indonesia.yaml` does, and the catalog stays the single control "
        f"surface on purpose. Enter the history first, then flip the flag.",
        icon="ℹ️",
    )
if spec.notes:
    charts.note(" ".join(spec.notes.split()))

if not history.empty:
    with st.expander(f"What is stored now — {len(history):,} rows", expanded=False):
        st.plotly_chart(
            charts.line_figure(
                history.to_frame(name=spec.series_id),
                {spec.series_id: spec.name},
                {spec.series_id: charts.COUNTRY_COLORS.get(spec.country, "#4C8DF6")},
                unit=spec.unit,
            ),
            width="stretch",
        )
        recent = history.sort_index(ascending=False).head(24).reset_index()
        recent.columns = ["Date", "Value"]
        recent["Date"] = pd.to_datetime(recent["Date"]).dt.strftime("%Y-%m-%d")
        st.dataframe(recent, hide_index=True, width="stretch")


# ==========================================================================
# the grid
# ==========================================================================

st.subheader("Enter observations")
PERIOD_NAME = {"D": "day", "W": "week", "M": "month", "Q": "quarter", "A": "year"}

if spec.frequency == "D":
    # An event series has nothing to snap to, and saying otherwise would imply a
    # grid that does not exist. This is the case that lets two decisions share a
    # month — the reason ID.POLICY.RATE is dated daily at all.
    st.caption(
        "One row per **dated event**. Nothing is snapped: the date you type is the "
        "date stored, so two entries in the same month are kept apart rather than "
        "colliding. Values are entered as the publication prints them; the "
        "catalog's scale is applied on the way in."
    )
else:
    st.caption(
        f"One row per period. Dates are snapped to the start of the "
        f"{PERIOD_NAME[spec.frequency]} — the convention every fetcher in this "
        f"project follows. Values are entered as the publication prints them; "
        f"the catalog's scale is applied on the way in."
    )


def blank_grid() -> pd.DataFrame:
    """An empty grid whose first row is already dated to the next period due."""
    dates = [manual.next_period(last_obs, spec.frequency)] + [pd.NaT] * (BLANK_ROWS - 1)
    return pd.DataFrame(
        {
            "obs_date": pd.Series(dates, dtype="datetime64[ns]"),
            "value": pd.Series([None] * BLANK_ROWS, dtype="float64"),
            "source_ref": pd.Series([""] * BLANK_ROWS, dtype="object"),
        }
    )


# Keyed on the series so switching the picker clears whatever was half-typed for
# the previous one, rather than carrying it silently into a different series.
grid_key = f"dm_grid_{chosen_id}"

edited = st.data_editor(
    blank_grid(),
    num_rows="dynamic",
    width="stretch",
    key=grid_key,
    column_config={
        "obs_date": st.column_config.DateColumn(
            "Date", format="YYYY-MM-DD", help="Any date inside the period; it is snapped."
        ),
        "value": st.column_config.NumberColumn(
            "Value", format="%.6g", help=f"In {spec.unit or 'the series unit'}."
        ),
        "source_ref": st.column_config.TextColumn(
            "Source", help="Where this number came from — a URL, a release name, a page."
        ),
    },
)

default_ref = st.text_input(
    "Default source reference",
    value=spec.source_ref or "",
    help=(
        "Applied to any row above whose Source is blank. A typed number has no "
        "upstream to re-query, so this pointer is the only way it can ever be checked."
    ),
    key=f"dm_ref_{chosen_id}",
)

prepared = manual.prepare(spec, edited, stored=history, source_ref=default_ref.strip())


# ==========================================================================
# the diff — what committing would actually do
# ==========================================================================

st.subheader("Preview")

for problem in prepared.problems:
    st.error(problem, icon="🚫")

if prepared.diff.empty:
    st.caption("Nothing typed yet.")
else:
    counts = st.columns(3)
    counts[0].metric("New", prepared.n_new)
    counts[1].metric("Revisions", prepared.n_revised)
    counts[2].metric("Unchanged", prepared.n_unchanged)

    display = prepared.diff.copy()
    display["Date"] = pd.to_datetime(display["obs_date"]).dt.strftime("%Y-%m-%d")
    display["Status"] = display["status"].map(
        {"new": "🟢 new", "revision": "🟡 revision", "unchanged": "⚪ unchanged"}
    )
    display["Stored"] = display["stored_value"].map(
        lambda v: "—" if pd.isna(v) else charts.format_value(v, spec.unit)
    )
    display["Entered"] = display["value"].map(lambda v: charts.format_value(v, spec.unit))
    display["Snapped from"] = display["moved_from"].replace("", "—")
    display["Check"] = display["warning"].replace("", "")
    st.dataframe(
        display.loc[:, ["Date", "Status", "Stored", "Entered", "Snapped from", "Check", "source_ref"]]
        .rename(columns={"source_ref": "Source"}),
        hide_index=True,
        width="stretch",
    )

    for warning in prepared.warnings:
        st.warning(warning, icon="⚠️")
    if prepared.warnings:
        st.caption(
            "These are questions, not refusals — every one of them has a legitimate "
            "exception. Commit anyway if the number is right."
        )

    writes = prepared.n_new + prepared.n_revised
    commit = st.button(
        f"Commit {writes} row(s) to {spec.series_id}",
        type="primary",
        disabled=bool(prepared.problems) or writes == 0,
        key=f"dm_commit_{chosen_id}",
    )
    if writes == 0 and not prepared.problems:
        st.caption("Every typed row already matches what is stored.")

    if commit:
        try:
            result = manual.save(spec, prepared.entries)
        except Exception as exc:  # noqa: BLE001 — surfaced to the user, not swallowed
            st.error(
                f"Could not write: {type(exc).__name__}: {exc}\n\n"
                f"If this mentions a lock, a `refresh_all.py` run has the database "
                f"open. Your typing is safe in `data/manual/` either way.",
                icon="🚫",
            )
        else:
            # Syncs the catalog too, so a series activated in YAML minutes ago
            # appears the moment its first values land. The write already
            # succeeded, so a sync failure is a warning beside the success, not
            # instead of it.
            problem = charts.reload_data()
            st.success(result.summary(), icon="✅")
            if problem:
                st.warning(problem, icon="⚠️")
            st.caption(f"Written to `{result.csv_path}`.")
            if not spec.active:
                st.info(
                    f"`{spec.series_id}` now holds data but is still inactive. Set "
                    f"`active: true` on it in `catalog/indonesia.yaml` to bring it "
                    f"onto the Indonesia page.",
                    icon="📌",
                )


# ==========================================================================
# what has been entered by hand, across the whole store
# ==========================================================================

st.divider()
st.subheader("Manually entered data")

entered = manual.manual_coverage()
if entered.empty:
    st.caption(
        "Nothing has been entered by hand yet. Files appear in `data/manual/`, one "
        "CSV per series, and are replayed by every refresh."
    )
else:
    table = entered.copy()
    table["name"] = table["series_id"].map(
        lambda sid: catalog[sid].name if catalog.get(sid) else "— not in the catalog —"
    )
    for column in ("first_obs", "last_obs"):
        table[column] = pd.to_datetime(table[column]).dt.strftime("%Y-%m-%d")
    st.dataframe(
        table.loc[:, ["series_id", "name", "n_obs", "first_obs", "last_obs", "last_entered", "file"]]
        .rename(
            columns={
                "series_id": "Series",
                "name": "Name",
                "n_obs": "Rows",
                "first_obs": "From",
                "last_obs": "To",
                "last_entered": "Last entered",
                "file": "File",
            }
        ),
        hide_index=True,
        width="stretch",
    )

    own = manual.read_manual(chosen_id)
    if not own.empty:
        st.download_button(
            f"Download {chosen_id}.csv",
            data=own.to_csv(index=False).encode("utf-8"),
            file_name=f"{chosen_id}.csv",
            mime="text/csv",
            key=f"dm_download_{chosen_id}",
        )

charts.note(
    "Manual CSVs are the durable copy. data/macro.duckdb is rebuildable and "
    "gitignored; data/manual/ is not, so commit it."
)
