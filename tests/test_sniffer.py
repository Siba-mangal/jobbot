"""The sniffer is what makes scrapers survive markup churn — it locates job
records by shape rather than by hardcoded endpoint paths."""

from jobbot.scrapers.sniffer import Captured, JsonSniffer, _shape, get_path


def sniffer_with(*payloads) -> JsonSniffer:
    """Build a sniffer without a live Page."""
    obj = JsonSniffer.__new__(JsonSniffer)
    obj.captured = [
        Captured(url=f"https://x/api/{i}", status=200, payload=p)
        for i, p in enumerate(payloads)
    ]
    return obj


class TestFindRecords:
    def test_finds_a_top_level_list(self):
        payload = [{"title": "Backend Engineer", "company": "Acme"}]
        found = sniffer_with(payload).find_records(required_keys=("title",))
        assert len(found) == 1
        assert found[0]["title"] == "Backend Engineer"

    def test_finds_a_nested_list(self):
        # Django REST pagination wraps results — the common real-world shape.
        payload = {"count": 2, "next": None, "objects": [
            {"title": "Backend Engineer"},
            {"title": "Data Engineer"},
        ]}
        found = sniffer_with(payload).find_records(required_keys=("title",))
        assert len(found) == 2

    def test_finds_a_deeply_nested_list(self):
        payload = {"data": {"search": {"jobs": {"edges": [
            {"designation": "SRE"}, {"designation": "SWE"},
        ]}}}}
        found = sniffer_with(payload).find_records(required_keys=("designation",))
        assert len(found) == 2

    def test_key_matching_is_case_insensitive(self):
        payload = [{"Title": "Backend Engineer"}]
        assert sniffer_with(payload).find_records(required_keys=("title",))

    def test_ignores_lists_without_the_marker_keys(self):
        payload = {"filters": [{"label": "Remote"}, {"label": "Onsite"}]}
        assert sniffer_with(payload).find_records(required_keys=("title",)) == []

    def test_prefers_the_longest_matching_list(self):
        # A page often fetches both "recommended" (3) and "results" (25).
        # We want the results list.
        short = {"recommended": [{"title": f"r{i}"} for i in range(3)]}
        long = {"results": [{"title": f"j{i}"} for i in range(25)]}
        found = sniffer_with(short, long).find_records(required_keys=("title",))
        assert len(found) == 25

    def test_no_captures_yields_nothing(self):
        assert sniffer_with().find_records(required_keys=("title",)) == []


class TestGetPath:
    def test_first_present_key_wins(self):
        assert get_path({"designation": "SRE"}, "title", "designation") == "SRE"

    def test_falls_through_empty_values(self):
        assert get_path({"title": "", "designation": "SRE"}, "title", "designation") == "SRE"

    def test_dotted_path(self):
        assert get_path({"company": {"name": "Acme"}}, "company.name") == "Acme"

    def test_case_insensitive_at_each_level(self):
        assert get_path({"Company": {"Name": "Acme"}}, "company.name") == "Acme"

    def test_missing_returns_default(self):
        assert get_path({}, "title", default="unknown") == "unknown"

    def test_broken_dotted_path_returns_default(self):
        assert get_path({"company": "Acme"}, "company.name", default="") == ""

    def test_zero_is_not_treated_as_missing(self):
        # An id of 0 is a legitimate value; only None/""/[]/{} count as empty.
        assert get_path({"id": 0}, "id", default="missing") == 0


class TestShape:
    def test_describes_a_dict(self):
        assert "title" in _shape({"title": "x", "company": "y"})

    def test_describes_a_list(self):
        assert "list[2]" in _shape([{"a": 1}, {"a": 2}])

    def test_handles_empty_list(self):
        assert _shape([]) == "list(empty)"
