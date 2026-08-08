from jobbot.config import PrefilterConfig
from jobbot.scrapers.base import JobStub
from jobbot.scrapers.prefilter import (
    description_rejection,
    min_years_required,
    stub_rejection,
)


def stub(**kwargs) -> JobStub:
    base = {
        "source": "instahyre",
        "source_job_id": "1",
        "url": "https://x/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Bangalore",
    }
    return JobStub(**{**base, **kwargs})


class TestStubFilter:
    def test_keeps_a_matching_job(self):
        cfg = PrefilterConfig(allow_locations=["bangalore"])
        assert stub_rejection(stub(), cfg) is None

    def test_rejects_excluded_title_keyword(self):
        cfg = PrefilterConfig(exclude_title_keywords=["intern"])
        assert stub_rejection(stub(title="Backend Intern"), cfg) is not None

    def test_title_keyword_match_is_case_insensitive(self):
        cfg = PrefilterConfig(exclude_title_keywords=["INTERN"])
        assert stub_rejection(stub(title="backend intern"), cfg) is not None

    def test_rejects_excluded_company(self):
        cfg = PrefilterConfig(exclude_companies=["Acme"])
        assert stub_rejection(stub(company="acme"), cfg) is not None

    def test_partial_company_name_does_not_reject(self):
        # Excluding "Acme" must not also drop "Acme Health Systems" — company
        # exclusion is an exact match, deliberately.
        cfg = PrefilterConfig(exclude_companies=["Acme"])
        assert stub_rejection(stub(company="Acme Health Systems"), cfg) is None

    def test_rejects_location_outside_allow_list(self):
        cfg = PrefilterConfig(allow_locations=["bangalore"])
        assert stub_rejection(stub(location="Pune"), cfg) is not None

    def test_remote_job_bypasses_location_filter(self):
        cfg = PrefilterConfig(allow_locations=["bangalore"])
        assert stub_rejection(stub(location="Anywhere", remote=True), cfg) is None

    def test_empty_allow_list_accepts_any_location(self):
        assert stub_rejection(stub(location="Reykjavik"), PrefilterConfig()) is None


class TestYearsParsing:
    def test_plain_requirement(self):
        assert min_years_required("We need 5+ years of experience in Python") == 5

    def test_range_takes_the_lower_bound(self):
        assert min_years_required("3-5 years experience required") == 3

    def test_takes_the_minimum_across_multiple_mentions(self):
        # "3+ required, 8+ preferred" has a real bar of 3. Rejecting on the 8
        # would throw away a job you're qualified for.
        jd = "Requires 3+ years of experience. 8+ years experience preferred."
        assert min_years_required(jd) == 3

    def test_no_requirement_returns_none(self):
        assert min_years_required("We are hiring a backend engineer.") is None

    def test_implausible_numbers_ignored(self):
        assert min_years_required("Founded 1998 years experience") is None

    def test_abbreviated_form(self):
        assert min_years_required("Minimum 4 yrs experience") == 4


class TestDescriptionFilter:
    def test_rejects_over_the_cap(self):
        cfg = PrefilterConfig(max_years_required=8)
        assert description_rejection("12+ years of experience required", cfg) is not None

    def test_keeps_within_the_cap(self):
        cfg = PrefilterConfig(max_years_required=8)
        assert description_rejection("5+ years of experience required", cfg) is None

    def test_no_cap_configured_accepts_everything(self):
        cfg = PrefilterConfig(max_years_required=None)
        assert description_rejection("20+ years of experience required", cfg) is None
