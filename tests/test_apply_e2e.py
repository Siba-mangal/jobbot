"""End-to-end applier tests: real browser, real form, real fill.

The unit tests in test_applier.py stub the DOM layer, so they can't catch a
broken selector or a filled-but-not-actually-filled field. These drive
Chromium against fixture forms and assert on the resulting page state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobbot.appliers.ats.greenhouse import GreenhouseApplier
from jobbot.config import Eligibility, Employment, Identity, Links, Profile
from jobbot.db import ApplyRoute, AppStatus, Job, make_fingerprint

FIXTURES = Path(__file__).parent / "fixtures"

playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        yield browser
        browser.close()


@pytest.fixture()
def page(browser):
    page = browser.new_page()
    yield page
    page.close()


@pytest.fixture()
def profile(tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("Jane Doe. Backend engineer. Python, Go, Kafka. " * 20)
    p = Profile(
        identity=Identity(
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
            phone="+91 99999 99999",
            city="Bangalore",
            country="India",
        ),
        links=Links(linkedin="https://linkedin.com/in/jane"),
        employment=Employment(
            total_years_experience=6, notice_period_days=60, expected_ctc="26 LPA"
        ),
        eligibility=Eligibility(authorized_to_work_in=["India"], willing_to_relocate=True),
        standard_answers={
            "why do you want to work here": "Your platform work maps to my six years in Python.",
            "i agree to the privacy policy": "Yes",
        },
    )
    p.documents.resume_path = str(resume)
    return p


def job_for(fixture_name: str) -> Job:
    url = (FIXTURES / fixture_name).as_uri()
    return Job(
        id=1,
        source="instahyre",
        source_job_id="e2e",
        url=url,
        ats_url=url,
        title="Backend Engineer",
        company="Acme Corp",
        location="Bangalore",
        description="Python, Kafka, PostgreSQL.",
        apply_route=ApplyRoute.ATS_GREENHOUSE,
        fingerprint=make_fingerprint("Acme Corp", "Backend Engineer", "Bangalore"),
        status=AppStatus.APPROVED,
    )


@pytest.fixture(autouse=True)
def _evidence_to_tmp(tmp_path, monkeypatch):
    """Keep test screenshots out of the real data/evidence directory."""
    import jobbot.appliers.base as base_mod

    monkeypatch.setattr(base_mod, "EVIDENCE_DIR", tmp_path / "evidence")


class TestFullFill:
    def test_dry_run_fills_every_field_and_does_not_submit(self, page, profile, session):
        applier = GreenhouseApplier()
        outcome = applier.apply(
            page, job_for("greenhouse_like.html"), profile, session, submit=False
        )

        assert outcome.status is AppStatus.SUBMITTED
        assert outcome.submitted is False

        # Assert against the page, not the outcome — this is what a recruiter
        # would actually receive.
        assert page.input_value("#first_name") == "Jane"
        assert page.input_value("#last_name") == "Doe"
        assert page.input_value("#email") == "jane@example.com"
        assert page.input_value("#phone") == "+91 99999 99999"
        assert page.input_value("#visa") == "No"
        assert page.input_value("#years") == "6"
        assert page.is_checked("input[name='relocate'][value='Yes']")
        assert page.is_checked("#terms")

    def test_resume_is_attached(self, page, profile, session):
        GreenhouseApplier().apply(
            page, job_for("greenhouse_like.html"), profile, session, submit=False
        )
        attached = page.evaluate("document.querySelector('#resume').files.length")
        assert attached == 1

    def test_evidence_files_are_written(self, page, profile, session, tmp_path):
        outcome = GreenhouseApplier().apply(
            page, job_for("greenhouse_like.html"), profile, session, submit=False
        )
        assert Path(outcome.evidence_path).exists()
        assert Path(outcome.evidence_path).with_suffix(".html").exists()


class TestParking:
    def test_unanswerable_required_question_parks_without_typing(self, page, profile, session):
        """The invariant, verified against a real DOM."""
        outcome = GreenhouseApplier().apply(
            page, job_for("form_with_unknown_question.html"), profile, session, submit=True
        )

        assert outcome.status is AppStatus.NEEDS_INPUT
        assert [q["question"] for q in outcome.pending] == ["Security clearance level"]

        # Nothing may have been typed — not even the fields we *could* answer.
        assert page.input_value("#first_name") == ""
        assert page.input_value("#email") == ""
        assert page.input_value("#clearance") == ""


class TestSubmission:
    def test_confirmed_submission(self, page, profile, session):
        outcome = GreenhouseApplier().apply(
            page, job_for("form_with_confirmation.html"), profile, session, submit=True
        )
        assert outcome.status is AppStatus.SUBMITTED
        assert outcome.submitted is True
        assert "thank you for applying" in page.inner_text("body").lower()

    def test_form_without_confirmation_is_reported_as_failed(self, page, profile, session):
        # The fixture's submit does nothing observable. Claiming success here
        # would mean never following up on a job you wanted.
        outcome = GreenhouseApplier().apply(
            page, job_for("form_with_unknown_question.html"), profile, session, submit=True
        )
        assert outcome.status is not AppStatus.SUBMITTED
