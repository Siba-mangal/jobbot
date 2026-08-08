"""Lever applier.

Lever's forms are the cleanest of any ATS: `data-qa` attributes throughout,
consistent `name` values (`name`, `email`, `resume`, `urls[LinkedIn]`), and a
distinct `/thanks` confirmation URL that makes verification unambiguous.

Job pages and application pages are separate routes — `/jobs/<id>` vs
`/jobs/<id>/apply` — so navigation appends the suffix rather than hunting for
an Apply button.
"""

from __future__ import annotations

from playwright.sync_api import Page

from ...db import Job
from ..base import FormApplier


class LeverApplier(FormApplier):
    name = "lever"

    def target_url(self, job: Job) -> str:
        url = (job.ats_url or job.url).split("?")[0].rstrip("/")
        return url if url.endswith("/apply") else f"{url}/apply"

    def open_form(self, page: Page, job: Job) -> None:
        page.goto(self.target_url(job), wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)
        page.wait_for_selector("input, textarea, select", timeout=15_000)

    def form_root(self) -> str | None:
        return "form[data-qa='application-form'], .application-form, form"

    def resume_selectors(self) -> tuple[str, ...]:
        return (
            "input[name='resume']",
            "input[type='file'][name*='resume' i]",
            "input[type='file']",
        )

    def submit_form(self, page: Page) -> bool:
        for selector in (
            "button[data-qa='btn-submit']",
            "button.postings-btn[type='submit']",
            "button:has-text('Submit application')",
            "button[type='submit']",
        ):
            locator = page.locator(selector)
            if locator.count() and locator.first.is_enabled():
                locator.first.click()
                page.wait_for_timeout(5_000)
                return True
        return False

    def verify_submitted(self, page: Page) -> bool:
        # Lever redirects to /thanks — the most reliable signal of any ATS.
        if "/thanks" in page.url.lower():
            return True
        return super().verify_submitted(page)
