"""Apply orchestration.

Caps enforced here, in order of how annoying it is to breach them:

- **per company per week** — applying to five roles at one company in a day
  reads as spam to a recruiter and costs you the company, not just the role.
- **per day overall** — bounds the blast radius of any bug.
- **circuit breaker** — a portal that starts failing repeatedly stops being
  hammered.

Dry run is the default everywhere. `submit=True` is the only thing that
clicks a submit button.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from ..browser import pacing
from ..browser.session import browser_page
from ..config import SearchConfig, load_profile
from ..db import Application, ApplyRoute, AppStatus, Job, Run, session_scope, utcnow
from ..resume import get_resume_text
from .base import ApplyOutcome
from .drafts import Drafter
from .registry import applier_for
from .resolve import resolve_route


@dataclass
class ApplyStats:
    attempted: int = 0
    submitted: int = 0
    dry_filled: int = 0
    parked: int = 0
    manual: int = 0
    failed: int = 0
    skipped_by_cap: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, outcome: ApplyOutcome) -> None:
        self.attempted += 1
        if outcome.status is AppStatus.SUBMITTED:
            if outcome.submitted:
                self.submitted += 1
            else:
                self.dry_filled += 1
        elif outcome.status is AppStatus.NEEDS_INPUT:
            self.parked += 1
        elif outcome.status is AppStatus.MANUAL:
            self.manual += 1
        else:
            self.failed += 1
            if outcome.error:
                self.errors.append(outcome.error)

    def as_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "submitted": self.submitted,
            "dry_filled": self.dry_filled,
            "parked": self.parked,
            "manual": self.manual,
            "failed": self.failed,
            "skipped_by_cap": self.skipped_by_cap,
            "errors": self.errors[:10],
        }


# --------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------


def applications_today(session) -> int:
    since = utcnow() - timedelta(days=1)
    return (
        session.execute(
            select(func.count(Application.id)).where(
                Application.submitted_at.is_not(None), Application.submitted_at >= since
            )
        ).scalar()
        or 0
    )


def applications_to_company(session, company: str, *, days: int = 7) -> int:
    since = utcnow() - timedelta(days=days)
    return (
        session.execute(
            select(func.count(Application.id))
            .join(Job, Job.id == Application.job_id)
            .where(
                Job.company == company,
                Application.submitted_at.is_not(None),
                Application.submitted_at >= since,
            )
        ).scalar()
        or 0
    )


# --------------------------------------------------------------------------


def approved_jobs(limit: int | None = None) -> list[Job]:
    with session_scope() as session:
        # Eager-load the score: these rows are detached below, and a lazy load
        # on a detached instance raises rather than querying.
        stmt = (
            select(Job)
            .options(joinedload(Job.score))
            .where(Job.status == AppStatus.APPROVED)
            .order_by(Job.id)
        )
        if limit:
            stmt = stmt.limit(limit)
        jobs = list(session.execute(stmt).unique().scalars())
        for job in jobs:
            session.expunge(job)
        return jobs


def apply_to_approved(
    search_cfg: SearchConfig,
    *,
    limit: int | None = None,
    submit: bool = False,
    headless: bool = True,
    on_event=None,
) -> ApplyStats:
    emit = on_event or (lambda msg: None)
    stats = ApplyStats()

    jobs = approved_jobs(limit)
    if not jobs:
        emit("Nothing approved to apply to. Approve some jobs at `jobbot serve`.")
        return stats

    profile = load_profile()
    missing = profile.missing_required_fields()
    if missing:
        emit("Cannot apply — config/profile.yaml is incomplete:")
        for item in missing:
            emit(f"    missing: {item}")
        return stats

    resume_text = get_resume_text(profile.resume_file())
    cap = search_cfg.apply

    emit(f"{len(jobs)} approved. Mode: {'SUBMIT' if submit else 'dry run'}.")

    # Group by source so each site uses one browser session, serially.
    by_source: dict[str, list[Job]] = {}
    for job in jobs:
        by_source.setdefault(job.source, []).append(job)

    for source, source_jobs in by_source.items():
        with session_scope() as session:
            try:
                pacing.assert_available(session, source)
            except pacing.SitePaused as exc:
                stats.errors.append(str(exc))
                emit(f"[!] {exc}")
                continue

        try:
            with browser_page(source, headless=headless) as page:
                for job in source_jobs:
                    with session_scope() as session:
                        if applications_today(session) >= cap.max_per_day:
                            stats.skipped_by_cap += 1
                            emit(f"Daily apply cap ({cap.max_per_day}) reached. Stopping.")
                            break
                        if (
                            applications_to_company(session, job.company)
                            >= cap.max_per_company_per_week
                        ):
                            stats.skipped_by_cap += 1
                            emit(f"  skip {job.company} — weekly per-company cap reached")
                            continue

                    outcome = _apply_one(
                        page, job, profile, resume_text, search_cfg, submit=submit, emit=emit
                    )
                    stats.add(outcome)
                    _persist(outcome)
                    emit(f"  {job.title} @ {job.company}: {outcome.describe()}")

                    with session_scope() as session:
                        if outcome.status is AppStatus.FAILED:
                            pacing.record_failure(
                                session, source, outcome.error, cap.failure_circuit_breaker
                            )
                        else:
                            pacing.record_success(session, source)

                    pacing.pause()
        except Exception as exc:
            stats.errors.append(f"{source}: {exc}")
            emit(f"[!] {source} failed: {exc}")

    with session_scope() as session:
        session.add(
            Run(
                kind="apply",
                finished_at=utcnow(),
                ok=stats.failed == 0,
                stats_json={**stats.as_dict(), "submit": submit},
                error="; ".join(stats.errors[:3]),
            )
        )

    return stats


def _apply_one(
    page, job: Job, profile, resume_text: str, search_cfg: SearchConfig, *, submit: bool, emit
) -> ApplyOutcome:
    # An external link we never followed — find out what's behind it now.
    if job.apply_route.needs_resolution:
        emit(f"  resolving apply link for {job.company}…")
        resolved = resolve_route(page, job)
        if resolved.ok:
            job.apply_route = resolved.route
            job.ats_type = resolved.ats_type
            job.ats_url = resolved.url
            _update_route(job.id, resolved.route, resolved.ats_type, resolved.url)
        else:
            return ApplyOutcome(
                job_id=job.id,
                status=AppStatus.MANUAL,
                method="unresolved",
                error=resolved.error or "could not identify the application portal",
            )

    applier = applier_for(job)
    if applier is None:
        return ApplyOutcome(
            job_id=job.id,
            status=AppStatus.MANUAL,
            method=job.ats_type or job.apply_route.value,
            error=f"{job.ats_type or 'this portal'} is not automated",
        )

    drafter = Drafter(
        job,
        profile,
        resume_text,
        model=search_cfg.model.scoring,
        tailored_summary=job.score.tailored_summary if job.score else "",
    )
    with session_scope() as session:
        return applier.apply(page, job, profile, session, submit=submit, draft_fn=drafter)


def _update_route(job_id: int, route: ApplyRoute, ats_type: str, url: str) -> None:
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is not None:
            job.apply_route = route
            job.ats_type = ats_type
            job.ats_url = url


def _persist(outcome: ApplyOutcome) -> None:
    with session_scope() as session:
        job = session.get(Job, outcome.job_id)
        if job is None:
            return

        application = job.application
        if application is None:
            # Column defaults are applied by the database at INSERT, so a
            # freshly constructed row still has None in Python. Flush so the
            # counters below have real integers to work with.
            application = Application(job_id=job.id, attempts=0)
            session.add(application)
            session.flush()

        application.method = outcome.method
        application.attempts = (application.attempts or 0) + 1
        application.dry_run = outcome.dry_run
        application.evidence_path = outcome.evidence_path
        application.error = outcome.error
        application.answers_json = outcome.answers
        application.pending_questions = outcome.pending

        if outcome.submitted:
            application.submitted_at = utcnow()

        # A dry run that filled everything is *not* submitted — leave it
        # approved so the real run still has something to do.
        if outcome.status is AppStatus.SUBMITTED and not outcome.submitted:
            job.status = AppStatus.APPROVED
        else:
            job.status = outcome.status
