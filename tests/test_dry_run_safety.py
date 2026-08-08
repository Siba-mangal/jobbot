"""Dry runs must never submit an application.

This exists because of a real incident: `InstahyreApplier.open_form()` clicked
the "Apply" button in order to "reach the form". On Instahyre there is no form
— that click *is* the application. `open_form` runs before the dry-run gate is
consulted, so two real applications were sent to real employers during a run
that was explicitly a dry run.

The lesson generalizes: **anything irreversible must happen in `submit_form`,
never in `open_form`.** These tests enforce that for every applier, so no new
board can reintroduce it.
"""

from __future__ import annotations

import pytest
from helpers import make_job

from jobbot.appliers.board import (
    CutshortApplier,
    InstahyreApplier,
    LinkedInEasyApplyApplier,
)
from jobbot.appliers.registry import _BY_ROUTE, _BY_SOURCE
from jobbot.config import Eligibility, Employment, Identity, Profile
from jobbot.db import AppStatus

ALL_APPLIERS = list(_BY_ROUTE.values()) + list(_BY_SOURCE.values())


@pytest.fixture()
def profile(tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("Jane Doe. Backend engineer. Python, Go, Kafka. " * 20)
    p = Profile(
        identity=Identity(
            first_name="Jane", last_name="Doe", email="j@example.com", phone="+91 99999 99999"
        ),
        employment=Employment(total_years_experience=6, notice_period_days=30, expected_ctc="26 LPA"),
        eligibility=Eligibility(authorized_to_work_in=["India"]),
    )
    p.documents.resume_path = str(resume)
    return p


class ClickRecordingPage:
    """A page that records every click and never reports an existing form."""

    def __init__(self, body: str = "Apply for this role"):
        self.url = "https://example.com/job/1"
        self.body = body
        self.clicks: list[str] = []
        self.screenshots = 0

    # -- navigation is fine ---------------------------------------------
    def goto(self, url, **_):
        self.url = url

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_selector(self, _sel, **_):
        pass

    def inner_text(self, _sel, **_):
        return self.body

    def content(self):
        return f"<html><body>{self.body}</body></html>"

    def screenshot(self, **_):
        self.screenshots += 1

    def evaluate(self, *_a, **_k):
        return []

    # -- interaction is recorded ----------------------------------------
    def locator(self, selector):
        return RecordingLocator(self, selector)

    def get_by_role(self, _role, **_kw):
        return RecordingLocator(self, "role")


class RecordingLocator:
    def __init__(self, page: ClickRecordingPage, selector: str):
        self.page = page
        self.selector = selector

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def is_enabled(self):
        return True

    def inner_text(self, **_):
        return self.page.body

    def get_attribute(self, _name):
        return None

    def click(self, **_):
        self.page.clicks.append(self.selector)

    def is_checked(self, **_):
        return False

    def uncheck(self, **_):
        pass

    def set_input_files(self, *_a, **_k):
        pass


@pytest.mark.parametrize(
    "applier_cls", ALL_APPLIERS, ids=lambda c: c.name if hasattr(c, "name") else c.__name__
)
class TestOpenFormIsInert:
    """Where the line sits, and why.

    On a **one-click board** (`allows_empty_form=True`) there is no form, so
    the "Apply" control *is* the submission. `open_form` must click nothing at
    all — this is the exact bug that sent real applications during a dry run.

    On a **form board** the Apply control merely reveals a form, and
    submission is a separate, later button that only `submit_form` presses
    (Greenhouse `#submit_app`, LinkedIn "Submit application"). Revealing
    clicks are therefore safe there.
    """

    def test_open_form_respects_the_boundary(self, applier_cls, profile):
        applier = applier_cls()
        page = ClickRecordingPage()

        try:
            applier.open_form(page, make_job())
        except Exception:
            pass  # a refusal (e.g. an assessment gate) is fine; a click is not

        if applier.allows_empty_form:
            assert page.clicks == [], (
                f"{applier.name}.open_form() clicked {page.clicks}. On a "
                "one-click board that sends a real application — dry run or "
                "not. Move the click into submit_form()."
            )
        else:
            # A form board may reveal its form here, but must not press the
            # control that actually submits.
            forbidden = [c for c in page.clicks if "submit" in c.lower()]
            assert forbidden == [], (
                f"{applier.name}.open_form() clicked {forbidden} — submission "
                "belongs in submit_form(), behind the dry-run gate."
            )


class TestOneClickBoardsAreDeclared:
    @pytest.mark.parametrize("applier_cls", [InstahyreApplier, CutshortApplier])
    def test_one_click_boards_allow_an_empty_form(self, applier_cls):
        # Without this they'd be filed as "manual" and never applied to.
        assert applier_cls().allows_empty_form is True

    def test_form_based_appliers_do_not(self):
        assert LinkedInEasyApplyApplier().allows_empty_form is False
        for cls in _BY_ROUTE.values():
            assert cls().allows_empty_form is False, cls.name


class TestDryRunOnAOneClickBoard:
    """End to end through the state machine, with no form present."""

    def _run(self, session, profile, *, submit: bool):
        applier = InstahyreApplier()
        page = ClickRecordingPage(body="Apply for this role")
        outcome = applier.apply(page, make_job(), profile, session, submit=submit)
        return page, outcome

    def test_dry_run_clicks_nothing(self, session, profile):
        page, outcome = self._run(session, profile, submit=False)
        assert page.clicks == [], f"dry run clicked {page.clicks}"
        assert outcome.submitted is False
        assert "ready to apply" in outcome.describe()

    def test_real_run_does_click(self, session, profile):
        page, outcome = self._run(session, profile, submit=True)
        assert page.clicks, "a real run must actually click Apply"

    def test_one_click_board_is_not_filed_as_manual(self, session, profile):
        _, outcome = self._run(session, profile, submit=False)
        assert outcome.status is not AppStatus.MANUAL


class TestAlreadyApplied:
    def test_a_previously_applied_job_is_not_reapplied(self, session, profile):
        applier = InstahyreApplier()
        page = ClickRecordingPage(body="Application sent!")
        outcome = applier.apply(page, make_job(), profile, session, submit=True)

        assert page.clicks == [], "must not re-apply to a job already applied to"
        assert outcome.status is AppStatus.SUBMITTED
        assert outcome.error == "already applied"

    def test_detection_is_case_insensitive(self):
        assert InstahyreApplier().already_applied(ClickRecordingPage(body="APPLICATION SENT"))

    def test_a_fresh_page_is_not_flagged(self):
        assert not InstahyreApplier().already_applied(
            ClickRecordingPage(body="Apply now to this role")
        )
