"""Instahyre record mapping, pinned to the live API shape.

The fixture below is a real `/api/v1/job_search` record (identifiers changed).
It exists because the first version of this scraper silently returned zero
jobs: `title` and `id` were read correctly, but the company was looked up at
`employer.name` when the live field is `employer.company_name`. Every record
failed the "no company" check and was dropped without an error.
"""

from __future__ import annotations

import pytest

from jobbot.config import SearchQuery
from jobbot.scrapers.instahyre import InstahyreScraper, _matches, _query_terms

# Verbatim shape from the live API, with the company/id changed.
LIVE_RECORD = {
    "accept_outstation": True,
    "candidate_title": "Software Developer",
    "employer": {
        "resource_uri": "/api/v1/candidate_opportunity_employer/2917",
        "profile_image_src": "https://media.instahyre.com/images/x.webp",
        "id": 2917,
        "company_name": "SmartCoin Financials",
        "company_tagline": "Credit for the next billion",
        "company_founded": 2016,
        "employee_count": 200,
    },
    "gender": 0,
    "id": 410045,
    "interview_status": 0,
    "is_active": True,
    "is_strong_match": False,
    "keywords": ["Java", "Spring Boot", "Data Structures"],
    "locations": "Bangalore,Pune",
    "public_url": "https://www.instahyre.com/job-410045-software-developer-at-smartcoin-bangalore-pune/",
    "resource_uri": "/api/v1/job_search/410045",
    "reviewed_at": None,
    "score": "1.763",
    "title": "Software Developer",
}


@pytest.fixture()
def scraper():
    return InstahyreScraper()


class TestRecordMapping:
    def test_maps_a_live_record(self, scraper):
        stub = scraper._stub_from_record(LIVE_RECORD)
        assert stub is not None
        assert stub.title == "Software Developer"
        assert stub.company == "SmartCoin Financials"
        assert stub.source_job_id == "410045"
        assert stub.url == LIVE_RECORD["public_url"]

    def test_company_comes_from_employer_company_name(self, scraper):
        """The exact field that broke discovery."""
        record = {**LIVE_RECORD, "employer": {"company_name": "Acme"}}
        assert scraper._stub_from_record(record).company == "Acme"

    def test_comma_separated_locations_are_readable(self, scraper):
        # "Bangalore,Pune" would otherwise fail a location prefilter looking
        # for ", " and read badly in the dashboard.
        assert scraper._stub_from_record(LIVE_RECORD).location == "Bangalore, Pune"

    def test_remote_is_detected_from_location(self, scraper):
        record = {**LIVE_RECORD, "locations": "Remote"}
        assert scraper._stub_from_record(record).remote is True

    def test_non_remote_is_not_flagged(self, scraper):
        assert scraper._stub_from_record(LIVE_RECORD).remote is False

    def test_missing_company_is_dropped(self, scraper):
        assert scraper._stub_from_record({**LIVE_RECORD, "employer": {}}) is None

    def test_missing_title_is_dropped(self, scraper):
        record = {k: v for k, v in LIVE_RECORD.items() if k != "title"}
        record.pop("candidate_title")
        assert scraper._stub_from_record(record) is None

    def test_missing_url_falls_back_to_a_constructed_one(self, scraper):
        record = {k: v for k, v in LIVE_RECORD.items() if k != "public_url"}
        assert "410045" in scraper._stub_from_record(record).url

    def test_fingerprint_is_stable(self, scraper):
        a = scraper._stub_from_record(LIVE_RECORD).fingerprint
        b = scraper._stub_from_record(dict(LIVE_RECORD)).fingerprint
        assert a == b


class TestApiUrl:
    def test_location_is_sent_as_joblocations(self, scraper):
        url = scraper._api_url(SearchQuery(keywords="backend", location="Bangalore"))
        assert "jobLocations=Bangalore" in url

    def test_keywords_are_not_sent(self, scraper):
        # The API ignores q/keywords/search — it returns a feed personalized to
        # your profile. Sending them would imply a filter that doesn't happen.
        url = scraper._api_url(SearchQuery(keywords="backend engineer"))
        for param in ("q=", "keywords=", "search="):
            assert param not in url

    def test_pagination_offset(self, scraper):
        assert "offset=40" in scraper._api_url(SearchQuery(keywords=""), offset=40)


class TestKeywordFiltering:
    """Because the feed is personalized rather than searched, keywords are
    applied on our side."""

    def test_generic_words_are_dropped(self):
        # Filtering on "engineer" would keep everything and filter nothing.
        assert _query_terms("backend engineer") == ["backend"]

    def test_an_all_generic_query_keeps_its_words(self):
        # Better to filter on something weak than on nothing at all.
        assert _query_terms("software engineer") == ["software", "engineer"]

    def test_empty_query_yields_no_terms(self):
        assert _query_terms("") == []

    def test_matches_on_title(self):
        stub = InstahyreScraper()._stub_from_record(
            {**LIVE_RECORD, "title": "Backend Developer"}
        )
        assert _matches({}, stub, ["backend"])

    def test_matches_on_record_tags(self):
        # Title says "Software Developer" but the tags say Java — a "java"
        # query should still find it.
        stub = InstahyreScraper()._stub_from_record(LIVE_RECORD)
        assert _matches(LIVE_RECORD, stub, ["java"])

    def test_rejects_an_unrelated_role(self):
        stub = InstahyreScraper()._stub_from_record(LIVE_RECORD)
        assert not _matches(LIVE_RECORD, stub, ["kubernetes"])

    def test_matching_is_case_insensitive(self):
        stub = InstahyreScraper()._stub_from_record(LIVE_RECORD)
        assert _matches(LIVE_RECORD, stub, ["java"])  # tag is "Java"
