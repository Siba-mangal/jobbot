"""Greenhouse applier.

Greenhouse is one of the two ATSes worth automating: a single-page form,
stable field naming (`job_application[...]`), a real file input for the
resume, and an unambiguous confirmation page.

The one wrinkle is embedding — companies serve the board from their own
domain inside a `#grnhse_app` iframe. `_form_frame` handles that so the same
code path works either way.
"""

from __future__ import annotations

from playwright.sync_api import Page

from ...db import Job
from ..base import FormApplier


class GreenhouseApplier(FormApplier):
    name = "greenhouse"

    def open_form(self, page: Page, job: Job) -> None:
        page.goto(self.target_url(job), wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)

        # Some boards hide the form behind an "Apply for this job" toggle.
        for selector in (
            "a:has-text('Apply for this job')",
            "button:has-text('Apply for this job')",
            "a#apply_button",
        ):
            locator = page.locator(selector)
            if locator.count():
                try:
                    locator.first.click()
                    page.wait_for_timeout(2_000)
                except Exception:
                    pass
                break

        page.wait_for_selector("input, textarea, select", timeout=15_000)

    def form_root(self) -> str | None:
        return "#application_form, form#application-form, form"

    def resume_selectors(self) -> tuple[str, ...]:
        return (
            "input[type='file'][name*='resume' i]",
            "input#resume",
            "input[type='file']",
        )

    def submit_form(self, page: Page) -> bool:
        for selector in (
            "input#submit_app",
            "button#submit_app",
            "button:has-text('Submit application')",
            "input[type='submit']",
            "button[type='submit']",
        ):
            locator = page.locator(selector)
            if locator.count() and locator.first.is_enabled():
                locator.first.click()
                page.wait_for_timeout(5_000)
                return True
        return False

    def verify_submitted(self, page: Page) -> bool:
        if super().verify_submitted(page):
            return True
        # Greenhouse redirects to a confirmation route on success.
        return "confirmation" in page.url.lower() or "application_confirmation" in page.url.lower()
