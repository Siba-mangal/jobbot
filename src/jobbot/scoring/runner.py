"""Scoring orchestration: pick jobs, score them, persist, report."""

from __future__ import annotations

from sqlalchemy import select

from ..config import SearchConfig, load_profile
from ..db import AppStatus, Job, Run, Score, session_scope, utcnow
from ..resume import get_resume_text
from .matcher import Matcher, ScoringStats, to_score_row


def pending_jobs(limit: int | None = None, *, rescore: bool = False) -> list[Job]:
    """Jobs awaiting a score, newest first."""
    with session_scope() as session:
        stmt = select(Job).where(Job.status == AppStatus.NEW)
        if not rescore:
            stmt = stmt.where(~Job.id.in_(select(Score.job_id)))
        stmt = stmt.order_by(Job.scraped_at.desc())
        if limit:
            stmt = stmt.limit(limit)
        jobs = list(session.execute(stmt).scalars())
        # Detach so they stay usable after the session closes.
        for job in jobs:
            session.expunge(job)
        return jobs


def score_pending(
    search_cfg: SearchConfig,
    *,
    limit: int | None = None,
    force_batch: bool = False,
    force_live: bool = False,
    rescore: bool = False,
    on_event=None,
) -> ScoringStats:
    emit = on_event or (lambda msg: None)
    stats = ScoringStats()

    jobs = pending_jobs(limit, rescore=rescore)
    if not jobs:
        emit("Nothing to score.")
        return stats

    profile = load_profile()
    resume_text = get_resume_text(profile.resume_file())
    model = search_cfg.model.scoring

    matcher = Matcher(resume_text, model=model, effort=search_cfg.model.effort)

    use_batch = force_batch or (
        not force_live and len(jobs) >= search_cfg.model.batch_threshold
    )
    stats.used_batch = use_batch

    emit(
        f"Scoring {len(jobs)} job(s) with {model} "
        f"(effort={search_cfg.model.effort}, {'batch' if use_batch else 'live'})"
    )

    if use_batch:
        results = matcher.score_batch(jobs, on_event=emit)
    else:
        results = matcher.score_many(jobs, on_event=emit)

    with session_scope() as session:
        for result in results:
            stats.add(result)
            if not result.ok:
                continue
            existing = session.execute(
                select(Score).where(Score.job_id == result.job_id)
            ).scalar_one_or_none()
            if existing is not None:
                session.delete(existing)
                session.flush()
            session.add(to_score_row(result, model))

            job = session.get(Job, result.job_id)
            if job is not None:
                if result.score.is_disqualified:
                    job.status = AppStatus.SKIPPED
                    job.skip_reason = "; ".join(result.score.blockers) or "disqualified"
                else:
                    job.status = AppStatus.SCORED

        session.add(
            Run(
                kind="score",
                finished_at=utcnow(),
                ok=stats.failed == 0,
                stats_json={
                    **stats.as_dict(),
                    "model": model,
                    "estimated_cost_usd": round(stats.estimated_cost_usd(model), 4),
                },
                error="; ".join(stats.errors[:3]),
            )
        )

    return stats
