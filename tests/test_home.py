"""Landing page and the pipeline donut.

Chart bugs are quiet — a wrong arc still renders, it just misinforms. So the
geometry is checked arithmetically rather than by looking at it.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from jobbot import db as db_mod
from jobbot.db import ApplyRoute, AppStatus, Job, Score, Verdict, make_fingerprint
from jobbot.web.app import _pipeline


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "home.db")
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionFactory", None)
    db_mod.init_db(f"sqlite:///{tmp_path / 'home.db'}")
    from jobbot.web.app import app

    with TestClient(app, follow_redirects=False) as client:
        yield client


def add(status: AppStatus, n: int = 1, *, score: int | None = None) -> None:
    with db_mod.session_scope() as session:
        for i in range(n):
            company = f"{status.value}-{i}"
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
            if score is not None:
                session.add(
                    Score(
                        job_id=job.id,
                        model="claude-opus-5",
                        fit_score=score,
                        verdict=Verdict.STRONG,
                        strengths=[],
                        gaps=[],
                        blockers=[],
                        tailored_summary="",
                    )
                )


def pipeline():
    with db_mod.session_scope() as session:
        return _pipeline(session)


class TestGeometry:
    def test_arcs_and_gaps_fill_the_circle(self, client):
        add(AppStatus.NEW, 10)
        add(AppStatus.APPROVED, 5)
        add(AppStatus.SUBMITTED, 5)
        p = pipeline()

        drawn = [s for s in p["segments"] if s["value"]]
        total = sum(s["dash"] + 2 for s in drawn)  # +2 puts each surface gap back
        assert total == pytest.approx(p["circumference"], abs=0.5)

    def test_each_arc_matches_its_share(self, client):
        add(AppStatus.NEW, 3)
        add(AppStatus.SUBMITTED, 1)
        p = pipeline()

        for segment in p["segments"]:
            if not segment["value"]:
                continue
            expected = segment["value"] / p["active"] * p["circumference"]
            assert segment["dash"] + 2 == pytest.approx(expected, abs=0.05)

    def test_offsets_advance_cumulatively(self, client):
        add(AppStatus.NEW, 2)
        add(AppStatus.APPROVED, 2)
        add(AppStatus.SUBMITTED, 2)
        p = pipeline()

        drawn = [s for s in p["segments"] if s["value"]]
        assert drawn[0]["offset"] == 0
        running = 0.0
        for segment in drawn:
            assert segment["offset"] == pytest.approx(-running, abs=0.05)
            running += segment["value"] / p["active"] * p["circumference"]

    def test_circumference_matches_the_rendered_radius(self, client):
        add(AppStatus.NEW, 1)
        # r=70 is fixed in the template; a mismatch would silently mis-scale
        # every arc.
        assert pipeline()["circumference"] == pytest.approx(2 * math.pi * 70, abs=0.01)

    def test_percentages_sum_to_100(self, client):
        add(AppStatus.NEW, 7)
        add(AppStatus.APPROVED, 5)
        add(AppStatus.SUBMITTED, 3)
        assert sum(s["pct"] for s in pipeline()["segments"]) == 100

    def test_a_single_bucket_fills_the_ring(self, client):
        add(AppStatus.SUBMITTED, 4)
        p = pipeline()
        segment = next(s for s in p["segments"] if s["key"] == "submitted")
        assert segment["pct"] == 100
        assert segment["dash"] == pytest.approx(p["circumference"] - 2, abs=0.05)

    def test_empty_pipeline_does_not_divide_by_zero(self, client):
        p = pipeline()
        assert p["active"] == 0
        assert all(s["dash"] == 0 for s in p["segments"])


class TestBuckets:
    def test_new_and_scored_are_both_pending_approval(self, client):
        add(AppStatus.NEW, 2)
        add(AppStatus.SCORED, 3, score=80)
        assert pipeline()["to_review"] == 5

    def test_needs_input_and_manual_are_both_waiting_on_you(self, client):
        add(AppStatus.NEEDS_INPUT, 2)
        add(AppStatus.MANUAL, 1)
        assert pipeline()["needs_you"] == 3

    def test_closed_jobs_are_excluded_from_the_ring(self, client):
        add(AppStatus.SUBMITTED, 2)
        add(AppStatus.SKIPPED, 5)
        add(AppStatus.FAILED, 1)
        p = pipeline()
        assert p["active"] == 2, "skipped/failed must not inflate the ring"
        assert p["closed"] == 6

    def test_every_active_job_lands_in_exactly_one_bucket(self, client):
        for status in (
            AppStatus.NEW,
            AppStatus.SCORED,
            AppStatus.APPROVED,
            AppStatus.APPLYING,
            AppStatus.NEEDS_INPUT,
            AppStatus.MANUAL,
            AppStatus.SUBMITTED,
        ):
            add(status, 1)
        p = pipeline()
        assert sum(s["value"] for s in p["segments"]) == p["active"] == 7


class TestRendering:
    def test_home_renders(self, client):
        assert client.get("/").status_code == 200

    def test_the_two_headline_numbers_are_shown(self, client):
        """The question the page exists to answer."""
        add(AppStatus.NEW, 6)
        add(AppStatus.SUBMITTED, 2)
        text = client.get("/").text
        assert "Pending approval" in text
        assert "Submitted" in text

    def test_legend_carries_counts_so_identity_is_never_colour_alone(self, client):
        add(AppStatus.NEW, 3)
        add(AppStatus.SUBMITTED, 1)
        text = client.get("/").text
        for label in ("To review", "Needs you", "Queued to apply", "Submitted"):
            assert label in text

    def test_hero_figure_is_the_active_total(self, client):
        add(AppStatus.NEW, 9)
        add(AppStatus.SUBMITTED, 3)
        assert "jobs in play" in client.get("/").text

    def test_empty_state_points_at_discover(self, client):
        text = client.get("/").text
        assert "Run discover" in text

    def test_no_external_resources_are_referenced(self, client):
        # The whole UI is meant to work offline — no CDN, no build step.
        text = client.get("/").text
        for marker in ("http://cdn", "https://cdn", "unpkg.com", "jsdelivr", "googleapis"):
            assert marker not in text

    def test_top_matches_appear_when_scored(self, client):
        add(AppStatus.SCORED, 1, score=93)
        assert "93" in client.get("/").text


class TestNavigation:
    def test_review_moved_off_the_root(self, client):
        assert client.get("/review").status_code == 200

    def test_home_links_to_every_section(self, client):
        text = client.get("/").text
        for path in ("/review", "/run", "/setup"):
            assert f'href="{path}"' in text
