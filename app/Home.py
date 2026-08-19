"""Home — the one screen where Indonesia and Global appear together.

Everywhere else the two contexts are separate pages, on purpose: mixing them
makes both harder to read. Here they share a screen only as an at-a-glance tile
row, and the rest of the page is about the data itself — what moved this week,
what is going stale, what the store actually holds.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from macrodash import charts, transforms

st.set_page_config(
    page_title="Macro Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

charts.inject_css()

st.title("📈 Macro Dashboard")
st.caption(
    "Indonesian and global macro data, stored locally in DuckDB and refreshed from "
    "FRED, the BPS WebAPI and Yahoo Finance."
)

freshness = charts.load_freshness()

# --------------------------------------------------------------------------
# The at-a-glance row — the only place the two contexts share a screen
# --------------------------------------------------------------------------
st.subheader("Indonesia")
indonesia_cards = charts.kpis_from(
    ["ID.CPI.INFLATION.YOY"],
    labels={"ID.CPI.INFLATION.YOY": "Inflation, YoY"},
)
gdp_card = charts.kpi_from_series(
    "Real GDP growth, YoY",
    transforms.yoy(charts.load_series("ID.GDP.REAL"), "Q"),
    frequency="Q",
)
if gdp_card is not None:
    indonesia_cards.append(gdp_card)
indonesia_cards += charts.kpis_from(
    ["ID.POLICY.RATE", "MKT.USDIDR"],
    labels={
        "ID.POLICY.RATE": "BI Rate",
        "MKT.USDIDR": "USD/IDR",
    },
    captions={"ID.POLICY.RATE": "Bank Indonesia policy rate"},
)
charts.kpi_row(indonesia_cards)

st.subheader("Global")
global_cards = [
    card
    for card in (
        charts.transformed_kpi("US.CPI.HEADLINE.IDX", "yoy", "US CPI, YoY"),
        charts.transformed_kpi("EA.HICP.IDX", "yoy", "Euro area HICP, YoY"),
    )
    if card is not None
]
global_cards += charts.kpis_from(
    ["US.POLICY.RATE", "US.UST.10Y"],
    labels={"US.POLICY.RATE": "Fed funds rate", "US.UST.10Y": "US 10-year yield"},
)
charts.kpi_row(global_cards)

st.divider()

left, right = st.columns([3, 2])

# --------------------------------------------------------------------------
# What changed
# --------------------------------------------------------------------------
with left:
    st.subheader("What changed recently")
    window = st.segmented_control(
        "Window", options=[7, 14, 30], default=7,
        format_func=lambda days: f"{days} days", key="home_window",
    ) or 7

    changes = charts.load_recent_changes(days=window)
    if changes.empty:
        st.info(
            f"Nothing added or revised in the last {window} days. "
            "Run `scripts/refresh_all.py` to pull new observations."
        )
    else:
        changes = changes.copy()
        changes["updated_at"] = pd.to_datetime(changes["updated_at"])
        display = pd.DataFrame(
            {
                "Series": changes["name"],
                "Country": changes["country"],
                "New rows": changes["rows_added"],
                "Revised": changes["rows_updated"],
                "Through": pd.to_datetime(changes["last_obs_date"]).dt.strftime("%d %b %Y"),
                "Refreshed": changes["updated_at"].dt.strftime("%d %b %H:%M"),
            }
        )
        st.dataframe(display, width="stretch", hide_index=True, height=340)
        # Revisions matter as much as new rows here: `rows_updated` counts values
        # that actually changed, because the upsert only writes on a real
        # difference. A statistical agency quietly restating history shows up in
        # this column and nowhere else.
        revised = int(changes["rows_updated"].sum())
        if revised:
            st.caption(
                f"{revised} previously-published value(s) were revised in this window."
            )

# --------------------------------------------------------------------------
# Data health
# --------------------------------------------------------------------------
with right:
    st.subheader("Data health")
    if freshness.empty:
        st.warning("The store is empty. Run `scripts/refresh_all.py` first.")
    else:
        counts = freshness["status"].value_counts()
        columns = st.columns(len(charts.STATUS_BADGE))
        for column, (status, (icon, label)) in zip(
            columns, charts.STATUS_BADGE.items(), strict=False
        ):
            with column:
                st.metric(f"{icon} {label.title()}", int(counts.get(status, 0)))

        problems = freshness[freshness["status"].isin(["stale", "ageing", "empty"])]
        if problems.empty:
            st.success("Every active series is current.")
        else:
            st.dataframe(
                pd.DataFrame(
                    {
                        "Series": problems["name"],
                        "Status": problems["status"],
                        "Last obs": pd.to_datetime(problems["last_obs_date"]).dt.strftime(
                            "%d %b %Y"
                        ),
                        "Days": problems["days_since"].astype("Int64"),
                    }
                ).sort_values("Days", ascending=False),
                width="stretch",
                hide_index=True,
                height=220,
            )
            charts.note(
                "Ageing and stale are judged against each frequency's own publication "
                "lag, so they mean something is genuinely late — not merely recent."
            )

st.divider()

# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------
with st.expander("What the store holds"):
    coverage = charts.load_coverage()
    if not coverage.empty:
        populated = coverage[coverage["n_obs"] > 0]
        summary = st.columns(4)
        summary[0].metric("Series with data", len(populated))
        summary[1].metric("Observations", f"{int(coverage['n_obs'].sum()):,}")
        summary[2].metric(
            "Earliest", pd.to_datetime(coverage["first_obs"]).min().strftime("%b %Y")
        )
        summary[3].metric(
            "Latest", pd.to_datetime(coverage["last_obs"]).max().strftime("%d %b %Y")
        )

        st.dataframe(
            pd.DataFrame(
                {
                    "Series": coverage["name"],
                    "ID": coverage["series_id"],
                    "Country": coverage["country"],
                    "Group": coverage["category"],
                    "Source": coverage["source"],
                    "Freq": coverage["frequency"],
                    "Rows": coverage["n_obs"],
                    "From": pd.to_datetime(coverage["first_obs"]).dt.strftime("%b %Y"),
                    "To": pd.to_datetime(coverage["last_obs"]).dt.strftime("%d %b %Y"),
                }
            ),
            width="stretch",
            hide_index=True,
            height=380,
        )

with st.sidebar:
    st.subheader("Pages")
    st.page_link("pages/1_Indonesia.py", label="Indonesia", icon="🇮🇩")
    st.page_link("pages/2_Global.py", label="Global", icon="🌍")
    st.divider()
    if st.button("Reload data", width="stretch", key="home_reload"):
        problem = charts.reload_data()
        if problem:
            st.warning(problem, icon="⚠️")
        else:
            st.rerun()
    st.caption(f"Cached for {charts.CACHE_TTL // 60} min.")
