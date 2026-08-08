"""Pacing and safety rails.

Every job board here prohibits automation in its terms. This module is what
keeps the tool from behaving like an obvious bot: randomized delays, hard
daily caps, and a circuit breaker that stops a site cold the moment it shows
any sign of having noticed.

The rule of thumb encoded here: it is always better to scrape 40 jobs a day
for a year than 4,000 jobs once and lose the account.
"""

from __future__ import annotations

import random
import re
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ..db import SiteState, utcnow

# Randomized delay bounds (seconds) between meaningful actions.
ACTION_DELAY = (3.0, 9.0)
# Shorter pause for trivial in-page interactions (scroll, hover).
MICRO_DELAY = (0.4, 1.6)
# How long a tripped circuit breaker keeps a site offline.
PAUSE_DURATION = timedelta(hours=24)


class SitePaused(RuntimeError):
    """The circuit breaker is open for this site."""


class DailyCapReached(RuntimeError):
    """Today's view budget for this site is spent."""


class BlockDetected(RuntimeError):
    """The site served a captcha / rate-limit / 'unusual activity' page."""


# --------------------------------------------------------------------------
# Delays
# --------------------------------------------------------------------------


def pause(bounds: tuple[float, float] = ACTION_DELAY) -> None:
    """Sleep a randomized interval. Uniform-random, not fixed — a constant
    delay is itself a fingerprint."""
    time.sleep(random.uniform(*bounds))


def micro_pause() -> None:
    pause(MICRO_DELAY)


# --------------------------------------------------------------------------
# Per-site state
# --------------------------------------------------------------------------


def _get_state(session: Session, site: str) -> SiteState:
    state = session.get(SiteState, site)
    if state is None:
        state = SiteState(site=site)
        session.add(state)
        session.flush()
    return state


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def assert_available(session: Session, site: str) -> None:
    """Raise if the site is paused by the circuit breaker."""
    state = _get_state(session, site)
    if state.paused_until is None:
        return
    paused_until = state.paused_until
    if paused_until.tzinfo is None:  # SQLite round-trips naive datetimes
        paused_until = paused_until.replace(tzinfo=UTC)
    if paused_until > utcnow():
        raise SitePaused(
            f"{site} is paused until {paused_until:%Y-%m-%d %H:%M UTC} "
            f"({state.pause_reason}). Clear it with: jobbot resume-site {site}"
        )
    # Pause expired — reset.
    state.paused_until = None
    state.pause_reason = ""
    state.consecutive_failures = 0


def remaining_budget(session: Session, site: str, daily_cap: int) -> int:
    """How many more detail-page views this site has left today."""
    state = _get_state(session, site)
    if state.views_date != _today():
        return daily_cap
    return max(0, daily_cap - state.views_today)


def consume_view(session: Session, site: str, daily_cap: int, count: int = 1) -> None:
    """Record detail-page views against today's budget.

    Raises DailyCapReached *before* consuming if the budget is spent, so the
    caller stops rather than going one over.
    """
    state = _get_state(session, site)
    if state.views_date != _today():
        state.views_date = _today()
        state.views_today = 0
    if state.views_today + count > daily_cap:
        raise DailyCapReached(
            f"{site} daily cap reached ({state.views_today}/{daily_cap}). "
            f"Raise `sites.{site}.daily_cap` in config/search.yaml, or come back tomorrow."
        )
    state.views_today += count


def record_success(session: Session, site: str) -> None:
    _get_state(session, site).consecutive_failures = 0


def record_failure(session: Session, site: str, reason: str, threshold: int = 3) -> None:
    """Count a failure; trip the breaker at the threshold."""
    state = _get_state(session, site)
    state.consecutive_failures += 1
    if state.consecutive_failures >= threshold:
        trip_breaker(session, site, f"{state.consecutive_failures} consecutive failures: {reason}")


def trip_breaker(session: Session, site: str, reason: str) -> None:
    """Take a site offline for PAUSE_DURATION. Called on any detection signal."""
    state = _get_state(session, site)
    state.paused_until = utcnow() + PAUSE_DURATION
    state.pause_reason = reason


def clear_breaker(session: Session, site: str) -> None:
    state = _get_state(session, site)
    state.paused_until = None
    state.pause_reason = ""
    state.consecutive_failures = 0


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

# Phrases that mean "we noticed you". Matched against page text, lowercased.
_BLOCK_PHRASES = (
    "unusual activity",
    "verify you are human",
    "verify you're human",
    "are you a robot",
    "complete the security check",
    "captcha",
    "too many requests",
    "rate limit",
    "access to this page has been denied",
    "your account has been restricted",
    "temporarily restricted",
    "suspicious activity",
    "please slow down",
)

_BLOCK_URL_MARKERS = ("/checkpoint", "/challenge", "/captcha", "/authwall")


def block_signal(url: str, page_text: str, status: int | None = None) -> str | None:
    """Return a reason string if this page looks like a block, else None.

    Kept as a pure function so it can be unit-tested against saved fixtures
    without a live browser.
    """
    if status is not None and status in (403, 429):
        return f"HTTP {status}"

    lowered_url = (url or "").lower()
    for marker in _BLOCK_URL_MARKERS:
        if marker in lowered_url:
            return f"redirected to {marker}"

    lowered = (page_text or "").lower()
    # Only inspect the head of the page — a long JD legitimately containing the
    # word "captcha" shouldn't trip the breaker.
    head = lowered[:4000]
    for phrase in _BLOCK_PHRASES:
        if phrase in head:
            return f"page says {phrase!r}"

    return None


_LOGIN_MARKERS = re.compile(
    r"(sign in|log in|login|create account|continue with google)", re.IGNORECASE
)


def looks_logged_out(url: str, page_text: str) -> bool:
    """Heuristic: did we get bounced to a login wall?"""
    lowered_url = (url or "").lower()
    if any(m in lowered_url for m in ("/login", "/signin", "/sign-in", "/authwall")):
        return True
    head = (page_text or "")[:1500]
    return bool(_LOGIN_MARKERS.search(head)) and "sign out" not in head.lower()
