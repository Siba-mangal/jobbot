"""Scraper registry."""

from __future__ import annotations

from .base import Scraper
from .cutshort import CutshortScraper
from .instahyre import InstahyreScraper
from .linkedin import LinkedInScraper


def _build() -> dict[str, Scraper]:
    scrapers: dict[str, Scraper] = {}
    for cls in (InstahyreScraper, CutshortScraper, LinkedInScraper):
        instance = cls()
        scrapers[instance.site] = instance
    return scrapers


SCRAPERS: dict[str, Scraper] = _build()


def get_scraper(site: str) -> Scraper:
    try:
        return SCRAPERS[site]
    except KeyError:
        known = ", ".join(sorted(SCRAPERS)) or "(none)"
        raise KeyError(f"No scraper for {site!r}. Available: {known}") from None
