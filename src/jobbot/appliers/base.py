"""The apply state machine.

Every application, on every portal, runs through the same sequence:

    open → read form → resolve answers → [park] → fill → evidence → submit → verify

Two properties this shape guarantees, and both matter:

**All-or-nothing.** Every field is resolved to an answer *before* anything is
typed. A form with one unanswerable question parks untouched rather than
half-submitting itself.

**Evidence always.** A screenshot and the raw HTML are written to
``data/evidence/`` before the submit button is considered — on dry runs too.
When something goes wrong on a real application you want to see exactly what
was on screen, not reconstruct it.

Dry run is the default. `submit=True` is the only thing that clicks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Page
from sqlalchemy.orm import Session

from ..config import DATA_DIR, Profile
from ..db import AppStatus, Job
from .answers import resolve_all
from .forms import FormField, fill_field, read_fields, upload_resume

EVIDENCE_DIR = DATA_DIR / "evidence"


@dataclass
class ApplyOutcome:
    job_id: int
    status: AppStatus
    method: str
    submitted: bool = False
    dry_run: bool = True
    evidence_path: str = ""
    error: str = ""
    answers: dict[str, str] = field(default_factory=dict)
    pending: list[dict] = field(default_factory=list)
    filled_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (AppStatus.SUBMITTED, AppStatus.APPROVED)

    def describe(self) -> str:
        if self.status is AppStatus.SUBMITTED:
            if self.error == "already applied":
                return "already applied — skipped"
            if self.submitted:
                return f"submitted ({self.filled_count} fields)"
            if self.filled_count == 0:
                # One-click board: nothing to fill, and the Apply click is the
                # submission, so a dry run genuinely did nothing.
                return "dry run — ready to apply (one click, no form)"
            return f"dry-run filled ({self.filled_count} fields)"
        if self.status is AppStatus.NEEDS_INPUT:
            questions = ", ".join(p["question"] for p in self.pending[:2])
            return f"parked — needs: {questions}"
        if self.status is AppStatus.MANUAL:
            return f"manual — {self.error or 'portal not automated'}"
        return f"failed — {self.error}"


class ApplyError(RuntimeError):
    pass


class FormApplier:
    """Template for a portal applier.

    Subclasses supply navigation and the submit/verify specifics; the flow,
    the answer gate, and evidence capture are shared.

    `max_steps > 1` handles wizard-style forms (LinkedIn Easy Apply). Note
    that the all-or-nothing guarantee is weaker there by nature: a wizard
    doesn't reveal step 3's questions until step 2 is filled, so parking can
    happen mid-flow. Both portals with wizards save a draft in that case, so
    the work isn't lost — but it is a real difference from single-page forms,
    where nothing is typed until every answer is known.
    """

    name: str = "generic"
    max_steps: int = 1

    #: True for boards with no form at all, where clicking "Apply" *is* the
    #: whole application (your profile is already on file). Those appliers
    #: must do that click in `submit_form`, never in `open_form`.
    allows_empty_form: bool = False

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def target_url(self, job: Job) -> str:
        return job.ats_url or job.url

    def open_form(self, page: Page, job: Job) -> None:
        """Navigate to the page carrying the application form.

        **Must not perform any action that could submit the application.**
        This runs before the dry-run gate, so anything irreversible done here
        happens even when the caller asked for a dry run. Clicking a control
        labelled "Apply" is only safe when it is known to open a form rather
        than send the application — when in doubt, do it in `submit_form`.
        """
        page.goto(self.target_url(job), wait_until="domcontentloaded")
        page.wait_for_timeout(2_500)

    def already_applied(self, page: Page) -> bool:
        """Has this application already been sent?

        Checked before anything is clicked, so a re-run doesn't double-apply.
        """
        return False

    def form_root(self) -> str | None:
        """CSS selector scoping field extraction, or None for the whole page."""
        return None

    def resume_selectors(self) -> tuple[str, ...]:
        return ()

    def submit_form(self, page: Page) -> bool:
        """Click submit. Return False if no submit control was found."""
        for selector in (
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
            "button:has-text('Apply')",
        ):
            locator = page.locator(selector)
            if locator.count() and locator.first.is_enabled():
                locator.first.click()
                page.wait_for_timeout(4_000)
                return True
        return False

    def advance(self, page: Page) -> bool:
        """Move to the next step of a wizard. False when there is no next step.

        Must never click the final submit control — that's `submit_form`'s
        job, and only when `submit=True`.
        """
        return False

    def verify_submitted(self, page: Page) -> bool:
        """Did the submission actually land?"""
        text = ""
        try:
            text = page.inner_text("body", timeout=5_000).lower()
        except Exception:
            pass
        markers = (
            "thank you for applying",
            "application received",
            "application submitted",
            "successfully applied",
            "we have received your application",
            "thanks for applying",
            "your application has been",
        )
        return any(marker in text for marker in markers)

    # ------------------------------------------------------------------
    # Flow
    # ------------------------------------------------------------------

    def apply(
        self,
        page: Page,
        job: Job,
        profile: Profile,
        session: Session,
        *,
        submit: bool = False,
        draft_fn=None,
    ) -> ApplyOutcome:
        outcome = ApplyOutcome(
            job_id=job.id, status=AppStatus.FAILED, method=self.name, dry_run=not submit
        )

        try:
            self.open_form(page, job)
        except Exception as exc:
            outcome.error = f"could not open the form: {exc}"
            return outcome

        # Don't apply twice. Checked before any click, so re-running after a
        # crash is safe.
        if self.already_applied(page):
            outcome.status = AppStatus.SUBMITTED
            outcome.submitted = True
            outcome.error = "already applied"
            outcome.evidence_path = self._capture(page, job, "already-applied")
            return outcome

        # --- walk the form, one step for a normal page, N for a wizard ----
        all_answers: dict[str, str] = {}
        saw_any_field = False

        for step in range(self.max_steps):
            fields = read_fields(page, self.form_root())
            if not fields:
                if step == 0 and not self.allows_empty_form:
                    outcome.status = AppStatus.MANUAL
                    outcome.error = "no form fields found on the page"
                    outcome.evidence_path = self._capture(page, job, "no-form")
                    return outcome
                break  # one-click board, or a wizard review step

            saw_any_field = True

            # The gate: resolve every field on this step before typing any of it.
            questions = [f.question for f in fields if not f.is_file]
            ready, pending = resolve_all(questions, profile, session, draft_fn=draft_fn)

            if pending:
                outcome.status = AppStatus.NEEDS_INPUT
                outcome.answers = {**all_answers, **ready}
                outcome.pending = pending
                outcome.evidence_path = self._capture(page, job, "parked")
                return outcome

            try:
                outcome.filled_count += self._fill_all(page, fields, ready, profile)
            except Exception as exc:
                outcome.error = f"filling failed: {exc}"
                outcome.evidence_path = self._capture(page, job, "fill-error")
                return outcome

            all_answers.update(ready)

            if not self.advance(page):
                break
        else:
            outcome.error = f"form did not finish within {self.max_steps} steps"
            outcome.evidence_path = self._capture(page, job, "too-many-steps")
            return outcome

        if not saw_any_field and not self.allows_empty_form:
            outcome.status = AppStatus.MANUAL
            outcome.error = "no form fields found on the page"
            outcome.evidence_path = self._capture(page, job, "no-form")
            return outcome

        outcome.answers = all_answers
        outcome.evidence_path = self._capture(page, job, "filled")

        # --- submit ------------------------------------------------------
        if not submit:
            outcome.status = AppStatus.SUBMITTED
            outcome.submitted = False
            return outcome

        try:
            clicked = self.submit_form(page)
        except Exception as exc:
            outcome.error = f"submit failed: {exc}"
            return outcome

        if not clicked:
            outcome.status = AppStatus.MANUAL
            outcome.error = "form filled but no submit button found"
            return outcome

        outcome.evidence_path = self._capture(page, job, "submitted")

        if self.verify_submitted(page):
            outcome.status = AppStatus.SUBMITTED
            outcome.submitted = True
        else:
            # The click landed but we can't prove the application did. Say so
            # rather than claiming success — a false "submitted" means you
            # never follow up on a job you wanted.
            outcome.status = AppStatus.FAILED
            outcome.error = "clicked submit but saw no confirmation — check the evidence screenshot"
        return outcome

    # ------------------------------------------------------------------

    def _fill_all(
        self, page: Page, fields: list[FormField], answers: dict[str, str], profile: Profile
    ) -> int:
        filled = 0
        for field_ in fields:
            if field_.is_file:
                if upload_resume(page, profile.resume_file(), self.resume_selectors()):
                    filled += 1
                continue

            value = answers.get(field_.question.question)
            if value is None:
                continue
            fill_field(page, field_, value)
            filled += 1
            page.wait_for_timeout(250)  # let any dependent fields react
        return filled

    def _capture(self, page: Page, job: Job, tag: str) -> str:
        """Screenshot + HTML. Never raises — evidence failing must not fail
        the application."""
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        stem = EVIDENCE_DIR / f"{job.id:05d}-{stamp}-{tag}"
        try:
            page.screenshot(path=f"{stem}.png", full_page=True)
        except Exception:
            pass
        try:
            Path(f"{stem}.html").write_text(page.content())
        except Exception:
            pass
        return f"{stem}.png"
