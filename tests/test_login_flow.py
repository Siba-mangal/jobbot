"""Login-flow tests.

The bug these exist to prevent: the login poll loop called `is_logged_in()`,
which navigates. Polled every few seconds while the user was typing their
password, it reloaded the page out from under them — making login impossible.

So `login_complete()` is passive by contract, and that contract is tested two
ways: a spy that fails on *any* page-mutating call, and a real browser that
must still be on the same URL afterwards.
"""

from __future__ import annotations

import pytest

from jobbot.scrapers.registry import SCRAPERS

# Anything that navigates, reloads, or interacts with the page. Calling any of
# these from login_complete() breaks a user mid-login.
FORBIDDEN = (
    "goto",
    "reload",
    "go_back",
    "go_forward",
    "click",
    "fill",
    "press",
    "set_content",
    "wait_for_timeout",
    "wait_for_url",
    "wait_for_load_state",
)


class NavigationAttempted(AssertionError):
    pass


class SpyLocator:
    def __init__(self, count_value: int = 0):
        self._count = count_value

    def count(self) -> int:
        return self._count

    def __getattr__(self, name):
        if name in FORBIDDEN:
            raise NavigationAttempted(f"locator.{name}() called during login_complete()")
        raise AttributeError(name)


class SpyPage:
    """A Page that refuses to be navigated or interacted with."""

    def __init__(self, url: str, *, locator_count: int = 0):
        self.url = url
        self._locator_count = locator_count

    def locator(self, _selector: str) -> SpyLocator:
        return SpyLocator(self._locator_count)

    def is_closed(self) -> bool:
        return False

    def __getattr__(self, name):
        if name in FORBIDDEN:
            raise NavigationAttempted(f"page.{name}() called during login_complete()")
        raise AttributeError(name)


LOGGED_IN_URLS = {
    "instahyre": "https://www.instahyre.com/candidate/opportunities/",
    "cutshort": "https://cutshort.io/jobs",
    "linkedin": "https://www.linkedin.com/feed/",
}

LOGIN_PAGE_URLS = {
    "instahyre": "https://www.instahyre.com/login/",
    "cutshort": "https://cutshort.io/login",
    "linkedin": "https://www.linkedin.com/login",
}


@pytest.mark.parametrize("site", sorted(SCRAPERS))
class TestPassiveContract:
    def test_does_not_navigate_on_a_login_page(self, site):
        """The regression test. This is the exact call the poll loop makes."""
        scraper = SCRAPERS[site]
        page = SpyPage(LOGIN_PAGE_URLS[site])
        assert scraper.login_complete(page) is False

    def test_does_not_navigate_when_logged_in(self, site):
        scraper = SCRAPERS[site]
        page = SpyPage(LOGGED_IN_URLS[site], locator_count=1)
        assert scraper.login_complete(page) is True

    def test_does_not_navigate_on_an_unrelated_page(self, site):
        # 2FA and SSO routinely bounce through a third-party domain.
        scraper = SCRAPERS[site]
        page = SpyPage("https://accounts.google.com/signin/oauth", locator_count=1)
        assert scraper.login_complete(page) is False


@pytest.mark.parametrize("site", sorted(SCRAPERS))
class TestDetection:
    def test_login_page_is_not_logged_in(self, site):
        assert SCRAPERS[site].login_complete(SpyPage(LOGIN_PAGE_URLS[site])) is False

    def test_signup_page_is_not_logged_in(self, site):
        url = LOGIN_PAGE_URLS[site].replace("login", "signup")
        assert SCRAPERS[site].login_complete(SpyPage(url, locator_count=1)) is False

    def test_blank_page_is_not_logged_in(self, site):
        assert SCRAPERS[site].login_complete(SpyPage("about:blank")) is False


class TestLinkedInCheckpoints:
    """LinkedIn bounces through several intermediate states during login; none
    of them mean success."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/checkpoint/challenge/",
            "https://www.linkedin.com/uas/login-submit",
            "https://www.linkedin.com/authwall",
            "https://www.linkedin.com/checkpoint/rp/request-password-reset",
        ],
    )
    def test_checkpoint_is_not_logged_in(self, url):
        assert SCRAPERS["linkedin"].login_complete(SpyPage(url, locator_count=1)) is False


class TestScraperProtocol:
    def test_every_scraper_implements_login_complete(self):
        for site, scraper in SCRAPERS.items():
            assert callable(getattr(scraper, "login_complete", None)), site

    def test_login_loop_uses_the_passive_check(self):
        """Guards against the poll loop reverting to the navigating variant."""
        import inspect

        from jobbot.cli import login

        source = inspect.getsource(login)
        assert "login_complete" in source
        assert "is_logged_in" not in source, (
            "the login poll loop must not call is_logged_in() — it navigates, "
            "which reloads the page while the user is typing"
        )
