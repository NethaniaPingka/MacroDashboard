# MacroDashboard

Indonesian and global macroeconomic data in one store, viewed from two points of
view: **Indonesia** and **Global** (US and the euro area). Each is its own page,
with Inflation, Growth, Rates & Money and External & Markets as tabs inside it —
so flipping between the two reads as a comparison. Every tab opens with four KPI
cards, then the charts that explain them, then the detail underneath.

FRED and market data refresh from APIs on demand. Indonesian data arrives by a
mix of FRED mirrors, the BPS WebAPI where it works, committed seed files, and
manual upload for the rest — which is what "semi-automated" means here.

## Quick start

```bash
# from the project root
.venv\Scripts\activate                    # Windows
python scripts/refresh_all.py             # populate the store (needs API keys)
streamlit run app/Home.py                 # open the dashboard
```

`--source yahoo` needs no API key, so it is the quickest way to prove the
pipeline works before filling in secrets. The dashboard reads whatever the store
holds and hides series it has no data for, so a partial refresh still opens.

## Setup

The virtualenv already exists at `.venv` (Python 3.12.4, built from Anaconda).
To rebuild it from scratch:

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .
```

### API keys

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in:

| Key | Where | Cost |
|---|---|---|
| `FRED_API_KEY` | https://fredaccount.stlouisfed.org/apikeys | free, instant |
| `BPS_APP_KEY` | https://webapi.bps.go.id/developer | free, registration |

Environment variables of the same name take precedence, which is how the app
reads secrets once deployed. Market data needs no key at all.

## How it works

Every source normalises to the same three columns — `series_id`, `obs_date`,
`value`. That single agreement is what lets storage, transforms, and charts stay
ignorant of where a number came from, and it is why adding an indicator is a
YAML edit rather than a code change.

```
catalog/*.yaml  ──►  refresh.py  ──►  data/macro.duckdb  ──►  charts.py ──► app/
     what to pull      fetch+upsert       observations        cards+figures  pages
```

`charts.py` holds the whole visual vocabulary — KPI cards, figures, slicers — and
both context pages are drawn from it, so the shared layout is shared by
construction rather than by discipline.

### Adding an indicator

Add a block to the relevant file in `catalog/`:

```yaml
- series_id: US.HOUSING.STARTS      # canonical id — never changes, even if the source does
  name: US housing starts
  country: US
  category: growth                  # inflation | growth | rates | external | markets
  source: fred                      # fred | yahoo | seed | bps_api | manual
  source_id: HOUST                  # what the source calls it
  frequency: M                      # D | W | M | Q | A
  unit: Thousands of units, SAAR
  default_transform: yoy            # level | yoy | mom | qoq_saar | ann_3m
```

Then `python scripts/refresh_all.py --series US.HOUSING.STARTS`.

The `series_id` is deliberately independent of the source. When an Indonesian
series graduates from a seed CSV to the BPS API, only `source` changes and the
history underneath stays continuous.

### Commands

```bash
python scripts/refresh_all.py                       # everything
python scripts/refresh_all.py --source fred         # one source
python scripts/refresh_all.py --country ID          # one country
python scripts/refresh_all.py --dry-run             # fetch, report, write nothing
python scripts/refresh_all.py --include-seed        # also load data/seed/
python scripts/refresh_all.py --include-manual      # also load data/manual/
python scripts/refresh_all.py --reset --series X    # wipe first; use when source_id changed
python scripts/audit_catalog.py                     # validate ids against sources
python scripts/audit_catalog.py --fix-inactive      # deactivate ids that failed
python scripts/find_series.py "Thailand CPI" --freq M   # search FRED for new ids
python scripts/bps_explore.py subjects              # browse the BPS taxonomy
python scripts/bps_explore.py search inflasi --subject 3
python scripts/bps_explore.py profile 2263          # inspect one BPS variable
python -m pytest                                    # 153 tests
```

## Running it

From the project root, in a terminal:

```bash
.venv\Scripts\python.exe -m streamlit run app/Home.py
```

It opens http://localhost:8501 in your browser. Leave the terminal open — that
window *is* the server. `Ctrl+C` in it stops the app.

First time on a new machine, or after deleting `data/macro.duckdb`:

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt   # once
.venv\Scripts\python.exe scripts/refresh_all.py               # fills the store
.venv\Scripts\python.exe scripts/refresh_all.py --include-manual   # replays data/manual/
```

FRED and BPS keys live in `.streamlit/secrets.toml` (copy
`secrets.toml.example`). Without them the Yahoo market series still refresh.

## Data model

Three tables in `data/macro.duckdb`:

- **`series_meta`** — one row per catalog entry, mirrored from YAML on every run.
- **`observations`** — the fact table, keyed on `(series_id, obs_date)`. Upserts
  overwrite on conflict *only when the value actually differs*, so `ingested_at`
  is a genuine revision timestamp rather than a record of when we last looked.
- **`refresh_log`** — one row per series per run. Powers the freshness panel and
  the "what changed this week" feed, and makes a series that has quietly stopped
  updating visible instead of silent.

The database file is gitignored and disposable — it rebuilds from `catalog/`
plus `data/seed/` and `data/manual/`.

### Entering data by hand

Some numbers have no API. Bank Indonesia publishes the BI Rate but offers no
open endpoint, and FRED does not mirror it, so it can only arrive by being
typed. The **Data Manager** page (`app/pages/9_Data_Manager.py`) is that route,
and it works for any series in the catalog, not just that one.

Typed rows are saved to `data/manual/<series_id>.csv` *first*, then upserted
into DuckDB. That order is the point: the database is disposable, so a history
that existed only inside it would not survive the rebuild this README recommends
whenever something looks wrong. The CSVs are tracked by git and replayed by
every refresh, which is what makes a hand-maintained series behave like a
fetched one.

**Event series vs periodic series.** Most indicators arrive on a grid: one CPI
print per month, one GDP print per quarter. An administered rate does not — it
is dated to the decisions that move it, and a central bank can move twice in one
month. Give such a series `frequency: D` and its announcements keep their own
dates instead of colliding on the 1st. Two settings travel with that choice and
should not be separated from it:

- `staleness_days:` on the catalog entry, so a rate that has held for weeks is
  not judged by the 5-day daily freshness threshold and reported as broken.
- `step=` on `charts.line_figure`, so the line holds flat and jumps rather than
  sloping between decisions. A sloping line draws a gradual move that never
  happened.

Two things the page does before it writes anything:

- **Snaps dates to the start of their period.** Every fetcher here dates an
  observation to the start of what it covers, and the transforms match on
  calendar offsets from that date. A monthly value filed under the 15th would
  not error — it would silently fail to match the year-earlier lookup. The snap
  is shown in the preview, so the correction is visible before you commit.
- **Shows the diff.** New rows, revisions with the stored value beside the new
  one, and rows that change nothing. The commit button acts on that diff. Values
  far outside the series' own history, rates outside a plausible band, and
  future dates are flagged as questions — never as refusals. So is a percent
  value entered as a decimal fraction: 5.75% typed as 0.0575 is inside every
  plausible band and wrong by the factor that flips the sign of a real rate.

## Series status

Where each part of the catalog currently gets its data. Updated as automation
improves.

Audited against the live APIs on 2026-08-19. 53 series populated, ~144k
observations, everything current except one deliberate backfill series.

| Group | Source | Status |
|---|---|---|
| Markets (10) | yfinance | **Live** — all tickers current |
| US (15) | FRED | **Live** — all fresh, transforms cross-checked against FRED's own maths |
| Euro area — HICP, GDP, ECB rate, Bund 10y | FRED | **Live** |
| Indonesia — inflation YoY & MoM | BPS WebAPI | **Live**, through 2026-07 |
| Indonesia — GDP and its 8 expenditure components | BPS WebAPI | **Live**, through 2026 Q2 |
| US — GDP growth contributions (6) | FRED (BEA) | **Live**, through 2026 Q2 |
| Indonesia — exports, imports, trade balance | BPS WebAPI | **Live**, through 2026-06 |
| Indonesia — reserves, interbank rate | FRED (IMF) | **Live**, through 2026-06 |
| Indonesia — CPI index | FRED | **Backfill only**, 1990–2025-04, abandoned upstream |
| Indonesia — BI Rate | Data Manager | **Live**, entered by hand, 2025-01 to 2026-08 |
| Indonesia — interbank call money rate | FRED (IMF) | **Live** — market cross-check only, not the policy rate |
| Asia ex-Indonesia | — | Out of scope for v1, see `catalog/global.yaml` |

### Two things that will bite you if you forget them

**An id that resolves is not an id that still publishes.** FRED keeps abandoned
OECD/IMF mirrors online indefinitely — they answer the API happily while their
last observation recedes into history. Japan's CPI mirror last moved in June
2021; Indonesia's stops at April 2025. `audit_catalog.py` judges the age of the
last observation, not just whether the id exists, and flags anything past twice
its staleness threshold as `ABANDONED`. Run it periodically, not just once.

**BPS rebases and renumbers.** The headline CPI has been 2012=100 (var 2, ends
2019), then 2018=100, now 2022=100 (var 2262/2263, starts 2024) — each rebasing
gets a *new var_id* and the old one is frozen. No single variable spans the
history, which is why Indonesian inflation is stored as published *rates* rather
than an index: rates chain across base changes, index levels do not. Use
`scripts/bps_explore.py` to find the current ids after a rebasing.

### How the Indonesian data was validated

Not "it loaded", but "it is right":

- BPS's published YoY inflation against YoY computed from FRED's independent CPI
  index, over their 16-month overlap: mean absolute difference **0.091pp**,
  agreeing to 0.005pp in recent months.
- Exports − imports − trade balance across every month from 2021: residual
  **0.0** to reporting precision.
- That same identity is what exposed a silent unit change — BPS switched the
  trade balance from billions to millions of USD around 2021 under the same
  var_id with no metadata flag. Hence `start_date` on that catalog entry.

## Conventions worth knowing

- **Calendar-based transforms, not row-based.** YoY means "the observation dated
  twelve months earlier", never "twelve rows back". This is not pedantry: US CPI
  has no October 2025 observation, and row-counting compared July 2026 against
  June 2025, overstating inflation by 0.24pp with nothing visibly wrong. Where
  the base period has no observation, the result is NaN — the honest answer.
  Daily and weekly series match to the nearest date within a few days, since
  markets do not trade on every anniversary.
- **Transforms are verified against FRED's own calculations.** `yoy`, `qoq_saar`,
  and `mom` agree with FRED's `pc1`/`pca`/`pch` units to 5 decimal places across
  US, euro-area, and Indonesian series. Re-run that check after touching
  `transforms.py`.
- **Indonesian GDP is not seasonally adjusted**, so its default transform is YoY,
  not QoQ — the Ramadan and harvest seasonality would swamp a quarterly rate.
- **Nulls are dropped, never stored.** A gap and a zero are different things.
- **One failure never ends a run.** A retired FRED id or a yfinance outage marks
  that one series as errored in `refresh_log`; everything else still refreshes.
