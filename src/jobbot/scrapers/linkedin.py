"""LinkedIn scraper.

⚠️  LinkedIn's User Agreement prohibits automated scraping and applying, and
they actively detect it. This ships disabled in config/search.yaml and runs
with the lowest daily cap of any site. Enable at your own risk.

Two things make LinkedIn cheaper to scrape than it looks:

- The search page is master/detail. Clicking a card loads the description in
  the right pane without a navigation, so one page load covers a whole page
  of results.
- LinkedIn's own frontend fetches from `/voyager/api/`, which the sniffer
  reads in preference to the DOM.

External-apply jobs are left as UNKNOWN rather than resolved during
discovery. Clicking "Apply" registers application intent on LinkedIn's side
and opens the employer's site — doing that for jobs you may never apply to is
both noisy and dishonest. The route is resolved at apply time instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from playwright.sync_api import Page

from ..config import SearchQuery
from ..db import ApplyRoute
from .base import JobDetail, JobStub, NotLoggedIn
from .domutil import (
    click_if_present,
    find_cards,
    first_attr,
    first_text,
    id_from_url,
    longest_text,
    scroll_and_settle,
)
from .sniffer import JsonSniffer

BASE = "https://www.linkedin.com"

_CARD_SELECTORS = (
    "div.job-card-container",
    "li.jobs-search-results__list-item",
    "[data-job-id]",
    "li.scaffold-layout__list-item",
)
_TITLE_SELECTORS = (
    "a.job-card-container__link",
    ".job-card-list__title",
    ".job-card-container__link",
    "a[href*='/jobs/view/']",
)
_COMPANY_SELECTORS = (
    ".job-card-container__primary-description",
    ".artdeco-entity-lockup__subtitle",
    ".job-card-container__company-name",
)
_LOCATION_SELECTORS = (
    ".job-card-container__metadata-item",
    ".artdeco-entity-lockup__caption",
    ".job-card-container__metadata-wrapper",
)
_DESCRIPTION_SELECTORS = (
    ".jobs-description__content",
    ".jobs-box__html-content",
    "#job-details",
    ".jobs-description-content__text",
    ".jobs-search__job-details--container",
)
_APPLY_BUTTON_SELECTORS = (
    "button.jobs-apply-button",
    ".jobs-s-apply button",
    "[data-live-test-job-apply-button]",
)

# f_TPR is a seconds-ago window, not a fixed set of buckets — r3600 is the
# last hour, r86400 the last day. f_WT=2 is remote. Sorting by date (sortBy=DD)
# matters for tight windows: relevance ordering can bury a 20-minute-old post.
_SORT_BY_DATE = "DD"


class LinkedInScraper:
    site = "linkedin"
    login_url = f"{BASE}/login"

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def is_logged_in(self, page: Page) -> bool:
        page.goto(f"{BASE}/feed/", wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)
        return self.login_complete(page)

    def login_complete(self, page: Page) -> bool:
        # Passive only — this is polled while the user is typing, and LinkedIn
        # logins routinely involve 2FA and a checkpoint. See base.py.
        url = page.url.lower()
        if "linkedin.com" not in url:
            return False
        if any(
            marker in url
            for marker in ("/login", "/authwall", "/checkpoint", "/uas/", "/signup")
        ):
            return False
        # Nav links to the signed-in-only sections. Class names on LinkedIn's
        # nav churn constantly (`nav.global-nav` no longer exists); hrefs for
        # Messaging and My Network have been stable for years and only render
        # for an authenticated session.
        return (
            page.locator(
                "a[href*='/messaging'], a[href*='/mynetwork'], a[href*='/notifications']"
            ).count()
            > 0
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search_url(self, query: SearchQuery, start: int = 0) -> str:
        params: dict[str, str] = {}
        if query.keywords:
            params["keywords"] = query.keywords
        if query.location:
            params["location"] = query.location
        if query.remote:
            params["f_WT"] = "2"
        if window := query.freshness_seconds:
            params["f_TPR"] = f"r{window}"
            # Newest-first, so a narrow window isn't ordered by relevance and
            # doesn't hide the freshest posting below page one.
            params["sortBy"] = _SORT_BY_DATE
        if start:
            params["start"] = str(start)
        return f"{BASE}/jobs/search/?{urlencode(params)}"

    def search(self, page: Page, query: SearchQuery, limit: int) -> Iterator[JobStub]:
        sniffer = JsonSniffer(page, patterns=("/voyager/api/",))
        # f_TPR is honoured server-side, but a job whose real timestamp falls
        # outside the window still occasionally rides along (LinkedIn pads thin
        # result sets with related postings). A 1-hour view that shows
        # 15-hour-old jobs is worse than useless, so re-check the dates we get.
        window = query.freshness_seconds
        cutoff = datetime.now(UTC) - timedelta(seconds=window) if window else None

        try:
            seen: set[str] = set()
            yielded = 0

            for page_index in range(10):  # 25 results per page; hard-bounded
                # Captures accumulate on the page, so a previous page's cards
                # would otherwise leak into this one's result set.
                sniffer.clear()

                page.goto(self._search_url(query, start=page_index * 25), wait_until="domcontentloaded")
                page.wait_for_timeout(3_500)

                if self._is_authwall(page):
                    raise NotLoggedIn("LinkedIn showed the auth wall. Run: jobbot login linkedin")

                # The results list is virtualized — scroll it so every card renders.
                for _ in range(4):
                    if not scroll_and_settle(page, pixels=2_000, wait_ms=1_200):
                        break

                stubs = [
                    stub
                    for card in _job_cards(sniffer)
                    if (stub := self._stub_from_card(card))
                ]
                if not stubs:
                    stubs = self._stubs_from_dom(page)

                if not stubs:
                    return

                fresh = 0
                for stub in stubs:
                    if stub.source_job_id in seen:
                        continue
                    seen.add(stub.source_job_id)
                    fresh += 1
                    # Only drop when we actually know the date — an unknown
                    # date is not evidence of staleness.
                    if cutoff and stub.posted_at and stub.posted_at < cutoff:
                        continue
                    yield stub
                    yielded += 1
                    if yielded >= limit:
                        return

                if fresh == 0:
                    return  # pagination exhausted
        finally:
            sniffer.detach()

    def _is_authwall(self, page: Page) -> bool:
        url = page.url.lower()
        return any(m in url for m in ("/authwall", "/login", "/checkpoint", "/uas/"))

    # ------------------------------------------------------------------
    # Record → stub
    # ------------------------------------------------------------------

    def _stub_from_card(self, card: dict) -> JobStub | None:
        """Turn one `JobPostingCard` into a stub.

        LinkedIn wraps every display string in a "text view model" — an object
        carrying `text` plus styling attributes — so the fields are not plain
        strings, and a naive `record["title"]` yields a dict.
        """
        title = _tvm(card.get("title"))
        company = _tvm(card.get("primaryDescription"))
        if not title or not company:
            return None

        job_id = _numeric_id(str(card.get("entityUrn") or card.get("trackingUrn") or ""))
        if not job_id:
            return None

        location = _tvm(card.get("secondaryDescription"))
        return JobStub(
            source=self.site,
            source_job_id=job_id,
            url=f"{BASE}/jobs/view/{job_id}/",
            title=_clean_title(title),
            company=_clean_company(company),
            location=location,
            remote="remote" in location.lower(),
            salary_raw=_salary(_tvm(card.get("tertiaryDescription"))),
            posted_at=_listed_date(card),
        )

    def _stubs_from_dom(self, page: Page) -> list[JobStub]:
        cards = find_cards(page, _CARD_SELECTORS)
        if cards is None:
            return []

        stubs: list[JobStub] = []
        for i in range(min(cards.count(), 30)):
            card = cards.nth(i)

            job_id = ""
            try:
                job_id = card.get_attribute("data-job-id") or ""
            except Exception:
                pass
            if not job_id:
                href = first_attr(card, _TITLE_SELECTORS, "href")
                job_id = id_from_url(href, (r"/jobs/view/(\d+)", r"currentJobId=(\d+)"))
            if not job_id:
                continue

            title = first_text(card, _TITLE_SELECTORS)
            company = first_text(card, _COMPANY_SELECTORS)
            if not title or not company:
                continue

            location = first_text(card, _LOCATION_SELECTORS)
            stubs.append(
                JobStub(
                    source=self.site,
                    source_job_id=job_id,
                    url=f"{BASE}/jobs/view/{job_id}/",
                    title=_clean_title(title),
                    company=_clean_company(company),
                    location=location,
                    remote="remote" in location.lower(),
                    posted_at=_posted_at_from_card(card),
                )
            )
        return stubs

    # ------------------------------------------------------------------
    # Hydrate
    # ------------------------------------------------------------------

    def hydrate(self, page: Page, stub: JobStub) -> JobDetail:
        page.goto(stub.url, wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)

        if self._is_authwall(page):
            raise NotLoggedIn("LinkedIn showed the auth wall. Run: jobbot login linkedin")

        # "See more" collapses long descriptions.
        click_if_present(page, ("see more", "show more"), wait_ms=800)

        description = longest_text(page, _DESCRIPTION_SELECTORS, min_len=400)
        if not description:
            description = page.inner_text("body")[:20_000]

        route, ats_url = self._detect_route(page)

        # The list page doesn't always carry a date; the detail page states
        # "Posted N ago" in its header. Without it the job never shows up in a
        # freshness view at all.
        posted_at = stub.posted_at
        if posted_at is None:
            header = first_text(
                page,
                (
                    ".jobs-unified-top-card__primary-description",
                    ".job-details-jobs-unified-top-card__primary-description-container",
                    ".jobs-unified-top-card__subtitle-secondary-grouping",
                    "[class*='unified-top-card']",
                ),
                timeout=3_000,
            )
            posted_at = parse_relative_age(header)

        return JobDetail(
            description=description,
            apply_route=route,
            ats_url=ats_url,
            location=stub.location,
            posted_at=posted_at,
        )

    def _detect_route(self, page: Page) -> tuple[ApplyRoute, str]:
        """Easy Apply vs external, read without clicking anything.

        Clicking "Apply" would register application intent on LinkedIn and
        open the employer's site — not something to do while merely browsing.
        External jobs stay UNKNOWN and are resolved at apply time.
        """
        button_text = first_text(page, _APPLY_BUTTON_SELECTORS, timeout=3_000).lower()
        if "easy apply" in button_text:
            return ApplyRoute.BOARD_NATIVE, ""

        # Occasionally the external target is present as a plain link.
        href = first_attr(page, ("a.jobs-apply-button", "a[data-tracking-control-name*='apply']"), "href")
        if href and "linkedin.com" not in href:
            return ApplyRoute.UNKNOWN, href

        return ApplyRoute.UNKNOWN, ""


# ----------------------------------------------------------------------

_URN_ID = re.compile(r"(\d{6,})")


def _numeric_id(value: str) -> str:
    match = _URN_ID.search(value or "")
    return match.group(1) if match else ""


def _tvm(value) -> str:
    """Unwrap a LinkedIn "text view model" — `{"text": ..., "attributesV2": …}`."""
    if isinstance(value, dict):
        value = value.get("text", "")
    return str(value).strip() if value else ""


def _job_cards(sniffer: JsonSniffer) -> list[dict]:
    """Pull search-result cards out of the captured Voyager responses.

    A generic "find the longest list of dicts with a `title` key" search does
    not work here: the same payloads carry the filter panel (whose entries also
    have a `title`) and a `meta.microSchema` block describing every field name
    in the response. Both outrank the real results by length. The cards are
    identifiable exactly, so match them exactly — a `JobPostingCard` from the
    JOBS_SEARCH surface, as opposed to the JOB_DETAILS or recommendation ones.
    """
    cards: dict[str, dict] = {}
    for capture in sniffer.captured:
        included = capture.payload.get("included") if isinstance(capture.payload, dict) else None
        if not isinstance(included, list):
            continue
        for node in included:
            if not isinstance(node, dict):
                continue
            urn = str(node.get("entityUrn", ""))
            if "jobPostingCard" not in urn or "JOBS_SEARCH" not in urn:
                continue
            if _tvm(node.get("title")):
                cards[urn] = node  # dedupe: cards repeat across paged XHRs
    return list(cards.values())


def _listed_date(card: dict) -> datetime | None:
    """The posting time, from the card's LISTED_DATE footer item.

    This is the only authoritative date on a search card — `tertiaryDescription`
    holds salary when there is one, and the visible "2 hours ago" is rendered
    from this same field.
    """
    for item in card.get("footerItems") or []:
        if isinstance(item, dict) and item.get("type") == "LISTED_DATE":
            if stamp := _epoch_ms(item.get("timeAt")):
                return stamp
    return None


_MONEY = re.compile(r"[₹$€£]|\b(?:lpa|per year|/yr|/hr|k/)\b", re.IGNORECASE)


def _salary(text: str) -> str:
    """`tertiaryDescription` is salary on some cards and filler on others."""
    return text if text and _MONEY.search(text) else ""


def _epoch_ms(value) -> datetime | None:
    """LinkedIn's `listedAt` is epoch milliseconds."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    # Guard against a seconds-vs-milliseconds mix-up producing a 1970 date.
    if ms < 10_000_000_000:
        ms *= 1000
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


# "3 minutes ago", "2 hours ago", "1 day ago", "Posted 45 minutes ago"
_AGO = re.compile(
    r"(\d+)\s*(minute|min|hour|hr|day|week|month)s?\s*ago", re.IGNORECASE
)
_AGO_UNITS = {
    "minute": 60, "min": 60, "hour": 3600, "hr": 3600,
    "day": 86400, "week": 604800, "month": 2592000,
}


def parse_relative_age(text: str) -> datetime | None:
    """Turn a card's "posted N units ago" into a timestamp.

    The DOM fallback has no epoch field, and for a 1-hour window the posting
    time is the whole point — a job with no age is indistinguishable from a
    stale one.
    """
    match = _AGO.search(text or "")
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    seconds = _AGO_UNITS.get(unit)
    if not seconds:
        return None
    return datetime.now(UTC) - timedelta(seconds=amount * seconds)


_AGE_SELECTORS = (
    "time",
    "[class*='listdate']",
    "[class*='posted']",
    ".job-search-card__listdate",
    ".job-card-container__footer-item",
)


def _posted_at_from_card(card) -> datetime | None:
    # A <time datetime="..."> is exact; the visible "2 hours ago" is not, but
    # it is good enough to tell a fresh posting from yesterday's.
    iso = first_attr(card, ("time[datetime]",), "datetime")
    if iso:
        try:
            parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return parse_relative_age(first_text(card, _AGE_SELECTORS))


def _clean_title(text: str) -> str:
    """LinkedIn duplicates the title for screen readers ("Foo\\nFoo")."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) >= 2 and lines[0] == lines[1]:
        return lines[0]
    return lines[0] if lines else text.strip()


def _clean_company(text: str) -> str:
    return text.split("\n")[0].strip(" ·-–")
