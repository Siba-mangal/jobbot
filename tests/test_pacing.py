"""Pacing is the safety layer — these tests guard the rails that keep the
tool from getting an account restricted."""

from datetime import timedelta

import pytest

from jobbot.browser import pacing
from jobbot.db import SiteState, utcnow


class TestDailyCap:
    def test_fresh_site_has_full_budget(self, session):
        assert pacing.remaining_budget(session, "instahyre", 50) == 50

    def test_consuming_reduces_budget(self, session):
        pacing.consume_view(session, "instahyre", 50, count=10)
        assert pacing.remaining_budget(session, "instahyre", 50) == 40

    def test_cap_raises_before_going_over(self, session):
        pacing.consume_view(session, "instahyre", 5, count=5)
        # The 6th must be refused, not allowed-then-counted.
        with pytest.raises(pacing.DailyCapReached):
            pacing.consume_view(session, "instahyre", 5)
        assert session.get(SiteState, "instahyre").views_today == 5

    def test_budget_resets_on_a_new_day(self, session):
        pacing.consume_view(session, "instahyre", 50, count=50)
        assert pacing.remaining_budget(session, "instahyre", 50) == 0
        session.get(SiteState, "instahyre").views_date = "2020-01-01"
        assert pacing.remaining_budget(session, "instahyre", 50) == 50

    def test_caps_are_tracked_per_site(self, session):
        pacing.consume_view(session, "instahyre", 50, count=20)
        assert pacing.remaining_budget(session, "linkedin", 40) == 40


class TestCircuitBreaker:
    def test_fresh_site_is_available(self, session):
        pacing.assert_available(session, "instahyre")  # no raise

    def test_tripping_blocks_the_site(self, session):
        pacing.trip_breaker(session, "instahyre", "captcha")
        with pytest.raises(pacing.SitePaused, match="captcha"):
            pacing.assert_available(session, "instahyre")

    def test_failures_trip_only_at_threshold(self, session):
        pacing.record_failure(session, "instahyre", "timeout", threshold=3)
        pacing.record_failure(session, "instahyre", "timeout", threshold=3)
        pacing.assert_available(session, "instahyre")  # still fine at 2

        pacing.record_failure(session, "instahyre", "timeout", threshold=3)
        with pytest.raises(pacing.SitePaused):
            pacing.assert_available(session, "instahyre")

    def test_success_resets_the_failure_count(self, session):
        pacing.record_failure(session, "instahyre", "timeout", threshold=3)
        pacing.record_failure(session, "instahyre", "timeout", threshold=3)
        pacing.record_success(session, "instahyre")
        pacing.record_failure(session, "instahyre", "timeout", threshold=3)
        pacing.assert_available(session, "instahyre")  # count restarted

    def test_expired_pause_auto_clears(self, session):
        pacing.trip_breaker(session, "instahyre", "captcha")
        state = session.get(SiteState, "instahyre")
        state.paused_until = utcnow() - timedelta(minutes=1)
        pacing.assert_available(session, "instahyre")  # no raise
        assert state.paused_until is None
        assert state.consecutive_failures == 0

    def test_manual_clear(self, session):
        pacing.trip_breaker(session, "instahyre", "captcha")
        pacing.clear_breaker(session, "instahyre")
        pacing.assert_available(session, "instahyre")


class TestBlockDetection:
    """False negatives risk the account; false positives just cost a day of
    scraping. Both directions are tested."""

    def test_clean_page_is_not_a_block(self):
        assert pacing.block_signal("https://instahyre.com/job/1/", "Backend Engineer at Acme") is None

    def test_captcha_text_is_a_block(self):
        assert pacing.block_signal("https://x.com/", "Please complete the captcha") is not None

    def test_unusual_activity_is_a_block(self):
        assert pacing.block_signal("https://x.com/", "We detected unusual activity") is not None

    def test_checkpoint_url_is_a_block(self):
        assert pacing.block_signal("https://linkedin.com/checkpoint/challenge", "") is not None

    def test_429_and_403_are_blocks(self):
        assert pacing.block_signal("https://x.com/", "", status=429) is not None
        assert pacing.block_signal("https://x.com/", "", status=403) is not None

    def test_200_is_not_a_block(self):
        assert pacing.block_signal("https://x.com/", "Backend Engineer", status=200) is None

    def test_keyword_deep_in_a_long_jd_does_not_trip(self):
        # A JD for a security role legitimately mentions captcha. Only the head
        # of the page is inspected, so this must not pause the whole site.
        jd = "Backend Engineer at Acme.\n" + ("Build services. " * 500) + "Experience with captcha systems."
        assert pacing.block_signal("https://instahyre.com/job/1/", jd) is None


class TestLoggedOutDetection:
    def test_login_url_means_logged_out(self):
        assert pacing.looks_logged_out("https://instahyre.com/login/", "")

    def test_sign_in_prompt_means_logged_out(self):
        assert pacing.looks_logged_out("https://instahyre.com/", "Sign in to continue")

    def test_normal_page_is_not_logged_out(self):
        assert not pacing.looks_logged_out(
            "https://instahyre.com/candidate/opportunities/",
            "Backend Engineer at Acme Corp. Apply now.",
        )
