# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The project is installed editable into `.venv` (Python 3.12). On Windows use
`.venv\Scripts\python.exe`; from the Bash tool use `.venv/Scripts/python.exe`.

```bash
.venv/Scripts/python.exe -m streamlit run app/Home.py         # the dashboard
.venv/Scripts/python.exe -m pytest                       # 153 tests, ~16s, no network
.venv/Scripts/python.exe -m pytest tests/test_transforms.py::test_yoy_is_calendar_based_not_row_based
.venv/Scripts/python.exe -m pytest tests/test_app.py     # renders the real pages headlessly
.venv/Scripts/python.exe scripts/refresh_all.py --dry-run     # fetch, report, write nothing
.venv/Scripts/python.exe scripts/refresh_all.py --source yahoo   # no API key needed
.venv/Scripts/python.exe scripts/refresh_all.py --series US.CPI.HEADLINE.IDX
.venv/Scripts/python.exe scripts/refresh_all.py --reset --series X  # wipe first; see below
.venv/Scripts/python.exe scripts/refresh_all.py --include-manual  # replay data/manual/, inactive too
.venv/Scripts/python.exe scripts/audit_catalog.py             # validate ids against live sources
.venv/Scripts/python.exe scripts/find_series.py "Thailand CPI" --freq M   # search FRED for ids
.venv/Scripts/python.exe scripts/bps_explore.py profile 2263  # inspect a BPS variable
```

**`--reset` is mandatory whenever a `source_id` changes.** `series_id` is
source-independent by design, so repointing one at a different upstream leaves
the old vendor's rows underneath the new one's — two different series averaged
into a single chart. This actually happened when `ID.GDP.REAL` moved from FRED
to BPS: the first refresh reported "1 new, 65 revised" and silently overwrote
values in different units.

`pyproject.toml` sets `pythonpath = ["src"]` for pytest, so tests import
`macrodash` without an install. Scripts do not — they rely on the editable
install. There is no linter or formatter configured.

FRED and BPS need keys in `.streamlit/secrets.toml` (env vars of the same name
win). Yahoo and seed need none, so `--source yahoo` is the offline-friendly
smoke test.

## Architecture

```
catalog/*.yaml  ──►  refresh.py  ──►  data/macro.duckdb  ──►  charts.py ──► app/
   what to pull      fetch+upsert       observations        cards+figures   pages
```

**The catalog is the control surface.** Adding an indicator is a YAML block in
`catalog/{global,indonesia,markets}.yaml`, never a code change. `catalog.py`
loads all files, rejects duplicate ids across files, and validates category /
frequency / source / transform against the tuples in `config.py` and
`catalog.py` — a typo fails loudly at load rather than surfacing later as a
wrong chart.

**`series_id` is deliberately independent of the source.** When an Indonesian
series graduates from a seed CSV to the BPS API, only `source`/`source_id`
change; the stored history stays continuous. Ids are upper case and dotted
(`ID.CPI.HEADLINE.IDX`) — `tests/test_catalog.py` enforces that against the real
catalog.

**Every fetcher returns the same three columns** (`series_id`, `obs_date`,
`value`) via `sources/base.py::tidy`, which also applies the catalog's `scale`.
That single agreement is why store, transforms, and charts never learn where a
number came from. `sources/__init__.py::get_fetcher` constructs fetchers lazily
so a missing FRED key does not block a markets-only run. Every source is now
implemented; `bps_manual.py` is a thin alias kept so `source: bps_manual` in a
catalog file still resolves after the Phase 4 rename to `manual`.

**A failure is always scoped to one series.** `refresh._refresh_one` catches
everything, records `status: error` in `refresh_log`, and the run continues.
`refresh_all.py` exits non-zero only if *every* series failed — a scheduled
refresh should not page anyone because one ticker was down.

**Three tables in `data/macro.duckdb`** (gitignored, rebuildable from
`catalog/` + `data/seed/` + `data/manual/`): `series_meta` mirrored from YAML on every run,
`observations` keyed on `(series_id, obs_date)`, and `refresh_log` one row per
series per run.

## Invariants worth preserving

- **Transforms are calendar-based, not row-based.** YoY means "the observation
  dated twelve months earlier", never "twelve rows back". This guards a real
  bug: a missing US CPI month made a row-counting YoY compare July 2026 against
  June 2025, overstating inflation by 0.24pp with nothing visibly wrong. Missing
  base period → NaN. Only `D`/`W` match to nearest-within-tolerance
  (`MATCH_TOLERANCE`), since markets do not trade on every anniversary. Regression
  tests: `test_yoy_is_calendar_based_not_row_based`,
  `test_yoy_is_nan_when_the_base_month_is_missing`.
- **The upsert's `WHERE observations.value IS DISTINCT FROM excluded.value`**
  (`store.py`) is load-bearing. Without it every refresh rewrites every row and
  `ingested_at` degrades from "when this value last changed" to "when we last
  looked", which is what the revision feed reads.
- **Nulls are dropped, never stored.** A gap and a zero are different things.
  The Data Manager enforces the same rule earlier: a row with a date and no
  value is a *blocked* entry, not a zero.
- **For a manual series the store is a projection of the CSV, not a running
  total.** `manual.save` writes the whole merged file and calls
  `store.prune_observations` to drop stored rows the file no longer contains, so
  committing leaves the store in the state a `--reset` rebuild would produce.
  An upsert alone adds and revises but never removes: re-dating the BI Rate from
  month-starts to real announcement dates left twenty orphaned rows underneath
  the new ones and drew a policy move that never happened — 5.25, up to 5.75 on
  1 June (stale), then "down" to 5.50 on the 9th. **The prune is scoped to
  `manual.MANUAL_SOURCES`**; on an API-backed series the vendor sends a window
  rather than a full history, and pruning to it would delete everything outside
  that window. Regression tests:
  `test_redating_a_series_does_not_leave_orphans_behind`,
  `test_saving_leaves_the_store_matching_a_rebuild`,
  `test_an_api_backed_series_is_never_pruned_to_the_manual_file`.
  Editing a CSV by hand bypasses this — that path still needs
  `refresh_all.py --reset --series X`.
- **Hand-entered data is written to `data/manual/<series_id>.csv` before it is
  written to DuckDB**, and the CSV is tracked by git. The database is gitignored
  and the README tells you to delete it when something looks wrong; a typed
  history has no upstream to re-fetch, so a copy that lived only in the store
  would not survive its own recovery procedure. `sources/manual.py` replays the
  CSVs on every refresh, which is what makes a manual series behave like any
  other. Regression test: `test_a_manual_series_rebuilds_from_its_csv_alone`.
- **An administered rate is an EVENT series, not a periodic one.**
  `ID.POLICY.RATE` is `frequency: D` for exactly one reason: Bank Indonesia can
  move twice inside one calendar month, and on a monthly grid both announcements
  snap to the 1st, collide, and are refused as ambiguous. D means each decision
  keeps its own date. The consequences travel together and must not be split up:
  the series carries `staleness_days: 400` (the 5-day daily default would paint
  a holding central bank red), and it is drawn with `step=` in `line_figure`
  (joining decisions with a sloping line draws a gradual move that never
  happened). Regression tests: `test_two_decisions_in_one_month_both_survive`,
  `test_a_staleness_override_beats_the_frequency_default`,
  `test_the_bi_rate_is_drawn_as_a_step_and_the_proxy_is_not_drawn_at_all`.
- **`staleness_days` on a catalog entry overrides the frequency default.** 0
  keeps the default. It exists for series whose gaps mean "nothing happened"
  rather than "nothing arrived".
- **Typed dates are snapped to the start of their period** (`manual.normalise_date`).
  A monthly value filed under the 15th does not error — it silently fails to
  match the calendar-based YoY lookup a year later and produces NaN. The Data
  Manager shows the snap in the preview so the correction is never invisible.
- **A percent-unit value entered as a decimal fraction is the trap that has
  actually bitten this project.** The BI Rate was first entered as 0.0575 for
  5.75%. It sits inside the plausible percent band, so the basis-points check
  could not see it, and it is wrong by exactly the factor that flips the real
  policy rate from +2.87% to −2.82% against 2.88% inflation — a sign error
  wearing a plausible number. `manual._warning` now flags a percent value in
  (0, 1) unless the series' own history already lives there. Regression tests:
  `test_a_rate_typed_as_a_decimal_fraction_is_queried`,
  `test_a_genuinely_near_zero_rate_is_not_nagged_about`.
- **The manual CSV stores unscaled values**, exactly as the publication prints
  them; `scale` is applied on the way into the store by `tidy`, as for every
  other source. This is what keeps a CSV row checkable against the `source_ref`
  beside it. Regression test:
  `test_the_csv_keeps_unscaled_values_so_it_still_matches_the_publication`.
- **`SeriesNotFound` vs `FetchError`.** Only the former justifies deactivating a
  catalog entry. A timeout or 502 means "we do not know", and `audit_catalog.py`
  marks those `UNKNOWN` so `--fix-inactive` can never empty the catalog after one
  bad afternoon.
- **An id that resolves is not an id that still publishes.** FRED keeps
  abandoned OECD/IMF mirrors online indefinitely (Japan's CPI mirror last moved
  in June 2021), so the audit judges the age of the last observation against
  `STALENESS_DAYS` and flags anything past twice the threshold as `ABANDONED`.
  Practical consequence: FRED cannot be Indonesia's live CPI source — it is
  backfill only, and BPS has to supply anything current.
- **Indonesian GDP is not seasonally adjusted**, so its default transform is YoY,
  not QoQ.
- **Growth contributions divide by the *total's* base, not the component's.**
  `(component_t − component_{t−1yr}) / total_{t−1yr}`. That is what makes them
  additive, and additivity is what lets a stacked chart reconcile to the headline
  drawn over it. Dividing by the component's own base gives its growth rate — a
  real number, but one that cannot be stacked, and one that is meaningless for
  inventories, which cross zero. Regression tests:
  `test_growth_contributions_sum_to_the_headline_growth_rate`,
  `test_growth_contribution_handles_a_component_that_crosses_zero`.
- `config.py` imports no Streamlit — the fetch/store layer must run from Task
  Scheduler and CI. Secrets go through `config.get_secret`, env var first.
  `charts.py` and the pages are the only modules that may import it.
- Transform outputs were verified to 5dp against FRED's own `pc1`/`pca`/`pch`
  units. Re-check after touching `transforms.py`.

## Where the work is up to

**Phases 1-4 are complete. Phase 5 (Task Scheduler refresh, Parquet export,
optional GitHub + Streamlit Cloud) is next, and nothing in it has been begun.**

Phase 4 outcome, 2026-08-19: hand entry exists end to end — `manual.py` (pure),
`sources/manual.py` (the fetcher), and `app/pages/9_Data_Manager.py` (a typing
grid with a diff preview). Scope was set by the user: **a grid only.** File
upload, creating catalog entries from the UI, and deleting stored observations
were all offered and all declined — do not add them unprompted. Editing an
existing value is reachable, because typing a date that already exists produces
a revision, and the preview labels it as one.

`ID.POLICY.RATE` now has a working route but **still has no data** — the BI Rate
has to be typed from Bank Indonesia's publication by someone who can read it,
and nothing in this repo may invent it. It stays `active: false` until entered;
flip that flag once it holds history.

Phase 3 outcome: `src/macrodash/charts.py` plus three pages. Both context pages
are drawn entirely from `charts.py`, which is the point — the shared tab layout
is shared by construction rather than by discipline. Building it required
extending the catalog: the growth tabs needed a GDP breakdown that Phase 2 had
not fetched, so eight BPS expenditure components and six BEA contribution series
were added (see "The GDP breakdown" below).

Phase 2 outcome: the BPS WebAPI works and is the live Indonesian source
(`sources/bps.py`, called directly — `stadata` was rejected because it caps
pandas<3 and declares pytest as a runtime dependency). Indonesian inflation,
GDP, exports, imports, and trade balance all come from BPS and are current;
reserves and the interbank rate come from FRED's IMF-sourced series. The
originally-planned hand-built seed CSVs proved unnecessary — `data/seed/` is
still empty and only `ID.POLICY.RATE` (the actual BI Rate) awaits manual entry.

Scope decision taken 2026-08-18: **Asia ex-Indonesia is out of v1.** The global
view is US + euro area. See the header comment in `catalog/global.yaml` for the
sourcing dead-ends already explored, so they are not rediscovered.

### BPS specifics worth knowing before touching `sources/bps.py`

- Its `datacontent` keys are five ids concatenated as strings. They are
  **constructed, never parsed** — parts are variable width (month 7 is one
  character, month 12 is two), so reading from the right is ambiguous.
- `vervar` means REGION for most variables (9999 = INDONESIA) but EXPENDITURE
  COMPONENT for the national accounts (800 = PRODUK DOMESTIK BRUTO).
- The `th` range is capped at **3 years per request**; long histories are
  chunked by `_year_chunks`.
- The annual aggregate is turtahun 13 beside months and 35 beside quarters, and
  must never enter a periodic series.
- BPS returns `status: Error` with HTTP 200 for everything. A *parameter* error
  must raise `FetchError`, not `SeriesNotFound`, or `audit --fix-inactive` would
  deactivate a healthy variable.
- `SeriesSpec.start_date` exists for silent structural breaks: BPS switched the
  trade balance from billions to millions of USD around 2021 under the same
  var_id with no metadata flag.

### The GDP breakdown, added 2026-08-19

The Growth tabs needed a component breakdown, which the catalog did not have.
Both sides are now covered, by deliberately different routes:

- **Indonesia** — eight expenditure components off the same BPS variable as
  `ID.GDP.REAL` (1956), read at a different `vervar`. For the national accounts
  `vervar` is the EXPENDITURE COMPONENT, not the region, so these are siblings
  of the total rather than a regional split of it. Contributions are computed
  here, by `transforms.growth_contribution`.
- **United States** — six BEA contribution series straight off FRED, already in
  percentage points. **Not** derived from real component levels (`PCEC96`,
  `GPDIC1`, ...): those are chain-weighted, so their changes do not add up to
  the headline. BEA's own decomposition does.

Two rules that the charts depend on and that are easy to break:

- **Imports are stored positive**, as both agencies publish them. The minus sign
  belongs to the contribution calculation and is applied once, at the point of
  use, in the page. Negating them at ingest would make the level charts wrong
  instead.
- **The statistical discrepancy is charted, not dropped.** Without it the
  Indonesian components do not sum to the total, and a stack that quietly omits
  it misattributes the gap to whatever is drawn last.

`US.GDP.CONTRIB.EXPORTS` and `.IMPORTS` are the *split* of `.NETEXPORTS`.
Charting all six double-counts trade; the Global page lists them in the detail
table only.

### Verified state as of 2026-08-19

A clean rebuild (delete `data/macro.duckdb`, then `refresh_all.py`) gives
**53/53 series ok, 0 errors, 144,480 observations, 1927-12-30 .. 2026-08-17**,
50 fresh, 1 deliberately stale, and 2 daily Treasury series reading "ageing" over
a weekend. 107 tests pass. If a future run does not reproduce that, something
regressed or a source changed — check `audit_catalog.py` first.

| Phase | Scope | State |
|---|---|---|
| 1 | Catalog, store, sources, transforms, refresh, audit scripts | **Done** |
| 2 | Indonesian live data via BPS WebAPI | **Done** |
| 3 | `charts.py`, `app/Home.py`, `app/pages/1_Indonesia.py`, `2_Global.py` | **Done** |
| 4 | `manual.py`, `sources/manual.py`, `app/pages/9_Data_Manager.py` | **Done** |
| 5 | Task Scheduler refresh, Parquet export; optionally GitHub + Streamlit Cloud | **Next, not started** |

## The UI — read before touching `charts.py` or the pages

The user's navigation requirements, confirmed three times and not to be
reinterpreted:

- **Context-first navigation.** Indonesia and Global are *separate pages*. The
  four indicator groups are `st.tabs` **inside** each page, in the same order on
  both, so flipping between them reads as a comparison. The order lives in
  `charts.TABS` so the two pages cannot drift apart, and
  `test_both_context_pages_use_the_same_four_tabs` checks what actually
  rendered. The two contexts share a screen only on Home.
- **Three bands per tab, always in this order:** four KPI cards, then the charts
  that explain those four numbers, then the detail — breakdowns and tables,
  mostly folded into an expander. `test_every_tab_opens_with_four_cards_and_then_charts`
  enforces the count and the order.
- **Never open empty.** Data lands before UI, which is why Phase 2 came first.
- **Asia ex-Indonesia is out of v1** (decided 2026-08-18). Global means US +
  euro area. Do not re-add Singapore/Malaysia/Thailand/Philippines or the
  China/Japan/India CPI mirrors without asking — the header comment in
  `catalog/global.yaml` records the sourcing dead-ends.

Conventions inside `charts.py` worth keeping:

- **It splits into a pure half and a Streamlit half.** Formatting and figure
  construction take plain pandas and return plain figures, so `tests/test_charts.py`
  reaches them without a browser. Only `render_*` and `*_row` touch `st`.
  `charts.py` is the *only* module besides the pages allowed to import Streamlit.
- **A change on a rate is percentage points, never percent.** Inflation going
  3.34 → 2.88 fell 0.46pp; "-13.8%" is arithmetic applied to a number that is
  already a rate. `format_change` decides from the unit.
- **A stacked contribution chart carries the headline line it reconciles to.**
  That overlay is the chart's own proof the bars add up. When a slicer leaves the
  selection partial the page *removes* the line rather than drawing a claim it
  can no longer support — see `test_dropping_a_gdp_component_hides_the_headline`.
- **Colour follows country when countries differ, the categorical palette when
  they do not** (`series_colors`), so four US series are not four identical reds.
- **Card colour follows direction only** — up green, down red. It does not try to
  encode whether a move is good; rising unemployment is not a green number, but
  nor is `charts.py` the place to decide that for thirty indicators.
- **`st.plotly_chart` is left unkeyed.** A key is only needed to carry selection
  state, and a keyed chart becomes a widget whose figure AppTest cannot read
  back.
- Streamlit 1.61 has deprecated `use_container_width`; use `width="stretch"`.

**The Data Manager is not a context page** and is deliberately exempt from the
three-band rule above — it has no KPI cards, because it is a form, not a view.
The rules it does follow: nothing is written until the diff has been shown, and
the commit button acts on that diff rather than on the raw grid. It is the only
page that opens a *write* connection to DuckDB; if a `refresh_all.py` run holds
the file, the save fails loudly and the typing is already safe in `data/manual/`.

**`charts.reload_data()` is what the "Reload data" buttons call, not
`clear_caches()`.** The difference is the catalog: `catalog()` is
`@st.cache_resource`, which `st.cache_data.clear()` deliberately does not touch,
and the KPI cards read `series_meta` in DuckDB rather than the YAML directly —
`store.latest_values` filters on `series_meta.active`. So a YAML edit used to
need a `refresh_all.py` run *and* an app restart before anything on screen
changed. `reload_data()` drops the catalog cache, drops the data caches, then
re-mirrors the catalog into `series_meta`, in that order — reversing the first
two would faithfully re-sync the stale copy. It is also the app's only write
outside a Data Manager commit, so it returns an error string instead of raising:
a concurrent refresh holds DuckDB's write lock, and a reader pressing Reload
must not get a traceback. The Data Manager commit calls it too, so a series
activated in YAML minutes earlier appears as soon as its first values land.

`store.py` supplies everything the pages read: `read_wide`, `series_as_pandas`,
`latest_values`, `freshness`, `recent_changes`, `coverage`. All of it is wrapped
in `charts.py` behind a five-minute `st.cache_data`, with a "Reload data" button
that clears it.

## Flagged for the user — unresolved

1. **`ID.POLICY.RATE` now carries the rates displays; `ID.RATES.INTERBANK` no
   longer stands in for policy.** The BI Rate was entered by hand on 2026-08-19
   (20 monthly rows, 2025-01 to 2026-08, 4.75-5.75%) and is `active: true`. The
   interbank call money rate (FRED `IRSTCI01IDM156N`) is still fetched and still
   shown, but only in the Rates detail table as a market cross-check — the gap
   between it and the policy rate is a liquidity signal. It must never be
   labelled "policy rate" or "BI Rate"; `test_the_interbank_rate_is_never_labelled_a_policy_rate`
   still guards that, and `test_the_rates_tab_leads_with_the_bi_rate` guards the
   swap itself.
   The Rates tab carries this caveat on screen and
   `test_the_interbank_rate_is_never_labelled_a_policy_rate` guards the wording.
2. **`ID.FX.RESERVES` excludes gold**, so it prints slightly below the
   *cadangan devisa* figure BI announces. Label it "reserves excluding gold".
3. **`MKT.NIKKEI` is still active** — a live Japanese equity index, kept when
   Asia ex-Indonesia was cut because the cut targeted stale CPI mirrors, not
   markets. The user was asked and has not said either way; leave it unless
   asked.
4. **No commits yet.** `master` is still at zero commits, now with Phase 4
   (`src/macrodash/manual.py`, `src/macrodash/sources/manual.py`,
   `app/pages/9_Data_Manager.py`, `tests/test_manual.py`) on top of untracked
   Phases 2 and 3. This matters more than it did: `.gitignore` was changed so
   `data/manual/*.csv` is tracked, and that only protects anything once there
   is a repository to track it in. The user has been offered a first commit
   three times and not taken it up — ask, do not assume.
5. **`data/seed/` is empty and unused.** The hand-built seed CSVs the plan
   called for proved unnecessary; FRED's IMF series covered reserves and rates.
   The `seed` fetcher and `seed_coverage()` still work, but the BI Rate route is
   now `data/manual/` and the Data Manager, not seed — the two directories mean
   different things (`seed` bootstraps a clone, `manual` is the ongoing typing
   path with provenance and revisions). **No macro data has ever been
   transcribed into this repo by an agent, and none should be.** The Data
   Manager exists so a person who can read the publication types it; a
   plausible-looking rate invented from memory is exactly the failure every
   verification habit here is built to catch.
6. **The Bank Indonesia target band is hard-coded** in `1_Indonesia.py` as
   `BI_TARGET = (1.5, 3.5)`, i.e. 2.5% ± 1%, in force for 2024-2026. It is a
   policy setting rather than data, so nothing upstream will correct it when BI
   restates the target. Check it when the target period rolls over.

## How this project verifies data

The habit that has caught every real bug here: check numbers are *right*, not
merely present. Two checks worth re-running after touching fetchers or
transforms, both of which caught plausible-looking errors:

- **Against the source's own maths.** Request FRED with `units=pc1`/`pca`/`pch`
  and compare to our `yoy`/`qoq_saar`/`mom`. They agree to 5dp across US, euro
  area, and Indonesia. This is what exposed the row-vs-calendar YoY bug — a
  0.24pp inflation overstatement caused by a missing US CPI month.
- **Against accounting identities.** `ID.EXPORTS - ID.IMPORTS - ID.TRADE.BALANCE`
  is 0.0 from 2021. This is what exposed BPS silently switching that variable
  from billions to millions of USD under the same var_id. This one now runs in
  the app itself, on the Indonesia External tab, so a future unit change surfaces
  as a red box rather than as a plausible-looking chart.
- **Against the agency's own decomposition.** The eight BPS GDP components sum to
  `ID.GDP.REAL` to within 0.4 Bn IDR on a 3.6m base across all 66 quarters, and
  their computed contributions sum to its YoY to 1.2e-5pp. On the US side BEA's
  four published contributions sum to our own `qoq_saar` of `US.GDP.REAL` to
  within 0.019pp over 145 quarters — the residual is BEA's own 2dp rounding.
  Both checks were run when the series were added; re-run them after touching
  `transforms.growth_contribution` or the BPS fetcher.

Two independent sources agreeing is stronger evidence than either alone: BPS's
published Indonesian YoY and YoY computed from FRED's separate CPI index differ
by a mean of 0.091pp over their 16-month overlap.

Requirements are pinned hard: pandas 3.x and yfinance 1.x both broke APIs from
their previous major lines, and `requirements.txt` records which packages were
deliberately *not* used (`fredapi`, `stadata`) and why.
