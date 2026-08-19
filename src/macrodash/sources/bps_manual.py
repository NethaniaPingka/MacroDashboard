"""Backwards-compatible alias for the manual source.

Phase 4 generalised hand entry beyond BPS: the first series that needed it was
the BI Rate, which is Bank Indonesia's, not BPS's. The implementation therefore
lives in ``manual.py`` under the source name ``manual``, and this module stays
so that ``source: bps_manual`` in a catalog file keeps resolving.

Prefer ``source: manual`` in new catalog entries.
"""

from __future__ import annotations

from .manual import ManualFetcher


class BpsManualFetcher(ManualFetcher):
    name = "bps_manual"


__all__ = ["BpsManualFetcher", "ManualFetcher"]
