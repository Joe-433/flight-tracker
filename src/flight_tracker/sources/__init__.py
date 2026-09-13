"""Scraping backends.

EVERY call that touches Google lives in this package. When Google changes
something -- and it will -- this is the only place that should need edits.
"""

from __future__ import annotations

from ..config import Config
from .base import Offer, ScrapeError, Source, date_pairs


def get_source(cfg: Config) -> Source:
    backend = cfg.source.backend
    if backend == "pairs":
        from .pairs import PairsSource

        return PairsSource(cfg)
    if backend == "grid":
        from .grid import GridSource

        return GridSource(cfg)
    if backend == "mock":
        from .mock import MockSource

        return MockSource(cfg)
    raise ValueError("unknown source backend: %r" % backend)


__all__ = ["Offer", "ScrapeError", "Source", "date_pairs", "get_source"]
