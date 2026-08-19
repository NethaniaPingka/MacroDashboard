"""Catalog loading and validation, including the real catalog/ files."""

from __future__ import annotations

import pytest
import yaml

from macrodash.catalog import SeriesSpec, load_catalog

BASE = {
    "series_id": "X.Y",
    "name": "X",
    "country": "ID",
    "category": "inflation",
    "source": "fred",
    "source_id": "XY",
    "frequency": "M",
}


def test_valid_spec_constructs():
    spec = SeriesSpec(**BASE)
    assert spec.active is True
    assert spec.default_transform == "level"
    assert spec.scale == 1.0


@pytest.mark.parametrize(
    "field,bad_value,message",
    [
        ("category", "vibes", "category"),
        ("frequency", "H", "frequency"),
        ("source", "bloomberg", "source"),
        ("default_transform", "log", "default_transform"),
    ],
)
def test_invalid_field_raises_naming_the_series(field, bad_value, message):
    with pytest.raises(ValueError, match=message) as exc:
        SeriesSpec(**{**BASE, field: bad_value})
    assert "X.Y" in str(exc.value), "the error must say which series is wrong"


def test_duplicate_series_id_across_files_raises(tmp_path):
    entry = [dict(BASE)]
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(entry), encoding="utf-8")
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(entry), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate series_id"):
        load_catalog(tmp_path)


def test_non_list_yaml_raises(tmp_path):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump({"series_id": "X.Y"}), encoding="utf-8")
    with pytest.raises(ValueError, match="expected a list"):
        load_catalog(tmp_path)


def test_empty_catalog_dir_loads_empty(tmp_path):
    assert len(load_catalog(tmp_path)) == 0


def test_select_filters_and_respects_active(tmp_path):
    entries = [
        dict(BASE, series_id="A", country="ID", source="fred"),
        dict(BASE, series_id="B", country="US", source="fred"),
        dict(BASE, series_id="C", country="ID", source="yahoo", category="markets"),
        dict(BASE, series_id="D", country="ID", source="fred", active=False),
    ]
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
    catalog = load_catalog(tmp_path)

    assert {s.series_id for s in catalog.select(country="ID")} == {"A", "C"}
    assert {s.series_id for s in catalog.select(source="fred")} == {"A", "B"}
    assert {s.series_id for s in catalog.select(country="ID", active_only=False)} == {"A", "C", "D"}
    assert {s.series_id for s in catalog.select(category="markets")} == {"C"}


# --------------------------------------------------------------------------
# the real catalog
# --------------------------------------------------------------------------

def test_real_catalog_loads():
    catalog = load_catalog()
    assert len(catalog) > 40


def test_real_catalog_ids_follow_the_naming_convention():
    for spec in load_catalog().specs.values():
        assert spec.series_id.isupper(), f"{spec.series_id} should be upper case"
        assert "." in spec.series_id, f"{spec.series_id} should be dotted, e.g. ID.CPI.HEADLINE"


def test_every_real_series_has_a_name_and_unit_or_note():
    for spec in load_catalog().specs.values():
        assert spec.name, f"{spec.series_id} has no name"


def test_indonesia_and_us_both_have_inflation_coverage():
    catalog = load_catalog()
    for country in ("ID", "US"):
        matches = catalog.select(country=country, category="inflation", active_only=False)
        assert matches, f"{country} has no inflation series"
