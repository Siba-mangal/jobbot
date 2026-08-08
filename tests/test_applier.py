"""Apply state machine tests, with a fake Page.

Two properties are the whole point of the state machine and both are pinned
here: an application either fills completely or parks untouched, and a
submission is only ever reported when it was actually confirmed.
"""

from __future__ import annotations

from helpers import make_job

from jobbot.appliers.answers import Question
from jobbot.appliers.base import FormApplier
from jobbot.appliers.forms import FormField
from jobbot.config import Eligibility, Employment, Identity, Profile
from jobbot.db import AppStatus


def profile_with(resume, **overrides) -> Profile:
    base = Profile(
        identity=Identity(
            first_name="Jane", last_name="Doe", email="jane@example.com", phone="+91 99999 99999"
        ),
        employment=Employment(total_years_experience=6, notice_period_days=30, expected_ctc="26 LPA"),
        eligibility=Eligibility(authorized_to_work_in=["India"]),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    base.documents.resume_path = str(resume)
    return base


class FakePage:
    """Records what was filled and clicked, without a browser."""

    def __init__(self, *, body_text: str = "", url: str = "https://x/apply"):
        self.url = url
        self.body_text = body_text
        self.filled: dict[str, str] = {}
        self.submitted = False
        self.screenshots = 0

    # -- used by the applier --------------------------------------------
    def goto(self, url, **_):
        self.url = url

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_selector(self, _sel, **_):
        pass

    def inner_text(self, _sel, **_):
        return self.body_text

    def content(self):
        return f"<html><body>{self.body_text}</body></html>"

    def screenshot(self, **_):
        self.screenshots += 1

    def locator(self, _sel):
        raise AssertionError("test appliers should not reach real locators")


class RecordingApplier(FormApplier):
    """Applier with the browser bits stubbed out."""

    name = "test"

    def __init__(self, fields: list[FormField], *, submit_ok=True, confirmed=True, steps=None):
        self._fields = fields
        self._submit_ok = submit_ok
        self._confirmed = confirmed
        self._steps = steps or []
        self._step = 0
        self.fill_calls: list[tuple[str, str]] = []

    def open_form(self, page, job):
        page.goto(job.url)

    def _read(self, page):
        if self._steps:
            return self._steps[min(self._step, len(self._steps) - 1)]
        return self._fields

    def advance(self, page):
        if self._steps and self._step < len(self._steps) - 1:
            self._step += 1
            return True
        return False

    def submit_form(self, page):
        page.submitted = self._submit_ok
        return self._submit_ok

    def verify_submitted(self, page):
        return self._confirmed

    # Bypass the real DOM layer.
    def _fill_all(self, page, fields, answers, profile):
        count = 0
        for field in fields:
            if field.is_file:
                count += 1
                continue
            value = answers.get(field.question.question)
            if value is None:
                continue
            self.fill_calls.append((field.question.question, value))
            page.filled[field.question.question] = value
            count += 1
        return count

    def _capture(self, page, job, tag):
        page.screenshot()
        return f"/evidence/{job.id}-{tag}.png"


def text_field(label, *, required=True, kind="text") -> FormField:
    return FormField(
        question=Question(label, kind=kind, required=required),
        selector=f"#{label}",
        kind=kind,
        options=[],
    )


def file_field() -> FormField:
    return FormField(
        question=Question("Resume", kind="file", required=True),
        selector="#resume",
        kind="file",
        options=[],
    )


def run(applier, session, tmp_path, *, submit=False, draft_fn=None, body=""):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"x")
    page = FakePage(body_text=body)
    applier.read_fields_override = applier._read
    # Patch read_fields at the module the base class calls it from.
    import jobbot.appliers.base as base_mod

    original = base_mod.read_fields
    base_mod.read_fields = lambda p, root=None: applier._read(p)
    try:
        return page, applier.apply(
            page,
            make_job(),
            profile_with(resume),
            session,
            submit=submit,
            draft_fn=draft_fn,
        )
    finally:
        base_mod.read_fields = original


class TestFillsCompletely:
    def test_answerable_form_fills_and_dry_runs(self, session, tmp_path):
        applier = RecordingApplier([text_field("First name"), text_field("Expected CTC")])
        page, outcome = run(applier, session, tmp_path)

        assert outcome.status is AppStatus.SUBMITTED
        assert outcome.submitted is False  # dry run
        assert page.filled == {"First name": "Jane", "Expected CTC": "26 LPA"}
        assert page.submitted is False

    def test_resume_upload_counts_as_a_filled_field(self, session, tmp_path):
        applier = RecordingApplier([text_field("First name"), file_field()])
        _, outcome = run(applier, session, tmp_path)
        assert outcome.filled_count == 2

    def test_evidence_is_captured_even_on_a_dry_run(self, session, tmp_path):
        applier = RecordingApplier([text_field("First name")])
        page, outcome = run(applier, session, tmp_path)
        assert page.screenshots >= 1
        assert outcome.evidence_path


class TestParksRatherThanGuessing:
    def test_unanswerable_question_parks_the_whole_form(self, session, tmp_path):
        """The core guarantee: nothing is typed if anything is unanswerable."""
        applier = RecordingApplier(
            [text_field("First name"), text_field("Security clearance level")]
        )
        page, outcome = run(applier, session, tmp_path)

        assert outcome.status is AppStatus.NEEDS_INPUT
        assert [p["question"] for p in outcome.pending] == ["Security clearance level"]
        assert applier.fill_calls == [], "nothing may be typed when the form can't be completed"
        assert page.submitted is False

    def test_parked_form_is_never_submitted_even_with_submit_true(self, session, tmp_path):
        applier = RecordingApplier([text_field("Security clearance level")])
        page, outcome = run(applier, session, tmp_path, submit=True)
        assert outcome.status is AppStatus.NEEDS_INPUT
        assert page.submitted is False

    def test_llm_draft_parks_for_approval_rather_than_submitting(self, session, tmp_path):
        applier = RecordingApplier([text_field("Why do you want to work here?")])
        page, outcome = run(
            applier, session, tmp_path, submit=True, draft_fn=lambda q: "A drafted pitch."
        )
        assert outcome.status is AppStatus.NEEDS_INPUT
        assert outcome.pending[0]["draft"] == "A drafted pitch."
        assert page.submitted is False

    def test_optional_unanswerable_field_does_not_park(self, session, tmp_path):
        applier = RecordingApplier(
            [text_field("First name"), text_field("Referral code", required=False)]
        )
        _, outcome = run(applier, session, tmp_path)
        assert outcome.status is AppStatus.SUBMITTED


class TestSubmission:
    def test_confirmed_submission_is_reported_as_submitted(self, session, tmp_path):
        applier = RecordingApplier([text_field("First name")], confirmed=True)
        page, outcome = run(applier, session, tmp_path, submit=True)
        assert outcome.status is AppStatus.SUBMITTED
        assert outcome.submitted is True
        assert page.submitted is True

    def test_unconfirmed_submission_is_reported_as_failed(self, session, tmp_path):
        # Claiming success without confirmation means you never follow up on a
        # job you wanted. Better to flag it.
        applier = RecordingApplier([text_field("First name")], confirmed=False)
        _, outcome = run(applier, session, tmp_path, submit=True)
        assert outcome.status is AppStatus.FAILED
        assert "no confirmation" in outcome.error

    def test_missing_submit_button_goes_manual(self, session, tmp_path):
        applier = RecordingApplier([text_field("First name")], submit_ok=False)
        _, outcome = run(applier, session, tmp_path, submit=True)
        assert outcome.status is AppStatus.MANUAL
        assert "no submit button" in outcome.error


class TestNoForm:
    def test_page_without_fields_goes_manual(self, session, tmp_path):
        applier = RecordingApplier([])
        _, outcome = run(applier, session, tmp_path)
        assert outcome.status is AppStatus.MANUAL
        assert "no form fields" in outcome.error


class TestWizard:
    def test_multi_step_form_fills_every_step(self, session, tmp_path):
        applier = RecordingApplier(
            [],
            steps=[
                [text_field("First name")],
                [text_field("Expected CTC")],
                [text_field("Notice period")],
            ],
        )
        applier.max_steps = 5
        page, outcome = run(applier, session, tmp_path)

        assert outcome.status is AppStatus.SUBMITTED
        assert page.filled == {
            "First name": "Jane",
            "Expected CTC": "26 LPA",
            "Notice period": "1 month",
        }

    def test_wizard_parks_at_the_step_it_cannot_answer(self, session, tmp_path):
        applier = RecordingApplier(
            [],
            steps=[[text_field("First name")], [text_field("Security clearance level")]],
        )
        applier.max_steps = 5
        _, outcome = run(applier, session, tmp_path)

        assert outcome.status is AppStatus.NEEDS_INPUT
        # Step 1 was legitimately filled before step 2 revealed itself — that's
        # inherent to wizards, and the portal keeps the draft.
        assert outcome.answers["First name"] == "Jane"

    def test_runaway_wizard_is_bounded(self, session, tmp_path):
        applier = RecordingApplier([text_field("First name")])
        applier.max_steps = 3
        applier.advance = lambda page: True  # never terminates
        _, outcome = run(applier, session, tmp_path)
        assert outcome.status is AppStatus.FAILED
        assert "did not finish" in outcome.error
