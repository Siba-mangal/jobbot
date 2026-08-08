"""Dashboard tests.

The review gate is the only thing standing between a scraped listing and a
real submitted application, so its state transitions are worth pinning down.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobbot import db as db_mod
from jobbot.db import ApplyRoute, AppStatus, Job, Score, Verdict, make_fingerprint


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "web.db")
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionFactory", None)
    db_mod.init_db(f"sqlite:///{tmp_path / 'web.db'}")

    from jobbot.web.app import app

    with TestClient(app, follow_redirects=False) as client:
        yield client


def add_job(
    company="Acme",
    title="Backend Engineer",
    *,
    route=ApplyRoute.BOARD_NATIVE,
    status=AppStatus.SCORED,
    score=85,
) -> int:
    with db_mod.session_scope() as session:
        job = Job(
            source="instahyre",
            source_job_id=f"{company}-{title}",
            url="https://example.com/1",
            title=title,
            company=company,
            location="Bangalore",
            description="A job description with enough text to render.",
            apply_route=route,
            fingerprint=make_fingerprint(company, title, "Bangalore"),
            status=status,
        )
        session.add(job)
        session.flush()
        session.add(
            Score(
                job_id=job.id,
                model="claude-opus-5",
                fit_score=score,
                verdict=Verdict.STRONG,
                strengths=["Python"],
                gaps=["no K8s"],
                blockers=[],
                tailored_summary="A good fit.",
            )
        )
        return job.id


def status_of(job_id: int) -> AppStatus:
    with db_mod.session_scope() as session:
        return session.get(Job, job_id).status


class TestPagesRender:
    def test_review_page(self, client):
        add_job()
        response = client.get("/review")
        assert response.status_code == 200
        assert "Backend Engineer" in response.text

    def test_empty_state_points_at_the_next_step(self, client):
        response = client.get("/review")
        assert "Run discover" in response.text

    def test_empty_state_explains_unscored_jobs(self, client):
        """An empty Review page with jobs sitting one step upstream is the
        most confusing state in the app — it must say so."""
        add_job(company="Unscored Co", status=AppStatus.NEW)
        with db_mod.session_scope() as session:
            job = session.query(Job).filter(Job.company == "Unscored Co").one()
            session.delete(job.score)

        text = client.get("/review").text
        assert "not scored yet" in text
        assert "review them unscored" in text  # the escape hatch

    def test_unscored_jobs_are_reviewable_without_a_score(self, client):
        # Approving without scoring is a legitimate path — it's how you test
        # applying before setting up an API key.
        job_id = add_job(status=AppStatus.NEW)
        with db_mod.session_scope() as session:
            session.delete(session.get(Job, job_id).score)

        assert "Backend Engineer" in client.get("/review?status=new&min_score=0").text
        client.post("/decide", data={"job_ids": [job_id], "action": "approve"})
        assert status_of(job_id) is AppStatus.APPROVED

    def test_needs_input_page(self, client):
        assert client.get("/needs-input").status_code == 200

    def test_manual_page(self, client):
        assert client.get("/manual").status_code == 200

    def test_score_and_reasoning_are_shown(self, client):
        add_job(score=91)
        text = client.get("/review").text
        assert "91" in text
        assert "no K8s" in text  # the gaps, so you can judge the judgment
        assert "A good fit." in text


class TestFilters:
    def test_min_score_filter_excludes_low_scores(self, client):
        add_job(company="High", score=90)
        add_job(company="Low", score=30)
        text = client.get("/review?min_score=60").text
        assert "High" in text
        assert "Low" not in text

    def test_source_filter(self, client):
        add_job(company="Acme")
        assert "Acme" in client.get("/review?source=instahyre").text
        assert "Acme" not in client.get("/review?source=cutshort").text

    def test_status_all_shows_skipped(self, client):
        add_job(company="Rejected", status=AppStatus.SKIPPED)
        assert "Rejected" not in client.get("/review").text
        assert "Rejected" in client.get("/review?status=all&min_score=0").text


class TestDecide:
    def test_approve_queues_an_automated_job(self, client):
        job_id = add_job(route=ApplyRoute.BOARD_NATIVE)
        client.post("/decide", data={"job_ids": [job_id], "action": "approve"})
        assert status_of(job_id) is AppStatus.APPROVED

    def test_skip_marks_rejected(self, client):
        job_id = add_job()
        client.post("/decide", data={"job_ids": [job_id], "action": "skip"})
        assert status_of(job_id) is AppStatus.SKIPPED

    def test_approving_a_known_manual_portal_goes_to_manual(self, client):
        # Otherwise it sits in the apply queue forever and silently never gets
        # applied to.
        job_id = add_job(route=ApplyRoute.ATS_OTHER)
        client.post("/decide", data={"job_ids": [job_id], "action": "approve"})
        assert status_of(job_id) is AppStatus.MANUAL

    def test_unresolved_external_link_still_reaches_the_apply_queue(self, client):
        # An external link from LinkedIn is often Greenhouse or Lever. Diverting
        # it to manual on sight would hand you work the bot can actually do —
        # the applier follows the link and decides.
        job_id = add_job(route=ApplyRoute.UNKNOWN)
        client.post("/decide", data={"job_ids": [job_id], "action": "approve"})
        assert status_of(job_id) is AppStatus.APPROVED

    @pytest.mark.parametrize(
        "route", [ApplyRoute.ATS_GREENHOUSE, ApplyRoute.ATS_LEVER, ApplyRoute.BOARD_NATIVE]
    )
    def test_automated_routes_reach_the_apply_queue(self, client, route):
        job_id = add_job(route=route)
        client.post("/decide", data={"job_ids": [job_id], "action": "approve"})
        assert status_of(job_id) is AppStatus.APPROVED

    def test_bulk_approve(self, client):
        ids = [add_job(company=f"Co{i}") for i in range(3)]
        client.post("/decide", data={"job_ids": ids, "action": "approve"})
        assert all(status_of(i) is AppStatus.APPROVED for i in ids)

    def test_empty_selection_is_a_no_op(self, client):
        job_id = add_job()
        response = client.post("/decide", data={"action": "approve"})
        assert response.status_code == 303
        assert status_of(job_id) is AppStatus.SCORED


class TestNeedsInput:
    def _parked_job(self) -> int:
        from jobbot.db import Application

        job_id = add_job(status=AppStatus.NEEDS_INPUT)
        with db_mod.session_scope() as session:
            session.add(
                Application(
                    job_id=job_id,
                    pending_questions=[
                        {"question": "Expected CTC?", "kind": "text", "required": True,
                         "options": [], "draft": ""},
                        {"question": "Why us?", "kind": "text", "required": True,
                         "options": [], "draft": "A draft."},
                    ],
                )
            )
        return job_id

    def test_pending_questions_are_listed(self, client):
        self._parked_job()
        text = client.get("/needs-input").text
        assert "Expected CTC?" in text
        assert "Why us?" in text

    def test_draft_is_prefilled_for_editing(self, client):
        self._parked_job()
        assert "A draft." in client.get("/needs-input").text

    def test_answering_one_question_keeps_it_parked(self, client):
        job_id = self._parked_job()
        client.post(
            "/answer", data={"job_id": job_id, "question": "Expected CTC?", "answer": "26 LPA"}
        )
        assert status_of(job_id) is AppStatus.NEEDS_INPUT

    def test_answering_the_last_question_resumes_the_application(self, client):
        job_id = self._parked_job()
        for question, answer in [("Expected CTC?", "26 LPA"), ("Why us?", "Because.")]:
            client.post("/answer", data={"job_id": job_id, "question": question, "answer": answer})
        assert status_of(job_id) is AppStatus.APPROVED

    def test_answer_enters_the_reusable_bank_as_approved(self, client):
        from sqlalchemy import select

        from jobbot.appliers.answers import normalize_question
        from jobbot.db import Answer

        job_id = self._parked_job()
        client.post(
            "/answer", data={"job_id": job_id, "question": "Expected CTC?", "answer": "26 LPA"}
        )
        with db_mod.session_scope() as session:
            row = session.execute(
                select(Answer).where(Answer.question_norm == normalize_question("Expected CTC?"))
            ).scalar_one()
            assert row.answer == "26 LPA"
            assert row.approved is True


class TestManualQueue:
    def test_manual_jobs_are_listed_with_a_link(self, client):
        add_job(company="Workday Co", route=ApplyRoute.ATS_OTHER, status=AppStatus.MANUAL)
        text = client.get("/manual").text
        assert "Workday Co" in text
        assert "https://example.com/1" in text

    def test_mark_applied_records_the_submission(self, client):
        job_id = add_job(route=ApplyRoute.ATS_OTHER, status=AppStatus.MANUAL)
        client.post("/mark-applied", data={"job_id": job_id})
        assert status_of(job_id) is AppStatus.SUBMITTED
        with db_mod.session_scope() as session:
            application = session.get(Job, job_id).application
            assert application.method == "manual"
            assert application.dry_run is False
            assert application.submitted_at is not None
