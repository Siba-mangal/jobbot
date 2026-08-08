"""Applications page: did it actually get submitted?

Approving a job only queues it. Without this page the answer to "was it sent?"
lives nowhere a user can see — which is exactly the question that prompted it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from jobbot import db as db_mod
from jobbot.db import (
    Application,
    ApplyRoute,
    AppStatus,
    Job,
    make_fingerprint,
    utcnow,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "apps.db")
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionFactory", None)
    db_mod.init_db(f"sqlite:///{tmp_path / 'apps.db'}")

    import jobbot.appliers.base as base_mod

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setattr(base_mod, "EVIDENCE_DIR", evidence)

    from jobbot.web.app import app

    with TestClient(app, follow_redirects=False) as client:
        client.evidence_dir = evidence
        yield client


def add_application(
    company="Acme",
    *,
    status=AppStatus.SUBMITTED,
    dry_run=False,
    submitted=True,
    error="",
    answers=None,
    pending=None,
    evidence="",
) -> int:
    with db_mod.session_scope() as session:
        job = Job(
            source="instahyre",
            source_job_id=company,
            url="https://example.com/1",
            title="Backend Engineer",
            company=company,
            location="Bangalore",
            description="A job.",
            apply_route=ApplyRoute.BOARD_NATIVE,
            fingerprint=make_fingerprint(company, "Backend Engineer", "Bangalore"),
            status=status,
        )
        session.add(job)
        session.flush()
        session.add(
            Application(
                job_id=job.id,
                method="instahyre",
                attempts=1,
                dry_run=dry_run,
                submitted_at=utcnow() if submitted else None,
                error=error,
                answers_json=answers or {},
                pending_questions=pending or [],
                evidence_path=evidence,
            )
        )
        return job.id


class TestOutcomeIsVisible:
    def test_a_sent_application_says_sent(self, client):
        add_application("SentCo", dry_run=False, submitted=True)
        text = client.get("/applications").text
        assert "SentCo" in text
        assert "Sent" in text

    def test_a_dry_run_is_clearly_not_sent(self, client):
        """The distinction that matters most — a filled form is not an
        application."""
        add_application("DryCo", status=AppStatus.APPROVED, dry_run=True, submitted=False)
        text = client.get("/applications").text
        assert "Dry run only" in text or "dry run" in text
        assert "not sent" in text.lower()

    def test_a_failed_submission_is_not_reported_as_success(self, client):
        add_application(
            "FailCo",
            status=AppStatus.FAILED,
            dry_run=False,
            submitted=False,
            error="clicked submit but saw no confirmation",
        )
        text = client.get("/applications").text
        assert "Failed" in text
        assert "no confirmation" in text

    def test_a_parked_application_points_at_needs_input(self, client):
        add_application(
            "ParkCo",
            status=AppStatus.NEEDS_INPUT,
            dry_run=True,
            submitted=False,
            pending=[{"question": "Expected CTC?", "kind": "text", "required": True}],
        )
        text = client.get("/applications").text
        assert "Waiting on you" in text
        assert "Expected CTC?" in text
        assert "/needs-input" in text

    def test_submitted_answers_are_shown(self, client):
        add_application("AnsCo", answers={"First Name": "Jane", "Expected CTC": "26 LPA"})
        text = client.get("/applications").text
        assert "26 LPA" in text

    def test_one_click_boards_explain_the_empty_answer_set(self, client):
        add_application("OneClick", answers={}, pending=[])
        assert "no form" in client.get("/applications").text


class TestCounts:
    def test_only_real_sends_are_counted(self, client):
        add_application("Real", dry_run=False, submitted=True)
        add_application("Dry", status=AppStatus.APPROVED, dry_run=True, submitted=False)
        text = client.get("/applications").text
        assert "<strong>1</strong> actually sent" in text

    def test_approved_but_never_attempted_is_surfaced(self, client):
        # The state behind "I approved it, now what?"
        with db_mod.session_scope() as session:
            session.add(
                Job(
                    source="instahyre",
                    source_job_id="queued",
                    url="https://example.com/2",
                    title="Backend Engineer",
                    company="QueuedCo",
                    location="Bangalore",
                    description="x",
                    apply_route=ApplyRoute.BOARD_NATIVE,
                    fingerprint=make_fingerprint("QueuedCo", "Backend Engineer", "Bangalore"),
                    status=AppStatus.APPROVED,
                )
            )
        text = client.get("/applications").text
        assert "never attempted" in text
        assert "only queues" in text


class TestFilters:
    def test_sent_filter(self, client):
        add_application("SentCo", dry_run=False, submitted=True)
        add_application("DryCo", status=AppStatus.APPROVED, dry_run=True, submitted=False)
        text = client.get("/applications?show=sent").text
        assert "SentCo" in text
        assert "DryCo" not in text

    def test_pending_filter(self, client):
        add_application("SentCo", dry_run=False, submitted=True)
        add_application("DryCo", status=AppStatus.APPROVED, dry_run=True, submitted=False)
        text = client.get("/applications?show=pending").text
        assert "DryCo" in text
        assert "SentCo" not in text


class TestEvidence:
    def test_screenshot_is_served(self, client):
        png = client.evidence_dir / "00001-shot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        add_application("EvidenceCo", evidence=str(png))

        assert "/evidence/00001-shot.png" in client.get("/applications").text
        response = client.get("/evidence/00001-shot.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    @pytest.mark.parametrize(
        "attack",
        [
            "../../etc/passwd",
            "..%2f..%2fetc%2fpasswd",
            "....//....//etc/passwd",
            "../config/profile.yaml",
            "../../data/jobs.db",
        ],
    )
    def test_path_traversal_is_blocked(self, client, attack):
        # The filename comes from a URL — untrusted input that must never
        # escape the evidence directory.
        assert client.get(f"/evidence/{attack}").status_code == 404

    def test_non_evidence_file_types_are_refused(self, client):
        secret = client.evidence_dir / "secret.yaml"
        secret.write_text("password: hunter2")
        assert client.get("/evidence/secret.yaml").status_code == 404

    def test_missing_file_is_404(self, client):
        assert client.get("/evidence/nope.png").status_code == 404


class TestApproveDoesNotUndoSubmission:
    def test_a_submitted_job_cannot_be_requeued(self, client):
        """A stray select-all must not re-send applications you already made."""
        job_id = add_application("AlreadySent", status=AppStatus.SUBMITTED, submitted=True)

        client.post("/decide", data={"job_ids": [job_id], "action": "approve"})

        with db_mod.session_scope() as session:
            assert session.get(Job, job_id).status is AppStatus.SUBMITTED

    def test_a_submitted_job_cannot_be_skipped_either(self, client):
        job_id = add_application("AlreadySent", status=AppStatus.SUBMITTED, submitted=True)
        client.post("/decide", data={"job_ids": [job_id], "action": "skip"})
        with db_mod.session_scope() as session:
            assert session.get(Job, job_id).status is AppStatus.SUBMITTED

    def test_other_jobs_in_the_same_batch_still_get_approved(self, client):
        sent = add_application("Sent", status=AppStatus.SUBMITTED, submitted=True)
        fresh = add_application(
            "Fresh", status=AppStatus.SCORED, dry_run=True, submitted=False
        )
        client.post("/decide", data={"job_ids": [sent, fresh], "action": "approve"})
        with db_mod.session_scope() as session:
            assert session.get(Job, sent).status is AppStatus.SUBMITTED
            assert session.get(Job, fresh).status is AppStatus.APPROVED


class TestOrdering:
    def test_most_recent_first(self, client):
        add_application("Older")
        add_application("Newer")
        with db_mod.session_scope() as session:
            older = session.query(Job).filter(Job.company == "Older").one()
            older.application.updated_at = utcnow() - timedelta(days=2)

        text = client.get("/applications").text
        assert text.index("Newer") < text.index("Older")
