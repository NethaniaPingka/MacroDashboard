"""Paths, secrets, and display constants.

Deliberately free of any Streamlit import: the fetch/store layer has to run
from the command line (Task Scheduler, CI) where Streamlit is not running.
Secrets are read from environment variables first, then .streamlit/secrets.toml,
so the same code works locally and deployed.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CATALOG_DIR = PROJECT_ROOT / "catalog"
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "macro.duckdb"
SEED_DIR = DATA_DIR / "seed"
MANUAL_DIR = DATA_DIR / "manual"
EXPORT_DIR = DATA_DIR / "exports"
SECRETS_PATH = PROJECT_ROOT / ".streamlit" / "secrets.toml"

FRED_BASE_URL = "https://api.stlouisfed.org/fred"

#: How far back to pull when a series has never been fetched.
DEFAULT_HISTORY_START = "1990-01-01"

#: Observations per year, by catalog frequency code. Used by the periodic
#: transforms (yoy, ann_3m). Daily uses trading days, not calendar days.
PERIODS_PER_YEAR = {"D": 252, "W": 52, "M": 12, "Q": 4, "A": 1}

#: Days after which a series of each frequency is considered stale, measured
#: from the observation's own date.
#:
#: Calibrated against how official statistics actually arrive, which is later
#: than it first looks: an observation is dated to the START of its period, and
#: publication follows the END of that period by weeks. June CPI is dated
#: 1 June, covers through 30 June, and prints in mid-July — already ~45 days
#: from its own date on the day it is published. Annual World Bank series are
#: worse: 2025 is dated 1 Jan 2025 and lands in mid-2026, ~550 days out.
#:
#: These thresholds sit just beyond normal publication lag, so "lagging" means
#: something is genuinely wrong rather than merely recent. Anything past twice
#: the threshold is treated as abandoned.
STALENESS_DAYS = {"D": 5, "W": 14, "M": 90, "Q": 200, "A": 700}

#: One colour per country, used everywhere a country appears so that (say)
#: Indonesia is the same blue on every chart in the app.
COUNTRY_COLORS = {
    "ID": "#4C8DF6",
    "US": "#E4572E",
    "EA": "#17BEBB",
    "CN": "#F2B705",
    "SG": "#9B5DE5",
    "MY": "#00BB7E",
    "TH": "#F15BB5",
    "PH": "#7A8B99",
    "VN": "#C77DFF",
    "IN": "#FF8C42",
    "JP": "#5C7AEA",
    "GLOBAL": "#8D99AE",
}

CATEGORIES = ("inflation", "growth", "rates", "external", "markets")
FREQUENCIES = tuple(PERIODS_PER_YEAR)


@lru_cache(maxsize=1)
def _secrets_file() -> dict[str, str]:
    if not SECRETS_PATH.exists():
        return {}
    with SECRETS_PATH.open("rb") as fh:
        return tomllib.load(fh)


def get_secret(name: str, default: str | None = None) -> str | None:
    """Environment variable first, then secrets.toml, then the default."""
    value = os.environ.get(name)
    if value:
        return value
    value = _secrets_file().get(name)
    if value and not str(value).startswith("your-"):
        return str(value)
    return default


def require_secret(name: str) -> str:
    value = get_secret(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to {SECRETS_PATH} (see secrets.toml.example) "
            f"or export it as an environment variable."
        )
    return value


def ensure_dirs() -> None:
    for path in (DATA_DIR, SEED_DIR, MANUAL_DIR, EXPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)
