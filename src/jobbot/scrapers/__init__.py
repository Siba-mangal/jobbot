"""Job board scrapers."""

from .base import JobDetail, JobStub, Scraper
from .registry import SCRAPERS, get_scraper

__all__ = ["JobDetail", "JobStub", "SCRAPERS", "Scraper", "get_scraper"]
