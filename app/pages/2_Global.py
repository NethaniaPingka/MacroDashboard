"""Global — the US and the euro area, in the same four tabs as Indonesia.

Same three bands per tab, same order, same helpers. Where this page differs from
`1_Indonesia.py` it is because the data differs: the US publishes an official
decomposition of GDP growth, so the Growth tab charts BEA's contributions
directly rather than deriving them from levels the way the Indonesian page must.

Asia ex-Indonesia is out of scope for v1 (decided 2026-08-18) — global means the
US and the euro area. The header comment in catalog/global.yaml records the
sourcing dead-ends so they are not rediscovered.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from macrodash import charts, transforms
from macrodash.config import COUNTRY_COLORS

st.set_page_config(page_title="Global · Macro Dashboard", page_icon="🌍", layout="wide")

INFLATION = ["US.CPI.HEADLINE.IDX", "US.CPI.CORE.IDX", "US.PCE.HEADLINE.IDX",
             "US.PCE.CORE.IDX", "EA.HICP.IDX"]
GROWTH = ["US.GDP.REAL", "EA.GDP.REAL", "US.UNEMPLOYMENT", "US.PAYROLLS",
          "US.INDPRO", "US.RETAIL.SALES", "US.CONSUMER.SENTIMENT"]
#: BEA's own decomposition. The four here sum to headline growth; exports and
#: imports are the split of net exports and would double-count if added alongside.
GDP_CONTRIBUTIONS = {
    "Consumption": "US.GDP.CONTRIB.PCE",
    "Investment": "US.GDP.CONTRIB.INVESTMENT",
    "Government": "US.GDP.CONTRIB.GOVERNMENT",
    "Net exports": "US.GDP.CONTRIB.NETEXPORTS",
}
TRADE_SPLIT = {
    "Exports": "US.GDP.CONTRIB.EXPORTS",
    "Imports": "US.GDP.CONTRIB.IMPORTS",
}
RATES = ["US.POLICY.RATE", "EA.POLICY.RATE", "US.UST.2Y", "US.UST.10Y",
         "US.CURVE.2S10S", "EA.BUND.10Y", "US.M2"]
MARKETS = ["MKT.SPX", "MKT.STOXX50", "MKT.NIKKEI", "MKT.DXY", "MKT.EURUSD",
           "MKT.BRENT", "MKT.WTI", "MKT.GOLD"]

ALL_SERIES = INFLATION + GROWTH + list(GDP_CONTRIBUTIONS.values()) + RATES + MARKETS

charts.inject_css()
charts.page_header(
    "🌍 Global",
    "United States and euro area",
    ALL_SERIES,
)
view = charts.sidebar_controls("global")

tab_inflation, tab_growth, tab_rates, tab_markets = st.tabs(charts.TABS)


# ==========================================================================
# Inflation
# ==========================================================================
with tab_inflation:
    cards = [
        card
        for card in (
            charts.transformed_kpi("US.CPI.HEADLINE.IDX", "yoy", "US CPI, YoY"),
            charts.transformed_kpi("US.CPI.CORE.IDX", "yoy", "US core CPI, YoY"),
            charts.transformed_kpi(
                "US.PCE.CORE.IDX", "yoy", "US core PCE, YoY",
                caption="The Fed's target measure",
            ),
            charts.transformed_kpi("EA.HICP.IDX", "yoy", "Euro area HICP, YoY"),
        )
        if card is not None
    ]
    charts.kpi_row(cards)

    st.divider()

    inflation_transform = charts.transform_selector(
        "gl_infl_transform",
        options=("yoy", "ann_3m", "mom", "level"),
        default="yoy",
        label="Show price indices as",
    )
    chosen = charts.series_multiselect(
        "Price measures",
        charts.available(INFLATION),
        default=charts.available(["US.CPI.HEADLINE.IDX", "US.CPI.CORE.IDX", "EA.HICP.IDX"]),
        key="gl_infl_series",
    )
    if chosen:
        frame = charts.transform_frame(
            charts.load_wide(tuple(chosen)),
            inflation_transform,
            charts.frequencies_for(chosen),
        )
        figure = charts.line_figure(
            view.apply(frame),
            labels=charts.labels_for(chosen),
            colors=charts.series_colors(chosen, charts.catalog()),
            unit="Index",
            transform=inflation_transform,
            zero_line=inflation_transform != "level",
        )
        if inflation_transform in ("yoy", "ann_3m"):
            figure.add_hline(
                y=2.0, line_width=1, line_dash="dash",
                line_color=charts.REFERENCE_LINE, opacity=0.7,
                annotation_text="2% target", annotation_position="top left",
            )
        charts.chart_block(
            "Inflation across measures",
            figure,
            "Both the Fed and the ECB target 2%. Three-month annualised turns "
            "months before year-on-year does, at the cost of more noise.",
        )

    left, right = st.columns(2)
    with left:
        core_gap = transforms.spread(
            charts.transformed("US.CPI.HEADLINE.IDX", "yoy"),
            charts.transformed("US.CPI.CORE.IDX", "yoy"),
        )
        charts.chart_block(
            "US headline less core",
            charts.bar_figure(
                charts.slice_dates(core_gap.to_frame("g"), view.start, view.end)["g"],
                name="Headline − core",
                unit="Percentage points",
            ),
            "Food and energy, essentially. Positive spikes are the part of inflation "
            "monetary policy is least able to reach.",
        )
    with right:
        cpi_pce = pd.DataFrame(
            {
                "US core CPI": charts.transformed("US.CPI.CORE.IDX", "yoy"),
                "US core PCE": charts.transformed("US.PCE.CORE.IDX", "yoy"),
            }
        ).sort_index()
        charts.chart_block(
            "Core CPI against core PCE",
            charts.line_figure(
                view.apply(cpi_pce),
                colors={
                    "US core CPI": charts.COMPONENT_COLORS[0],
                    "US core PCE": charts.COMPONENT_COLORS[1],
                },
                unit="Percent",
            ),
            "CPI usually runs above PCE — different weights and different treatment of "
            "healthcare. The Fed targets the lower one.",
        )

    with st.expander("Detail — price series"):
        charts.render_detail_table(charts.latest_table(charts.available(INFLATION)))
        charts.note(
            "The EZ19 suffix on the HICP id tracks euro-area membership and will need "
            "updating if the bloc grows."
        )


# ==========================================================================
# Growth
# ==========================================================================
with tab_growth:
    us_gdp = charts.load_series("US.GDP.REAL")
    us_growth = transforms.qoq_saar(us_gdp, "Q")

    cards = [
        card
        for card in (
            charts.kpi_from_series(
                "US real GDP, QoQ annualised", us_growth, frequency="Q",
                caption="Seasonally adjusted, so the quarterly rate is meaningful",
            ),
            charts.transformed_kpi(
                "EA.GDP.REAL", "qoq_saar", "Euro area real GDP, QoQ annualised"
            ),
        )
        if card is not None
    ]
    cards += charts.kpis_from(
        ["US.UNEMPLOYMENT"], labels={"US.UNEMPLOYMENT": "US unemployment rate"}
    )
    payrolls_change = charts.load_series("US.PAYROLLS").diff()
    payroll_card = charts.kpi_from_series(
        "US payrolls, monthly change", payrolls_change,
        unit="Thousands of persons",
        caption="Change on the month, in thousands of jobs",
    )
    if payroll_card is not None:
        cards.append(payroll_card)
    charts.kpi_row(cards)

    st.divider()

    gdp_frame = pd.DataFrame(
        {
            "United States": us_growth,
            "Euro area": charts.transformed("EA.GDP.REAL", "qoq_saar"),
        }
    ).sort_index()
    charts.chart_block(
        "Real GDP growth, quarterly annualised",
        charts.line_figure(
            view.apply(gdp_frame),
            colors={
                "United States": COUNTRY_COLORS["US"],
                "Euro area": COUNTRY_COLORS["EA"],
            },
            unit="Percent",
            zero_line=True,
        ),
        "Both series are seasonally adjusted, so the quarterly rate is the convention "
        "here — unlike Indonesia, where it is not.",
    )

    # BEA publishes the decomposition, so this is read rather than derived. The
    # Indonesian page has to compute the equivalent from component levels because
    # BPS publishes no such series.
    contribution_names = charts.series_multiselect(
        "Components in the stack",
        list(GDP_CONTRIBUTIONS),
        key="gl_growth_components",
    )
    contribution_ids = [GDP_CONTRIBUTIONS[name] for name in contribution_names]
    contribution_frame = charts.load_wide(tuple(contribution_ids))
    if not contribution_frame.empty:
        stacked = contribution_frame.rename(
            columns={series_id: name for name, series_id in GDP_CONTRIBUTIONS.items()}
        )
        complete = len(contribution_names) == len(GDP_CONTRIBUTIONS)
        charts.chart_block(
            "What is driving US growth",
            charts.stacked_contribution_figure(
                view.apply(stacked),
                headline=charts.slice_dates(us_growth.to_frame("g"), view.start, view.end)["g"]
                if complete
                else None,
                headline_name="Real GDP, QoQ annualised",
            ),
            "BEA's own contributions, in percentage points of the annualised quarterly "
            "rate. With all four shown they sum to the headline to within a rounding "
            "tenth."
            if complete
            else "BEA's own contributions. The headline line is hidden because a partial "
                 "selection cannot reconcile to it.",
        )

    left, right = st.columns(2)
    with left:
        labour = pd.DataFrame(
            {
                "Unemployment rate": charts.load_series("US.UNEMPLOYMENT"),
            }
        ).sort_index()
        charts.chart_block(
            "US unemployment",
            charts.line_figure(
                view.apply(labour),
                colors={"Unemployment rate": charts.COMPONENT_COLORS[1]},
                unit="Percent",
            ),
            "The single most-watched number in the US data calendar.",
        )
    with right:
        charts.chart_block(
            "US payrolls, monthly change",
            charts.bar_figure(
                charts.slice_dates(payrolls_change.to_frame("p"), view.start, view.end)["p"],
                name="Payrolls",
                unit="Thousands of persons",
            ),
            "Thousands of jobs added or lost each month.",
        )

    with st.expander("Detail — activity and the growth decomposition", expanded=True):
        latest = us_growth.dropna().index[-1] if not us_growth.dropna().empty else None
        if latest is not None:
            split_frame = charts.load_wide(
                tuple(list(GDP_CONTRIBUTIONS.values()) + list(TRADE_SPLIT.values()))
            )
            rows = []
            for name, series_id in {**GDP_CONTRIBUTIONS, **TRADE_SPLIT}.items():
                if series_id not in split_frame.columns:
                    continue
                column = split_frame[series_id]
                if latest not in column.index or pd.isna(column.loc[latest]):
                    continue
                rows.append(
                    {
                        "Component": name,
                        "Contribution (pp)": float(column.loc[latest]),
                        "Part of": "Net exports" if name in TRADE_SPLIT else "Headline",
                        "4-quarter average": float(column.tail(4).mean()),
                    }
                )
            table = pd.DataFrame(rows)
            st.caption(
                f"{charts.format_period(latest, 'Q')} · US real GDP "
                f"{us_growth.loc[latest]:+.2f}% annualised. Exports and imports are the "
                "split of net exports — they are listed for detail, not to be added on top."
            )
            st.dataframe(
                table,
                width="stretch",
                hide_index=True,
                column_config={
                    "Contribution (pp)": st.column_config.NumberColumn(format="%+.2f"),
                    "4-quarter average": st.column_config.NumberColumn(format="%+.2f"),
                },
            )

        st.markdown("**Activity indicators**")
        charts.render_detail_table(charts.latest_table(charts.available(GROWTH)))
        charts.note(
            "Euro area unemployment is inactive — FRED's series was abandoned upstream "
            "at January 2023 and there is no live replacement short of Eurostat direct."
        )


# ==========================================================================
# Rates & Money
# ==========================================================================
with tab_rates:
    cards = charts.kpis_from(
        ["US.POLICY.RATE", "EA.POLICY.RATE", "US.UST.10Y", "US.CURVE.2S10S"],
        labels={
            "US.POLICY.RATE": "Fed funds rate",
            "EA.POLICY.RATE": "ECB deposit rate",
            "US.UST.10Y": "US 10-year yield",
            "US.CURVE.2S10S": "US 10y − 2y spread",
        },
        captions={"US.CURVE.2S10S": "Negative has preceded every recent US recession"},
    )
    charts.kpi_row(cards)

    st.divider()

    policy_ids = charts.available(["US.POLICY.RATE", "EA.POLICY.RATE"])
    charts.chart_block(
        "Policy rates",
        charts.line_figure(
            view.apply(charts.load_wide(tuple(policy_ids))),
            labels=charts.labels_for(policy_ids),
            colors=charts.series_colors(policy_ids, charts.catalog()),
            unit="Percent",
        ),
        "The ECB rate is the deposit facility, the floor of its corridor; fed funds is "
        "an effective market rate. Close enough to compare, not the same construct.",
    )

    left, right = st.columns(2)
    with left:
        curve_ids = charts.available(["US.UST.2Y", "US.UST.10Y", "EA.BUND.10Y"])
        charts.chart_block(
            "Government bond yields",
            charts.line_figure(
                view.apply(charts.load_wide(tuple(curve_ids))),
                labels=charts.labels_for(curve_ids),
                colors=charts.series_colors(curve_ids, charts.catalog()),
                unit="Percent",
            ),
            "The Bund stands in for the euro area — FRED's bloc aggregate was abandoned "
            "at January 2026, and the Bund is the benchmark anyway.",
        )
    with right:
        curve = charts.load_wide(("US.CURVE.2S10S",))
        charts.chart_block(
            "US yield curve, 10y − 2y",
            charts.bar_figure(
                view.apply(curve)["US.CURVE.2S10S"]
                if not curve.empty
                else pd.Series(dtype="float64"),
                name="2s10s",
                unit="Percentage points",
            ),
            "Stored directly rather than derived from the two legs, so a gap in either "
            "one does not silently blank the spread.",
        )

    charts.chart_block(
        "US money supply growth",
        charts.line_figure(
            view.apply(
                charts.transform_frame(charts.load_wide(("US.M2",)), "yoy", {"US.M2": "M"})
            ),
            labels={"US.M2": "M2, YoY"},
            colors={"US.M2": charts.COMPONENT_COLORS[3]},
            unit="Percent",
            transform="yoy",
            zero_line=True,
        ),
        "M2 contracted outright in 2023 for the first time in the series' history.",
    )

    with st.expander("Detail — rates and money"):
        charts.render_detail_table(charts.latest_table(charts.available(RATES)))


# ==========================================================================
# External & Markets
# ==========================================================================
with tab_markets:
    cards = charts.kpis_from(
        ["MKT.SPX", "MKT.DXY", "MKT.EURUSD", "MKT.BRENT"],
        labels={
            "MKT.SPX": "S&P 500",
            "MKT.DXY": "US dollar index",
            "MKT.EURUSD": "EUR/USD",
            "MKT.BRENT": "Brent crude",
        },
    )
    charts.kpi_row(cards)

    st.divider()

    equity_ids = charts.available(["MKT.SPX", "MKT.STOXX50", "MKT.NIKKEI", "MKT.JKSE"])
    rebase_on = st.toggle(
        "Rebase to 100 at the start of the window",
        value=True,
        key="gl_mkt_rebase",
        help="Indices on different scales only compare as shapes. Rebasing makes the "
             "comparison an honest one.",
    )
    equities = view.apply(charts.load_wide(tuple(equity_ids)))
    if rebase_on and not equities.empty:
        equities = pd.DataFrame(
            {
                column: transforms.rebase(
                    equities[column].dropna(), equities[column].dropna().index[0]
                )
                for column in equities.columns
                if not equities[column].dropna().empty
            }
        ).sort_index()
    charts.chart_block(
        "Equity indices",
        charts.line_figure(
            equities,
            labels=charts.labels_for(equity_ids),
            colors=charts.series_colors(equity_ids, charts.catalog()),
            unit="Index, rebased to 100" if rebase_on else "Index",
        ),
        "The Jakarta Composite is included here so the two context pages have one "
        "chart genuinely in common.",
    )

    left, right = st.columns(2)
    with left:
        fx_ids = charts.available(["MKT.DXY", "MKT.EURUSD"])
        charts.chart_block(
            "Dollar",
            charts.dual_axis_figure(
                charts.slice_dates(
                    charts.load_series("MKT.DXY").to_frame("v"), view.start, view.end
                )["v"],
                charts.slice_dates(
                    charts.load_series("MKT.EURUSD").to_frame("v"), view.start, view.end
                )["v"],
                "DXY (left)",
                "EUR/USD (right)",
                left_unit="Index",
                right_unit="USD per EUR",
            ),
            "They are close to mirror images by construction — the euro is the largest "
            "weight in the dollar index.",
        )
    with right:
        commodity_ids = charts.available(["MKT.BRENT", "MKT.WTI", "MKT.GOLD"])
        chosen_commodities = charts.series_multiselect(
            "Commodities",
            commodity_ids,
            default=charts.available(["MKT.BRENT", "MKT.WTI"]),
            key="gl_mkt_commodities",
        )
        charts.chart_block(
            "Commodities",
            charts.line_figure(
                view.apply(charts.load_wide(tuple(chosen_commodities))),
                labels=charts.labels_for(chosen_commodities),
                colors={
                    series_id: charts.COMPONENT_COLORS[i]
                    for i, series_id in enumerate(chosen_commodities)
                },
                unit="USD",
            ),
            "Brent and gold both feed Indonesia's story — one through the import bill, "
            "the other through reserves.",
        )

    with st.expander("Detail — market levels"):
        charts.render_detail_table(charts.latest_table(charts.available(MARKETS)))
        charts.note(
            "MKT.NIKKEI survived the Asia ex-Indonesia cut because that decision "
            "targeted stale CPI mirrors, not live market data."
        )
