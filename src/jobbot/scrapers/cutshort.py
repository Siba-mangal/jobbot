"""Cutshort scraper.

Cutshort is a React SPA over a JSON API, so the sniffer path is primary and
DOM selectors are the fallback. Applications are hosted on Cutshort itself,
though some listings link out to the company's own portal.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlencode

from playwright.sync_api import Page

from ..config import SearchQuery
from ..db import ApplyRoute
from .base import JobDetail, JobStub, NotLoggedIn
from .domutil import (
    click_if_present,
    find_cards,
    first_attr,
    first_href,
    first_text,
    id_from_url,
    longest_text,
    scroll_and_settle,
    slugify_id,
)
from .sniffer import JsonSniffer, get_path

BASE = "https://cutshort.io"

_JOB_KEYS = ("title", "job_title", "designation", "role")

_CARD_SELECTORS = (
    "[class*='job-card']",
    "[class*='JobCard']",
    "[data-testid*='job']",
    "a[href*='/jobs/']",
)
_TITLE_SELECTORS = ("[class*='job-title']", "[class*='JobTitle']", "h2", "h3")
_COMPANY_SELECTORS = ("[class*='company']", "[class*='Company']", "h4")
_LOCATION_SELECTORS = ("[class*='location']", "[class*='Location']", "[class*='city']")
_DESCRIPTION_SELECTORS = (
    "[class*='job-description']",
    "[class*='JobDescription']",
    "[class*='description']",
    "main",
    "article",
)


class CutshortScraper:
    site = "cutshort"
    login_url = f"{BASE}/login"

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def is_logged_in(self, page: Page) -> bool:
        page.goto(f"{BASE}/jobs", wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)
        return self.login_complete(page)

    def login_complete(self, page: Page) -> bool:
        # Passive only — this is polled while the user is typing. See the
        # protocol docstring in base.py.
        url = page.url.lower()
        if "cutshort.io" not in url:
            return False
        if any(marker in url for marker in ("/login", "/signup", "/register")):
            return False
        return (
            page.locator("a[href*='logout'], [class*='avatar'], [class*='profile-menu']").count() > 0
            or "/jobs" in url
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search_url(self, query: SearchQuery) -> str:
        params: dict[str, str] = {}
        if query.keywords:
            params["q"] = query.keywords
        if query.location:
            params["location"] = query.location
        if query.remote:
            params["remote"] = "true"
        return f"{BASE}/jobs?{urlencode(params)}" if params else f"{BASE}/jobs"

    def search(self, page: Page, query: SearchQuery, limit: int) -> Iterator[JobStub]:
        sniffer = JsonSniffer(page, patterns=("/api/", "/graphql"))
        try:
            page.goto(self._search_url(query), wait_until="domcontentloaded")
            page.wait_for_timeout(3_500)

            if any(m in page.url.lower() for m in ("/login", "/signup")):
                raise NotLoggedIn("Cutshort bounced us to the login page. Run: jobbot login cutshort")

            seen: set[str] = set()
            yielded = 0

            for _ in range(20):
                stubs = [
                    stub
                    for record in sniffer.find_records(required_keys=_JOB_KEYS)
                    if (stub := self._stub_from_record(record))
                ]
                if not stubs:
                    stubs = self._stubs_from_dom(page)

                fresh = 0
                for stub in stubs:
                    if stub.source_job_id in seen:
                        continue
                    seen.add(stub.source_job_id)
                    fresh += 1
                    yield stub
                    yielded += 1
                    if yielded >= limit:
                        return

                grew = scroll_and_settle(page, pixels=3_500, wait_ms=2_000)
                clicked = click_if_present(page, ("load more", "show more", "next"), wait_ms=2_500)
                if not grew and not clicked and fresh == 0:
                    return
        finally:
            sniffer.detach()

    # ------------------------------------------------------------------
    # Record → stub
    # ------------------------------------------------------------------

    def _stub_from_record(self, record: dict) -> JobStub | None:
        title = get_path(record, "title", "job_title", "designation", "role")
        if not title or not isinstance(title, str):
            return None

        company = get_path(
            record, "company.name", "company_name", "companyName", "organisation.name", "company"
        )
        if isinstance(company, dict):
            company = get_path(company, "name", "display_name", default="")
        if not company:
            return None

        job_id = get_path(record, "id", "_id", "job_id", "slug", "uuid")
        if not job_id:
            return None

        location = get_path(record, "location", "locations", "city", default="")
        if isinstance(location, list):
            location = ", ".join(str(x) for x in location[:3])
        elif isinstance(location, dict):
            location = get_path(location, "name", "city", default="")

        url = get_path(record, "url", "public_url", "absolute_url", default="")
        if url and str(url).startswith("/"):
            url = BASE + str(url)
        if not url:
            url = f"{BASE}/jobs/{job_id}"

        salary = get_path(record, "salary", "ctc", "compensation", default="")
        if isinstance(salary, dict):
            salary = get_path(salary, "display", "text", default="")

        remote_flag = get_path(record, "is_remote", "remote", "work_from_home", default=False)
        remote = bool(remote_flag) or "remote" in str(location).lower()

        return JobStub(
            source=self.site,
            source_job_id=str(job_id),
            url=str(url),
            title=title.strip(),
            company=str(company).strip(),
            location=str(location).strip(),
            remote=remote,
            salary_raw=str(salary).strip(),
        )

    def _stubs_from_dom(self, page: Page) -> list[JobStub]:
        cards = find_cards(page, _CARD_SELECTORS)
        if cards is None:
            return []

        stubs: list[JobStub] = []
        for i in range(min(cards.count(), 40)):
            card = cards.nth(i)
            title = first_text(card, _TITLE_SELECTORS)
            company = first_text(card, _COMPANY_SELECTORS)
            if not title or not company:
                continue

            href = first_href(card, BASE)
            job_id = id_from_url(href, (r"/jobs/([\w-]+)",)) or slugify_id(company, title)
            location = first_text(card, _LOCATION_SELECTORS)

            stubs.append(
                JobStub(
                    source=self.site,
                    source_job_id=job_id,
                    url=href or f"{BASE}/jobs/{job_id}",
                    title=title,
                    company=company,
                    location=location,
                    remote="remote" in location.lower(),
                )
            )
        return stubs

    # ------------------------------------------------------------------
    # Hydrate
    # ------------------------------------------------------------------

    def hydrate(self, page: Page, stub: JobStub) -> JobDetail:
        page.goto(stub.url, wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)
        click_if_present(page, ("see more", "read more", "show more"), wait_ms=800)

        description = longest_text(page, _DESCRIPTION_SELECTORS, min_len=300)
        if not description:
            description = page.inner_text("body")[:20_000]

        route = ApplyRoute.BOARD_NATIVE
        ats_url = ""
        external = first_attr(
            page, ("a[href^='http']:has-text('Apply')", "a[class*='apply'][href^='http']"), "href"
        )
        if external and "cutshort.io" not in external:
            route = ApplyRoute.UNKNOWN  # resolved by ats.detect at apply time
            ats_url = external

        return JobDetail(
            description=description,
            apply_route=route,
            ats_url=ats_url,
            location=stub.location,
        )
