"""Following an external apply link to find out what's behind it.

Discovery deliberately leaves external postings as UNKNOWN — clicking "Apply"
on LinkedIn registers application intent, and doing that for jobs you may
never apply to is both noisy and dishonest. So the link gets followed here,
at apply time, when you've actually approved the job.

The destination is usually an ATS. Greenhouse and Lever we can drive;
anything else is handed back for you to do by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from ..db import ApplyRoute, Job
from .ats.detect import detect_ats

_APPLY_SELECTORS = (
    "button.jobs-apply-button",
    "a.jobs-apply-button",
    ".jobs-s-apply button",
    "a:has-text('Apply on company website')",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
)


@dataclass
class ResolvedRoute:
    route: ApplyRoute
    ats_type: str
    url: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.route is not ApplyRoute.UNKNOWN


def resolve_route(page: Page, job: Job, *, timeout: int = 20_000) -> ResolvedRoute:
    """Work out where this job's application actually lives."""
    # Cheapest path: we already captured the external URL during discovery.
    if job.ats_url:
        route, name = detect_ats(job.ats_url)
        if route is not ApplyRoute.UNKNOWN:
            return ResolvedRoute(route, name, job.ats_url)

    target = job.ats_url or job.url
    try:
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)
    except Exception as exc:
        return ResolvedRoute(ApplyRoute.UNKNOWN, "", target, error=f"could not open {target}: {exc}")

    # The page we landed on may itself be the ATS (a direct careers link).
    route, name = detect_ats(page.url, _safe_content(page))
    if route is not ApplyRoute.UNKNOWN:
        return ResolvedRoute(route, name, page.url)

    # Otherwise follow the apply control. It either opens a popup or
    # navigates in place; handle both.
    before_url = page.url
    popup = None
    for selector in _APPLY_SELECTORS:
        locator = page.locator(selector)
        if not locator.count():
            continue
        try:
            with page.context.expect_page(timeout=timeout) as popup_info:
                locator.first.click()
            popup = popup_info.value
            break
        except Exception:
            # No popup — it may have navigated in place instead.
            page.wait_for_timeout(3_000)
            if page.url != before_url:
                break

    if popup is not None:
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=timeout)
            popup.wait_for_timeout(2_000)
            route, name = detect_ats(popup.url, _safe_content(popup))
            resolved_url = popup.url
        finally:
            try:
                popup.close()
            except Exception:
                pass
        return ResolvedRoute(route, name, resolved_url)

    if page.url != before_url:
        route, name = detect_ats(page.url, _safe_content(page))
        return ResolvedRoute(route, name, page.url)

    return ResolvedRoute(
        ApplyRoute.UNKNOWN, "", page.url, error="could not follow the apply link"
    )


def _safe_content(page: Page) -> str:
    try:
        return page.content()
    except Exception:
        return ""
