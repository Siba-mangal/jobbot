"""Instahyre scraper.

Calibrated against the live site. Two things about Instahyre shape this:

**Its listing feed is personalized, not searched.** `/api/v1/job_search`
ignores `q`, `keywords`, and `search` entirely — it returns the roles
Instahyre has matched to *your* profile. Only `jobLocations` filters
server-side. So the `keywords` in `config/search.yaml` are applied here as a
client-side filter over that feed rather than sent to the site. Leave them
empty to take the whole feed and let the scorer sort it out.

**The API is read directly.** The page is an AngularJS shell over
`/api/v1/job_search`; calling it with the session cookies is both far more
stable than scraping the rendered DOM and much lighter on the site — one JSON
request per 20 jobs instead of a page render and a scroll. The DOM path
remains as a fallback if the response shape ever changes.
"""

from __future__ import annotations

import re
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
    slugify_id,
)
from .sniffer import get_path

BASE = "https://www.instahyre.com"
SEARCH_API = f"{BASE}/api/v1/job_search"
PAGE_SIZE = 20

# DOM fallback, used only if the JSON shape changes.
_CARD_SELECTORS = (
    "[class*='opportunity-card']",
    "[class*='job-card']",
    "[ng-repeat*='job']",
    "li[class*='job']",
)
_TITLE_SELECTORS = ("[class*='job-title']", "h2", "h3", "a[href*='/job-']")
_COMPANY_SELECTORS = ("[class*='company-name']", "[class*='employer']", "h4")
_LOCATION_SELECTORS = ("[class*='location']", "[class*='city']")
# Job pages, in signal order. `[ng-bind-html]` is the posting body itself;
# `.container` is the whole job panel (company profile, skills, reviews) with
# none of the site nav. `body` would drag in the header chrome, which is pure
# token cost and adds nothing a scorer can use.
_JD_SELECTORS = ("[ng-bind-html]", "[class*='job-detail']")
_CONTEXT_SELECTORS = (".container", "[class*='opportunity-detail']", "main")


class InstahyreScraper:
    site = "instahyre"
    login_url = f"{BASE}/login/"

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def is_logged_in(self, page: Page) -> bool:
        page.goto(f"{BASE}/search-jobs", wait_until="domcontentloaded")
        page.wait_for_timeout(3_000)
        return self.login_complete(page)

    def login_complete(self, page: Page) -> bool:
        # Passive only — this is polled while the user is typing. See the
        # protocol docstring in base.py.
        url = page.url.lower()
        if "instahyre.com" not in url:
            return False
        if any(marker in url for marker in ("/login", "/signup", "/register")):
            return False
        return (
            page.locator("a[href*='logout'], [class*='logout']").count() > 0
            or page.get_by_text("SIGN OUT", exact=False).count() > 0
            or "/candidate/" in url
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _api_url(self, query: SearchQuery, offset: int = 0) -> str:
        params = {
            "limit": PAGE_SIZE,
            "offset": offset,
            "source": "opportunities",
            "company_size": 0,
            "job_type": 0,
        }
        if query.location:
            # The one filter the API actually honours.
            params["jobLocations"] = query.location
        return f"{SEARCH_API}?{urlencode(params)}"

    def search(self, page: Page, query: SearchQuery, limit: int) -> Iterator[JobStub]:
        # Load the shell once so the request context carries session cookies
        # and looks like it came from the app.
        page.goto(f"{BASE}/search-jobs", wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)

        if any(m in page.url.lower() for m in ("/login", "/signup")):
            raise NotLoggedIn("Instahyre bounced us to the login page. Run: jobbot login instahyre")

        terms = _query_terms(query.keywords)
        seen: set[str] = set()
        yielded = 0
        url = self._api_url(query)

        for _ in range(25):  # hard bound: 500 records
            try:
                response = page.request.get(url)
            except Exception as exc:
                raise NotLoggedIn(f"Instahyre API request failed: {exc}") from exc

            if response.status == 403:
                raise NotLoggedIn("Instahyre rejected the API request. Run: jobbot login instahyre")
            if response.status != 200:
                break

            payload = response.json()
            records = payload.get("objects") or []
            if not records:
                break

            for record in records:
                stub = self._stub_from_record(record)
                if stub is None or stub.source_job_id in seen:
                    continue
                if terms and not _matches(record, stub, terms):
                    continue
                seen.add(stub.source_job_id)
                yield stub
                yielded += 1
                if yielded >= limit:
                    return

            next_path = (payload.get("meta") or {}).get("next")
            if not next_path:
                break
            url = next_path if next_path.startswith("http") else BASE + next_path

        # Nothing from the API (shape changed?) — try the rendered page.
        if yielded == 0:
            for stub in self._stubs_from_dom(page):
                if terms and not _matches({}, stub, terms):
                    continue
                yield stub
                yielded += 1
                if yielded >= limit:
                    return

    # ------------------------------------------------------------------
    # Record → stub
    # ------------------------------------------------------------------

    def _stub_from_record(self, record: dict) -> JobStub | None:
        title = get_path(record, "title", "candidate_title", "designation")
        if not title or not isinstance(title, str):
            return None

        # Company lives at employer.company_name — confirmed against the live
        # API. The other spellings are kept as cheap insurance.
        company = get_path(
            record,
            "employer.company_name",
            "employer.name",
            "company.name",
            "company_name",
        )
        if not company:
            return None

        job_id = get_path(record, "id", "pk", "job_id")
        if not job_id:
            return None

        location = get_path(record, "locations", "location", "city", default="")
        if isinstance(location, list):
            location = ", ".join(str(x) for x in location[:3])
        location = str(location).replace(",", ", ")

        url = get_path(record, "public_url", "url", "absolute_url", default="")
        if url and str(url).startswith("/"):
            url = BASE + str(url)
        if not url:
            url = f"{BASE}/job-{job_id}/"

        return JobStub(
            source=self.site,
            source_job_id=str(job_id),
            url=str(url),
            title=title.strip(),
            company=str(company).strip(),
            location=location.strip(),
            remote="remote" in location.lower() or "work from home" in location.lower(),
        )

    # ------------------------------------------------------------------
    # DOM fallback
    # ------------------------------------------------------------------

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
            job_id = id_from_url(href, (r"/job-(\d+)", r"/job/(\d+)")) or slugify_id(company, title)
            location = first_text(card, _LOCATION_SELECTORS)
            stubs.append(
                JobStub(
                    source=self.site,
                    source_job_id=job_id,
                    url=href or f"{BASE}/job-{job_id}/",
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

    def _description(self, page: Page) -> str:
        """The posting body plus company context, without the site chrome.

        Composed rather than taken from one selector: the posting body and the
        company panel live in separate elements, and the only container
        holding both also holds the nav bar.
        """
        jd = longest_text(page, _JD_SELECTORS, min_len=200, timeout=4_000)
        context = longest_text(page, _CONTEXT_SELECTORS, min_len=400, timeout=4_000)

        # When the context block already contains the posting body, it is the
        # superset — keep it, since the company profile it adds (size, domain,
        # tech) is real signal for a fit score. Only concatenate when they are
        # genuinely disjoint.
        if jd and context and jd[:200] in context:
            composed = context
        else:
            composed = "\n\n".join(p for p in (jd, context) if p).strip()
        if len(composed) >= 200:
            return composed

        # Last resort: the whole page, chrome and all. Better a noisy
        # description than an empty one.
        try:
            return page.inner_text("body")[:20_000]
        except Exception:
            return composed

    def hydrate(self, page: Page, stub: JobStub) -> JobDetail:
        page.goto(stub.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3_000)
        click_if_present(page, ("see more", "read more", "show more", "view more"), wait_ms=800)

        description = self._description(page)

        # Instahyre applications normally happen on Instahyre itself.
        route = ApplyRoute.BOARD_NATIVE
        ats_url = ""
        external = first_attr(
            page, ("a[href^='http']:has-text('Apply')", "a[class*='apply'][href^='http']"), "href"
        )
        if external and "instahyre.com" not in external:
            route = ApplyRoute.UNKNOWN  # resolved by ats.detect at apply time
            ats_url = external

        return JobDetail(
            description=description,
            apply_route=route,
            ats_url=ats_url,
            location=stub.location,
        )


# ----------------------------------------------------------------------
# Client-side keyword filtering
# ----------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9+#.]+")
# Words that match nearly every posting and so filter nothing useful.
_STOPWORDS = {"engineer", "developer", "software", "senior", "junior", "lead", "sr", "jr"}


def _query_terms(keywords: str) -> list[str]:
    """Meaningful terms from a config query.

    Instahyre's feed is already personalized, so the useful signal in
    "backend engineer" is *backend* — matching on "engineer" would keep
    everything. If a query is nothing but generic words, we keep them rather
    than filter on nothing.
    """
    words = _WORD.findall((keywords or "").lower())
    specific = [w for w in words if w not in _STOPWORDS and len(w) > 2]
    return specific or [w for w in words if len(w) > 2]


def _matches(record: dict, stub: JobStub, terms: list[str]) -> bool:
    """True if any query term appears in the title or the record's own tags."""
    haystack = stub.title.lower()
    tags = record.get("keywords") if isinstance(record, dict) else None
    if isinstance(tags, list):
        haystack += " " + " ".join(str(t).lower() for t in tags)
    return any(term in haystack for term in terms)
