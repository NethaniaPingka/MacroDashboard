"""The pages themselves, driven headlessly through Streamlit's AppTest.

These are smoke tests with teeth: they render each real page against the real
store and then move the slicers, because the branches that break are the ones a
first render never reaches — a partial component selection, a transform that
produces an all-NaN column, an empty date window.

Unlike the rest of the suite these need `data/macro.duckdb` to exist, so they
skip rather than fail on a fresh clone. They still need no network.
"""

from __future__ import annotations

import base64

import numpy as np
import plotly.io
import pytest

from macrodash import store
from macrodash.config import DB_PATH, PROJECT_ROOT

pytest.importorskip("streamlit.testing.v1", reason="Streamlit testing API unavailable")
from streamlit.testing.v1 import AppTest  # noqa: E402

# Absolute, because AppTest resolves a relative path against the file that calls
# it — which is this one, inside tests/, not the project root.
HOME = str(PROJECT_ROOT / "app" / "Home.py")
INDONESIA = str(PROJECT_ROOT / "app" / "pages" / "1_Indonesia.py")
GLOBAL = str(PROJECT_ROOT / "app" / "pages" / "2_Global.py")
DATA_MANAGER = str(PROJECT_ROOT / "app" / "pages" / "9_Data_Manager.py")

#: Rendering three pages against DuckDB is slower than the rest of the suite.
TIMEOUT = 120


def _store_is_populated() -> bool:
    if not DB_PATH.exists():
        return False
    with store.connect(read_only=True) as con:
        return con.execute("SELECT count(*) FROM observations").fetchone()[0] > 0


pytestmark = pytest.mark.skipif(
    not _store_is_populated(),
    reason="needs a populated data/macro.duckdb — run scripts/refresh_all.py",
)


def run(page: str) -> AppTest:
    app = AppTest.from_file(page, default_timeout=TIMEOUT).run()
    assert not app.exception, [str(e.value) for e in app.exception]
    return app


def figure_of(chart):
    """Rebuild the plotly Figure a chart element rendered.

    Read from the element's serialised spec rather than `.value`: Streamlit
    surfaces a plotly chart to AppTest as an UnknownElement whose `.value` goes
    looking for widget state a non-interactive chart never has. `from_json`
    rather than `json.loads`, because plotly encodes numeric arrays as base64
    and only its own reader turns them back into numbers.
    """
    return plotly.io.from_json(chart.proto.spec)


def traces(chart) -> list[str]:
    return [trace.name for trace in figure_of(chart).data]


def values(trace) -> np.ndarray:
    """The y values of a trace, decoded.

    Plotly 6 serialises numeric arrays as base64 in a {"dtype", "bdata"} wrapper
    and `from_json` hands that dict straight back rather than unpacking it, so
    the decode has to happen here.
    """
    data = trace.y
    if isinstance(data, dict) and "bdata" in data:
        return np.frombuffer(base64.b64decode(data["bdata"]), dtype=np.dtype(data["dtype"]))
    return np.asarray(data)


def chart_containing(app_tab, trace_name: str):
    """The one chart in a tab that carries a given trace, or None."""
    for chart in app_tab.get("plotly_chart"):
        if trace_name in traces(chart):
            return chart
    return None


@pytest.fixture(scope="module")
def indonesia() -> AppTest:
    return run(INDONESIA)


@pytest.fixture(scope="module")
def world() -> AppTest:
    return run(GLOBAL)


# ------------------------------------------------------------------- rendering

@pytest.mark.parametrize(
    "page",
    [HOME, INDONESIA, GLOBAL, DATA_MANAGER],
    ids=["home", "indonesia", "global", "data_manager"],
)
def test_every_page_renders(page):
    run(page)


def test_both_context_pages_use_the_same_four_tabs(indonesia, world):
    """The navigation requirement, checked against what actually rendered.

    Separate pages for the two contexts, four groups as tabs inside each, same
    order on both — otherwise flipping between them is not a comparison.
    """
    from macrodash.charts import TABS

    assert len(indonesia.tabs) == 4
    assert len(world.tabs) == 4
    assert tuple(tab.label for tab in indonesia.tabs) == TABS
    assert tuple(tab.label for tab in world.tabs) == TABS


@pytest.mark.parametrize("fixture", ["indonesia", "world"])
def test_every_tab_opens_with_four_cards_and_then_charts(fixture, request):
    """The band order the page is built around: cards first, charts after.

    A KPI row rendered below its charts would satisfy every other test here and
    still be the wrong page.
    """
    app = request.getfixturevalue(fixture)
    for tab in app.tabs:
        cards = [block for block in tab.markdown if "kpi-card" in block.value]
        assert len(cards) == 4, f"{tab.label} rendered {len(cards)} cards"
        assert tab.get("plotly_chart"), f"{tab.label} has no charts"


@pytest.mark.parametrize("fixture", ["indonesia", "world"])
def test_cards_report_a_current_a_previous_and_a_change(fixture, request):
    app = request.getfixturevalue(fixture)
    for tab in app.tabs:
        for card in (b for b in tab.markdown if "kpi-card" in b.value):
            assert "kpi-value" in card.value
            assert "prev" in card.value
            assert "kpi-change" in card.value
            # An em dash everywhere would mean the card found no data at all.
            assert card.value.count("—") < 3


def test_home_shows_both_contexts_without_mixing_them():
    app = run(HOME)
    headers = [block.value for block in app.subheader]
    assert "Indonesia" in headers
    assert "Global" in headers
    assert not app.tabs, "Home is an overview, not another tabbed context page"


# ---------------------------------------------------------------- interactions

def test_growth_transform_selector_redraws_without_error(indonesia):
    for choice in ("level", "mom", "yoy"):
        app = AppTest.from_file(INDONESIA, default_timeout=TIMEOUT)
        app.session_state["id_growth_transform"] = choice
        app.run()
        assert not app.exception, [str(e.value) for e in app.exception]


def test_dropping_a_gdp_component_hides_the_headline_it_can_no_longer_match():
    """A partial stack must not keep the reconciliation line above it.

    The line is the chart's own proof that the bars add up. Left drawn over an
    incomplete stack it becomes a claim the chart can no longer support, so the
    page removes it — and says why in the caption.
    """
    full = AppTest.from_file(INDONESIA, default_timeout=TIMEOUT).run()
    assert not full.exception, [str(e.value) for e in full.exception]
    complete = chart_containing(full.tabs[1], "Household consumption")
    assert complete is not None
    complete_names = traces(complete)
    assert "Real GDP, YoY" in complete_names
    assert "Statistical discrepancy" in complete_names

    partial = AppTest.from_file(INDONESIA, default_timeout=TIMEOUT)
    partial.session_state["id_growth_components"] = [
        "Household consumption",
        "Investment (GFCF)",
    ]
    partial.run()
    assert not partial.exception, [str(e.value) for e in partial.exception]

    reduced = chart_containing(partial.tabs[1], "Household consumption")
    assert reduced is not None
    reduced_names = traces(reduced)
    assert set(reduced_names) == {"Household consumption", "Investment (GFCF)"}
    assert "Real GDP, YoY" not in reduced_names

    captions = " ".join(block.value for block in partial.tabs[1].caption)
    assert "hidden" in captions


def test_selecting_no_components_at_all_is_survivable():
    app = AppTest.from_file(INDONESIA, default_timeout=TIMEOUT)
    app.session_state["id_growth_components"] = []
    app.run()
    assert not app.exception, [str(e.value) for e in app.exception]


def test_global_inflation_transforms_all_redraw(world):
    for choice in ("yoy", "ann_3m", "mom", "level"):
        app = AppTest.from_file(GLOBAL, default_timeout=TIMEOUT)
        app.session_state["gl_infl_transform"] = choice
        app.run()
        assert not app.exception, [str(e.value) for e in app.exception]


def test_rebasing_equities_anchors_every_line_at_one_hundred():
    """Indices on different scales only compare as shapes.

    Unrebased, the S&P and the Nikkei differ by an order of magnitude and the
    chart says nothing except which number is bigger.
    """
    plain = AppTest.from_file(GLOBAL, default_timeout=TIMEOUT)
    plain.session_state["gl_mkt_rebase"] = False
    plain.run()
    assert not plain.exception, [str(e.value) for e in plain.exception]

    rebased = AppTest.from_file(GLOBAL, default_timeout=TIMEOUT)
    rebased.session_state["gl_mkt_rebase"] = True
    rebased.run()
    assert not rebased.exception, [str(e.value) for e in rebased.exception]

    plain_chart = chart_containing(plain.tabs[3], "S&P 500")
    rebased_chart = chart_containing(rebased.tabs[3], "S&P 500")
    assert plain_chart is not None and rebased_chart is not None
    assert len(traces(plain_chart)) == len(traces(rebased_chart))

    for series in figure_of(rebased_chart).data:
        assert values(series)[0] == pytest.approx(100.0)

    # Unrebased, the raw index levels are nowhere near 100.
    assert all(values(series)[0] > 1_000 for series in figure_of(plain_chart).data)


def test_a_short_window_still_renders_every_tab():
    """1Y leaves a quarterly series with about four points and an annual one with
    one — the case where a transform legitimately returns nothing to draw."""
    app = AppTest.from_file(INDONESIA, default_timeout=TIMEOUT)
    app.session_state["indonesia_preset"] = "1Y"
    app.run()
    assert not app.exception, [str(e.value) for e in app.exception]
    for tab in app.tabs:
        assert tab.get("plotly_chart")


# --------------------------------------------------------------------- content

def test_the_rates_tab_leads_with_the_bi_rate(indonesia):
    """The real policy rate now exists, so the proxy no longer stands in for it.

    Before the BI Rate was entered by hand, ID.RATES.INTERBANK carried the rates
    cards with a caveat attached. Now that ID.POLICY.RATE holds data, a card
    reading "interbank" where policy is meant is the regression to catch.
    """
    cards = " ".join(
        block.value for block in indonesia.tabs[2].markdown if "kpi-card" in block.value
    ).lower()
    assert "bi rate" in cards
    assert "interbank" not in cards


def test_the_interbank_rate_is_never_labelled_a_policy_rate(indonesia):
    """The caveat survives the swap, because the series is still on the page.

    ID.RATES.INTERBANK is the overnight call money rate and is kept as a
    cross-check beside the policy rate. It must stay visibly distinguished from
    it — the two differ by whatever liquidity conditions are doing, which is
    precisely the reason to show both.
    """
    rates_tab = indonesia.tabs[2]
    text = " ".join(
        [block.value for block in rates_tab.markdown]
        + [block.value for block in rates_tab.caption]
    ).lower()
    assert "interbank" in text, "the cross-check series vanished from the page"
    assert "not the policy rate" in text


def test_the_bi_rate_is_drawn_as_a_step_and_the_proxy_is_not_drawn_at_all(indonesia):
    """An administered rate holds flat and then jumps.

    Joining its decisions with a sloping line draws a gradual move that never
    happened. Fed funds shares the axis and is a monthly average of a market
    rate, so it legitimately stays linear — which is why the shape is chosen per
    trace rather than per figure.
    """
    chart = chart_containing(indonesia.tabs[2], "BI Rate")
    assert chart is not None, "the policy-rate chart no longer draws the BI Rate"
    figure = figure_of(chart)
    by_name = {trace.name: trace for trace in figure.data}

    assert by_name["BI Rate"].line.shape == "hv"
    assert by_name["US federal funds rate"].line.shape != "hv"
    assert not any("interbank" in name.lower() for name in by_name)


def test_reserves_are_labelled_as_excluding_gold(indonesia):
    external = indonesia.tabs[3]
    text = " ".join(
        [block.value for block in external.markdown]
        + [block.value for block in external.caption]
    ).lower()
    assert "gold" in text


# ---------------------------------------------------------------- data manager

def test_the_data_manager_offers_the_series_it_exists_for():
    """ID.POLICY.RATE is inactive precisely because it has no data.

    A picker that filtered on `active` would hide the one series this page was
    built to fill in, and the omission would look like a working page.
    """
    app = run(DATA_MANAGER)
    # AppTest reports the formatted labels, not the underlying ids.
    options = app.selectbox(key="dm_series").options
    assert any(option.startswith("ID.POLICY.RATE") for option in options)


def test_the_data_manager_warns_before_typing_over_an_automated_series():
    """A value typed onto a FRED series is real until the next refresh, and then
    it is gone. The page has to say so where it can be seen."""
    app = AppTest.from_file(DATA_MANAGER, default_timeout=TIMEOUT)
    app.session_state["dm_series"] = "US.CPI.HEADLINE.IDX"
    app.run()
    assert not app.exception, [str(e.value) for e in app.exception]
    warnings = " ".join(block.value for block in app.warning).lower()
    assert "refresh_all" in warnings and "overwrit" in warnings


def test_an_empty_grid_offers_nothing_to_commit():
    """The page opens with a prefilled date and no value. That is not an entry,
    and must not present a live commit button."""
    app = run(DATA_MANAGER)
    commit = [b for b in app.button if b.key and b.key.startswith("dm_commit_")]
    assert commit == [] or commit[0].disabled


def test_reload_data_button_runs_clean_on_every_page():
    """Reloading opens the app's only *write* connection outside a commit.

    Clearing caches is safe by construction; syncing the catalog into
    series_meta is not, because DuckDB allows one writer. This checks the button
    completes rather than tracebacking, on each page that offers it.
    """
    for page, key in ((HOME, "home_reload"), (DATA_MANAGER, "dm_reload"), (INDONESIA, "indonesia_reload")):
        app = AppTest.from_file(page, default_timeout=TIMEOUT).run()
        assert not app.exception, [str(e.value) for e in app.exception]
        app.button(key=key).click().run()
        assert not app.exception, [str(e.value) for e in app.exception]
        assert not app.warning or all(
            "could not be synced" not in block.value for block in app.warning
        ), f"{key} reported a sync failure"
