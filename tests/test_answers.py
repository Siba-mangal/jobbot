"""Answer-resolution tests.

The invariant under test: **the bot never invents a factual answer about you.**
A wrong salary or visa answer on a real application is unrecoverable, so the
correct behaviour when the profile lacks a fact is to park the application —
never to guess, and never to let a model write it.
"""

from __future__ import annotations

import pytest

from jobbot.appliers.answers import (
    Question,
    Source,
    is_factual,
    normalize_question,
    remember_answer,
    resolve,
    resolve_all,
    resolve_from_profile,
)
from jobbot.config import Eligibility, Employment, Identity, Links, Profile


def full_profile() -> Profile:
    return Profile(
        identity=Identity(
            first_name="Jane", last_name="Doe", email="jane@example.com",
            phone="+91 98765 43210", city="Bangalore", country="India",
        ),
        links=Links(linkedin="https://linkedin.com/in/jane", github="https://github.com/jane"),
        employment=Employment(
            current_company="Acme", current_title="Backend Engineer",
            total_years_experience=6, notice_period_days=60,
            current_ctc="18 LPA", expected_ctc="26 LPA",
        ),
        eligibility=Eligibility(
            authorized_to_work_in=["India"], requires_visa_sponsorship=False,
            willing_to_relocate=True, preferred_work_mode="hybrid",
        ),
    )


class TestNormalization:
    def test_wording_variants_collapse(self):
        variants = [
            "What is your notice period?",
            "Notice period",
            "Please enter your notice period.",
            "your NOTICE PERIOD",
        ]
        assert len({normalize_question(v) for v in variants}) == 1

    def test_distinct_questions_stay_distinct(self):
        assert normalize_question("Current CTC") != normalize_question("Expected CTC")


class TestProfileFacts:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("First name", "Jane"),
            ("Last name", "Doe"),
            ("Email address", "jane@example.com"),
            ("Phone number", "+91 98765 43210"),
            ("Current company", "Acme"),
            ("Expected CTC", "26 LPA"),
            ("Current CTC", "18 LPA"),
            ("LinkedIn profile URL", "https://linkedin.com/in/jane"),
        ],
    )
    def test_reads_stated_facts(self, question, expected):
        assert resolve_from_profile(Question(question), full_profile()) == expected

    def test_years_of_experience_is_formatted_cleanly(self):
        assert resolve_from_profile(Question("Total years of experience"), full_profile()) == "6"

    def test_notice_period_rendered_in_months(self):
        assert resolve_from_profile(Question("Notice period"), full_profile()) == "2 months"

    def test_booleans_become_yes_no(self):
        p = full_profile()
        assert resolve_from_profile(Question("Do you require visa sponsorship?"), p) == "No"
        assert resolve_from_profile(Question("Are you willing to relocate?"), p) == "Yes"

    def test_unknown_question_returns_none(self):
        assert resolve_from_profile(Question("What is your favourite colour?"), full_profile()) is None

    def test_blank_profile_field_yields_nothing(self):
        # An empty expected_ctc must NOT resolve to "" — that would submit a
        # blank salary rather than asking you.
        p = full_profile()
        p.employment.expected_ctc = ""
        assert resolve_from_profile(Question("Expected CTC"), p) is None

    def test_field_hint_participates_in_matching(self):
        # Forms often label a box "*" and carry the meaning in the name attr.
        q = Question("*", field_hint="expected_ctc")
        assert resolve_from_profile(q, full_profile()) == "26 LPA"


class TestClassification:
    @pytest.mark.parametrize(
        "text",
        ["Expected CTC", "Notice period", "Do you require sponsorship?",
         "Years of experience", "Date of birth", "Gender"],
    )
    def test_factual_questions_are_flagged(self, text):
        assert is_factual(Question(text))

    @pytest.mark.parametrize(
        "text",
        ["Why do you want to work here?", "Tell us about a challenging project",
         "Cover letter", "What makes you a good fit?"],
    )
    def test_opinion_questions_are_flagged(self, text):
        assert not is_factual(Question(text))

    def test_unclassifiable_defaults_to_factual(self):
        # Safe direction: park rather than let a model write something that
        # could be a false statement about you.
        assert is_factual(Question("Blorp?"))


class TestResolution:
    def test_profile_wins(self, session):
        r = resolve(Question("Expected CTC"), full_profile(), session)
        assert r.source is Source.PROFILE
        assert r.can_submit

    def test_approved_bank_answer_is_submittable(self, session):
        remember_answer(session, "Why do you want to work here?", "Because X.", approved=True)
        session.flush()
        r = resolve(Question("Why do you want to work here?"), full_profile(), session)
        assert r.source is Source.BANK
        assert r.can_submit

    def test_unapproved_bank_answer_is_not_used(self, session):
        # A draft sitting in the bank must never leak into a submission.
        remember_answer(session, "Why do you want to work here?", "Draft text", approved=False)
        session.flush()
        r = resolve(Question("Why do you want to work here?"), full_profile(), session)
        assert r.source is not Source.BANK

    def test_llm_draft_is_never_submittable(self, session):
        r = resolve(
            Question("Why do you want to work here?"),
            full_profile(),
            session,
            draft_fn=lambda q: "A drafted answer.",
        )
        assert r.source is Source.DRAFT
        assert r.answer == "A drafted answer."
        assert not r.can_submit  # the whole point
        assert r.needs_you

    def test_missing_fact_parks_rather_than_drafting(self, session):
        """The core invariant."""
        p = full_profile()
        p.employment.expected_ctc = ""

        drafts_called = []

        def draft_fn(q):
            drafts_called.append(q.question)
            return "18 LPA"  # a model happily inventing a salary

        r = resolve(Question("Expected CTC"), p, session, draft_fn=draft_fn)

        assert r.source is Source.UNRESOLVED
        assert r.answer is None
        assert not r.can_submit
        assert drafts_called == [], "a factual question must never reach the drafter"

    def test_missing_visa_answer_parks(self, session):
        p = full_profile()
        p.eligibility.authorized_to_work_in = []
        r = resolve(
            Question("Are you legally authorized to work in the United States?"),
            p, session, draft_fn=lambda q: "Yes",
        )
        assert r.source is Source.UNRESOLVED


class TestResolveAll:
    def test_splits_ready_from_pending(self, session):
        ready, pending = resolve_all(
            [Question("First name"), Question("Expected CTC"), Question("Why us?")],
            full_profile(),
            session,
        )
        assert ready["First name"] == "Jane"
        assert ready["Expected CTC"] == "26 LPA"
        assert [p["question"] for p in pending] == ["Why us?"]

    def test_optional_unanswerable_question_is_dropped_not_parked(self, session):
        ready, pending = resolve_all(
            [Question("Referral code", required=False)], full_profile(), session
        )
        assert ready == {}
        assert pending == []

    def test_required_unanswerable_question_parks(self, session):
        ready, pending = resolve_all(
            [Question("Security clearance level", required=True)], full_profile(), session
        )
        assert pending and pending[0]["question"] == "Security clearance level"

    def test_draft_is_surfaced_for_approval_not_submitted(self, session):
        ready, pending = resolve_all(
            [Question("Why do you want to work here?")],
            full_profile(),
            session,
            draft_fn=lambda q: "Drafted pitch.",
        )
        assert ready == {}
        assert pending[0]["draft"] == "Drafted pitch."

    def test_choice_options_are_carried_through(self, session):
        ready, pending = resolve_all(
            [Question("Preferred shift", kind="choice", options=["Day", "Night"])],
            full_profile(),
            session,
        )
        assert pending[0]["options"] == ["Day", "Night"]


class TestAnswerBank:
    def test_remember_then_reuse(self, session):
        remember_answer(session, "Why do you want to work here?", "Answer A", approved=True)
        session.flush()
        # A differently-worded but equivalent question hits the same entry.
        r = resolve(Question("why DO YOU want to work here"), full_profile(), session)
        assert r.answer == "Answer A"

    def test_re_answering_updates_in_place(self, session):
        remember_answer(session, "Why us?", "Old", approved=True)
        session.flush()
        remember_answer(session, "Why us?", "New", approved=True)
        session.flush()
        assert resolve(Question("Why us?"), full_profile(), session).answer == "New"

    def test_usage_is_counted(self, session):
        row = remember_answer(session, "Why us?", "A", approved=True)
        session.flush()
        resolve(Question("Why us?"), full_profile(), session)
        resolve(Question("Why us?"), full_profile(), session)
        assert row.times_used == 2
