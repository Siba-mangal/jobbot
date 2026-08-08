"""LinkedIn card mapping and freshness, pinned to the live Voyager shape.

These exist because the first version of this scraper looked like it worked
while extracting the wrong thing entirely. It searched every captured payload
for "the longest list of dicts carrying a `title` key" — and the winner was
never the job list. Two decoys outrank it:

- the search *filter panel*, whose entries are `{"title": "Jobs", "filters": …}`
- `meta.microSchema`, which describes every field name in the response

So a 1-hour search returned filter metadata, silently fell through to the DOM,
and surfaced 15-hour-old jobs in a "last hour" view. The fix is to match the
cards exactly rather than by shape, which is what `_job_cards` now does.

The second trap is that LinkedIn wraps every display string in a "text view
model" — `{"text": …, "attributesV2": […]}` — so `card["title"]` is a dict, and
anything expecting a plain string drops the record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobbot.config import SearchQuery
from jobbot.scrapers.linkedin import (
    LinkedInScraper,
    _job_cards,
    _listed_date,
    _salary,
    _tvm,
)


def tvm(text: str) -> dict:
    """A text view model, as LinkedIn actually sends it."""
    return {
        "text": text,
        "textDirection": "USER_LOCALE",
        "attributesV2": [{"start": 0, "length": len(text)}],
    }


def card(
    job_id: int,
    title: str,
    company: str,
    location: str = "Bengaluru, Karnataka, India",
    tertiary: str | None = None,
    listed_ms: int | None = None,
    surface: str = "JOBS_SEARCH",
) -> dict:
    node = {
        "$type": "com.linkedin.voyager.dash.jobs.JobPostingCard",
        "entityUrn": f"urn:li:fsd_jobPostingCard:({job_id},{surface})",
        "title": tvm(title),
        "primaryDescription": tvm(company),
        "secondaryDescription": tvm(location),
        "logo": {},
    }
    if tertiary is not None:
        node["tertiaryDescription"] = tvm(tertiary)
    if listed_ms is not None:
        node["footerItems"] = [
            {
                "type": "LISTED_DATE",
                "timeAt": listed_ms,
                "$type": "com.linkedin.voyager.dash.jobs.JobPostingCardFooterItem",
            }
        ]
    return node


# The two decoys that beat the real card list on length.
FILTER_PANEL_ENTRY = {
    "$type": "com.linkedin.voyager.dash.search.SearchFilterCluster",
    "title": "Jobs",
    "filters": [{"name": "f_TPR"}, {"name": "f_WT"}],
}
MICRO_SCHEMA_ENTRY = {
    "title": {"type": "com.linkedin.390ac1c1431d5b4e340a65af24ed68f2"},
    "primaryDescription": {"type": "com.linkedin.390ac1c1431d5b4e340a65af24ed68f2"},
    "jobPostingTitle": {"type": "string"},
}


class FakeSniffer:
    """Just enough of JsonSniffer for the extractor."""

    def __init__(self, payloads):
        self.captured = [type("Cap", (), {"payload": p})() for p in payloads]


def ms_ago(**kwargs) -> int:
    return int((datetime.now(UTC) - timedelta(**kwargs)).timestamp() * 1000)


# ----------------------------------------------------------------------
# Text view models


def test_tvm_unwraps_the_text_field():
    assert _tvm(tvm("Backend Engineer")) == "Backend Engineer"


def test_tvm_tolerates_a_plain_string():
    assert _tvm("Backend Engineer") == "Backend Engineer"


def test_tvm_is_empty_for_missing_or_null():
    assert _tvm(None) == ""
    assert _tvm({}) == ""


# ----------------------------------------------------------------------
# Card extraction


def test_finds_cards_and_ignores_the_filter_panel_and_microschema():
    payload = {
        "included": [
            FILTER_PANEL_ENTRY,
            MICRO_SCHEMA_ENTRY,
            card(4449256491, "Backend Engineer", "Incubyte"),
            FILTER_PANEL_ENTRY,
            card(4442118277, "Software Engineer (AI)", "Cisco"),
        ],
        "meta": {"microSchema": {"types": {"com.linkedin.x": {"fields": MICRO_SCHEMA_ENTRY}}}},
    }
    found = _job_cards(FakeSniffer([payload]))
    assert {_tvm(c["title"]) for c in found} == {"Backend Engineer", "Software Engineer (AI)"}


def test_ignores_cards_from_other_surfaces():
    """Only the JOBS_SEARCH surface is a search result.

    The detail pane and the "similar jobs" rail emit JobPostingCards too, and
    folding those in silently widens every search beyond its filters.
    """
    payload = {
        "included": [
            card(1111111, "Real Result", "Acme"),
            card(2222222, "Detail Pane", "Other", surface="JOB_DETAILS"),
            card(3333333, "Recommendation", "Third", surface="JOBS_HOME_JYMBII"),
        ]
    }
    found = _job_cards(FakeSniffer([payload]))
    assert [_tvm(c["title"]) for c in found] == ["Real Result"]


def test_dedupes_cards_repeated_across_payloads():
    dup = card(4449256491, "Backend Engineer", "Incubyte")
    found = _job_cards(FakeSniffer([{"included": [dup]}, {"included": [dup]}]))
    assert len(found) == 1


def test_survives_payloads_without_an_included_list():
    sniffer = FakeSniffer([{"data": {}}, [1, 2, 3], {"included": "not-a-list"}])
    assert _job_cards(sniffer) == []


# ----------------------------------------------------------------------
# Card → stub


def test_stub_from_card_maps_every_field():
    stub = LinkedInScraper()._stub_from_card(
        card(
            4449256491,
            "Backend Engineer",
            "Incubyte",
            location="India (Remote)",
            tertiary="₹1.5M/yr - ₹4.5M/yr",
            listed_ms=ms_ago(minutes=20),
        )
    )
    assert stub is not None
    assert stub.source == "linkedin"
    assert stub.source_job_id == "4449256491"
    assert stub.url == "https://www.linkedin.com/jobs/view/4449256491/"
    assert stub.title == "Backend Engineer"
    assert stub.company == "Incubyte"
    assert stub.location == "India (Remote)"
    assert stub.remote is True
    assert stub.salary_raw == "₹1.5M/yr - ₹4.5M/yr"
    assert stub.posted_at is not None
    assert (datetime.now(UTC) - stub.posted_at) < timedelta(hours=1)


def test_stub_is_dropped_without_a_company():
    node = card(4449256491, "Backend Engineer", "Incubyte")
    node["primaryDescription"] = tvm("")
    assert LinkedInScraper()._stub_from_card(node) is None


def test_stub_is_dropped_without_a_numeric_id():
    node = card(4449256491, "Backend Engineer", "Incubyte")
    node["entityUrn"] = "urn:li:fsd_jobPostingCard:(abc,JOBS_SEARCH)"
    assert LinkedInScraper()._stub_from_card(node) is None


def test_missing_date_is_none_not_an_epoch_zero_date():
    """A dateless job must stay dateless.

    Defaulting it to 1970 would make it look ancient; defaulting to now would
    smuggle it into every freshness view. Neither is honest.
    """
    stub = LinkedInScraper()._stub_from_card(card(4449256491, "Backend Engineer", "Incubyte"))
    assert stub is not None and stub.posted_at is None


# ----------------------------------------------------------------------
# Dates and salary


def test_listed_date_reads_the_footer_item():
    stamp = _listed_date(card(1, "T", "C", listed_ms=1786198515000))
    assert stamp == datetime.fromtimestamp(1786198515, tz=UTC)


def test_listed_date_ignores_other_footer_types():
    node = card(1, "T", "C")
    node["footerItems"] = [{"type": "EASY_APPLY_TEXT"}, {"type": "PROMOTED"}]
    assert _listed_date(node) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("₹1.5M/yr - ₹4.5M/yr", "₹1.5M/yr - ₹4.5M/yr"),
        ("$180,000/yr - $220,000/yr", "$180,000/yr - $220,000/yr"),
        ("12 LPA", "12 LPA"),
        # tertiaryDescription is not always money — these must not become salary
        ("Be an early applicant", ""),
        ("Actively reviewing applicants", ""),
        ("", ""),
    ],
)
def test_salary_only_accepts_money(text, expected):
    assert _salary(text) == expected


# ----------------------------------------------------------------------
# Freshness


def test_search_url_encodes_the_window_and_sorts_by_date():
    url = LinkedInScraper()._search_url(
        SearchQuery(keywords="backend engineer", location="India", posted_within_hours=1)
    )
    assert "f_TPR=r3600" in url
    assert "sortBy=DD" in url


def test_search_url_converts_days_to_seconds():
    url = LinkedInScraper()._search_url(SearchQuery(keywords="x", posted_within_days=1))
    assert "f_TPR=r86400" in url


def test_search_url_omits_the_window_when_unset():
    url = LinkedInScraper()._search_url(SearchQuery(keywords="x"))
    assert "f_TPR" not in url and "sortBy" not in url


class FakePage:
    """A page that serves one fixed set of cards, then goes empty."""

    def __init__(self, cards):
        self.url = "https://www.linkedin.com/jobs/search/?f_TPR=r3600"
        self._cards = cards
        self._served = False
        self._handlers = []

    def on(self, _event, handler):
        self._handlers.append(handler)

    def remove_listener(self, _event, handler):
        self._handlers.remove(handler)

    def goto(self, url, **_kwargs):
        self.url = url

    def wait_for_timeout(self, _ms):
        pass

    def mouse(self):  # pragma: no cover - unused
        pass


@pytest.fixture()
def patched(monkeypatch):
    """Feed cards straight into search(), bypassing the browser."""

    def install(cards):
        monkeypatch.setattr("jobbot.scrapers.linkedin.scroll_and_settle", lambda *a, **k: False)
        monkeypatch.setattr("jobbot.scrapers.linkedin._job_cards", lambda _s: cards)
        monkeypatch.setattr(
            "jobbot.scrapers.linkedin.JsonSniffer",
            lambda page, patterns=(): type(
                "S", (), {"captured": [], "clear": lambda s: None, "detach": lambda s: None}
            )(),
        )
        return FakePage(cards)

    return install


def test_stale_jobs_are_dropped_from_a_one_hour_window(patched):
    """The bug this whole file exists for: a 1h view showing 15h-old jobs."""
    page = patched(
        [
            card(1111111, "Fresh Job", "Acme", listed_ms=ms_ago(minutes=20)),
            card(2222222, "Stale Job", "Beta", listed_ms=ms_ago(hours=15)),
        ]
    )
    query = SearchQuery(keywords="backend engineer", posted_within_hours=1)
    titles = [s.title for s in LinkedInScraper().search(page, query, limit=50)]
    assert titles == ["Fresh Job"]


def test_a_24h_window_keeps_a_15h_old_job(patched):
    page = patched(
        [
            card(1111111, "Fresh Job", "Acme", listed_ms=ms_ago(minutes=20)),
            card(2222222, "Older Job", "Beta", listed_ms=ms_ago(hours=15)),
        ]
    )
    query = SearchQuery(keywords="backend engineer", posted_within_hours=24)
    titles = [s.title for s in LinkedInScraper().search(page, query, limit=50)]
    assert titles == ["Fresh Job", "Older Job"]


def test_dateless_jobs_survive_a_freshness_window(patched):
    """An unknown date is not evidence of staleness — don't silently drop it."""
    page = patched([card(1111111, "No Date", "Acme")])
    query = SearchQuery(keywords="x", posted_within_hours=1)
    titles = [s.title for s in LinkedInScraper().search(page, query, limit=50)]
    assert titles == ["No Date"]


def test_no_window_keeps_everything(patched):
    page = patched(
        [
            card(1111111, "Fresh", "Acme", listed_ms=ms_ago(minutes=20)),
            card(2222222, "Ancient", "Beta", listed_ms=ms_ago(days=90)),
        ]
    )
    titles = [s.title for s in LinkedInScraper().search(page, SearchQuery(keywords="x"), limit=50)]
    assert titles == ["Fresh", "Ancient"]


def test_search_respects_the_limit(patched):
    page = patched([card(1000000 + i, f"Job {i}", "Acme") for i in range(30)])
    got = list(LinkedInScraper().search(page, SearchQuery(keywords="x"), limit=5))
    assert len(got) == 5
