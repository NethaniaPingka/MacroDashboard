"""Indonesia — four indicator groups as tabs, each read top to bottom.

Every tab is the same three bands: four cards, then the charts that explain
those four numbers, then the detail underneath. `2_Global.py` is the same file
with different series, and `charts.TABS` keeps the tab order identical so
flipping between the two pages reads as a comparison.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from macrodash import charts, transforms

st.set_page_config(page_title="Indonesia · Macro Dashboard", page_icon="🇮🇩", layout="wide")

#: Bank Indonesia's inflation target, 2.5% ± 1%, in force for 2024-2026. Drawn as
#: a band behind the inflation chart because "is inflation on target" is a
#: different question from "is inflation high", and the chart should answer both.
#: If BI restates the target this needs updating — it is a policy setting, not
#: data, so nothing upstream will correct it.
BI_TARGET = (1.5, 3.5)

INFLATION = ["ID.CPI.INFLATION.YOY", "ID.CPI.INFLATION.MOM",
             "ID.CPI.HEADLINE.IDX", "ID.CPI.INFLATION.ANNUAL"]
GROWTH = ["ID.GDP.REAL"]
GDP_COMPONENTS = {
    "Household consumption": "ID.GDP.CONSUMPTION",
    "Government": "ID.GDP.GOVERNMENT",
    "Investment (GFCF)": "ID.GDP.GFCF",
    "Exports": "ID.GDP.EXPORTS",
    "Imports": "ID.GDP.IMPORTS",
    "Inventories": "ID.GDP.INVENTORY",
    "NPISH": "ID.GDP.NPISH",
    "Statistical discrepancy": "ID.GDP.DISCREPANCY",
}
#: Imports subtract from GDP. They are stored positive, as BPS publishes them,
#: so the sign is applied once, here, at the point of use.
SUBTRACTS = {"Imports"}

#: The BI Rate leads everywhere it appears. ID.RATES.INTERBANK is kept in the
#: list so the detail table can still show the market rate beside the policy
#: rate — the gap between them is a liquidity signal — but it no longer stands
#: in for policy on any card or chart.
RATES = ["ID.POLICY.RATE", "US.POLICY.RATE", "ID.RATES.INTERBANK"]
EXTERNAL = ["ID.EXPORTS", "ID.IMPORTS", "ID.TRADE.BALANCE", "ID.FX.RESERVES"]
MARKETS = ["MKT.USDIDR", "MKT.JKSE"]

ALL_SERIES = INFLATION + GROWTH + list(GDP_COMPONENTS.values()) + RATES + EXTERNAL + MARKETS

charts.inject_css()
charts.page_header(
    "🇮🇩 Indonesia",
    "Inflation, growth, rates and the external accounts",
    ALL_SERIES,
)
view = charts.sidebar_controls("indonesia")

tab_inflation, tab_growth, tab_rates, tab_external = st.tabs(charts.TABS)


# ==========================================================================
# Inflation
# ==========================================================================
with tab_inflation:
    monthly_inflation = charts.load_series("ID.CPI.INFLATION.MOM")
    # BPS publishes monthly inflation as a rate, not an index, so momentum has to
    # be compounded back into one before ann_3m can read it. Chaining the rates is
    # exact — the index this rebuilds is the one the rates were computed from.
    rebuilt_index = (1.0 + monthly_inflation.dropna() / 100.0).cumprod() * 100.0
    momentum = transforms.ann_3m(rebuilt_index, "M")

    policy_rate = charts.load_series("ID.POLICY.RATE")
    headline_yoy = charts.load_series("ID.CPI.INFLATION.YOY")
    real_rate = transforms.real_rate(policy_rate, headline_yoy)

    cards = charts.kpis_from(
        ["ID.CPI.INFLATION.YOY", "ID.CPI.INFLATION.MOM"],
        labels={
            "ID.CPI.INFLATION.YOY": "Headline inflation, YoY",
            "ID.CPI.INFLATION.MOM": "Monthly inflation, MoM",
        },
        captions={"ID.CPI.INFLATION.YOY": f"BI target {BI_TARGET[0]}–{BI_TARGET[1]}%"},
    )
    cards += [
        card
        for card in (
            charts.kpi_from_series(
                "3-month momentum, annualised",
                momentum,
                caption="Compounded from monthly prints — turns before YoY does",
            ),
            charts.kpi_from_series(
                "Real policy rate",
                real_rate,
                caption="BI Rate less headline inflation",
            ),
        )
        if card is not None
    ]
    charts.kpi_row(cards)

    st.divider()

    left, right = st.columns([3, 2])

    with left:
        figure = charts.line_figure(
            view.apply(charts.load_wide(("ID.CPI.INFLATION.YOY",))),
            labels={"ID.CPI.INFLATION.YOY": "Headline YoY"},
            colors={"ID.CPI.INFLATION.YOY": charts.COMPONENT_COLORS[0]},
            unit="Percent",
        )
        figure.add_hrect(
            y0=BI_TARGET[0], y1=BI_TARGET[1],
            fillcolor="#16a34a", opacity=0.10, line_width=0,
            annotation_text="BI target band", annotation_position="top left",
        )
        charts.chart_block(
            "Headline inflation against the target band",
            figure,
            f"Shaded: Bank Indonesia's {BI_TARGET[0]}–{BI_TARGET[1]}% target.",
        )

    with right:
        bars = view.apply(charts.load_wide(("ID.CPI.INFLATION.MOM",)))
        momentum_figure = charts.bar_figure(
            bars["ID.CPI.INFLATION.MOM"] if not bars.empty else pd.Series(dtype="float64"),
            name="MoM",
            unit="Percent",
        )
        windowed_momentum = charts.slice_dates(momentum.to_frame("m"), view.start, view.end)["m"]
        if not windowed_momentum.dropna().empty:
            momentum_figure.add_scatter(
                x=windowed_momentum.dropna().index,
                y=windowed_momentum.dropna().to_numpy(),
                name="3m annualised",
                line={"width": 2, "color": charts.REFERENCE_LINE},
                hovertemplate="%{x|%b %Y}<br>%{y:,.2f}%<extra>3m annualised</extra>",
            )
            momentum_figure.update_layout(showlegend=True)
        charts.chart_block(
            "Monthly prints and where they are heading",
            momentum_figure,
            "Bars are single months; the line compounds three of them into an annual rate.",
        )

    # The cross-source check from Phase 2, kept in the app rather than in a
    # script: BPS's published YoY against a YoY computed independently from
    # FRED's separate CPI index. They agreed to a mean 0.091pp over the overlap,
    # which is why the BPS series can be trusted where the FRED one has stopped.
    fred_yoy = charts.transformed("ID.CPI.HEADLINE.IDX", "yoy")
    comparison = pd.DataFrame(
        {"BPS, published": headline_yoy, "FRED CPI index, computed": fred_yoy}
    ).sort_index()
    charts.chart_block(
        "Two independent sources for the same number",
        charts.line_figure(
            view.apply(comparison),
            colors={
                "BPS, published": charts.COMPONENT_COLORS[0],
                "FRED CPI index, computed": charts.COMPONENT_COLORS[1],
            },
            unit="Percent",
        ),
        "The FRED mirror was abandoned upstream in April 2025, so only the BPS line "
        "continues. Where they overlap they agree — that agreement is the reason to "
        "trust the part that carries on alone.",
    )

    with st.expander("Detail — seasonality and the underlying numbers"):
        st.markdown("**Monthly inflation by calendar month**")
        st.caption(
            "Indonesian prices have a pronounced Ramadan and harvest pattern. Reading "
            "down a column shows how unusual a given month's print really was."
        )
        seasonal = monthly_inflation.dropna().to_frame("value")
        if not seasonal.empty:
            seasonal["Year"] = seasonal.index.year
            seasonal["Month"] = seasonal.index.strftime("%b")
            pivot = seasonal.pivot_table(
                index="Year", columns="Month", values="value", aggfunc="last"
            )
            order = [m for m in
                     ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                     if m in pivot.columns]
            pivot = pivot[order].sort_index(ascending=False)
            # Plotly rather than a pandas Styler gradient: `background_gradient`
            # pulls in matplotlib, which is not a dependency of this project and
            # is not worth becoming one for a single heatmap.
            st.plotly_chart(
                charts.heatmap_figure(
                    pivot,
                    unit="Percent",
                    height=max(240, 26 * len(pivot) + 90),
                ),
                width="stretch",
            )

        st.markdown("**All inflation series**")
        charts.render_detail_table(charts.latest_table(charts.available(INFLATION)))
        charts.note(
            "ID.CPI.HEADLINE.IDX is backfill only — abandoned upstream at April 2025 "
            "and kept for the 1990-2025 depth the current BPS variables do not have."
        )


# ==========================================================================
# Growth
# ==========================================================================
with tab_growth:
    gdp = charts.load_series("ID.GDP.REAL")
    gdp_yoy = transforms.yoy(gdp, "Q")

    cards = [
        card
        for card in (
            charts.kpi_from_series(
                "Real GDP growth, YoY", gdp_yoy,
                frequency="Q", caption="Not seasonally adjusted — YoY is the honest read",
            ),
            charts.transformed_kpi("ID.GDP.CONSUMPTION", "yoy", "Household consumption, YoY"),
            charts.transformed_kpi("ID.GDP.GFCF", "yoy", "Investment (GFCF), YoY"),
            charts.transformed_kpi("ID.GDP.GOVERNMENT", "yoy", "Government consumption, YoY"),
        )
        if card is not None
    ]
    charts.kpi_row(cards)

    st.divider()

    growth_transform = charts.transform_selector(
        "id_growth_transform",
        options=("yoy", "level", "mom"),
        default="yoy",
        label="Show real GDP as",
    )
    gdp_frame = charts.transform_frame(
        charts.load_wide(("ID.GDP.REAL",)), growth_transform, {"ID.GDP.REAL": "Q"}
    )
    charts.chart_block(
        "Real GDP",
        charts.line_figure(
            view.apply(gdp_frame),
            labels={"ID.GDP.REAL": "Real GDP"},
            colors={"ID.GDP.REAL": charts.COMPONENT_COLORS[0]},
            unit="Bn IDR, constant 2010 prices",
            transform=growth_transform,
            zero_line=growth_transform != "level",
        ),
        "Indonesian GDP is not seasonally adjusted, so the quarter-on-quarter reading "
        "mostly measures the calendar. Year-on-year is the one that means something.",
    )

    # The contribution chart is the reason the eight component series exist. The
    # headline line drawn over the stack is what makes it checkable: bars that do
    # not reach the line mean a component is missing or the maths is wrong.
    chosen = charts.series_multiselect(
        "Components in the stack",
        list(GDP_COMPONENTS),
        key="id_growth_components",
    )
    component_frame = charts.load_wide(tuple(GDP_COMPONENTS[name] for name in chosen))
    if not component_frame.empty:
        components = {
            name: (
                -component_frame[GDP_COMPONENTS[name]]
                if name in SUBTRACTS
                else component_frame[GDP_COMPONENTS[name]]
            ).dropna()
            for name in chosen
            if GDP_COMPONENTS[name] in component_frame.columns
        }
        contributions = transforms.contribution_table(components, gdp, "Q")
        complete = len(chosen) == len(GDP_COMPONENTS)
        charts.chart_block(
            "What is driving growth",
            charts.stacked_contribution_figure(
                view.apply(contributions),
                headline=charts.slice_dates(gdp_yoy.to_frame("g"), view.start, view.end)["g"]
                if complete
                else None,
                headline_name="Real GDP, YoY",
            ),
            "Percentage points of the year-on-year growth rate. With every component "
            "shown, the bars reconcile to the headline line exactly."
            if complete
            else "Percentage points of year-on-year growth. The headline line is hidden "
                 "because a partial selection cannot reconcile to it.",
        )

    with st.expander("Detail — the expenditure breakdown", expanded=True):
        latest_quarter = gdp.dropna().index[-1] if not gdp.dropna().empty else None
        if latest_quarter is not None:
            full_frame = charts.load_wide(tuple(GDP_COMPONENTS.values()))
            full_components = {
                name: (
                    -full_frame[series_id] if name in SUBTRACTS else full_frame[series_id]
                ).dropna()
                for name, series_id in GDP_COMPONENTS.items()
                if series_id in full_frame.columns
            }
            full_contributions = transforms.contribution_table(full_components, gdp, "Q")
            gdp_now = float(gdp.loc[latest_quarter])

            rows = []
            for name, series_id in GDP_COMPONENTS.items():
                if series_id not in full_frame.columns:
                    continue
                published = full_frame[series_id]
                if latest_quarter not in published.index or pd.isna(published.loc[latest_quarter]):
                    continue
                level_now = float(published.loc[latest_quarter])
                signed = -level_now if name in SUBTRACTS else level_now
                component_yoy = transforms.yoy(published.dropna(), "Q")
                rows.append(
                    {
                        "Component": name,
                        "Level (Bn IDR)": level_now,
                        "Share of GDP": signed / gdp_now * 100.0,
                        "YoY %": float(component_yoy.get(latest_quarter, float("nan"))),
                        "Contribution (pp)": float(
                            full_contributions[name].get(latest_quarter, float("nan"))
                        )
                        if name in full_contributions.columns
                        else float("nan"),
                    }
                )

            detail = pd.DataFrame(rows).sort_values("Contribution (pp)", ascending=False)
            st.caption(
                f"{charts.format_period(latest_quarter, 'Q')} · real GDP "
                f"{gdp_now:,.0f} Bn IDR, {gdp_yoy.loc[latest_quarter]:+.2f}% YoY. "
                "Imports are shown with the sign they carry into GDP."
            )
            st.dataframe(
                detail,
                width="stretch",
                hide_index=True,
                column_config={
                    "Level (Bn IDR)": st.column_config.NumberColumn(format="%,.0f"),
                    "Share of GDP": st.column_config.NumberColumn(format="%.1f%%"),
                    "YoY %": st.column_config.NumberColumn(format="%+.2f"),
                    "Contribution (pp)": st.column_config.ProgressColumn(
                        format="%+.2f",
                        min_value=float(detail["Contribution (pp)"].min(skipna=True) or 0),
                        max_value=float(detail["Contribution (pp)"].max(skipna=True) or 1),
                    ),
                },
            )
            charts.note(
                "Components sum to the total to within 0.4 Bn IDR on a 3.6m base — "
                "rounding. The statistical discrepancy is charted rather than dropped "
                "so the stack cannot quietly misattribute it."
            )


# ==========================================================================
# Rates & Money
# ==========================================================================
with tab_rates:
    policy_rate = charts.load_series("ID.POLICY.RATE")
    fed_funds = charts.load_series("US.POLICY.RATE")
    inflation_yoy = charts.load_series("ID.CPI.INFLATION.YOY")
    real_policy = transforms.real_rate(policy_rate, inflation_yoy)
    differential = transforms.spread(policy_rate, fed_funds)
    usdidr = charts.load_series("MKT.USDIDR")

    cards = charts.kpis_from(
        ["ID.POLICY.RATE"],
        labels={"ID.POLICY.RATE": "BI Rate"},
        captions={"ID.POLICY.RATE": "Bank Indonesia policy rate"},
    )
    cards += [
        card
        for card in (
            charts.kpi_from_series(
                "Real policy rate", real_policy,
                caption="BI Rate less headline inflation",
            ),
            charts.kpi_from_series(
                "Spread over fed funds", differential,
                unit="Percentage points",
                caption="What compensates for holding rupiah",
            ),
            charts.kpi_from_series(
                "USD/IDR", usdidr, unit="IDR per USD", frequency="D",
                caption="Higher means a weaker rupiah",
            ),
        )
        if card is not None
    ]
    charts.kpi_row(cards)

    charts.note(
        "The BI Rate is entered by hand from Bank Indonesia's own publication — it has "
        "no API and FRED does not carry it — and is dated to the Board of Governors "
        "decisions that moved it, so the line steps rather than slopes. "
        "ID.RATES.INTERBANK, the overnight call money rate, is a market rate and is "
        "kept in the detail table below as a cross-check; it is not the policy rate."
    )

    st.divider()

    charts.chart_block(
        "Indonesian and US policy rates",
        charts.line_figure(
            view.apply(charts.load_wide(("ID.POLICY.RATE", "US.POLICY.RATE"))),
            labels=charts.labels_for(["ID.POLICY.RATE", "US.POLICY.RATE"]),
            colors=charts.series_colors(["ID.POLICY.RATE", "US.POLICY.RATE"], charts.catalog()),
            unit="Percent",
            step=("ID.POLICY.RATE",),
        ),
        "Two countries, so the lines take their country colours. The BI Rate steps, "
        "because an administered rate holds flat and then jumps; fed funds is a monthly "
        "average of a market rate, so it does not. The gap is what the rupiah is paid for.",
    )

    left, right = st.columns(2)
    with left:
        charts.chart_block(
            "Rate differential over fed funds",
            charts.bar_figure(
                charts.slice_dates(differential.to_frame("d"), view.start, view.end)["d"],
                name="ID − US",
                unit="Percentage points",
            ),
            "Narrowing differentials have historically preceded rupiah weakness.",
        )
    with right:
        charts.chart_block(
            "Real policy rate",
            charts.bar_figure(
                charts.slice_dates(real_policy.to_frame("r"), view.start, view.end)["r"],
                name="Real rate",
                unit="Percent",
            ),
            "Negative means the nominal rate is not keeping up with inflation.",
        )

    with st.expander("Detail — rate series"):
        charts.render_detail_table(charts.latest_table(charts.available(RATES)))


# ==========================================================================
# External & Markets
# ==========================================================================
with tab_external:
    trade = charts.load_wide(("ID.EXPORTS", "ID.IMPORTS", "ID.TRADE.BALANCE"))

    cards = charts.kpis_from(
        ["ID.TRADE.BALANCE", "ID.FX.RESERVES"],
        labels={
            "ID.TRADE.BALANCE": "Trade balance",
            "ID.FX.RESERVES": "FX reserves",
        },
        captions={"ID.FX.RESERVES": "Excludes gold — below BI's headline figure"},
    )
    cards += [
        card
        for card in (
            charts.transformed_kpi("ID.EXPORTS", "yoy", "Exports, YoY"),
            charts.transformed_kpi("ID.IMPORTS", "yoy", "Imports, YoY"),
        )
        if card is not None
    ]
    charts.kpi_row(cards)

    st.divider()

    trade_transform = charts.transform_selector(
        "id_trade_transform", options=("level", "yoy"), default="level",
        label="Show trade flows as",
    )
    flows = charts.transform_frame(
        trade[["ID.EXPORTS", "ID.IMPORTS"]] if not trade.empty else trade,
        trade_transform,
        {"ID.EXPORTS": "M", "ID.IMPORTS": "M"},
    )
    charts.chart_block(
        "Exports and imports",
        charts.line_figure(
            view.apply(flows),
            labels={"ID.EXPORTS": "Exports", "ID.IMPORTS": "Imports"},
            colors={
                "ID.EXPORTS": charts.COMPONENT_COLORS[0],
                "ID.IMPORTS": charts.COMPONENT_COLORS[1],
            },
            unit="Mn USD",
            transform=trade_transform,
            zero_line=trade_transform != "level",
        ),
        "Monthly customs data in current US dollars — a different measure from the "
        "national-accounts exports in the Growth tab, and not comparable with it.",
    )

    left, right = st.columns(2)
    with left:
        charts.chart_block(
            "Trade balance",
            charts.bar_figure(
                view.apply(trade)["ID.TRADE.BALANCE"]
                if not trade.empty
                else pd.Series(dtype="float64"),
                name="Balance",
                unit="Mn USD",
            ),
            "Starts 2021: BPS switched this variable from billions to millions of USD "
            "under the same id, with nothing in the metadata to say so.",
        )
    with right:
        charts.chart_block(
            "FX reserves",
            charts.line_figure(
                view.apply(charts.load_wide(("ID.FX.RESERVES",))),
                labels={"ID.FX.RESERVES": "Reserves ex gold"},
                colors={"ID.FX.RESERVES": charts.COMPONENT_COLORS[2]},
                unit="Bn USD",
            ),
            "Excludes gold, so it prints a little below the cadangan devisa figure "
            "Bank Indonesia announces.",
        )

    charts.chart_block(
        "The rupiah and the Jakarta Composite",
        charts.dual_axis_figure(
            charts.slice_dates(
                charts.load_series("MKT.USDIDR").to_frame("v"), view.start, view.end
            )["v"],
            charts.slice_dates(
                charts.load_series("MKT.JKSE").to_frame("v"), view.start, view.end
            )["v"],
            "USD/IDR (left)",
            "JKSE (right)",
            left_unit="IDR per USD",
            right_unit="Index",
        ),
        "Two scales, so read the shapes rather than the gap between the lines.",
    )

    with st.expander("Detail — external accounts and markets"):
        st.markdown("**Latest values**")
        charts.render_detail_table(charts.latest_table(charts.available(EXTERNAL + MARKETS)))

        st.markdown("**Accounting identity check**")
        st.caption(
            "Exports − imports − balance should be exactly zero from 2021. This is the "
            "check that caught the unit change; it runs against live data every time "
            "this page loads."
        )
        if not trade.empty and {"ID.EXPORTS", "ID.IMPORTS", "ID.TRADE.BALANCE"} <= set(trade.columns):
            residual = (
                trade["ID.EXPORTS"] - trade["ID.IMPORTS"] - trade["ID.TRADE.BALANCE"]
            ).dropna()
            recent = residual.loc[residual.index >= "2021-01-01"]
            worst = recent.abs().max() if not recent.empty else float("nan")
            if pd.notna(worst) and worst < 0.5:
                st.success(
                    f"Identity holds across {len(recent)} months since 2021 — "
                    f"largest residual {worst:,.2f} Mn USD."
                )
            else:
                st.error(
                    f"Identity broken: largest residual {worst:,.2f} Mn USD. "
                    "Check whether BPS changed the unit again."
                )
