"""Persistent browser sessions.

No credentials are ever stored or typed by the bot. You log in by hand once
(`jobbot login <site>`), and Playwright's persistent context keeps the cookies
in `data/browser/<site>/`. Every later run reuses that profile.

This is both safer — no password in a config file, 2FA works normally — and
much less detectable than a scripted login, which is the single most
fingerprinted action on any of these sites.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from ..config import DATA_DIR

# A stable, real-looking desktop UA. Kept constant per profile — a UA that
# changes between runs on the same cookie jar is a strong bot signal.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

VIEWPORT = {"width": 1440, "height": 900}

# Playwright sets navigator.webdriver=true and leaves a couple of other
# tells. This removes the cheapest ones. It is not a full stealth suite and
# does not pretend to be — the real defense is pacing, in pacing.py.
_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
]


def profile_dir(site: str) -> Path:
    path = DATA_DIR / "browser" / site
    path.mkdir(parents=True, exist_ok=True)
    return path


def has_profile(site: str) -> bool:
    """True if `jobbot login <site>` has been run at least once."""
    path = DATA_DIR / "browser" / site
    return path.is_dir() and any(path.iterdir())


@contextmanager
def browser_context(
    site: str,
    *,
    headless: bool = True,
    slow_mo: int = 0,
) -> Iterator[BrowserContext]:
    """Open the persistent context for `site`."""
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(site)),
            headless=headless,
            slow_mo=slow_mo,
            args=_LAUNCH_ARGS,
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="Asia/Kolkata",
            ignore_default_args=["--enable-automation"],
        )
        context.add_init_script(_INIT_SCRIPT)
        context.set_default_timeout(30_000)
        try:
            yield context
        finally:
            context.close()


@contextmanager
def browser_page(site: str, *, headless: bool = True, slow_mo: int = 0) -> Iterator[Page]:
    """Open the persistent context and hand back a single page.

    One page per site, used serially. Parallel tabs against the same site are
    a bot signal and are deliberately not supported.
    """
    with browser_context(site, headless=headless, slow_mo=slow_mo) as context:
        page = context.pages[0] if context.pages else context.new_page()
        yield page


def page_text(page: Page, *, limit: int = 20_000) -> str:
    """Visible text of the page, for block detection and logged-out checks."""
    try:
        text = page.inner_text("body", timeout=5_000)
    except Exception:
        try:
            text = page.content()
        except Exception:
            return ""
    return text[:limit]
