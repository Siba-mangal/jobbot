"""ATS detection tests.

The asymmetry that matters: mistaking Workday for Greenhouse produces a
half-filled multi-page wizard on a real application. Failing to recognise
Greenhouse just means you apply by hand. So every rule demands a positive
signal and anything unrecognized falls through to manual.
"""

import pytest

from jobbot.appliers.ats import detect_ats, detect_from_dom, detect_from_url
from jobbot.db import ApplyRoute


class TestUrlDetection:
    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/acme/jobs/12345",
            "https://job-boards.greenhouse.io/acme/jobs/12345",
            "https://acme.greenhouse.io/careers",
        ],
    )
    def test_greenhouse(self, url):
        route, name = detect_from_url(url)
        assert route is ApplyRoute.ATS_GREENHOUSE
        assert name == "greenhouse"

    @pytest.mark.parametrize(
        "url", ["https://jobs.lever.co/acme/abc-123", "https://acme.lever.co/apply"]
    )
    def test_lever(self, url):
        route, name = detect_from_url(url)
        assert route is ApplyRoute.ATS_LEVER
        assert name == "lever"

    @pytest.mark.parametrize(
        ("url", "expected_name"),
        [
            ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/123", "workday"),
            ("https://acme.taleo.net/careersection/jobdetail.ftl", "taleo"),
            ("https://careers-acme.icims.com/jobs/1234/login", "icims"),
            ("https://jobs.smartrecruiters.com/Acme/12345", "smartrecruiters"),
            ("https://jobs.ashbyhq.com/acme/abc", "ashby"),
            ("https://apply.workable.com/acme/j/ABC123/", "workable"),
        ],
    )
    def test_known_but_unautomated_portals_go_manual(self, url, expected_name):
        route, name = detect_from_url(url)
        assert route is ApplyRoute.ATS_OTHER
        assert name == expected_name
        assert not route.is_automated

    def test_unknown_host_is_unknown(self):
        route, _ = detect_from_url("https://careers.acme.com/apply/123")
        assert route is ApplyRoute.UNKNOWN

    def test_empty_url_is_unknown(self):
        assert detect_from_url("")[0] is ApplyRoute.UNKNOWN

    def test_case_insensitive(self):
        assert detect_from_url("HTTPS://BOARDS.GREENHOUSE.IO/x")[0] is ApplyRoute.ATS_GREENHOUSE


class TestDomDetection:
    def test_embedded_greenhouse_iframe(self):
        # Companies proxy Greenhouse behind their own domain; the URL says
        # nothing but the embed div is unmistakable.
        html = "<html><body><div id='grnhse_app'></div></body></html>"
        route, name = detect_from_dom(html)
        assert route is ApplyRoute.ATS_GREENHOUSE
        assert name == "greenhouse"

    def test_lever_data_qa_attribute(self):
        html = "<form data-qa=\"application-form\"><input name='name'></form>"
        assert detect_from_dom(html)[0] is ApplyRoute.ATS_LEVER

    def test_workday_markup(self):
        assert detect_from_dom("<div class='workday-app'>")[0] is ApplyRoute.ATS_OTHER

    def test_plain_page_is_unknown(self):
        assert detect_from_dom("<html><body><h1>Careers</h1></body></html>")[0] is ApplyRoute.UNKNOWN

    def test_empty_html_is_unknown(self):
        assert detect_from_dom("")[0] is ApplyRoute.UNKNOWN

    def test_footer_link_deep_in_a_page_does_not_match(self):
        # A careers page that merely links to a Greenhouse board somewhere far
        # down must not be mistaken for one.
        html = "<html><body>" + ("<p>filler</p>" * 8000) + "<a href='boards.greenhouse.io'>x</a>"
        assert detect_from_dom(html)[0] is ApplyRoute.UNKNOWN


class TestCombined:
    def test_url_wins_when_decisive(self):
        route, _ = detect_ats("https://jobs.lever.co/acme/1", "<div id='grnhse_app'>")
        assert route is ApplyRoute.ATS_LEVER

    def test_falls_back_to_dom(self):
        route, name = detect_ats("https://careers.acme.com/apply", "<div id='grnhse_app'></div>")
        assert route is ApplyRoute.ATS_GREENHOUSE
        assert name == "greenhouse"

    def test_neither_signal_is_unknown(self):
        assert detect_ats("https://careers.acme.com", "<h1>Jobs</h1>")[0] is ApplyRoute.UNKNOWN


class TestRouteSemantics:
    def test_automated_routes(self):
        assert ApplyRoute.ATS_GREENHOUSE.is_automated
        assert ApplyRoute.ATS_LEVER.is_automated
        assert ApplyRoute.BOARD_NATIVE.is_automated

    def test_unknown_is_resolved_later_not_sent_to_manual(self):
        # An external LinkedIn link is often Greenhouse. Diverting it on sight
        # would hand you work the bot can do.
        assert ApplyRoute.UNKNOWN.needs_resolution
        assert not ApplyRoute.UNKNOWN.send_to_manual

    def test_recognized_unautomated_portal_goes_to_manual(self):
        assert ApplyRoute.ATS_OTHER.send_to_manual
        assert not ApplyRoute.ATS_OTHER.needs_resolution

    def test_automated_routes_never_go_to_manual(self):
        for route in (ApplyRoute.ATS_GREENHOUSE, ApplyRoute.ATS_LEVER, ApplyRoute.BOARD_NATIVE):
            assert not route.send_to_manual
