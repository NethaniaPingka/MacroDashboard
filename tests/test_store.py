"""Store round-trip behaviour: idempotency, revisions, and reads."""

from __future__ import annotations

import pandas as pd
import pytest

from macrodash.catalog import SeriesSpec
from macrodash.store import (
    connect,
    coverage,
    latest_values,
    delete_observations,
    freshness,
    read_series,
    read_wide,
    series_as_pandas,
    sync_series_meta,
    upsert_observations,
)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "test.duckdb"


@pytest.fixture
def spec():
    return SeriesSpec(
        series_id="TEST.CPI",
        name="Test CPI",
        country="ID",
        category="inflation",
        source="seed",
        source_id="test",
        frequency="M",
        default_transform="yoy",
    )


def frame(values, start="2024-01-01", series_id="TEST.CPI"):
    dates = pd.date_range(start, periods=len(values), freq="MS")
    return pd.DataFrame({"series_id": series_id, "obs_date": dates, "value": values})


def test_upsert_is_idempotent(db, spec):
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        added, updated = upsert_observations(con, frame([100.0, 101.0, 102.0]))
        assert (added, updated) == (3, 0)

        added, updated = upsert_observations(con, frame([100.0, 101.0, 102.0]))
        assert (added, updated) == (0, 0), "re-running the same data must change nothing"

        assert len(read_series(con, "TEST.CPI")) == 3


def test_revision_updates_in_place(db, spec):
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        upsert_observations(con, frame([100.0, 101.0, 102.0]))

        # An agency revises the middle observation.
        added, updated = upsert_observations(con, frame([100.0, 101.5, 102.0]))
        assert (added, updated) == (0, 1)

        stored = series_as_pandas(con, "TEST.CPI")
        assert stored.iloc[1] == 101.5
        assert len(stored) == 3, "a revision must not duplicate the row"


def test_new_observations_append(db, spec):
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        upsert_observations(con, frame([100.0, 101.0]))
        added, updated = upsert_observations(con, frame([100.0, 101.0, 102.0, 103.0]))
        assert (added, updated) == (2, 0)
        assert len(read_series(con, "TEST.CPI")) == 4


def test_nulls_are_dropped_not_stored(db, spec):
    """A gap and a zero are different things; missing values must not become rows."""
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        added, _ = upsert_observations(con, frame([100.0, None, 102.0]))
        assert added == 2
        assert not series_as_pandas(con, "TEST.CPI").isna().any()


def test_duplicate_dates_in_one_batch_keep_last(db, spec):
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        dupes = pd.DataFrame(
            {
                "series_id": "TEST.CPI",
                "obs_date": pd.to_datetime(["2024-01-01", "2024-01-01"]),
                "value": [100.0, 105.0],
            }
        )
        added, _ = upsert_observations(con, dupes)
        assert added == 1
        assert series_as_pandas(con, "TEST.CPI").iloc[0] == 105.0


def test_missing_columns_raise(db, spec):
    with connect(db_path=db) as con:
        with pytest.raises(ValueError, match="missing columns"):
            upsert_observations(con, pd.DataFrame({"series_id": ["X"], "value": [1.0]}))


def test_read_wide_pivots_by_series(db, spec):
    other = SeriesSpec(
        series_id="TEST.GDP",
        name="Test GDP",
        country="ID",
        category="growth",
        source="seed",
        source_id="test",
        frequency="M",
    )
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec, other])
        upsert_observations(con, frame([1.0, 2.0, 3.0]))
        upsert_observations(con, frame([10.0, 20.0, 30.0], series_id="TEST.GDP"))

        wide = read_wide(con, ["TEST.CPI", "TEST.GDP"])
        assert list(wide.columns) == ["TEST.CPI", "TEST.GDP"]
        assert wide.shape == (3, 2)
        assert wide["TEST.GDP"].iloc[-1] == 30.0


def test_date_filters(db, spec):
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        upsert_observations(con, frame([1.0, 2.0, 3.0, 4.0, 5.0]))
        subset = read_series(con, "TEST.CPI", start="2024-02-01", end="2024-04-01")
        assert len(subset) == 3


def test_freshness_flags_stale_series(db, spec):
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        upsert_observations(con, frame([1.0, 2.0], start="2020-01-01"))
        status = freshness(con)
        assert status.loc[status["series_id"] == "TEST.CPI", "status"].iloc[0] == "stale"


def test_coverage_reports_empty_series(db, spec):
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        report = coverage(con)
        assert report.loc[report["series_id"] == "TEST.CPI", "n_obs"].iloc[0] == 0


def test_delete_observations_clears_only_the_named_series(db, spec):
    """Repointing a series_id at a new source_id must not leave the old
    upstream's history underneath the new one's."""
    other = SeriesSpec(
        series_id="TEST.GDP",
        name="Test GDP",
        country="ID",
        category="growth",
        source="seed",
        source_id="test",
        frequency="M",
    )
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec, other])
        upsert_observations(con, frame([1.0, 2.0, 3.0]))
        upsert_observations(con, frame([10.0, 20.0], series_id="TEST.GDP"))

        dropped = delete_observations(con, ["TEST.CPI"])
        assert dropped == 3
        assert read_series(con, "TEST.CPI").empty
        assert len(read_series(con, "TEST.GDP")) == 2, "other series must be untouched"

        # The series_meta row survives, so the series can be refetched.
        assert not coverage(con).query("series_id == 'TEST.CPI'").empty


def test_delete_observations_of_nothing_is_safe(db, spec):
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        assert delete_observations(con, []) == 0


def test_read_series_of_unknown_id_is_empty(db, spec):
    with connect(db_path=db) as con:
        sync_series_meta(con, [spec])
        assert read_series(con, "NOPE").empty
        assert series_as_pandas(con, "NOPE").empty


def test_a_staleness_override_beats_the_frequency_default(db):
    """An event-dated series must not be judged by how recently it changed.

    ID.POLICY.RATE is frequency D so that two decisions in one month can coexist,
    but a policy rate that has held for weeks is Bank Indonesia holding, not a
    broken feed. Without the override the daily 5-day threshold paints a
    perfectly healthy series red.
    """
    import datetime

    event = SeriesSpec(
        series_id="TEST.POLICY",
        name="Test policy rate",
        country="ID",
        category="rates",
        source="manual",
        source_id="test",
        frequency="D",
        unit="Percent",
        staleness_days=400,
    )
    plain = SeriesSpec(
        series_id="TEST.DAILY",
        name="Test daily series",
        country="ID",
        category="markets",
        source="yahoo",
        source_id="test",
        frequency="D",
        unit="Index",
    )

    today = datetime.date(2026, 8, 19)
    sixty_days_back = pd.Timestamp(today) - pd.Timedelta(days=60)
    rows = pd.DataFrame(
        {
            "series_id": ["TEST.POLICY", "TEST.DAILY"],
            "obs_date": [sixty_days_back, sixty_days_back],
            "value": [5.75, 100.0],
        }
    )

    with connect(db_path=db) as con:
        sync_series_meta(con, [event, plain])
        upsert_observations(con, rows)
        status = freshness(con, today=today).set_index("series_id")["status"]

    assert status["TEST.POLICY"] == "fresh"   # 60 days is nothing for a policy rate
    assert status["TEST.DAILY"] == "stale"    # 60 days on a daily feed is a fault


def test_re_syncing_the_catalog_makes_an_activated_series_visible(db):
    """The mechanism behind the Reload data button.

    `latest_values` filters on `series_meta.active`, and that table is a mirror
    of the YAML rather than the YAML itself. Flipping `active: true` in the
    catalog therefore changes nothing a page can see until the mirror is
    rewritten — which is why reloading has to sync, not just drop caches.
    """
    def policy(active: bool) -> SeriesSpec:
        return SeriesSpec(
            series_id="TEST.POLICY",
            name="Test policy rate",
            country="ID",
            category="rates",
            source="manual",
            source_id="test",
            frequency="D",
            unit="Percent",
            active=active,
        )

    rows = pd.DataFrame(
        {
            "series_id": ["TEST.POLICY"],
            "obs_date": [pd.Timestamp("2026-06-18")],
            "value": [5.75],
        }
    )

    with connect(db_path=db) as con:
        sync_series_meta(con, [policy(active=False)])
        upsert_observations(con, rows)
        assert "TEST.POLICY" not in set(latest_values(con)["series_id"])

        # The YAML edit, mirrored — exactly what reload_data does.
        sync_series_meta(con, [policy(active=True)])
        visible = latest_values(con).set_index("series_id")

    assert "TEST.POLICY" in visible.index
    assert visible.loc["TEST.POLICY", "last_value"] == 5.75
