"""Resilient DOM reading.

Every helper here takes a *list* of candidate selectors rather than one.
Job boards rewrite their markup constantly, and a scraper that depends on a
single class name breaks on the next deploy; one that tries four survives
most of them.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page


def first_text(scope: Locator | Page, selectors: tuple[str, ...], *, timeout: int = 2_000) -> str:
    """Text of the first selector that matches and is non-empty."""
    for selector in selectors:
        locator = scope.locator(selector)
        if not locator.count():
            continue
        try:
            text = locator.first.inner_text(timeout=timeout).strip()
        except Exception:
            continue
        if text:
            return text
    return ""


def longest_text(
    scope: Locator | Page, selectors: tuple[str, ...], *, min_len: int = 200, timeout: int = 5_000
) -> str:
    """Text of the first selector yielding a substantial block.

    Used for job descriptions, where a too-specific selector often matches a
    truncated teaser and the right one matches the full posting.
    """
    best = ""
    for selector in selectors:
        locator = scope.locator(selector)
        if not locator.count():
            continue
        try:
            text = locator.first.inner_text(timeout=timeout).strip()
        except Exception:
            continue
        if len(text) > len(best):
            best = text
        if len(best) >= min_len:
            break
    return best


def first_attr(scope: Locator | Page, selectors: tuple[str, ...], attr: str) -> str:
    for selector in selectors:
        locator = scope.locator(selector)
        if not locator.count():
            continue
        try:
            value = locator.first.get_attribute(attr)
        except Exception:
            continue
        if value:
            return value
    return ""


def first_href(scope: Locator | Page, base: str = "", selectors: tuple[str, ...] = ("a[href]",)) -> str:
    href = first_attr(scope, selectors, "href")
    if href.startswith("/") and base:
        return base.rstrip("/") + href
    return href


def find_cards(page: Page, selectors: tuple[str, ...], *, min_count: int = 1) -> Locator | None:
    """First selector matching at least `min_count` elements."""
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() >= min_count:
            return locator
    return None


def scroll_and_settle(page: Page, *, pixels: int = 3_000, wait_ms: int = 2_000) -> bool:
    """Scroll down to trigger lazy loading. False if the page didn't grow."""
    before = page.evaluate("document.body.scrollHeight")
    page.mouse.wheel(0, pixels)
    page.wait_for_timeout(wait_ms)
    return page.evaluate("document.body.scrollHeight") > before


def click_if_present(page: Page, labels: tuple[str, ...], *, wait_ms: int = 2_000) -> bool:
    """Click the first enabled button matching any label. False if none."""
    for label in labels:
        button = page.get_by_role("button", name=re.compile(label, re.I))
        if not button.count():
            continue
        try:
            if button.first.is_enabled():
                button.first.click()
                page.wait_for_timeout(wait_ms)
                return True
        except Exception:
            continue
    return False


_DIGITS = re.compile(r"(\d{4,})")


def id_from_url(url: str, patterns: tuple[str, ...]) -> str:
    """Extract a job id from a URL using the first matching pattern."""
    for pattern in patterns:
        match = re.search(pattern, url or "")
        if match:
            return match.group(1)
    match = _DIGITS.search(url or "")
    return match.group(1) if match else ""


def slugify_id(*parts: str) -> str:
    """Deterministic fallback id when the URL carries none."""
    joined = "-".join(p.strip().lower() for p in parts if p)
    return re.sub(r"[^a-z0-9]+", "-", joined).strip("-")[:120]
