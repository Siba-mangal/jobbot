"""Appliers for the job boards themselves.

LinkedIn Easy Apply is a wizard in a modal; Instahyre and Cutshort are close
to one-click since your profile is already on file.
"""

from __future__ import annotations

from playwright.sync_api import Page

from ..db import Job
from .base import FormApplier

# ----------------------------------------------------------------------
# LinkedIn Easy Apply
# ----------------------------------------------------------------------

_EASY_APPLY_BUTTONS = (
    "button.jobs-apply-button",
    "button:has-text('Easy Apply')",
    ".jobs-s-apply button",
)
_MODAL = ".jobs-easy-apply-modal, [role='dialog'][aria-labelledby*='easy-apply']"


class LinkedInEasyApplyApplier(FormApplier):
    """LinkedIn's in-modal wizard.

    Steps are Contact info → Resume → Screening questions → Review → Submit.
    The step count varies by posting, hence the generous `max_steps`.
    """

    name = "linkedin_easy_apply"
    max_steps = 8

    def open_form(self, page: Page, job: Job) -> None:
        # Safe to click here: on LinkedIn "Easy Apply" opens the modal wizard,
        # it does not send anything. Submission is a separate "Submit
        # application" button at the end, handled in submit_form. The button
        # text is checked before clicking so an external "Apply" — which would
        # leave the site — is never pressed.
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)

        for selector in _EASY_APPLY_BUTTONS:
            locator = page.locator(selector)
            if not locator.count():
                continue
            text = (locator.first.inner_text() or "").lower()
            if "easy apply" not in text:
                continue
            locator.first.click()
            page.wait_for_timeout(3_000)
            break
        else:
            raise RuntimeError("no Easy Apply button on this posting")

        page.wait_for_selector(_MODAL, timeout=15_000)

    def form_root(self) -> str | None:
        return _MODAL

    def advance(self, page: Page) -> bool:
        """Click Next/Continue/Review. Never the final Submit."""
        for label in ("Continue to next step", "Next", "Review your application", "Review"):
            button = page.locator(f"{_MODAL} button:has-text('{label}')")
            if not button.count():
                continue
            try:
                if button.first.is_enabled():
                    button.first.click()
                    page.wait_for_timeout(2_500)
                    return True
            except Exception:
                continue
        return False

    def submit_form(self, page: Page) -> bool:
        # Don't opt into following the company by default — that's a side
        # effect on your profile that nobody asked for.
        follow = page.locator(f"{_MODAL} input#follow-company-checkbox")
        try:
            if follow.count() and follow.first.is_checked():
                follow.first.uncheck()
        except Exception:
            pass

        button = page.locator(f"{_MODAL} button:has-text('Submit application')")
        if button.count() and button.first.is_enabled():
            button.first.click()
            page.wait_for_timeout(4_000)
            return True
        return False

    def verify_submitted(self, page: Page) -> bool:
        try:
            text = page.inner_text("body", timeout=5_000).lower()
        except Exception:
            return False
        return any(
            marker in text
            for marker in (
                "your application was sent",
                "application sent",
                "premium",  # the post-apply upsell modal
                "applied",
            )
        ) and page.locator(_MODAL).count() == 0


# ----------------------------------------------------------------------
# Instahyre
# ----------------------------------------------------------------------


class InstahyreApplier(FormApplier):
    """Instahyre keeps your profile on file — there is no form, and the
    "Apply" button sends the application immediately.

    That makes the Apply click a *submission*, not navigation. It happens in
    `submit_form` only, so a dry run never touches it. An earlier version
    clicked it in `open_form` to "reach the form", which sent real
    applications during dry runs — see `allows_empty_form` in base.py.
    """

    name = "instahyre"
    allows_empty_form = True

    _APPLY = (
        "button:has-text('Apply')",
        "a:has-text('Apply Now')",
        "[class*='apply-button']",
    )
    _APPLIED_MARKERS = ("application sent", "already applied", "applied on")

    def open_form(self, page: Page, job: Job) -> None:
        # Navigate only. Clicking anything here would submit.
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3_000)

    def form_root(self) -> str | None:
        return "form, [class*='application'], [role='dialog']"

    def already_applied(self, page: Page) -> bool:
        try:
            text = page.inner_text("body", timeout=5_000).lower()
        except Exception:
            return False
        return any(marker in text for marker in self._APPLIED_MARKERS)

    def submit_form(self, page: Page) -> bool:
        for selector in self._APPLY:
            locator = page.locator(selector)
            if not locator.count():
                continue
            try:
                if locator.first.is_enabled():
                    locator.first.click()
                    page.wait_for_timeout(3_500)
                    return True
            except Exception:
                continue
        return False

    def verify_submitted(self, page: Page) -> bool:
        return self.already_applied(page) or super().verify_submitted(page)


# ----------------------------------------------------------------------
# Cutshort
# ----------------------------------------------------------------------


class CutshortApplier(FormApplier):
    """Cutshort. Like Instahyre, applying can be a single click, so the Apply
    control is treated as a submission and never touched during a dry run.

    Postings gated behind a timed assessment are handed back as manual rather
    than started — abandoning one part-way can count against you.
    """

    name = "cutshort"
    allows_empty_form = True

    _APPLY = ("button:has-text('Apply')", "a:has-text('Apply')", "[class*='apply-btn']")
    _APPLIED_MARKERS = ("application sent", "already applied", "you have applied", "applied on")

    def open_form(self, page: Page, job: Job) -> None:
        # Navigate only — see InstahyreApplier for why nothing is clicked here.
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3_000)

        try:
            body = page.inner_text("body", timeout=5_000).lower()
        except Exception:
            body = ""
        if any(marker in body for marker in ("start assessment", "take the test", "coding test")):
            raise RuntimeError("posting requires an assessment — do this one yourself")

    def form_root(self) -> str | None:
        return "form, [class*='application'], [role='dialog']"

    def already_applied(self, page: Page) -> bool:
        try:
            text = page.inner_text("body", timeout=5_000).lower()
        except Exception:
            return False
        return any(marker in text for marker in self._APPLIED_MARKERS)

    def submit_form(self, page: Page) -> bool:
        for selector in self._APPLY:
            locator = page.locator(selector)
            if not locator.count():
                continue
            try:
                if locator.first.is_enabled():
                    locator.first.click()
                    page.wait_for_timeout(3_500)
                    return True
            except Exception:
                continue
        return False

    def verify_submitted(self, page: Page) -> bool:
        return self.already_applied(page) or super().verify_submitted(page)
