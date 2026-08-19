"""Hand entry: alignment, the diff, and the round trip back out of the CSV.

The point of these is not that typing a number stores it. It is that the two
things which make hand entry dangerous — a value landing on a date the
transforms will never look for, and a hand-typed history existing only inside a
disposable database — are both prevented, and stay prevented.
"""

from __future__ import annotations

import pandas as pd
import pytest

from macrodash import manual
from macrodash.catalog import SeriesSpec
from macrodash.sources import get_fetcher
from macrodash.sources.manual import ManualFetcher
from macrodash.store import connect, read_series, upsert_observations


@pytest.fixture
def spec():
    return SeriesSpec(
        series_id="ID.POLICY.RATE",
        name="BI Rate",
        country="ID",
        category="rates",
        source="manual",
        source_id="bi/bi-rate",
        frequency="M",
        unit="Percent",
        default_transform="level",
    )


@pytest.fixture
def quarterly_spec():
    return SeriesSpec(
        series_id="TEST.GDP",
        name="Test GDP",
        country="ID",
        category="growth",
        source="manual",
        source_id="test",
        frequency="Q",
        unit="Bn IDR",
    )


def typed(pairs, source_ref=""):
    """The frame st.data_editor hands back."""
    return pd.DataFrame(
        {
            "obs_date": pd.to_datetime([d for d, _ in pairs]),
            "value": [v for _, v in pairs],
            "source_ref": [source_ref] * len(pairs),
        }
    )


# ==========================================================================
# period alignment
# ==========================================================================

@pytest.mark.parametrize(
    ("frequency", "typed_date", "expected"),
    [
        ("M", "2026-08-15", "2026-08-01"),
        ("M", "2026-08-01", "2026-08-01"),
        ("Q", "2026-08-15", "2026-07-01"),
        ("Q", "2026-01-01", "2026-01-01"),
        ("Q", "2026-12-31", "2026-10-01"),
        ("A", "2026-08-15", "2026-01-01"),
        ("W", "2026-08-19", "2026-08-17"),  # a Wednesday -> its Monday
        ("D", "2026-08-19", "2026-08-19"),
    ],
)
def test_dates_snap_to_the_start_of_their_period(frequency, typed_date, expected):
    assert manual.normalise_date(typed_date, frequency) == pd.Timestamp(expected)


def test_a_midmonth_date_does_not_silently_break_the_yoy_lookup(spec):
    """The bug this guards is invisible, which is why it is worth a test.

    Transforms in this project are calendar-based: YoY looks up the observation
    dated exactly twelve months earlier. A value typed as 2026-08-15 and stored
    as typed would never be found by that lookup — no error, just NaN a year
    later. Snapping is what keeps hand entry on the same footing as every
    fetcher, so the two dates below must land on the same key.
    """
    august = manual.normalise_date("2026-08-15", spec.frequency)
    year_earlier = manual.normalise_date("2025-08-03", spec.frequency)
    assert august - pd.DateOffset(months=12) == year_earlier


def test_next_period_follows_the_frequency():
    assert manual.next_period(pd.Timestamp("2026-08-01"), "M") == pd.Timestamp("2026-09-01")
    assert manual.next_period(pd.Timestamp("2026-10-01"), "Q") == pd.Timestamp("2027-01-01")
    assert manual.next_period(pd.Timestamp("2026-01-01"), "A") == pd.Timestamp("2027-01-01")


def test_next_period_on_an_empty_series_is_the_current_period(spec):
    assert manual.next_period(None, "M") == manual.normalise_date(pd.Timestamp.today(), "M")


# ==========================================================================
# the diff
# ==========================================================================

def test_entries_are_classified_against_what_is_stored(spec):
    stored = pd.Series(
        [6.00, 5.75], index=pd.to_datetime(["2026-06-01", "2026-07-01"])
    )
    prepared = manual.prepare(
        spec,
        typed([("2026-06-01", 6.00), ("2026-07-01", 5.50), ("2026-08-01", 5.25)]),
        stored=stored,
    )
    assert prepared.ok
    assert (prepared.n_unchanged, prepared.n_revised, prepared.n_new) == (1, 1, 1)

    revision = prepared.diff.loc[prepared.diff["status"] == "revision"].iloc[0]
    assert revision["stored_value"] == 5.75
    assert revision["value"] == 5.50


def test_blank_rows_are_not_entries(spec):
    grid = pd.DataFrame(
        {
            "obs_date": pd.to_datetime(["2026-08-01", None, None]),
            "value": [5.25, None, None],
            "source_ref": ["", "", ""],
        }
    )
    prepared = manual.prepare(spec, grid)
    assert len(prepared.entries) == 1
    assert not prepared.problems


def test_a_date_without_a_value_is_a_problem_not_a_zero(spec):
    """A gap and a zero are different things — the store's rule, applied earlier."""
    grid = pd.DataFrame(
        {
            "obs_date": pd.to_datetime(["2026-08-01"]),
            "value": [None],
            "source_ref": [""],
        }
    )
    prepared = manual.prepare(spec, grid)
    assert prepared.problems
    assert not prepared.ok


def test_two_rows_landing_on_one_period_block_the_commit(spec, quarterly_spec):
    """Snapping can collide rows that looked distinct when typed."""
    prepared = manual.prepare(
        quarterly_spec, typed([("2026-07-01", 100.0), ("2026-08-15", 101.0)])
    )
    assert not prepared.ok
    assert any("same period" in problem for problem in prepared.problems)


def test_the_preview_reports_the_date_it_moved(spec):
    prepared = manual.prepare(spec, typed([("2026-08-15", 5.25)]))
    assert prepared.diff.iloc[0]["moved_from"] == "2026-08-15"
    assert prepared.diff.iloc[0]["obs_date"] == pd.Timestamp("2026-08-01")


def test_a_default_source_reference_fills_only_blank_rows(spec):
    grid = typed([("2026-07-01", 5.5), ("2026-08-01", 5.25)])
    grid.loc[0, "source_ref"] = "BI press release, July"
    prepared = manual.prepare(spec, grid, source_ref="bi.go.id")
    assert list(prepared.entries["source_ref"]) == ["BI press release, July", "bi.go.id"]


# ==========================================================================
# sanity flags — warnings, never blocks
# ==========================================================================

def test_a_rate_typed_in_basis_points_is_queried(spec):
    """525 instead of 5.25 is the mistake this catches — plausible, and wrong."""
    prepared = manual.prepare(spec, typed([("2026-08-01", 525.0)]))
    assert prepared.warnings
    assert "units" in prepared.diff.iloc[0]["warning"]
    # Flagged, but still committable: the check asks, it does not refuse.
    assert prepared.ok


def test_a_plausible_rate_is_not_flagged(spec):
    prepared = manual.prepare(spec, typed([("2026-08-01", 5.25)]))
    assert prepared.warnings == []


def test_a_value_far_outside_its_own_history_is_queried(spec):
    # Anchored on today so the entry is never itself in the future, which would
    # trip the future-date check first and make this test pass for the wrong reason.
    this_month = manual.normalise_date(pd.Timestamp.today(), "M")
    stored = pd.Series(
        [5.0 + 0.1 * i for i in range(12)],
        index=pd.date_range(this_month - pd.DateOffset(months=12), periods=12, freq="MS"),
    )
    prepared = manual.prepare(spec, typed([(this_month, 19.0)]), stored=stored)
    assert any("history" in w for w in prepared.warnings)


def test_a_future_date_is_queried(spec):
    ahead = (pd.Timestamp.today() + pd.DateOffset(months=3)).strftime("%Y-%m-01")
    prepared = manual.prepare(spec, typed([(ahead, 5.25)]))
    assert any("future" in w for w in prepared.warnings)


# ==========================================================================
# the durable copy
# ==========================================================================

def test_save_writes_the_csv_and_the_store(spec, tmp_path):
    manual_dir = tmp_path / "manual"
    db = tmp_path / "test.duckdb"

    prepared = manual.prepare(spec, typed([("2026-07-01", 5.50), ("2026-08-01", 5.25)]))
    result = manual.save(spec, prepared.entries, manual_dir=manual_dir, db_path=db)

    assert result.rows_added == 2
    assert result.csv_path.exists()

    with connect(db_path=db) as con:
        stored = read_series(con, spec.series_id)
    assert list(stored["value"]) == [5.50, 5.25]


def test_a_manual_series_rebuilds_from_its_csv_alone(spec, tmp_path):
    """The reason the CSV is written first, expressed as a test.

    data/macro.duckdb is gitignored and the README tells you to delete it when
    something looks wrong. Hand-typed history has no upstream to re-fetch, so if
    it did not survive that deletion it would be gone for good. Deleting the
    store here and rebuilding through the ordinary fetcher path is the same
    operation a `--reset` refresh performs.
    """
    manual_dir = tmp_path / "manual"
    db = tmp_path / "test.duckdb"

    prepared = manual.prepare(spec, typed([("2026-07-01", 5.50), ("2026-08-01", 5.25)]))
    manual.save(spec, prepared.entries, manual_dir=manual_dir, db_path=db)

    db.unlink()  # the rebuild the README recommends

    rebuilt = ManualFetcher(manual_dir=manual_dir).fetch(spec)
    assert list(rebuilt["value"]) == [5.50, 5.25]
    assert list(rebuilt["obs_date"]) == [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-08-01")]


def test_saving_twice_revises_rather_than_duplicates(spec, tmp_path):
    manual_dir = tmp_path / "manual"
    db = tmp_path / "test.duckdb"

    first = manual.prepare(spec, typed([("2026-08-01", 5.25)]))
    manual.save(spec, first.entries, manual_dir=manual_dir, db_path=db)

    second = manual.prepare(spec, typed([("2026-08-01", 5.00)]))
    result = manual.save(spec, second.entries, manual_dir=manual_dir, db_path=db)

    assert result.total_manual_rows == 1
    assert result.rows_updated == 1
    on_disk = manual.read_manual(spec.series_id, manual_dir)
    assert len(on_disk) == 1
    assert on_disk.iloc[0]["value"] == 5.00


def test_the_csv_keeps_unscaled_values_so_it_still_matches_the_publication(tmp_path):
    """Scale belongs to the catalog, not to the typist.

    ID.FX.RESERVES is published in millions and stored in billions. Someone
    typing from the release types what the release says; the CSV therefore has
    to keep that number, or it stops being checkable against its own source_ref.
    """
    scaled = SeriesSpec(
        series_id="TEST.RESERVES",
        name="Test reserves",
        country="ID",
        category="external",
        source="manual",
        source_id="test",
        frequency="M",
        unit="Bn USD",
        scale=0.001,
    )
    manual_dir = tmp_path / "manual"
    db = tmp_path / "test.duckdb"

    prepared = manual.prepare(scaled, typed([("2026-08-01", 152_000.0)]))
    manual.save(scaled, prepared.entries, manual_dir=manual_dir, db_path=db)

    assert manual.read_manual("TEST.RESERVES", manual_dir).iloc[0]["value"] == 152_000.0
    with connect(db_path=db) as con:
        assert read_series(con, "TEST.RESERVES").iloc[0]["value"] == 152.0

    # And the fetcher applies it identically on the way back in.
    rebuilt = ManualFetcher(manual_dir=manual_dir).fetch(scaled)
    assert rebuilt.iloc[0]["value"] == 152.0


def test_an_unentered_series_is_a_fetch_error_not_a_missing_series(spec, tmp_path):
    """Only SeriesNotFound may deactivate a catalog entry.

    "Nobody has typed this yet" is a statement about us, not about the series,
    so audit_catalog.py --fix-inactive must not read it as one.
    """
    from macrodash.sources.base import FetchError, SeriesNotFound

    fetcher = ManualFetcher(manual_dir=tmp_path / "empty")
    with pytest.raises(FetchError) as raised:
        fetcher.fetch(spec)
    assert not isinstance(raised.value, SeriesNotFound)


def test_manual_coverage_reports_what_has_been_typed(spec, tmp_path):
    manual_dir = tmp_path / "manual"
    db = tmp_path / "test.duckdb"
    prepared = manual.prepare(spec, typed([("2026-07-01", 5.50), ("2026-08-01", 5.25)]))
    manual.save(spec, prepared.entries, manual_dir=manual_dir, db_path=db)

    coverage = manual.manual_coverage(manual_dir)
    row = coverage.loc[coverage["series_id"] == spec.series_id].iloc[0]
    assert row["n_obs"] == 2
    assert pd.Timestamp(row["last_obs"]) == pd.Timestamp("2026-08-01")


def test_both_manual_source_names_resolve():
    """`bps_manual` predates the rename and may still appear in a catalog file."""
    assert isinstance(get_fetcher("manual"), ManualFetcher)
    assert isinstance(get_fetcher("bps_manual"), ManualFetcher)


# ==========================================================================
# event-dated series — two decisions inside one calendar month
# ==========================================================================

@pytest.fixture
def policy_spec():
    """The BI Rate as catalogued after 2026-08-19: an event series, not a monthly one."""
    return SeriesSpec(
        series_id="ID.POLICY.RATE",
        name="BI Rate",
        country="ID",
        category="rates",
        source="manual",
        source_id="bi/bi-rate",
        frequency="D",
        unit="Percent",
        default_transform="level",
        staleness_days=400,
    )


def test_two_decisions_in_one_month_both_survive(policy_spec):
    """The case that forced the frequency change.

    Bank Indonesia can move twice inside one calendar month. On a monthly grid
    both announcements snap to the 1st, collide, and the whole batch is refused
    as ambiguous — which is correct behaviour for a monthly series and useless
    for a policy rate. Dated to the decisions themselves, both stand.
    """
    prepared = manual.prepare(
        policy_spec, typed([("2026-06-04", 5.50), ("2026-06-18", 5.75)])
    )
    assert prepared.ok, prepared.problems
    assert prepared.n_new == 2
    assert list(prepared.entries["obs_date"]) == [
        pd.Timestamp("2026-06-04"),
        pd.Timestamp("2026-06-18"),
    ]


def test_an_event_dated_entry_is_not_snapped_at_all(policy_spec):
    """The announcement date IS the observation date — nothing to align to."""
    prepared = manual.prepare(policy_spec, typed([("2026-06-18", 5.75)]))
    assert prepared.diff.iloc[0]["obs_date"] == pd.Timestamp("2026-06-18")
    assert prepared.diff.iloc[0]["moved_from"] == ""


def test_the_same_two_dates_would_still_collide_on_a_monthly_series(spec):
    """The guard is not removed, only made inapplicable to event series.

    A genuinely monthly series with two rows for one month is still ambiguous,
    and still refused.
    """
    prepared = manual.prepare(spec, typed([("2026-06-04", 5.50), ("2026-06-18", 5.75)]))
    assert not prepared.ok
    assert any("same period" in problem for problem in prepared.problems)


def test_the_next_entry_for_an_event_series_is_dated_today(policy_spec):
    """Not "one day after the last decision", which could be months ago."""
    assert manual.next_period(pd.Timestamp("2026-01-15"), "D") == pd.Timestamp.today().normalize()


# ==========================================================================
# the decimal-fraction trap
# ==========================================================================

def test_a_rate_typed_as_a_decimal_fraction_is_queried(policy_spec):
    """The mistake that actually reached the store, on 2026-08-19.

    A 5.75% policy rate entered as 0.0575 sits well inside the plausible
    percent band, so the basis-points check could not see it. It is wrong by
    exactly the factor that flips the real policy rate from +2.87% to -2.82%
    against 2.88% inflation — a sign error dressed as a number.
    """
    prepared = manual.prepare(policy_spec, typed([("2026-06-18", 0.0575)]))
    assert prepared.warnings
    warning = prepared.diff.iloc[0]["warning"]
    assert "decimal fraction" in warning and "5.75" in warning


def test_a_genuinely_near_zero_rate_is_not_nagged_about(policy_spec):
    """A validator that cries wolf gets ignored when it is right.

    A ZIRP-era policy rate really does sit below 1%, so once a series' own
    history lives down there the check stands down.
    """
    zirp = pd.Series(
        [0.10, 0.10, 0.25, 0.25, 0.25, 0.10, 0.10, 0.25, 0.25, 0.10],
        index=pd.date_range("2024-01-01", periods=10, freq="MS"),
    )
    prepared = manual.prepare(policy_spec, typed([("2026-06-18", 0.25)]), stored=zirp)
    assert prepared.warnings == []


# ==========================================================================
# the store is a projection of the CSV, not a running total
# ==========================================================================

def test_redating_a_series_does_not_leave_orphans_behind(policy_spec, tmp_path):
    """The failure that reached a live chart on 2026-08-20.

    The BI Rate was first entered on month-starts, then re-entered on the actual
    announcement dates. An upsert adds and revises but never removes, so all
    twenty original rows survived underneath the new ones — and the chart drew a
    policy move that never happened: 5.25, up to 5.75 on 1 June (stale), then
    "down" to 5.50 on the 9th.
    """
    manual_dir, db = tmp_path / "manual", tmp_path / "test.duckdb"

    first = manual.prepare(policy_spec, typed([("2026-05-01", 5.25), ("2026-06-01", 5.75)]))
    manual.save(policy_spec, first.entries, manual_dir=manual_dir, db_path=db)

    # Re-entered on the real decision dates. The CSV is rewritten wholesale.
    manual.write_manual(
        policy_spec.series_id,
        pd.DataFrame(columns=manual.MANUAL_COLUMNS),  # the file replaced wholesale
        manual_dir=manual_dir,
    )
    redated = manual.prepare(
        policy_spec, typed([("2026-05-20", 5.25), ("2026-06-09", 5.50), ("2026-06-18", 5.75)])
    )
    result = manual.save(policy_spec, redated.entries, manual_dir=manual_dir, db_path=db)

    assert result.rows_removed == 2, "the month-start rows should have been pruned"
    with connect(db_path=db) as con:
        stored = read_series(con, policy_spec.series_id)

    assert list(stored["obs_date"].dt.strftime("%Y-%m-%d")) == [
        "2026-05-20", "2026-06-09", "2026-06-18",
    ]
    assert "2026-06-01" not in set(stored["obs_date"].dt.strftime("%Y-%m-%d"))


def test_saving_leaves_the_store_matching_a_rebuild(policy_spec, tmp_path):
    """The invariant the prune exists to hold.

    Committing should leave the store in the state a `--reset` refresh would
    produce. If those two ever disagree, the dashboard shows one thing and a
    rebuild shows another, and neither is obviously wrong.
    """
    manual_dir, db = tmp_path / "manual", tmp_path / "test.duckdb"

    manual.save(
        policy_spec,
        manual.prepare(policy_spec, typed([("2026-06-01", 5.75)])).entries,
        manual_dir=manual_dir, db_path=db,
    )
    manual.write_manual(
        policy_spec.series_id,
        pd.DataFrame(columns=manual.MANUAL_COLUMNS),  # the file replaced wholesale
        manual_dir=manual_dir,
    )
    manual.save(
        policy_spec,
        manual.prepare(policy_spec, typed([("2026-06-09", 5.50), ("2026-06-18", 5.75)])).entries,
        manual_dir=manual_dir, db_path=db,
    )

    with connect(db_path=db) as con:
        after_save = read_series(con, policy_spec.series_id).reset_index(drop=True)

    rebuilt = ManualFetcher(manual_dir=manual_dir).fetch(policy_spec).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        after_save.loc[:, ["obs_date", "value"]], rebuilt.loc[:, ["obs_date", "value"]]
    )


def test_an_api_backed_series_is_never_pruned_to_the_manual_file(tmp_path):
    """Typing one provisional value must not delete a vendor's whole history.

    The prune is only correct where the file IS the history. On a FRED series
    the store holds years the manual CSV knows nothing about.
    """
    fred_spec = SeriesSpec(
        series_id="US.CPI.TEST",
        name="US CPI (test)",
        country="US",
        category="inflation",
        source="fred",
        source_id="CPIAUCSL",
        frequency="M",
        unit="Index",
    )
    manual_dir, db = tmp_path / "manual", tmp_path / "test.duckdb"

    vendor = pd.DataFrame(
        {
            "series_id": "US.CPI.TEST",
            "obs_date": pd.date_range("2025-01-01", periods=12, freq="MS"),
            "value": range(100, 112),
        }
    )
    with connect(db_path=db) as con:
        upsert_observations(con, vendor)

    provisional = manual.prepare(fred_spec, typed([("2026-01-01", 999.0)]))
    result = manual.save(fred_spec, provisional.entries, manual_dir=manual_dir, db_path=db)

    assert result.rows_removed == 0
    with connect(db_path=db) as con:
        assert len(read_series(con, "US.CPI.TEST")) == 13  # 12 vendor + 1 typed
