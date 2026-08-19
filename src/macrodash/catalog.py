"""The series catalog: YAML in, validated SeriesSpec objects out.

The catalog is the app's control surface. Adding an indicator means adding a
YAML block in catalog/*.yaml — no code change anywhere else. Validation is
strict and fails loudly, because a typo in a frequency code would otherwise
surface much later as a silently wrong transform.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

from .config import CATALOG_DIR, CATEGORIES, FREQUENCIES

#: `manual` is hand entry through the Data Manager page, backed by
#: data/manual/*.csv. `bps_manual` is its former name, kept resolving.
VALID_SOURCES = ("fred", "yahoo", "manual", "bps_manual", "bps_api", "seed", "worldbank")
VALID_TRANSFORMS = ("level", "yoy", "mom", "qoq_saar", "ann_3m")


@dataclass(frozen=True)
class SeriesSpec:
    series_id: str
    name: str
    country: str
    category: str
    source: str
    source_id: str
    frequency: str
    subcategory: str = ""
    unit: str = ""
    seasonal_adj: bool = False
    default_transform: str = "level"
    scale: float = 1.0
    notes: str = ""
    active: bool = True
    #: Optional free-text pointer to the publication a seeded value came from.
    source_ref: str = ""
    #: Discard observations before this date (YYYY-MM-DD). For structural
    #: breaks a source does not signal — BPS silently switched the trade
    #: balance from billions to millions of USD around 2021, so the earlier
    #: values are off by a factor of 1000 under the same variable id.
    start_date: str = ""
    #: Override `config.STALENESS_DAYS[frequency]` for this series. 0 keeps the
    #: frequency default. Needed by event-dated series, where the gap between
    #: observations carries meaning rather than indicating a problem: a policy
    #: rate is dated to the decisions that moved it, so months of silence is
    #: Bank Indonesia holding, not a broken feed. Judging it by the daily
    #: threshold would paint a working series permanently red.
    staleness_days: int = 0

    def __post_init__(self) -> None:
        problems = []
        if self.category not in CATEGORIES:
            problems.append(f"category {self.category!r} not in {CATEGORIES}")
        if self.frequency not in FREQUENCIES:
            problems.append(f"frequency {self.frequency!r} not in {FREQUENCIES}")
        if self.source not in VALID_SOURCES:
            problems.append(f"source {self.source!r} not in {VALID_SOURCES}")
        if self.default_transform not in VALID_TRANSFORMS:
            problems.append(
                f"default_transform {self.default_transform!r} not in {VALID_TRANSFORMS}"
            )
        if problems:
            raise ValueError(f"{self.series_id}: " + "; ".join(problems))

    def as_row(self) -> dict:
        return asdict(self)


@dataclass
class Catalog:
    specs: dict[str, SeriesSpec] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.specs)

    def __iter__(self):
        return iter(self.specs.values())

    def __getitem__(self, series_id: str) -> SeriesSpec:
        return self.specs[series_id]

    def get(self, series_id: str) -> SeriesSpec | None:
        return self.specs.get(series_id)

    def ids(self) -> list[str]:
        return list(self.specs)

    def select(
        self,
        *,
        source: str | None = None,
        country: str | None = None,
        category: str | None = None,
        active_only: bool = True,
    ) -> list[SeriesSpec]:
        """Filter the catalog. This is how pages and the refresher pick series."""
        out = []
        for spec in self.specs.values():
            if active_only and not spec.active:
                continue
            if source and spec.source != source:
                continue
            if country and spec.country != country:
                continue
            if category and spec.category != category:
                continue
            out.append(spec)
        return out

    def countries(self) -> list[str]:
        return sorted({s.country for s in self.specs.values()})

    def sources(self) -> list[str]:
        return sorted({s.source for s in self.specs.values()})


def load_catalog(catalog_dir: Path | None = None) -> Catalog:
    """Load and merge every YAML file in the catalog directory."""
    catalog_dir = catalog_dir or CATALOG_DIR
    specs: dict[str, SeriesSpec] = {}
    seen_in: dict[str, str] = {}

    for path in sorted(catalog_dir.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or []
        if not isinstance(raw, list):
            raise ValueError(f"{path.name}: expected a list of series, got {type(raw).__name__}")

        for entry in raw:
            spec = SeriesSpec(**entry)
            if spec.series_id in specs:
                raise ValueError(
                    f"duplicate series_id {spec.series_id!r} "
                    f"in {path.name} and {seen_in[spec.series_id]}"
                )
            specs[spec.series_id] = spec
            seen_in[spec.series_id] = path.name

    return Catalog(specs)
