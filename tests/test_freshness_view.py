"""The freshness chips must deliver what they promise.

The chips ("last hour 29") once counted every still-open job in the window
while the table underneath them still applied the ranked view's defaults —
scored-only status, and a minimum score of 60. Unscored jobs satisfy neither,
so with scoring not yet run, every chip advertised a couple of dozen jobs and
opened a page reading "Nothing here".

The invariant these tests hold is simply: **the number on the chip equals the
number of rows you get when you click it.**
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobbot import db as db_mod
from jobbot.db import ApplyRoute, AppStatus, Job, Score, Verdict, make_fingerprint


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "fresh.db")
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionFactory", None)
    db_mod.init_db(f"sqlite:///{tmp_path / 'fresh.db'}")

    from jobbot.web.app import app

    with TestClient(app, follow_redirects=False) as client:
        yield client


def add_job(
    title: str,
    *,
    minutes_ago: int | None,
    status: AppStatus = AppStatus.NEW,
    score: int | None = None,
) -> int:
    posted = (
        None if minutes_ago is None else datetime.now(UTC) - timedelta(minutes=minutes_ago)
    )
    with db_mod.session_scope() as session:
        job = Job(
            source="linkedin",
            source_job_id=title,
            url=f"https://example.com/{title}",
            title=title,
            company="Acme",
            location="Bangalore",
            description="A job description with enough text to render.",
            apply_route=ApplyRoute.BOARD_NATIVE,
            fingerprint=make_fingerprint("Acme", title, "Bangalore"),
            status=status,
            posted_at=posted,
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
                    strengths=["Python"],
                    gaps=[],
                    blockers=[],
                    tailored_summary="Fits.",
                )
            )
        return job.id


def chips(body: str) -> dict[str, tuple[str, int]]:
    """Map window -> (href, promised count) as rendered on the page."""
    out = {}
    for href, count in re.findall(
        r'href="(/review\?fresh=(?:1h|24h|7d)[^"]*)"[^>]*>\s*[^<]*<strong>(\d+)</strong>', body
    ):
        window = re.search(r"fresh=(\w+)", href).group(1)
        out[window] = (href.replace("&amp;", "&"), int(count))
    return out


def rows(body: str) -> int:
    return len(re.findall(r'name="job_ids" value="\d+"', body))


def titles(body: str) -> list[str]:
    return re.findall(r'rel="noopener">([^<]+)</a>', body)


# ----------------------------------------------------------------------
# The invariant


def test_every_chip_delivers_exactly_what_it_promises_when_nothing_is_scored(client):
    """The exact scenario that broke: jobs discovered, scoring not yet run."""
    add_job("Fresh A", minutes_ago=10)
    add_job("Fresh B", minutes_ago=45)
    add_job("Yesterday", minutes_ago=60 * 20)
    add_job("Last week", minutes_ago=60 * 24 * 5)

    page = client.get("/review").text
    found = chips(page)
    assert found["1h"][1] == 2, "sanity: two jobs posted within the hour"

    for window, (href, promised) in found.items():
        delivered = rows(client.get(href).text)
        assert delivered == promised, f"{window}: chip said {promised}, page showed {delivered}"


def test_chips_agree_with_the_page_when_scores_exist_and_vary(client):
    add_job("High score", minutes_ago=10, status=AppStatus.SCORED, score=90)
    add_job("Low score", minutes_ago=10, status=AppStatus.SCORED, score=12)
    add_job("Unscored", minutes_ago=10)

    for window, (href, promised) in chips(client.get("/review").text).items():
        delivered = rows(client.get(href).text)
        assert delivered == promised, f"{window}: chip said {promised}, page showed {delivered}"


def test_a_low_scoring_fresh_job_is_still_listed(client):
    """The score threshold belongs to the ranked view, not the freshness view.

    Filtering it out here would mean a chip counting a job it refuses to show.
    """
    add_job("Weak but fresh", minutes_ago=10, status=AppStatus.SCORED, score=12)
    body = client.get("/review?fresh=1h").text
    assert "Weak but fresh" in titles(body)


def test_an_unscored_fresh_job_is_listed(client):
    """Not scored is not the same as scored badly."""
    add_job("Never scored", minutes_ago=10)
    assert "Never scored" in titles(client.get("/review?fresh=1h").text)


# ----------------------------------------------------------------------
# Window boundaries


def test_windows_select_the_right_jobs(client):
    add_job("Minutes old", minutes_ago=10)
    add_job("Hours old", minutes_ago=60 * 5)
    add_job("Days old", minutes_ago=60 * 24 * 3)
    add_job("Ancient", minutes_ago=60 * 24 * 30)

    assert titles(client.get("/review?fresh=1h").text) == ["Minutes old"]
    assert set(titles(client.get("/review?fresh=24h").text)) == {"Minutes old", "Hours old"}
    assert set(titles(client.get("/review?fresh=7d").text)) == {
        "Minutes old",
        "Hours old",
        "Days old",
    }


def test_dateless_jobs_never_appear_in_a_freshness_view(client):
    """Unknown date must not be treated as "now" — that would fake freshness."""
    add_job("No date", minutes_ago=None)
    for window in ("1h", "24h", "7d"):
        assert titles(client.get(f"/review?fresh={window}").text) == []
    assert chips(client.get("/review").text)["1h"][1] == 0


def test_freshness_view_sorts_newest_first(client):
    add_job("Older", minutes_ago=50)
    add_job("Newest", minutes_ago=2)
    add_job("Middle", minutes_ago=20)
    assert titles(client.get("/review?fresh=1h").text) == ["Newest", "Middle", "Older"]


# ----------------------------------------------------------------------
# Terminal states


@pytest.mark.parametrize("status", [AppStatus.SKIPPED, AppStatus.SUBMITTED])
def test_terminal_states_are_excluded_from_both_count_and_page(client, status):
    add_job("Still open", minutes_ago=10)
    add_job("Done", minutes_ago=10, status=status)

    href, promised = chips(client.get("/review").text)["1h"]
    assert promised == 1
    body = client.get(href).text
    assert titles(body) == ["Still open"]
    assert rows(body) == promised


def test_approved_jobs_still_count_as_open(client):
    """Approved means queued to apply — still worth seeing in a fresh view."""
    add_job("Approved", minutes_ago=10, status=AppStatus.APPROVED)
    assert chips(client.get("/review").text)["1h"][1] == 1


# ----------------------------------------------------------------------
# Explicit parameters still win


def test_an_explicit_min_score_is_respected_inside_a_freshness_view(client):
    add_job("Strong", minutes_ago=10, status=AppStatus.SCORED, score=90)
    add_job("Weak", minutes_ago=10, status=AppStatus.SCORED, score=12)
    assert titles(client.get("/review?fresh=1h&min_score=50").text) == ["Strong"]


def test_an_explicit_status_is_respected_inside_a_freshness_view(client):
    add_job("New one", minutes_ago=10, status=AppStatus.NEW)
    add_job("Scored one", minutes_ago=10, status=AppStatus.SCORED, score=90)
    assert titles(client.get("/review?fresh=1h&status=new").text) == ["New one"]


def test_the_ranked_view_keeps_its_score_threshold(client):
    """The freshness defaults must not leak into the normal review page."""
    add_job("Strong", minutes_ago=10, status=AppStatus.SCORED, score=90)
    add_job("Weak", minutes_ago=10, status=AppStatus.SCORED, score=12)
    assert titles(client.get("/review").text) == ["Strong"]


def test_an_unknown_status_does_not_500(client):
    add_job("Anything", minutes_ago=10, status=AppStatus.SCORED, score=90)
    assert client.get("/review?status=not-a-real-status").status_code == 200


# ----------------------------------------------------------------------
# Round-trip through the approve action


def test_the_fresh_filter_survives_approving_from_a_freshness_view(client):
    job_id = add_job("Fresh one", minutes_ago=10)
    body = client.get("/review?fresh=1h").text
    redirect_to = re.search(r'name="redirect_to" value="([^"]+)"', body).group(1)
    assert "fresh=1h" in redirect_to.replace("&amp;", "&")

    response = client.post(
        "/decide",
        data={"job_ids": [job_id], "action": "approve", "redirect_to": redirect_to},
    )
    assert response.status_code in (302, 303)
    assert "fresh=1h" in response.headers["location"].replace("&amp;", "&")


def test_review_badge_counts_unscored_jobs_too(client):
    """A "Review 0" badge above a page listing jobs reads as "nothing to do"."""
    add_job("Unscored", minutes_ago=10, status=AppStatus.NEW)
    add_job("Scored", minutes_ago=10, status=AppStatus.SCORED, score=90)
    add_job("Submitted", minutes_ago=10, status=AppStatus.SUBMITTED)

    body = client.get("/review").text
    badge = re.search(r'Review <span class="n">(\d+)</span>', body).group(1)
    assert badge == "2"
