"""Discovery orchestration.

Sequencing here is deliberate and is the main thing standing between "works"
and "gets the account restricted":

1. Check the circuit breaker before touching the network.
2. Search list pages (cheap) to collect stubs.
3. Drop stubs that fail the prefilter or that we already have — *before*
   hydrating. Detail-page views are the rate-limited resource; spending one
   on a job we'd discard is the expensive mistake.
4. Hydrate survivors one at a time, pausing between, counting against the
   daily cap, and checking every page for block signals.

A failure in one site never affects another: each site gets its own browser
session, its own Run row, and its own try/except.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..browser import pacing
from ..browser.session import browser_page, has_profile, page_text
from ..config import SearchConfig, SiteConfig
from ..db import ApplyRoute, AppStatus, Job, Run, session_scope, utcnow
from .base import JobStub, NotLoggedIn, ScraperError
from .prefilter import description_rejection, stub_rejection
from .registry import get_scraper


@dataclass
class DiscoverStats:
    site: str
    seen: int = 0
    prefiltered: int = 0
    duplicates: int = 0
    hydrated: int = 0
    saved: int = 0
    rejected_after_hydrate: int = 0
    errors: list[str] = field(default_factory=list)
    stopped_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "seen": self.seen,
            "prefiltered": self.prefiltered,
            "duplicates": self.duplicates,
            "hydrated": self.hydrated,
            "saved": self.saved,
            "rejected_after_hydrate": self.rejected_after_hydrate,
            "errors": self.errors[:10],
            "stopped_reason": self.stopped_reason,
        }

    def summary(self) -> str:
        return (
            f"{self.site}: {self.saved} saved "
            f"({self.seen} seen, {self.prefiltered} prefiltered, "
            f"{self.duplicates} dupes, {self.rejected_after_hydrate} rejected)"
        )


def _known_source_ids(session: Session, site: str) -> set[str]:
    rows = session.execute(select(Job.source_job_id).where(Job.source == site)).scalars()
    return set(rows)


def _known_fingerprints(session: Session) -> dict[str, int]:
    rows = session.execute(select(Job.fingerprint, Job.id)).all()
    return {fp: job_id for fp, job_id in rows}


def discover_site(
    site: str,
    site_cfg: SiteConfig,
    search_cfg: SearchConfig,
    *,
    limit: int | None = None,
    headless: bool = True,
    on_event=None,
) -> DiscoverStats:
    """Scrape one site. Never raises for expected conditions — returns stats."""
    stats = DiscoverStats(site=site)
    emit = on_event or (lambda msg: None)

    scraper = get_scraper(site)

    if not has_profile(site):
        stats.errors.append(f"No saved session. Run: jobbot login {site}")
        stats.stopped_reason = "not_logged_in"
        return stats

    with session_scope() as session:
        run = Run(kind="discover", site=site)
        session.add(run)
        try:
            pacing.assert_available(session, site)
        except pacing.SitePaused as exc:
            stats.errors.append(str(exc))
            stats.stopped_reason = "paused"
            run.finished_at = utcnow()
            run.error = str(exc)
            run.stats_json = stats.as_dict()
            return stats

        budget = pacing.remaining_budget(session, site, site_cfg.daily_cap)
        if budget <= 0:
            stats.stopped_reason = "daily_cap"
            stats.errors.append(f"{site} daily cap already spent.")
            run.finished_at = utcnow()
            run.stats_json = stats.as_dict()
            return stats

        target = min(budget, limit) if limit else budget
        known_ids = _known_source_ids(session, site)
        known_fps = _known_fingerprints(session)

    emit(f"{site}: budget {target} detail views today")

    # --- collect stubs (cheap) ------------------------------------------
    candidates: list[JobStub] = []
    seen_fps: set[str] = set()

    try:
        with browser_page(site, headless=headless) as page:
            if not scraper.is_logged_in(page):
                raise NotLoggedIn(f"Session expired. Run: jobbot login {site}")

            for query in site_cfg.queries:
                if len(candidates) >= target:
                    break
                emit(f"{site}: searching {query.keywords!r} in {query.location or 'anywhere'}")

                for stub in scraper.search(page, query, limit=target * 3):
                    stats.seen += 1

                    if reason := stub_rejection(stub, search_cfg.prefilter):
                        stats.prefiltered += 1
                        continue
                    if stub.source_job_id in known_ids:
                        stats.duplicates += 1
                        continue
                    fp = stub.fingerprint
                    if fp in known_fps or fp in seen_fps:
                        stats.duplicates += 1
                        continue

                    seen_fps.add(fp)
                    candidates.append(stub)
                    if len(candidates) >= target:
                        break

                pacing.pause()

            emit(f"{site}: {len(candidates)} new jobs to hydrate")

            # --- hydrate survivors (expensive) --------------------------
            for stub in candidates:
                with session_scope() as session:
                    try:
                        pacing.consume_view(session, site, site_cfg.daily_cap)
                    except pacing.DailyCapReached as exc:
                        stats.stopped_reason = "daily_cap"
                        emit(str(exc))
                        break

                try:
                    detail = scraper.hydrate(page, stub)
                except Exception as exc:
                    stats.errors.append(f"hydrate {stub.url}: {exc}")
                    with session_scope() as session:
                        pacing.record_failure(
                            session, site, str(exc), search_cfg.apply.failure_circuit_breaker
                        )
                    pacing.pause()
                    continue

                stats.hydrated += 1

                blocked = pacing.block_signal(page.url, page_text(page))
                if blocked:
                    with session_scope() as session:
                        pacing.trip_breaker(session, site, blocked)
                    stats.stopped_reason = "blocked"
                    stats.errors.append(f"Block detected ({blocked}) — pausing {site} for 24h.")
                    emit(f"[!] {site} appears to have flagged us: {blocked}. Stopping.")
                    break

                if reason := description_rejection(detail.description, search_cfg.prefilter):
                    stats.rejected_after_hydrate += 1
                    _save(stub, detail, status=AppStatus.SKIPPED, skip_reason=reason)
                else:
                    _save(stub, detail, status=AppStatus.NEW)
                    stats.saved += 1
                    emit(f"  + {stub}")

                with session_scope() as session:
                    pacing.record_success(session, site)

                pacing.pause()

    except NotLoggedIn as exc:
        stats.errors.append(str(exc))
        stats.stopped_reason = "not_logged_in"
    except ScraperError as exc:
        stats.errors.append(str(exc))
        stats.stopped_reason = "scraper_error"
    except Exception as exc:  # keep one bad site from killing the run
        stats.errors.append(f"{type(exc).__name__}: {exc}")
        stats.stopped_reason = "error"
        with session_scope() as session:
            pacing.record_failure(
                session, site, str(exc), search_cfg.apply.failure_circuit_breaker
            )

    with session_scope() as session:
        run = Run(
            kind="discover",
            site=site,
            finished_at=utcnow(),
            ok=not stats.errors,
            stats_json=stats.as_dict(),
            error="; ".join(stats.errors[:3]),
        )
        session.add(run)

    return stats


def _save(stub: JobStub, detail, *, status: AppStatus, skip_reason: str = "") -> None:
    with session_scope() as session:
        fp = stub.fingerprint
        existing = session.execute(
            select(Job).where(Job.fingerprint == fp)
        ).scalar_one_or_none()

        if existing is not None:
            # Same role already known from another board. Record the sighting
            # rather than creating a second row, and keep whichever apply
            # route is actually automatable.
            sighting = {"source": stub.source, "id": stub.source_job_id, "url": stub.url}
            seen = list(existing.also_seen_on or [])
            if sighting not in seen and existing.source != stub.source:
                seen.append(sighting)
                existing.also_seen_on = seen
            if not existing.apply_route.is_automated and detail.apply_route.is_automated:
                existing.apply_route = detail.apply_route
                existing.ats_url = detail.ats_url or existing.ats_url
            return

        session.add(
            Job(
                source=stub.source,
                source_job_id=stub.source_job_id,
                url=stub.url,
                title=stub.title,
                company=stub.company,
                location=detail.location or stub.location,
                remote=stub.remote if detail.remote is None else detail.remote,
                posted_at=detail.posted_at or stub.posted_at,
                salary_raw=detail.salary_raw or stub.salary_raw,
                description=detail.description,
                apply_route=detail.apply_route or ApplyRoute.UNKNOWN,
                ats_type=detail.ats_type,
                ats_url=detail.ats_url,
                fingerprint=fp,
                also_seen_on=[],
                status=status,
                skip_reason=skip_reason,
            )
        )


def discover_all(
    search_cfg: SearchConfig,
    *,
    sites: list[str] | None = None,
    limit: int | None = None,
    headless: bool = True,
    on_event=None,
) -> list[DiscoverStats]:
    targets = sites or search_cfg.enabled_sites()
    results = []
    for site in targets:
        site_cfg = search_cfg.sites.get(site)
        if site_cfg is None:
            continue
        results.append(
            discover_site(
                site,
                site_cfg,
                search_cfg,
                limit=limit,
                headless=headless,
                on_event=on_event,
            )
        )
    return results
