"""The scraper contract.

Two phases, deliberately split:

- ``search``   — cheap. Reads list pages, yields stubs (title/company/URL).
- ``hydrate``  — expensive. Opens one detail page, returns the full JD.

The split exists because detail-page views are the scarce, rate-limited
resource. The runner filters and dedupes stubs *before* hydrating, so we never
spend a page view on a job we already have or would have thrown away.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from playwright.sync_api import Page

from ..config import SearchQuery
from ..db import ApplyRoute, make_fingerprint


@dataclass
class JobStub:
    """What a list page gives us — enough to dedupe and prefilter."""

    source: str
    source_job_id: str
    url: str
    title: str
    company: str
    location: str = ""
    remote: bool = False
    posted_at: datetime | None = None
    salary_raw: str = ""

    @property
    def fingerprint(self) -> str:
        return make_fingerprint(self.company, self.title, self.location)

    def __str__(self) -> str:
        where = f" — {self.location}" if self.location else ""
        return f"{self.title} @ {self.company}{where}"


@dataclass
class JobDetail:
    """What a detail page adds."""

    description: str
    apply_route: ApplyRoute = ApplyRoute.UNKNOWN
    ats_type: str = ""
    ats_url: str = ""
    salary_raw: str = ""
    location: str = ""
    remote: bool | None = None
    #: Fallback for when the list page carried no posting date. A job with no
    #: date is invisible to the freshness filters, so it's worth a second try.
    posted_at: datetime | None = None
    extra: dict = field(default_factory=dict)


class ScraperError(RuntimeError):
    """Scraper could not do its job. Non-fatal — the runner isolates sites."""


class NotLoggedIn(ScraperError):
    """Session expired or was never established."""


@runtime_checkable
class Scraper(Protocol):
    """Every board implements this. The runner supplies the page and handles
    pacing, caps, dedupe, and failure isolation."""

    site: str
    login_url: str

    def is_logged_in(self, page: Page) -> bool:
        """Check the persistent session is still valid.

        May navigate — callers use this before scraping, when a navigation
        costs nothing. Never call it while the user is interacting with the
        page.
        """
        ...

    def login_complete(self, page: Page) -> bool:
        """Has the user finished logging in?

        **Must not navigate, click, or reload.** This is polled every couple
        of seconds while the user is typing their credentials, so any
        navigation here would yank the form out from under them. Read the
        current URL and DOM only.
        """
        ...

    def search(self, page: Page, query: SearchQuery, limit: int) -> Iterator[JobStub]:
        """Walk list pages, yielding stubs. Must respect `limit` and stop."""
        ...

    def hydrate(self, page: Page, stub: JobStub) -> JobDetail:
        """Open the detail page and extract the full description."""
        ...
